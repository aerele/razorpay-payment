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
