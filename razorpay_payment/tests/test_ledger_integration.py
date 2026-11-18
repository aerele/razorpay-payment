# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT
#
# The shared-ledger side of the Razorpay gateway: transaction lifecycle at
# checkout (create → order id → checkout URL → submit), completion through
# webhook events, the payment_core controller contract (reconcile, refund,
# webhook signal) and webhook dedupe on the shared Payment Webhook Log.
# Everything here runs on plain-Frappe sites with payment_core's ledger; the
# ERPNext settlement paths are covered by the other suites.

import json
from unittest import skipUnless
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from razorpay_payment.gateway import ledger, reconciliation, webhooks
from razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings import RazorpaySettings

LEDGER_INSTALLED = frappe.db.table_exists("Payment Transaction")

CONTROLLER_MODULE = "razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings"
LEDGER_MODULE = "razorpay_payment.gateway.ledger"


def _make_integration_request(reference_doctype, reference_docname):
	todo_data = {
		"reference_doctype": reference_doctype,
		"reference_docname": reference_docname,
		"order_id": "order_TST",
		"amount": 10000,
		"currency": "INR",
	}
	return (
		frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Razorpay",
				"status": "Queued",
				"data": json.dumps(todo_data),
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def make_transaction(status="Draft", provider_transaction_id=None):
	"""A real ledger row pinned to a throwaway ToDo reference."""
	todo = frappe.get_doc({"doctype": "ToDo", "description": "rzp ledger test"}).insert(
		ignore_permissions=True
	)
	doc = frappe.get_doc(
		{
			"doctype": "Payment Transaction",
			"reference_doctype": "ToDo",
			"reference_docname": todo.name,
			"amount": 100,
			"currency": "INR",
			"gateway": "Razorpay:Razorpay",
			"payer_name": "Ledger Test",
			"provider_transaction_id": provider_transaction_id,
		}
	)
	doc.insert(ignore_permissions=True)
	if status != "Draft":
		ledger.sync_payment_transaction({"payment_transaction": doc.name}, status)
	return doc.name


def captured_event(transaction_name, payment_id="pay_T1"):
	return {
		"event": "payment.captured",
		"id": f"evt_{payment_id}",
		"payload": {
			"payment": {
				"entity": {
					"id": payment_id,
					"status": "captured",
					"amount": 10000,
					"currency": "INR",
					"notes": {
						"reference_doctype": "ToDo",
						"reference_docname": frappe.db.get_value(
							"Payment Transaction", transaction_name, "reference_docname"
						),
						"payment_transaction": transaction_name,
					},
				}
			}
		},
	}


@skipUnless(LEDGER_INSTALLED, "ledger integration needs the shared Payment Transaction")
class TestLedgerCheckout(FrappeTestCase):
	def test_checkout_syncs_order_and_initiates_transaction(self):
		settings = RazorpaySettings({"doctype": "Razorpay Settings"})
		with (
			patch(f"{LEDGER_MODULE}.ensure_reference_transaction", return_value="PAY-TRX-1") as ensure,
			patch(f"{LEDGER_MODULE}.update_payment_transaction") as update,
			patch(f"{CONTROLLER_MODULE}.initiate_transaction") as initiate,
			patch.object(
				RazorpaySettings,
				"create_order",
				return_value={"id": "order_TEST1", "integration_request": "IR-TEST1"},
			),
		):
			url = settings.get_payment_url(amount=100, currency="INR", title="Test")

		self.assertIn("razorpay_checkout?token=IR-TEST1", url)
		ensure.assert_called_once()
		update.assert_called_once()
		_, kwargs = update.call_args
		self.assertEqual(kwargs.get("status"), "Initiated")
		self.assertEqual(kwargs.get("provider_transaction_id"), "order_TEST1")
		self.assertEqual(kwargs.get("checkout_url"), url)
		initiate.assert_called_once_with("PAY-TRX-1")

	def test_transaction_outstanding_is_the_amount_authority(self):
		settings = RazorpaySettings({"doctype": "Razorpay Settings"})
		txn = frappe._dict(
			outstanding_amount=60.0, reference_doctype=None, reference_docname=None, currency="INR"
		)
		with (
			patch(f"{LEDGER_MODULE}.ensure_reference_transaction", return_value="PAY-TRX-2"),
			patch(f"{LEDGER_MODULE}.update_payment_transaction"),
			patch(f"{CONTROLLER_MODULE}.initiate_transaction"),
			patch.object(frappe.db, "get_value", return_value=txn),
			patch.object(
				RazorpaySettings,
				"create_order",
				**{"return_value": {"id": "order_TEST2", "integration_request": "IR-TEST2"}},
			) as create_order,
		):
			settings.get_payment_url(amount=100, currency="INR")

		self.assertEqual(create_order.call_args.kwargs.get("amount"), 60.0)


@skipUnless(LEDGER_INSTALLED, "ledger integration needs the shared Payment Transaction")
class TestLedgerCompletion(FrappeTestCase):
	def setUp(self):
		# the web request handler sets this flag when an Integration Request
		# is created; authorize_payment reads it for the response status
		frappe.flags.integration_request = frappe._dict(status_code=200)
		super().setUp()

	def tearDown(self):
		frappe.flags.integration_request = None
		super().tearDown()

	def test_webhook_payment_captured_pays_the_transaction(self):
		transaction = make_transaction(status="Initiated", provider_transaction_id="order_T1")
		settings = object.__new__(RazorpaySettings)

		with (
			# force the ledger branch: on ERPNext CI sites the Payment Entry
			# table exists and the handler would take the ERPNext path instead
			patch(
				"razorpay_payment.gateway.reconciliation.frappe.db.table_exists",
				side_effect=lambda doctype: doctype != "Payment Entry",
			),
			patch("razorpay_payment.gateway.reconciliation.settle_ledger_reference") as settle,
		):
			result = reconciliation.reconcile_payment_captured(captured_event(transaction), settings)

		self.assertEqual(result["status_label"], "Processed")
		row = frappe.db.get_value(
			"Payment Transaction", transaction, ["status", "provider_transaction_id"], as_dict=True
		)
		self.assertEqual(row.status, "Paid")
		self.assertEqual(row.provider_transaction_id, "pay_T1")
		settle.assert_called_once()

	def test_webhook_payment_failed_fails_the_transaction(self):
		transaction = make_transaction(status="Initiated")
		event = {
			"event": "payment.failed",
			"payload": {
				"payment": {
					"entity": {
						"id": "pay_T9",
						"error_description": "Card declined",
						"notes": {"payment_transaction": transaction},
					}
				}
			},
		}
		reconciliation.mark_payment_failed(event, None)

		row = frappe.db.get_value(
			"Payment Transaction", transaction, ["status", "error_message"], as_dict=True
		)
		self.assertEqual(row.status, "Failed")
		self.assertIn("Card declined", row.error_message)

	def test_reconcile_transaction_settles_from_the_order_payments(self):
		transaction = make_transaction(status="Initiated", provider_transaction_id="order_T7")
		# the real Single doc: reconcile reads its credentials before the
		# (patched) HTTP lookup
		settings = frappe.get_doc("Razorpay Settings")

		with (
			patch(
				f"{CONTROLLER_MODULE}.make_get_request",
				return_value={
					"items": [{"id": "pay_T7", "status": "captured", "amount": 10000, "currency": "INR"}]
				},
			),
			patch(f"{LEDGER_MODULE}.settle_ledger_reference") as settle,
		):
			result = settings.reconcile_transaction(transaction)

		self.assertEqual(result, {"status": "Paid"})
		row = frappe.db.get_value(
			"Payment Transaction", transaction, ["status", "provider_transaction_id"], as_dict=True
		)
		self.assertEqual(row.status, "Paid")
		self.assertEqual(row.provider_transaction_id, "pay_T7")
		settle.assert_called_once()

	def test_refund_transaction_converts_to_minor_units(self):
		transaction = make_transaction(status="Paid", provider_transaction_id="pay_T5")
		settings = object.__new__(RazorpaySettings)
		client = MagicMock()
		client.payment.refund.return_value = {"id": "rfnd_T5"}

		with patch(f"{CONTROLLER_MODULE}.get_razorpay_client", return_value=client):
			refund_id = settings.refund_transaction(transaction, 12.5)

		self.assertEqual(refund_id, "rfnd_T5")
		payment_id, payload = client.payment.refund.call_args[0]
		self.assertEqual(payment_id, "pay_T5")
		self.assertEqual(payload, {"amount": 1250})

	def test_reconcile_rejects_a_payment_for_a_different_amount(self):
		transaction = make_transaction(status="Initiated", provider_transaction_id="order_T8")
		settings = frappe.get_doc("Razorpay Settings")

		with (
			patch(
				f"{CONTROLLER_MODULE}.make_get_request",
				return_value={
					"items": [{"id": "pay_T8", "status": "captured", "amount": 9900, "currency": "INR"}]
				},
			),
			patch(f"{LEDGER_MODULE}.settle_ledger_reference") as settle,
		):
			result = settings.reconcile_transaction(transaction)

		self.assertIsNone(result)
		row = frappe.db.get_value("Payment Transaction", transaction, "status")
		self.assertEqual(row, "Initiated")
		settle.assert_not_called()

	def test_callback_updates_the_stored_transaction(self):
		"""The ledger sync reads the transaction name from the stored
		Integration Request data, not from request params (the normal page
		flow never carries it in the request)."""
		transaction = make_transaction(status="Initiated", provider_transaction_id="order_T9")
		ir = _make_integration_request(
			"ToDo", frappe.db.get_value("Payment Transaction", transaction, "reference_docname")
		)
		data = json.loads(frappe.db.get_value("Integration Request", ir, "data"))
		data["payment_transaction"] = transaction
		data["order_id"] = "order_T9"
		data["amount"] = 10000
		frappe.db.set_value("Integration Request", ir, "data", json.dumps(data), update_modified=False)

		with (
			patch.object(RazorpaySettings, "verify_checkout_signature", return_value=True),
			patch(
				f"{CONTROLLER_MODULE}.make_get_request",
				return_value={
					"id": "pay_T9",
					"status": "captured",
					"order_id": "order_T9",
					"amount": 10000,
					"currency": "INR",
				},
			),
			patch.object(RazorpaySettings, "authorize_reference", return_value=None),
		):
			from razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings import (
				order_payment_success,
			)

			order_payment_success(
				ir, json.dumps({"razorpay_payment_id": "pay_T9", "razorpay_signature": "sig"})
			)

		row = frappe.db.get_value(
			"Payment Transaction", transaction, ["status", "provider_transaction_id"], as_dict=True
		)
		self.assertEqual(row.status, "Paid")
		self.assertEqual(row.provider_transaction_id, "pay_T9")

	def test_reference_settles_exactly_once(self):
		"""The callback, webhook and success page can all land for one
		payment: the reference_authorized guard lets only the first settle."""
		transaction = make_transaction(status="Initiated")
		ir = _make_integration_request(
			"ToDo", frappe.db.get_value("Payment Transaction", transaction, "reference_docname")
		)
		controller = object.__new__(RazorpaySettings)
		controller.integration_request = frappe.get_doc("Integration Request", ir)
		controller.data = frappe._dict(
			reference_doctype="ToDo",
			reference_docname=controller.integration_request.get("data")
			and json.loads(controller.integration_request.data)["reference_docname"],
		)
		controller.flags = frappe._dict()

		# first call settles (a ToDo has no on_payment_authorized and no
		# mapping, so payment_core's settler is a harmless real no-op)
		controller.authorize_reference()
		self.assertTrue(controller._reference_already_authorized())

		# a second landing (webhook, success page) must not settle again
		with patch.object(RazorpaySettings, "settle_payment_request") as settle:
			result = controller.authorize_reference()
		self.assertIsNone(result)
		settle.assert_not_called()

	def test_refund_transaction_requires_a_payment_id(self):
		transaction = make_transaction(status="Initiated", provider_transaction_id="order_T5")
		settings = object.__new__(RazorpaySettings)
		self.assertRaises(frappe.ValidationError, settings.refund_transaction, transaction, 10)


@skipUnless(LEDGER_INSTALLED, "ledger integration needs the shared Payment Transaction")
class TestWebhookLog(FrappeTestCase):
	def test_handle_event_dedupes_on_the_shared_log(self):
		event = captured_event(make_transaction(status="Initiated"))
		with patch.object(reconciliation, "route_event", return_value={"status_label": "Processed"}):
			first = webhooks.handle_event(event, None)
			second = webhooks.handle_event(event, None)

		self.assertEqual(first, {"status": "ok"})
		self.assertEqual(second, webhooks.DUPLICATE)

	def test_is_webhook_configured_reads_the_secret(self):
		settings = object.__new__(RazorpaySettings)
		with patch(f"{CONTROLLER_MODULE}.get_webhook_secret", return_value="whsec_x"):
			self.assertTrue(settings.is_webhook_configured())
		with patch(f"{CONTROLLER_MODULE}.get_webhook_secret", return_value=None):
			self.assertFalse(settings.is_webhook_configured())
