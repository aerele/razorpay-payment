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
from frappe.utils import call_hook_method, cint, get_timestamp, get_url
from payment_core.api.gateway import GatewayControllerMixin
from payment_core.utils import create_payment_gateway, get_reference_amount, guard_payment_reference

from razorpay_payment.gateway import settlement
from razorpay_payment.gateway.client import (
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
	success_redirect,
)
from razorpay_payment.gateway.subscriptions import create_razorpay_subscription
from razorpay_payment.gateway.webhooks import clear_webhook_secret_cache


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

		# Subscription references branch to the Razorpay Subscription create flow
		# (POST /v1/subscriptions → short_url) instead of the one-off Order flow.
		if is_subscription_reference(kwargs):
			# create_razorpay_subscription needs a plain dict, not kwargs.
			return create_razorpay_subscription(self.name, kwargs)

		# create a razorpay order unless a valid razorpay order id is already provided
		if not str(kwargs.get("order_id") or "").startswith("order_"):
			kwargs.setdefault("receipt", kwargs.get("order_id"))
			order = self.create_order(**kwargs)
			kwargs.update({"order_id": order.get("id")})
			# create_order already logged the Integration Request; only its name is
			# needed here to build the checkout URL — read the scalar directly.
			integration_request_name = order["integration_request"]
		else:
			# Token data always carries minor units (same as the create_order path)
			# so the checkout page and capture flow read one consistent unit.

			kwargs["amount"] = to_minor_units(kwargs["amount"], kwargs.get("currency") or "INR")
			integration_request_name = create_request_log(kwargs, service_name="Razorpay").name
		return get_url(f"./razorpay_checkout?token={integration_request_name}")

	def create_order(self, **kwargs):
		# Creating Orders https://razorpay.com/docs/api/orders/

		currency = kwargs.get("currency") or "INR"
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
				integration_request.update_status({"order_id": order.get("id")}, integration_request.status)
				order["integration_request"] = integration_request.name
				return order  # Order returned to be consumed by razorpay.js
			except Exception:
				frappe.log(frappe.get_traceback())
				frappe.throw(_("Could not create razorpay order"))

	def create_request(self, data):
		self.data = frappe._dict(data)

		try:
			self.integration_request = frappe.get_doc("Integration Request", self.data.token)
			# Never let client params overwrite server-stored order binding fields
			# (signature verification pins the payment to this stored order_id).
			protected = {"order_id", "subscription_id", "amount", "currency"}
			stored = json.loads(self.integration_request.data)
			params = {k: v for k, v in self.data.items() if k not in protected or stored.get(k) in (None, "")}
			self.integration_request.update_status(params, "Queued")
			return self.authorize_payment()

		except Exception:
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
		settings = self.get_settings(data)

		if not self.verify_checkout_signature(data, settings):
			self.integration_request.db_set("status", "Failed", update_modified=False)
			self.integration_request.db_set(
				"error", _("Razorpay checkout signature verification failed."), update_modified=False
			)
			frappe.log_error(
				title="Razorpay signature verification failed",
				message=f"Integration Request: {self.integration_request.name}",
			)
			return {"redirect_to": "payment-failed", "status": 401}

		try:
			resp = make_get_request(
				f"https://api.razorpay.com/v1/payments/{self.data.razorpay_payment_id}",
				auth=(settings.api_key, settings.api_secret),
			)

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

		status = frappe.flags.integration_request.status_code

		redirect_to = data.get("redirect_to") or None
		redirect_message = data.get("redirect_message") or None
		if self.flags.status_changed_to in ("Authorized", "Verified", "Completed"):
			if self.data.reference_doctype and self.data.reference_docname:
				custom_redirect_to = None
				try:
					frappe.flags.data = data
					custom_redirect_to = self.authorize_reference()
				except Exception:
					frappe.log_error(frappe.get_traceback())

				if custom_redirect_to:
					redirect_to = custom_redirect_to

			redirect_url = success_redirect(
				reference_doctype=self.data.reference_doctype,
				reference_docname=self.data.reference_docname,
			)
		else:
			redirect_url = "payment-failed"

		if redirect_to:
			redirect_url += "&" + urlencode({"redirect_to": redirect_to})
		if redirect_message:
			redirect_url += "&" + urlencode({"redirect_message": redirect_message})

		return {"redirect_to": redirect_url, "status": status}

	def authorize_reference(self):
		"""Settle the paid reference document via the partial-aware settler."""
		ref = frappe.get_doc(self.data.reference_doctype, self.data.reference_docname)
		if hasattr(ref, "on_payment_authorized"):
			return ref.run_method("on_payment_authorized", self.flags.status_changed_to)
		if ref.doctype == "Payment Request":
			payment_id = self.data.get("razorpay_payment_id")
			self.settle_payment_request(ref, payment_id=payment_id)
		return None

	def settle_payment_request(self, pr, payment_id=None):
		return settlement.settle_payment_request(self, pr, payment_id=payment_id)

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
	# Load the controller once for the whole batch (it carries no per-row state).
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
		try:
			if is_sandbox:
				resp = sanbox_response
			else:
				data = json.loads(row.data)
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
				captured.append(row.name)
			else:
				frappe.db.release_savepoint("razorpay_capture_row")
		except Exception:
			frappe.db.rollback(save_point="razorpay_capture_row")
			frappe.log_error(frappe.get_traceback(), f"Razorpay capture failed for {row.name}")
			failed.append(row.name)

	# Bulk flush: two UPDATEs total instead of one set_value per row.
	if captured:
		frappe.db.sql(
			"UPDATE `tabIntegration Request` SET status = 'Completed' WHERE name IN %(names)s",
			{"names": tuple(captured)},
		)
	if failed:
		frappe.db.sql(
			"UPDATE `tabIntegration Request` SET status = 'Failed' WHERE name IN %(names)s",
			{"names": tuple(failed)},
		)


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

	# Update integration request
	integration.update_status(params, integration.status)
	integration.reload()

	data = json.loads(integration.data)
	controller = frappe.get_doc("Razorpay Settings")

	# Update payment and integration data for payment controller object
	controller.integration_request = integration
	controller.data = frappe._dict(data)

	# Authorize payment
	controller.authorize_payment()


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def order_payment_failure(integration_request: str, params: str):
	"""Called by razorpay.js on failure

	Args:
	        integration_request (TYPE): Description
	        params (TYPE): error data to be updated
	"""
	if frappe.session.user != "Guest":
		frappe.has_permission("Integration Request", "read", integration_request, throw=True)
	frappe.log_error(params, "Razorpay Payment Failure")
	params = json.loads(params)
	integration = frappe.get_doc("Integration Request", integration_request)
	integration.update_status(params, integration.status)


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
