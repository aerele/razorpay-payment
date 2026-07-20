# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Razorpay-specific reference helpers. The gateway-agnostic ones (subscription
# flag, success redirect, Payment Request settlement) live in payment_core and
# are re-exported here for callers inside this app.

from payment_core.utils import (
	authorize_reference,
	is_subscription_reference,
	settle_payment_request,
	success_redirect,
)

__all__ = [
	"authorize_reference",
	"get_razorpay_notes",
	"is_subscription_reference",
	"settle_payment_request",
	"success_redirect",
]


def get_razorpay_notes(data=None, integration_request=None):
	"""String notes map for Razorpay Orders / Payments (string → string)."""
	data = data or {}
	notes = {
		"reference_doctype": data.get("reference_doctype"),
		"reference_docname": data.get("reference_docname"),
		"integration_request": integration_request,
	}
	return {k: str(v) for k, v in notes.items() if v is not None}
