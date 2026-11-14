# CiteIntegrity 2.1.0, customer accounts and prepaid subscriptions

Customers can sign in at `/account` with a single-use email code and manage
credits, prepaid subscription terms, payment history and available reports.
Completed Free Previews show a server-calculated credit cost before an explicit
unlock. Existing pay-as-you-go checkout and paid report links remain supported.

The upload page presents Create account, Sign in and Continue as guest before
the document picker. Creating an account is free. Email verification returns
customers to the upload page and preserves an eligible recheck selection.
Existing signed-in users see their account and balance before uploading.

## Failed paid-analysis recovery

The portal accepts a receipt reference or analysis ID under the verified payment
email. Pending Paystack and Stripe receipts are reconciled through their existing
server-side verification helpers. No recovery code creates a checkout or a new
charge. Subscription/top-up receipts restore access to the existing credit balance.

Incomplete reference verification is retried on the original job. A fatal failed
cash-paid analysis can be retried from its still-available temporary file, or the
customer can upload the same manuscript again. Recovery cases serialize replacement
uploads and reuse an existing queued/completed replacement on repeated requests.
The cash entitlement is attached only after successful analysis, document identity
checks where original content remains, and the original tier's size checks. A
replacement does not increase the purchase's analyses-used counter. Credit-funded
failures receive an idempotent refund and can be retried using the restored credits.

Deleted content is not recreated or retained secretly. Replacement uploads start
a fresh ordinary temporary-processing window. Expired payment entitlements or
unmatched documents are directed to support without another checkout.

## Prices and allowances

| Plan | Monthly | Quarterly | Annual | Credits released monthly |
| --- | ---: | ---: | ---: | ---: |
| Individual | GHS 49 | GHS 135 | GHS 490 | 12 |
| Researcher | GHS 99 | GHS 270 | GHS 990 | 30 |

Article reviews use 2 credits, research paper/project reviews use 4, master's
theses use 7, and PhD/large documents use 10. Existing word, reference and
in-text-citation limits determine the tier on the server. Documents exceeding
the largest tier require support, with no debit. Viewing and downloading an
available report is free of additional credits.

Quarterly and annual purchases release the allowance monthly. Calendar
boundaries are computed from the original term anchor, including January 31
and leap years. Unused subscription credits roll over one monthly period,
capped at one allowance of the plan starting that period. Top-ups cost GHS 5
per credit, minimum 2, with no expiry. Expiring subscription credits are spent
first. Manual renewal is stated on the pricing page and must be acknowledged
at checkout. Early renewals are queued after the current prepaid term.

## Payment and accounting safeguards

- GHS subscription and top-up orders use the existing Ghana Paystack key.
  Server-side verification checks reference, amount, currency, email and order
  metadata. Browser redirects alone never grant credits.
- The existing direct Paystack webhook and signed central confirmation route
  both recognise portal orders. Account payment-history buttons can reconcile
  a delayed callback without creating another order.
- PostgreSQL account row locks serialize activations, monthly releases and
  spending. Unique order, release, grant and ledger keys make duplicate
  callbacks and repeated unlocks idempotent. Balances cannot go negative.
- A completed analysis is required before an unlock. Fatal failed/error jobs
  are reconciled for an idempotent refund by the existing background loop and
  when the account loads. Expired credit refunds receive a month to be used.
- Each paid review includes one revision of the same manuscript within 14 days.
  New uploads can select that recheck from the account page. Hashed content
  signatures prevent a filename alone from identifying a different manuscript
  as a revision. Identical uploads reuse an available review without a debit.

## Privacy and access

These additive `ci_*` tables store accounts, hashed login/session tokens,
payment orders, credit lots, grants and audit events. They do not store manuscript
passages or detailed reports. Nonreversible salted content signatures are cleared
after the 14-day recheck window. Expired session and login-code records are
also cleared. Account cookies are HttpOnly and SameSite Lax. POST operations
require a same-origin portal header, with persistent sign-in and checkout limits.

The existing manuscript cleanup schedule is unchanged. Report lists project
only status metadata and links from the temporary jobs store. Paid one-off
reports are also discoverable by their verified account email. Expired content
is labelled unavailable. The included recheck can be validated after original
content deletion without preserving the original manuscript.

The ACII panel exposes publication-status coverage and the existing weighted
methodology. Final scores remain withheld while required checks are pending.
Publication safety warnings and the existing score qualifications are preserved.

## Deployment

No new external service or paid infrastructure is required. The web app creates
the additive portal tables at startup against its existing `DATABASE_URL`.
The existing account must have permission to create tables and indexes.

Existing SMTP values must be configured for email sign-in: `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`.
The existing Ghana Paystack secret must be configured, with `APP_BASE_URL`
set to the public HTTPS site. Production `PAYMENT_COOKIE_SECURE` should remain
true. Set the deployment's `RELEASE_VERSION` to `2.1.0-customer-subscriptions`
if an explicit override is already present.

Developer sign-in requires a configured `DEVELOPER_PASSWORD`. The old hardcoded
password fallback has been removed. With no configured password, developer
Basic authentication and developer sessions are disabled. A configured
`DEVELOPER_SESSION_SECRET` or the existing password-derived signing key is
required for developer sessions.

Deploy the web service from this commit. Document worker changes are not
required for the portal routes. Verify `/health`, `/account`,
`/api/account/plans` and `/api/pricing`, then perform an email sign-in and a
Paystack test-mode purchase before using a live customer payment for a test.
The callback is `/payment/subscription/callback`; the existing webhook URL
does not need to be replaced.

## Validation

The new ledger tests use a real SQLite transaction store with concurrent
activation/spend tests. Gateway and email delivery are simulated. The production
PostgreSQL schema and live gateway integration must also be checked during
deployment. The main FastAPI app serves the portal and both pricing endpoints
in a local smoke check. Dashboard tests exercise the production scripts in
jsdom, including sign-in, all terms, manual-renewal consent, receipts, safe
report labels and pre-unlock costs.

Run `python -m pytest -q` after installing `requirements.txt` and
`requirements-test.txt`. Run the `test_*dashboard.cjs` and
`test_dashboard_*.cjs` scripts with jsdom 27 installed as a development-only
dependency. A full Chromium visual check was unavailable in the execution
environment because the browser's required socket operation was denied.
