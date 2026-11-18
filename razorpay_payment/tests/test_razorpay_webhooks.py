# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT
#
# Unit tests for Razorpay webhook verification, dispatch, and sweep (no live
# API calls). Dedupe and the retryable Failed state live in payment_core's
# shared Payment Webhook Log; the ERPNext settlement branch of
# payment.captured runs only on ERPNext sites (table-gated below).

import json
from unittest import skipUnless
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from payment_core.webhooks import DUPLICATE

from razorpay_payment.gateway.reconciliation import route_event, sweep_pending
from razorpay_payment.gateway.webhooks import construct_event, handle_event
from razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings import (
	RazorpaySettings,
	capture_payment,
	order_payment_success,
)

ERPNext_INSTALLED = frappe.db.table_exists("Payment Entry")


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
					"order_id": "order_001",
					"amount": 10000,
					"currency": "INR",
					"notes": {
						"reference_doctype": reference_doctype,
						"reference_docname": reference_docname,
					},
					"error_description": None,
				}
			}
		},
	}


def _mock_order(reference_doctype="Payment Request", reference_docname="PR-001", amount=10000):
	"""Order entity as returned by GET /orders/{id} — server-minted notes."""
	return {
		"id": "order_001",
		"amount": amount,
		"currency": "INR",
		"notes": {"reference_doctype": reference_doctype, "reference_docname": reference_docname},
	}


class TestRazorpayWebhooks(FrappeTestCase):
	@skipUnless(ERPNext_INSTALLED, "scheduled Payment Request settlement needs ERPNext")
	def test_authorized_callback_settles_payment_request_after_scheduled_capture(self):
		payment_request = frappe.get_doc(
			{
				"doctype": "Payment Request",
				"payment_request_type": "Inward",
				"status": "Requested",
				"grand_total": 100,
				"currency": "INR",
			}
		)
		payment_request.db_insert()
		data = {
			"reference_doctype": "Payment Request",
			"reference_docname": payment_request.name,
			"order_id": "order_CAPTURE",
			"razorpay_payment_id": "pay_CAPTURE",
			"amount": 10000,
			"currency": "INR",
		}
		integration_request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Razorpay",
				"status": "Queued",
				"data": json.dumps(data),
			}
		).insert(ignore_permissions=True)
		frappe.flags.integration_request = frappe._dict(status_code=200)
		provider_responses = [
			{
				"id": "pay_CAPTURE",
				"status": "authorized",
				"order_id": "order_CAPTURE",
				"amount": 10000,
				"currency": "INR",
			},
			{
				"id": "pay_CAPTURE",
				"status": "captured",
				"order_id": "order_CAPTURE",
				"amount": 10000,
				"currency": "INR",
			},
		]

		with (
			patch.object(RazorpaySettings, "verify_checkout_signature", return_value=True),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.make_get_request",
				side_effect=provider_responses,
			),
			patch.object(RazorpaySettings, "settle_payment_request", return_value="PE-1") as settle,
		):
			order_payment_success(
				integration_request.name,
				json.dumps({"razorpay_payment_id": "pay_CAPTURE", "razorpay_signature": "sig"}),
			)
			settle.assert_not_called()
			capture_payment()
			capture_payment()

		settle.assert_called_once()
		self.assertEqual(settle.call_args.kwargs, {"payment_id": "pay_CAPTURE"})
		self.assertEqual(
			frappe.db.get_value("Integration Request", integration_request.name, "status"), "Completed"
		)

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
			"razorpay_payment.gateway.webhooks.get_or_create_log",
			return_value=(MagicMock(name="log"), True),
		):
			result = handle_event(_build_event(), settings)
		self.assertEqual(result, DUPLICATE)

	def test_route_event_ignored_unknown(self):
		"""Unknown event types return Ignored."""
		settings = MagicMock(name="RS")
		event = _build_event(event_type="refund.created")
		result = route_event(event, settings)
		self.assertEqual(result["status_label"], "Ignored")

	@skipUnless(ERPNext_INSTALLED, "the Payment Entry settlement branch needs ERPNext")
	def test_payment_captured_settles_payment_request(self):
		"""Payment Requests always use payment_core's builder."""
		settings = MagicMock(name="RS")
		settings.settle_payment_request = MagicMock()

		event = _build_event(event_type="payment.captured")

		with (
			patch("razorpay_payment.gateway.reconciliation.make_get_request", return_value=_mock_order()),
			patch("razorpay_payment.gateway.reconciliation.get_razorpay_auth", return_value=("k", "s")),
			# 1st call (duplicate guard): False. 2nd call (verify PE created): True.
			patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", side_effect=[False, True]),
			patch("razorpay_payment.gateway.reconciliation.frappe.get_doc") as get_doc,
		):
			pr = MagicMock(name="PR")
			pr.doctype = "Payment Request"
			get_doc.return_value = pr
			result = route_event(event, settings)

		settings.settle_payment_request.assert_called_once_with(pr, payment_id="pay_001")
		self.assertEqual(result["status_label"], "Processed")

	@skipUnless(ERPNext_INSTALLED, "the Payment Entry settlement branch needs ERPNext")
	def test_payment_captured_fails_if_pe_not_created(self):
		"""If settlement doesn't produce a PE, the handler must throw (marking it Failed)."""
		settings = MagicMock(name="RS")
		settings.settle_payment_request = MagicMock()

		event = _build_event(event_type="payment.captured")

		with (
			patch("razorpay_payment.gateway.reconciliation.make_get_request", return_value=_mock_order()),
			patch("razorpay_payment.gateway.reconciliation.get_razorpay_auth", return_value=("k", "s")),
			# 1st call (duplicate guard): False. 2nd call (verify PE created): False.
			patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", side_effect=[False, False]),
			patch("razorpay_payment.gateway.reconciliation.frappe.get_doc") as get_doc,
		):
			pr = MagicMock(name="PR")
			pr.doctype = "Payment Request"
			get_doc.return_value = pr
			with self.assertRaises(frappe.ValidationError) as ctx:
				route_event(event, settings)
		self.assertIn("Settlement failed", str(ctx.exception))

	@skipUnless(ERPNext_INSTALLED, "the Payment Entry settlement branch needs ERPNext")
	def test_payment_captured_ignores_duplicate(self):
		"""Second webhook for same payment_id → Ignored (ledger dedupe)."""
		settings = MagicMock(name="RS")
		event = _build_event(event_type="payment.captured")

		with (
			patch("razorpay_payment.gateway.reconciliation.make_get_request", return_value=_mock_order()),
			patch("razorpay_payment.gateway.reconciliation.get_razorpay_auth", return_value=("k", "s")),
			patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", return_value=True),
		):
			result = route_event(event, settings)

		settings.settle_payment_request.assert_not_called()
		self.assertEqual(result["status_label"], "Ignored")

	def test_payment_failed_adds_comment(self):
		"""payment.failed adds a comment to the reference."""
		settings = MagicMock(name="RS")
		event = _build_event(event_type="payment.failed")
		event["payload"]["payment"]["entity"]["error_description"] = "Insufficient funds"

		with (
			patch("razorpay_payment.gateway.reconciliation.make_get_request", return_value=_mock_order()),
			patch("razorpay_payment.gateway.reconciliation.get_razorpay_auth", return_value=("k", "s")),
			patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", return_value=True),
			patch("razorpay_payment.gateway.reconciliation.frappe.get_doc") as get_doc,
		):
			pr = MagicMock(name="PR")
			get_doc.return_value = pr

			result = route_event(event, settings)

		pr.add_comment.assert_called_once()
		self.assertEqual(result["status_label"], "Processed")
		args, _ = pr.add_comment.call_args
		self.assertIn("Insufficient funds", args[1])

	def test_sweep_retries_failed_events(self):
		"""sweep_pending retries Failed Payment Webhook Log rows via mark_failed."""
		# a real _dict: the sweep reads row.get("error") and parses it
		failed_row = frappe._dict(
			name="PWL-1",
			payload=json.dumps(_build_event()),
			event_type="payment.captured",
			error=None,
		)

		settings = MagicMock(name="RS")

		with (
			patch("razorpay_payment.gateway.reconciliation.frappe.get_all", return_value=[failed_row]),
			patch("razorpay_payment.gateway.reconciliation.frappe.get_doc", return_value=settings),
			patch("razorpay_payment.gateway.reconciliation.route_event", side_effect=Exception("DB down")),
			patch("razorpay_payment.gateway.reconciliation.frappe.db.savepoint"),
			patch("razorpay_payment.gateway.reconciliation.frappe.db.rollback"),
			patch("razorpay_payment.gateway.reconciliation.frappe.log_error"),
			patch("razorpay_payment.gateway.reconciliation.mark_failed") as mark,
		):
			sweep_pending()

		mark.assert_called_once()
		self.assertIn("sweep attempt 1", mark.call_args[0][1])
