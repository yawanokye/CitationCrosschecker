# CiteIntegrity 2.0.8: reviewable Word edits and download choices

## Changes

- Before an accepted correction is saved, the server checks that it can be placed in the original DOCX as Word Track Changes. Unlocatable edits are rejected with an actionable reason shown on the item card. Bulk reference-format approval leaves unplaceable items pending.
- An approved missing-reference source can revise the matching in-text citation and add its completed reference within the References section. Citation insertion, claim revision, reference completion, citation-case correction, and approved table or figure introductions use tracked insertions or replacements.
- For an unmentioned table or figure, or one first mentioned after its display, the review card proposes a section-opening introduction. The author must replace the descriptive placeholder, verify the wording and approve it before insertion as a tracked paragraph. An exact unique DOCX anchor is required.
- The source approval button shows a saving state, then an approved checkmark and source title. A rejected placement remains visible as an error and is not silently recorded.
- The results panel offers **Download only — keep editing** and **Download and delete**. The package endpoint defaults to no deletion; explicit deletion still uses the existing deployment setting. Download only retains the analysis until its ordinary scheduled expiry.
- The report ZIP includes applied/unapplied Track Changes counts and reasons, plus a highlighted change-control note in the HTML report. The note is not inserted into the manuscript. The CSV also records whether each approved item was applied.

## Verification and deployment

Run `python -m unittest test_table_figure_release.py test_integrity_corrective_release.py test_product_release.py test_verification_operational_release.py test_commercial_release.py` and `node test_dashboard_safety.cjs`. Deploy the web service and active processing workers from this same package, setting `RELEASE_VERSION=2.0.8-reviewable-track-changes`. Keep the existing verification cache namespace and provider/payment credentials.

For a live DOCX check, approve a source needing an in-text update, an incomplete reference, and an editable section-opening table introduction; download without deletion, inspect the Word revision marks and report status, make another decision, then download again. Use **Download and delete** only after the final revision.

A TXT/PDF upload does not provide editable original Word text for tracked manuscript changes. An approved change requiring a DOCX cannot be recorded for such a file; upload the original DOCX. Changes involving complex Word fields or an anchor repeated in multiple paragraphs may also need a longer exact passage or manual editing.
