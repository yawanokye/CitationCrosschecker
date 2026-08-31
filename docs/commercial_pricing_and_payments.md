# CiteIntegrity commercial pricing and payments

## Launch model

CiteIntegrity uses a free preview plus one-time Full Review purchases. There is
no compulsory subscription. Every purchase covers the current Full Review and
one recheck of the same document within 14 days.

| Document package | Ghana | Nigeria | International |
|---|---:|---:|---:|
| Article or assignment | GHS 10 | NGN 1,500 | USD 2.99 |
| Project or long report | GHS 20 | NGN 2,500 | USD 4.99 |
| Master's thesis | GHS 35 | NGN 4,000 | USD 7.99 |
| PhD or large document | GHS 50 | NGN 6,000 | USD 12.99 |

These are fixed local prices, not live foreign-exchange conversions.

## Gateway routing

- Ghana: Paystack, charged directly in GHS with `PAYSTACK_GH_SECRET_KEY`.
- Nigeria: Paystack, charged directly in NGN with `PAYSTACK_NG_SECRET_KEY`.
- Every other country: Stripe Checkout, charged in USD with
  `STRIPE_SECRET_KEY`.
- The server derives the package and amount from the completed preview. Browser
  requests cannot supply or override a price.
- Paystack callbacks/webhooks and Stripe success/webhook handlers verify the
  amount, currency, customer email and provider reference before activation.

Configure these public production URLs in the payment dashboards:

- Paystack Ghana webhook: `/api/webhooks/paystack/ghana`
- Paystack Nigeria webhook: `/api/webhooks/paystack/nigeria`
- Stripe webhook: `/api/webhooks/stripe`
- Stripe event: `checkout.session.completed`

`APP_BASE_URL` must be the HTTPS production origin. Test both gateways in test
mode before replacing the keys with live secrets.

## Developer commercial controls

The protected `/developer/access` portal supports four global modes:

- `payment_required`: show pricing and accept new checkouts.
- `open_access`: temporarily unlock Full Review and hide all public pricing.
- `payments_suspended`: show pricing, preserve existing paid access, and disable
  new checkout attempts.
- `maintenance`: block public product use while authenticated developer testing
  remains available.

The same portal publishes or removes the animated public announcement banner on
the landing and upload pages. The banner text and optional HTTPS link are stored
in `service_settings` and are rendered as text, not injected HTML.

## Launch checks

1. Set a strong `DEVELOPER_PASSWORD` and `DEVELOPER_SESSION_SECRET`.
2. Set `DATABASE_URL`, Redis, `APP_BASE_URL`, both Paystack market keys, and both
   Stripe secrets in the deployment secret store.
3. Run database initialization or start the web process once so the commercial
   tables and safe schema upgrades are applied.
4. Verify a successful and failed payment in every market, including duplicate
   callbacks and webhooks.
5. Confirm open access hides the pricing section and link, and that automatic
   expiry restores payment-required mode.
6. Publish and remove a test announcement from `/developer/access`.
7. Rotate any credential that has ever appeared in a source archive or commit.
