# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Unit tests for shared Razorpay client primitives (no live Razorpay calls).

import hashlib
import hmac
import unittest

from razorpay_payment.gateway.client import (
	from_minor_units,
	idempotency_key,
	to_minor_units,
	verify_payment_signature,
	verify_subscription_signature,
	verify_webhook_signature,
)
from razorpay_payment.gateway.constants import (
	RAZORPAY_API_BASE,
	THREE_DECIMAL_CURRENCIES,
	ZERO_DECIMAL_CURRENCIES,
)
from razorpay_payment.gateway.references import get_razorpay_notes, success_redirect


def _sign(message, secret):
	"""Sign a string message with a string secret (str input → str hex)."""
	return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _sign_bytes(message, secret):
	"""Sign raw bytes (webhook body) with a string secret → str hex."""
	return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class TestRazorpayClientPrimitives(unittest.TestCase):
	def test_to_minor_units_decimal_currency(self):
		self.assertEqual(to_minor_units(12.50, "INR"), 1250)
		self.assertEqual(to_minor_units("10", "USD"), 1000)

	def test_to_minor_units_zero_decimal_currency(self):
		self.assertEqual(to_minor_units(1000, "JPY"), 1000)
		self.assertEqual(to_minor_units(99.6, "JPY"), 100)

	def test_to_minor_units_three_decimal_currency(self):
		# Razorpay expects 1/1000 subunits for KWD/BHD/OMR etc.
		self.assertEqual(to_minor_units(295.991, "KWD"), 295991)
		self.assertEqual(to_minor_units(1, "BHD"), 1000)
		self.assertEqual(to_minor_units("2.5", "OMR"), 2500)

	def test_from_minor_units_round_trip(self):
		self.assertEqual(from_minor_units(1250, "INR"), 12.5)
		self.assertEqual(from_minor_units(1000, "JPY"), 1000)
		self.assertEqual(from_minor_units(295991, "KWD"), 295.991)

	def test_zero_decimal_set_includes_jpy(self):
		self.assertIn("JPY", ZERO_DECIMAL_CURRENCIES)
		self.assertNotIn("INR", ZERO_DECIMAL_CURRENCIES)

	def test_three_decimal_set(self):
		for code in ("KWD", "BHD", "OMR"):
			self.assertIn(code, THREE_DECIMAL_CURRENCIES)
		self.assertNotIn("INR", THREE_DECIMAL_CURRENCIES)
		# The sets must not overlap; each currency has exactly one conversion rule.
		self.assertFalse(THREE_DECIMAL_CURRENCIES & ZERO_DECIMAL_CURRENCIES)

	def test_idempotency_key_stable(self):
		a = idempotency_key("order", "PR-0001", 100)
		b = idempotency_key("order", "PR-0001", 100)
		c = idempotency_key("order", "PR-0001", 101)
		self.assertEqual(a, b)
		self.assertNotEqual(a, c)
		self.assertEqual(len(a), 64)

	def test_api_base(self):
		self.assertTrue(RAZORPAY_API_BASE.startswith("https://"))
		self.assertIn("razorpay.com", RAZORPAY_API_BASE)

	def test_get_razorpay_notes_drops_empty(self):
		notes = get_razorpay_notes(
			{"reference_doctype": "Payment Request", "reference_docname": "PR-1"},
			integration_request="IR-1",
		)
		self.assertEqual(notes["reference_doctype"], "Payment Request")
		self.assertEqual(notes["integration_request"], "IR-1")
		self.assertNotIn("missing", notes)

	def test_success_redirect(self):
		url = success_redirect(reference_doctype="Payment Request", reference_docname="PR-1")
		self.assertIn("payment-success", url)
		self.assertIn("PR-1", url)
		self.assertEqual(success_redirect({}), "payment-success")

	def test_verify_payment_signature(self):
		secret = "test_secret"
		good = _sign("order_1|pay_1", secret)
		self.assertTrue(verify_payment_signature("order_1", "pay_1", good, secret))
		# Signature valid for a different (cheaper) order must not settle this one.
		self.assertFalse(verify_payment_signature("order_2", "pay_1", good, secret))
		self.assertFalse(verify_payment_signature("order_1", "pay_1", "deadbeef", secret))
		self.assertFalse(verify_payment_signature("order_1", "pay_1", "", secret))

	def test_verify_subscription_signature(self):
		secret = "test_secret"
		good = _sign("pay_1|sub_1", secret)
		self.assertTrue(verify_subscription_signature("sub_1", "pay_1", good, secret))
		self.assertFalse(verify_subscription_signature("sub_2", "pay_1", good, secret))
		self.assertFalse(verify_subscription_signature("sub_1", "pay_1", None, secret))

	def test_verify_webhook_signature(self):
		secret = "whsec"
		body = '{"event":"payment.captured"}'
		self.assertTrue(verify_webhook_signature(body, _sign(body, secret), secret))
		self.assertFalse(verify_webhook_signature(body, _sign(body, "other"), secret))
		self.assertFalse(verify_webhook_signature(body, None, secret))

	def test_verify_webhook_signature_accepts_raw_bytes_body(self):
		"""Raw webhook bodies arrive as bytes (frappe.request.get_data())."""
		secret = "whsec"
		body = b'{"event":"payment.captured"}'
		self.assertTrue(verify_webhook_signature(body, _sign_bytes(body, secret), secret))
		self.assertFalse(verify_webhook_signature(body, _sign_bytes(body, "other"), secret))
