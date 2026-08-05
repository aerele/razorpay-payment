# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

from frappe.model.document import Document


class RazorpayWebhookLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		razorpay_event_id: DF.Data
		event_type: DF.Data | None
		razorpay_payment_id: DF.Data | None
		status: DF.Literal["Received", "Processed", "Pending", "Ignored", "Failed"]
		razorpay_settings: DF.Link | None
		reference_doctype: DF.Data | None
		reference_name: DF.Data | None
		payload: DF.Code | None
		error: DF.LongText | None
		retry_count: DF.Int
	# end: auto-generated types

	pass
