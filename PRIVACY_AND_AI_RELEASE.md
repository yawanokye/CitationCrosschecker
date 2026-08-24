# CiteIntegrity privacy and academic-voice release

## Processing lifecycle

CiteIntegrity now supports a privacy-first `analyse → download → delete` flow.
The complete report package is generated in memory. When delete-after-download is
enabled, detailed manuscript content is removed from PostgreSQL, Redis and the
in-process store after the response is delivered. A startup expiry pass removes
completed or failed job content older than `CONTENT_TTL_SECONDS`.

The deletion pass removes uploaded or extracted manuscript text, citations,
references, claims, detailed verification results, voice-review passages,
correction plans, autofix data and generated document content. Minimal job,
transaction and entitlement metadata can remain.

## Academic Voice and Writing Signals

The module is optional and off by default. When a student enables it, the
initial review is explainable and rule based. It reports formulaic language,
long sentences, repeated openings and uniform rhythm while excluding likely
tables, equations, reference entries and statistical result statements. It does
not classify passages as human or AI written.

AI-assisted revision is optional and passage-level only. The user must select a
passage of no more than 500 words. The configured model is instructed to preserve
meaning, citations, numerical findings and evidence, and to return structured
JSON. It must not promise detector evasion.

## Production configuration

`DEMO_UNLOCK_ALL_FEATURES` now defaults to `false`. Enable it only on a separate
demonstration deployment. Configure `OPENAI_API_KEY` only if optional rewriting
will be offered. The citation engine and rule-based voice diagnostics do not
require an AI API.

## New endpoints

- `GET /api/privacy/{job_id}`
- `DELETE /api/privacy/{job_id}`
- `GET /api/report-package/{job_id}?delete_after=1`
- `GET /api/academic-voice/{job_id}`
- `POST /api/academic-voice/{job_id}/settings`
- `POST /api/academic-voice/rewrite`
- `POST /api/revision-compare`
- `POST /api/corrections/{job_id}/decision`
- `GET /developer/access`
- `GET/POST /api/developer/access`

## Submission-Ready Correction Pack

The results page now opens on the prioritized action plan. Every issue includes
an estimated page and section where recoverable, paragraph and sentence,
problem explanation, importance, supporting metadata, confidence, evidence
link, recommended action and an Accept, Reject or Ignore decision. `Fix next
issue` moves through undecided items one at a time.

The complete package contains exactly four user-facing files: an annotated
DOCX, a controlled Track Changes DOCX, an HTML submission-readiness report and
a CSV correction register. Internal JSON, code, manifests and debug outputs are
excluded. Only accepted changes and very high-confidence bibliographic
corrections are eligible for Track Changes.
Claims, new citations and source replacements are never applied silently.

## Temporary global open access

The protected developer page at `/developer/access` can switch the service
between payment-controlled access and temporary global Full Review. Temporary
access requires a number and a period of hours, days or weeks. The saved expiry
is checked whenever access is evaluated. Expired open access automatically
reverts to `payment_required`.

## Maintenance and developer testing access

The same protected developer page also provides a `maintenance` mode. The
developer specifies a check-back period in hours, days or weeks and may write a
short public notice. Public pages return a branded HTTP 503 maintenance page;
public API and processing requests return a structured HTTP 503 response with
the same notice and ISO check-back time.

Valid developer Basic authentication bypasses the maintenance gate, so the
developer can exercise every upgrade function on the live deployment. The
check-back time is an estimate and does not reopen the service automatically.
After testing, the developer must explicitly select payment-controlled or
temporary open access. The health endpoint remains available for deployment
monitoring, and document expiry/deletion controls continue operating.

Developer authentication responses preserve the `WWW-Authenticate` challenge,
so a browser visiting `/developer/access` opens its username and password prompt
instead of displaying a raw 401 JSON response.

After authentication, the developer page offers two signed, HTTP-only session
levels. `Full Access` opens the complete product testing experience. `Full
Review Unlocked` opens paid manuscript review results, correction outputs and
downloads without changing public payment access. Both work across the full
site during maintenance, expire after 12 hours by default and can be ended
immediately from the developer page.

The results interface suppresses all Free Preview, Full Review locked, Payment
required and payment-unlock banners during either developer testing level and
during temporary global open access. Ordinary unpaid public sessions continue
to see the commercial access panel.

Verification refreshes now merge new reference evidence into the current
dashboard without discarding the correction plan, autofix suggestions or the
selected results tab. Academic-voice citation heuristics recognise broader
author-year formats, inspect citation coverage at paragraph level and exclude
likely present-study results. These low-confidence writing signals stay in the
Academic Voice review and are no longer duplicated as critical correction-plan
items.

## Approval-driven tracked corrections

Citation-needed and missing-reference fixes can search Crossref and OpenAlex
for context-ranked candidates. Each candidate includes a direct evidence link,
metadata, relevance details, an author-year citation and a formatted reference.
The user must open, verify, select and approve a candidate before CiteIntegrity
may insert it as a tracked change. Missing references are classified as fixes
and approved full references are added through Track Changes.

Uncited references offer two explicit actions. The user may identify the exact
claim where the reference applies and approve a tracked citation insertion, or
confirm that the unused reference should be deleted with Track Changes. All
decisions, selected sources and operations are retained in the correction plan.
No source, claim change or deletion is applied silently.

AI remains optional. `/api/ai/status` and the developer console report whether
`OPENAI_API_KEY` is configured. The key must be stored as a secret server-side
environment variable and is used only for selected-passage academic rewriting.

## Evidence Resolution Workspace

The output now consolidates unsupported claims, incomplete mappings, weak or
unclear support, missing references, incomplete reference metadata, identity
conflicts and every unresolved online-verification result into one approval-led
workspace. Missing Citations and Uncited References remain permanently visible
as separate audit tabs because users still need their full lists at a glance.

Claim-source discovery first profiles the manuscript title, abstract,
objectives, questions, keywords, inferred discipline, geography, population,
setting and section headings. It then fingerprints each claim by population,
concepts or variables, claimed relationship, location, time period, evidence
type, nearby citations and current section. Candidate titles and abstracts are
compared field by field. A candidate with only broad keyword overlap, no usable
abstract, a mismatched relationship, or a missing required geographical scope
is withheld. Every displayed source has a direct link and must be opened and
explicitly confirmed before CiteIntegrity can add a citation, reference or
replacement to the Track Changes document. Context fit is never presented as
proof that the source supports the exact claim.

The improved Submission-Readiness Certificate records Evidence Resolution
progress, remaining critical and important actions, approved tracked
corrections, and whether the optional writing-signals module was enabled. Its
stamp says Review Recorded while unresolved evidence remains, avoiding a false
claim that every source was independently verified.

## Protected feature-usage statistics

The password-protected `/stats` dashboard now reports count-only use of the
Evidence Resolution Workspace, context-aware claim searches, reference-identity
searches, candidates returned or withheld by the context gate, correction
decisions, tracked approvals, Academic Voice opt-in, writing signals, rewrite
requests and approved revisions. Missing Citations and Uncited References remain
headline statistics.

Before detailed content is deleted or expires, CiteIntegrity snapshots only
these integer totals into `feature_usage`. Manuscript passages, search queries,
candidate titles, DOI values, author names and source metadata are excluded.
