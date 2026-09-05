<div align="center">
<a href="https://razorpay.com">
<img src="razorpay_payment/public/images/razorpay_payment-logo.svg" height="80px" width="80px" alt="Razorpay Payment Logo">
</a>
<h2>Razorpay Payment</h2>
<p>Secure Razorpay payments for Frappe and ERPNext</p>

[![CI](https://github.com/aerele/razorpay-payment/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/aerele/razorpay-payment/actions/workflows/ci.yml)
[![Linters](https://github.com/aerele/razorpay-payment/actions/workflows/linter.yml/badge.svg?branch=develop)](https://github.com/aerele/razorpay-payment/actions/workflows/linter.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](license.txt)
</div>

<div align="center">
<a href="https://docs.frappe.io/erpnext/razorpay-integration">Documentation</a>
·
<a href="https://github.com/aerele/razorpay-payment/issues">Report an Issue</a>
·
<a href="https://github.com/aerele/razorpay-payment/pulls">Contribute</a>
</div>

## Razorpay Payment

Razorpay Payment is a standalone payment gateway for Frappe. It connects Razorpay with Frappe applications through [Payment Core](https://github.com/aerele/payment-core) and adds ERPNext settlement workflows when ERPNext is installed.

## Key Features

- **Razorpay Checkout**: Create Razorpay Orders and collect payments through the Razorpay Checkout interface.
- **Secure verification**: Verify checkout signatures and bind successful payments to their server-created orders before settlement.
- **ERPNext settlement**: Settle Payment Requests and create Payment Entries for full and partial payments.
- **Subscriptions and add-ons**: Create and cancel subscriptions, schedule their start dates, and include add-on charges.
- **Signed callbacks**: Verify subscription webhook signatures and process valid notifications asynchronously.
- **Automated capture**: Capture pending authorized payments through an hourly background job.
- **Currency handling**: Validate supported currencies and convert decimal, zero-decimal, and three-decimal amounts correctly.

### Under the Hood

- [**Frappe Framework**](https://github.com/frappe/frappe): The full-stack framework on which the app runs.
- [**Payment Core**](https://github.com/aerele/payment-core): The shared payment gateway contracts and utilities used by the integration.
- [**ERPNext**](https://github.com/frappe/erpnext): Provides Payment Request and Payment Entry workflows.
- [**Razorpay Python SDK**](https://github.com/razorpay/razorpay-python): Communicates with the Razorpay API.

## Installation

The `develop` branch requires Python 3.14 and compatible `develop` branches of Frappe and Payment Core. Install ERPNext before this app when you need its accounting workflows.

Set up a Frappe bench by following the [Frappe installation guide](https://docs.frappe.io/framework/user/en/installation), then run:

```sh
bench get-app https://github.com/aerele/payment-core --branch develop
bench get-app https://github.com/aerele/razorpay-payment --branch develop
bench --site <site-name> install-app payment_core
bench --site <site-name> install-app razorpay_payment
```

## Configuration

1. In the Desk, open **Razorpay Settings**.
2. Enter the API key and API secret from the Razorpay Dashboard.
3. Optionally set **Redirect To** to choose the page shown after a successful payment.
4. Save the settings to create the Razorpay Payment Gateway.
5. Use **Test Credentials** to verify the configured keys.
6. Configure the generated Razorpay gateway in a Payment Gateway Account before creating Payment Requests.

For subscription callbacks, create a webhook in the Razorpay Dashboard with this endpoint:

```text
https://<your-site>/api/method/razorpay_payment.razorpay.doctype.razorpay_settings.razorpay_settings.razorpay_subscription_callback
```

Subscribe it to `subscription.activated` and `subscription.charged`, then enter the same signing secret in **Webhook Secret** under Razorpay Settings.

Use test API keys while testing and replace them with live keys before accepting production payments.

## Development

This app uses `pre-commit` for formatting and linting:

```sh
cd apps/razorpay_payment
pre-commit install
```

Run the test suite with:

```sh
bench --site <site-name> run-tests --app razorpay_payment
```

## Contributing

Contributions are welcome. Before opening a pull request, please create or reference an [issue](https://github.com/aerele/razorpay-payment/issues), add tests for behavioral changes, and ensure the test suite and pre-commit checks pass.

## License

This project is licensed under the [MIT License](license.txt).
