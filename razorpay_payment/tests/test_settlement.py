# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from razorpay_payment.gateway.settlement import settle_payment_request


def _build_settings():
	"""Build a mock Razorpay Settings doc. Settlement no longer reads the IR."""
	return MagicMock(name="RS")


def _build_pr(outstanding=1000, status="Requested", reference_doctype="Sales Invoice", reference_name="SI-1"):
	pr = MagicMock(name="PR")
	pr.docstatus = 1
	pr.status = status
	pr.payment_channel = "Razorpay"
	pr.reference_name = reference_name
	pr.reference_doctype = reference_doctype
	pr.outstanding_amount = outstanding
	pr.currency = "INR"
	pr.payment_account = "Bank - INR"
	pr.name = "PR-1"
	return pr


class TestRazorpaySettlement(FrappeTestCase):
	def test_settle_skips_already_paid(self):
		settings = _build_settings()
		pr = _build_pr(status="Paid")
		with (
			patch("razorpay_payment.gateway.settlement.frappe.set_user") as su,
			patch("erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry") as gpe,
		):
			settle_payment_request(settings, pr)
		gpe.assert_not_called()
		su.assert_not_called()

	def test_settle_uses_pr_outstanding_as_pe_amount(self):
		settings = _build_settings()
		pr = _build_pr(outstanding=500)
		pe = MagicMock(name="PE")

		meta_mock = MagicMock()
		meta_mock.has_field.return_value = True

		with (
			patch("razorpay_payment.gateway.settlement.frappe.set_user"),
			patch("razorpay_payment.gateway.settlement.frappe.get_meta", return_value=meta_mock),
			patch("razorpay_payment.gateway.settlement.frappe.db.get_value", return_value=500.0),
			patch("razorpay_payment.gateway.settlement.nowdate", return_value="2026-07-28"),
			patch(
				"erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
				return_value=pe,
			) as gpe,
			patch(
				"erpnext.accounts.doctype.payment_request.payment_request.get_existing_payment_entry",
				return_value=None,
			),
		):
			settle_payment_request(settings, pr)

		gpe.assert_called_once()
		_, kwargs = gpe.call_args
		self.assertEqual(kwargs.get("party_amount"), 500.0)
		pe.insert.assert_called_once()
		pe.submit.assert_called_once()

	def test_settle_allows_second_pe_when_si_has_outstanding(self):
		settings = _build_settings()
		pr = _build_pr(outstanding=500)
		pe = MagicMock(name="PE")

		meta_mock = MagicMock()
		meta_mock.has_field.return_value = True

		with (
			patch("razorpay_payment.gateway.settlement.frappe.set_user"),
			patch("razorpay_payment.gateway.settlement.frappe.get_meta", return_value=meta_mock),
			patch("razorpay_payment.gateway.settlement.frappe.db.get_value", return_value=500.0),
			patch("razorpay_payment.gateway.settlement.nowdate", return_value="2026-07-28"),
			patch(
				"erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
				return_value=pe,
			),
			patch(
				"erpnext.accounts.doctype.payment_request.payment_request.get_existing_payment_entry",
				return_value="PE-1",
			),
		):
			settle_payment_request(settings, pr)

		pe.insert.assert_called_once()
		pe.submit.assert_called_once()

	def test_settle_blocks_duplicate_when_si_fully_paid(self):
		settings = _build_settings()
		pr = _build_pr(outstanding=1000)

		meta_mock = MagicMock()
		meta_mock.has_field.return_value = True

		with (
			patch("razorpay_payment.gateway.settlement.frappe.set_user"),
			patch("razorpay_payment.gateway.settlement.frappe.get_meta", return_value=meta_mock),
			patch("razorpay_payment.gateway.settlement.frappe.db.get_value", return_value=0.0),
			patch("razorpay_payment.gateway.settlement.nowdate", return_value="2026-07-28"),
			patch("erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry") as gpe,
			patch(
				"erpnext.accounts.doctype.payment_request.payment_request.get_existing_payment_entry",
				return_value="PE-1",
			),
		):
			settle_payment_request(settings, pr)

		gpe.assert_not_called()

	def test_settle_skips_duplicate_guard_for_sales_order(self):
		settings = _build_settings()
		pr = _build_pr(outstanding=500, reference_doctype="Sales Order", reference_name="SO-1")
		pe = MagicMock(name="PE")

		meta_mock = MagicMock()
		meta_mock.has_field.return_value = False

		with (
			patch("razorpay_payment.gateway.settlement.frappe.set_user"),
			patch("razorpay_payment.gateway.settlement.frappe.get_meta", return_value=meta_mock),
			patch("razorpay_payment.gateway.settlement.frappe.db.get_value") as gv,
			patch("razorpay_payment.gateway.settlement.nowdate", return_value="2026-07-28"),
			patch(
				"erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
				return_value=pe,
			),
			patch(
				"erpnext.accounts.doctype.payment_request.payment_request.get_existing_payment_entry",
				return_value="PE-1",
			),
		):
			settle_payment_request(settings, pr)

		pe.insert.assert_called_once()
		pe.submit.assert_called_once()
		gv.assert_not_called()
