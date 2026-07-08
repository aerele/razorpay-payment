# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE
#
# Shared Razorpay primitives: auth resolution, minor-unit conversion and
# signature verification. Razorpay's REST calls go through frappe's
# make_*_request with an inline (key, secret) tuple, so there is no shared
# SDK client to guard for thread-safety.

import hashlib
import hmac

import frappe
from frappe.utils import cint, flt

RAZORPAY_API = "https://api.razorpay.com/v1"


def resolve_auth(doc, data=None):
	"""Return the (api_key, api_secret) tuple, honouring a sandbox override.

	`data` is the Integration Request payload; a truthy `use_sandbox` (top level
	or under `notes`) swaps in the sandbox credentials from site config.
	"""
	key = doc.api_key
	secret = doc.get_password(fieldname="api_secret", raise_exception=False)
	data = data or {}
	if cint((data.get("notes") or {}).get("use_sandbox")) or data.get("use_sandbox"):
		key = frappe.conf.sandbox_api_key
		secret = frappe.conf.sandbox_api_secret
	return key, secret


def to_paisa(amount):
	"""Rupee (major unit) -> integer paisa the Razorpay API expects (12.50 -> 1250)."""
	return round(flt(amount) * 100)


def from_paisa(amount):
	"""Inverse of to_paisa (1250 -> 12.50)."""
	return flt(amount) / 100.0


def verify_payment_signature(order_id, payment_id, signature, secret):
	"""Verify the Checkout success signature: HMAC_SHA256(order_id|payment_id).

	This binds a razorpay_payment_id to the order we created, so a payment that
	succeeded for a different (cheaper) order cannot settle this one.
	"""
	return _signature_ok(f"{order_id}|{payment_id}", signature, secret)


def verify_webhook_signature(body, signature, secret):
	"""Verify the X-Razorpay-Signature header against the raw webhook body."""
	return _signature_ok(body, signature, secret)


def _signature_ok(message, signature, secret):
	expected = hmac.new(
		bytes(secret or "", "utf-8"), bytes(message or "", "utf-8"), hashlib.sha256
	).hexdigest()
	return bool(signature) and hmac.compare_digest(expected, signature)
