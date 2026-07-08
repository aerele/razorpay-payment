# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE
#
# Razorpay customer resolution (reuse the same Razorpay customer per ERPNext
# party) and storage of saved-card tokens returned on a payment. Off-session
# charging via those tokens is intentionally out of scope here.

import json

import frappe
from frappe.integrations.utils import make_post_request

from razorpay_payment.gateway.client import RAZORPAY_API, resolve_auth


def get_party_for_reference(data):
	"""Resolve the ERPNext Customer behind a payment's reference document."""
	dt, dn = data.get("reference_doctype"), data.get("reference_docname")
	if not dt or not dn or not frappe.db.exists(dt, dn):
		return None
	if dt == "Payment Request":
		row = frappe.db.get_value("Payment Request", dn, ["party_type", "party"], as_dict=True)
		return row.party if row and row.party_type == "Customer" else None
	if frappe.get_meta(dt).has_field("customer"):
		return frappe.db.get_value(dt, dn, "customer")
	return None


def get_or_create_customer(data):
	"""Return a Razorpay customer id, reusing Customer.razorpay_customer_id when possible.

	Razorpay's `fail_existing=0` returns the existing customer (matched on contact/
	email) instead of erroring, so repeat calls converge on one customer per party.
	"""
	party = get_party_for_reference(data)
	has_field = bool(party) and frappe.db.has_column("Customer", "razorpay_customer_id")

	if has_field:
		existing = frappe.db.get_value("Customer", party, "razorpay_customer_id")
		if existing:
			return existing

	settings = frappe.get_doc("Razorpay Settings")
	key, secret = resolve_auth(settings, data)
	payload = {"fail_existing": "0", "name": data.get("payer_name") or party}
	if data.get("payer_email"):
		payload["email"] = data.get("payer_email")
	if data.get("payer_contact"):
		payload["contact"] = data.get("payer_contact")
	if party:
		payload["notes"] = {"erpnext_customer": party}

	try:
		customer = make_post_request(
			f"{RAZORPAY_API}/customers",
			auth=(key, secret),
			data=json.dumps(payload),
			headers={"content-type": "application/json"},
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Razorpay customer create failed")
		return None

	customer_id = customer.get("id")
	if has_field and customer_id:
		frappe.db.set_value("Customer", party, "razorpay_customer_id", customer_id, update_modified=False)
	return customer_id


def store_payment_token(payment):
	"""Persist a saved-card token returned on a payment (idempotent on token_id).

	Best-effort post-settlement bookkeeping: a failure here never blocks the
	settlement that called it.
	"""
	try:
		token_id = (payment or {}).get("token_id")
		if not token_id or frappe.db.exists("Razorpay Payment Token", {"token_id": token_id}):
			return
		card = payment.get("card") or {}
		frappe.get_doc(
			{
				"doctype": "Razorpay Payment Token",
				"token_id": token_id,
				"razorpay_customer_id": payment.get("customer_id"),
				"customer": _customer_for_razorpay_id(payment.get("customer_id")),
				"method": payment.get("method"),
				"card_last4": card.get("last4"),
				"card_network": card.get("network"),
				"status": "Active",
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Razorpay token storage failed")


def _customer_for_razorpay_id(razorpay_customer_id):
	if not (razorpay_customer_id and frappe.db.has_column("Customer", "razorpay_customer_id")):
		return None
	return frappe.db.get_value("Customer", {"razorpay_customer_id": razorpay_customer_id}, "name")
