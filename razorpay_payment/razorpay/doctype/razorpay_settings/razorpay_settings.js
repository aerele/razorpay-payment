// Copyright (c) 2016, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Razorpay Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Clear"), () => {
			frm.call({
				doc: frm.doc,
				method: "clear",
				callback(r) {
					if (!r.exc) {
						// Reload the whole doc — clear() nulls api_key/api_secret and
						// redirect_url, which a single refresh_field can't represent.
						frm.reload_doc();
					}
				},
			});
		});

		// Credential validation is on-demand only — never block a save on a
		// live Razorpay round-trip (slow / unreachable / rate-limited).
		frm.add_custom_button(__("Test Credentials"), () => {
			frm.call({
				doc: frm.doc,
				method: "test_credentials",
				freeze: true,
				freeze_message: __("Verifying Razorpay credentials..."),
				callback(r) {
					if (!r.exc) {
						frappe.msgprint(__("Razorpay credentials are valid."));
					}
				},
			});
		});
	},
});
