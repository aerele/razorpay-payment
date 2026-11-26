import hashlib
import hmac
import json
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import MagicMock, patch

from frappe import _dict

from razorpay_payment.gateway import collect_now, reconciliation
from razorpay_payment.gateway.references import merge_checkout_params
from razorpay_payment.razorpay.doctype.razorpay_settings import razorpay_settings
from razorpay_payment.templates.pages import razorpay_checkout


class TestCollectNow(TestCase):
	def setUp(self):
		self.checkout = {
			collect_now.POLICY: True,
			"order_id": "order_1",
			"reference_doctype": "Payment Request",
			"reference_docname": "PR-1",
			"amount": 25000,
			"currency": "INR",
			"use_sandbox": True,
		}
		self.payment = {
			"id": "pay_1",
			"order_id": "order_1",
			"amount": 25000,
			"currency": "INR",
			"status": "captured",
		}
		self.settings = MagicMock()
		self.settings.flags = _dict()
		self.settings.get_settings.return_value = _dict(api_key="sandbox", api_secret="secret")
		self.row = _dict(data=json.dumps(self.checkout), status="Queued", output="")
		self.payment_request = _dict(docstatus=1, status="Requested")
		self.checkout_link = _dict(name="IR-1", reference_doctype="Payment Request", reference_docname="PR-1")
		self.creation = datetime(2026, 11, 24)
		self.frappe = self.enterContext(patch.object(collect_now, "frappe"))
		self.frappe.parse_json.side_effect = json.loads
		self.frappe.as_json.side_effect = json.dumps
		self.frappe.qb.from_.return_value.select.return_value.where.return_value.for_update.return_value.run.return_value = [
			self.row
		]
		self.frappe.db.set_value.side_effect = self._write
		self.frappe.db.get_value.side_effect = self._get_value
		self.enterContext(patch.object(collect_now, "now_datetime", return_value=datetime(2026, 11, 25)))
		self.enterContext(
			patch.object(
				collect_now,
				"add_to_date",
				side_effect=lambda date, days: date + timedelta(days=days),
			)
		)
		self.fetch = self.enterContext(
			patch.object(collect_now, "make_get_request", return_value=self.payment)
		)
		self.capture = self.enterContext(patch.object(collect_now, "make_post_request"))

	def _write(self, doctype, name, values, value=None, **kwargs):
		self.row.update(values if isinstance(values, dict) else {values: value})

	def _get_value(self, doctype, filters, fieldname=None, **kwargs):
		if doctype == "Payment Request":
			return self.payment_request
		if fieldname == "creation":
			return self.creation
		return self.checkout_link

	def _orders(self, *payments):
		return {"entity": "collection", "count": len(payments), "items": list(payments)}

	def _evidence(self):
		return json.loads(self.row.output)

	def _verify(self, **kwargs):
		return collect_now.verify_and_settle(self.settings, "IR-1", **kwargs)

	def test_checkout_policy_is_server_controlled(self):
		with (
			patch.object(razorpay_settings, "frappe") as frappe,
			patch.object(razorpay_settings, "guard_payment_reference"),
			patch.object(razorpay_settings, "assert_reference_payable"),
			patch.object(razorpay_settings, "is_subscription_reference", return_value=False),
			patch.object(razorpay_settings, "get_url", side_effect=lambda url: url),
			patch.object(razorpay_settings.ledger, "ensure_payment_transaction"),
			patch.object(razorpay_settings.ledger, "sync_payment_transaction"),
		):
			frappe.get_meta.return_value.has_field.return_value = False
			frappe.db.table_exists.return_value = False
			self.settings.create_order.return_value = {"id": "order_1", "integration_request": "IR-1"}
			for enabled in (0, 1):
				self.settings.get.return_value = enabled
				razorpay_settings.RazorpaySettings.get_payment_url(
					self.settings,
					reference_doctype="Payment Request",
					reference_docname="PR-1",
					**{collect_now.POLICY: not enabled},
				)
				order = self.settings.create_order.call_args.kwargs
				self.assertEqual(bool(order.get(collect_now.POLICY)), bool(enabled))
				self.assertEqual(
					bool(
						merge_checkout_params(order, {collect_now.POLICY: not enabled}).get(
							collect_now.POLICY
						)
					),
					bool(enabled),
				)

	def test_captured_payment_posts_once_across_replays(self):
		self.assertEqual(self._verify(payment_id="pay_1"), "completed")
		self.assertEqual(self._verify(payment_id="pay_1"), "completed")
		self.assertEqual(self._verify(allow_capture=True), "completed")
		self.fetch.assert_called_once_with(
			f"{collect_now.RAZORPAY_API_BASE}/payments/pay_1",
			auth=("sandbox", "secret"),
			timeout=collect_now.STATUS_API_TIMEOUT,
		)
		self.settings.settle_payment_request.assert_called_once_with(
			self.frappe.get_doc.return_value, payment_id="pay_1"
		)
		[entry] = self._evidence()
		self.assertEqual(
			{key: entry[key] for key in ("source", "outcome", "payment_id", "order_id", "provider_status")},
			{
				"source": "status_api",
				"outcome": "verified",
				"payment_id": "pay_1",
				"order_id": "order_1",
				"provider_status": "captured",
			},
		)
		self.assertEqual(entry["checked_at"], "2026-11-25T00:00:00")
		self.assertEqual(
			entry["calls"],
			[{"method": "GET", "path": "/payments/pay_1", "response": self.payment}],
		)

	def test_rejects_mismatched_or_missing_provider_fields(self):
		for field, value in {
			"id": "pay_other",
			"order_id": "order_other",
			"amount": 1,
			"currency": None,
		}.items():
			with self.subTest(field=field):
				self.fetch.return_value = {**self.payment, field: value}
				self.assertEqual(self._verify(payment_id="pay_1"), "invalid")
		self.settings.settle_payment_request.assert_not_called()
		self.capture.assert_not_called()

	def test_capture_response_cannot_settle_without_fresh_status(self):
		authorized = {**self.payment, "status": "authorized"}
		self.fetch.side_effect = [
			authorized,
			self._orders(authorized),
			authorized,
			self._orders(self.payment),
		]
		self.capture.return_value = self.payment
		self.assertEqual(self._verify(payment_id="pay_1"), "pending")
		self.assertEqual(self.row.status, "Authorized")
		self.capture.assert_not_called()
		self.assertEqual(self._verify(allow_capture=True), "pending")
		self.settings.settle_payment_request.assert_not_called()
		self.capture.assert_called_once()
		self.assertEqual(self._verify(allow_capture=True), "completed")

	def test_api_timeout_and_accounting_failure_are_retryable(self):
		self.fetch.side_effect = [TimeoutError, self._orders(self.payment), self._orders(self.payment)]
		self.settings.settle_payment_request.side_effect = [RuntimeError, object()]
		self.assertEqual(self._verify(payment_id="pay_1"), "pending")
		self.assertEqual(self._evidence()[-1]["outcome"], "provider_unavailable")
		self.assertEqual(self._evidence()[-1]["calls"][0]["error"], "TimeoutError")
		self.assertEqual(self._verify(allow_capture=True), "pending")
		self.frappe.db.rollback.assert_called_once_with(save_point="collect_now_accounting")
		self.assertEqual(self._evidence()[-1]["outcome"], "accounting_pending")
		self.assertEqual(self._verify(allow_capture=True), "completed")

	def test_callback_requires_signature_before_fetching_status(self):
		params = {"razorpay_payment_id": "pay_1", "razorpay_order_id": "order_1", "razorpay_signature": "bad"}
		self.assertEqual(
			collect_now.handle_callback(self.settings, "IR-1", self.checkout, params)["status"], 401
		)
		self.fetch.assert_not_called()

		params["razorpay_signature"] = hmac.new(b"secret", b"order_1|pay_1", hashlib.sha256).hexdigest()
		result = collect_now.handle_callback(self.settings, "IR-1", self.checkout, params)
		self.assertEqual(result["redirect_to"], "payment-success?doctype=Payment+Request&docname=PR-1")
		self.assertNotIn("razorpay_signature", json.loads(self.row.data))

	def test_webhook_uses_independently_fetched_status(self):
		self.fetch.return_value = {**self.payment, "status": "created"}
		event = {"payload": {"payment": {"entity": self.payment}}}
		result = reconciliation.reconcile_payment_captured(event, self.settings)
		self.assertEqual(
			result,
			{"status_label": "Processed", "reference_doctype": "Payment Request", "reference_name": "PR-1"},
		)
		self.settings.settle_payment_request.assert_not_called()
		self.assertEqual(self.row.status, "Queued")
		self.assertEqual(self._evidence()[-1]["provider_status"], "created")

	def test_ignored_order_event_links_the_stored_checkout(self):
		event = {"event": "order.paid", "payload": {"order": {"entity": {"id": "order_1"}}}}
		self.assertEqual(
			reconciliation.route_event(event, self.settings),
			{"status_label": "Ignored", "reference_doctype": "Payment Request", "reference_name": "PR-1"},
		)
		self.fetch.assert_not_called()
		self.settings.settle_payment_request.assert_not_called()

	def test_retry_recovers_order_without_callback_or_webhook(self):
		self.assertEqual(self._verify(allow_capture=True), "pending")
		self.assertEqual(self.row.status, "Queued")
		self.assertEqual(self._evidence()[-1]["outcome"], "no_attempt")

		failed = {**self.payment, "id": "pay_0", "status": "failed", "created_at": 2}
		other = {**self.payment, "id": "pay_9", "amount": 1, "created_at": 3}
		self.fetch.return_value = self._orders(failed, {**self.payment, "created_at": 1}, other)
		self.assertEqual(self._verify(allow_capture=True), "completed")
		self.fetch.assert_called_with(
			f"{collect_now.RAZORPAY_API_BASE}/orders/order_1/payments",
			auth=("sandbox", "secret"),
			timeout=collect_now.STATUS_API_TIMEOUT,
		)
		self.settings.settle_payment_request.assert_called_once_with(
			self.frappe.get_doc.return_value, payment_id="pay_1"
		)
		self.assertEqual(json.loads(self.row.data)["razorpay_payment_id"], "pay_1")

	def test_failed_payment_is_rechecked_for_late_authorization(self):
		self.fetch.return_value = {**self.payment, "status": "failed"}
		self.assertEqual(self._verify(payment_id="pay_1"), "failed")
		self.assertEqual(self.row.status, "Failed")
		self.assertIn("Failed", collect_now.RETRY_STATUSES)

		self.fetch.return_value = self._orders(self.payment)
		self.assertEqual(self._verify(allow_capture=True), "completed")

	def test_paid_request_is_not_captured_again(self):
		self.payment_request.status = "Paid"
		self.fetch.return_value = self._orders({**self.payment, "status": "authorized"})
		self.assertEqual(self._verify(allow_capture=True), "failed")
		self.capture.assert_not_called()
		self.assertEqual(self.row.status, "Cancelled")
		self.assertEqual(self._evidence()[-1]["outcome"], "not_payable")
		self.frappe.log_error.assert_called_once()
		self.assertEqual(self._verify(allow_capture=True), "invalid")

	def test_captured_payment_on_paid_request_needs_review(self):
		self.payment_request.status = "Paid"
		self.assertEqual(self._verify(payment_id="pay_1"), "failed")
		self.settings.settle_payment_request.assert_not_called()
		self.assertEqual(self.row.status, "Cancelled")
		self.assertEqual(self._evidence()[-1]["outcome"], "refund_required")

	def test_declined_posting_stops_retrying(self):
		self.settings.settle_payment_request.return_value = None
		self.assertEqual(self._verify(payment_id="pay_1"), "failed")
		self.frappe.db.rollback.assert_called_once_with(save_point="collect_now_accounting")
		self.assertEqual(self.row.status, "Cancelled")
		self.assertEqual(self._evidence()[-1]["outcome"], "not_posted")
		self.frappe.log_error.assert_called_once()

	def test_evidence_is_redacted_and_repeats_are_counted(self):
		self.fetch.return_value = {
			**self.payment,
			"status": "authorized",
			"email": "payer@example.com",
			"contact": "+919000000000",
			"card": {"last4": "1111"},
		}
		self._verify(payment_id="pay_1")
		self._verify(payment_id="pay_1")
		[entry] = self._evidence()
		self.assertEqual(entry["count"], 2)
		self.assertEqual(entry["last_checked_at"], "2026-11-25T00:00:00")
		response = entry["calls"][0]["response"]
		self.assertNotIn("email", response)
		self.assertNotIn("contact", response)
		self.assertNotIn("card", response)
		self.assertEqual(response["status"], "authorized")

	def test_browser_failure_is_stored_without_changing_status(self):
		error = {
			"error": {
				"code": "BAD_REQUEST_ERROR",
				"description": "Payment failed",
				"reason": "payment_failed",
				"metadata": {"order_id": "order_1", "payment_id": "pay_2"},
			}
		}
		collect_now.record_client_failure("IR-1", error)
		entry = self._evidence()[-1]
		self.assertEqual(entry["source"], "browser")
		self.assertEqual(entry["outcome"], "client_reported_failure")
		self.assertEqual(entry["payment_id"], "pay_2")
		self.assertEqual(entry["error"]["code"], "BAD_REQUEST_ERROR")
		self.assertEqual(self.row.status, "Queued")
		self.assertNotIn("razorpay_payment_id", json.loads(self.row.data))
		self.fetch.assert_not_called()

		error["error"]["metadata"]["order_id"] = "order_other"
		error["error"]["code"] = "OTHER"
		collect_now.record_client_failure("IR-1", error)
		self.assertIsNone(self._evidence()[-1]["payment_id"])

	def test_failure_endpoint_records_opted_in_checkouts(self):
		integration = MagicMock()
		integration.name = "IR-1"
		integration.data = json.dumps(self.checkout)
		with (
			patch.object(razorpay_settings, "frappe") as frappe,
			patch.object(razorpay_settings.collect_now, "record_client_failure") as record,
		):
			frappe.session.user = "Guest"
			frappe.get_doc.return_value = integration
			razorpay_settings.order_payment_failure("IR-1", json.dumps({"error": {"code": "X"}}))
		record.assert_called_once_with("IR-1", {"error": {"code": "X"}})
		integration.update_status.assert_not_called()

	def test_failed_webhook_is_verified_for_the_checkout(self):
		failed = {**self.payment, "status": "failed"}
		self.fetch.return_value = failed
		event = {"event": "payment.failed", "payload": {"payment": {"entity": failed}}}
		self.assertEqual(
			reconciliation.route_event(event, self.settings),
			{"status_label": "Processed", "reference_doctype": "Payment Request", "reference_name": "PR-1"},
		)
		self.assertEqual(self.row.status, "Failed")
		self.assertEqual(self._evidence()[-1]["provider_status"], "failed")

	def test_unresolved_payment_is_reported_once_after_the_window(self):
		self.frappe.get_doc.return_value = self.settings
		self.creation = datetime(2026, 11, 19)
		self.row.data = json.dumps({**self.checkout, "razorpay_payment_id": "pay_1"})
		self.fetch.return_value = self._orders({**self.payment, "status": "created"})
		collect_now.retry_checkout("IR-1")
		collect_now.retry_checkout("IR-1")
		self.assertEqual(
			[entry["outcome"] for entry in self._evidence()], ["verified", "expired", "verified"]
		)
		self.frappe.log_error.assert_called_once()
		self.assertEqual(self.row.status, "Queued")

		self.creation = datetime(2026, 11, 24)
		self.frappe.log_error.reset_mock()
		self.row.output = ""
		collect_now.retry_checkout("IR-1")
		self.assertNotIn("expired", [entry["outcome"] for entry in self._evidence()])
		self.frappe.log_error.assert_not_called()

	def test_review_reason_is_written_for_staff(self):
		self.payment_request.status = "Paid"
		self._verify(payment_id="pay_1")
		self.assertIn("refund_required", self.row.error)
		self.assertIn("pay_1", self.row.error)

	def test_abandoned_checkouts_are_polled_less_often_after_the_first_day(self):
		self.assertEqual(collect_now._retry_since(datetime(2026, 11, 25, 6)), datetime(2026, 11, 19, 6))
		self.assertEqual(collect_now._retry_since(datetime(2026, 11, 25, 7)), datetime(2026, 11, 24, 7))

	def test_browser_reports_cannot_push_out_status_checks(self):
		self.fetch.return_value = {**self.payment, "status": "failed"}
		self._verify(payment_id="pay_1")
		for code in range(collect_now.MAX_BROWSER_EVIDENCE + 10):
			collect_now.record_client_failure("IR-1", {"error": {"code": f"E{code}"}})
		entries = self._evidence()
		self.assertEqual(entries[0]["source"], "status_api")
		browser = [entry for entry in entries if entry["source"] == "browser"]
		self.assertEqual(len(browser), collect_now.MAX_BROWSER_EVIDENCE)
		self.assertEqual(browser[-1]["error"]["code"], f"E{collect_now.MAX_BROWSER_EVIDENCE + 9}")

	def test_paid_request_does_not_open_checkout(self):
		with (
			patch.object(razorpay_checkout, "frappe") as frappe,
			patch.object(razorpay_checkout, "validate_integration_request"),
			patch.object(razorpay_checkout, "guard_payment_reference"),
			patch.object(razorpay_checkout, "is_payable", return_value=False),
		):
			frappe.Redirect = type("Redirect", (Exception,), {})
			frappe.form_dict = {"token": "IR-1"}
			frappe.db.get_value.return_value = json.dumps(
				{**self.checkout, "title": "T", "description": "D", "payer_name": "P", "payer_email": "E"}
			)
			with self.assertRaises(frappe.Redirect):
				razorpay_checkout.get_context(_dict())
		self.assertEqual(frappe.redirect_to_message.call_count, 1)
		self.assertEqual(frappe.redirect_to_message.call_args.kwargs["indicator_color"], "orange")
