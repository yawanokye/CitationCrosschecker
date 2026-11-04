# CiteIntegrity 2.0.18: Web-service memory controls

Prepared 4 November 2026. Complete source archive based on 2.0.17, including its
verified payment recovery, receipt email and report-link confirmation changes.

## Confirmed incident evidence

Render's event history records an out-of-memory kill for CitationCrosschecker,
service `srv-d6artvvpm1nc73di73ag`, at **12:39:48 UTC on 4 November 2026**. The
reported per-instance limit was **2 GiB**. The current service configuration is
the Standard plan with one running instance and autoscaling configured up to two.
No service plan, scaling or runtime setting was changed during this work.

Render's latest live deployment still points to commit
`295d9328d321bba4ce1bf793d34194b66a36c81f`, deployed on 27 October. Its source was
checked directly in `yawanokye/CitationCrosschecker`. Both the unbounded job
cache and the cleanup batch described below are present in that deployed source.

Live runtime logs and metrics required an explicit workspace selection, which
was unavailable in this session. The exact triggering request, memory-growth
curve and contribution of traffic remain unconfirmed. The source risks below
are supported by code inspection, not an incident-time heap profile.

## Source risks and changes

| Memory risk | Change |
|---|---|
| `load_job_record` retained complete manuscript results in an unbounded process dictionary. | Persistent reads now go directly to the fresh loader. A small legacy fallback store has entry, byte and expiry limits. |
| Cleanup selected and fetched up to 200 complete result JSON objects together. | Select only a bounded list of IDs, then fetch, purge, commit and release one full result at a time. Locked records are skipped. |
| Fresh reads serialized another complete JSON copy into Redis. Result polling also called presentation with persistence enabled. | Reads no longer refresh the shared Redis result or write presentation changes back to the database. Workers and explicit actions retain responsibility for stored results. |
| Uploads used an unrestricted file read. | File reads are capped at 64 MiB plus one detection byte. An ASGI guard also limits declared and chunked multipart bodies before route parsing. |
| DOCX preflight opened the whole Word package and collected text in several forms. | Read only bounded `word/document.xml`, count paragraph text incrementally and leave images and embedded files untouched. Large or uncertain files go to the background queue. |
| Several large result, correction or export requests could allocate memory concurrently. | Admit at most two expensive requests per web process, keeping each slot until response delivery completes. Further requests receive HTTP 429 and Retry-After. Health checks, static pages and payment callbacks bypass this limit. |

Analytical methods, reference-matching rules and payment prices are unchanged.
The file-size ceiling and temporary busy response are new user-visible limits.
They are configurable in the environment. The fallback cache is temporary,
not a replacement for the existing PostgreSQL and Redis services.

These controls address identified allocation and retention paths. They do not
guarantee that every possible document workload fits within 2 GiB. Profile the
deployed application if large-document peaks or sustained growth remain.

## Deploy the prepared update

1. Extract `CitationCrosschecker-main` and copy its contents into the root of
   `yawanokye/CitationCrosschecker`. Preserve production secrets. Include the
   new `web_memory.py` and the earlier `payment_notifications.py` modules.
2. Keep the existing build command `pip install -r requirements.txt` and start
   command `bash start.sh`. The start script already uses one Gunicorn worker.
   No production package dependency was added for these memory controls.
3. Set `RELEASE_VERSION=2.0.18-web-memory`. The additional controls default to
   `MAX_UPLOAD_MB=64`, `WEB_PREFLIGHT_MAX_MB=8` and
   `WEB_MAX_HEAVY_REQUESTS=2`. SMTP and Stripe settings for the included payment
   fixes remain documented in `RENDER_ENVIRONMENT.md`.
4. Commit the complete source. This service currently has automatic deployments
   off, so trigger a normal deployment of the intended new commit. A restart of
   the existing deployment does not install these source changes.
5. Wait for Render to mark the deployment live. Check `/health` for the new
   release identity. Exercise a small upload, a large-file queue route, result
   refresh, a saved source decision and a report download. Review memory use and
   errors while several documents are active and during the next cleanup pass.

The service's configured HTTP health-check path was empty at inspection. If
enabling an HTTP path, verify `/health` responds promptly before saving that
setting. This release does not change the live health-check configuration.
Monitor the existing 2 GiB instance before deciding whether more memory is needed.
Horizontal autoscaling does not increase the memory limit of each instance.

No production deployment, repository push or live payment operation was performed.

## Validation

- **282 Python tests passed**, including 21 new memory regression checks and
  the 30 payment and email checks from 2.0.17. Four existing subtests also passed.
- **All eight dashboard scripts passed**, including correction approvals,
  reference metadata, report downloads and payment recovery.
- The cleanup fixture confirms that only IDs are fetched in bulk and at most
  one source result remains live before the next full-result fetch.
- A cache stress check inserts 200 separate 128,000-byte result payloads into
  a 1 MiB test cache. It retains at most eight entries, with traced peak
  allocation below 4 MiB during that check.
- ASGI tests cover holding capacity through response delivery, cancellation,
  oversized declared bodies, a real FastAPI chunked multipart rejection, and
  health/webhook access while document capacity is full.
- Preflight tests cover skipped media, split Word runs, large compressed XML,
  DTD declarations and malformed files.

Tests use controlled database and network fixtures. They do not reproduce the
production incident or replace post-deployment memory measurements. Existing
warnings concern dependency and datetime deprecations.

## Changed files from 2.0.17

| File | Change |
|---|---|
| `main.py` | Cleanup, persistent loaders, read-only polling, upload cap and request guard |
| `web_memory.py` | Bounded fallback cache, ASGI capacity/body guards and lighter preflight |
| `test_web_memory_release.py` | New regression checks |
| `test_verification_coverage_release.py` | Load the shared fresh-loader dependency in the existing coverage fixture |
| `.env.example`, `env.example`, `RENDER_ENVIRONMENT.md` | Current release and configurable limits |
| `new_results.html`, `templates/new_results.html`, `worker.py`, `verify.py` | Release identity only |
| `test_product_release.py` | Expected current release identity |

Current Render references checked 4 November 2026:

- [Deploying on Render](https://render.com/docs/deploys)
- [Health checks](https://render.com/docs/health-checks)
