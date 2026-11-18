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
	# Razorpay's delivery id: the body carries no event id, so the header is
	# the dedupe key
	event_id = frappe.get_request_header("X-Razorpay-Event-ID")

	event = construct_event(raw_body, signature)
	if event is None:
		frappe.local.response["http_status_code"] = 400
		return {"status": "invalid signature"}

	settings = frappe.get_doc("Razorpay Settings")
	original_user = frappe.session.user
	try:
		frappe.set_user("Administrator")  # nosemgrep
		return handle_event(event, settings, event_id=event_id)
	finally:
		frappe.set_user(original_user)  # nosemgrep
