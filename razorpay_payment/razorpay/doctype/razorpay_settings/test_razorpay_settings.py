# Copyright (c) 2024, Frappe Technologies and contributors
# License: MIT. See LICENSE

import frappe
from frappe.tests import IntegrationTestCase
from payment_core.api.gateway import GatewayControllerMixin
from payment_core.utils import guard_payment_reference

from razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings import RazorpaySettings


class TestRazorpaySettings(IntegrationTestCase):
	def test_controller_implements_v1_contract(self):
		self.assertTrue(issubclass(RazorpaySettings, GatewayControllerMixin))
		self.assertTrue(hasattr(RazorpaySettings, "get_payment_url"))
		self.assertTrue(hasattr(RazorpaySettings, "validate_transaction_currency"))

	def test_guard_rejects_unknown_reference(self):
		with self.assertRaises(frappe.PermissionError):
			guard_payment_reference("Payment Request", "does-not-exist-xyz")

	def test_gateway_registered(self):
		frappe.get_doc("Razorpay Settings").db_set("api_key", None, update_modified=False)
		doc = frappe.get_doc("Razorpay Settings")
		doc.flags.ignore_mandatory = True
		doc.save(ignore_permissions=True)
		self.assertTrue(frappe.db.exists("Payment Gateway", "Razorpay"))
