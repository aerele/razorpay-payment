# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# On existing sites, reassign the Razorpay Settings doctype to the
# razorpay_payment app's "Razorpay" module. Only the `module` metadata
# changes; records are untouched.

import frappe


def execute():
	if frappe.db.exists("DocType", "Razorpay Settings"):
		frappe.db.set_value("DocType", "Razorpay Settings", "module", "Razorpay", update_modified=False)
