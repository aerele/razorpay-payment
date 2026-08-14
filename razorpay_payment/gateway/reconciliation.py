# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Maps verified Razorpay webhook events onto ERPNext records; each handler is idempotent.

import json
from datetime import datetime, timezone

import frappe
from frappe import _
from frappe.integrations.utils import make_get_request
from frappe.utils import cint, flt, getdate, now_datetime, nowdate
from payment_core.utils import erpnext_app_import_guard

from razorpay_payment.gateway.client import from_minor_units, get_razorpay_auth
from razorpay_payment.gateway.constants import RAZORPAY_API_BASE
from razorpay_payment.gateway.subscriptions import link_razorpay_subscription


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

	# Subscription payments are owned by subscription.charged; payment.captured
	# skips them to avoid a double PE on the auth transaction.
	if payment_entity.get("subscription_id"):
		return {"status_label": "Ignored"}

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
	"""Bulk-update sweep outcomes: failed rows increment retry_count, processed rows set status."""
	log = frappe.qb.DocType("Razorpay Webhook Log")
	if failed:
		frappe.qb.update(log).set(log.status, "Failed").set(log.retry_count, log.retry_count + 1).where(
			log.name.isin(failed)
		).run()
	for status_label, names in processed.items():
		if names:
			frappe.qb.update(log).set(log.status, status_label).where(log.name.isin(names)).run()


def reconcile_subscription_charged(event, settings):  # dispatched via _HANDLERS
	"""subscription.charged — settle a recurring cycle against the period's Sales Invoice."""
	payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
	payment_id = payment_entity.get("id")
	subscription_entity = event.get("payload", {}).get("subscription", {}).get("entity", {})
	subscription_id = subscription_entity.get("id")

	if not payment_id or not subscription_id:
		return {"status_label": "Ignored"}

	# Ledger-level dedupe: one Payment Entry per Razorpay payment id.
	if frappe.db.exists("Payment Entry", {"reference_no": payment_id, "docstatus": 1}):
		return {"status_label": "Ignored"}

	erpnext_sub = _find_linked_subscription(subscription_id, settings)
	if not erpnext_sub:
		return {"status_label": "Ignored"}

	# Match the Sales Invoice by billing period, not just oldest unpaid.
	start_ts = subscription_entity.get("current_start")
	end_ts = subscription_entity.get("current_end")
	period_start = getdate(datetime.fromtimestamp(start_ts, tz=timezone.utc)) if start_ts else None
	period_end = getdate(datetime.fromtimestamp(end_ts, tz=timezone.utc)) if end_ts else None

	si = _find_unpaid_sales_invoice(erpnext_sub, period_start, period_end)
	if not si:
		# Razorpay charged before the scheduler generated this period's invoice — generate it now.
		sub_doc = frappe.get_doc("Subscription", erpnext_sub)
		if getdate(nowdate()) < getdate(sub_doc.next_billing_period_start):
			return {"status_label": "Ignored"}  # future subscription — can't generate yet
		sub_doc.process(posting_date=period_end or getdate(now_datetime()))
		si = _find_unpaid_sales_invoice(erpnext_sub, period_start, period_end)

	# No Sales Invoice found (or generation failed), or it is already fully paid.
	if not si or flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount")) <= 0:
		return {"status_label": "Ignored"}

	# Use the actual charge amount, not the full Sales Invoice outstanding.
	charged_amount = from_minor_units(payment_entity.get("amount"), payment_entity.get("currency"))

	with erpnext_app_import_guard():
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	pe = get_payment_entry("Sales Invoice", si, party_amount=charged_amount)
	pe.reference_no = payment_id
	pe.reference_date = nowdate()
	if pe.meta.has_field("razorpay_payment_id"):
		pe.razorpay_payment_id = payment_id
	pe.insert()
	pe.submit()

	return {
		"status_label": "Processed",
		"reference_doctype": "Sales Invoice",
		"reference_name": si,
	}


def link_subscription_on_activation(event, settings):  # dispatched via _HANDLERS
	"""subscription.activated — stamp the link from notes (self-heal entry point)."""
	subscription_entity = event.get("payload", {}).get("subscription", {}).get("entity", {})
	subscription_id = subscription_entity.get("id")
	notes = subscription_entity.get("notes") or {}
	erpnext_sub = notes.get("erpnext_subscription")

	if erpnext_sub and frappe.db.exists("Subscription", erpnext_sub):
		link_razorpay_subscription(erpnext_sub, subscription_id)
		return {
			"status_label": "Processed",
			"reference_doctype": "Subscription",
			"reference_name": erpnext_sub,
		}
	return {"status_label": "Ignored"}


def sync_subscription_status(event, settings):  # dispatched via _HANDLERS
	"""subscription.cancelled/.completed/.expired/.paused/.updated — comment only.

	Like the Stripe handler, we deliberately do NOT mutate the linked
	Subscription status from a webhook — that stays an auditable, manual action.
	"""
	subscription_entity = event.get("payload", {}).get("subscription", {}).get("entity", {})
	subscription_id = subscription_entity.get("id")
	notes = subscription_entity.get("notes") or {}
	erpnext_sub = notes.get("erpnext_subscription") or _subscription_from_razorpay_id(subscription_id)

	if not erpnext_sub:
		return {"status_label": "Ignored"}

	frappe.get_doc("Subscription", erpnext_sub).add_comment(
		"Comment",
		_("Razorpay subscription {0}: {1}.").format(subscription_id, event.get("event")),
	)
	return {
		"status_label": "Processed",
		"reference_doctype": "Subscription",
		"reference_name": erpnext_sub,
	}


def _find_linked_subscription(subscription_id, settings):
	"""Two-step resolver: custom field first, then notes self-heal."""
	erpnext_sub = _subscription_from_razorpay_id(subscription_id)
	if erpnext_sub:
		return erpnext_sub

	# Self-heal from Razorpay notes, then re-stamp the link.
	api_key, api_secret = get_razorpay_auth(settings)
	resp = make_get_request(
		f"{RAZORPAY_API_BASE}/subscriptions/{subscription_id}",
		auth=(api_key, api_secret),
	)
	notes = resp.get("notes") or {}
	erpnext_sub = notes.get("erpnext_subscription")
	if erpnext_sub and frappe.db.exists("Subscription", erpnext_sub):
		link_razorpay_subscription(erpnext_sub, subscription_id)
		return erpnext_sub
	return None


def _subscription_from_razorpay_id(subscription_id):
	if not subscription_id or not frappe.db.has_column("Subscription", "razorpay_subscription_id"):
		return None
	return frappe.db.get_value("Subscription", {"razorpay_subscription_id": subscription_id}, "name")


def _find_unpaid_sales_invoice(erpnext_sub, period_start, period_end):
	"""Find the Sales Invoice for this charge's billing period (exact → overlap)."""
	if not frappe.db.has_column("Sales Invoice", "subscription"):
		return None
	if not (period_start and period_end):
		return None

	filters = {"subscription": erpnext_sub, "docstatus": 1, "is_return": 0}

	# Tier 1: invoice whose billing period equals this charge's period — the
	# definitive match, matched even when already paid so the caller's
	# double-book guard can fire.
	exact = frappe.get_all(
		"Sales Invoice",
		filters={**filters, "from_date": period_start, "to_date": period_end},
		pluck="name",
		limit=1,
	)
	if exact:
		return exact[0]

	# Tier 2: day-level drift tolerance — an unpaid invoice whose period
	# overlaps the charge's period, so a date range instead of equality.
	overlap = frappe.get_all(
		"Sales Invoice",
		filters={
			**filters,
			"outstanding_amount": (">", 0),
			"from_date": ("<=", period_end),
			"to_date": (">=", period_start),
		},
		pluck="name",
		order_by="posting_date asc",
		limit=1,
	)
	return overlap[0] if overlap else None


# Dispatch table at the very bottom
_HANDLERS = {
	"payment.captured": reconcile_payment_captured,
	"payment.failed": mark_payment_failed,
	"subscription.activated": link_subscription_on_activation,
	"subscription.charged": reconcile_subscription_charged,
	"subscription.cancelled": sync_subscription_status,
	"subscription.completed": sync_subscription_status,
	"subscription.expired": sync_subscription_status,
	"subscription.paused": sync_subscription_status,
	"subscription.updated": sync_subscription_status,
}
