$(document).ready(function(){
	(function(e){
		var options = {
			"key": "{{ api_key }}",
			"amount": cint({{ amount }}), // already in minor units (2000 paise = INR 20)
			"currency": "{{ currency }}",
			"name": "{{ title }}",
			"description": "{{ description }}",
			"handler": function (response){
				razorpay.make_payment_log(response, options, "{{ reference_doctype }}", "{{ reference_docname }}", "{{ token }}");
			},
			"prefill": {
				"name": "{{ payer_name }}",
				"email": "{{ payer_email }}"
			},
			"notes": {{ frappe.form_dict|json }}
		};

		// Subscription auth payments are anchored on subscription_id, NOT order_id.
		// Passing both confuses Razorpay's mandate processor — pass only one.
		if ("{{ subscription_id }}") {
			options["subscription_id"] = "{{ subscription_id }}";
		} else if ("{{ order_id }}") {
			options["order_id"] = "{{ order_id }}";
		}

		var rzp = new Razorpay(options);
		rzp.open();
		//	e.preventDefault();
	})();
})

frappe.provide('razorpay');

razorpay.make_payment_log = function(response, options, doctype, docname, token){
	$('.razorpay-loading').addClass('hidden');
	$('.razorpay-confirming').removeClass('hidden');

	frappe.call({
		method:"razorpay_payment.templates.pages.razorpay_checkout.make_payment",
		freeze:true,
		headers: {"X-Requested-With": "XMLHttpRequest"},
		args: {
			"razorpay_payment_id": response.razorpay_payment_id,
			"razorpay_order_id": response.razorpay_order_id,
			"razorpay_signature": response.razorpay_signature,
			"options": options,
			"reference_doctype": doctype,
			"reference_docname": docname,
			"token": token
		},
		callback: function(r){
			if (r.message && r.message.status == 200) {
				window.location.href = r.message.redirect_to
			}
			else if (r.message && ([401,400,500].indexOf(r.message.status) > -1)) {
				window.location.href = r.message.redirect_to
			}
		}
	})
}
