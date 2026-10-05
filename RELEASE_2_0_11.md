# CiteIntegrity 2.0.11: separate reference review and tracked source corrections

Release date: 5 October 2026.

## User-visible changes

Manual Verification now keeps three distinct lists:

1. **References needing human review**: possible matches, metadata differences
   and identity conflicts.
2. **References not found in searched indexes**: no suitable indexed record was
   found. This does not establish that a reference is fabricated.
3. **Reference lookups failed or unavailable**: requests did not complete.
   These references are not counted as not found.

Evidence Resolution uses corresponding distinct groups. Verification summary
cards show Verified, Metadata differences, Human review, Not found and Lookup
failed separately. Full counts are prepared before free-preview sampling.

Where available, unresolved-reference cards show extracted author, year, title,
publication and identifier information. Manuscript-derived fields are labelled
as not independently verified. A sufficiently complete possible external record
can be offered as a source lead, with its evidence URL and proposed reference.
The manuscript's own metadata is never presented as an independently verified
source. No new scholarly lookup is made merely to render these details.

The manual-verification cards now include **Find sources** and **Approve
selected source for Track Changes**. These use the same source discovery and
approval process as Evidence Resolution. Candidates show the proposed reference
and recoverable associated citation edits before approval. The user must open
the source and confirm the intended publication. A visible approval indicator
appears only after the server confirms Word placement and saves the decision.

## Tracked changes

- Approved source corrections replace the reference at its original location.
- Where identifiable, corresponding author/year citations are updated at their
  paragraph anchors, including surname capitalisation and corrected years.
- Numeric citation markers keep their existing reference number.
- Ambiguous author/year keys shared by different references are not updated
  collectively. Repeated indistinguishable paragraph anchors are not guessed.
- A reference and its proposed associated citation edits are applied together.
  If any part cannot be located or edited safely, that correction is left
  unapplied with a reason. Approval is rejected when its initial placement
  check fails.
- Different approved sources in the same paragraph retain each other's edits.
- Pending, ignored and rejected source proposals do not change manuscript text.

Marking a reference manually verified remains a user-attested decision. It does
not itself replace document text. The distinct source-approval action prepares
the approved document edits.

## Files to update from 2.0.10

| File | Action | Purpose |
| --- | --- | --- |
| `reference_review.py` | Add | Grounded source extraction, status counts and citation-edit preparation |
| `evidence_resolution.py` | Replace | Distinct review, not-found and failed-lookup groups |
| `correction_plan.py` | Replace | Source leads, extraction details and saved edit groups |
| `document_correction_pack.py` | Replace | Complete tracked reference/citation corrections |
| `main.py` | Replace | Counts, source-approval preparation and release identity |
| `templates/new_results.html` | Replace | Served dashboard lists, controls, previews and status feedback |
| `new_results.html` | Replace | Matching dashboard fallback/mirror |
| `.env.example` | Replace example only | Current release identity |
| `test_reference_review_release.py` | Add | Classification, approval and tracked-edit regression coverage |
| `test_reference_review_dashboard.cjs` | Add | Dashboard grouping, previews and approval checks |
| `test_harvard_result_release.py` | Replace | Updated result-preparation test dependencies |
| `test_product_release.py` | Replace | New group and release expectations |
| `RELEASE_2_0_11.md` | Add | This guide |

Use the full package if upgrading from an older release. It preserves the
2.0.9 reference extraction and 2.0.10 Harvard formatter fixes. No pricing,
Paystack/Stripe routing, publication-event dataset, database schema or
verification-cache namespace was changed.

## Deployment

1. Deploy the updated package to the web service and every active worker.
   All services need the new shared `reference_review.py` module.
2. If explicitly configured, set
   `RELEASE_VERSION=2.0.11-reference-resolution` in Render. Preserve production
   credentials and settings, do not replace them with `.env.example`.
3. Reload the results page to pick up the new dashboard. The UI build marker is
   `PRODUCTION_RESULTS-commercial-v2.0.11-reference-resolution`.
4. Retained completed results can acquire the new groups when reloaded. Their
   original DOCX must still be available for source-approval placement checks
   and tracked exports. Upload a new analysis if the original document expired
   or if an old result has an incorrect extracted reference list.
5. Open a source candidate, review the proposed reference/citation edits,
   confirm its identity, then approve. Download the Track Changes document
   and review the revisions in Word.

Existing source-verification item IDs and saved decisions are retained. Source
approval and document correction retain the existing Full Review entitlement.
Publication-status safety alerts remain visible without payment.

## Validation and limits

155 pytest tests and four reference-layout subtests passed. Dashboard JavaScript
syntax/safety checks and the new reference-review rendering/approval checks
passed. Python compilation passed for the changed runtime modules. Existing
UTC timestamp deprecation warnings remain unrelated to this release.

New coverage includes 240 unresolved references without truncation, canonical
status aliases, separate free-preview counts, manuscript hints versus external
source leads, approval after opening a source, failed placement without saved
approval, actual Word insertion/deletion XML, associated citation edits, two
sources in one paragraph, whitespace normalisation and rollback of incomplete
corrections.

The service/database/cache and DOM checks use simulated saved payloads and
local Word fixtures. They are not confirmation of live provider availability,
your deployed service or any particular user's stored manuscript. Complex Word
fields, missing originals and ambiguous anchors can still require manual edits,
and are reported rather than silently changed.

Run from the project folder after installing test dependencies:

```sh
python -m pytest -q test_reference_review_release.py test_harvard_result_release.py test_reference_extraction_release.py test_integrity_corrective_release.py test_final_release.py test_table_figure_release.py test_product_release.py test_verification_operational_release.py test_commercial_release.py
node test_reference_review_dashboard.cjs
node test_dashboard_safety.cjs
```
