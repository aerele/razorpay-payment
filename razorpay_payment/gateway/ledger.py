# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT
#
# The shared Payment Transaction ledger side of Razorpay: creating the
# transaction at checkout, mirroring provider state onto it, and the
# controller contract pieces payment_core dispatches to (reconcile, refund,
# webhook log). Everything here no-ops on gateway-only sites (ERPNext or
# apps with their own payment flow), where no Payment Transaction table
# exists and the reference document plus Integration Request track the
# attempt — mirroring stripe_payment's ledger integration.

import frappe
from payment_core.payment_ledger.status_flow import SETTLED_TRANSACTION_STATUSES
from payment_core.utils import (
	authorize_reference,
	ensure_reference_transaction,
	update_payment_transaction,
)


def gateway_string() -> str:
	"""The canonical gateway identity: `<Payment Gateway row name>:<row name>`.

	Must match payment_core's desk action so checkout-created and desk-created
	transactions for the same reference reuse each other. Razorpay Settings is
	a Single and registers exactly one Payment Gateway row, named ``Razorpay``.
	"""
	return "Razorpay:Razorpay"


def ensure_payment_transaction(settings, data):
	"""Create (or reuse) the shared transaction row for a Razorpay attempt.

	Returns the transaction name, or None on gateway-only sites (no ledger
	table). The name is written back into ``data.payment_transaction`` so the
	order notes and later sync calls can carry it.
	"""
	data = data or settings.data
	transaction = data.get("payment_transaction")
	if transaction:
		return transaction
	if not frappe.db.table_exists("Payment Transaction"):
		return None
	transaction = ensure_reference_transaction(
		data.get("reference_doctype"),
		data.get("reference_docname"),
		gateway_string(),
	)
	# item assignment: callers pass either frappe._dict or a plain dict
	data["payment_transaction"] = transaction
	return transaction


def sync_payment_transaction(data, status, **kwargs):
	"""Mirror Razorpay state onto the shared transaction ledger.

	No-op without a transaction name (gateway-only sites, or attempts that
	predated this integration). ``metadata`` accumulates into the transaction's
	diagnostics, merged by payment_core.
	"""
	transaction = (data or {}).get("payment_transaction")
	if not transaction:
		return None
	if status == "Failed" and kwargs.get("error_message"):
		current_status = frappe.db.get_value("Payment Transaction", transaction, "status")
		if current_status in SETTLED_TRANSACTION_STATUSES:
			kwargs.pop("error_message")
	return update_payment_transaction(transaction, status=status, **kwargs)


def settle_ledger_reference(data, status_changed_to):
	"""Settle a mapped reference through payment_core's generic settler.

	Payment Requests and duck-typed own-flow references are settled by the
	controller's authorize_reference; this is the shared-ledger counterpart
	for plain-Frappe sites, where payment_core's authorize_reference marks
	the mapped reference paid and books the receipt.
	"""
	reference_doctype = (data or {}).get("reference_doctype")
	reference_docname = (data or {}).get("reference_docname")
	if not (reference_doctype and reference_docname):
		return None
	return authorize_reference(reference_doctype, reference_docname, status_changed_to)
