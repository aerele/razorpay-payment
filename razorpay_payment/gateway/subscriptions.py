# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Subscription plan / ERPNext-Subscription linkage helpers, programmatic
# subscription creation (V1 contract), and Subscription Plan -> Razorpay Plan sync.

import json

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request
from frappe.utils import cint, flt, get_url
from payment_core.utils import erpnext_app_import_guard

from razorpay_payment.gateway.client import get_razorpay_auth, to_minor_units
from razorpay_payment.gateway.constants import RAZORPAY_API_BASE

# ERPNext billing interval → Razorpay plan period.
PERIOD_MAP = {"Day": "daily", "Week": "weekly", "Month": "monthly", "Year": "yearly"}


def find_erpnext_subscription(party, plan_names):
	"""Best-effort match of an active ERPNext Subscription by party + plan set.

	The Razorpay subscription is created from a Payment Request, while ERPNext's
	Subscription doctype generates the recurring Sales Invoices; they are not
	linked natively. This finds the Subscription whose plans cover the paid
	plans so we can stamp the Razorpay id onto it.
	"""
	if not party or not plan_names or not frappe.db.exists("DocType", "Subscription"):
		return None

	candidates = frappe.get_all(
		"Subscription",
		filters={"party": party, "status": ("in", ["Active", "Trialing"])},
		pluck="name",
		order_by="creation desc",
	)
	if not candidates:
		return None

	# Fetch ALL candidates' plans in one query (avoids N+1).
	all_plans = frappe.get_all(
		"Subscription Plan Detail",
		filters={"parent": ("in", candidates), "parenttype": "Subscription"},
		fields=["parent", "plan"],
	)
	plans_by_sub = {}
	for row in all_plans:
		plans_by_sub.setdefault(row["parent"], set()).add(row["plan"])

	paid = set(plan_names)
	for name in candidates:
		if paid.issubset(plans_by_sub.get(name, set())):
			return name
	return None


def link_razorpay_subscription(erpnext_subscription, razorpay_subscription_id):
	"""Stamp the Razorpay subscription id onto an ERPNext Subscription."""
	if not erpnext_subscription:
		return
	if frappe.db.has_column("Subscription", "razorpay_subscription_id"):
		frappe.db.set_value(
			"Subscription",
			erpnext_subscription,
			"razorpay_subscription_id",
			razorpay_subscription_id,
			update_modified=False,
		)


# ── Programmatic create (V1 contract via gateway_subscription_handler hook) ──


def create_razorpay_subscription(gateway_controller, data):
	"""Create a Razorpay subscription and route the auth payment through our
	Standard Checkout (passing ``subscription_id`` in the checkout options).

	Per Razorpay's test/live docs, subscription auth payments must go through
	Standard Checkout with ``subscription_id`` — NOT the standalone short_url.
	Returns the ERPNext checkout URL (consistent with the Order flow).
	"""
	settings = frappe.get_doc("Razorpay Settings", gateway_controller)
	settings.data = frappe._dict(data)

	integration_request = create_request_log(data, service_name="Razorpay")
	settings.integration_request = integration_request

	try:
		subscription_id = _create_subscription_on_razorpay(settings)
		# Stamp subscription_id onto the IR for the checkout page.
		ir_data = json.loads(integration_request.data)
		ir_data["subscription_id"] = subscription_id
		integration_request.db_set("data", json.dumps(ir_data), update_modified=False)
		return get_url(f"./razorpay_checkout?token={integration_request.name}")
	except Exception:
		integration_request.db_set("status", "Failed", update_modified=False)
		frappe.log_error(frappe.get_traceback(), "Unable to create Razorpay subscription")
		frappe.throw(_("Could not create the Razorpay subscription. Please contact Administrator."))


def _create_subscription_on_razorpay(settings):
	data = settings.data

	pr = frappe.db.get_value(
		"Payment Request", data.reference_docname, ["name", "party", "party_type"], as_dict=True
	)
	if pr.party_type != "Customer":
		frappe.throw(_("Razorpay subscriptions are only supported for Customers."))
	details = frappe.get_all(
		"Subscription Plan Detail",
		filters={"parent": pr.name, "parenttype": "Payment Request"},
		fields=["plan", "qty"],
		order_by="idx",
	)
	plan_names = [row.plan for row in details]

	plan_id = _resolve_plan_id(details, settings)

	erpnext_sub = find_erpnext_subscription(pr.party, plan_names)

	# Notes carry the ERPNext reference — the self-heal source for webhooks.
	notes = {"reference_doctype": "Payment Request", "reference_docname": pr.name}
	if erpnext_sub:
		notes["erpnext_subscription"] = erpnext_sub
	if pr.party:
		notes["erpnext_customer"] = pr.party

	subscription_details = {
		"plan_id": plan_id,
		"total_count": cint(data.get("total_count")) or 12,
		"customer_notify": 1,
		"notes": {k: str(v) for k, v in notes.items()},
	}

	api_key, api_secret = get_razorpay_auth(settings)
	resp = make_post_request(
		f"{RAZORPAY_API_BASE}/subscriptions",
		auth=(api_key, api_secret),
		json=subscription_details,
		headers={"content-type": "application/json"},
	)

	if resp.get("status") != "created" or not resp.get("id"):
		settings.integration_request.db_set("status", "Failed", update_modified=False)
		frappe.log_error(message=str(resp), title="Razorpay subscription create failed")
		frappe.throw(_("Razorpay subscription creation failed. Check the Error Log."))

	settings.integration_request.db_set("output", resp.get("short_url"), update_modified=False)
	settings.integration_request.db_set("status", "Completed", update_modified=False)
	settings.flags.status_changed_to = "Completed"

	link_razorpay_subscription(erpnext_sub, resp.get("id"))
	return resp.get("id")


def _resolve_plan_id(details, settings):
	"""Resolve the Razorpay plan_id for a subscription create.

	Single-plan: reuse the plan's synced ``product_price_id`` directly.
	Multi-plan: Razorpay doesn't support multiple plans per subscription, so
	sum all plan costs into one combined plan and return its id.
	"""
	if not details:
		frappe.throw(_("No subscription plans found on this Payment Request."))

	if len(details) == 1:
		plan_id = frappe.db.get_value("Subscription Plan", details[0].plan, "product_price_id")
		if not plan_id:
			frappe.throw(
				_("Subscription Plan {0} has no synced Razorpay plan; sync it before checkout.").format(
					details[0].plan
				)
			)
		return plan_id

	# Multi-plan: create a combined plan whose amount = sum of all plan costs.
	plans = frappe.get_all(
		"Subscription Plan",
		filters={"name": ("in", [row.plan for row in details])},
		fields=["name", "cost", "currency", "billing_interval", "billing_interval_count"],
	)
	if not plans:
		frappe.throw(_("Could not load subscription plan details."))

	combined_cost = sum(
		flt(p.cost) * cint(next((d.qty or 1 for d in details if d.plan == p.name), 1)) for p in plans
	)
	if combined_cost <= 0:
		frappe.throw(_("Combined plan cost is zero; check the subscription plan costs."))

	first = plans[0]
	period = PERIOD_MAP.get(first.billing_interval)
	if not period:
		frappe.throw(_("Billing interval '{0}' is not supported.").format(first.billing_interval))

	api_key, api_secret = get_razorpay_auth(settings)
	payload = {
		"period": period,
		"interval": cint(first.billing_interval_count) or 1,
		"item": {
			"name": f"Combined Plan ({len(plans)} plans)",
			"amount": to_minor_units(combined_cost, first.currency),
			"currency": first.currency or "INR",
		},
		"notes": {"erpnext_plans": ", ".join(p.name for p in plans)},
	}
	resp = make_post_request(
		f"{RAZORPAY_API_BASE}/plans",
		auth=(api_key, api_secret),
		json=payload,
		headers={"content-type": "application/json"},
	)
	if not resp.get("id"):
		frappe.log_error(message=str(resp), title="Combined plan creation failed")
		frappe.throw(_("Could not create a combined plan for multi-plan subscription."))
	return resp["id"]


# ── Plan sync (doc_event on Subscription Plan.on_update) ──


def sync_razorpay_plan(doc, method=None):
	"""Create or update the Razorpay plan when a Razorpay-gated Subscription Plan is saved.

	Stores the plan id in the native ``product_price_id`` field (Stripe stores
	its price id there too) so it exists before checkout — a Razorpay plan is a
	prerequisite for subscriptions. Runs on on_update so a Razorpay outage never
	blocks a plan save; failures are logged and surfaced as a non-blocking message.
	"""
	if doc.price_determination not in ("Fixed Rate", "Monthly Rate", "Based On Price List"):
		return

	pg = frappe.db.get_value("Payment Gateway Account", doc.payment_gateway, "payment_gateway")
	gw = (
		frappe.db.get_value("Payment Gateway", pg, ["gateway_settings", "gateway_controller"], as_dict=True)
		if pg
		else None
	)
	settings = (
		frappe.get_doc("Razorpay Settings", gw.gateway_controller)
		if (gw and gw.gateway_settings == "Razorpay Settings")
		else None
	)
	if not settings:
		return  # plan is not on a Razorpay gateway

	if doc.price_determination == "Based On Price List":
		with erpnext_app_import_guard():
			from erpnext.accounts.doctype.subscription_plan.subscription_plan import get_plan_rate
		unit_cost = get_plan_rate(doc.name, quantity=1)
	else:
		unit_cost = doc.cost
	if not unit_cost:
		return

	period = PERIOD_MAP.get(doc.billing_interval)
	if not period:
		return

	interval = cint(doc.billing_interval_count) or 1
	if period == "daily" and interval < 7:
		frappe.msgprint(
			_(
				"Razorpay requires a minimum interval of 7 for daily plans. "
				"Set Billing Interval Count to 7 or higher."
			),
			indicator="red",
			alert=True,
		)
		return

	api_key, api_secret = get_razorpay_auth(settings)
	plan_payload = {
		"period": period,
		"interval": interval,
		"item": {
			"name": doc.plan_name,
			"amount": to_minor_units(unit_cost, doc.currency),
			"currency": (doc.currency or "INR"),
		},
		"notes": {"erpnext_plan": doc.name},
	}

	try:
		if doc.product_price_id:
			try:
				existing = make_get_request(
					f"{RAZORPAY_API_BASE}/plans/{doc.product_price_id}",
					auth=(api_key, api_secret),
				)
				item = existing.get("item", {})
				if (
					existing.get("period") == plan_payload["period"]
					and existing.get("interval") == plan_payload["interval"]
					and item.get("amount") == plan_payload["item"]["amount"]
					and (item.get("currency") or "").upper() == plan_payload["item"]["currency"].upper()
				):
					return  # in sync
			except Exception:
				frappe.log_error(
					title=f"Razorpay plan {doc.product_price_id} stale",
					message=f"Plan {doc.product_price_id} for {doc.name} not found at Razorpay — recreating.",
				)

		resp = make_post_request(
			f"{RAZORPAY_API_BASE}/plans",
			auth=(api_key, api_secret),
			json=plan_payload,
			headers={"content-type": "application/json"},
		)
		if resp.get("id"):
			doc.db_set("product_price_id", resp["id"])
	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), "Razorpay plan sync failed")
		detail = ""
		if getattr(exc, "response", None) and exc.response.text:
			try:
				detail = ": " + (json.loads(exc.response.text).get("error", {}).get("description", "") or "")
			except Exception:
				frappe.log_error(frappe.get_traceback(), "Razorpay error response could not be parsed")
		frappe.msgprint(
			_("Could not sync this plan to Razorpay{0}").format(detail),
			indicator="orange",
			alert=True,
		)
