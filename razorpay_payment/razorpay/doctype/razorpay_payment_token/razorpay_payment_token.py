# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE

from frappe.model.document import Document


class RazorpayPaymentToken(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		card_last4: DF.Data | None
		card_network: DF.Data | None
		customer: DF.Link | None
		method: DF.Data | None
		razorpay_customer_id: DF.Data | None
		status: DF.Literal["Active", "Deleted"]
		token_id: DF.Data
	# end: auto-generated types

	pass
