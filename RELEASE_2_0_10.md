# CiteIntegrity 2.0.10: Harvard saved-result and export fix

Release date: 5 October 2026.

## Reported failure and cause

The result page reported:

```text
cannot access local variable 'doi_str' where it is not associated with a value
```

This is an `UnboundLocalError` in `format_reference_harvard` in
`reference_formatter.py`. An entry with a publisher and no journal/source takes
the book/report branch, which previously returned before `doi_str` was assigned.
The crash occurs with or without a DOI. It was reproduced both by formatting a
book directly and by building a correction plan from a mixed bibliography.

The `/result/{job_id}` endpoint reads the stored result, then rebuilds its
correction plan. A formatter failure at this stage was logged as a database
error by a broad exception handler, although it does not establish a failed
database query or lost job data. Worker-side correction-plan preparation can
also encounter this formatter path.

## Changes

- Prepare Harvard DOI text before the book/report and journal branches.
- Preserve existing author, title, publisher, edition and DOI formatting.
- Clarify result-endpoint failure logging as loading or preparation, including
  the exception type and job ID.
- Set the default application release and environment example to
  `2.0.10-harvard-result-fix`.
- Add executable regression coverage for optional DOI forms, mixed reference
  audits, free-tier result loading from database/cache, and reference exports.

## Files to update from 2.0.9

| File | Action | Purpose |
| --- | --- | --- |
| `reference_formatter.py` | Replace | Runtime fix for Harvard books/reports |
| `main.py` | Replace | Accurate failure logging and default release identity |
| `.env.example` | Replace example only | Current release configuration example |
| `test_harvard_result_release.py` | Add | Regression tests for this defect |
| `test_product_release.py` | Replace | Update the expected release identity |
| `RELEASE_2_0_10.md` | Add | This deployment and validation guide |

Do not overwrite your production environment or secrets with the example file.
If `RELEASE_VERSION` is explicitly set in Render, update that variable there.

The full archive also contains the 2.0.9 extraction corrections documented in
`RELEASE_2_0_9.md`. When upgrading from 2.0.8 or earlier, deploy the full archive,
including `engine.py`, `reference_boundaries.py` and `worker.py`. The parser
build remains `commercial-2026-10-05-reference-boundaries-v2.0.9` because the
parser itself was not changed in this hotfix.

## Deployment and affected results

1. Deploy the updated package to the web service and all active workers, then
   restart/redeploy them so they import the new formatter.
2. If present, set `RELEASE_VERSION=2.0.10-harvard-result-fix` in Render.
   Check `/health` for the release identity after deployment.
3. Reopen the affected result. A completed result still retained in storage can
   be rendered again with this fix. The fresh-read request is
   `/result/6d06f9cb-8b6b-4611-bcd6-757a2e0e3e3b?fresh=1`.
4. Confirm the dashboard loads its correction plan and reference-verification
   rows. Metadata-provider completion remains separate from page rendering.
5. Start a fresh analysis if the stored job failed, expired, or has an incorrect
   reference count from an earlier parser. Refreshing does not re-extract an
   old manuscript's references.

No database migration, queue purge, payment configuration, dataset change or
verification-cache invalidation is required for this formatter fix.

## Validation and limits

136 pytest tests and four layout subtests passed. Dashboard JavaScript syntax
and publication-status card checks passed. The two existing certificate
`datetime.utcnow()` deprecation warnings are unrelated to this change.

The new tests cover:

- Harvard books with no DOI, an empty DOI, a bare DOI, `doi:` and DOI URLs.
- A book, report and journal article audited before online verification.
- Real production result preparation on simulated database and Redis payloads,
  with normal free-tier entitlements.
- HTML and DOCX reference export containing books and reports.

The saved-result tests execute the production endpoint and preparation functions
without starting service connections. Database, Redis and access lookups are
simulated. This is local validation, not confirmation of the deployed job's
stored state or live scholarly-provider results. The original 118-reference
manuscript remains unavailable, so its exact count is not certified here.

Run from the project folder after installing the test dependencies:

```sh
python -m pytest -q test_harvard_result_release.py test_reference_extraction_release.py test_integrity_corrective_release.py test_final_release.py test_table_figure_release.py test_product_release.py test_verification_operational_release.py test_commercial_release.py
node test_dashboard_safety.cjs
```
