# CiteIntegrity 2.0.12: complete reference accounting

Release date: 5 October 2026

This release addresses fewer-than-expected verification results in Full View. It retains the preceding reference extraction, publication-status, evidence-resolution and approved Track Changes improvements.

## Corrected behavior

- A saved first chunk no longer makes a job appear completed. Expected totals come from the extracted list, saved reference count and verification target, rather than the number of rows received so far.
- Both result loaders preserve queued/running states. Previously completed partial runs become `incomplete` and can be queued again. The verification endpoint reads the current saved job before deciding whether it is complete.
- Opening or refreshing a dashboard with an active verification job resumes polling, even when some results are already visible. Old completion timestamps and final flags cannot stop a new run's polling.
- Every extracted reference retains its own outcome, including duplicate bibliography entries. Reordered or shortened batch results are aligned by the original reference. Missing results, invalid rows and failed chunks become visible lookup-failure rows, with no claimed match. A missing result keeps processing coverage incomplete.
- A user's click on Verify References can explicitly rerun an already completed check, including a completed check with provider failures. Automatic requests continue to reuse a genuinely completed check and do not force repeated verification.
- Summary cards and reports distinguish references processed, sources matched and references awaiting a result. Full View displays every verification row, including lists longer than the old 600-row presentation limit.
- Author-year search titles use the bibliography parser to exclude journal names, volumes and pages. This corrects contaminated queries observed in the supplied Harvard examples, without changing match thresholds or claiming that every source must match.
- The reference-verification cache key has a new release identity and default namespace, so old matching results do not override the corrected searches.

## Files to deploy

For an installation already running 2.0.11, update the following files from the `CitationCrosschecker-main` folder inside the ZIP:

| File | Action |
| --- | --- |
| `verification_coverage.py` | Add |
| `main.py` | Replace |
| `worker.py` | Replace |
| `verify.py` | Replace |
| `templates/new_results.html` | Replace the served template |
| `new_results.html` | Replace the template mirror |
| `.env.example` | Update the configuration reference |

The ZIP includes this change log and the regression checks. `RELEASE_2_0_12.md` is a file in the project root, not a folder.

Set the deployment environment to:

```text
RELEASE_VERSION=2.0.12-verification-coverage
VERIFY_CACHE_NAMESPACE=v7-verification-coverage
```

Redeploy the web service and all active workers using the same source release. Keep an active worker subscribed to the `verification` queue. No database migration or payment configuration change is required. Existing Redis data does not need to be flushed.

For a retained result with a correctly extracted reference list, reopen the dashboard and click Verify References to run the corrected verification. If References itself still shows fewer entries than the manuscript contains, upload the original document again to repeat extraction. This release cannot reconstruct bibliography entries that are absent from a saved result and its retained source.

## Count interpretation

For a 118-reference manuscript, `118 processed / 6 matched` means all entries have outcomes, with only six successful metadata matches. Human-review results, not-found results and provider failures remain visible in their separate categories. A processed lookup failure does not establish that a publication does not exist. A metadata match does not establish publication safety.

`40 processed / 118 expected` is partial progress, not a completed check. Missing provider results remain in the list as failure rows and keep the run incomplete. Where the extracted list and saved reference total disagree, the dashboard says that extraction needs to be repeated.

## Validation

- Python suite: 183 tests and 4 subtests passed.
- Three dashboard checks passed, covering JavaScript syntax, publication-safety display, source-approval behavior, Full View counts, 650-row display, partial-run warnings, resumed polling and explicit rechecks.
- A 118-reference fixture runs through all three worker chunks, preserves order and duplicate occurrences, and keeps all 118 rows. An omitted middle result produces 117 processed and one pending outcome, rather than false completion.
- Both production result loaders and the verification endpoint are tested with simulated persistence. A completed partial run is retryable, while a complete run is reused unless explicitly rechecked.
- The real Harvard query planner is checked against the previously supplied bibliography examples.

The original 118-reference document and production provider responses were not available in this workspace. These tests establish corrected code behavior, not a live match rate or confirmation that every reference in that document is indexed. Database, queue and provider services are simulated in lifecycle tests.
