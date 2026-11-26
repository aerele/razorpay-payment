"""Independent Razorpay payment verification for opted-in ERPNext checkouts."""

import frappe
from frappe import _
from frappe.integrations.utils import make_get_request, make_post_request
from frappe.utils import add_to_date, get_datetime, now_datetime
from payment_core.utils import success_redirect

from razorpay_payment.gateway.client import verify_payment_signature
from razorpay_payment.gateway.constants import RAZORPAY_API_BASE

POLICY = "require_collect_now_status_verification"
STATUS_API_TIMEOUT = 10
# Razorpay keeps asking the bank about an unresolved payment for five days (late authorization).
RETRY_WINDOW_DAYS = 5
# Checks continue for one more day so a stuck payment is recorded and reported exactly once.
EXPIRY_GRACE_DAYS = 1
RETRY_STATUSES = ("Queued", "Authorized", "Failed")
# Poll hourly on the first day, then every few hours: most unresolved checkouts are simply abandoned.
HOURLY_RETRY_DAYS = 1
SLOW_RETRY_HOURS = 6
MAX_EVIDENCE = 100
# Unsigned browser reports get a smaller share so they cannot push Status API evidence out.
MAX_BROWSER_EVIDENCE = 20
# Only these provider fields are kept as evidence; payer contact and instrument details are dropped.
PAYMENT_FIELDS = (
	"id",
	"entity",
	"order_id",
	"amount",
	"currency",
	"status",
	"method",
	"captured",
	"amount_refunded",
	"refund_status",
	"error_code",
	"error_description",
	"error_source",
	"error_step",
	"error_reason",
	"created_at",
)
CLIENT_ERROR_FIELDS = ("code", "description", "source", "step", "reason")
CLIENT_ERROR_MAX_LENGTH = 140
DEDUPE_KEYS = ("source", "outcome", "payment_id", "provider_status", "error")


def find_checkout(order_id):
	return frappe.db.get_value(
		"Integration Request",
		{"integration_request_service": "Razorpay", "request_id": order_id},
		["name", "reference_doctype", "reference_docname"],
		as_dict=True,
	)


def handle_callback(settings, checkout, data, params):
	payment_id = params.get("razorpay_payment_id")
	signature = params.get("razorpay_signature")
	auth = settings.get_settings(data)
	if (
		not data.get("order_id")
		or not _is_payment_id(payment_id)
		or params.get("razorpay_order_id") != data["order_id"]
		or not isinstance(signature, str)
		or not verify_payment_signature(data["order_id"], payment_id, signature, auth.api_secret)
	):
		return {"redirect_to": "/payment-failed", "status": 401}

	result = verify_and_settle(settings, checkout, payment_id=payment_id)
	if result == "completed":
		return {
			"redirect_to": success_redirect(
				reference_doctype=data["reference_doctype"], reference_docname=data["reference_docname"]
			),
			"status": 200,
		}
	if result in ("invalid", "failed"):
		return {"redirect_to": "/payment-failed", "status": 400}
	return {
		"redirect_to": frappe.redirect_to_message(
			_("Payment confirmation pending"),
			_(
				"We are checking your payment with Razorpay. Please wait for confirmation before paying again."
			),
		),
		"status": 200,
	}


def verify_and_settle(settings, checkout, payment_id=None, *, allow_capture=False):
	"""Serialize verification and accounting for this order across all entry points.

	With a payment ID (browser callback, webhook) that payment is fetched. Without one
	(scheduled retry) every attempt on the order is fetched, so a payment is recovered
	even when neither the browser callback nor the webhook reached the site.
	"""
	row = _lock(checkout)
	if not row:
		return "invalid"

	data = frappe.parse_json(row.data)
	if not data.get(POLICY) or data.get("reference_doctype") != "Payment Request":
		return "invalid"
	if row.status == "Completed":
		return "completed" if not payment_id or payment_id == data.get("razorpay_payment_id") else "invalid"
	if row.status == "Cancelled" or not data.get("order_id"):
		return "invalid"
	if payment_id is not None and not _is_payment_id(payment_id):
		return "invalid"

	auth = settings.get_settings(data)
	credentials = (auth.api_key, auth.api_secret)
	calls = []
	try:
		if payment_id:
			payment = _get(f"/payments/{payment_id}", credentials, calls)
		else:
			payment = _order_payment(data, credentials, calls)
			if payment is None:
				_record(checkout, row, data, calls, "no_attempt", row.status)
				return "pending"
			payment_id = payment["id"]

		if (
			allow_capture
			and _matches_checkout(data, payment_id, payment)
			and payment["status"] == "authorized"
		):
			if not is_payable(data):
				return _close(checkout, row, data, calls, payment_id, "not_payable", "authorized")
			_post(f"/payments/{payment_id}/capture", credentials, calls, {"amount": data["amount"]})
			payment = _get(f"/payments/{payment_id}", credentials, calls)
	except Exception:
		_record(checkout, row, data, calls, "provider_unavailable", row.status or "Queued", payment_id)
		return "pending"

	if not _matches_checkout(data, payment_id, payment):
		_record(
			checkout,
			row,
			data,
			calls,
			"mismatch",
			row.status if data.get("razorpay_payment_id") else "Failed",
			payment_id,
		)
		return "invalid"

	provider_status = payment["status"]
	if provider_status != "captured":
		status = {"authorized": "Authorized", "failed": "Failed", "refunded": "Failed"}.get(
			provider_status, "Queued"
		)
		_record(checkout, row, data, calls, "verified", status, payment_id, provider_status)
		return "failed" if status == "Failed" else "pending"

	if not is_payable(data):
		return _close(checkout, row, data, calls, payment_id, "refund_required", provider_status)

	frappe.db.savepoint("collect_now_accounting")
	previous_payment = settings.flags.get("collect_now_verified_payment")
	previous_payment_id = settings.flags.get("razorpay_payment_id")
	try:
		settings.flags.collect_now_verified_payment = payment
		pr = frappe.get_doc("Payment Request", data["reference_docname"])
		settled = settings.settle_payment_request(pr, payment_id=payment_id)
	except Exception:
		frappe.db.rollback(save_point="collect_now_accounting")
		_record(checkout, row, data, calls, "accounting_pending", "Queued", payment_id, provider_status)
		frappe.log_error(title="Collect Now Payment Entry posting failed", message=f"Checkout: {checkout}")
		return "pending"
	finally:
		settings.flags.collect_now_verified_payment = previous_payment
		settings.flags.razorpay_payment_id = previous_payment_id

	if not settled:
		# The builder declines deterministically (e.g. the invoice has no outstanding), so retrying cannot help.
		frappe.db.rollback(save_point="collect_now_accounting")
		return _close(checkout, row, data, calls, payment_id, "not_posted", provider_status)

	_record(checkout, row, data, calls, "verified", "Completed", payment_id, provider_status)
	return "completed"


def record_client_failure(checkout, error):
	"""Store a failure reported by the payer's browser.

	The report is unsigned, so it never changes the checkout status or bound payment.
	The scheduled Status API check confirms the provider state of the order.
	"""
	row = _lock(checkout)
	if not row:
		return
	data = frappe.parse_json(row.data)
	if not data.get(POLICY):
		return

	error = error if isinstance(error, dict) else {}
	if isinstance(error.get("error"), dict):
		error = error["error"]
	metadata = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
	payment_id = metadata.get("payment_id")
	if not _is_payment_id(payment_id) or metadata.get("order_id") != data.get("order_id"):
		payment_id = None
	reported = {
		key: str(error[key])[:CLIENT_ERROR_MAX_LENGTH]
		for key in CLIENT_ERROR_FIELDS
		if error.get(key) not in (None, "")
	}
	_record(
		checkout,
		row,
		data,
		[],
		"client_reported_failure",
		row.status,
		payment_id,
		source="browser",
		error=reported,
	)


def retry_pending():
	"""Queue each unresolved order separately so a slow provider call holds only its own lock."""
	request = frappe.qb.DocType("Integration Request")
	since = _retry_since(now_datetime())
	checkouts = (
		frappe.qb.from_(request)
		.select(request.name)
		.where(
			(request.integration_request_service == "Razorpay")
			& request.request_id.isnotnull()
			& (request.request_id != "")
			& (request.status.isin(RETRY_STATUSES))
			& (request.creation >= since)
		)
		.run(pluck=True)
	)
	for checkout in checkouts:
		frappe.enqueue(retry_checkout, checkout=checkout, job_id=f"collect-now:{checkout}", deduplicate=True)


def _retry_since(now):
	"""Oldest creation time polled this hour: the full window every few hours, else only the first day."""
	if now.hour % SLOW_RETRY_HOURS:
		return add_to_date(now, days=-HOURLY_RETRY_DAYS)
	return add_to_date(now, days=-(RETRY_WINDOW_DAYS + EXPIRY_GRACE_DAYS))


def retry_checkout(checkout):
	settings = frappe.get_cached_doc("Razorpay Settings")
	if verify_and_settle(settings, checkout, allow_capture=True) != "pending":
		return
	creation = frappe.db.get_value("Integration Request", checkout, "creation")
	if creation and get_datetime(creation) < add_to_date(now_datetime(), days=-RETRY_WINDOW_DAYS):
		_expire(checkout)


def _expire(checkout):
	row = _lock(checkout)
	if not row:
		return
	if any(entry.get("outcome") == "expired" for entry in _evidence(row.output)):
		return
	data = frappe.parse_json(row.data)
	payment_id = data.get("razorpay_payment_id")
	if payment_id:
		frappe.log_error(
			title="Collect Now payment unresolved",
			message=_review_message(checkout, data, payment_id, "expired"),
		)
	_record(checkout, row, data, [], "expired", row.status, payment_id, source="scheduler")


def _close(checkout, row, data, calls, payment_id, outcome, provider_status):
	"""Stop processing an order that must not post, and leave it for staff review."""
	_record(checkout, row, data, calls, outcome, "Cancelled", payment_id, provider_status)
	# The Integration Request list can be filtered on this field to find orders needing a refund.
	frappe.db.set_value(
		"Integration Request",
		checkout,
		"error",
		_("Collect Now {0}: payment {1} was not posted. Review and refund if needed.").format(
			outcome, payment_id
		),
		update_modified=False,
	)
	frappe.log_error(
		title="Collect Now payment needs review",
		message=_review_message(checkout, data, payment_id, outcome),
	)
	return "failed"


def _review_message(checkout, data, payment_id, outcome):
	return (
		f"Checkout: {checkout}\nPayment Request: {data.get('reference_docname')}\n"
		f"Order: {data.get('order_id')}\nPayment: {payment_id}\nOutcome: {outcome}"
	)


def is_payable(data):
	pr = frappe.db.get_value(
		"Payment Request", data.get("reference_docname"), ["docstatus", "status"], as_dict=True
	)
	return bool(pr) and pr.docstatus == 1 and pr.status not in ("Paid", "Cancelled")


def _order_payment(data, credentials, calls):
	"""Pick the attempt that decides the order: captured, then authorized, then the latest."""
	response = _get(f"/orders/{data['order_id']}/payments", credentials, calls)
	items = response.get("items") if isinstance(response, dict) else None
	attempts = [
		payment
		for payment in items or []
		if isinstance(payment, dict) and _matches_checkout(data, payment.get("id"), payment)
	]
	if not attempts:
		return None
	rank = {"captured": 0, "authorized": 1}
	return min(
		attempts, key=lambda payment: (rank.get(payment["status"], 2), -(payment.get("created_at") or 0))
	)


def _get(path, credentials, calls):
	call = {"method": "GET", "path": path}
	calls.append(call)
	try:
		response = make_get_request(
			f"{RAZORPAY_API_BASE}{path}", auth=credentials, timeout=STATUS_API_TIMEOUT
		)
	except Exception as exc:
		call["error"] = _error_name(exc)
		raise
	call["response"] = _redact(response)
	return response


def _post(path, credentials, calls, body):
	call = {"method": "POST", "path": path, "request": body}
	calls.append(call)
	try:
		response = make_post_request(
			f"{RAZORPAY_API_BASE}{path}", auth=credentials, json=body, timeout=STATUS_API_TIMEOUT
		)
	except Exception as exc:
		call["error"] = _error_name(exc)
		raise
	call["response"] = _redact(response)
	return response


def _error_name(exc):
	status_code = getattr(getattr(exc, "response", None), "status_code", None)
	return f"{type(exc).__name__} (HTTP {status_code})" if status_code else type(exc).__name__


def _redact(response):
	if not isinstance(response, dict):
		return None
	if response.get("entity") == "collection":
		items = response.get("items") or []
		return {
			"count": response.get("count"),
			"items": [_redact(item) for item in items if isinstance(item, dict)],
		}
	return {field: response[field] for field in PAYMENT_FIELDS if field in response}


def _is_payment_id(value):
	return isinstance(value, str) and value.startswith("pay_") and len(value) <= 40


def _matches_checkout(data, payment_id, payment):
	return (
		isinstance(payment, dict)
		and _is_payment_id(payment_id)
		and payment.get("id") == payment_id
		and bool(data.get("order_id"))
		and payment.get("order_id") == data["order_id"]
		and isinstance(payment.get("amount"), int)
		and not isinstance(payment["amount"], bool)
		and payment["amount"] == data.get("amount")
		and bool(data.get("currency"))
		and payment.get("currency") == data["currency"]
		and payment.get("status") in ("created", "authorized", "captured", "failed", "refunded")
	)


def _lock(checkout):
	request = frappe.qb.DocType("Integration Request")
	rows = (
		frappe.qb.from_(request)
		.select(request.data, request.status, request.output)
		.where((request.name == checkout) & (request.integration_request_service == "Razorpay"))
		.for_update()
		.run(as_dict=True)
	)
	return rows[0] if rows else None


def _evidence(output):
	if not output:
		return []
	try:
		parsed = frappe.parse_json(output)
	except ValueError:
		return []
	if isinstance(parsed, dict):
		return [parsed]
	return parsed if isinstance(parsed, list) else []


def _trim(entries):
	"""Keep the latest entries of each kind, in their original order."""
	browser = [entry for entry in entries if entry.get("source") == "browser"][-MAX_BROWSER_EVIDENCE:]
	checks = [entry for entry in entries if entry.get("source") != "browser"][-MAX_EVIDENCE:]
	kept = {id(entry) for entry in browser + checks}
	return [entry for entry in entries if id(entry) in kept]


def _record(
	checkout,
	row,
	data,
	calls,
	outcome,
	status,
	payment_id=None,
	provider_status=None,
	*,
	source="status_api",
	error=None,
):
	"""Append redacted evidence of one check; repeats of the same result only bump a counter."""
	if payment_id and source == "status_api" and outcome != "mismatch":
		data["razorpay_payment_id"] = payment_id
	checked_at = now_datetime().isoformat()
	entry = {
		"source": source,
		"outcome": outcome,
		"order_id": data.get("order_id"),
		"payment_id": payment_id,
		"provider_status": provider_status,
		"checked_at": checked_at,
		"count": 1,
		"calls": calls,
	}
	if error:
		entry["error"] = error

	entries = _evidence(row.output)
	last = entries[-1] if entries else None
	if last and all(last.get(key) == entry.get(key) for key in DEDUPE_KEYS):
		last.update(calls=calls, last_checked_at=checked_at, count=(last.get("count") or 1) + 1)
	else:
		entries.append(entry)
	output = frappe.as_json(_trim(entries))
	row.update(output=output, status=status)
	frappe.db.set_value(
		"Integration Request",
		checkout,
		{"data": frappe.as_json(data), "status": status, "output": output},
		update_modified=False,
	)
