# CiteIntegrity 2.0 commercial release

This release turns the existing demo-oriented build into a pay-as-you-go launch
product for Ghana, Nigeria and international users.

## Customer offer

- Free Preview before payment. Missing Citations, Uncited References and Match
  Rate stay locked. Other available finding categories show 25% of complete
  rows, capped at 10 rows per category.
- One-time document pricing with no required subscription.
- Full Review plus one same-document recheck within 14 days.
- Server-selected package based on document word, reference and citation counts.
- Paystack for Ghana and Nigeria; Stripe for other countries.

## Operator controls

Use the authenticated `/developer/access` portal to require payment, grant
temporary open access, suspend only new payments, or enter maintenance mode.
Temporary open access automatically expires and hides pricing while active.
The portal also controls the animated green notice banner on the landing and
upload pages.

## Security and operations

- Payment amounts and packages are calculated server-side.
- Paid-only APIs enforce entitlement checks on the server.
- Gateway amount, currency, email, reference and webhook signatures are checked.
- Checkout attempts are rate-limited when Redis is available.
- Payment access cookies are HTTP-only, secure by default and valid for 14 days.
- Detailed manuscript content follows the configured privacy lifecycle.

Before launch, put all production credentials in the deployment secret store,
rotate credentials previously present in source code, configure HTTPS callback
and webhook URLs, and complete the test-mode matrix in
`docs/commercial_pricing_and_payments.md`.
