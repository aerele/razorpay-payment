# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Shared Razorpay primitives: auth resolution, client factory, minor-unit
# conversion, idempotency and signature verification.

import hashlib
import hmac

import frappe
from frappe.utils import cint, flt

from razorpay_payment.gateway.constants import THREE_DECIMAL_CURRENCIES, ZERO_DECIMAL_CURRENCIES


def get_razorpay_auth(razorpay_settings, data=None):
	"""Return ``(api_key, api_secret)`` for HTTP basic auth to Razorpay.

	``razorpay_settings`` may be a Doc or the name of a **Razorpay Settings** row.
	A truthy ``use_sandbox`` (top level of ``data`` or under ``notes``) swaps in
	the sandbox credentials from site config.
	"""
	if isinstance(razorpay_settings, str):
		razorpay_settings = frappe.get_doc("Razorpay Settings", razorpay_settings)
	api_key = razorpay_settings.api_key
	api_secret = razorpay_settings.get_password(fieldname="api_secret", raise_exception=False)
	data = data or {}
	if cint((data.get("notes") or {}).get("use_sandbox")) or data.get("use_sandbox"):
		api_key = frappe.conf.sandbox_api_key
		api_secret = frappe.conf.sandbox_api_secret
	return api_key, api_secret


def get_razorpay_client(razorpay_settings):
	"""Return a ``razorpay.Client`` bound to this Settings doc (or name).

	Builds a fresh client per call so concurrent requests for different
	credentials do not share mutable module-level auth state.
	"""
	import razorpay

	api_key, api_secret = get_razorpay_auth(razorpay_settings)
	return razorpay.Client(auth=(api_key, api_secret))


def to_minor_units(amount, currency):
	"""Convert a human amount to the integer Razorpay expects (e.g. 12.50 INR -> 1250).

	Zero-decimal currencies pass through; three-decimal currencies (KWD, BHD, OMR…)
	use a 1/1000 minor unit (e.g. 295.991 KWD -> 295991).
	"""
	code = (currency or "").upper()
	if code in ZERO_DECIMAL_CURRENCIES:
		return round(flt(amount))
	if code in THREE_DECIMAL_CURRENCIES:
		return round(flt(amount) * 1000)
	return round(flt(amount) * 100)


def from_minor_units(amount, currency):
	"""Inverse of to_minor_units (e.g. 1250 INR -> 12.50)."""
	code = (currency or "").upper()
	if code in ZERO_DECIMAL_CURRENCIES:
		return flt(amount)
	if code in THREE_DECIMAL_CURRENCIES:
		return flt(amount) / 1000.0
	return flt(amount) / 100.0


def idempotency_key(*parts):
	"""Deterministic key for safe retries of Order create / refunds.

	Same inputs → same key. Prefer stable ERPNext identifiers (reference name,
	amount, operation), never a timestamp or random value.
	"""
	raw = ":".join(str(p) for p in parts if p is not None)
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_payment_signature(order_id, payment_id, signature, secret):
	"""Verify the Checkout success signature: HMAC_SHA256(order_id|payment_id).

	This binds a razorpay_payment_id to the order we created, so a payment that
	succeeded for a different (cheaper) order cannot settle this one.
	"""
	return _signature_ok(f"{order_id}|{payment_id}", signature, secret)


def verify_subscription_signature(subscription_id, payment_id, signature, secret):
	"""Verify the subscription Checkout signature: HMAC_SHA256(payment_id|subscription_id)."""
	return _signature_ok(f"{payment_id}|{subscription_id}", signature, secret)


def verify_webhook_signature(body, signature, secret):
	"""Verify the X-Razorpay-Signature header against the raw webhook body."""
	return _signature_ok(body, signature, secret)


def _signature_ok(message, signature, secret):
	# ``message`` is a str for payment/subscription signatures and raw bytes for
	# webhook body verification; coerce both to bytes before HMAC.
	if isinstance(message, str):
		message = message.encode("utf-8")
	elif message is None:
		message = b""
	secret_b = (secret or "").encode("utf-8") if isinstance(secret, str) else (secret or b"")
	expected = hmac.new(secret_b, message, hashlib.sha256).hexdigest()
	return bool(signature) and hmac.compare_digest(expected, signature)
