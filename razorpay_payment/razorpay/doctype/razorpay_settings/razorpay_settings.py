# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""
# Integrating RazorPay

### Validate Currency

Example:

	from payment_core.utils import get_payment_gateway_controller

	controller = get_payment_gateway_controller("Razorpay")
	controller().validate_transaction_currency(currency)

### 2. Redirect for payment

Example:

	payment_details = {
		"amount": 600,
		"title": "Payment for bill : 111",
		"description": "payment via cart",
		"reference_doctype": "Payment Request",
		"reference_docname": "PR0001",
		"payer_email": "NuranVerkleij@example.com",
		"payer_name": "Nuran Verkleij",
		"order_id": "111",
		"currency": "INR",
		"payment_gateway": "Razorpay",
		"subscription_details": {
			"plan_id": "plan_12313", # if Required
			"start_date": "2018-08-30",
			"billing_period": "Month" #(Day, Week, Month, Year),
			"billing_frequency": 1,
			"customer_notify": 1,
			"upfront_amount": 1000
		}
	}

	# Redirect the user to this url
	url = controller().get_payment_url(**payment_details)


### 3. On Completion of Payment

Write a method for `on_payment_authorized` in the reference doctype

Example:

	def on_payment_authorized(payment_status):
		# this method will be called when payment is complete


##### Notes:

payment_status - payment gateway will put payment status on callback.
For razorpay payment status is Authorized

"""

import hashlib
import json
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import (
	create_request_log,
	make_get_request,
	make_post_request,
)
from frappe.model.document import Document
from frappe.utils import call_hook_method, cint, flt, get_datetime, get_timestamp, get_url, now_datetime
from payment_core.api.gateway import GatewayControllerMixin
from payment_core.payment_ledger.status_flow import PaymentTransactionStatus
from payment_core.utils import (
	create_payment_gateway,
	elevated_user,
	get_amount_precision,
	get_reference_amount,
	guard_payment_reference,
	initiate_transaction,
)
from payment_core.webhooks import mark_failed, mark_processed
from razorpay.errors import BadRequestError

from razorpay_payment.gateway import collect_now, ledger, reconciliation, settlement
from razorpay_payment.gateway.client import (
	from_minor_units,
	get_razorpay_auth,
	get_razorpay_client,
	to_minor_units,
	verify_payment_signature,
	verify_subscription_signature,
	verify_webhook_signature,
)
from razorpay_payment.gateway.constants import RAZORPAY_API_BASE
from razorpay_payment.gateway.references import (
	assert_reference_payable,
	get_razorpay_notes,
	is_subscription_reference,
	merge_checkout_params,
	success_redirect,
)
from razorpay_payment.gateway.subscriptions import create_razorpay_subscription
from razorpay_payment.gateway.webhooks import clear_webhook_secret_cache, get_webhook_secret

REFUND_RECEIPT_MAX_LENGTH = 40
REFUND_PAGE_SIZE = 100
SETTLEMENT_CLAIM_TTL_SECONDS = 10 * 60


def _refund_receipt(idempotency_key):
	"""Fit payment_core's stable key into Razorpay's receipt field."""
	if not idempotency_key:
		return None
	key = str(idempotency_key)
	if len(key) <= REFUND_RECEIPT_MAX_LENGTH:
		return key
	digest = hashlib.sha256(key.encode()).hexdigest()[:16]
	return f"{key[: REFUND_RECEIPT_MAX_LENGTH - len(digest) - 1]}-{digest}"


def _refund_header_key(idempotency_key):
	"""Return a Razorpay-safe refund idempotency header value."""
	digest = hashlib.sha256(str(idempotency_key).encode()).hexdigest()[:32]
	return f"pc-refund-{digest}"


def _find_refund_by_receipt(client, payment_id, receipt):
	"""Resolve the existing refund after Razorpay rejects a duplicate receipt."""
	skip = 0
	while True:
		params = {"count": REFUND_PAGE_SIZE}
		if skip:
			params["skip"] = skip
		response = client.payment.fetch_multiple_refund(payment_id, params) or {}
		items = response.get("items", [])
		refund = next((item for item in items if item.get("receipt") == receipt), None)
		if refund or len(items) < REFUND_PAGE_SIZE:
			return refund
		skip += REFUND_PAGE_SIZE


def _settlement_claim_is_active(started_at):
	"""Return whether a persisted settlement claim is still within its lease."""
	if not started_at:
		return False
	try:
		age = (now_datetime() - get_datetime(started_at)).total_seconds()
	except (TypeError, ValueError):
		return False
	return age <= SETTLEMENT_CLAIM_TTL_SECONDS


class RazorpaySettings(GatewayControllerMixin, Document):
	supported_currencies = (
		"AED",
		"ALL",
		"AMD",
		"ARS",
		"AUD",
		"AWG",
		"AZN",
		"BAM",
		"BBD",
		"BDT",
		"BGN",
		"BHD",
		"BIF",
		"BMD",
		"BND",
		"BOB",
		"BRL",
		"BSD",
		"BTN",
		"BWP",
		"BZD",
		"CAD",
		"CHF",
		"CLP",
		"CNY",
		"COP",
		"CRC",
		"CUP",
		"CVE",
		"CZK",
		"DJF",
		"DKK",
		"DOP",
		"DZD",
		"EGP",
		"ETB",
		"EUR",
		"FJD",
		"GBP",
		"GHS",
		"GIP",
		"GMD",
		"GNF",
		"GTQ",
		"GYD",
		"HKD",
		"HNL",
		"HRK",
		"HTG",
		"HUF",
		"IDR",
		"ILS",
		"INR",
		"IQD",
		"ISK",
		"JMD",
		"JOD",
		"JPY",
		"KES",
		"KGS",
		"KHR",
		"KMF",
		"KRW",
		"KWD",
		"KYD",
		"KZT",
		"LAK",
		"LKR",
		"LRD",
		"LSL",
		"MAD",
		"MDL",
		"MGA",
		"MKD",
		"MMK",
		"MNT",
		"MOP",
		"MUR",
		"MVR",
		"MWK",
		"MXN",
		"MYR",
		"MZN",
		"NAD",
		"NGN",
		"NIO",
		"NOK",
		"NPR",
		"NZD",
		"OMR",
		"PEN",
		"PGK",
		"PHP",
		"PKR",
		"PLN",
		"PYG",
		"QAR",
		"RON",
		"RSD",
		"RUB",
		"RWF",
		"SAR",
		"SCR",
		"SEK",
		"SGD",
		"SLL",
		"SOS",
		"SSP",
		"SVC",
		"SZL",
		"THB",
		"TND",
		"TRY",
		"TTD",
		"TWD",
		"TZS",
		"UAH",
		"UGX",
		"USD",
		"UYU",
		"UZS",
		"VND",
		"VUV",
		"XAF",
		"XCD",
		"XOF",
		"XPF",
		"YER",
		"ZAR",
		"ZMW",
	)

	def init_client(self):
		if self.api_key:
			self.client = get_razorpay_client(self)

	def validate(self):
		# Link Payment Gateway to this controller (needed by plan sync).
		create_payment_gateway("Razorpay", settings="Razorpay Settings", controller=self.name)
		# Backfill link on pre-existing gateways created without it.
		if frappe.db.has_column("Payment Gateway", "gateway_settings"):
			existing = frappe.db.get_value(
				"Payment Gateway", "Razorpay", ["gateway_settings", "gateway_controller"], as_dict=True
			)
			if existing and (not existing.gateway_settings or not existing.gateway_controller):
				frappe.db.set_value(
					"Payment Gateway",
					"Razorpay",
					{"gateway_settings": "Razorpay Settings", "gateway_controller": self.name},
					update_modified=False,
				)
		call_hook_method("payment_gateway_enabled", gateway="Razorpay")
		self.set_webhook_endpoint()

	def set_webhook_endpoint(self):
		"""Show the admin which URL to register as a Razorpay webhook endpoint."""

		endpoint = get_url("/api/method/razorpay_payment.razorpay.doctype.razorpay_settings.webhooks")
		if self.webhook_endpoint != endpoint:
			self.db_set("webhook_endpoint", endpoint, update_modified=False)

		clear_webhook_secret_cache()

	@frappe.whitelist()
	def test_credentials(self):
		"""Verify credentials with a live Razorpay call.

		Issued on-demand from the 'Test Credentials' desk button so a slow /
		unreachable Razorpay API degrades a click, not every save.
		"""
		# Credential test exposes the live API secret behaviour — restrict to
		# users who can manage this Settings doc.
		frappe.has_permission("Razorpay Settings", "write", self, throw=True)
		self.validate_razorpay_credentails()

	def validate_razorpay_credentails(self):
		if self.api_key and self.api_secret:
			try:
				make_get_request(
					url="https://api.razorpay.com/v1/payments",
					auth=(
						self.api_key,
						self.get_password(fieldname="api_secret", raise_exception=False),
					),
				)
			except Exception:
				frappe.throw(_("Seems API Key or API Secret is wrong !!!"))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Razorpay does not support transactions in currency '{0}'"
				).format(currency)
			)

	def setup_addon(self, settings, **kwargs):
		"""
		Addon template:
		{
		        "item": {
		                "name": row.upgrade_type,
		                "amount": row.amount,
		                "currency": currency,
		                "description": "add-on description"
		        },
		        "quantity": 1 (The total amount is calculated as item.amount * quantity)
		}
		"""

		# api_secret is a Password field — decrypt it for HTTP basic auth.
		api_secret = settings.get_password(fieldname="api_secret", raise_exception=False)
		url = f"{RAZORPAY_API_BASE}/subscriptions/{kwargs.get('subscription_id')}/addons"

		try:
			for addon in kwargs.get("addons"):
				item = addon.get("item") or {}
				if "amount" in item:
					item["amount"] = to_minor_units(item["amount"], item.get("currency") or "INR")
				resp = make_post_request(
					url,
					auth=(settings.api_key, api_secret),
					data=json.dumps(addon),
					headers={"content-type": "application/json"},
				)
				if not resp.get("id"):
					frappe.log_error(message=str(resp), title="Razorpay Failed while creating subscription")
		except Exception:
			frappe.log_error()
			# failed
			pass

	def setup_subscription(self, settings, **kwargs):
		# api_secret is a Password field — decrypt it for HTTP basic auth.
		api_secret = settings.get_password(fieldname="api_secret", raise_exception=False)

		start_date = (
			get_timestamp(kwargs.get("subscription_details").get("start_date"))
			if kwargs.get("subscription_details").get("start_date")
			else None
		)

		subscription_details = {
			"plan_id": kwargs.get("subscription_details").get("plan_id"),
			"total_count": kwargs.get("subscription_details").get("billing_frequency"),
			"customer_notify": kwargs.get("subscription_details").get("customer_notify"),
		}

		if start_date:
			subscription_details["start_at"] = cint(start_date)

		if kwargs.get("addons"):
			# Convert each addon's amount to minor units locally; this avoids
			# mutating any shared state across concurrent subscription calls.
			converted = []
			for addon in kwargs.get("addons"):
				addon = dict(addon)
				item = addon.get("item") or {}
				if "amount" in item:
					item["amount"] = to_minor_units(item["amount"], item.get("currency") or "INR")
					addon["item"] = item
				converted.append(addon)
			subscription_details.update({"addons": converted})

		try:
			resp = make_post_request(
				f"{RAZORPAY_API_BASE}/subscriptions",
				auth=(settings.api_key, api_secret),
				data=json.dumps(subscription_details),
				headers={"content-type": "application/json"},
			)

			if resp.get("status") == "created":
				kwargs["subscription_id"] = resp.get("id")
				frappe.flags.status = "created"
				return kwargs
			else:
				frappe.log_error(message=str(resp), title="Razorpay Failed while creating subscription")

		except Exception:
			frappe.log_error()

	def prepare_subscription_details(self, settings, **kwargs):
		if not kwargs.get("subscription_id"):
			kwargs = self.setup_subscription(settings, **kwargs)

		if frappe.flags.status != "created":
			kwargs["subscription_id"] = None

		return kwargs

	def get_payment_url(self, **kwargs):
		reference_doctype = kwargs.get("reference_doctype")
		reference_docname = kwargs.get("reference_docname")
		if reference_doctype and reference_docname:
			guard_payment_reference(reference_doctype, reference_docname)
			assert_reference_payable(reference_doctype, reference_docname)
			# Never trust a client-supplied amount for a reference that carries an authoritative total.
			meta = frappe.get_meta(reference_doctype)
			if meta.has_field("grand_total") or meta.has_field("amount"):
				amount, currency = get_reference_amount(reference_doctype, reference_docname)
				if amount is not None:
					kwargs["amount"] = amount
				if currency:
					kwargs["currency"] = currency

		is_subscription = is_subscription_reference(kwargs)
		# Collect Now uses ERPNext Payment Entry settlement even when the shared ledger is enabled.
		kwargs.pop(collect_now.POLICY, None)
		if (
			reference_doctype == "Payment Request"
			and not is_subscription
			and cint(self.get(collect_now.POLICY))
		):
			kwargs[collect_now.POLICY] = True

		# Shared-ledger transactions supply the outstanding amount for retries and partial payments.
		ledger_available = not kwargs.get(collect_now.POLICY) and frappe.db.table_exists(
			"Payment Transaction"
		)
		if not ledger_available:
			# A caller may pass a transaction name even on an ERPNext-only site.
			# Do not persist a ledger-only binding that webhook routing cannot read.
			kwargs.pop("payment_transaction", None)
		payment_transaction_supplied = bool(kwargs.get("payment_transaction"))
		if not kwargs.get(collect_now.POLICY):
			ledger.ensure_payment_transaction(self, kwargs)
		if kwargs.get("payment_transaction") and ledger_available:
			txn = frappe.db.get_value(
				"Payment Transaction",
				kwargs["payment_transaction"],
				["outstanding_amount", "reference_doctype", "reference_docname", "currency", "status"],
				as_dict=True,
			)
			if payment_transaction_supplied and not txn:
				frappe.throw(_("Payment Transaction does not exist."))
			if payment_transaction_supplied and txn.status not in (*PaymentTransactionStatus.OPEN, "Failed"):
				frappe.throw(_("Only an open or Failed Payment Transaction can start a new checkout."))
			if txn and (txn.reference_doctype, txn.reference_docname) == (
				reference_doctype,
				reference_docname,
			):
				kwargs["amount"] = txn.outstanding_amount
				if txn.currency:
					kwargs["currency"] = txn.currency
			elif txn:
				frappe.throw(_("Payment Transaction does not belong to this reference."))

		# Subscription references branch to the Razorpay Subscription create flow
		# (POST /v1/subscriptions → short_url) instead of the one-off Order flow.
		if is_subscription:
			# create_razorpay_subscription needs a plain dict, not kwargs.
			checkout_url = create_razorpay_subscription(self.name, kwargs)
			ledger.sync_payment_transaction(
				kwargs,
				None if payment_transaction_supplied else "Initiated",
				checkout_url=checkout_url,
			)
			if not payment_transaction_supplied and kwargs.get("payment_transaction"):
				initiate_transaction(kwargs["payment_transaction"])
			return checkout_url

		# Always create a fresh server-side order: a caller-supplied order_id
		# would let the client name which order the buyer pays for. The caller's
		# value is kept as the order receipt (the merchant sees the document
		# name in the Razorpay dashboard), never as the order id itself.
		kwargs.setdefault("receipt", kwargs.pop("order_id", None))
		order = self.create_order(**kwargs)
		kwargs.update({"order_id": order.get("id")})
		# create_order already logged the Integration Request; only its name is
		# needed here to build the checkout URL — read the scalar directly.
		integration_request_name = order["integration_request"]
		checkout_url = get_url(f"./razorpay_checkout?token={integration_request_name}")

		# ledger sync: the order id is the provider handle while the payment is
		# open; the checkout URL lets the desk/list re-share the link
		ledger.sync_payment_transaction(
			kwargs,
			None if payment_transaction_supplied else "Initiated",
			provider_transaction_id=kwargs.get("order_id"),
			checkout_url=checkout_url,
			metadata={"razorpay_order_id": kwargs.get("order_id")},
		)
		# checkout-first origin: the transaction was created as Draft above;
		# submit it now that its checkout URL exists (idempotent, elevated)
		if not payment_transaction_supplied and kwargs.get("payment_transaction"):
			initiate_transaction(kwargs["payment_transaction"])
		return checkout_url

	def create_order(self, **kwargs):
		# Creating Orders https://razorpay.com/docs/api/orders/

		currency = kwargs.get("currency") or "INR"
		if kwargs.get(collect_now.POLICY):
			kwargs["currency"] = currency
		# Convert major units → minor (paise) for all non-zero-decimal currencies.
		kwargs["amount"] = to_minor_units(kwargs["amount"], currency)

		# Create integration log
		integration_request = create_request_log(kwargs, service_name="Razorpay")

		# Setup payment options. Razorpay rejects null payment_capture.
		payment_options = {
			"amount": kwargs.get("amount"),
			"currency": currency,
			"receipt": kwargs.get("receipt"),
			"payment_capture": kwargs.get("payment_capture") or 1,
			"notes": get_razorpay_notes(kwargs, integration_request=integration_request.name),
		}
		# Razorpay rejects null payment_capture; be safe with other None values
		payment_options = {k: v for k, v in payment_options.items() if v is not None}
		if self.api_key and self.api_secret:
			try:
				# Order retries dedupe via ``receipt``; there is no HTTP idempotency header for Orders.
				api_key, api_secret = get_razorpay_auth(self, kwargs)
				order = make_post_request(
					f"{RAZORPAY_API_BASE}/orders",
					auth=(api_key, api_secret),
					json=payment_options,
				)
				# Persist the order id on the Integration Request so the reused token
				# carries it (checkout context + settlement order-binding need it).
				if kwargs.get(collect_now.POLICY):
					integration_request.db_set(
						{
							"request_id": order["id"],
							"output": "",
							"data": frappe.as_json({**kwargs, "order_id": order["id"]}),
						},
						update_modified=False,
					)
				else:
					integration_request.update_status(
						{"order_id": order.get("id")}, integration_request.status
					)
				order["integration_request"] = integration_request.name
				return order  # Order returned to be consumed by razorpay.js
			except Exception:
				frappe.log(frappe.get_traceback())
				frappe.throw(_("Could not create razorpay order"))

	def create_request(self, data):
		self.data = frappe._dict(data)
		self._settlement_claimed = False

		try:
			self.integration_request = frappe.get_doc("Integration Request", self.data.token)

			# Fail closed: only Razorpay Integration Requests with a provider binding
			if self.integration_request.integration_request_service != "Razorpay":
				frappe.throw(_("Invalid payment token."), frappe.ValidationError)
			stored = json.loads(self.integration_request.data)
			if not (stored.get("order_id") or stored.get("subscription_id")):
				frappe.throw(_("Invalid payment token."), frappe.ValidationError)

			if stored.get(collect_now.POLICY):
				return collect_now.handle_callback(self, self.integration_request.name, stored, self.data)

			if not self._claim_settlement(self.data, status="Queued"):
				return {"redirect_to": "payment-success", "status": 200}

			return self.authorize_payment()

		except Exception:
			self._release_settlement_claim()
			frappe.log_error(frappe.get_traceback())
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"Seems issue with server's razorpay config. Don't worry, in case of failure amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def authorize_payment(self):
		"""
		An authorization is performed when user's payment details are successfully authenticated by the bank.
		The money is deducted from the customer's account, but will not be transferred to the merchant's account
		until it is explicitly captured by merchant.
		"""
		data = json.loads(self.integration_request.data)
		# settlement (reference, ledger transaction, amounts) reads the stored
		# Integration Request data — never request values. The request's result
		# keys (razorpay_payment_id/signature) were merged into it above.
		self.data = frappe._dict(data)
		settings = self.get_settings(data)

		# a completed token is settled: replays (page refreshes, a second
		# callback with the same token) must not run settlement again
		if self.integration_request.status == "Completed":
			return {"redirect_to": "payment-success", "status": 200}

		if not self.verify_checkout_signature(data, settings):
			self.integration_request.db_set("status", "Failed", update_modified=False)
			self.integration_request.db_set(
				"error", _("Razorpay checkout signature verification failed."), update_modified=False
			)
			ledger.sync_payment_transaction(
				self.data, "Failed", error_message=_("Razorpay checkout signature verification failed.")
			)
			frappe.log_error(
				title="Razorpay signature verification failed",
				message=f"Integration Request: {self.integration_request.name}",
			)
			self._release_settlement_claim()
			return {"redirect_to": "payment-failed", "status": 401}

		resp = {}
		try:
			resp = make_get_request(
				f"https://api.razorpay.com/v1/payments/{self.data.razorpay_payment_id}",
				auth=(settings.api_key, settings.api_secret),
			)

			if not self.payment_matches_stored_checkout(data, resp):
				self.integration_request.db_set("status", "Failed", update_modified=False)
				self.integration_request.db_set(
					"error", _("Razorpay payment does not match the stored checkout."), update_modified=False
				)
				frappe.log_error(
					title="Razorpay payment mismatch",
					message=f"Integration Request: {self.integration_request.name}",
				)
				self._release_settlement_claim()
				return {"redirect_to": "payment-failed", "status": 401}

			if resp.get("status") == "authorized":
				self.integration_request.update_status(data, "Authorized")
				self.flags.status_changed_to = "Authorized"

			elif resp.get("status") == "captured":
				self.integration_request.update_status(data, "Completed")
				self.flags.status_changed_to = "Completed"

			elif data.get("subscription_id"):
				if resp.get("status") == "refunded":
					# if subscription start date is in future then
					# razorpay refunds the amount after authorizing the card details
					# thus changing status to Verified

					self.integration_request.update_status(data, "Completed")
					self.flags.status_changed_to = "Verified"

			else:
				frappe.log_error(message=str(resp), title="Razorpay Payment not authorized")

		except Exception:
			frappe.log_error()
			self._release_settlement_claim()

		status = frappe.flags.integration_request.status_code

		redirect_to = data.get("redirect_to") or None
		redirect_message = data.get("redirect_message") or None
		if self.flags.status_changed_to in ("Authorized", "Verified", "Completed"):
			# Settle a captured reference before the Paid sync. authorize_reference
			# claims Payment Transaction.reference_authorized after the handler;
			# the Paid sync then sees the same flag and cannot dispatch it again.
			# Authorized payments stay Initiated and wait for capture.
			if (
				self.flags.status_changed_to == "Completed"
				and self.data.reference_doctype
				and self.data.reference_docname
			):
				custom_redirect_to = None
				try:
					frappe.flags.data = data
					custom_redirect_to = self.authorize_reference()
				except Exception:
					frappe.log_error(frappe.get_traceback())

				if custom_redirect_to:
					redirect_to = custom_redirect_to

			ledger.sync_payment_transaction(
				self.data,
				"Paid" if self.flags.status_changed_to == "Completed" else "Initiated",
				provider_transaction_id=self.data.get("razorpay_payment_id"),
				metadata={"razorpay_payment_id": self.data.get("razorpay_payment_id")},
			)

			redirect_url = success_redirect(
				reference_doctype=self.data.reference_doctype,
				reference_docname=self.data.reference_docname,
			)
		else:
			self._release_settlement_claim()
			redirect_url = "payment-failed"

		if redirect_to:
			redirect_url += "&" + urlencode({"redirect_to": redirect_to})
		if redirect_message:
			redirect_url += "&" + urlencode({"redirect_message": redirect_message})

		return {"redirect_to": redirect_url, "status": status}

	def payment_matches_stored_checkout(self, data, resp):
		"""The provider's payment must belong to this token's stored checkout.

		The order binding and amount come from the Integration Request (both in
		minor units); the payment entity comes straight from the Razorpay API.
		A payment for any other order, or for a different amount, must never
		settle this checkout. One-off orders check both; subscription checkouts
		auth against the subscription id instead of an order.
		"""
		if data.get("subscription_id"):
			if resp.get("subscription_id") and resp.get("subscription_id") != data.get("subscription_id"):
				return False
			return True
		if str(data.get("order_id") or "").startswith("order_"):
			if resp.get("order_id") != data.get("order_id"):
				return False
			if resp.get("amount") is not None and cint(resp.get("amount")) != cint(data.get("amount")):
				return False
			return True
		# no provider binding on this token: fail closed
		return False

	def authorize_reference(self):
		"""Settle the paid reference document via the partial-aware settler.

		Payment Requests settle through payment_core's builder with this
		controller (mode of payment, charged amount, provider stamps);
		everything else goes to payment_core's generic settler, which
		duck-types own-flow references first and settles mapped references on
		shared-ledger sites. Exactly-once: the callback, webhook and success
		page can all land for one payment — only the first settles.
		"""
		if self._reference_already_authorized():
			return None
		ref = frappe.get_doc(self.data.reference_doctype, self.data.reference_docname)
		if ref.doctype == "Payment Request":
			payment_id = self.data.get("razorpay_payment_id")
			self.flags.razorpay_payment_id = payment_id
			result = self.settle_payment_request(ref, payment_id=payment_id)
		else:
			result = ledger.settle_ledger_reference(self.data, self.flags.status_changed_to)
		self._mark_reference_authorized()
		return result

	def _reference_already_authorized(self):
		"""True when the shared Payment Transaction settled its reference.

		All completion paths use this one flag: callback, webhook, and success
		page reconciliation cannot maintain independent settlement state.
		"""
		transaction_name = self.data.get("payment_transaction")
		return bool(
			transaction_name
			and frappe.db.table_exists("Payment Transaction")
			and frappe.db.get_value("Payment Transaction", transaction_name, "reference_authorized")
		)

	def _mark_reference_authorized(self):
		transaction_name = self.data.get("payment_transaction")
		if transaction_name and frappe.db.table_exists("Payment Transaction"):
			frappe.db.set_value(
				"Payment Transaction", transaction_name, "reference_authorized", 1, update_modified=False
			)

	def get_charged_amount(self, pr):
		"""What Razorpay actually charged for this request, in major units.

		payment_core's Payment Request builder asks the controller for the
		charged amount so a partial charge books a partial Payment Entry.
		Unknown (None) lets the builder settle the outstanding instead.
		"""
		data = getattr(self, "data", None) or {}
		flags = getattr(self, "flags", None) or {}
		verified = flags.get("collect_now_verified_payment")
		if verified:
			return from_minor_units(verified["amount"], verified["currency"])
		payment_id = flags.get("razorpay_payment_id") or data.get("razorpay_payment_id")
		if not payment_id or not str(payment_id).startswith("pay_"):
			return None
		try:
			api_key, api_secret = get_razorpay_auth(self)
			resp = make_get_request(f"{RAZORPAY_API_BASE}/payments/{payment_id}", auth=(api_key, api_secret))
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Razorpay charged amount lookup failed")
			return None
		return from_minor_units(resp.get("amount"), resp.get("currency"))

	def annotate_payment_entry(self, payment_entry, pr):
		"""Stamp the provider payment id onto the booked Payment Entry.

		Called by payment_core's builder after submit: ``reference_no``
		carries the Razorpay payment id so the webhook's ledger-level dedupe
		and verification can find the entry.
		"""
		data = getattr(self, "data", None) or {}
		flags = getattr(self, "flags", None) or {}
		payment_id = flags.get("razorpay_payment_id") or data.get("razorpay_payment_id")
		if not payment_id:
			return
		updates = {"reference_no": payment_id}
		if payment_entry.meta.has_field("razorpay_payment_id"):
			updates["razorpay_payment_id"] = payment_id
		frappe.db.set_value("Payment Entry", payment_entry.name, updates, update_modified=False)

	def settle_payment_request(self, pr, payment_id=None):
		return settlement.settle_payment_request(self, pr, payment_id=payment_id)

	def is_webhook_configured(self):
		"""Structured webhook signal for payment_core (language-safe).

		Consumers must not match get_health_warnings() text — it is
		translated. This reports the signing-secret fact directly.
		"""
		return bool(get_webhook_secret())

	def get_health_warnings(self):
		"""Setup gaps that would destabilise settlement in production."""
		warnings = []
		if not self.webhook_secret:
			warnings.append(_("Webhook signing secret is not configured on {0}").format(self.name))
		if not self.webhook_endpoint:
			warnings.append(_("Webhook endpoint is not registered on {0}").format(self.name))
		return warnings

	def process_webhook_log(self, log_name):
		"""Re-run the processor for a Failed Payment Webhook Log row (desk retry)."""
		log = frappe.get_doc("Payment Webhook Log", log_name)
		event = frappe.parse_json(log.payload) if log.payload else None
		if not event:
			mark_failed(log_name, reconciliation.preserve_sweep_attempt(log.error, "empty payload"))
			return
		# savepoint: route_event's partial writes must not leak into the
		# committed state when the retry fails (same pattern as handle_event)
		frappe.db.savepoint("razorpay_webhook_retry")
		try:
			# elevated: webhook handlers settle references and book receipts —
			# a desk retry must not fail on the retrying user's permissions
			with elevated_user():
				result = reconciliation.route_event(event, self) or {}
			mark_processed(
				log_name,
				status=result.get("status_label", "Processed"),
				reference_doctype=result.get("reference_doctype") or log.reference_doctype,
				reference_docname=result.get("reference_name") or log.reference_docname,
			)
		except Exception:
			frappe.db.rollback(save_point="razorpay_webhook_retry")
			mark_failed(
				log_name,
				reconciliation.preserve_sweep_attempt(log.error, frappe.get_traceback()),
			)

	def reconcile_transaction(self, transaction_name: str):
		"""Re-confirm an open Payment Transaction against Razorpay.

		Called by payment_core when the buyer lands on the shared success page:
		the checkout callback can lose the race with the browser redirect.
		Idempotent — settlement runs through the same paths as the callback
		and webhook. Ledger-only: gateway-only sites settle through their own
		flow.
		"""
		if not frappe.db.table_exists("Payment Transaction"):
			return None
		row = frappe.db.get_value(
			"Payment Transaction",
			transaction_name,
			["provider_transaction_id", "reference_doctype", "reference_docname", "amount", "currency"],
			as_dict=True,
		)
		provider_id = (row.provider_transaction_id if row else "") or ""
		# an open transaction carries the order id; a captured one the payment
		# id. Both resolve to a payment status check below.
		if not provider_id:
			return None

		api_key, api_secret = get_razorpay_auth(self)
		try:
			if provider_id.startswith("pay_"):
				resp = make_get_request(
					f"{RAZORPAY_API_BASE}/payments/{provider_id}", auth=(api_key, api_secret)
				)
			else:
				resp = make_get_request(
					f"{RAZORPAY_API_BASE}/orders/{provider_id}/payments", auth=(api_key, api_secret)
				)
				# the order's payments, newest first: settle on the first
				# captured/authorized one
				resp = next(
					(
						p
						for p in (resp.get("items") or resp or [])
						if p.get("status") in ("captured", "authorized")
					),
					None,
				)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Razorpay reconcile lookup failed")
			return None
		if not resp:
			return None

		# the fetched payment must belong to this transaction's checkout: a
		# captured payment for a different amount never settles it
		charged = from_minor_units(resp.get("amount"), resp.get("currency"))
		precision = get_amount_precision()
		if charged is not None and flt(charged, precision) != flt(row.amount, precision):
			frappe.logger("payment_core").warning(
				"razorpay reconcile: payment %s amount %s != transaction %s amount %s",
				resp.get("id"),
				charged,
				transaction_name,
				row.amount,
			)
			return None

		status = "Paid" if resp.get("status") == "captured" else "Initiated"
		ledger.sync_payment_transaction(
			{"payment_transaction": transaction_name},
			status,
			provider_transaction_id=resp.get("id"),
			metadata={"razorpay_payment_id": resp.get("id")},
		)
		return {"status": status}

	def refund_transaction(
		self, transaction_name: str, amount: float, reason: str = "", idempotency_key: str | None = None
	):
		"""Refund a transaction's Razorpay payment; returns the refund id.

		Ledger-only: gateway-only sites refund through their own flow.
		Razorpay treats ``receipt`` as an idempotency key and also supports the
		``X-Refund-Idempotency`` header. A duplicate receipt resolves to the
		existing provider refund instead of paying twice.
		"""
		if not frappe.db.table_exists("Payment Transaction"):
			frappe.throw(_("Ledger refunds need the shared Payment Transaction"))
		row = frappe.db.get_value(
			"Payment Transaction", transaction_name, ["provider_transaction_id", "currency"], as_dict=True
		)
		payment_id = row.provider_transaction_id if row else None
		if not (payment_id or "").startswith("pay_"):
			frappe.throw(_("Transaction {0} has no Razorpay payment to refund").format(transaction_name))

		client = get_razorpay_client(self)
		payload = {"amount": to_minor_units(flt(amount), row.currency if row else None)}
		headers = None
		receipt = _refund_receipt(idempotency_key)
		if receipt:
			payload["receipt"] = receipt
			headers = {"X-Refund-Idempotency": _refund_header_key(idempotency_key)}
		try:
			refund = (
				client.payment.refund(payment_id, payload, headers=headers)
				if headers
				else client.payment.refund(payment_id, payload)
			)
		except BadRequestError as exc:
			# Razorpay's SDK exposes duplicate receipts only through this error
			# text. If that text changes, re-raising is the safe failure mode.
			if not receipt or "Duplicate receipt found" not in str(exc):
				raise
			refund = _find_refund_by_receipt(client, payment_id, receipt)
			if not refund:
				raise
		return refund.get("id")

	def on_trash(self):
		clear_webhook_secret_cache()

	def verify_checkout_signature(self, data, settings):
		"""Verify the Checkout success signature before settling anything.

		Razorpay requires server-side verification of ``razorpay_signature``
		(HMAC_SHA256 keyed with the API secret) before fulfilling an order. The
		order/subscription id comes from the server-stored Integration Request —
		never from the client — so a signature minted for a different (cheaper)
		order cannot settle this one.
		"""

		payment_id = self.data.get("razorpay_payment_id")
		signature = self.data.get("razorpay_signature")

		if data.get("subscription_id"):
			return verify_subscription_signature(
				data.get("subscription_id"), payment_id, signature, settings.api_secret
			)

		order_id = data.get("order_id")
		if not order_id:
			# Legacy flow without a server-side Order (no signature is issued);
			# authorize_payment still verifies the payment against the API.
			return True

		return verify_payment_signature(order_id, payment_id, signature, settings.api_secret)

	def _claim_settlement(self, params, status=None):
		"""Atomically claim this Integration Request before settlement.

		``IntegrationRequest.update_status`` commits, so the row lock alone does
		not cover the provider lookup and settlement that follow it. Persisting
		A timestamp in ``settlement_started`` in that same write makes the claim
		survive the commit; another request sees an unexpired lease and returns
		without settling again. A crashed worker's lease expires for a later retry.
		"""
		IntegrationRequest = frappe.qb.DocType("Integration Request")
		locked = (
			frappe.qb.from_(IntegrationRequest)
			.select(IntegrationRequest.status, IntegrationRequest.data)
			.where(IntegrationRequest.name == self.integration_request.name)
			.for_update()
			.run(as_dict=True)
		)
		if not locked:
			frappe.throw(_("Invalid payment token."), frappe.ValidationError)

		row = locked[0]
		stored = frappe.parse_json(row.data) or {}
		if row.status in ("Completed", "Authorized") or _settlement_claim_is_active(
			stored.get("settlement_started")
		):
			return False

		claimed = merge_checkout_params(stored, params)
		claimed.pop("token", None)
		claimed["settlement_started"] = now_datetime().isoformat()

		# update_status reads from the loaded document before applying params.
		# Seed it with the locked row so this commit contains the claim and the
		# latest server-stored checkout data in one write.
		self.integration_request.data = frappe.as_json(stored)
		self.integration_request.update_status(claimed, status or row.status)
		self._settlement_claimed = True
		self.data = frappe._dict(frappe.parse_json(self.integration_request.data))
		return True

	def _release_settlement_claim(self):
		"""Clear the settlement_started key so a real retry can run."""
		if not getattr(self, "_settlement_claimed", False):
			return
		stored = frappe.parse_json(
			frappe.db.get_value("Integration Request", self.integration_request.name, "data")
		)
		if stored and stored.get("settlement_started"):
			stored.pop("settlement_started", None)
			self.integration_request.db_set("data", frappe.as_json(stored), update_modified=False)
		self._settlement_claimed = False

	def get_settings(self, data):
		settings = frappe._dict(
			{
				"api_key": self.api_key,
				"api_secret": self.get_password(fieldname="api_secret", raise_exception=False),
			}
		)

		if cint(data.get("notes", {}).get("use_sandbox")) or data.get("use_sandbox"):
			settings.update(
				{
					"api_key": frappe.conf.sandbox_api_key,
					"api_secret": frappe.conf.sandbox_api_secret,
				}
			)

		return settings

	def cancel_subscription(self, subscription_id):
		settings = self.get_settings({})

		try:
			make_post_request(
				f"https://api.razorpay.com/v1/subscriptions/{subscription_id}/cancel",
				auth=(settings.api_key, settings.api_secret),
			)
		except Exception:
			frappe.log_error(frappe.get_traceback())

	@frappe.whitelist()
	def clear(self):
		self.api_key = self.api_secret = None
		self.redirect_url = None
		self.flags.ignore_mandatory = True
		self.save()


def capture_payment(is_sandbox=False, sanbox_response=None):
	"""Scheduler: verify authorized Razorpay payments as complete by capturing them.

	After capture, the amount is transferred to the merchant within T+3 days
	where T is the day on which payment is captured.

	Note: Attempting to capture a payment whose status is not authorized will produce an error.
	"""
	# Load the controller once for the whole batch; per-row state is reset below.
	controller = frappe.get_doc("Razorpay Settings")

	# Never let a scheduled task raise: a failure here would crash the scheduler
	# worker. Log and bail; the next scheduled run retries the same rows.
	try:
		# Cheap COUNT early-exit so the common case (nothing pending) is one
		# query, not a fetch-and-iterate loop that fires every hour.
		if not frappe.db.count(
			"Integration Request",
			{"status": "Authorized", "integration_request_service": "Razorpay"},
		):
			return
		rows = frappe.get_all(
			"Integration Request",
			filters={"status": "Authorized", "integration_request_service": "Razorpay"},
			fields=["name", "data"],
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Razorpay capture_payment could not load pending rows")
		return

	captured = []
	failed = []
	for row in rows:
		# Isolate each row in a savepoint so a failing capture rolls back only
		# its own partial writes; the scheduler commits the whole batch at end.
		frappe.db.savepoint("razorpay_capture_row")
		provider_captured = False
		try:
			data = json.loads(row.data)
			if data.get(collect_now.POLICY):
				frappe.db.release_savepoint("razorpay_capture_row")
				continue
			if is_sandbox:
				resp = sanbox_response
			else:
				# Subscription auth payments are captured automatically by Razorpay
				# via the mandate; manual capture here conflicts with that and 400s.
				if data.get("subscription_id"):
					continue
				settings = controller.get_settings(data)

				resp = make_get_request(
					"https://api.razorpay.com/v1/payments/{}".format(data.get("razorpay_payment_id")),
					auth=(settings.api_key, settings.api_secret),
					data={"amount": data.get("amount")},
				)

				if resp.get("status") == "authorized":
					resp = make_post_request(
						"https://api.razorpay.com/v1/payments/{}/capture".format(
							data.get("razorpay_payment_id")
						),
						auth=(settings.api_key, settings.api_secret),
						json={"amount": data.get("amount")},
					)

			if resp.get("status") == "captured":
				provider_captured = True
				payment_id = resp.get("id") or data.get("razorpay_payment_id")
				data["razorpay_payment_id"] = payment_id
				controller.integration_request = frappe.get_doc("Integration Request", row.name)
				controller.data = frappe._dict(data)
				controller.flags.status_changed_to = "Completed"
				frappe.flags.data = data
				controller.authorize_reference()
				ledger.sync_payment_transaction(
					controller.data,
					"Paid",
					provider_transaction_id=payment_id,
					metadata={"razorpay_payment_id": payment_id},
				)
				captured.append(row.name)
			frappe.db.release_savepoint("razorpay_capture_row")
		except Exception:
			frappe.db.rollback(save_point="razorpay_capture_row")
			frappe.log_error(frappe.get_traceback(), f"Razorpay capture failed for {row.name}")
			# A provider capture cannot be undone. Keep its Integration Request
			# Authorized so the next hourly run retries only local settlement.
			if not provider_captured:
				failed.append(row.name)

	# Bulk flush: two UPDATEs total instead of one set_value per row.
	IntegrationRequest = frappe.qb.DocType("Integration Request")
	if captured:
		(
			frappe.qb.update(IntegrationRequest)
			.set(IntegrationRequest.status, "Completed")
			.where(IntegrationRequest.name.isin(captured))
		).run()
	if failed:
		(
			frappe.qb.update(IntegrationRequest)
			.set(IntegrationRequest.status, "Failed")
			.where(IntegrationRequest.name.isin(failed))
		).run()


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def get_api_key():
	# Publishable API key for the Razorpay checkout SDK. Authenticated callers
	# must have read on Razorpay Settings; the guest checkout flow needs this
	# key client-side, so the gate is the user being a Guest.
	if frappe.session.user != "Guest":
		frappe.has_permission("Razorpay Settings", "read", throw=True)
	return frappe.db.get_single_value("Razorpay Settings", "api_key")


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def get_order(doctype: str, docname: str):
	# Order returned to be consumed by razorpay.js. Authenticated callers must
	# own the reference; guests reach this via the checkout flow (the reference
	# was already authorised when the Integration Request token was minted).
	if frappe.session.user != "Guest":
		frappe.has_permission(doctype, "read", docname, throw=True)
	doc = frappe.get_doc(doctype, docname)
	try:
		# Do not use run_method here as it fails silently
		return doc.get_razorpay_order()
	except AttributeError:
		frappe.log_error(frappe.get_traceback(), _("Controller method get_razorpay_order missing"))
		frappe.throw(_("Could not create Razorpay order. Please contact Administrator"))


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def order_payment_success(integration_request: str, params: str):
	"""Called by razorpay.js on order payment success, the params
	contains razorpay_payment_id, razorpay_order_id, razorpay_signature
	that is updated in the data field of integration request

	Args:
	        integration_request (string): Name for integration request doc
	        params (string): Params to be updated for integration request.
	"""
	if frappe.session.user != "Guest":
		frappe.has_permission("Integration Request", "read", integration_request, throw=True)
	params = json.loads(params)
	integration = frappe.get_doc("Integration Request", integration_request)
	controller = frappe.get_doc("Razorpay Settings")
	controller.integration_request = integration
	controller._settlement_claimed = False

	# Fail closed: only Razorpay Integration Requests with a provider binding
	if integration.integration_request_service != "Razorpay":
		frappe.throw(_("Invalid payment token."), frappe.ValidationError)
	stored = json.loads(integration.data)
	if not (stored.get("order_id") or stored.get("subscription_id")):
		frappe.throw(_("Invalid payment token."), frappe.ValidationError)

	if stored.get(collect_now.POLICY):
		return collect_now.handle_callback(controller, integration.name, stored, params)

	if not controller._claim_settlement(params):
		return

	try:
		controller.authorize_payment()
	except Exception:
		controller._release_settlement_claim()
		raise


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def order_payment_failure(integration_request: str, params: str):
	"""Called by razorpay.js on failure.

	Args:
	        integration_request: the checkout's Integration Request.
	        params: the failure reported by Razorpay Checkout, stored as the request's error.
	"""
	if frappe.session.user != "Guest":
		frappe.has_permission("Integration Request", "read", integration_request, throw=True)
	integration = frappe.get_doc("Integration Request", integration_request)
	if json.loads(integration.data or "{}").get(collect_now.POLICY):
		collect_now.record_client_failure(integration.name, json.loads(params))
		return

	frappe.log_error(params, "Razorpay Payment Failure")
	# Keep the reported failure beside the checkout data; the stored checkout itself is never changed here.
	integration.db_set("error", frappe.as_json(json.loads(params)), update_modified=False)


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def razorpay_subscription_callback():
	"""Razorpay subscription webhook receiver.

	Authorization is the X-Razorpay-Signature header (HMAC-SHA256 of the raw
	body with the Webhook Secret configured in the Razorpay dashboard). The
	caller is Razorpay's servers, not a Frappe user, so no role check applies
	— the signature IS the auth gate. We verify it, elevate to Administrator
	for the Integration Request insert, and let Frappe auto-commit at request
	end (no manual commit).
	"""
	raw_body = frappe.request.get_data()
	signature = frappe.get_request_header("X-Razorpay-Signature")

	if not verify_callback_signature(raw_body, signature):
		# Bad/absent signature — tell Razorpay to stop, do not process.
		frappe.local.response["http_status_code"] = 400
		return {"status": "invalid signature"}

	# Signature verified; elevate so the Integration Request insert + async
	# enqueue can run, then always restore so the next request isn't elevated.
	original_user = frappe.session.user
	try:
		frappe.set_user("Administrator")  # nosemgrep
		frappe.has_permission("Razorpay Settings", "read", throw=True)
		_record_subscription_notification()
	except frappe.InvalidStatusError:
		pass
	except Exception as e:
		frappe.log(frappe.log_error(title=e))
	finally:
		frappe.set_user(original_user)  # nosemgrep


def _record_subscription_notification():
	"""Persist the verified webhook payload + enqueue async handling.

	Lives outside the ``allow_guest`` endpoint so the write is gated behind
	the endpoint's signature verification + Administrator elevation. Runs as
	Administrator, so no ``ignore_permissions`` is needed.
	"""
	data = frappe.local.form_dict
	validate_payment_callback(data)
	data.update({"payment_gateway": "Razorpay"})

	doc = frappe.get_doc(
		{
			"data": json.dumps(frappe.local.form_dict),
			"doctype": "Integration Request",
			"request_description": "Subscription Notification",
			"is_remote_request": 1,
			"status": "Queued",
		}
	).insert()

	frappe.enqueue(
		method="razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.handle_subscription_notification",
		queue="long",
		timeout=600,
		is_async=True,
		**{"doctype": "Integration Request", "docname": doc.name},
	)


def verify_callback_signature(raw_body, signature):
	"""Verify the X-Razorpay-Signature header against the raw webhook body.

	Returns False when no Webhook Secret is configured (so the endpoint fails
	closed rather than silently accepting unsigned traffic) or when the
	signature does not match.
	"""

	# webhook_secret is a Password field, so decrypt it through the controller.
	controller = frappe.get_doc("Razorpay Settings")
	secret = controller.get_password(fieldname="webhook_secret", raise_exception=False)
	if not secret:
		return False
	return verify_webhook_signature(raw_body, signature, secret)


def validate_payment_callback(data):
	def _throw():
		frappe.throw(_("Invalid Subscription"), exc=frappe.InvalidStatusError)

	subscription_id = data.get("payload").get("subscription").get("entity").get("id")

	if not (subscription_id):
		_throw()

	controller = frappe.get_doc("Razorpay Settings")

	settings = controller.get_settings(data)

	resp = make_get_request(
		f"https://api.razorpay.com/v1/subscriptions/{subscription_id}",
		auth=(settings.api_key, settings.api_secret),
	)

	if resp.get("status") != "active":
		_throw()


def handle_subscription_notification(doctype, docname):
	call_hook_method("handle_subscription_notification", doctype=doctype, docname=docname)
