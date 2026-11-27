# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, flt
from payment_core.utils import guard_payment_reference, validate_integration_request

from razorpay_payment.gateway.collect_now import POLICY, is_payable, record_client_failure
from razorpay_payment.gateway.references import assert_reference_payable

no_cache = 1

expected_keys = (
	"amount",
	"title",
	"description",
	"reference_doctype",
	"reference_docname",
	"payer_name",
	"payer_email",
	"currency",
)


def get_context(context):
	context.no_cache = 1
	context.api_key = get_api_key()
	payable = True

	try:
		validate_integration_request(frappe.form_dict["token"])

		# Only the data field is consumed — read it directly (no full-doc load).
		data_json = frappe.db.get_value("Integration Request", frappe.form_dict["token"], "data")
		payment_details = json.loads(data_json)
		context.collect_now = cint(payment_details.get(POLICY))
		if context.collect_now:
			# Collect Now skips the payable check at payment time, so a stale link must not open checkout.
			payable = is_payable(payment_details)
			context.api_key = frappe.get_doc("Razorpay Settings").get_settings(payment_details).api_key

		for key in expected_keys:
			context[key] = payment_details[key]

		# order_id is optional — absent for subscription auth payments (which use
		# subscription_id as the checkout anchor instead).
		context["order_id"] = payment_details.get("order_id") or ""

		context["token"] = frappe.form_dict["token"]
		guard_payment_reference(context["reference_doctype"], context["reference_docname"])
		context["amount"] = flt(context["amount"])
		context["subscription_id"] = (
			payment_details["subscription_id"] if payment_details.get("subscription_id") else ""
		)

	except Exception:
		frappe.redirect_to_message(
			_("Invalid Token"),
			_("Seems token you are using is invalid!"),
			http_status_code=400,
			indicator_color="red",
		)

		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect

	if not payable:
		frappe.redirect_to_message(
			_("Payment Not Required"),
			_("This payment request is already paid or is no longer open."),
			http_status_code=400,
			indicator_color="orange",
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


def get_api_key():
	api_key = frappe.db.get_single_value("Razorpay Settings", "api_key")
	if cint(frappe.form_dict.get("use_sandbox")):
		api_key = frappe.conf.sandbox_api_key

	return api_key


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def make_payment(
	razorpay_payment_id: str,
	options: str,
	reference_doctype: str,
	reference_docname: str,
	token: str,
	razorpay_order_id: str | None = None,
	razorpay_signature: str | None = None,
):
	# The checkout binding (what is paid, against which reference) is the
	# Integration Request's stored data — minted by get_payment_url when the
	# token was issued. Request-supplied reference names are never trusted:
	# they name what the SERVER must settle, so only the stored values are.
	stored = json.loads(frappe.db.get_value("Integration Request", token, "data") or "{}")
	reference_doctype = stored.get("reference_doctype")
	reference_docname = stored.get("reference_docname")
	if not (reference_doctype and reference_docname):
		frappe.throw(_("Invalid payment token."), frappe.ValidationError)

	# Reject payment endpoints pointed at an arbitrary or non-existent reference.
	# Authenticated callers must also own it; the guest checkout flow is allowed
	# because the Integration Request token (minted by get_payment_url) is the auth.
	guard_payment_reference(reference_doctype, reference_docname)
	if frappe.session.user != "Guest":
		frappe.has_permission(reference_doctype, "read", reference_docname, throw=True)
	if not stored.get(POLICY):
		assert_reference_payable(reference_doctype, reference_docname)

	data = {}

	if isinstance(options, str):
		data = json.loads(options)

	data.update(
		{
			"razorpay_payment_id": razorpay_payment_id,
			"razorpay_order_id": razorpay_order_id,
			"razorpay_signature": razorpay_signature,
			"reference_docname": reference_docname,
			"reference_doctype": reference_doctype,
			"token": token,
		}
	)

	# Frappe commits at request end, before the response reaches the browser.
	return frappe.get_doc("Razorpay Settings").create_request(data)


@frappe.whitelist(  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
	allow_guest=True, methods=["POST"]
)
@rate_limit(limit=30, seconds=60 * 60)
def record_payment_failure(token: str, error: str):
	"""Store the failure Razorpay Checkout reported to the payer's browser.

	The Integration Request token authorizes the guest call, as in make_payment. The report is
	evidence only: it cannot change the checkout status, which the Status API check decides.
	"""
	if frappe.db.get_value("Integration Request", token, "integration_request_service") != "Razorpay":
		return
	try:
		payload = json.loads(error)
	except ValueError:
		return
	record_client_failure(token, payload)
