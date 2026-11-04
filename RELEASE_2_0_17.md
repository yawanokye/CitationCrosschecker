# CiteIntegrity 2.0.17: Payment confirmation and report recovery

Prepared 3 November 2026. Complete source archive based on 2.0.16, preserving
its dashboard, document export and verification fixes.

## Problem and evidence

A customer reported a USD 2.99 card payment with no accessible report or email.
The supplied code could return activation success when attaching the paid
purchase to its analysis failed. Its webhook could also acknowledge a failed
activation with HTTP 200, preventing normal gateway retry. Raw purchase
timestamps were passed directly to JSONResponse, which can raise a JSON
serialization error. The application had contact-form email but no purchase
acknowledgement or report-link email.

These are source-level findings. They do not establish the cause of the
particular production incident. No live customer payment, production database,
mailbox or retained manuscript was accessed during this work.

## Resulting behaviour

- Both the Stripe return page and signed successful-payment events use the same
  fulfilment function. Delayed successful payments are supported.
- A paid response is matched to the recorded provider reference, purchase ID,
  session, original checkout email, amount, currency and analysis. An editable
  billing email does not override the original purchase identity.
- A browser-supplied analysis ID can constrain recovery but cannot link a paid
  checkout to an unrelated manuscript. A verified job identity must exist in
  the ledger or Stripe metadata.
- Payment success is displayed only after the same entitlement check used by
  report access passes. Failed linking or database work returns a retryable
  webhook response. Repeated callbacks use the existing idempotent attachment
  helper and preserve an existing valid paid link.
- New Stripe Checkout sessions request a Stripe receipt email and save their
  session ID for recovery. The dashboard saves that session before redirecting.
- An interrupted return leads back to the analysis with a recovery message,
  rather than the pricing page. A saved checkout or return triggers recovery.
  Users can also click **Already paid? Restore access**. Existing paid access
  can be recognised even when the browser has no saved session.
- A paid result cannot start a second checkout. An expired or deleted report
  cannot start another checkout either.
- Stripe webhook responses correctly serialize purchase timestamps.
- An SMTP confirmation contains the amount, reference, manuscript name, report
  page and a download link that does not request deletion. A database lock and
  sent marker suppress ordinary duplicate sends. Failed mail remains retryable.
  A crash between SMTP acceptance and database commit can still cause a rare
  duplicate, because SMTP has no transactional exactly-once delivery guarantee.

## Deploy

1. Deploy this complete source to the existing CiteIntegrity web service and
   matching worker services. Preserve live secrets. This package does not
   deploy itself.
2. Set `RELEASE_VERSION=2.0.17-payment-report-recovery`. Keep the correct live
   `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `DATABASE_URL`, `APP_BASE_URL`
   and existing queue settings.
3. Register `https://citeintegrity.org/api/webhooks/stripe` for
   `checkout.session.completed` and `checkout.session.async_payment_succeeded`.
4. Configure the SMTP fields in `RENDER_ENVIRONMENT.md`. On startup, the existing
   commercial schema setup adds the session and email-state columns without
   replacing payment rows. The web process retries eligible unsent mail every
   minute, with a five-minute pause after a delivery failure.
5. Exercise a Stripe test purchase, confirm report access, then test recovery
   after closing the return page. Verify application SMTP mail separately.
   Stripe test payments do not automatically send receipt emails.

## Resolve the reported USD 2.99 transaction

Obtain the customer's checkout email and payment or receipt reference privately.
Find the original successful payment and its Checkout Session ID in Stripe.
Confirm the payment belongs to CiteIntegrity and its recorded manuscript.
Never mark a purchase paid from an email claim or screenshot alone.

After deploying, replay the original successful Checkout event to the configured
webhook, or run the following in the service environment with its existing
database and Stripe secrets. Replace the example ID with the original session.

```bash
python recover_stripe_payment.py --session-id cs_live_REPLACE_WITH_ORIGINAL
```

The command verifies payment with Stripe, repairs the original purchase link and
prints the report URL if retained content is available. It does not create a
charge. To request the application's confirmation email explicitly, add
`--send-confirmation`. A recorded sent marker prevents ordinary duplicate email.
Stripe's own receipt can also be sent from its payment details page.

The existing detailed-content retention default is 86,400 seconds, while paid
recheck credit lasts 14 days. This release preserves that privacy policy. It
cannot restore already deleted manuscript content. If the stored report is
gone, arrange recovery from an authorised retained copy or reprocess the
customer's manuscript without a second charge. The command and recovery API
identify unavailable reports rather than claiming delivery.

## Files changed from 2.0.16

| File | Purpose |
|---|---|
| `stripe_payments.py` | Verified shared fulfilment, receipt request, saved checkout, retry handling |
| `access_control.py` | Additive schema changes and browser checkout lookup |
| `payment_notifications.py` | Durable SMTP acknowledgement and report links |
| `main.py` | Recovery API, checkout guards, safe callback responses, email retry loop |
| `templates/new_results.html`, `new_results.html` | Recovery button, saved session and visible payment messages |
| `recover_stripe_payment.py` | Operator recovery for an existing checkout |
| `worker.py`, `verify.py` | Release identity only |
| `.env.example`, `env.example`, `RENDER_ENVIRONMENT.md` | Deployment and mail settings |
| `requirements-test.txt`, `test_product_release.py` | Test dependency and current release identity |
| `test_payment_recovery_release.py`, `test_payment_recovery_dashboard.cjs` | Regression checks |

## Verification

Completed locally: 261 Python tests passed, including 30 new payment and email
checks, and all eight dashboard test scripts passed. Four existing subtests
also passed. Remaining warnings concern dependency and datetime deprecations.

Run `python -m pytest -q` after installing production and test requirements.
The dashboard checks use jsdom 27, a development dependency, and run with
`node test_payment_recovery_dashboard.cjs` and the existing `test_*dashboard.cjs`
and dashboard-control scripts. No production dependency was added.

Gateway, SMTP and database behaviour in the new tests uses controlled fixtures.
Existing document export and correction tests exercise generated Word and ZIP
content. These checks do not replace a deployed Stripe and SMTP smoke test.

Implementation references, checked 3 November 2026:

- [Stripe fulfilment](https://docs.stripe.com/checkout/fulfillment)
- [Stripe receipts and Checkout receipt_email](https://docs.stripe.com/receipts)
