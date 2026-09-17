# CiteIntegrity integrity corrective release 2.0.5

## Purpose

This release responds to the preregistered 20-reference benchmark disclosed on
9 September 2026. The changes address four release-blocking findings: incomplete
verification coverage, missing publication-event detection, numeric reference
splitting, and an ACII rating that could overstate assurance.

## Corrective changes

- Numeric reference boundaries must advance sequentially. Page ranges, DOI
  suffixes, article numbers, and names such as `Grok 3.` no longer create false
  references.
- Reference and publication-status verification is enabled by default.
- Confirmed DOIs are checked against a compact DOI index generated from
  Crossref's official daily Retraction Watch dataset. Publisher `update-to` and
  relation metadata already returned during matching is merged as a second source.
- Full event history is preserved. Retraction, withdrawal, expression of concern,
  correction, reinstatement, and a publication notice cited in its own right are
  distinct outcomes.
- A later reinstatement clears the active-retraction flag while preserving the
  preceding history.
- A failed or unavailable status check is `unchecked`, never `clear`.
- Publication-safety alerts are visible in the free preview. Payment unlocks
  depth, remediation, exports, and workflow rather than concealing a retraction.
- The FastAPI-served results template now displays the publication-safety panel,
  publication status, event count, event history, evidence source, and data
  version. A regression test prevents the served template and maintained mirror
  from drifting apart again.
- Active publication events also appear in a prominent page-level alert above
  the dashboard, so a user does not need to open Source Verification to discover
  a retraction, expression of concern, correction, reinstatement, or notice.
- ACII is withheld until publication status is checked for every detected
  reference. Active retractions cap the score at 49 and force `Critical Review`.
- ACII recency now falls back to the year in the original citation when an
  otherwise verified provider row omits `matched_year`; current numeric
  references are no longer described as outdated for that reason.
- Reports now export publication status, all event records, check state, source,
  and timestamps.
- Verification cache keys were versioned so pre-release rows cannot bypass the
  new publication-status checks.
- Release 2.0.5 queues verification automatically in the backend after analysis,
  so it continues even when the upload page is closed.
- Verification now receives queue priority, uses bounded parallel requests, a
  six-hour DOI-aware cache, larger chunks, and one short-reference rescue query.
- Source-verification rows display as soon as they are complete. Recovery and
  Claim Support no longer block the verification result, and may finish in the
  background.
- Final-result polling reads fresh PostgreSQL state instead of repeatedly using
  a stale Redis result. The old ten-minute waiting loop is limited to 30 seconds
  and ends with a clear worker diagnostic if no verification rows arrive.
- The live-status endpoint normalises RQ status values, safely serialises its
  response, and remains available when a secondary dashboard table fails.
- Numeric reference completeness is assessed before the year. Complete
  Vancouver and IEEE entries are no longer labelled as missing a title merely
  because only volume, issue, pages, or an article number follows the year.
- Evidence Resolution counts now include visible formatting corrections, so the
  overview cannot show zero while pending cards are displayed.

## Acceptance criteria

Run:

```bash
python -m unittest -q test_integrity_corrective_release.py
python -m unittest -q test_product_release.py test_commercial_release.py
```

The release includes `data/retraction_watch.sqlite3`, generated from the
Crossref repository snapshot dated 15 September 2026. Rebuild the index before
future deployments with `scripts/build_retraction_watch_index.py`.

For the supplied DOI benchmark, confirm:

1. Exactly 20 reference entries and 20 verification rows.
2. References 1 to 5 surface active retractions.
3. Reference 5 retains all three events.
4. References 6 and 7 remain distinct from retraction.
5. Reference 8 is reinstated and is not red.
6. Reference 9 is identified as a legitimately cited publication notice.
7. References 10 to 20 have no event only after a successful DOI status check.
8. ACII cannot show `Excellent` when coverage is incomplete or an active
   retraction is present.

## Deployment control

Use the existing developer access control to place the application in maintenance
mode during deployment. Restore paid access only after the benchmark and payment
smoke tests pass. Do not reuse older verification cache rows. The release uses
the `verify:v4-fast-integrity` namespace with a six-hour expiry.

## Interpretation boundary

CiteIntegrity provides screening evidence. It does not determine research
misconduct. Reports must preserve sources, timestamps, event history, and human
review decisions so a qualified reviewer can assess each case.
