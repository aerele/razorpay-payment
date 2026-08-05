# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Maps verified Razorpay webhook events onto ERPNext records; each handler is idempotent.

import json

import frappe
from frappe import _
from frappe.utils import cint


def route_event(event, settings):
	"""Dispatch a Razorpay webhook event to its handler."""
	handler = _HANDLERS.get(event.get("event"))
	if not handler:
		return {"status_label": "Ignored"}
	return handler(event, settings)


def reconcile_payment_captured(event, settings):  # dispatched via _HANDLERS
	"""payment.captured — settle the Payment Request linked via order notes."""
	payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
	payment_id = payment_entity.get("id")
	notes = payment_entity.get("notes", {})

	reference_doctype = notes.get("reference_doctype")
	reference_docname = notes.get("reference_docname")

	if not reference_doctype or not reference_docname:
		return {"status_label": "Ignored"}

	# De-dupe at the ledger level: one Payment Entry per Razorpay payment id.
	if frappe.db.exists("Payment Entry", {"reference_no": payment_id, "docstatus": 1}):
		return {"status_label": "Ignored"}

	ref = frappe.get_doc(reference_doctype, reference_docname)
	if hasattr(ref, "on_payment_authorized"):
		ref.run_method("on_payment_authorized", "Completed")
	elif ref.doctype == "Payment Request":
		settings.settle_payment_request(ref, payment_id=payment_id)

	# Verify settlement actually produced a Payment Entry. If it silently failed,
	# throw an error so the webhook savepoint rolls back and marks the log Failed.
	if not frappe.db.exists("Payment Entry", {"reference_no": payment_id, "docstatus": 1}):
		frappe.throw(_("Settlement failed: Payment Entry was not created for {0}").format(payment_id))

	return {
		"status_label": "Processed",
		"reference_doctype": reference_doctype,
		"reference_name": reference_docname,
	}


def mark_payment_failed(event, settings):  # dispatched via _HANDLERS
	"""payment.failed — mark the Integration Request Failed and comment on the PR."""
	payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
	notes = payment_entity.get("notes", {})

	reference_doctype = notes.get("reference_doctype")
	reference_docname = notes.get("reference_docname")

	if reference_doctype and reference_docname:
		doc = frappe.get_doc(reference_doctype, reference_docname)
		doc.add_comment(
			"Comment",
			_("Razorpay payment failed: {0}").format(
				payment_entity.get("error_description", "Unknown error")
			),
		)

	return {"status_label": "Processed"}


def sweep_pending():
	"""Hourly retry sweep for Failed webhook events (max 5 retries, batch of 50)."""
	MAX_RETRIES = 5
	batch_size = cint(frappe.conf.get("razorpay_webhook_sweep_batch_size")) or 50

	try:
		failed_logs = frappe.get_all(
			"Razorpay Webhook Log",
			filters={"status": "Failed", "retry_count": ("<", MAX_RETRIES)},
			fields=["name", "razorpay_settings", "payload", "retry_count"],
			limit=batch_size,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Razorpay webhook sweep failed")
		return

	if not failed_logs:
		return

	settings_cache = {}
	failed = []
	processed = {}

	for row in failed_logs:
		frappe.db.savepoint("razorpay_webhook_sweep_row")
		try:
			event = json.loads(row.payload)
			settings = settings_cache.get(row.razorpay_settings) or frappe.get_doc(
				"Razorpay Settings", row.razorpay_settings
			)
			settings_cache[row.razorpay_settings] = settings

			status_label = (route_event(event, settings) or {}).get("status_label", "Processed")
			processed.setdefault(status_label, []).append(row.name)
		except Exception:
			frappe.db.rollback(save_point="razorpay_webhook_sweep_row")
			frappe.log_error(frappe.get_traceback(), "Razorpay webhook sweep row failed")
			failed.append(row.name)

	_apply_sweep_status(failed, processed)


def _apply_sweep_status(failed, processed):
	"""Bulk UPDATE outcomes: failed increments retry_count, processed updates status."""
	Log = frappe.qb.DocType("Razorpay Webhook Log")
	if failed:
		frappe.qb.update(Log).set(Log.status, "Failed").set(Log.retry_count, Log.retry_count + 1).where(
			Log.name.isin(failed)
		).run()
	for status_label, names in processed.items():
		if names:
			frappe.qb.update(Log).set(Log.status, status_label).where(Log.name.isin(names)).run()


# Dispatch table at the very bottom
_HANDLERS = {
	"payment.captured": reconcile_payment_captured,
	"payment.failed": mark_payment_failed,
}
