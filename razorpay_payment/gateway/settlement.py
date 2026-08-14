# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import frappe
from frappe.utils import flt, nowdate


def settle_payment_request(settings, pr, payment_id=None):
	"""Create a Payment Entry, allowing a second PE for partial payments.

	``payment_id`` (the Razorpay payment id, e.g. pay_xxx) is stamped onto the
	PE's ``reference_no`` so the webhook's ledger-level dedupe and verification
	can find it. Falls back to the PR name when no payment id is available
	(e.g. the non-webhook settlement path).
	"""
	if pr.docstatus != 1 or pr.status == "Paid":
		return

	from payment_core.utils import erpnext_app_import_guard

	with erpnext_app_import_guard():
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
		from erpnext.accounts.doctype.payment_request.payment_request import (
			get_existing_payment_entry,
		)

	# Allow a second PE while the reference doc is still owed (Sales Orders have no
	# outstanding_amount, so the guard is skipped for them).
	if pr.reference_name and get_existing_payment_entry(pr.reference_name):
		if frappe.get_meta(pr.reference_doctype).has_field("outstanding_amount"):
			si_outstanding = flt(
				frappe.db.get_value(pr.reference_doctype, pr.reference_name, "outstanding_amount")
			)
			if si_outstanding <= 0:
				return

	original_user = frappe.session.user
	try:
		frappe.set_user("Administrator")  # nosemgrep
		payment_entry = get_payment_entry(
			pr.reference_doctype,
			pr.reference_name,
			party_amount=flt(pr.outstanding_amount),
			bank_account=pr.payment_account,
		)
		payment_entry.reference_no = payment_id or pr.name
		payment_entry.reference_date = nowdate()
		payment_entry.insert()
		payment_entry.submit()
	finally:
		frappe.set_user(original_user)  # nosemgrep
