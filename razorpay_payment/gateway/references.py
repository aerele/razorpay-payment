# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Razorpay-specific reference helpers. The gateway-agnostic ones (subscription
# flag, success redirect, Payment Request settlement) live in payment_core and
# are re-exported here for callers inside this app.

import frappe
from frappe import _
from payment_core.utils import (
	authorize_reference,
	is_subscription_reference,
	settle_payment_request,
	success_redirect,
)

__all__ = [
	"assert_reference_payable",
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


def assert_reference_payable(reference_doctype, reference_docname):
	"""Block checkout when the reference is cancelled or already paid."""
	if not (reference_doctype and reference_docname):
		return
	if not frappe.db.exists(reference_doctype, reference_docname):
		frappe.throw(_("Invalid payment reference."), frappe.ValidationError)

	fields = ["docstatus"]
	meta = frappe.get_meta(reference_doctype)
	if meta.has_field("status"):
		fields.append("status")

	row = frappe.db.get_value(reference_doctype, reference_docname, fields, as_dict=True)
	if not row:
		frappe.throw(
			_("Payment reference {0} no longer exists.").format(reference_docname),
			frappe.ValidationError,
		)

	if row.docstatus == 2 or row.get("status") == "Cancelled":
		frappe.throw(
			_("The {0} {1} has been cancelled and cannot be paid.").format(
				_(reference_doctype), frappe.bold(reference_docname)
			)
		)

	if row.get("status") == "Paid":
		if reference_doctype == "Payment Request":
			frappe.throw(
				_("The Payment Request {0} is already paid, cannot process payment twice").format(
					frappe.bold(reference_docname)
				)
			)
		frappe.throw(_("This payment has already been paid."))
