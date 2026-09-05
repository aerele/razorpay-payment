# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for Razorpay webhook verification, dispatch, and sweep (no live API calls).

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from razorpay_payment.gateway.reconciliation import route_event, sweep_pending
from razorpay_payment.gateway.webhooks import construct_event, handle_event


def _build_event(
	event_id="evt_001",
	event_type="payment.captured",
	payment_id="pay_001",
	reference_doctype="Payment Request",
	reference_docname="PR-001",
):
	"""Build a mock Razorpay webhook payload."""
	return {
		"entity": "event",
		"event": event_type,
		"id": event_id,
		"payload": {
			"payment": {
				"entity": {
					"id": payment_id,
					"status": "captured",
					"notes": {
						"reference_doctype": reference_doctype,
						"reference_docname": reference_docname,
					},
					"error_description": None,
				}
			}
		},
	}


class TestRazorpayWebhooks(FrappeTestCase):
	def test_construct_event_rejects_bad_signature(self):
		"""A bad/absent signature must return None before any handler runs."""
		with (
			patch("razorpay_payment.gateway.webhooks.get_webhook_secret", return_value="secret"),
			patch("razorpay_payment.gateway.webhooks.verify_webhook_signature", return_value=False),
		):
			result = construct_event(b"{}", "bad_sig")
		self.assertIsNone(result)

	def test_construct_event_rejects_missing_secret(self):
		"""A missing webhook secret must return None (fail-closed)."""
		with patch("razorpay_payment.gateway.webhooks.get_webhook_secret", return_value=None):
			result = construct_event(b"{}", "sig")
		self.assertIsNone(result)

	def test_handle_event_duplicate(self):
		"""A re-delivered event with non-Failed prior status is a duplicate."""
		settings = MagicMock(name="RS")
		settings.name = "Razorpay Settings"

		with patch(
			"razorpay_payment.gateway.webhooks.frappe.db.get_value",
			return_value=frappe._dict(name="LOG-1", status="Processed"),
		):
			result = handle_event(json.dumps(_build_event()), "evt_001", settings)
		self.assertEqual(result["status"], "duplicate")

	def test_route_event_ignored_unknown(self):
		"""Unknown event types return Ignored."""
		settings = MagicMock(name="RS")
		event = _build_event(event_type="refund.created")
		result = route_event(event, settings)
		self.assertEqual(result["status_label"], "Ignored")

	def test_payment_captured_settles_payment_request(self):
		"""payment.captured calls authorize_reference to settle the PR."""
		settings = MagicMock(name="RS")
		settings.settle_payment_request = MagicMock()

		event = _build_event(event_type="payment.captured")

		with (
			# 1st call (duplicate guard): False. 2nd call (verify PE created): True.
			patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", side_effect=[False, True]),
			patch("razorpay_payment.gateway.reconciliation.frappe.get_doc") as get_doc,
		):
			pr = MagicMock(name="PR")
			pr.doctype = "Payment Request"
			# MagicMock auto-creates attributes, so hasattr returns True. We must
			# configure the spec to prevent auto-creation so `hasattr(ref, "on_payment_authorized")` is False.
			pr.on_payment_authorized = None  # Setting to None makes hasattr return False if spec is set
			get_doc.return_value = pr

			# Patch the global hasattr used inside the handler to return False
			import builtins

			original_hasattr = builtins.hasattr

			def mock_hasattr(obj, name):
				if name == "on_payment_authorized":
					return False
				return original_hasattr(obj, name)

			with patch("builtins.hasattr", side_effect=mock_hasattr):
				result = route_event(event, settings)

		settings.settle_payment_request.assert_called_once_with(pr, payment_id="pay_001")
		self.assertEqual(result["status_label"], "Processed")

	def test_payment_captured_fails_if_pe_not_created(self):
		"""If settlement doesn't produce a PE, the handler must throw (marking it Failed)."""
		settings = MagicMock(name="RS")
		settings.settle_payment_request = MagicMock()

		event = _build_event(event_type="payment.captured")

		with (
			# 1st call (duplicate guard): False. 2nd call (verify PE created): False.
			patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", side_effect=[False, False]),
			patch("razorpay_payment.gateway.reconciliation.frappe.get_doc") as get_doc,
		):
			pr = MagicMock(name="PR")
			pr.doctype = "Payment Request"
			pr.on_payment_authorized = None

			get_doc.return_value = pr

			import builtins

			original_hasattr = builtins.hasattr

			def mock_hasattr(obj, name):
				if name == "on_payment_authorized":
					return False
				return original_hasattr(obj, name)

			with patch("builtins.hasattr", side_effect=mock_hasattr):
				with self.assertRaises(frappe.ValidationError) as ctx:
					route_event(event, settings)
		self.assertIn("Settlement failed", str(ctx.exception))

	def test_payment_captured_ignores_duplicate(self):
		"""Second webhook for same payment_id → Ignored (ledger dedupe)."""
		settings = MagicMock(name="RS")
		event = _build_event(event_type="payment.captured")

		with patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", return_value=True):
			result = route_event(event, settings)

		settings.settle_payment_request.assert_not_called()
		self.assertEqual(result["status_label"], "Ignored")

	def test_payment_failed_adds_comment(self):
		"""payment.failed adds a comment to the PR."""
		settings = MagicMock(name="RS")
		event = _build_event(event_type="payment.failed")
		event["payload"]["payment"]["entity"]["error_description"] = "Insufficient funds"

		with patch("razorpay_payment.gateway.reconciliation.frappe.get_doc") as get_doc:
			pr = MagicMock(name="PR")
			get_doc.return_value = pr

			result = route_event(event, settings)

		pr.add_comment.assert_called_once()
		self.assertEqual(result["status_label"], "Processed")
		args, _ = pr.add_comment.call_args
		self.assertIn("Insufficient funds", args[1])

	def test_sweep_retries_failed_events(self):
		"""sweep_pending retries Failed events and increments retry_count."""
		failed_row = MagicMock()
		failed_row.name = "LOG-1"
		failed_row.razorpay_settings = "Razorpay Settings"
		failed_row.payload = json.dumps(_build_event())
		failed_row.retry_count = 0

		settings = MagicMock(name="RS")

		with (
			patch("razorpay_payment.gateway.reconciliation.frappe.get_all", return_value=[failed_row]),
			patch("razorpay_payment.gateway.reconciliation.frappe.get_doc", return_value=settings),
			patch("razorpay_payment.gateway.reconciliation.route_event", side_effect=Exception("DB down")),
			patch("razorpay_payment.gateway.reconciliation.frappe.db.savepoint"),
			patch("razorpay_payment.gateway.reconciliation.frappe.db.rollback"),
			patch("razorpay_payment.gateway.reconciliation.frappe.log_error"),
			patch("razorpay_payment.gateway.reconciliation._apply_sweep_status") as apply_status,
		):
			sweep_pending()

		apply_status.assert_called_once()
		failed, _processed = apply_status.call_args[0]
		self.assertIn("LOG-1", failed)
