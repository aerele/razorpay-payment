# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Razorpay webhook verification, dedupe and dispatch. The HTTP endpoint stays at
# ...doctype.razorpay_settings.webhooks and delegates here.

import json

import frappe
from frappe.utils.password import get_decrypted_password

from razorpay_payment.gateway import reconciliation
from razorpay_payment.gateway.client import verify_webhook_signature

WEBHOOK_SECRET_CACHE_KEY = "razorpay_webhook_secret"


def construct_event(raw_body, signature):
	"""Verify the Razorpay webhook signature against the configured secret.

	Returns the parsed event dict on success, None on failure.
	"""
	secret = get_webhook_secret()
	if not secret:
		frappe.log_error(
			title="Razorpay Webhook Secret Missing",
			message="Razorpay sent a webhook, but no Webhook Secret is configured in Razorpay Settings. "
			"The webhook was rejected. Please configure the secret to automate settlement.",
		)
		return None

	if verify_webhook_signature(raw_body, signature, secret):
		return json.loads(raw_body)
	return None


def handle_event(raw_body, event_id, settings):
	"""Dedupe, log, and dispatch a verified Razorpay webhook event.

	The caller is responsible for elevation (set_user Administrator in try/finally).
	"""
	prior = frappe.db.get_value(
		"Razorpay Webhook Log", {"razorpay_event_id": event_id}, ["name", "status"], as_dict=True
	)
	if prior and prior.status != "Failed":
		return {"status": "duplicate"}

	if prior and prior.status == "Failed":
		log = frappe.get_doc("Razorpay Webhook Log", prior.name)
	else:
		event_data = json.loads(raw_body)
		log = frappe.get_doc(
			{
				"doctype": "Razorpay Webhook Log",
				"razorpay_event_id": event_id,
				"event_type": event_data.get("event"),
				"razorpay_payment_id": event_data.get("payload", {})
				.get("payment", {})
				.get("entity", {})
				.get("id"),
				"status": "Received",
				"razorpay_settings": settings.name,
				"payload": raw_body,
			}
		)
		try:
			log.insert()
		except frappe.exceptions.DuplicateEntryError:
			return {"status": "duplicate"}

	frappe.db.savepoint("razorpay_webhook_handler")
	try:
		result = reconciliation.route_event(json.loads(raw_body), settings) or {}
		log.db_set("status", result.get("status_label", "Processed"), update_modified=False)
		if result.get("reference_doctype"):
			log.db_set("reference_doctype", result["reference_doctype"], update_modified=False)
		if result.get("reference_name"):
			log.db_set("reference_name", result["reference_name"], update_modified=False)
	except Exception:
		frappe.db.rollback(save_point="razorpay_webhook_handler")
		log.db_set("status", "Failed", update_modified=False)
		log.db_set("error", frappe.get_traceback(), update_modified=False)
		frappe.log_error(frappe.get_traceback(), "Razorpay webhook processing failed")
		frappe.local.response["http_status_code"] = 500
		return {"status": "error"}

	return {"status": "ok"}


def get_webhook_secret():
	"""Cache-through: read the encrypted webhook_secret from Razorpay Settings."""

	def _load():
		return get_decrypted_password(
			"Razorpay Settings", "Razorpay Settings", "webhook_secret", raise_exception=False
		)

	return frappe.cache().get_value(WEBHOOK_SECRET_CACHE_KEY, _load)


def clear_webhook_secret_cache():
	"""Invalidate the cache (called from on_update/on_trash)."""
	frappe.cache().delete_value(WEBHOOK_SECRET_CACHE_KEY)
