# Payment setup for MediaFetch

## Current default: manual UPI/UTR approval

MediaFetch defaults to manual payment verification. Users select a plan, pay using the configured UPI ID/QR, submit the UTR/reference, and the owner verifies the transaction in the bank/UPI app before approving it in the owner panel.

- Configure the UPI ID and plan prices/durations using /admin → Payments → Configure UPI/Prices.
- Open /admin → Payments → Pending Payments to review requests.
- Approve only after confirming the exact amount and UTR in the bank/UPI app.
- The plan activates only after owner approval.
- Keep MongoDB configured so pending requests and payment history survive restarts.

## Optional Cashfree (disabled by default)

Cashfree is opt-in only and does not run merely because credentials exist. Do not enable it until merchant KYC and live API access are ready.

If you later choose to test Cashfree, set CASHFREE_ENABLED=true, configure CASHFREE_APP_ID, CASHFREE_SECRET_KEY, CASHFREE_ENVIRONMENT=sandbox, CASHFREE_API_VERSION=2025-01-01, and PUBLIC_BASE_URL. Configure the matching webhook at https://YOUR-SERVICE-DOMAIN/cashfree/webhook. Use sandbox first and only switch to production after end-to-end verification.

Never put credentials in GitHub or paste them into chat. Payment verification and bank settlement are separate; fees and settlement timing depend on merchant eligibility and provider terms.
