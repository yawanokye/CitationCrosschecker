# CiteIntegrity 2.0.13 — Reference metadata safety

Prepared: 6 October 2026. This archive is ready for deployment; it does not change a running service by itself.

## What was found

The supplied dashboard PDF contains replacement suggestions on similar topics that are different publications. For example, it proposes a Jung/Oh article in place of Yin's methods book. Some candidate cards show two or more surnames while their proposed reference formats them as one person with invented initials. Several discovery paths also limited the stored author list to six or eight contributors.

The old date handling sometimes used a Crossref creation/deposit timestamp or one publication year without considering the other recorded publication dates. The manuscript's Taber reference uses 2018 with volume 48 and pages 1273–1296. The [publisher record](https://link.springer.com/article/10.1007/s11165-016-9602-2) records first online publication on 7 June 2017 and those volume/pages in 2018. A difference between these recorded years is not sufficient evidence of a different publication.

## Changes

1. Keep full structured author metadata separate from surname tokens used for matching. Preserve contributor order, repeated surnames, diacritics, collective authors, and all supplied contributors. Do not silently cap stored author lists. Apply the selected style's display rules only when formatting; long APA lists retain the final author.
2. Carry online, print, issued and publication dates with their provenance. Prefer the final publication date when available, recognize all recorded publication years during matching, and preserve a manuscript year supported by those dates. Creation and deposit dates cannot supply a missing publication year.
3. Withhold unrelated title/DOI records from existing-reference replacement suggestions, including stored suggestions and context-search fallbacks. A genuine DOI with conflicting title or authors requires review rather than verifying the whole citation. Manual search remains available when an exact record is not recovered.
4. Show the complete supplied author list, recorded publication dates, the proposed year and metadata warnings on source cards. Add a “Check / edit full author list and publication year” control after the user opens the source. User-entered metadata is identified as a transcription, not an independent verification result.
5. Require confirmation of a changed author list or publication year. Block approval of known incomplete/truncated authors until the full list has been recovered. Apply the same safeguards on the server; browser controls alone are not relied on.
6. Rebuild the approved reference from checked metadata instead of trusting an old client preview. Approved reference changes and uniquely identified linked author-year citations are written through the existing Word Track Changes workflow. Ambiguous citation mappings remain for individual review.
7. Preserve manuscript authors, title and year in formatted-reference exports when external metadata is unconfirmed. Preserve the original text when a reference cannot be parsed reliably. Include review warnings rather than silently substituting another source.
8. Move verification to the v8 cache namespace and identify the web, worker, verifier and dashboard builds as 2.0.13. Existing publication-status checks, safety visibility, download options, pricing and payment routing are retained.

Crossref's [bibliographic metadata guidance](https://www.crossref.org/documentation/principles-practices/best-practices/bibliographic/) supports recording all contributors and all applicable publication dates.

## Files to add or replace relative to the application root

| File | Purpose |
| --- | --- |
| `reference_metadata.py` | **New.** Full contributor/date metadata, identity screens, approval guards and safe export mapping. |
| `verify.py` | Date-aware matching, full metadata in result rows and DOI conflict handling. |
| `citation_suggester.py` | Full source metadata and stricter intended-publication recovery. |
| `worker.py` | Preserve metadata through recovery candidates and invalidate older cached verification. |
| `main.py` | Metadata-preview endpoint, source-search filtering and server approval validation. |
| `reference_review.py` | Safe extracted source leads and source metadata warnings. |
| `correction_plan.py` | Screen stored candidates before presenting replacements or associated citation edits. |
| `reference_formatter.py` | Separate author formatting, initials, long-list handling and safe exports. |
| `templates/new_results.html` | Author/date review controls and candidate warnings. |
| `new_results.html` | Matching dashboard copy for deployments using the root template. |
| `.env.example` | Updated release identity and cache defaults; use as a settings reference. |
| `RELEASE_2_0_13.md` | This deployment/change log. |

Regression files are also included: `test_reference_metadata_release.py`, `test_reference_metadata_dashboard.cjs`, `test_product_release.py`, `test_reference_review_release.py`, `test_reference_review_dashboard.cjs`, and `test_verification_operational_release.py`.

## Deployment

1. Deploy the application files together to the web service and every active worker. Include the new `reference_metadata.py` module.
2. Set these existing-service environment values explicitly; updating `.env.example` alone does not change service settings:

   ```text
   RELEASE_VERSION=2.0.13-reference-metadata-safety
   VERIFY_CACHE_NAMESPACE=v8-reference-metadata-safety
   ```

3. Restart the web service and active workers. Confirm the release identity at `/health`. Keep the current database, payment secrets, pricing, access-mode settings and scholarly API credentials.
4. For saved analyses, use **Recheck References** to refresh provider metadata and use **Find sources** again for unresolved entries. The namespace invalidates reusable verification cache entries; it does not rewrite already saved analyses or past approval decisions. Check previously approved wrong-source changes before exporting them again.
5. Check one multi-author source and the Taber online/final-date case, approve an intentional metadata change, and inspect its reference and linked citations in Word Track Changes. This is a deployment check, not an assertion that every manuscript reference has been independently verified.

## Validation

- Python regression suite: **208 tests and 4 subtests passed**. Six existing `datetime.utcnow()` deprecation warnings do not indicate test failures.
- Four JavaScript checks passed: dashboard safety rendering, verification coverage, manual-reference review and the new author/date confirmation controls.
- Compilation succeeded for every modified Python application module. Both dashboard copies are identical.
- An export check on the supplied manuscript preserved the original author count, title and year across all **62 parsed bibliography entries**, including duplicates present in that file. This is an export-preservation result, not a count of independently verified publications.
- Controlled provider fixtures cover full 3-, 15- and 31-author records; repeated surnames; corporate names; online/final dates; excluded registration timestamps; valid DOIs with conflicting metadata; incomplete author payloads; unrelated stored suggestions; and approved reference/citation changes in Word revision XML.

Index metadata can still be incomplete or wrong. This release exposes that uncertainty and provides a checked approval workflow. Its test results do not measure live scholarly-database coverage or guarantee every proposed source is correct.
