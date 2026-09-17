# CiteIntegrity 2.0.6: integrity verification and summary cards

Prepared 17 September 2026. Deploy this release instead of 2.0.5.

## What changed

- Unicode personal names and corporate editorial authors retain their titles
  during reference parsing. This fixes the missing title queries for the
  moulage article and the PLOS publication notice in the submitted benchmark.
- An original article cannot be substituted with its correction, retraction,
  reinstatement or replacement notice solely because their titles overlap.
- Confirmed DOI identity and publication status remain separate. A metadata
  outage no longer removes a retraction found independently using the input DOI.
- Provider title markup is converted to plain text before matching and display.
- One bounded search path replaces repeated recovery passes. A strong,
  corroborated match stops further lookups. Provider failures stop repeated
  requests to that provider; they remain incomplete checks, not proof of absence.
- Numbered APA entries retain their title without the publisher or edition
  being included in the title search. Book editions, Unicode names and
  hyphenated author initials are preserved when formatting.
- The reference recovery parser accepts a short manuscript's explicit References
  heading even when the bibliography starts before 60% of the document.
- Current publication status, not severity, drives the retraction headline.
  Reinstatements take precedence over an older correction, while the full event
  history is retained. Duplicate API/snapshot representations of the same
  Retraction Watch record are collapsed.
- Summary cards show retractions/withdrawals, expressions of concern, corrected
  works, reinstated works, publication notices, other publication updates,
  references with no recorded event, and unchecked references.
- These counts are calculated server-side before the free preview is sampled.
  Safety counts and alerts remain free. Missing citations, uncited references,
  match rate and existing paid features retain their previous access rules.
- No checks completed means unknown/dashes, not a misleading zero all-clear.
  The cards show checked/total coverage and the dataset date.
- Evidence Resolution merges duplicate lookup/unchecked findings only when an
  equivalent item exists. Independent unchecked-status findings remain visible.
  Publication history is included in its supporting metadata.
- Instructions such as “Review reference manually” cannot become manuscript
  replacement text, including when exporting previously accepted legacy items.
- Source-risk IDs are stable. Reference-related fallback locations are labelled
  References. Printing no longer clips a table to its scrollable viewport.
- Versioned cache keys include the input text as well as DOI/style. Metadata
  failures are not cached for six hours; uncertain results have a short cache.

Automatic backend verification, early display of completed verification rows,
ACII withholding/capping, and the green announcement banner from 2.0.5 remain.
Ghana prices remain GHS 10 / 20 / 35 / 50. Nigeria and international prices and
the Paystack/Stripe routing logic are unchanged.

## Validation and limits

Run from the project directory:

```bash
python -m pip install -r requirements.txt -r requirements-test.txt
python -m pytest -q
node test_dashboard_safety.cjs
python -m compileall -q .
```

The Node check is an optional developer test, not a production dependency.

Local validation: 104 Python tests pass, dashboard JavaScript syntax and
safety-card DOM assertions pass, and Python compilation passes. Tests exercise
the actual scalar and batch verifier with **simulated provider metadata** plus
the bundled official publication-event index. They are not evidence of live
provider availability, search ranking, production timing or a successful Render
deployment. Direct live Crossref access timed out from the build workspace.

The supplied nursing DOCX parsed 20 references and 20 in-text citations.
The supplied US2DF DOCX parsed 26 references and 25 in-text citations, with
one uncited reference. Both retained titles and years without false
missing-field findings in the reference audit.

The controlled 20-reference benchmark, with and without supplied DOIs, yields:

| Current status | Reference count |
| --- | ---: |
| Retracted / withdrawn | 5 |
| Expression of concern | 1 |
| Corrected | 1 |
| Reinstated | 1 |
| Publication notice | 1 |
| Other update | 0 |
| No recorded event in checked snapshot | 11 |
| Unchecked | 0 |

Reference 5 retains three events. Reference 8 is reinstated, not retracted.
Reference 9 is a legitimate notice. With complete checked coverage, ACII is
Critical Review and at most 49. If live matching cannot confirm every reference,
unchecked counts remain visible and ACII must remain Withheld.

The snapshot is still dated **15 September 2026**, not refreshed or represented
as live data. Crossref says Retraction Watch updates each working day, and
corrections/expressions of concern are less comprehensive than retractions:
https://www.crossref.org/documentation/retrieve-metadata/retraction-watch/
Use the root-level `build_retraction_watch_index.py` to rebuild from a validated
official CSV before future releases. A DOI hit establishes identity, not
research quality or a misconduct finding.

## Render deployment

1. Back up the currently deployed release. Use the protected developer portal
   to enable maintenance or temporary open access while validating this release.
2. Deploy the web service and every ACTIVE processing worker from this same
   release. Do not mix an old worker with the new web service.
3. Set these values on the web service and active workers:

```dotenv
RELEASE_VERSION=2.0.6-integrity-summary
VERIFY_CACHE_NAMESPACE=v6-identity-safety
PUBLICATION_STATUS_CHECK_ENABLED=true
VERIFY_REQUEST_TIMEOUT=4
VERIFY_REFERENCE_BUDGET_SECONDS=20
VERIFY_REDIS_CACHE_ENABLED=1
VERIFY_REDIS_CACHE_TTL=21600
VERIFY_USE_CACHE=0
VERIFY_PARALLEL_MODE=1
VERIFY_PARALLEL_WORKERS=8
VERIFY_CHUNK_SIZE=40
```

Keep the configured `OPENALEX_API_KEY`, Crossref contact email, database/Redis
URLs and payment secrets. Never put secrets into the ZIP or share them in chat.
Leave `RETRACTION_WATCH_DB_PATH` unset unless it points to the correct snapshot.
The 20-second search budget is checked between bounded requests, not a promise
that a queued document finishes within 20 seconds.

4. At least one active worker must consume `verification`. The core start
   command is `python core_worker.py`; its default queues are
   `verification,document_processing,large_document_processing`.
   Worker 3 may stay suspended if the remaining workers cover those queues.
   Do not suspend another worker until its queues and live capacity are checked.
5. Submit NEW runs of both nursing manuscripts, one without DOIs and one with
   DOIs. Old saved reports are historical output and are not silently rewritten.
6. Confirm verification starts without clicking Verify References. Verify
   retraction findings and counts in the results page, Evidence Resolution,
   free preview, full review and exported reports. Ensure reinstatement is not
   styled as an active retraction.
7. Check a known-good document, an unavailable-provider test, and a genuine
   no-DOI reference. A lookup failure must not become a fabricated-source claim.
8. Test Paystack Ghana/Nigeria and Stripe in test mode, including repeated
   webhook delivery, before restoring paid access. Keep the central Paystack
   webhook at `https://projectreadyai.com/api/paystack/webhook`.

Do not tell the benchmark author that the deployed service has passed until
these live checks have been recorded. This ZIP is the corrected source release,
not a deployment or certification of the live site.
