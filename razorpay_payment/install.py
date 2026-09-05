# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Install/uninstall Razorpay custom fields on ERPNext doctypes.

import click
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from razorpay_payment.gateway.constants import RAZORPAY_CUSTOM_FIELDS


def after_install():
	# Never let install/migrate abort mid-way and leave the site inconsistent.
	try:
		if "erpnext" not in frappe.get_installed_apps():
			return
		click.secho("* Installing Razorpay custom fields")
		create_custom_fields(RAZORPAY_CUSTOM_FIELDS)
		# Reconciliation looks up SIs by subscription; index it for O(log n).
		if frappe.db.has_column("Sales Invoice", "subscription"):
			frappe.db.add_index("Sales Invoice", ["subscription", "docstatus"])
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Razorpay after_install failed")


def before_uninstall():
	if "erpnext" not in frappe.get_installed_apps():
		return
	click.secho("* Uninstalling Razorpay custom fields")
	for dt, fields in RAZORPAY_CUSTOM_FIELDS.items():
		frappe.db.delete("Custom Field", {"dt": dt, "fieldname": ("in", [f["fieldname"] for f in fields])})
		frappe.clear_cache(doctype=dt)
