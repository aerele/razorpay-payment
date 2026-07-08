# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE
#
# Razorpay refunds. The ledger reversal is flagged on the Payment Entry via the
# refund.processed webhook (mirrors Stripe: a comment, not an automatic reversal).

import frappe
from frappe import _
from frappe.integrations.utils import make_post_request
from frappe.utils import flt

from razorpay_payment.gateway.client import RAZORPAY_API, resolve_auth, to_paisa


def refund_payment(payment_id, amount=None):
	"""Refund a Razorpay payment (full, or a partial `amount` in major units)."""
	settings = frappe.get_doc("Razorpay Settings")
	key, secret = resolve_auth(settings)
	data = {"amount": to_paisa(amount)} if amount else {}
	refund = make_post_request(
		f"{RAZORPAY_API}/payments/{payment_id}/refund",
		auth=(key, secret),
		data=data,
	)
	return {"refund": refund.get("id"), "status": refund.get("status")}


@frappe.whitelist()
def refund_payment_entry(payment_entry: str, amount: float | None = None):
	"""Refund a Razorpay-originated Payment Entry. Books follow via the webhook comment."""
	# Real-money action: require write access to this specific Payment Entry.
	frappe.has_permission("Payment Entry", "write", payment_entry, throw=True)
	payment_id = frappe.db.get_value("Payment Entry", payment_entry, "razorpay_payment_id")
	if not payment_id:
		frappe.throw(_("This Payment Entry has no linked Razorpay payment to refund."))
	return refund_payment(payment_id, flt(amount) if amount else None)
