# CiteIntegrity 2.0.9: author/year reference extraction

Release date: 5 October 2026.

## Reported defect and reproduction

The supplied screenshot uses Harvard-like bibliography entries with bare years,
for example `Amankwaa, E. F. 2013. ...`. The 2.0.8 extraction boundary detector
primarily recognised parenthesised dates. It could merge these entries even
though the individual reference parser and later completeness audit accepted
them. A DOCX constructed from the four complete screenshot entries returned one
merged reference before this change.

The original `final work updated 2.docx` was not attached. The exact extraction
of that file and its claimed 118 entries therefore remains to be checked.

## Changes

- Recognise bare publication years after personal/corporate author lists during
  extraction, independently of the selected output formatting style.
- Preserve explicit Word line breaks and tabs during XML extraction.
- Recognise a bibliography when authors and publication years occupy separate
  lines; retain multi-author lists without splitting co-authors into entries.
- Split flattened bare-year references only at a completed, dated entry boundary.
- Stop at appendix headings and remove the fallback recovery's 500-line cutoff.
- Clarify that the initial upload log has a pending reference count. Its total
  is the number of uploads, not the number of references in that document.
- Log actual extraction counts and parser build from the processing worker.

## Files to update

| File | Action | Purpose |
| --- | --- | --- |
| `engine.py` | Replace | Reference boundaries, Word extraction, recovery and parser build |
| `reference_boundaries.py` | Add | Shared bare-year and author-prefix recognition |
| `worker.py` | Replace | Actual post-parsing count/build log |
| `main.py` | Replace | Clear queued-upload logging and release version |
| `test_reference_extraction_release.py` | Add | Screenshot and 118-entry regression tests |
| `test_product_release.py` | Replace | Align the insertion fixture with the existing References-heading guard; test missing-anchor rejection |
| `RELEASE_2_0_9.md` | Add | This deployment and validation guide |

The four runtime files must be present together. Use this same package for the
web service and every active document/verification worker. An inactive worker
can remain suspended; update it before resuming it. No payment, database schema,
provider API, retraction dataset, or verification-cache setting changed.

## Deployment and rerun

1. Replace/add the files listed above or deploy the full supplied archive.
2. Set `RELEASE_VERSION=2.0.9-reference-extraction` if an older explicit value
   exists, then redeploy the web service and active workers.
3. Upload the original manuscript as a new analysis. Refreshing or verifying the
   old job does not reconstruct its stored reference list.
4. Confirm the References card against the manuscript's actual list before
   relying on missing-citation findings. Check the worker log for
   `build=commercial-2026-10-05-reference-boundaries-v2.0.9` and the actual count.

## Validation

Release checks: 127 pytest tests and four layout subtests passed; dashboard
JavaScript syntax and safety-card checks passed. Existing certificate date
deprecation warnings do not affect this parser change.

The screenshot transcription now produces four distinct references. A synthetic
118-entry fixture preserves all 118 entries and their exact text under ordinary
paragraphs, manual line breaks, flattened paragraphs, and wrapped author/year
layouts. Its deliberately absent citation remains flagged. The autofix wrapper
and production verification input helpers receive all 118 entries. This is
parser/input validation, not live scholarly verification of 118 real sources.

An older export test lacked the References heading now required by the 2.0.8
placement guard. Its valid-insertion fixture now includes that heading, and a
separate test confirms an insertion without it remains unapplied with a reason.
Production Track Changes behavior was not changed for this test correction.

Run:

```sh
python -m pytest -q test_reference_extraction_release.py test_integrity_corrective_release.py test_final_release.py test_table_figure_release.py test_product_release.py test_verification_operational_release.py test_commercial_release.py
node test_dashboard_safety.cjs
```

Install `requirements.txt` and `requirements-test.txt` in a development/test
environment before running the suite.
