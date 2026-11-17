# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT
#
# The two ways this app surfaces without owning a desk tile: the settings
# doctype declared to payment_core's "Add Gateway" picker, and the patch
# that removes the seeded tile on sites using the "Desktop Icons" grid.

from unittest import skipUnless
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from payment_core.workspace import get_gateway_settings_options

from razorpay_payment.patches.remove_desktop_icon import execute as remove_desktop_icon


class TestGatewayPicker(FrappeTestCase):
	def test_razorpay_in_add_gateway_picker_on_a_fresh_site(self):
		"""The hook alone must list Razorpay Settings: a new site has no
		Payment Gateway row until the first settings save registers one."""

		# a fresh site: no Payment Gateway rows anywhere in the registry
		with patch.object(frappe, "get_all", return_value=[]):
			options = get_gateway_settings_options()

		self.assertIn("Razorpay Settings", [option["label"] for option in options])


@skipUnless(frappe.db.exists("DocType", "Desktop Icon"), "Desktop Icon exists on current Frappe")
class TestDesktopIconPatch(FrappeTestCase):
	def test_patch_removes_seeded_icon_row(self):
		"""The icon-grid seeder stores one row per app; the patch must drop
		ours (all rows pointing at this app, standard or user copies)."""
		frappe.get_doc(
			{
				"doctype": "Desktop Icon",
				"label": "Razorpay Payment",
				"app": "razorpay_payment",
				"icon_type": "App",
				"link": "/desk/razorpay-settings",
			}
		).insert(ignore_permissions=True)
		self.assertTrue(frappe.db.exists("Desktop Icon", {"app": "razorpay_payment"}))

		remove_desktop_icon()

		self.assertFalse(frappe.db.exists("Desktop Icon", {"app": "razorpay_payment"}))
