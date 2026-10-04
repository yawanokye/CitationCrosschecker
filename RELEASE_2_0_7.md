# CiteIntegrity 2.0.7: manuscript presentation and reviewable corrections

Prepared 4 October 2026. This source release builds on 2.0.6; it has not been deployed by this package build.

## Changes

- Check numbered table and figure captions against mentions in the manuscript. The summary shows tables, figures, unmentioned items, unmatched callouts, and caption or numbering findings. Individual findings enter Evidence Resolution. Small lists and ranges such as “Tables 1 and 2” and “Figures 1–3” are expanded.
- A user can propose exact table or figure wording and approve it for Word Track Changes. An unmentioned item requires a selected body-text anchor; the system does not invent an interpretation of a table or image.
- Once a scholarly source has been opened and approved, author-year citations with an incorrectly lowercase surname can be reviewed and changed to the confirmed display case. A resolving DOI alone does not authorize a change.
- Separate incomplete bibliographic metadata findings from style discrepancies. Users can search for the intended source, open it, confirm identity, and approve a completed reference. An approved completed reference now survives plan regeneration and appears in the tracked document.
- Use the selected reference style or a clear author-year majority to suggest differences. Keep numeric author initials as written when the list is consistent. Harvard year syntax is parsed before calling a reference incomplete. Every style change requires approval.
- Preserve existing Word run formatting around tracked insertions and replacements. The free preview shows the table and figure totals and a capped sample of findings.
- Revise the landing-page feature announcement to describe the table/figure audit, approved citation case, and metadata completion.
- Synchronize both example environment files, keeping the 2.0.6 verification cache namespace and current Paystack/Stripe routing settings.

## Validation and limits

Focused and previous-release unit suites: 50 passed. Dashboard JavaScript safety checks passed. Python modules compiled. No live upload, provider, payment, or production deployment was tested.

Word files receive a structural caption/table check. PDF and plain-text uploads receive a text-only caption/callout check, which cannot establish the presence of every visual object. The audit does not validate numbers or claims inside the table or figure. Track Changes requires the original DOCX and an exact, unique user-approved passage; complex Word fields may be left for manual editing. Reanalyse old documents for these findings; saved historical results are not rewritten.

Deploy the web service and all active processing workers from the same package. Set `RELEASE_VERSION=2.0.7-manuscript-readiness` in both services. Retain `VERIFY_CACHE_NAMESPACE=v6-identity-safety`, existing scholarly API credentials, payment secrets, and callback/webhook routing. Check one DOCX with a cited caption, an unmentioned caption, an unmatched callout, an incomplete reference, and an approved tracked correction before promoting the release.
