# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE
#
# Razorpay webhook verification, dedupe and dispatch. The guest endpoint
# razorpay_settings.webhooks delegates here.

import frappe

from razorpay_payment.gateway.client import verify_webhook_signature


def handle_request():
	"""Verify the X-Razorpay-Signature on the raw body, then dispatch the event."""
	raw = frappe.request.get_data() if frappe.request else b""
	body = raw.decode("utf-8") if isinstance(raw, bytes) else (raw or "")
	signature = frappe.get_request_header("X-Razorpay-Signature")

	settings = frappe.get_doc("Razorpay Settings")
	secret = settings.get_password("webhook_secret", raise_exception=False)

	if not (secret and signature and verify_webhook_signature(body, signature, secret)):
		frappe.local.response["http_status_code"] = 400
		return {"status": "invalid signature"}

	event = frappe.parse_json(body) or {}
	return handle_event(event, settings)


def handle_event(event, settings):
	"""Dedupe on the Razorpay event id, then route to the reconciler.

	A prior *Failed* attempt is allowed to reprocess (its log row is reused); any
	other prior status is a genuine duplicate delivery and is skipped.
	"""
	# Signature-verified above; run reconciliation as Administrator so the handlers
	# can read/write ERPNext docs (the endpoint itself is allow_guest).
	frappe.set_user("Administrator")

	event_id = frappe.get_request_header("X-Razorpay-Event-Id") or event.get("id")
	if not event_id:
		frappe.local.response["http_status_code"] = 400
		return {"status": "missing event id"}

	prior = frappe.db.get_value(
		"Razorpay Webhook Log", {"razorpay_event_id": event_id}, ["name", "status"], as_dict=True
	)
	if prior and prior.status != "Failed":
		return {"status": "duplicate"}

	if prior:
		log = frappe.get_doc("Razorpay Webhook Log", prior.name)
	else:
		obj = _event_object(event)
		log = frappe.get_doc(
			{
				"doctype": "Razorpay Webhook Log",
				"razorpay_event_id": event_id,
				"event_type": event.get("event"),
				"razorpay_object_id": obj.get("id"),
				"status": "Received",
				"payload": frappe.as_json(event),
			}
		)
		try:
			log.insert(ignore_permissions=True)
			frappe.db.commit()  # persist the dedupe row before doing any work
		except (frappe.exceptions.DuplicateEntryError, frappe.UniqueValidationError):
			# Concurrent delivery already inserted this event id (unique key).
			frappe.db.rollback()
			return {"status": "duplicate"}

	try:
		from razorpay_payment.gateway import reconciliation

		result = reconciliation.route_event(event, settings) or {}
		log.db_set("status", result.get("status_label", "Processed"), update_modified=False)
		if result.get("reference_doctype"):
			log.db_set("reference_doctype", result.get("reference_doctype"), update_modified=False)
			log.db_set("reference_name", result.get("reference_name"), update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		log.db_set("status", "Failed", update_modified=False)
		log.db_set("error", frappe.get_traceback(), update_modified=False)
		frappe.db.commit()
		frappe.log_error(frappe.get_traceback(), "Razorpay webhook processing failed")
		# Transient failure — ask Razorpay to retry; the dedupe above reprocesses Failed.
		frappe.local.response["http_status_code"] = 500
		return {"status": "error"}

	return {"status": "ok"}


def _event_object(event):
	payload = event.get("payload") or {}
	for key in ("payment", "order", "refund", "subscription"):
		entity = (payload.get(key) or {}).get("entity")
		if entity:
			return entity
	return {}
