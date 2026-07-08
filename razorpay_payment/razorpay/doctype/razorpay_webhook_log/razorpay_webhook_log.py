# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE

from frappe.model.document import Document


class RazorpayWebhookLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		error: DF.LongText | None
		event_type: DF.Data | None
		payload: DF.Code | None
		razorpay_event_id: DF.Data
		razorpay_object_id: DF.Data | None
		reference_doctype: DF.Data | None
		reference_name: DF.Data | None
		retry_count: DF.Int
		status: DF.Literal["Received", "Processed", "Ignored", "Failed"]
	# end: auto-generated types

	pass
