# CiteIntegrity 2.0.14: Source approval recovery and error reporting

Prepared: 6 October 2026. Deploying this archive updates the application; preparing it does not change the running service.

## Cause and evidence

The supplied access log shows successful source discovery followed by repeated HTTP 422 responses from the correction-decision endpoint. That endpoint validates the source metadata and whether the approved edit can be placed in the original Word document before recording approval. An access log alone does not identify which validation failed for the production job.

Two defects were reproduced locally:

1. The supplied `final work updated 2.docx` has a ZIP checksum mismatch in `word/media/image1.png`. Its manuscript text can be extracted, but the Word loader used for tracked edits rejects the package. The previous loader swallowed that exception and treated the original document as unavailable, preventing approval.
2. The HTTP exception handler returned the reason under `error`, while the approval dashboard read only `detail`. The dashboard therefore hid the actual reason behind “Could not save correction decision.” Repeated clicks retried the same rejected edit.

These explain a reproducible failure on the supplied manuscript. They do not establish that the separate production job in the access log uses that same file or has that exact failure.

## Changes

- Recover a complete media member with a checksum mismatch by repackaging the DOCX and recomputing the checksum. Preserve every extracted package member's bytes during recovery, including the image. This does not reconstruct or edit image content. Core document XML, relationships and other parts must pass their normal checks. Truncated or unreadable members remain blocked.
- Return actionable reasons when the original manuscript has expired, is not DOCX, is damaged, or cannot be opened for tracked changes.
- Include the source-document recovery diagnostic in the tracked-change application manifest.
- Return both `detail` and `error` for HTTP exceptions, retaining authentication headers and existing response fields.
- Read both response formats in the dashboard, including structured validation errors, and display the reason next to the failed approval. Do not show an approved state unless the server accepts the edit.
- Log correction approval rejections with the job, correction identifier and reason. Source-approval validation and uniquely located Word edits remain required.

The manual-review workflow, source-selection safeguards, publication-year confirmation policy, full author handling, publication-status checks, pricing, payment routing and download options remain as in 2.0.13. The verification cache namespace remains `v8-reference-metadata-safety` because this release does not change verification matching.

## Files to replace relative to the application root

| File | Change |
| --- | --- |
| `document_correction_pack.py` | DOCX media-checksum recovery, loader diagnostics and tracked-change manifest. |
| `main.py` | HTTP error compatibility, approval rejection logging and release identity. |
| `templates/new_results.html` | Specific approval error messages and UI release identity. |
| `new_results.html` | Identical dashboard copy for installations using the root template. |
| `worker.py` | Release identity only. |
| `verify.py` | Release identity only. |
| `.env.example` | Release setting example. Apply the setting to the service environment rather than replacing live secrets. |

Also included: `RELEASE_2_0_14.md`, `test_approval_error_release.py`, `test_approval_error_dashboard.cjs`, and the updated release-identity assertion in `test_product_release.py`.

## Deployment and retry

1. Deploy the changed application files together. Set `RELEASE_VERSION=2.0.14-approval-recovery` in the service environment and retain existing credentials and other settings.
2. Restart the web service and active workers. Confirm the release at `/health` and refresh the results page to load the new dashboard code.
3. Retry the selected source after completing the existing source-opening and metadata confirmation steps. If its original DOCX is still retained, the reproduced media-checksum failure can be recovered during approval.
4. If the dashboard instead reports that the original document is unavailable, upload the manuscript again. This release cannot restore an expired or deleted upload. If it reports an ambiguous passage or missing bibliography heading, resolve that placement issue before retrying.
5. Download the tracked document and inspect the approved in-text citation and corresponding bibliography entry in Word. Failure to place an edit continues to prevent approval from being recorded.

## Validation

- Full Python regression suite: **219 tests and 4 subtests passed**.
- All **five JavaScript checks passed**, including legacy/current HTTP error responses and successful versus rejected approval states.
- Modified Python modules compiled and both dashboard copies matched.
- Controlled DOCX fixtures proved that media-checksum recovery preserves all package member bytes, while damaged document XML, relationships, content types, missing uploads and truncated packages remain blocked.
- The actual supplied manuscript opened with **768 paragraphs** after recovery. A controlled source-approval request through the actual correction route and error handler returned HTTP 200 and recorded approval. Its exported Word XML contained **two tracked insertions**, one for the citation and one for the bibliography entry. The image bytes were preserved, and the exported DOCX passed ZIP checksum validation. The test source was a fixture; this does not verify its scholarly relevance or the manuscript's references.
- Rejected placement and missing-original tests returned the specific HTTP 422 reason without saving an approval.

The live production job has not been inspected or rerun here. Its exact rejection reason must come from the response body or the new approval log entry.
