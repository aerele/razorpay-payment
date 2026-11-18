# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT
#
# Payment Request settlement: delegated to payment_core's own builder
# (created_from_payment_request=True allocation, mode of payment, charged
# amount via the controller, partial-charge handling), with the Razorpay
# payment id stamped onto the Payment Entry through annotate_payment_entry.

import frappe
from payment_core.utils import settle_payment_request as core_settle_payment_request


def settle_payment_request(settings, pr, payment_id=None):
	"""Settle a Payment Request through payment_core's builder.

	``payment_id`` (the Razorpay payment id, e.g. pay_xxx) reaches the
	builder via the controller flags: ``get_charged_amount`` uses it to
	report the actual charge, and ``annotate_payment_entry`` stamps it onto
	the booked Payment Entry's ``reference_no`` for webhook dedupe.
	"""
	settings.flags.razorpay_payment_id = payment_id
	return core_settle_payment_request(pr, controller=settings)
