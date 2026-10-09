# Cashfree setup for MediaFetch

MediaFetch uses Cashfree hosted checkout when both Cashfree API credentials are configured. If Cashfree is not configured, the existing manual UPI flow remains available when a UPI ID is configured.

## 1. Create and activate a Cashfree merchant account

Use the official Cashfree merchant portal and complete the required business/KYC onboarding. Create separate Sandbox and Production API credentials. Do not paste credentials into GitHub, chat, source files, or logs.

## 2. Set Koyeb environment variables

Add these in the MediaFetch Koyeb service:

- `CASHFREE_APP_ID`: App ID from Cashfree
- `CASHFREE_SECRET_KEY`: matching Secret Key
- `CASHFREE_ENVIRONMENT=sandbox` while testing
- `CASHFREE_API_VERSION=2025-01-01`
- `PUBLIC_BASE_URL=https://YOUR-SERVICE-DOMAIN` (use the exact public HTTPS Koyeb domain)

Test using Sandbox credentials first. After Cashfree approves the merchant and live credentials are available, switch to `CASHFREE_ENVIRONMENT=production` and use the matching Production keys. Never mix Sandbox and Production keys.

## 3. Configure Cashfree webhooks

In the Cashfree dashboard, add this HTTPS webhook URL:

`https://YOUR-SERVICE-DOMAIN/cashfree/webhook`

Enable the successful-payment event for orders. Use the API Secret Key associated with the same environment in Cashfree's webhook-signature configuration if the dashboard asks for a signing secret. The endpoint validates the signature against the raw request body and independently queries Cashfree's Orders API before activating a plan.

## 4. Test end-to-end

1. Restart/redeploy the Koyeb service after setting the variables.
2. In the bot, send `/plans` and choose a tier.
3. Share your own phone number using Telegram's contact button; it is sent to Cashfree to create the order and is not written to MediaFetch logs.
4. Open the secure checkout button and complete a Sandbox payment.
5. Confirm that the bot sends an automatic payment confirmation and `/premium` shows the active plan.
6. Test a failed/cancelled payment and confirm that it does not activate premium.
7. Only after successful Sandbox tests, configure Production credentials and webhook URL.

## Important settlement and fee notes

Payment verification and bank settlement are separate things. The webhook activates the subscription after Cashfree confirms the payment; Cashfree controls when funds are settled to the registered bank account.

Cashfree's published festive offer advertises 0% payment-gateway fees for eligible new merchants up to ₹20 lakh in sales, with next-day settlement through 31 March 2027, subject to its terms. Cashfree's published Instant Settlements pricing shows a 0.30% platform fee, excluding GST, and availability/charges may depend on business risk assessment and the selected settlement option. Ask Cashfree to confirm your account's exact offer, instant-settlement eligibility, limits, GST and any holiday/other charges before relying on it.

This integration does not enable instant settlement on the Cashfree account automatically; that must be approved/configured by Cashfree. It also does not activate a plan based only on a browser redirect, a screenshot, or a client-side success message.
