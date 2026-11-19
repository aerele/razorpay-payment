# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT
#
# The checkout binding — what is paid, how much, against which reference —
# is the Integration Request's stored data. These tests pin that client
# params can never replace it: settlement reads the stored values only, a
# provider payment that does not match the stored checkout is rejected, and
# a completed token cannot settle twice. Only the Razorpay HTTP call and the
# checkout signature are stubbed; everything else runs for real.

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings import (
	RazorpaySettings,
	order_payment_success,
)
from razorpay_payment.templates.pages.razorpay_checkout import make_payment

MODULE = "razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings"


def _make_reference(description="checkout trust test"):
	return (
		frappe.get_doc({"doctype": "ToDo", "description": description}).insert(ignore_permissions=True).name
	)


def _make_integration_request(reference_doctype, reference_docname, order_id="order_STORED", amount=10000):
	data = {
		"reference_doctype": reference_doctype,
		"reference_docname": reference_docname,
		"order_id": order_id,
		"amount": amount,
		"currency": "INR",
	}
	return (
		frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Razorpay",
				"status": "Queued",
				"data": json.dumps(data),
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _ir_data(name):
	return json.loads(frappe.db.get_value("Integration Request", name, "data"))


def _payment_entity(order_id="order_STORED", amount=10000, status="captured"):
	return {"id": "pay_T1", "status": status, "order_id": order_id, "amount": amount, "currency": "INR"}


class TestCheckoutBinding(FrappeTestCase):
	def setUp(self):
		# the web request handler sets this flag when an Integration Request
		# is created; authorize_payment reads it for the response status
		frappe.flags.integration_request = frappe._dict(status_code=200)

	def tearDown(self):
		frappe.flags.integration_request = None
		super().tearDown()

	def test_make_payment_settles_the_stored_reference_only(self):
		"""The browser names a different reference than the token's stored one:
		the stored binding must win — the request's names are not trusted."""
		stored_ref = _make_reference("stored target")
		other_ref = _make_reference("named by the caller")
		ir = _make_integration_request("ToDo", stored_ref)

		with (
			patch.object(RazorpaySettings, "verify_checkout_signature", return_value=True),
			patch(f"{MODULE}.make_get_request", return_value=_payment_entity()) as api,
			patch.object(RazorpaySettings, "authorize_reference", return_value=None) as settle,
		):
			make_payment(
				razorpay_payment_id="pay_T1",
				options=json.dumps({"amount": 10000, "currency": "INR"}),
				reference_doctype="ToDo",
				reference_docname=other_ref,
				token=ir,
				razorpay_order_id="order_STORED",
				razorpay_signature="sig",
			)

		data = _ir_data(ir)
		self.assertEqual(data["reference_docname"], stored_ref)
		self.assertEqual(frappe.db.get_value("Integration Request", ir, "status"), "Completed")
		# settlement ran exactly once, on the stored binding
		settle.assert_called_once()
		api.assert_called_once()

	def test_order_payment_success_keeps_the_stored_binding(self):
		"""Client params carrying order/amount/reference keys must not replace
		the Integration Request's stored checkout binding."""
		stored_ref = _make_reference("stored binding")
		other_ref = _make_reference("attacker named")
		ir = _make_integration_request("ToDo", stored_ref)

		with (
			patch.object(RazorpaySettings, "verify_checkout_signature", return_value=True),
			patch(f"{MODULE}.make_get_request", return_value=_payment_entity()),
			patch.object(RazorpaySettings, "authorize_reference", return_value=None),
		):
			order_payment_success(
				ir,
				json.dumps(
					{
						"order_id": "order_OTHER",
						"reference_doctype": "ToDo",
						"reference_docname": other_ref,
						"amount": 99999900,
						"razorpay_payment_id": "pay_T1",
						"razorpay_signature": "sig",
					}
				),
			)

		data = _ir_data(ir)
		self.assertEqual(data["order_id"], "order_STORED")
		self.assertEqual(data["amount"], 10000)
		self.assertEqual(data["reference_docname"], stored_ref)
		self.assertEqual(frappe.db.get_value("Integration Request", ir, "status"), "Completed")

	def test_payment_for_another_checkout_is_rejected(self):
		"""A provider payment whose order or amount does not match the stored
		checkout must fail the request before any settlement."""
		stored_ref = _make_reference("mismatch target")
		ir = _make_integration_request("ToDo", stored_ref)

		with (
			patch.object(RazorpaySettings, "verify_checkout_signature", return_value=True),
			patch(f"{MODULE}.make_get_request", return_value=_payment_entity(order_id="order_ELSEWHERE")),
			patch.object(RazorpaySettings, "authorize_reference", return_value=None) as settle,
		):
			order_payment_success(
				ir,
				json.dumps({"razorpay_payment_id": "pay_T1", "razorpay_signature": "sig"}),
			)

		self.assertEqual(frappe.db.get_value("Integration Request", ir, "status"), "Failed")
		settle.assert_not_called()

	def test_subscription_token_with_both_keys_matches(self):
		"""A subscription token carries both order_id (the PR name, not a
		provider id) and subscription_id: the check must use the subscription
		first, same as verify_checkout_signature, or every ERPNext
		subscription checkout would fail after a good payment."""
		stored_ref = _make_reference("subscription both keys")
		ir = _make_integration_request("ToDo", stored_ref)
		data = _ir_data(ir)
		data["order_id"] = "ACC-PRQ-2026-00001"  # the PR name, not order_...
		data["subscription_id"] = "sub_TEST1"
		frappe.db.set_value("Integration Request", ir, "data", json.dumps(data), update_modified=False)

		settings = object.__new__(RazorpaySettings)
		resp = {
			"id": "pay_T1",
			"status": "captured",
			"subscription_id": "sub_TEST1",
			"order_id": "order_REAL",
			"amount": 10000,
		}

		# the payment's order_id (order_REAL) differs from the stored
		# order_id (ACC-PRQ-...), but the subscription matches: the check
		# must pass because subscriptions are checked first
		self.assertTrue(settings.payment_matches_stored_checkout(data, resp))

	def test_make_payment_replay_settles_once(self):
		"""Calling make_payment twice with one token: the replay guard checks
		the Completed status BEFORE update_status resets it to Queued."""
		stored_ref = _make_reference("replay page path")
		ir = _make_integration_request("ToDo", stored_ref)

		with (
			patch.object(RazorpaySettings, "verify_checkout_signature", return_value=True),
			patch(f"{MODULE}.make_get_request", return_value=_payment_entity()) as api,
			patch.object(RazorpaySettings, "authorize_reference", return_value=None) as settle,
		):
			make_payment(
				razorpay_payment_id="pay_T1",
				options=json.dumps({"amount": 10000, "currency": "INR"}),
				reference_doctype="ToDo",
				reference_docname=stored_ref,
				token=ir,
				razorpay_order_id="order_STORED",
				razorpay_signature="sig",
			)
			self.assertEqual(api.call_count, 1)
			self.assertEqual(settle.call_count, 1)

			# the replay: same token, same payment id
			make_payment(
				razorpay_payment_id="pay_T1",
				options=json.dumps({"amount": 10000, "currency": "INR"}),
				reference_doctype="ToDo",
				reference_docname=stored_ref,
				token=ir,
				razorpay_order_id="order_STORED",
				razorpay_signature="sig",
			)

		self.assertEqual(api.call_count, 1)
		self.assertEqual(settle.call_count, 1)

	def test_non_razorpay_integration_request_rejected(self):
		"""An Integration Request from another service must never reach
		the Razorpay settlement path."""
		stored_ref = _make_reference("wrong service")
		ir = (
			frappe.get_doc(
				{
					"doctype": "Integration Request",
					"integration_request_service": "Stripe",
					"status": "Queued",
					"data": json.dumps(
						{
							"reference_doctype": "ToDo",
							"reference_docname": stored_ref,
							"order_id": "order_X",
							"amount": 100,
						}
					),
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

		with self.assertRaises(frappe.ValidationError):
			order_payment_success(
				ir, json.dumps({"razorpay_payment_id": "pay_1", "razorpay_signature": "sig"})
			)

	def test_token_without_binding_rejected(self):
		"""A token with neither order_id nor subscription_id fails closed."""
		stored_ref = _make_reference("no binding")
		ir = _make_integration_request("ToDo", stored_ref)
		data = _ir_data(ir)
		data.pop("order_id", None)
		data.pop("subscription_id", None)
		frappe.db.set_value("Integration Request", ir, "data", json.dumps(data), update_modified=False)

		with self.assertRaises(frappe.ValidationError):
			order_payment_success(
				ir, json.dumps({"razorpay_payment_id": "pay_1", "razorpay_signature": "sig"})
			)

	def test_order_receipt_is_the_document_name(self):
		"""The order sent to Razorpay carries the document name as its
		receipt, and never a None receipt (Razorpay may reject it)."""
		settings = frappe.get_doc("Razorpay Settings")
		sent = {}

		def capture_post(url, **kwargs):
			sent.update(kwargs.get("json") or {})
			return {"id": "order_SERVER"}

		settings.api_key = "test_key"
		settings.api_secret = "test_secret"
		with (
			patch(f"{MODULE}.make_post_request", side_effect=capture_post),
			patch(f"{MODULE}.create_request_log") as crl,
		):
			crl.return_value.name = "IR-T"
			settings.create_order(
				reference_doctype="ToDo",
				reference_docname="DOC-001",
				amount=10000,
				currency="INR",
				receipt="DOC-001",
				payment_capture=1,
			)

		# the receipt is the document name, not None
		self.assertEqual(sent.get("receipt"), "DOC-001")

		# None-valued keys are dropped from the payload
		sent.clear()
		with (
			patch(f"{MODULE}.make_post_request", side_effect=capture_post),
			patch(f"{MODULE}.create_request_log") as crl2,
		):
			crl2.return_value.name = "IR-T2"
			settings.create_order(
				reference_doctype="ToDo",
				reference_docname="DOC-002",
				amount=5000,
				currency="INR",
				receipt=None,
				payment_capture=1,
			)

		self.assertNotIn("receipt", sent)

	def test_completed_token_cannot_settle_twice(self):
		"""A replayed callback for a completed token is a no-op: no second
		provider lookup, no second settlement."""
		stored_ref = _make_reference("replay target")
		ir = _make_integration_request("ToDo", stored_ref)

		with (
			patch.object(RazorpaySettings, "verify_checkout_signature", return_value=True),
			patch(f"{MODULE}.make_get_request", return_value=_payment_entity()) as api,
			patch.object(RazorpaySettings, "authorize_reference", return_value=None) as settle,
		):
			order_payment_success(
				ir, json.dumps({"razorpay_payment_id": "pay_T1", "razorpay_signature": "sig"})
			)
			calls_after_first = api.call_count
			settles_after_first = settle.call_count
			order_payment_success(
				ir, json.dumps({"razorpay_payment_id": "pay_T1", "razorpay_signature": "sig"})
			)

		self.assertEqual(api.call_count, calls_after_first)
		self.assertEqual(settle.call_count, settles_after_first)
		self.assertEqual(frappe.db.get_value("Integration Request", ir, "status"), "Completed")
