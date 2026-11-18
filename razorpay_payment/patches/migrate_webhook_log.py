# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT

import json

import frappe


def execute():
	"""Move Razorpay Webhook Log rows onto payment_core's shared Payment Webhook Log.

	Webhook intake moved to the shared log; existing rows carry history the
	desk still needs, and *Failed* ones are retryable by the hourly sweep.
	Each row migrates in its own savepoint so one bad payload cannot stop
	the migrate. The old doctype and its rows stay untouched for audit.
	"""
	if not frappe.db.exists("DocType", "Razorpay Webhook Log"):
		return
	if not frappe.db.table_exists("Payment Webhook Log"):
		return

	from payment_core.webhooks import get_or_create_log, mark_processed

	rows = frappe.get_all(
		"Razorpay Webhook Log",
		fields=["name", "razorpay_event_id", "event_type", "razorpay_payment_id", "status", "payload"],
	)
	for row in rows:
		try:
			payload = json.loads(row.payload) if row.payload else None
		except Exception:
			frappe.clear_last_message()
			payload = None
		savepoint = f"rzp_wh_migrate_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(savepoint)
		try:
			log, _duplicate = get_or_create_log(
				source="Razorpay:Razorpay",
				event_id=row.razorpay_event_id,
				event_type=row.event_type,
				payload=payload,
				provider_reference=row.razorpay_payment_id,
			)
			if log and row.status == "Processed":
				mark_processed(log.name, status=row.status)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			frappe.clear_last_message()
			frappe.log_error(
				title="Razorpay webhook log migration failed",
				message=f"Razorpay Webhook Log {row.name}: {frappe.get_traceback()}",
				reference_doctype="Razorpay Webhook Log",
				reference_name=row.name,
			)
