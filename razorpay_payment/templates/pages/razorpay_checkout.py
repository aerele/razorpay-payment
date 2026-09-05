# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.utils import cint, flt
from payment_core.utils import guard_payment_reference, validate_integration_request

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

	try:
		validate_integration_request(frappe.form_dict["token"])

		# Only the data field is consumed — read it directly (no full-doc load).
		data_json = frappe.db.get_value("Integration Request", frappe.form_dict["token"], "data")
		payment_details = json.loads(data_json)

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
	# Reject payment endpoints pointed at an arbitrary or non-existent reference.
	# Authenticated callers must also own it; the guest checkout flow is allowed
	# because the Integration Request token (minted by get_payment_url) is the auth.
	guard_payment_reference(reference_doctype, reference_docname)
	if frappe.session.user != "Guest":
		frappe.has_permission(reference_doctype, "read", reference_docname, throw=True)
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
