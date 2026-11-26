# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Maps verified Razorpay webhook events onto ERPNext records; each handler is idempotent.

import json
import re
from datetime import datetime, timezone

import frappe
from frappe import _
from frappe.integrations.utils import make_get_request
from frappe.utils import cint, flt, getdate, now_datetime, nowdate
from payment_core.utils import erpnext_app_import_guard
from payment_core.webhooks import mark_failed, mark_processed

from razorpay_payment.gateway import collect_now
from razorpay_payment.gateway.client import from_minor_units, get_razorpay_auth
from razorpay_payment.gateway.constants import RAZORPAY_API_BASE
from razorpay_payment.gateway.ledger import (
	sync_payment_transaction,
)
from razorpay_payment.gateway.subscriptions import link_razorpay_subscription

MAX_SWEEP_RETRIES = 5
SWEEP_ATTEMPT_RE = re.compile(r"^sweep attempt (\d+):")


def route_event(event, settings):
	"""Dispatch a Razorpay webhook event to its handler."""
	handler = _HANDLERS.get(event.get("event"))
	result = handler(event, settings) if handler else {"status_label": "Ignored"}
	if result.get("reference_name") or event.get("event") == "payment.captured":
		return result

	payload = event.get("payload") or {}
	payment = (payload.get("payment") or {}).get("entity") or {}
	order = (payload.get("order") or {}).get("entity") or {}
	order_id = payment.get("order_id") or order.get("id")
	if order_id and (checkout := collect_now.find_checkout(order_id)):
		result.update(_checkout_reference(checkout))
	return result


def _checkout_reference(checkout):
	return {
		"reference_doctype": checkout.reference_doctype,
		"reference_name": checkout.reference_docname,
	}


def reconcile_payment_captured(event, settings):  # dispatched via _HANDLERS
	"""payment.captured — settle the reference linked via the ORDER's notes.

	The payment entity's notes are set by the browser and cannot be trusted.
	The server minted the order's notes at checkout; the payment's order_id
	resolves to that order, so the binding (reference, amount) is read from
	the order via the Razorpay API, and the payment amount is verified
	against the order amount before any settlement.
	"""
	payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
	payment_id = payment_entity.get("id")
	order_id = payment_entity.get("order_id")

	# Subscription payments are owned by subscription.charged; payment.captured
	# skips them to avoid a double settlement on the auth transaction.
	if payment_entity.get("subscription_id"):
		return {"status_label": "Ignored"}

	if not order_id:
		return {"status_label": "Ignored"}

	checkout = collect_now.find_checkout(order_id)
	if checkout:
		collect_now.verify_and_settle(settings, checkout.name, payment_id=payment_id)
		return {"status_label": "Processed", **_checkout_reference(checkout)}

	# Fetch the order: its notes are the server-minted binding, and its
	# amount is the checkout's amount. Both are compared with the payment.
	api_key, api_secret = get_razorpay_auth(settings)
	order = make_get_request(f"{RAZORPAY_API_BASE}/orders/{order_id}", auth=(api_key, api_secret))
	notes = order.get("notes") or {}

	if cint(payment_entity.get("amount")) != cint(order.get("amount")):
		frappe.throw(_("Payment amount does not match the order amount"))

	reference_doctype = notes.get("reference_doctype")
	reference_docname = notes.get("reference_docname")

	if not reference_doctype or not reference_docname:
		return {"status_label": "Ignored"}

	# Order notes are the authoritative mode signal. ERPNext may have the
	# Payment Transaction table when force_payment_ledger is enabled, and a
	# plain-Frappe test site may have other accounting doctypes installed.
	if notes.get("payment_transaction"):
		sync_payment_transaction(
			{"payment_transaction": notes.get("payment_transaction")},
			"Paid",
			provider_transaction_id=payment_id,
			metadata={"razorpay_payment_id": payment_id},
		)
		return {
			"status_label": "Processed",
			"reference_doctype": reference_doctype,
			"reference_name": reference_docname,
		}

	# De-dupe at the ledger level: one Payment Entry per Razorpay payment id.
	if frappe.db.exists("Payment Entry", {"reference_no": payment_id, "docstatus": 1}):
		return {
			"status_label": "Ignored",
			"reference_doctype": reference_doctype,
			"reference_name": reference_docname,
		}

	ref = frappe.get_doc(reference_doctype, reference_docname)
	if ref.doctype == "Payment Request":
		settings.settle_payment_request(ref, payment_id=payment_id)
	elif hasattr(ref, "on_payment_authorized"):
		ref.run_method("on_payment_authorized", "Completed")

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
	"""payment.failed — fail the ledger transaction and comment on the order-bound reference."""
	payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
	order_id = payment_entity.get("order_id")

	checkout = collect_now.find_checkout(order_id) if order_id else None
	if checkout:
		collect_now.verify_and_settle(settings, checkout.name, payment_id=payment_entity.get("id"))
		return {"status_label": "Processed", **_checkout_reference(checkout)}

	notes = {}
	if order_id:
		try:
			api_key, api_secret = get_razorpay_auth(settings)
			order = make_get_request(f"{RAZORPAY_API_BASE}/orders/{order_id}", auth=(api_key, api_secret))
			notes = order.get("notes") or {}
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Razorpay order lookup failed")

	sync_payment_transaction(
		{"payment_transaction": notes.get("payment_transaction")},
		"Failed",
		error_message=payment_entity.get("error_description") or _("Razorpay payment failed"),
		metadata={"razorpay_payment_id": payment_entity.get("id")},
	)

	reference_doctype = notes.get("reference_doctype")
	reference_docname = notes.get("reference_docname")

	if reference_doctype and reference_docname and frappe.db.exists(reference_doctype, reference_docname):
		doc = frappe.get_doc(reference_doctype, reference_docname)
		doc.add_comment(
			"Comment",
			_("Razorpay payment failed: {0}").format(
				payment_entity.get("error_description", "Unknown error")
			),
		)
		return {
			"status_label": "Processed",
			"reference_doctype": reference_doctype,
			"reference_name": reference_docname,
		}

	return {"status_label": "Processed"}


def sweep_pending():
	"""Scheduler: retry webhook events that failed processing.

	Uses payment_core's shared Payment Webhook Log (the active log). Each run
	loads a bounded batch of Failed Razorpay rows (oldest first, so permanently
	failing rows can't starve newer ones of batch slots), re-runs the processor
	in a savepoint, and records the outcome via mark_processed / mark_failed.
	A row that keeps failing leaves the retry pool after MAX_SWEEP_RETRIES
	attempts.
	"""
	batch_size = cint(frappe.conf.get("razorpay_webhook_sweep_batch_size")) or 50

	try:
		rows = (
			frappe.get_all(
				"Payment Webhook Log",
				filters={"status": "Failed", "source": ("like", "Razorpay%")},
				fields=["name", "payload", "event_type", "error"],
				order_by="creation asc",
				limit=batch_size,
			)
			if frappe.db.table_exists("Payment Webhook Log")
			else []
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Razorpay webhook sweep could not load pending events")
		return

	settings = None
	for row in rows:
		frappe.db.savepoint("razorpay_webhook_sweep_row")
		try:
			event = frappe.parse_json(row.payload)
			settings = settings or frappe.get_doc("Razorpay Settings", "Razorpay Settings")
			result = route_event(event, settings) or {}
			mark_processed(
				row.name,
				status=result.get("status_label", "Processed"),
				reference_doctype=result.get("reference_doctype"),
				reference_docname=result.get("reference_name"),
			)
		except Exception:
			frappe.db.rollback(save_point="razorpay_webhook_sweep_row")
			frappe.log_error(frappe.get_traceback(), "Razorpay webhook sweep failed")
			attempt_no = get_sweep_attempt(row.get("error"))
			if attempt_no + 1 >= MAX_SWEEP_RETRIES:
				# this sweep was the last allowed attempt: leave the retry pool
				# (mark_failed keeps the row Failed) with the reason recorded
				frappe.db.set_value(
					"Payment Webhook Log",
					row.name,
					{"status": "Ignored", "error": f"sweep gave up after {attempt_no + 1} attempts"},
					update_modified=False,
				)
			else:
				mark_failed(row.name, f"sweep attempt {attempt_no + 1}: {frappe.get_traceback()[:200]}")


def get_sweep_attempt(error):
	"""Return the durable sweep attempt count encoded in a log error."""
	match = SWEEP_ATTEMPT_RE.match(error or "")
	return cint(match.group(1)) if match else 0


def preserve_sweep_attempt(previous_error, error):
	"""Keep the sweep count when a manual desk retry writes a new error."""
	attempt = get_sweep_attempt(previous_error)
	prefix = f"sweep attempt {attempt}: " if attempt else ""
	return f"{prefix}{error}"


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
