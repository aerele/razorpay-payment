# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT
#
# Razorpay webhook verification, dedupe and dispatch. The HTTP endpoint stays at
# ...doctype.razorpay_settings.webhooks and delegates here. Dedupe and the
# retryable Failed state live in payment_core's shared Payment Webhook Log —
# the same log the desk shows for every gateway — via get_or_create_log /
# mark_processed / mark_failed.

import json

import frappe
from frappe.utils.password import get_decrypted_password
from payment_core.webhooks import DUPLICATE, get_or_create_log, mark_failed, mark_processed

from razorpay_payment.gateway import reconciliation
from razorpay_payment.gateway.client import verify_webhook_signature
from razorpay_payment.gateway.ledger import gateway_string

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


def handle_event(event, settings, event_id=None):
	"""Dedupe on the Razorpay event id, then route to the reconciler.

	``event_id`` is the ``X-Razorpay-Event-ID`` delivery header — the body
	carries no id. A prior *Failed* attempt is allowed to reprocess (its log
	row is reused); any other prior status is a genuine duplicate and is
	skipped.

	The caller (the webhooks endpoint) is responsible for elevation: it has
	already set_user("Administrator") after verifying the signature, and will
	restore the original user in its own finally block.
	"""
	if not event_id:
		event_id = f"{event.get('event')}:{_provider_reference(event)}"
	obj = _provider_reference(event)
	log, duplicate = get_or_create_log(
		source=gateway_string(settings) if settings else "Razorpay",
		event_id=event_id,
		event_type=event.get("event"),
		payload=event,
		provider_reference=obj,
	)
	if duplicate:
		return DUPLICATE

	# Run the handler inside a savepoint: a failure rolls back only the handler's
	# partial writes while keeping the log row, and the request transaction is
	# committed by Frappe once we return — no manual commit required.
	frappe.db.savepoint("razorpay_webhook_handler")
	try:
		result = reconciliation.route_event(event, settings) or {}
		mark_processed(
			log.name,
			status=result.get("status_label", "Processed"),
			reference_doctype=result.get("reference_doctype"),
			reference_docname=result.get("reference_name"),
		)
	except Exception:
		frappe.db.rollback(save_point="razorpay_webhook_handler")
		mark_failed(log.name, frappe.get_traceback())
		frappe.log_error(frappe.get_traceback(), "Razorpay webhook processing failed")
		# Transient failure — ask Razorpay to retry; the dedupe above reprocesses the Failed row.
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


def _provider_reference(event):
	"""The payment/subscription id the event is about, for the log's reference."""
	return event.get("payload", {}).get("payment", {}).get("entity", {}).get("id") or event.get(
		"payload", {}
	).get("subscription", {}).get("entity", {}).get("id")
