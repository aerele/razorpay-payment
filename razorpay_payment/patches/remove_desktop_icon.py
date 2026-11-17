# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Sites on the "Desktop Icons" grid seeded a tile for this app while it
# declared add_to_apps_screen. The hook is gone, but seeded rows are kept
# by design (switching desktop pages deletes nothing), so remove ours here.

import frappe


def execute():
	if not frappe.db.exists("DocType", "Desktop Icon"):
		return

	icon = frappe.qb.DocType("Desktop Icon")
	frappe.qb.from_(icon).delete().where(icon.app == "razorpay_payment").run()
