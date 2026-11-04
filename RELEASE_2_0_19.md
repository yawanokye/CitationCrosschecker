# CiteIntegrity 2.0.19: Pricing and verification request lifecycle

Prepared 4 November 2026. Complete source based on 2.0.18. It includes the
earlier memory controls, payment recovery and confirmation-email changes.

## What the supplied logs establish

- Result, pricing and status routes returned HTTP 200. That establishes that the
  requests were served. The response bodies are needed to establish whether
  verification finished, whether findings were available or whether paid access
  was active. These GETs do not establish that a payment was made.
- Seven pricing requests for the same job have client-generated cache-buster
  timestamps spanning 239 milliseconds. The server-receipt timestamps and tab
  identities are absent, so the excerpt cannot prove which browser callback or
  tab caused every request.
- Status requests continue over several minutes. The excerpt alone does not
  prove overlap or a memory leak. The previous frontend's fixed interval could
  start another request before a slow response completed.
- The 13:09:59 UTC SIGTERM sequence shows an orderly application and master
  shutdown. Render's event history separately shows autoscaling from two
  instances to one, started at 13:07:58 and completed at 13:08:13. The shutdown
  is consistent with that scale-down, although the supplied process IDs do not
  directly identify the removed service instance.
- The earlier event at 12:39:48 UTC explicitly records an out-of-memory kill
  against the 2 GiB limit. The orderly 13:09 shutdown is separate evidence.
- The missing favicon is a minor asset request and does not establish a report
  failure.

No change to Render scaling, plans or production deployment was made during this
work. The exact memory peak and triggering request still require incident-time
metrics or profiling.

## Source findings and resulting behaviour

`renderPaywall` previously rebuilt the checkout form and called
`loadPlanRecommendation("GH")` on every dashboard redraw. This could repeat
pricing requests, reset the selected billing country and discard a typed email.
Status requests used a fixed eight-second interval without an in-flight guard.
The pricing handler loaded the entire saved manuscript result to obtain counts.

This release makes the following changes:

1. Keep an unchanged checkout form in place. Preserve the email and country when
   changed document counts require a rebuild. Each relevant form state binds its
   listeners once.
2. Share in-flight pricing requests and reuse small successful quotes for two
   minutes per document-count signature and country. Keep at most 12 entries.
   Failed requests can be retried explicitly and do not enable stale checkout.
   A late response for an earlier country cannot overwrite the current choice.
3. Share concurrent identical GETs before adding a cache-buster. Rejected reads
   are removed from the in-flight map so a later request can retry.
4. Run one verification poll at a time. Schedule the next poll after the response
   finishes, rather than on an independent interval. Ignore late responses after
   stopping or restarting the lifecycle. Defer hidden-tab polling and stop on
   page exit. Resume when a persisted page is restored.
5. Back off for HTTP 429 and 503 without immediately exhausting the ordinary
   polling-error counter. Respect Retry-After within a bounded delay.
6. Let the new dashboard request `include_result=false` from `/online/status`.
   These responses preserve counts and queue state while skipping full result
   presentation, correction-plan rebuilding and status-driven result rewrites.
   The underlying saved record is still read for coverage reconciliation.
   Initial and final reports remain full `/result` reads. Older clients retain
   the existing full-result status response by default.
7. Read only PostgreSQL pricing count metadata, not manuscript JSON, for normal
   recommendation requests. Preserve the same count precedence and privacy,
   completion and missing-job checks. Legacy records without a saved word count
   use the original counting path. Checkout retains its authoritative document
   and pricing validation. Run the projection in the thread pool and include
   pricing in the existing heavy-request guard.

These changes reduce duplicate requests and repeated allocation. They do not
establish that the request burst caused the earlier memory kill.

## Files changed from 2.0.18

| File | Change |
|---|---|
| `templates/new_results.html`, `new_results.html` | Stable checkout, cached quotes, shared reads and serial polling |
| `main.py` | Count projection and optional smaller status response |
| `web_memory.py` | Include pricing recommendations in the request guard |
| `test_polling_release.py` | Pricing and status response regressions |
| `test_request_polling_dashboard.cjs` | Browser request and timer lifecycle checks |
| `.env.example`, `env.example`, `RENDER_ENVIRONMENT.md` | Current release and operating guidance |
| `worker.py`, `verify.py`, `test_product_release.py` | Release identity only |

## Validation

**295 Python tests passed**, including 13 new pricing/status checks. Four existing
subtests also passed. **All nine dashboard test scripts passed.**

The new browser test exercises seven rapid redraws producing one pricing GET,
form preservation, stale-country responses, cache expiry, failed-quote retry,
shared result GETs, slow status responses, Retry-After, hidden tabs, page exit
and late-response invalidation. Timers and responses are controlled, so these
checks do not require real-time waiting.

The backend tests use controlled database and queue fixtures. They cover count
projection, legacy fallback, connection cleanup, pricing-field privacy, accurate
verification progress, smaller status responses and older-client compatibility.
They do not replace deployed PostgreSQL and browser smoke tests. Existing
warnings concern datetime and dependency deprecations.

## Deploy

Deploy the complete application source, including the previous new modules.
Set `RELEASE_VERSION=2.0.19-request-polling`. Keep the existing build and start
commands, secrets and queue settings. This release adds no production package
dependency or database migration beyond the included earlier payment changes.

After deployment, confirm the new release identity in `/health`. Hard-refresh an
unpaid dashboard and inspect the browser Network panel. Repeated table refreshes
should not repeatedly request an unchanged pricing recommendation. Change the
billing country and confirm the displayed currency and typed email persist.
Status requests should use `include_result=false` and have no overlapping calls
from the same dashboard polling loop. Test paid access and a report download.
Observe memory and request volume under normal use and cleanup.

The source archive is prepared and tested. It has not been pushed or deployed.
