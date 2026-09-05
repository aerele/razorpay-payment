app_name = "razorpay_payment"
app_title = "Razorpay Payment"
app_publisher = "Frappe Technologies"
app_description = "Standalone Razorpay gateway for Frappe (depends on payment_core)"
app_email = "hello@frappe.io"
app_license = "mit"

# Depends on the shared base app.
required_apps = ["payment_core"]

# Gateway registry for payment_core.
payment_gateway_module = {"Razorpay": "razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings"}

# Subscription create handler for payment_core's dispatcher.
gateway_subscription_handler = {
	"razorpay": "razorpay_payment.gateway.subscriptions.create_razorpay_subscription",
}

# Sync Razorpay plan on save (on_update so outages don't block saves).
doc_events = {
	"Subscription Plan": {"on_update": "razorpay_payment.gateway.subscriptions.sync_razorpay_plan"},
}

# Hourly: capture authorized payments + retry failed webhooks.
scheduler_events = {
	"hourly": [
		"razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.capture_payment",
		"razorpay_payment.gateway.reconciliation.sweep_pending",
	],
}

# Custom field lifecycle.
after_install = "razorpay_payment.install.after_install"
before_uninstall = "razorpay_payment.install.before_uninstall"
after_migrate = ["razorpay_payment.install.after_install"]

add_to_apps_screen = [
	{
		"name": "razorpay_payment",
		"logo": "/assets/razorpay_payment/images/razorpay_payment-logo.svg",
		"title": "Razorpay Payment",
		"route": "/app",
	}
]

export_python_type_annotations = True
