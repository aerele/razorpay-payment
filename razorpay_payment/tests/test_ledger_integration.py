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
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.desk.doctype.todo.todo import ToDo
from frappe.tests.utils import FrappeTestCase
from razorpay.errors import BadRequestError

from razorpay_payment.gateway import ledger, reconciliation, webhooks
from razorpay_payment.patches.migrate_webhook_log import execute as migrate_webhook_log
from razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings import (
	RazorpaySettings,
	capture_payment,
	order_payment_success,
)

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
					"order_id": "order_T1",
					"amount": 10000,
					"currency": "INR",
				}
			}
		},
	}


def order_for_transaction(transaction_name):
	"""Server-minted order binding returned by GET /orders/{order_id}."""
	return {
		"id": "order_T1",
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

	def test_paid_supplied_transaction_cannot_start_another_checkout(self):
		transaction = make_transaction(status="Paid", provider_transaction_id="pay_DONE")
		row = frappe.db.get_value(
			"Payment Transaction", transaction, ["reference_doctype", "reference_docname"], as_dict=True
		)
		settings = RazorpaySettings({"doctype": "Razorpay Settings"})

		with (
			patch.object(RazorpaySettings, "create_order") as create_order,
			self.assertRaises(frappe.ValidationError),
		):
			settings.get_payment_url(
				payment_transaction=transaction,
				reference_doctype=row.reference_doctype,
				reference_docname=row.reference_docname,
				amount=100,
				currency="INR",
			)

		create_order.assert_not_called()

	def test_subscription_checkout_submits_the_new_transaction(self):
		settings = RazorpaySettings({"doctype": "Razorpay Settings"})
		with (
			patch(f"{LEDGER_MODULE}.ensure_reference_transaction", return_value="PAY-TRX-SUB"),
			patch(f"{LEDGER_MODULE}.update_payment_transaction") as update,
			patch(f"{CONTROLLER_MODULE}.initiate_transaction") as initiate,
			patch(f"{CONTROLLER_MODULE}.is_subscription_reference", return_value=True),
			patch(
				f"{CONTROLLER_MODULE}.create_razorpay_subscription",
				return_value="/razorpay_checkout?token=IR-SUB",
			),
		):
			url = settings.get_payment_url(amount=100, currency="INR")

		self.assertEqual(url, "/razorpay_checkout?token=IR-SUB")
		update.assert_called_once()
		self.assertEqual(update.call_args.kwargs["status"], "Initiated")
		initiate.assert_called_once_with("PAY-TRX-SUB")


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
			patch(
				"razorpay_payment.gateway.reconciliation.make_get_request",
				return_value=order_for_transaction(transaction),
			),
			patch("razorpay_payment.gateway.reconciliation.get_razorpay_auth", return_value=("k", "s")),
		):
			result = reconciliation.reconcile_payment_captured(captured_event(transaction), settings)

		self.assertEqual(result["status_label"], "Processed")
		row = frappe.db.get_value(
			"Payment Transaction", transaction, ["status", "provider_transaction_id"], as_dict=True
		)
		self.assertEqual(row.status, "Paid")
		self.assertEqual(row.provider_transaction_id, "pay_T1")

	def test_webhook_payment_failed_fails_the_transaction(self):
		transaction = make_transaction(status="Initiated")
		event = {
			"event": "payment.failed",
			"payload": {
				"payment": {
					"entity": {
						"id": "pay_T9",
						"order_id": "order_T1",
						"error_description": "Card declined",
					}
				}
			},
		}
		with (
			patch(
				"razorpay_payment.gateway.reconciliation.make_get_request",
				return_value=order_for_transaction(transaction),
			),
			patch("razorpay_payment.gateway.reconciliation.get_razorpay_auth", return_value=("k", "s")),
		):
			reconciliation.mark_payment_failed(event, None)

		row = frappe.db.get_value(
			"Payment Transaction", transaction, ["status", "error_message"], as_dict=True
		)
		self.assertEqual(row.status, "Failed")
		self.assertIn("Card declined", row.error_message)

	def test_failed_event_does_not_write_an_error_on_a_paid_transaction(self):
		transaction = make_transaction(status="Paid", provider_transaction_id="pay_PAID")
		frappe.db.set_value("Payment Transaction", transaction, "error_message", None, update_modified=False)

		ledger.sync_payment_transaction(
			{"payment_transaction": transaction}, "Failed", error_message="late failed attempt"
		)

		row = frappe.db.get_value(
			"Payment Transaction", transaction, ["status", "error_message"], as_dict=True
		)
		self.assertEqual(row.status, "Paid")
		self.assertFalse(row.error_message)

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
		):
			result = settings.reconcile_transaction(transaction)

		self.assertEqual(result, {"status": "Paid"})
		row = frappe.db.get_value(
			"Payment Transaction", transaction, ["status", "provider_transaction_id"], as_dict=True
		)
		self.assertEqual(row.status, "Paid")
		self.assertEqual(row.provider_transaction_id, "pay_T7")

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

	def test_refund_transaction_sends_provider_idempotency(self):
		transaction = make_transaction(status="Paid", provider_transaction_id="pay_T5")
		settings = object.__new__(RazorpaySettings)
		client = MagicMock()
		client.payment.refund.return_value = {"id": "rfnd_T5"}
		key = "pc-refund-PAY-TRANSACTION-WITH-A-LONG-NAME-1-12.5"

		with patch(f"{CONTROLLER_MODULE}.get_razorpay_client", return_value=client):
			refund_id = settings.refund_transaction(transaction, 12.5, idempotency_key=key)

		self.assertEqual(refund_id, "rfnd_T5")
		payment_id, payload = client.payment.refund.call_args.args
		self.assertEqual(payment_id, "pay_T5")
		self.assertLessEqual(len(payload["receipt"]), 40)
		self.assertEqual(set(client.payment.refund.call_args.kwargs["headers"]), {"X-Refund-Idempotency"})

	def test_duplicate_refund_receipt_returns_the_existing_refund(self):
		transaction = make_transaction(status="Paid", provider_transaction_id="pay_T5")
		settings = object.__new__(RazorpaySettings)
		client = MagicMock()
		client.payment.refund.side_effect = BadRequestError("Duplicate receipt found for this refund request")
		client.payment.fetch_multiple_refund.return_value = {
			"items": [{"id": "rfnd_EXISTING", "receipt": "pc-refund-key-123"}]
		}

		with patch(f"{CONTROLLER_MODULE}.get_razorpay_client", return_value=client):
			refund_id = settings.refund_transaction(transaction, 12.5, idempotency_key="pc-refund-key-123")

		self.assertEqual(refund_id, "rfnd_EXISTING")
		client.payment.fetch_multiple_refund.assert_called_once_with("pay_T5", {"count": 100})

	def test_duplicate_refund_receipt_searches_later_pages(self):
		transaction = make_transaction(status="Paid", provider_transaction_id="pay_T5")
		settings = object.__new__(RazorpaySettings)
		client = MagicMock()
		client.payment.refund.side_effect = BadRequestError("Duplicate receipt found for this refund request")
		client.payment.fetch_multiple_refund.side_effect = [
			{"items": [{"id": f"rfnd_{index}", "receipt": f"other-{index}"} for index in range(100)]},
			{"items": [{"id": "rfnd_EXISTING", "receipt": "pc-refund-key-123"}]},
		]

		with patch(f"{CONTROLLER_MODULE}.get_razorpay_client", return_value=client):
			refund_id = settings.refund_transaction(transaction, 12.5, idempotency_key="pc-refund-key-123")

		self.assertEqual(refund_id, "rfnd_EXISTING")
		client.payment.fetch_multiple_refund.assert_has_calls(
			[
				call("pay_T5", {"count": 100}),
				call("pay_T5", {"count": 100, "skip": 100}),
			]
		)

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
		):
			result = settings.reconcile_transaction(transaction)

		self.assertIsNone(result)
		row = frappe.db.get_value("Payment Transaction", transaction, "status")
		self.assertEqual(row, "Initiated")

	def test_reconcile_compares_at_currency_precision(self):
		transaction = make_transaction(status="Initiated", provider_transaction_id="order_T8")
		settings = frappe.get_doc("Razorpay Settings")

		with (
			patch(
				f"{CONTROLLER_MODULE}.make_get_request",
				return_value={
					"items": [{"id": "pay_T8", "status": "captured", "amount": 10000, "currency": "INR"}]
				},
			),
			patch(f"{CONTROLLER_MODULE}.from_minor_units", return_value=100.004),
		):
			result = settings.reconcile_transaction(transaction)

		self.assertEqual(result, {"status": "Paid"})

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
			order_payment_success(
				ir, json.dumps({"razorpay_payment_id": "pay_T9", "razorpay_signature": "sig"})
			)

		row = frappe.db.get_value(
			"Payment Transaction", transaction, ["status", "provider_transaction_id"], as_dict=True
		)
		self.assertEqual(row.status, "Paid")
		self.assertEqual(row.provider_transaction_id, "pay_T9")

	def test_authorized_callback_settles_once_after_scheduled_capture(self):
		transaction = make_transaction(status="Initiated", provider_transaction_id="order_T9")
		ir = _make_integration_request(
			"ToDo", frappe.db.get_value("Payment Transaction", transaction, "reference_docname")
		)
		data = json.loads(frappe.db.get_value("Integration Request", ir, "data"))
		data.update({"payment_transaction": transaction, "order_id": "order_T9"})
		frappe.db.set_value("Integration Request", ir, "data", json.dumps(data), update_modified=False)

		provider_responses = [
			{
				"id": "pay_T9",
				"status": "authorized",
				"order_id": "order_T9",
				"amount": 10000,
				"currency": "INR",
			},
			{
				"id": "pay_T9",
				"status": "captured",
				"order_id": "order_T9",
				"amount": 10000,
				"currency": "INR",
			},
		]
		with (
			patch.object(RazorpaySettings, "verify_checkout_signature", return_value=True),
			patch(f"{CONTROLLER_MODULE}.make_get_request", side_effect=provider_responses),
			patch.object(ToDo, "on_payment_authorized", return_value=None, create=True) as authorize,
		):
			order_payment_success(
				ir, json.dumps({"razorpay_payment_id": "pay_T9", "razorpay_signature": "sig"})
			)
			authorize.assert_not_called()
			capture_payment()
			capture_payment()

		authorize.assert_called_once_with("Completed")
		self.assertEqual(frappe.db.get_value("Integration Request", ir, "status"), "Completed")
		row = frappe.db.get_value(
			"Payment Transaction",
			transaction,
			["status", "reference_authorized", "provider_transaction_id"],
			as_dict=True,
		)
		self.assertEqual(row.status, "Paid")
		self.assertTrue(row.reference_authorized)
		self.assertEqual(row.provider_transaction_id, "pay_T9")

	def test_callback_webhook_and_reconcile_authorize_the_reference_once(self):
		"""One captured payment can reach all three completion paths."""
		transaction = make_transaction(status="Initiated", provider_transaction_id="order_T1")
		ir = _make_integration_request(
			"ToDo", frappe.db.get_value("Payment Transaction", transaction, "reference_docname")
		)
		data = json.loads(frappe.db.get_value("Integration Request", ir, "data"))
		data["payment_transaction"] = transaction
		data["order_id"] = "order_T1"
		frappe.db.set_value("Integration Request", ir, "data", json.dumps(data), update_modified=False)

		def provider_response(url, **_kwargs):
			if url.endswith("/orders/order_T1"):
				return order_for_transaction(transaction)
			return {
				"id": "pay_T1",
				"status": "captured",
				"order_id": "order_T1",
				"amount": 10000,
				"currency": "INR",
			}

		with (
			patch.object(RazorpaySettings, "verify_checkout_signature", return_value=True),
			patch(f"{CONTROLLER_MODULE}.make_get_request", side_effect=provider_response),
			patch("razorpay_payment.gateway.reconciliation.make_get_request", side_effect=provider_response),
			patch("razorpay_payment.gateway.reconciliation.get_razorpay_auth", return_value=("k", "s")),
			patch.object(ToDo, "on_payment_authorized", return_value=None, create=True) as authorize,
		):
			order_payment_success(
				ir, json.dumps({"razorpay_payment_id": "pay_T1", "razorpay_signature": "sig"})
			)
			reconciliation.reconcile_payment_captured(
				captured_event(transaction), frappe.get_doc("Razorpay Settings")
			)
			frappe.get_doc("Razorpay Settings").reconcile_transaction(transaction)

		authorize.assert_called_once_with("Completed")
		self.assertEqual(frappe.db.get_value("Payment Transaction", transaction, "reference_authorized"), 1)

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

	def test_source_is_stable_with_or_without_a_loaded_controller(self):
		event = captured_event(make_transaction(status="Initiated"), payment_id="pay_SOURCE")
		with patch.object(reconciliation, "route_event", return_value={"status_label": "Processed"}):
			first = webhooks.handle_event(event, None, event_id="evt_SOURCE")
			second = webhooks.handle_event(event, frappe.get_doc("Razorpay Settings"), event_id="evt_SOURCE")

		self.assertEqual(first, {"status": "ok"})
		self.assertEqual(second, webhooks.DUPLICATE)

	def test_desk_retry_preserves_the_sweep_attempt_count(self):
		log = frappe.get_doc(
			{
				"doctype": "Payment Webhook Log",
				"source": "Razorpay:Razorpay",
				"event_id": frappe.generate_hash(length=12),
				"event_type": "payment.captured",
				"status": "Failed",
				"payload": frappe.as_json(captured_event("PAY-TXN-NOT-USED")),
				"error": "sweep attempt 2: previous failure",
			}
		).insert(ignore_permissions=True)
		settings = frappe.get_doc("Razorpay Settings")

		with (
			patch.object(reconciliation, "route_event", side_effect=Exception("still down")),
			patch(f"{CONTROLLER_MODULE}.mark_failed") as mark,
		):
			settings.process_webhook_log(log.name)

		self.assertTrue(mark.call_args.args[1].startswith("sweep attempt 2:"))

	def test_webhook_log_migration_preserves_terminal_statuses(self):
		statuses = {"Failed": "Failed", "Ignored": "Ignored", "Processed": "Processed"}
		for old_status, expected in statuses.items():
			event_id = f"evt_migrate_{old_status.lower()}_{frappe.generate_hash(length=8)}"
			frappe.get_doc(
				{
					"doctype": "Razorpay Webhook Log",
					"razorpay_event_id": event_id,
					"event_type": "payment.captured",
					"status": old_status,
					"payload": "{}",
					"error": "legacy error",
					"retry_count": 2,
				}
			).insert(ignore_permissions=True)

			migrate_webhook_log()

			migrated = frappe.db.get_value(
				"Payment Webhook Log",
				{"source": "Razorpay:Razorpay", "event_id": event_id},
				["status", "error"],
				as_dict=True,
			)
			self.assertEqual(migrated.status, expected)
			self.assertTrue(migrated.error.startswith("sweep attempt 2: legacy error"))

	def test_is_webhook_configured_reads_the_secret(self):
		settings = object.__new__(RazorpaySettings)
		with patch(f"{CONTROLLER_MODULE}.get_webhook_secret", return_value="whsec_x"):
			self.assertTrue(settings.is_webhook_configured())
		with patch(f"{CONTROLLER_MODULE}.get_webhook_secret", return_value=None):
			self.assertFalse(settings.is_webhook_configured())
