# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for the audit-fix paths on the Razorpay controller + checkout page:
# webhook signature verification, permission guards on guest endpoints, the
# capture_payment scheduler, and the test_credentials button.

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings import (
	capture_payment,
	get_api_key,
	get_order,
	order_payment_failure,
	order_payment_success,
	razorpay_subscription_callback,
	verify_callback_signature,
)


def _hmac(secret, body):
	return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class TestWebhookSignatureAuth(FrappeTestCase):
	"""C1/C6/C8/I8: razorpay_subscription_callback is gated on signature verification."""

	def test_verify_callback_signature_false_without_secret(self):
		with patch(
			"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_doc"
		) as gd:
			gd.return_value.get_password.return_value = None
			self.assertFalse(verify_callback_signature(b"{}", "sig"))

	def test_verify_callback_signature_true_for_valid_hmac(self):
		secret = "whsec_test"
		body = b'{"event":"x"}'
		with patch(
			"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_doc"
		) as gd:
			gd.return_value.get_password.return_value = secret
			self.assertTrue(verify_callback_signature(body, _hmac(secret, body)))

	def test_verify_callback_signature_false_for_wrong_hmac(self):
		secret = "whsec_test"
		body = b'{"event":"x"}'
		with patch(
			"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_doc"
		) as gd:
			gd.return_value.get_password.return_value = secret
			self.assertFalse(verify_callback_signature(body, "deadbeef"))

	def test_callback_rejects_bad_signature_with_400(self):
		frappe.local.request = MagicMock()
		frappe.local.request.get_data.return_value = b"{}"
		frappe.local.response = {}
		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_request_header",
				return_value=None,
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.verify_callback_signature",
				return_value=False,
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_doc"
			) as gd,
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.enqueue"
			) as eq,
		):
			result = razorpay_subscription_callback()
		self.assertEqual(result, {"status": "invalid signature"})
		self.assertEqual(frappe.local.response["http_status_code"], 400)
		gd.assert_not_called()
		eq.assert_not_called()

	def test_callback_does_not_call_db_commit(self):
		"""I8: the manual frappe.db.commit() must be gone."""
		import inspect

		from razorpay_payment.razorpay.doctype.razorpay_settings import razorpay_settings as mod

		src = inspect.getsource(mod.razorpay_subscription_callback)
		self.assertNotIn("frappe.db.commit", src)


class TestGuestEndpointGuards(FrappeTestCase):
	"""C2-C5, C7: guest endpoints have inline permission guards for authenticated callers."""

	def test_get_api_key_calls_has_permission_when_authenticated(self):
		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.session",
				user="user@example.com",
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.has_permission"
			) as hp,
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.db.get_single_value",
				return_value="rzp_test_key",
			),
		):
			key = get_api_key()
		self.assertEqual(key, "rzp_test_key")
		hp.assert_called_once_with("Razorpay Settings", "read", throw=True)

	def test_get_api_key_skips_permission_for_guest(self):
		"""Guests need the publishable key for the checkout SDK — no perm check."""
		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.session",
				user="Guest",
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.has_permission"
			) as hp,
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.db.get_single_value",
				return_value="rzp_test_key",
			),
		):
			key = get_api_key()
		self.assertEqual(key, "rzp_test_key")
		hp.assert_not_called()

	def test_order_payment_success_enforces_auth_read(self):
		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.session",
				user="user@example.com",
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.has_permission"
			) as hp,
			patch("razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_doc"),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.json.loads",
				return_value={},
			),
		):
			order_payment_success("IR-1", "{}")
		hp.assert_called_once_with("Integration Request", "read", "IR-1", throw=True)

	def test_order_payment_failure_enforces_auth_read(self):
		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.session",
				user="user@example.com",
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.has_permission"
			) as hp,
			patch("razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.log_error"),
			patch("razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_doc"),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.json.loads",
				return_value={},
			),
		):
			order_payment_failure("IR-1", "{}")
		hp.assert_called_once_with("Integration Request", "read", "IR-1", throw=True)

	def test_get_order_enforces_reference_read_for_auth_user(self):
		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.session",
				user="user@example.com",
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.has_permission"
			) as hp,
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_doc"
			) as gd,
		):
			gd.return_value.get_razorpay_order.return_value = {"id": "order_1"}
			get_order("Payment Request", "PR-1")
		hp.assert_called_once_with("Payment Request", "read", "PR-1", throw=True)


class TestCapturePaymentScheduler(FrappeTestCase):
	"""W1/W4/W5: capture_payment is guarded, batched, and never raises."""

	def test_capture_payment_loads_controller_once_outside_loop(self):
		"""The controller is loaded exactly once for the whole batch."""
		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_doc"
			) as gd,
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_all",
				return_value=[],
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.db.count",
				return_value=0,
			),
			patch("razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.db.sql"),
		):
			capture_payment()
		self.assertEqual(gd.call_count, 1)

	def test_capture_payment_logs_and_returns_on_load_failure(self):
		"""W5: a get_all failure logs and returns, never raises."""
		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.db.count",
				return_value=1,
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_all",
				side_effect=Exception("DB down"),
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.log_error"
			) as le,
		):
			capture_payment()  # must not raise
		le.assert_called_once()

	def test_capture_payment_early_exits_when_nothing_pending(self):
		"""I10: when the COUNT guard returns 0, no fetch or iteration runs."""
		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_doc"
			) as gd,
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.db.count",
				return_value=0,
			) as cnt,
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_all"
			) as ga,
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.db.sql"
			) as sql,
		):
			capture_payment()
		# Controller loads once; COUNT guard returns 0; no get_all, no UPDATEs.
		self.assertEqual(gd.call_count, 1)
		cnt.assert_called_once()
		ga.assert_not_called()
		sql.assert_not_called()

	def test_capture_payment_uses_bulk_update(self):
		"""W4: one UPDATE per status bucket, not one set_value per row."""
		rows = [
			MagicMock(name=f"IR-{i}", data=json.dumps({"razorpay_payment_id": f"pay_{i}"})) for i in range(3)
		]
		settings_mock = MagicMock()
		settings_mock.get_settings.return_value = MagicMock(api_key="k", api_secret="s")

		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_doc",
				return_value=settings_mock,
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.get_all",
				return_value=rows,
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.make_get_request",
				return_value={"status": "authorized"},
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.make_post_request",
				return_value={"status": "captured"},
			),
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.db"
			) as db_mock,
		):
			db_mock.savepoint = MagicMock()
			db_mock.release_savepoint = MagicMock()
			db_mock.rollback = MagicMock()
			db_mock.sql = MagicMock()
			capture_payment()

		# Bulk captured UPDATE ran exactly once with all 3 names.
		captured_calls = [
			c for c in db_mock.sql.call_args_list if "Completed" in str(c) or "'Completed'" in str(c)
		]
		self.assertEqual(len(captured_calls), 1, f"expected 1 captured UPDATE, got {captured_calls}")
		# Names were passed as a tuple of all 3.
		names_arg = captured_calls[0].args[1]["names"]
		self.assertEqual(len(names_arg), 3)


class TestTestCredentialsButton(FrappeTestCase):
	"""I10: validate() no longer fires HTTP; test_credentials does, on demand."""

	def test_validate_does_not_call_credential_check(self):
		"""Save path must be free of network I/O (credential check is on-demand)."""
		from razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings import (
			RazorpaySettings,
		)

		settings = frappe.new_doc("Razorpay Settings")
		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.create_payment_gateway"
			),
			patch("razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.call_hook_method"),
			patch.object(RazorpaySettings, "validate_razorpay_credentails") as vrc,
		):
			settings.validate()
		vrc.assert_not_called()

	def test_test_credentials_invokes_validate_razorpay_credentails(self):
		from razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings import (
			RazorpaySettings,
		)

		settings = frappe.new_doc("Razorpay Settings")
		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.has_permission"
			) as hp,
			patch.object(RazorpaySettings, "validate_razorpay_credentails") as vrc,
		):
			settings.test_credentials()
		# Permission check ran (C1: test_credentials is gated on Settings write).
		hp.assert_called_once_with("Razorpay Settings", "write", settings, throw=True)
		vrc.assert_called_once()

	def test_test_credentials_blocked_without_permission(self):
		"""C1: callers without Settings write are rejected before the live call."""
		from razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings import (
			RazorpaySettings,
		)

		settings = frappe.new_doc("Razorpay Settings")
		with (
			patch(
				"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.frappe.has_permission",
				side_effect=frappe.PermissionError,
			),
			patch.object(RazorpaySettings, "validate_razorpay_credentails") as vrc,
		):
			with self.assertRaises(frappe.PermissionError):
				settings.test_credentials()
		vrc.assert_not_called()


class TestConvertRupeeToPaisaRemoved(FrappeTestCase):
	"""I6/I11: convert_rupee_to_paisa and frappe.conf.converted_rupee_to_paisa are gone."""

	def test_convert_rupee_to_paisa_function_deleted(self):
		import razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings as mod

		self.assertFalse(hasattr(mod, "convert_rupee_to_paisa"))

	def test_setup_addon_does_not_read_frappe_conf_flag(self):
		"""No path through setup_addon reads the removed frappe.conf flag."""
		import inspect

		from razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings import (
			RazorpaySettings,
		)

		src = inspect.getsource(RazorpaySettings.setup_addon)
		self.assertNotIn("converted_rupee_to_paisa", src)
		self.assertNotIn("convert_rupee_to_paisa", src)


class TestMakePaymentCheckout(FrappeTestCase):
	"""C7/I12: make_payment guards the reference and has no manual db.commit()."""

	def test_make_payment_calls_guard_payment_reference(self):
		from razorpay_payment.templates.pages import razorpay_checkout as page

		# Use a fake session object so the Guest check passes (auth user).
		fake_session = MagicMock()
		fake_session.user = "user@example.com"
		with (
			patch.object(page, "guard_payment_reference") as gpr,
			patch.object(page, "assert_reference_payable"),
			patch("razorpay_payment.templates.pages.razorpay_checkout.frappe.session", fake_session),
			patch("razorpay_payment.templates.pages.razorpay_checkout.frappe.has_permission"),
			patch("razorpay_payment.templates.pages.razorpay_checkout.frappe.get_doc"),
		):
			page.make_payment(
				razorpay_payment_id="pay_1",
				options="{}",
				reference_doctype="Payment Request",
				reference_docname="PR-1",
				token="IR-1",
			)
		gpr.assert_called_once_with("Payment Request", "PR-1")

	def test_make_payment_has_no_manual_commit(self):
		"""I12: the explicit frappe.db.commit() after create_request must be gone."""
		import inspect

		from razorpay_payment.templates.pages import razorpay_checkout as page

		src = inspect.getsource(page.make_payment)
		self.assertNotIn("frappe.db.commit", src)
