app_name = "razorpay_payment"
app_title = "Razorpay Payment"
app_publisher = "Frappe Technologies"
app_description = "Standalone Razorpay gateway for Frappe (depends on payment_core)"
app_email = "hello@frappe.io"
app_license = "mit"

# Depends on the shared base app.
required_apps = ["payment_core"]

# Register the Razorpay service module with payment_core's gateway registry.
payment_gateway_module = {"Razorpay": "razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings"}

# Capture authorized-but-uncaptured payments (was scheduler_events["all"] in payments).
scheduler_events = {
	"all": ["razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.capture_payment"],
}

add_to_apps_screen = [
	{
		"name": "razorpay_payment",
		"logo": "/assets/razorpay_payment/images/razorpay_payment-logo.svg",
		"title": "Razorpay Payment",
		"route": "/app",
	}
]

export_python_type_annotations = True
