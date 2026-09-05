# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for Razorpay subscription reconciliation (no live API calls).

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from razorpay_payment.gateway.reconciliation import (
	_find_linked_subscription,
	route_event,
	sweep_pending,
)


def _build_subscription_event(
	event_id="evt_sub_001",
	event_type="subscription.charged",
	subscription_id="sub_001",
	payment_id="pay_001",
	erpnext_subscription="SUB-001",
	amount=50000,
	currency="INR",
	current_start=1722729600,
	current_end=1725408000,
):
	"""Build a mock Razorpay subscription webhook payload.

	amount is in minor units (50000 paise = ₹500).
	current_start/current_end are Unix timestamps for the billing period.
	"""
	return {
		"entity": "event",
		"event": event_type,
		"id": event_id,
		"payload": {
			"payment": {
				"entity": {
					"id": payment_id,
					"status": "captured",
					"subscription_id": subscription_id,
					"amount": amount,
					"currency": currency,
				}
			},
			"subscription": {
				"entity": {
					"id": subscription_id,
					"status": "active",
					"customer_id": "cust_001",
					"current_start": current_start,
					"current_end": current_end,
					"notes": {"erpnext_subscription": erpnext_subscription},
				}
			},
		},
	}


class TestRazorpaySubscriptions(FrappeTestCase):
	def test_subscription_charged_settles_sales_invoice(self):
		"""subscription.charged creates a PE against the period SI."""
		settings = MagicMock(name="RS")
		event = _build_subscription_event()

		with (
			patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", return_value=False),
			patch(
				"razorpay_payment.gateway.reconciliation._find_linked_subscription",
				return_value="SUB-001",
			),
			patch(
				"razorpay_payment.gateway.reconciliation._find_unpaid_sales_invoice",
				return_value="SI-001",
			),
			patch("razorpay_payment.gateway.reconciliation.frappe.db.get_value", return_value=500.0),
			patch("erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry") as get_pe,
		):
			result = route_event(event, settings)

		get_pe.assert_called_once_with("Sales Invoice", "SI-001", party_amount=500.0)
		self.assertEqual(get_pe.return_value.reference_no, "pay_001")
		self.assertEqual(result["status_label"], "Processed")
		self.assertEqual(result["reference_doctype"], "Sales Invoice")

	def test_subscription_charged_skips_duplicate_payment_id(self):
		"""Second webhook for same payment_id → Ignored (ledger dedupe)."""
		settings = MagicMock(name="RS")
		event = _build_subscription_event()

		with patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", return_value=True):
			result = route_event(event, settings)

		self.assertEqual(result["status_label"], "Ignored")

	def test_subscription_charged_self_heals_missing_link(self):
		"""When the custom field is empty, _find_linked_subscription fetches the
		subscription from Razorpay and re-stamps the link from its notes."""
		settings = MagicMock(name="RS")

		# Custom-field lookup misses (returns None); the Razorpay GET then returns
		# the subscription with notes.erpnext_subscription, which is re-stamped.
		heal_resp = {
			"id": "sub_001",
			"customer_id": "cust_001",
			"notes": {"erpnext_subscription": "SUB-001"},
		}

		with (
			patch(
				"razorpay_payment.gateway.reconciliation._subscription_from_razorpay_id",
				return_value=None,
			),
			patch(
				"razorpay_payment.gateway.reconciliation.make_get_request",
				return_value=heal_resp,
			),
			patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", return_value=True),
			patch("razorpay_payment.gateway.reconciliation.link_razorpay_subscription") as link_fn,
		):
			result = _find_linked_subscription("sub_001", settings)

		# Self-heal re-stamped the link before returning the resolved name.
		link_fn.assert_called_once_with("SUB-001", "sub_001")
		self.assertEqual(result, "SUB-001")

	def test_subscription_charged_generates_si_if_missing(self):
		"""If no unpaid SI exists, force-generate one via Subscription.process()."""
		settings = MagicMock(name="RS")
		event = _build_subscription_event()

		call_count = {"find": 0}

		def mock_find(erpnext_sub, *args):
			call_count["find"] += 1
			return None if call_count["find"] == 1 else "SI-002"

		sub_doc = MagicMock(name="Subscription")
		sub_doc.next_billing_period_start = "2026-01-01"  # past date — passes the future guard

		with (
			patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", return_value=False),
			patch(
				"razorpay_payment.gateway.reconciliation._find_linked_subscription",
				return_value="SUB-001",
			),
			patch(
				"razorpay_payment.gateway.reconciliation._find_unpaid_sales_invoice",
				side_effect=mock_find,
			),
			patch("razorpay_payment.gateway.reconciliation.frappe.get_doc", return_value=sub_doc),
			patch("razorpay_payment.gateway.reconciliation.frappe.db.get_value", return_value=500.0),
			patch("erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry") as get_pe,
		):
			route_event(event, settings)

		sub_doc.process.assert_called_once()
		get_pe.assert_called_once_with("Sales Invoice", "SI-002", party_amount=500.0)

	def test_subscription_activated_stamps_link(self):
		"""subscription.activated stamps razorpay_subscription_id from notes."""
		settings = MagicMock(name="RS")
		event = _build_subscription_event(event_type="subscription.activated")
		# activated has no payment entity for settlement, only the subscription.
		del event["payload"]["payment"]

		with (
			patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", return_value=True),
			patch("razorpay_payment.gateway.reconciliation.link_razorpay_subscription") as link_fn,
		):
			result = route_event(event, settings)

		link_fn.assert_called_once_with("SUB-001", "sub_001")
		self.assertEqual(result["status_label"], "Processed")
		self.assertEqual(result["reference_doctype"], "Subscription")

	def test_payment_captured_skips_subscription_payments(self):
		"""payment.captured with subscription_id → Ignored (owned by subscription.charged)."""
		settings = MagicMock(name="RS")
		event = {
			"event": "payment.captured",
			"payload": {
				"payment": {
					"entity": {
						"id": "pay_auth_001",
						"status": "captured",
						"subscription_id": "sub_001",
						"notes": {
							"reference_doctype": "Payment Request",
							"reference_docname": "PR-001",
						},
					}
				}
			},
		}

		result = route_event(event, settings)
		self.assertEqual(result["status_label"], "Ignored")

	def test_subscription_cancelled_adds_comment_only(self):
		"""subscription.cancelled adds a comment, does NOT cancel the ERPNext Subscription."""
		settings = MagicMock(name="RS")
		event = _build_subscription_event(event_type="subscription.cancelled")
		del event["payload"]["payment"]

		sub_doc = MagicMock(name="Sub")
		with (
			patch("razorpay_payment.gateway.reconciliation.frappe.get_doc", return_value=sub_doc),
			patch("razorpay_payment.gateway.reconciliation.frappe.db.exists", return_value=True),
		):
			result = route_event(event, settings)

		sub_doc.add_comment.assert_called_once()
		args, _ = sub_doc.add_comment.call_args
		self.assertIn("subscription.cancelled", args[1])
		self.assertEqual(result["status_label"], "Processed")

	def test_sweep_retries_failed_subscription_event(self):
		"""sweep_pending retries a Failed subscription.charged event."""
		failed_row = MagicMock()
		failed_row.name = "LOG-SUB-1"
		failed_row.razorpay_settings = "Razorpay Settings"
		failed_row.payload = json.dumps(_build_subscription_event())
		failed_row.retry_count = 0

		settings = MagicMock(name="RS")

		with (
			patch(
				"razorpay_payment.gateway.reconciliation.frappe.get_all",
				return_value=[failed_row],
			),
			patch("razorpay_payment.gateway.reconciliation.frappe.get_doc", return_value=settings),
			patch(
				"razorpay_payment.gateway.reconciliation.route_event",
				side_effect=Exception("DB down"),
			),
			patch("razorpay_payment.gateway.reconciliation.frappe.db.savepoint"),
			patch("razorpay_payment.gateway.reconciliation.frappe.db.rollback"),
			patch("razorpay_payment.gateway.reconciliation.frappe.log_error"),
			patch("razorpay_payment.gateway.reconciliation._apply_sweep_status") as apply_status,
		):
			sweep_pending()

		apply_status.assert_called_once()
		failed, _ = apply_status.call_args[0]
		self.assertIn("LOG-SUB-1", failed)
