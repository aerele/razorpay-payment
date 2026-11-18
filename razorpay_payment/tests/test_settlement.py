# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT
#
# Payment Request settlement is a delegation: payment_core's own builder
# books the Payment Entry (created_from_payment_request allocation, mode of
# payment, charged amount via the controller, partial-charge handling) and
# the Razorpay controller stamps the provider payment id through
# annotate_payment_entry. These tests pin that contract; the builder itself
# is covered by payment_core's suite.

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from razorpay_payment.gateway.settlement import settle_payment_request
from razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings import RazorpaySettings

CONTROLLER_MODULE = "razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings"


def _build_settings():
	settings = MagicMock(name="RS")
	settings.flags = frappe._dict()
	return settings


def _build_pr(outstanding=1000, status="Requested"):
	pr = MagicMock(name="PR")
	pr.docstatus = 1
	pr.status = status
	pr.name = "PR-1"
	pr.outstanding_amount = outstanding
	return pr


class TestRazorpaySettlement(FrappeTestCase):
	def test_settle_delegates_to_payment_core_builder(self):
		settings = _build_settings()
		pr = _build_pr(outstanding=500)

		with patch(
			"razorpay_payment.gateway.settlement.core_settle_payment_request", return_value="PE-1"
		) as core:
			result = settle_payment_request(settings, pr, payment_id="pay_1")

		core.assert_called_once()
		args, kwargs = core.call_args
		self.assertEqual(args[0], pr)
		self.assertIs(kwargs.get("controller"), settings)
		self.assertEqual(result, "PE-1")

	def test_payment_id_reaches_the_controller_flags(self):
		"""get_charged_amount and annotate_payment_entry read the payment id
		from the controller flags when the controller has no request data."""
		settings = _build_settings()
		pr = _build_pr()

		with patch("razorpay_payment.gateway.settlement.core_settle_payment_request", return_value="PE-1"):
			settle_payment_request(settings, pr, payment_id="pay_9")

		self.assertEqual(settings.flags.razorpay_payment_id, "pay_9")

	def test_settle_without_payment_id_passes_none_through(self):
		settings = _build_settings()
		pr = _build_pr()

		with patch(
			"razorpay_payment.gateway.settlement.core_settle_payment_request", return_value=None
		) as core:
			result = settle_payment_request(settings, pr)

		self.assertIsNone(result)
		self.assertIsNone(settings.flags.razorpay_payment_id)
		core.assert_called_once()

	def test_fresh_webhook_controller_reads_payment_id_from_flags(self):
		settings = object.__new__(RazorpaySettings)
		settings.flags = frappe._dict(razorpay_payment_id="pay_9")
		payment_entry = MagicMock(name="PE")
		payment_entry.name = "PE-1"
		payment_entry.meta.has_field.return_value = False

		with (
			patch(f"{CONTROLLER_MODULE}.get_razorpay_auth", return_value=("k", "s")),
			patch(
				f"{CONTROLLER_MODULE}.make_get_request",
				return_value={"amount": 1250, "currency": "INR"},
			),
			patch(f"{CONTROLLER_MODULE}.frappe.db.set_value") as set_value,
		):
			amount = settings.get_charged_amount(_build_pr())
			settings.annotate_payment_entry(payment_entry, _build_pr())

		self.assertEqual(amount, 12.5)
		set_value.assert_called_once_with(
			"Payment Entry", "PE-1", {"reference_no": "pay_9"}, update_modified=False
		)
