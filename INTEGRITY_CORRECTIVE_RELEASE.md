# CiteIntegrity integrity corrective release 1.6.0

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
- ACII is withheld until publication status is checked for every detected
  reference. Active retractions cap the score at 49 and force `Critical Review`.
- Reports now export publication status, all event records, check state, source,
  and timestamps.
- Verification cache keys were versioned so pre-release rows cannot bypass the
  new publication-status checks.

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
smoke tests pass. Do not clear or reuse older `verify:v2` Redis rows. The release
uses a new `verify:v3-publication-integrity` cache namespace.

## Interpretation boundary

CiteIntegrity provides screening evidence. It does not determine research
misconduct. Reports must preserve sources, timestamps, event history, and human
review decisions so a qualified reviewer can assess each case.
