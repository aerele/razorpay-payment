# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE
#
# Razorpay webhook event routing. Settles one-off payments idempotently so a
# capture whose browser callback never fired is still booked.

import json

import frappe
from frappe import _

from razorpay_payment.gateway.client import from_paisa


def route_event(event, settings):
	handler = _HANDLERS.get(event.get("event"))
	if not handler:
		return {"status_label": "Ignored"}
	return handler(event, settings)


def reconcile_payment(event, settings):
	"""A captured/paid webhook: settle its Integration Request idempotently.

	Recovers a payment whose browser callback never fired. authorize_payment is
	row-locked and order/amount-bound, so re-driving it here is safe even when the
	redirect already settled the same payment.
	"""
	payment = _entity(event, "payment")
	order_id = payment.get("order_id")
	if not order_id:
		return {"status_label": "Ignored"}

	ir_name = _find_integration_request(order_id)
	if not ir_name:
		return {"status_label": "Ignored"}

	ir = frappe.get_doc("Integration Request", ir_name)
	data = json.loads(ir.data)
	data["razorpay_payment_id"] = payment.get("id")

	controller = frappe.get_doc("Razorpay Settings")
	controller.integration_request = ir
	controller.data = frappe._dict(data)
	controller.authorize_payment()

	return {
		"status_label": "Processed",
		"reference_doctype": data.get("reference_doctype"),
		"reference_name": data.get("reference_docname"),
	}


def mark_payment_failed(event, settings):
	"""A failed payment webhook: mark the Integration Request Failed unless settled."""
	order_id = _entity(event, "payment").get("order_id")
	ir_name = _find_integration_request(order_id) if order_id else None
	if not ir_name:
		return {"status_label": "Ignored"}
	if frappe.db.get_value("Integration Request", ir_name, "status") not in (
		"Authorized",
		"Completed",
		"Verified",
	):
		frappe.db.set_value("Integration Request", ir_name, "status", "Failed", update_modified=False)
	return {"status_label": "Processed"}


def process_refund(event, settings):
	"""A refund webhook: flag the linked Payment Entry (mirrors Stripe — a comment)."""
	refund = _entity(event, "refund")
	payment_id = refund.get("payment_id")
	if not payment_id:
		return {"status_label": "Ignored"}
	pe = frappe.db.get_value("Payment Entry", {"razorpay_payment_id": payment_id, "docstatus": 1}, "name")
	if not pe:
		return {"status_label": "Ignored"}
	amount = from_paisa(refund.get("amount") or 0)
	frappe.get_doc("Payment Entry", pe).add_comment(
		"Comment",
		_("Razorpay refund processed: {0} (refund {1}). Post a credit note / reversal if required.").format(
			amount, refund.get("id")
		),
	)
	return {"status_label": "Processed", "reference_doctype": "Payment Entry", "reference_name": pe}


def _entity(event, key):
	return ((event.get("payload") or {}).get(key) or {}).get("entity") or {}


def _find_integration_request(order_id):
	return frappe.db.get_value(
		"Integration Request",
		{"integration_request_service": "Razorpay", "data": ("like", f"%{order_id}%")},
		"name",
	)


_HANDLERS = {
	"payment.captured": reconcile_payment,
	"order.paid": reconcile_payment,
	"payment.failed": mark_payment_failed,
	"refund.processed": process_refund,
	"refund.created": process_refund,
}
