# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Razorpay webhook endpoint (path: .../razorpay_settings.webhooks).
# Verification/dispatch live in razorpay_payment.gateway.webhooks.

import frappe

from razorpay_payment.gateway.webhooks import construct_event, handle_event


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def webhooks():
	"""Razorpay webhook receiver.

	Authorization is the X-Razorpay-Signature header (HMAC-SHA256 over the raw
	body, keyed with the webhook_secret). No Frappe role check applies — the
	caller is Razorpay's servers, not a user. Reject with 400 before any DB write.
	"""
	r = frappe.request
	if not r:
		return
	raw_body = r.get_data()
	signature = frappe.get_request_header("X-Razorpay-Signature")
	event_id = frappe.get_request_header("X-Razorpay-Event-ID")

	event = construct_event(raw_body, signature)
	if event is None:
		frappe.local.response["http_status_code"] = 400
		return {"status": "invalid signature"}

	settings = frappe.get_doc("Razorpay Settings")
	original_user = frappe.session.user
	try:
		frappe.set_user("Administrator")  # nosemgrep
		return handle_event(raw_body, event_id, settings)
	finally:
		frappe.set_user(original_user)  # nosemgrep
