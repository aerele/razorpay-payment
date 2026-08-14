# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Razorpay constants shared across gateway modules.

# REST base for Orders / Payments / Subscriptions.
RAZORPAY_API_BASE = "https://api.razorpay.com/v1"

# Razorpay amounts are in the smallest currency unit, except zero-decimal currencies.
# Keep aligned with common payment-gateway zero-decimal sets (incl. JPY etc.).
ZERO_DECIMAL_CURRENCIES = {
	"BIF",
	"CLP",
	"DJF",
	"GNF",
	"JPY",
	"KMF",
	"KRW",
	"MGA",
	"PYG",
	"RWF",
	"UGX",
	"VND",
	"VUV",
	"XAF",
	"XOF",
	"XPF",
}

# Currencies whose minor unit is 1/1000 (Razorpay expects three decimal places,
# e.g. 295.991 KWD -> 295991 subunits).
THREE_DECIMAL_CURRENCIES = {
	"BHD",
	"IQD",
	"JOD",
	"KWD",
	"OMR",
	"TND",
}

# Custom fields for subscription linkage.
# Razorpay notes (max 15 string → string pairs) carry the ERPNext reference and
# are the self-heal source until razorpay_subscription_id is stamped.
# Subscription Plan.product_price_id (native field, not installed here) holds
# the gateway plan id: Stripe writes price_xxx, Razorpay writes plan_xxx.
RAZORPAY_CUSTOM_FIELDS = {
	"Customer": [
		{
			"fieldname": "razorpay_customer_id",
			"fieldtype": "Data",
			"label": "Razorpay Customer ID",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"search_index": 1,
			"insert_after": "default_currency",
			"module": "Razorpay",
		}
	],
	"Subscription": [
		{
			"fieldname": "razorpay_subscription_id",
			"fieldtype": "Data",
			"label": "Razorpay Subscription ID",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"search_index": 1,
			"insert_after": "status",
			"module": "Razorpay",
		},
	],
	"Payment Entry": [
		{
			"fieldname": "razorpay_payment_id",
			"fieldtype": "Data",
			"label": "Razorpay Payment ID",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"search_index": 1,
			"insert_after": "reference_no",
			"module": "Razorpay",
		}
	],
}
