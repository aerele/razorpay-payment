# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE
#
# Install/uninstall for Razorpay custom fields on ERPNext doctypes.

import click
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

RAZORPAY_CUSTOM_FIELDS = {
	"Payment Entry": [
		{
			"fieldname": "razorpay_payment_id",
			"fieldtype": "Data",
			"label": "Razorpay Payment ID",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"insert_after": "reference_no",
			"module": "Razorpay",
		}
	],
}


def after_install():
	setup_custom_fields()


def setup_custom_fields():
	if "erpnext" not in frappe.get_installed_apps():
		return
	click.secho("* Installing Razorpay custom fields")
	create_custom_fields(RAZORPAY_CUSTOM_FIELDS)


def remove_custom_fields():
	if "erpnext" not in frappe.get_installed_apps():
		return
	click.secho("* Uninstalling Razorpay custom fields")
	for dt, fields in RAZORPAY_CUSTOM_FIELDS.items():
		frappe.db.delete("Custom Field", {"dt": dt, "fieldname": ("in", [f["fieldname"] for f in fields])})
		frappe.clear_cache(doctype=dt)
