# CiteIntegrity 2.0.15: Report-package download fix

Prepared: 6 October 2026. This archive includes the previous fixes through 2.0.14. It must be deployed to change the running service.

## Reproduced failure

The supplied screenshot shows the generic HTTP 500 response from the report-package endpoint. Its access log does not contain the underlying Python exception.

The same download failure was reproduced with the supplied `final work updated 2.docx`. The manuscript does not define the Word table style `Table Grid`. The application-status endpoint builds only the tracked manuscript, so it can succeed. The subsequent package endpoint also builds an annotated manuscript and its correction-register table. Assigning the missing style to that table raised `KeyError: "no style with name 'Table Grid'"` and prevented the ZIP from being returned.

The previous download button navigated directly to the ZIP URL after its preflight check. A server error therefore replaced the dashboard with a JSON error page. The production job's traceback has not been inspected, but this is a confirmed reproduction using the provided manuscript.

## Changes

1. Use the manuscript's `Table Grid` style when available and valid. Otherwise draw borders directly on the new correction-register table. Preserve the manuscript's existing styles and tables.
2. Catch failures separately while building the annotated manuscript, tracked manuscript and report archive. Log the failing stage and traceback on the server. Return an actionable message stating that a failed package build did not delete the analysis.
3. Fetch the complete package before handing it to the browser as a direct ZIP download. Keep the dashboard open on failure and display the server's reason there. Do not open a preview.
4. Disable both download buttons while a request is running, prevent duplicate requests, and restore the controls after failure or cancellation.
5. Keep the two existing choices. Download only sends `delete_after=0`. Download and delete requests `delete_after=1` after confirmation. Deletion remains conditional on the service's existing deletion setting and a successfully delivered response. The UI follows the server's deletion header rather than assuming deletion occurred merely because it was requested.
6. Retain the Word application preflight and its existing warning for approved edits that could not be placed. A failed package build attaches no deletion background task.

Source selection, author and publication-year review, verification matching, publication-status checks, payment routing and pricing are unchanged. The verification cache namespace remains `v8-reference-metadata-safety`.

## Files to replace relative to the application root

| File | Change |
| --- | --- |
| `document_correction_pack.py` | Annotated correction-register table works without the manuscript's built-in table style. Includes the previous media-checksum recovery. |
| `main.py` | Report-generation diagnostics, actionable errors and release identity. |
| `templates/new_results.html` | Direct download with in-page errors, busy controls and deletion-state handling. |
| `new_results.html` | Identical dashboard copy for deployments using the root template. |
| `worker.py` | Release identity only relative to 2.0.14. |
| `verify.py` | Release identity only relative to 2.0.14. |
| `.env.example` | Release-setting example. Do not overwrite live credentials. |

Also included: this `RELEASE_2_0_15.md`, `test_report_download_release.py`, `test_report_download_dashboard.cjs`, and the updated release assertion in `test_product_release.py`.

## Deployment

1. Deploy the changed application files together to the web service and active workers. Set `RELEASE_VERSION=2.0.15-report-download` in the service environment. Keep existing payment secrets, access settings, scholarly API credentials and other environment values.
2. Restart the web service and active workers. Confirm the release at `/health` and refresh the results page so it loads the updated dashboard script.
3. Retry Download only on the retained analysis. It should return a ZIP containing `CiteIntegrity_Annotated_Manuscript.docx`, `CiteIntegrity_Track_Changes.docx`, `correction_plan.csv`, and `submission_readiness_report.html`.
4. Open both DOCX outputs and inspect the approved edits in Word. Download only keeps the analysis available until its existing expiry. If the upload has already expired or been deleted, this release cannot restore it.

## Validation

- Full Python regression suite: **231 tests and 4 subtests passed**.
- All **six JavaScript checks passed**, including direct download, error display, cancellation, duplicate-click prevention, invalid/empty response rejection and both deletion choices.
- Modified Python modules compiled. Both dashboard copies are identical.
- Tests exercised the actual package and application-status routes and actual result-preparation helper, with database/cache/access lookups simulated. Missing or incorrectly defined `Table Grid` styles no longer prevent annotation generation. Original style and media part bytes were preserved in the controlled fixtures.
- Download-only tests never deleted the analysis. Download-and-delete tests ran deletion only after successful response delivery. Injected failures at all three report-generation stages returned a specific HTTP 500 message and never triggered deletion. Deleted analyses remained unavailable.
- A controlled approved-source insertion on the actual supplied manuscript passed application-status preflight and returned HTTP 200 from the package route. The **429,758-byte ZIP** contained all four files and passed checksum validation. Both Word documents opened. The tracked document contained **two tracked insertions**, one citation and one bibliography entry. All **25 existing manuscript tables** remained, and the annotated output added its separate correction-register table. The selected source was a test fixture, not a claim that the manuscript's scholarly sources were independently verified.

The live production service has not been deployed or rerun by this work. A remaining failure will now provide its report-generation stage in the dashboard and a traceback in the server logs.
