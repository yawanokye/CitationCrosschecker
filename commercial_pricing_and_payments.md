# CiteIntegrity commercial pricing and payments

## Launch model

CiteIntegrity uses a free preview plus one-time Full Review purchases. There is
no compulsory subscription. Every purchase covers the current Full Review and
one recheck of the same document within 14 days.

The unpaid preview shows the ACII assessment, citation and reference totals,
and 25% of complete rows in each other available result category, capped at 10
rows. Missing Citations, Uncited References and Match Rate remain locked until
payment. Preview actions, exports and certificates are also locked.

| Document package | Ghana | Nigeria | International |
|---|---:|---:|---:|
| Article or assignment | GHS 10 | NGN 1,500 | USD 2.99 |
| Project or long report | GHS 20 | NGN 2,500 | USD 4.99 |
| Master's thesis | GHS 35 | NGN 4,000 | USD 7.99 |
| PhD or large document | GHS 50 | NGN 6,000 | USD 12.99 |

These are fixed local launch prices, not live foreign-exchange conversions.

## Gateway routing

- Ghana: Paystack, charged directly in GHS with `PAYSTACK_GH_SECRET_KEY`.
- Nigeria: Paystack, charged directly in NGN with `PAYSTACK_NG_SECRET_KEY`.
- Every other country: Stripe Checkout, charged in USD with
  `STRIPE_SECRET_KEY`.
- The server derives the package and amount from the completed preview. Browser
  requests cannot supply or override a price.
- Paystack callbacks, central confirmations and Stripe handlers verify the
  amount, currency, customer email and provider reference before activation.

Keep these existing settings in the Paystack dashboard:

- Live webhook: `https://projectreadyai.com/api/paystack/webhook`
- Live callback: ProjectReady's existing callback URL

CiteIntegrity supplies `https://citeintegrity.org/payment/callback` in each
transaction initialization request, so no Paystack dashboard URL needs to be
replaced. New CiteIntegrity references start with `CIT-` and include
`source_app=citeintegrity` plus `product_code=full_analysis` in metadata.

ProjectReady's central webhook must verify Paystack's signature first. It then
keeps processing `PRJ-` transactions locally and forwards only `CIT-`
`charge.success` events to:

- `POST https://citeintegrity.org/api/paystack/payment-confirmation`
- `Content-Type: application/json`
- `X-CiteIntegrity-Signature: <HMAC-SHA256 of the exact forwarded body>`

Both services must use the same `PAYSTACK_CONFIRMATION_SECRET`. CiteIntegrity
does not trust the forwarded success notice by itself. It calls Paystack's
transaction verification API, checks the stored order, reference, amount,
currency, email and metadata, and activates the purchase idempotently.

The direct CiteIntegrity webhook routes below remain available as optional
fallbacks but are not placed in the Paystack dashboard when ProjectReady is the
central router:

- `/api/webhooks/paystack/ghana`
- `/api/webhooks/paystack/nigeria`
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
