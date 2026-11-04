# CiteIntegrity Render environment

Add these values to the CiteIntegrity **web service** in Render. Use **Secret**
for every key or password. Do not commit live values to the repository.

## Central Paystack routing

| Render key | Value |
|---|---|
| `APP_BASE_URL` | `https://citeintegrity.org` |
| `PAYSTACK_PUBLIC_KEY` | Same existing Paystack public key used by ProjectReady |
| `PAYSTACK_GH_SECRET_KEY` | Same existing Ghana Paystack secret key used by ProjectReady |
| `PAYSTACK_NG_SECRET_KEY` | Use the same secret if your Paystack account is enabled for NGN, otherwise use the separate Nigeria-market key |
| `PAYSTACK_CALLBACK_URL` | `https://citeintegrity.org/payment/callback` |
| `PAYSTACK_REFERENCE_PREFIX` | `CIT` |
| `PAYSTACK_SOURCE_APP` | `citeintegrity` |
| `PAYSTACK_CONFIRMATION_SECRET` | A new long random secret shared only with ProjectReady |

Generate the confirmation secret locally with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the same generated value in ProjectReady. ProjectReady uses it to sign the
exact Paystack webhook body forwarded to CiteIntegrity in the
`X-CiteIntegrity-Signature` header.

## International Stripe routing

| Render key | Value |
|---|---|
| `STRIPE_SECRET_KEY` | Stripe live secret key |
| `STRIPE_WEBHOOK_SECRET` | Stripe signing secret for CiteIntegrity's webhook |

Register `https://citeintegrity.org/api/webhooks/stripe` for
`checkout.session.completed` and `checkout.session.async_payment_succeeded`.
Keep test and live credentials separate. After deploying 2.0.17, replay the
affected successful Checkout event or use `recover_stripe_payment.py` with the
original session ID. Recovery never creates another charge.

### Payment acknowledgement and report email

| Render key | Value |
|---|---|
| `SMTP_HOST` | Your mail provider's SMTP hostname |
| `SMTP_PORT` | `587` for STARTTLS, or `465` for implicit TLS |
| `SMTP_USERNAME` | SMTP login, stored as a secret |
| `SMTP_PASSWORD` | SMTP password or provider app password, stored as a secret |
| `SMTP_FROM_EMAIL` | Verified sender, for example `support@citeintegrity.org` |
| `PAYMENT_EMAIL_FROM_NAME` | `CiteIntegrity` |

The web service checks for unsent Stripe confirmations every minute. Failed
delivery is retried after five minutes. Only verified paid purchases with a
retained, accessible report receive a report-link email. No manuscript is
attached to the email. SMTP is independent of Stripe's payment receipt, which
new Checkout sessions request using `payment_intent_data.receipt_email`.
In live mode Stripe sends that receipt after payment succeeds. Test-mode
receipt emails need to be sent manually from Stripe's dashboard.

Set `RELEASE_VERSION=2.0.19-request-polling` for this release.

## Web memory controls

| Render key | Default | Effect |
|---|---|---|
| `MAX_UPLOAD_MB` | `64` | Maximum bytes read for one manuscript file. Larger uploads receive HTTP 413. |
| `WEB_PREFLIGHT_MAX_MB` | `8` | Files or DOCX document XML above this threshold go directly to background preflight. |
| `WEB_MAX_HEAVY_REQUESTS` | `2` | Maximum simultaneous expensive web requests per process, including response delivery. Excess requests receive HTTP 429 with a retry delay. |

Keep the existing single Gunicorn worker in `start.sh`. Document analysis remains
on the existing RQ workers. The web-process fallback cache is capped at eight
entries, approximately 32 MiB of retained Python objects and five minutes.
Production result reads use PostgreSQL and do not populate that cache.

Deploy the complete 2.0.19 source, including `web_memory.py`. Replacing `main.py`
alone will fail because it imports the new module. See `RELEASE_2_0_18.md` for
the incident evidence, validation and deployment steps. The archive also includes
the 2.0.17 payment-recovery and email changes.

## Dashboard request lifecycle

Version 2.0.19 preserves the checkout form across dashboard redraws, shares
concurrent GET requests and caches small pricing recommendations for two minutes
per country and document-count signature. A country change or changed document
counts selects the matching quote. Checkout still validates against live
server-side counts and pricing.

Verification polls run one at a time, wait at least eight seconds after a
response and use `include_result=false` to omit full dashboard preparation and
result delivery. The server still loads the saved record to reconcile coverage.
Initial and final dashboard reads continue to use `/result` for the full report.
Busy responses honor a bounded retry delay. Hidden tabs defer status requests,
and leaving the page stops the polling lifecycle.

Pricing recommendations now select count metadata from PostgreSQL instead of
transferring manuscript JSON to the web process. Legacy records without a saved
word count retain the original full-count fallback. No new environment variable
or database migration is required for the 2.0.19 request changes.

See `RELEASE_2_0_19.md` for the supplied-log interpretation and validation.

## Required commercial service values

| Render key | Recommended value |
|---|---|
| `GLOBAL_ACCESS_MODE` | `payment_required` |
| `PAYMENT_COOKIE_SECURE` | `true` |
| `DEMO_UNLOCK_ALL_FEATURES` | `false` |
| `DEVELOPER_USERNAME` | Private administrator username |
| `DEVELOPER_PASSWORD` | Long unique password |
| `DEVELOPER_SESSION_SECRET` | Long random secret |
| `DATABASE_URL` | CiteIntegrity PostgreSQL connection string |
| `REDIS_URL` | CiteIntegrity Redis connection string |

## Required integrity-verification values

| Render key | Recommended value |
|---|---|
| `CITATION_CROSSCHECKER_MAILTO` | A monitored technical contact email for Crossref polite-pool identification |
| `OPENALEX_API_KEY` | CiteIntegrity's OpenAlex production API key |
| `PUBLICATION_STATUS_CHECK_ENABLED` | `true` |
| `PUBLICATION_STATUS_TIMEOUT` | `8` |
| `PUBLICATION_STATUS_CACHE_TTL` | `21600` |
| `RETRACTION_WATCH_DB_PATH` | Leave unset to use the bundled `data/retraction_watch.sqlite3` snapshot |
| `VERIFY_PARALLEL_MODE` | `1` |
| `VERIFY_PARALLEL_WORKERS` | `8` |
| `VERIFY_CHUNK_SIZE` | `40` |
| `VERIFY_REQUEST_TIMEOUT` | `4` |
| `VERIFY_REFERENCE_BUDGET_SECONDS` | `20`, checked between requests |
| `VERIFY_USE_CACHE` | `0`, keeps the unbounded process-memory cache disabled |
| `VERIFY_REDIS_CACHE_ENABLED` | `1` |
| `VERIFY_REDIS_CACHE_TTL` | `21600` |
| `VERIFY_CACHE_NAMESPACE` | `v6-identity-safety` |
| `RELEASE_VERSION` | `2.0.6-integrity-summary` |
| `VERIFY_SHORT_OPENALEX_MAX_QUERIES` | `1` |

The web service and every document-processing worker must receive the same
integrity-verification values. A missing OpenAlex key can reduce fallback
coverage. A disabled or failed publication-status check is reported as
`unchecked`, never as clear.

The bundled Retraction Watch DOI index was generated from Crossref's official
daily dataset. Refresh it before each release:

```bash
git clone --depth 1 https://gitlab.com/crossref/retraction-watch-data.git /tmp/retraction-watch-data
python build_retraction_watch_index.py \
  --csv /tmp/retraction-watch-data/retraction_watch.csv \
  --output data/retraction_watch.sqlite3 \
  --dataset-date YYYY-MM-DD \
  --source-commit COMMIT_SHA
```

## Safe deployment sequence for this corrective release

1. Set `GLOBAL_ACCESS_MODE=maintenance` from the protected developer portal.
2. Add the integrity-verification values above to the web service and all workers.
3. Deploy the web service and core worker from the same release. Restart both so the new job argument and automatic queueing code match.
4. Set the core worker start command to `python core_worker.py`. Its default priority is `verification,document_processing,large_document_processing` so a completed upload is verified before the next waiting upload.
5. Install `requirements-test.txt` and run `python -m pytest -q`. See `RELEASE_2_0_6.md` for the validation limits and complete live checklist.
6. Process the supplied 20-reference benchmark and confirm 20 verification rows.
7. Confirm the upload page shows verification enabled and that verification starts without clicking a separate button.
8. Restore `GLOBAL_ACCESS_MODE=payment_required` only after the benchmark passes.

The existing Paystack dashboard webhook remains:

`https://projectreadyai.com/api/paystack/webhook`

Do not replace it with a CiteIntegrity webhook URL. ProjectReady must route
`CIT-` events to `https://citeintegrity.org/api/paystack/payment-confirmation`.
The server-side forwarding pattern is documented in
`PROJECTREADY_PAYSTACK_ROUTER.md`.
