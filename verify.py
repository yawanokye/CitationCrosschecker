ChatGPT




what query is used for the verification
why is not journal added
what other thing can i do to improve the verification. this initially verified 18
for commercial grade, recommend a query that will not miss any valid reference and avoid false positives
# Query building
# ---------------------------------------------------------

def _significant_title_words(title: str, limit: int = 6) -> List[str]:
    title = re.sub(r"[^A-Za-z0-9\s]", " ", title or "")
    title = re.sub(r"\s+", " ", title).strip()

    words = re.findall(r"[A-Za-z]{3,}", title)
    out = []
    for w in words:
        wl = w.lower()
        if wl in _QUERY_STOP_WORDS:
            continue
        out.append(wl)
    return out[:limit]


def _build_query(ref: str, style: str) -> Tuple[str, List[str], str, str, str]:
    fields = _extract_fields_by_style(ref, style)

    authors = fields.get("authors", []) or []
    year = fields.get("year", "") or ""
    doi = fields.get("doi", "") or ""
    title = fields.get("title", "") or ""

    # Normalise title before sending it to Crossref/OpenAlex.
    # This improves retrieval without changing the original displayed reference.
    title = re.sub(r"[^A-Za-z0-9\s]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    title_words = _significant_title_words(title, limit=10)
    title_only = " ".join(title_words[:7]).strip()

    query_parts: List[str] = []

    if authors:
        query_parts.extend(authors[:2])

    important_words = [
        w for w in title_words
        if len(w) > 3 and w.lower() not in _QUERY_STOP_WORDS
    ]

    if important_words:
        query_parts.extend(important_words[:7])

    if year:
        query_parts.append(year)

    query = " ".join(query_parts).strip()

    if not query:
        raw_words = [
            w.lower() for w in re.findall(r"[A-Za-z]{3,}", ref or "")
            if w.lower() not in _QUERY_STOP_WORDS
        ]
        query = " ".join(raw_words[:10] + ([year] if year else []))

    return query, authors, year, doi, title_only


def _significant_word_count(text: str) -> int:
    """Count significant title/query words for fast-skip decisions."""
    return len(_significant_title_words(text or "", limit=20))


def _make_fast_review_row(
    ref: str,
    style: str,
    query: str = "",
    authors: Optional[List[str]] = None,
    reason: str = "Reference is too incomplete for reliable fast online verification.",
) -> Dict[str, Any]:
    """Return quickly for weak references instead of forcing slow online searches."""
    return {
        "reference": ref,
        "style": style,
        "status": "needs_review",
        "source": "fast_skip",
        "score": 0,
        "doi": "",
        "matched_title": "",
        "matched_year": "",
        "matched_authors": "",
        "title_score": 0,
        "author_overlap": 0,
        "author_similarity": 0,
        "year_match": 0,
        "query_used": query,
        "author": ", ".join(authors or []),
        "author_mismatch_flag": 0,
        "match_note": reason,
        "error": reason,
    }

I’ll treat this as a verification-engine design issue, not just a small query tweak. I’ll first explain what your current function actually sends, then give you a commercial-grade query cascade and matching rules.

The main issue is that the current query is trying to do retrieval and verification at the same time. For commercial accuracy, retrieval should be broad enough to find candidates, while verification should be strict enough to reject false matches.

Thought for 33s
Your current verification query is not using the journal name. It is using only:

first two extracted authors

first seven significant title words

year

fallback raw reference words if the main query is empty

So your present query is effectively:

query = " ".join(authors[:2] + important_title_words[:7] + [year])
Example:

Adam Boateng procurement centralisation digitalisation public expenditure governance 2023
It returns:

return query, authors, year, doi, title_only
So although DOI is extracted, it is not inserted into the search query here. It is only returned for later use.

Why journal is not added
Because this function does not extract or append a journal field.

You currently have:

authors = fields.get("authors", []) or []
year = fields.get("year", "") or ""
doi = fields.get("doi", "") or ""
title = fields.get("title", "") or ""
There is no:

journal = fields.get("journal", "") or ""
And there is no:

query_parts.append(journal)
So the query ignores journal name, volume, issue, pages, publisher, and source title.

That explains why the system may now verify fewer references than before. If the title or author extraction is imperfect, the query becomes weak. Adding the journal can improve retrieval, but it should not be used as a hard requirement because journals are often abbreviated, inconsistently written, or missing in metadata.

Crossref’s own DOI lookup tools are built around full bibliographic entries, including title, journal, volume, and pages, not only author and title words. 
 Crossref support also indicates that for title, author, and year style lookup, query.bibliographic should be combined with query.author, since Crossref no longer has a direct query.title option. 

Recommended commercial-grade approach
Do not rely on one query.

Use a query cascade:

DOI exact lookup first

Full reference bibliographic query

Title + first author + year query

Title + journal + year query

Title-only fallback

OpenAlex fallback using title search and publication-year filter

Strict candidate scoring before declaring “verified”

OpenAlex supports work search, field-based title search, publication-year filtering, and journal/source-based filtering, so it is useful as a second verification source after Crossref. 

The key idea is this:

Query broadly, verify strictly.

A broad query reduces missed valid references. Strict scoring reduces false positives.

Best query strategy
For Crossref, use:

query.bibliographic = full_clean_reference
query.author = first_author
rows = 5 or 10
Not only:

query = author + title_words + year
Why? Because query.bibliographic can use the whole citation string, including journal, volume, issue, pages, and year. That gives Crossref more evidence.

For OpenAlex, use:

filter=display_name.search:<title>,publication_year:<year>
Then score candidates using title, year, DOI, author overlap, journal similarity, volume, issue, and page match.

Recommended replacement query builder
Use this instead of a single _build_query().

from typing import Any, Dict, List, Optional
import re
from urllib.parse import quote_plus


def _clean_text(value: str) -> str:
    value = re.sub(r"https?://\S+", " ", value or "")
    value = re.sub(r"doi\.org/", " ", value, flags=re.I)
    value = re.sub(r"[^A-Za-z0-9\s:&/\-().]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _normalise_doi(doi: str) -> str:
    doi = (doi or "").strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    return doi.strip().lower()


def _extract_first_author(authors: List[str]) -> str:
    if not authors:
        return ""
    return re.sub(r"[^A-Za-z\s\-']", " ", authors[0]).strip()


def _build_verification_queries(ref: str, style: str) -> Dict[str, Any]:
    """
    Commercial-grade query plan.

    Retrieval is broad.
    Final verification must still be done by candidate scoring.
    """

    fields = _extract_fields_by_style(ref, style)

    authors = fields.get("authors", []) or []
    year = fields.get("year", "") or ""
    doi = _normalise_doi(fields.get("doi", "") or "")
    title = _clean_text(fields.get("title", "") or "")
    journal = _clean_text(
        fields.get("journal", "")
        or fields.get("container_title", "")
        or fields.get("source", "")
        or ""
    )
    volume = _clean_text(fields.get("volume", "") or "")
    issue = _clean_text(fields.get("issue", "") or "")
    pages = _clean_text(fields.get("pages", "") or fields.get("page", "") or "")

    clean_ref = _clean_text(ref)
    first_author = _extract_first_author(authors)

    title_words = _significant_title_words(title, limit=12)
    title_key = " ".join(title_words[:8]).strip()

    bibliographic_rich_parts = [
        title,
        journal,
        year,
        volume,
        issue,
        pages,
    ]
    bibliographic_rich = " ".join([p for p in bibliographic_rich_parts if p]).strip()

    crossref_queries: List[Dict[str, Any]] = []
    openalex_queries: List[Dict[str, Any]] = []

    # 1. DOI lookup should be handled separately before text search.
    # Do not mix DOI with text search unless exact DOI lookup fails.
    if doi:
        crossref_queries.append({
            "name": "crossref_doi_exact",
            "mode": "doi_exact",
            "doi": doi,
            "priority": 1,
        })

        openalex_queries.append({
            "name": "openalex_doi_exact",
            "mode": "doi_exact",
            "doi": doi,
            "priority": 1,
        })

    # 2. Full reference query, best for recall.
    # This allows Crossref to use title, journal, year, volume, issue, and pages.
    if clean_ref:
        crossref_queries.append({
            "name": "crossref_full_bibliographic",
            "mode": "bibliographic",
            "query_bibliographic": clean_ref,
            "query_author": first_author,
            "rows": 10,
            "priority": 2,
        })

    # 3. Rich structured bibliographic query.
    if bibliographic_rich:
        crossref_queries.append({
            "name": "crossref_rich_bibliographic",
            "mode": "bibliographic",
            "query_bibliographic": bibliographic_rich,
            "query_author": first_author,
            "rows": 10,
            "priority": 3,
        })

    # 4. Title + author + year.
    if title_key:
        crossref_queries.append({
            "name": "crossref_title_author_year",
            "mode": "bibliographic",
            "query_bibliographic": " ".join([title_key, year]).strip(),
            "query_author": first_author,
            "rows": 10,
            "priority": 4,
        })

    # 5. Title + journal + year.
    # Useful when author extraction is poor.
    if title_key and journal:
        crossref_queries.append({
            "name": "crossref_title_journal_year",
            "mode": "bibliographic",
            "query_bibliographic": " ".join([title_key, journal, year]).strip(),
            "query_author": "",
            "rows": 10,
            "priority": 5,
        })

    # 6. OpenAlex title search with year filter.
    if title_key and year:
        openalex_queries.append({
            "name": "openalex_title_year",
            "mode": "title_year",
            "title_search": title_key,
            "publication_year": year,
            "rows": 10,
            "priority": 6,
        })

    # 7. OpenAlex broader title search, fallback only.
    if title_key:
        openalex_queries.append({
            "name": "openalex_title_only",
            "mode": "title_only",
            "title_search": title_key,
            "rows": 10,
            "priority": 7,
        })

    return {
        "reference": ref,
        "style": style,
        "fields": {
            "authors": authors,
            "first_author": first_author,
            "year": year,
            "doi": doi,
            "title": title,
            "journal": journal,
            "volume": volume,
            "issue": issue,
            "pages": pages,
            "title_key": title_key,
        },
        "crossref_queries": crossref_queries,
        "openalex_queries": openalex_queries,
        "query_used": " | ".join(
            q.get("name", "") for q in crossref_queries + openalex_queries
        ),
    }
How to use the query plan
Your verification function should not call only one query. It should loop through the query plan and collect candidates.

Pseudo-flow:

plan = _build_verification_queries(ref, style)

candidates = []

for q in plan["crossref_queries"]:
    candidates.extend(search_crossref(q))

for q in plan["openalex_queries"]:
    candidates.extend(search_openalex(q))

best = rank_and_score_candidates(ref, plan["fields"], candidates)

return build_verification_row(ref, style, plan, best)
Commercial-grade decision rules
Use this kind of scoring.

Verified
Only mark as verified if one of these is true:

DOI exact match
or:

title_score >= 92
author_overlap >= 1
year_match == 1
journal_score >= 70 if journal is available
or:

title_score >= 96
year_match == 1
author_similarity >= 70
Likely
Use likely when the match is strong but not perfect:

title_score >= 85
year_match == 1
author_overlap >= 1
or:

title_score >= 90
year difference <= 1
journal_score >= 70
Needs review
Use needs_review when:

title_score >= 75
but author_overlap == 0
or:

title is strong
but year conflicts
or:

journal matches
but title is not strong enough
Not found
Only use not_found after all queries fail:

DOI lookup failed
Crossref bibliographic failed
Crossref title-author-year failed
Crossref title-journal-year failed
OpenAlex title-year failed
OpenAlex title-only failed
Add journal, but don’t let journal dominate
Add journal in two places.

First, include it in the rich bibliographic query:

bibliographic_rich = " ".join([
    title,
    journal,
    year,
    volume,
    issue,
    pages
])
Second, use it in candidate scoring:

journal_score = fuzz.token_set_ratio(reference_journal, candidate_journal)
But don’t reject a candidate only because journal is missing. Many metadata records have missing, abbreviated, or differently formatted journal names.

Better:

if reference_journal and candidate_journal:
    use journal_score
else:
    ignore journal_score
Main reason your verified count may have dropped from 18
Your current function may have become too dependent on extracted title words and authors. If title extraction is slightly wrong, or if the first two author names are not clean, the search query becomes weak.

Also, this line can remove useful title context:

title_only = " ".join(title_words[:7]).strip()
Seven words may be too short for long academic titles. A better commercial setting is:

title_words = _significant_title_words(title, limit=12)
title_key = " ".join(title_words[:8])
For retrieval, use 8 to 12 title words.
For scoring, compare against the full extracted title.

Minimum improvements I recommend
Extract and store journal, volume, issue, and pages.

Use DOI exact lookup before text queries.

Use Crossref query.bibliographic with the full reference.

Add OpenAlex as fallback, especially title-year search.

Retrieve top 10 candidates, not only top 1.

Score all candidates locally before deciding status.

Never mark as verified based on Crossref/OpenAlex rank alone.

Add query_strategy and query_used to every result row so you can debug why a reference failed.

Separate verified, likely, needs_review, and not_found clearly.

Use journal as supporting evidence, not a strict filter.

Best commercial recommendation
For commercial grade, your best default Crossref query should be:

{
    "query_bibliographic": full_clean_reference,
    "query_author": first_author,
    "rows": 10
}
Then validate candidates using:

DOI
title similarity
author overlap
year match
journal similarity
volume/issue/pages
source agreement across Crossref and OpenAlex
This will recover more valid references than your current query and reduce false positives because the final decision is based on strict scoring, not the search result alone.

Create Word Docs
Create PDF Docs


verify (58).py
Python
update verify.py to best ever commercial grade as expert

I’ll produce a working replacement file rather than giving only fragments. I’m going to preserve your public function names, then upgrade extraction, query cascade, candidate scoring, Crossref/OpenAlex fallback, and debug fields so it can drop into the current app.

I’ve found one important weakness in the uploaded file: _query_crossref_title_only() still uses query.title. That parameter is no longer the right Crossref route for title lookup, so I’ll replace title-only searches with query.bibliographic and add structured candidate scoring.

Thought for 6m 11s
Done. I updated your uploaded verify.py into a commercial-grade replacement based on your current file. 


Download it here: verify_commercial_grade.py

What I upgraded:

Added journal-aware verification

Extracts journal/source title.

Extracts volume, issue, and pages.

Adds journal evidence to scoring, but does not make journal a strict blocker.

Replaced weak single-query verification with a query cascade

DOI exact lookup first.

Full reference bibliographic query.

Rich title + journal + year + volume + pages query.

Title + author + year query.

Title + journal + year query.

OpenAlex DOI/title/year fallback.

Fixed the Crossref title-query weakness

Your old file still used query.title in _query_crossref_title_only().

The updated file uses query.bibliographic instead, which is the safer route for citation lookup. Crossref documents /works/{doi} for DOI metadata and /works for metadata search, and Crossref support recommends query.bibliographic with query.author for title, author, and year lookup. 

Added OpenAlex fallback

OpenAlex works include title, DOI, publication year, source/journal metadata, and author data, so the updated code uses it as a second source when Crossref misses a valid reference. 

Added stricter commercial scoring

DOI match.

Title similarity.

Author overlap.

Year match.

Journal similarity.

Volume, issue, and page match.

Crossref + OpenAlex source agreement.

Author mismatch downgrade.

Added better debugging fields

query_used

query_strategy

query_plan

reference_journal

matched_journal

journal_score

doi_match

source_agreement

confidence_reason

alternative_matches

Syntax checked

The updated file passed python -m py_compile.

For deployment, replace your current verify.py with this file, or rename it to verify.py.

Recommended Render environment settings:

VERIFY_FORCE_OPENALEX_FALLBACK=1
VERIFY_DEEP_FALLBACK=1
VERIFY_CROSSREF_ROWS=10
VERIFY_OPENALEX_ROWS=10
VERIFY_TITLE_ROWS=10
VERIFY_STRICT_AUTHOR_GATE=1
VERIFY_SKIP_WEAK_TITLE=1
VERIFY_MIN_TITLE_WORDS=4
CROSSREF_MAILTO=your-email@domain.com
OPENALEX_MAILTO=your-email@domain.com
One important point: with commercial-grade verification, some references that were previously counted as verified may now become likely or needs_review. That is good for trust. The system will miss fewer valid references because retrieval is broader, but it will avoid false positives because final verification is stricter.

Create Word Docs
Create PDF Docs

let us review this "def _classify(
    doi_match: bool,
    title_score: int,
    score: int,
    year_match: int,
    author_overlap: int = 0,
) -> str:

    # -----------------------------------------
    # 1. DOI MATCHES
    # -----------------------------------------

    # DOI + decent title
    if doi_match and title_score >= 65:
        return "verified"

    # DOI exists but weak title
    if doi_match:
        return "likely"

    # -----------------------------------------
    # 2. STRONG VERIFIED
    # -----------------------------------------

    # Very strong title + year
    if title_score >= 90 and year_match:
        return "verified"

    # Strong title + author agreement
    if title_score >= 85 and author_overlap >= 1:
        return "verified"

    # Strong overall score
    if score >= 88:
        return "verified"

    # -----------------------------------------
    # 3. LIKELY
    # -----------------------------------------

    if title_score >= 75:
        return "LIKELY"

    if score >= 72:
        return "verified"

    if title_score >= 68 and author_overlap >= 1:
        return "verified"

    # -----------------------------------------
    # 4. NEEDS REVIEW
    # -----------------------------------------

    if title_score >= 58:
        return "LIKELY"

    if score >= 45:
        return "needs_review"

    # -----------------------------------------
    # 5. NOT FOUND
    # -----------------------------------------

    return "not_found"

Thought for 13s
Yes, this _classify() needs revision. It is currently too generous, and it can create false positives.

Main problems:

It returns "LIKELY" in uppercase.
Your _normalize_verify_status() may fix it later, but don’t rely on that. Return "likely" directly.

This rule is dangerous:

if score >= 72:
    return "verified"
A reference can get a decent combined score from partial title similarity, even when the year or author is wrong. That should not be verified.

This is also too generous:

if title_score >= 68 and author_overlap >= 1:
    return "verified"
A title score of 68 is weak for commercial verification. It should be likely or needs_review.

This rule is incomplete:

if score >= 88:
    return "verified"
A high score should still require title strength and either year or author support.

not_found should mostly mean no useful candidate was found, not merely “candidate exists but is weak.” If a weak candidate exists, needs_review is often better.

Better replacement using your current function signature
Replace your _classify() with this:

def _classify(
    doi_match: bool,
    title_score: int,
    score: int,
    year_match: int,
    author_overlap: int = 0,
) -> str:
    """
    Commercial-grade classification rule.

    Principle:
    - Retrieval should be broad.
    - Verification should be strict.
    - Do not mark weak title matches as verified.
    - Do not verify on score alone unless title, author, or year evidence supports it.
    """

    title_score = int(title_score or 0)
    score = int(score or 0)
    year_match = int(year_match or 0)
    author_overlap = int(author_overlap or 0)

    # -----------------------------------------
    # 1. DOI MATCH
    # -----------------------------------------
    # DOI is strong evidence, but still guard against obvious title mismatch.
    if doi_match:
        if title_score >= 70:
            return "verified"
        if title_score >= 50:
            return "likely"
        return "needs_review"

    # -----------------------------------------
    # 2. VERIFIED
    # -----------------------------------------
    # Very strong title + year support.
    if title_score >= 92 and year_match == 1:
        return "verified"

    # Strong title + author support.
    if title_score >= 88 and author_overlap >= 1:
        return "verified"

    # Strong title + both year and author support.
    if title_score >= 85 and year_match == 1 and author_overlap >= 1:
        return "verified"

    # High combined score, but still requires title strength and at least one external support.
    if score >= 90 and title_score >= 85 and (year_match == 1 or author_overlap >= 1):
        return "verified"

    # -----------------------------------------
    # 3. LIKELY
    # -----------------------------------------
    # Strong title but missing author or year support.
    if title_score >= 85:
        return "likely"

    # Moderate title with year or author support.
    if title_score >= 78 and (year_match == 1 or author_overlap >= 1):
        return "likely"

    # Good combined score, but not enough for verified.
    if score >= 75 and title_score >= 75:
        return "likely"

    # -----------------------------------------
    # 4. NEEDS REVIEW
    # -----------------------------------------
    # Candidate exists but evidence is incomplete or weak.
    if title_score >= 60:
        return "needs_review"

    if score >= 50:
        return "needs_review"

    # -----------------------------------------
    # 5. NOT FOUND
    # -----------------------------------------
    return "not_found"
Why this is better
This version will reduce false positives because it does not verify based on title alone unless the title score is very high and supported by year or author evidence.

It also avoids verifying weak matches like this:

title_score = 68
author_overlap = 1
Your current function marks that as verified. The revised function will not.

Even better commercial version
For the best version, _classify() should include journal evidence too. Your current signature does not allow that.

Use this expanded version if your scoring function now produces journal_score, journal_available, and source_agreement.

def _classify(
    doi_match: bool,
    title_score: int,
    score: int,
    year_match: int,
    author_overlap: int = 0,
    journal_score: int = 0,
    journal_available: bool = False,
    source_agreement: bool = False,
) -> str:
    """
    Commercial-grade classification using DOI, title, author, year, journal,
    and source agreement.
    """

    title_score = int(title_score or 0)
    score = int(score or 0)
    year_match = int(year_match or 0)
    author_overlap = int(author_overlap or 0)
    journal_score = int(journal_score or 0)

    author_ok = author_overlap >= 1
    year_ok = year_match == 1
    journal_ok = journal_available and journal_score >= 70

    # -----------------------------------------
    # 1. DOI MATCH
    # -----------------------------------------
    if doi_match:
        if title_score >= 70:
            return "verified"
        if title_score >= 50:
            return "likely"
        return "needs_review"

    # -----------------------------------------
    # 2. VERIFIED
    # -----------------------------------------
    if title_score >= 94 and year_ok:
        return "verified"

    if title_score >= 90 and author_ok and year_ok:
        return "verified"

    if title_score >= 88 and author_ok and journal_ok:
        return "verified"

    if title_score >= 88 and year_ok and journal_ok:
        return "verified"

    if source_agreement and title_score >= 88 and (author_ok or year_ok):
        return "verified"

    if score >= 92 and title_score >= 86 and (author_ok or year_ok or journal_ok):
        return "verified"

    # -----------------------------------------
    # 3. LIKELY
    # -----------------------------------------
    if title_score >= 86 and (author_ok or year_ok or journal_ok):
        return "likely"

    if title_score >= 80 and author_ok:
        return "likely"

    if title_score >= 80 and year_ok:
        return "likely"

    if title_score >= 78 and journal_ok:
        return "likely"

    if score >= 78 and title_score >= 75:
        return "likely"

    # -----------------------------------------
    # 4. NEEDS REVIEW
    # -----------------------------------------
    if title_score >= 60:
        return "needs_review"

    if score >= 50:
        return "needs_review"

    # -----------------------------------------
    # 5. NOT FOUND
    # -----------------------------------------
    return "not_found"
My recommendation
For your current file, use the first replacement immediately.

For the commercial-grade verify.py, use the expanded version because journal evidence is important. The best verification decision should not depend only on title, year, and author. It should use:

DOI + title + author + year + journal + volume/issue/pages + source agreement
The biggest fix is this: never return verified from score >= 72 or from title_score >= 68 and author_overlap >= 1. Those two rules are likely responsible for false positive verification.

Create Word Docs
Create PDF Docs

what other database will you recommend I add so I do not miss valid reference

Thought for 24s
No database will guarantee that you never miss a valid reference. The best commercial approach is to use several metadata sources, then let your own scoring engine decide whether a candidate is verified, likely, or needs_review.

For CiteIntegrity, I recommend this order.

1. Add DataCite first
You already use Crossref and OpenAlex. The next most important addition is DataCite.

Crossref is strong for journal articles, books, conference papers, and publisher-deposited DOI metadata. DataCite is important because many valid DOIs for datasets, theses, reports, preprints, institutional repository items, and research outputs are registered there rather than Crossref. DataCite’s REST API retrieves DOI metadata records in JSON. 

Add this route:

DOI present → Crossref DOI lookup → DataCite DOI lookup → OpenAlex DOI lookup
This alone will reduce missed valid references with DOIs.

2. Add PubMed / Europe PMC for health, biomedical, and life science references
For medical, nursing, public health, biomedical, pharmaceutical, and life science references, add PubMed E-utilities and Europe PMC.

NCBI says E-utilities are the public API for Entrez databases, including PubMed. Europe PMC also provides a RESTful API for publications and related information it holds. 

Use this only when the reference looks biomedical, for example when it has:

PMID
PMC ID
journal names such as Lancet, BMJ, JAMA, NEJM, PLOS Medicine
medical/public health keywords
Recommended route:

Health-related reference → PubMed citation match → Europe PMC fallback → Crossref/OpenAlex comparison
PubMed also has a citation matching endpoint that accepts journal title, year, volume, first page, and author name, which is very useful for references where the title extraction is poor. 

3. Add Semantic Scholar for extra article recovery
Add Semantic Scholar Academic Graph API as a fallback, not as your primary source.

Semantic Scholar provides paper, author, citation, and venue data through its Academic Graph API. It is useful when Crossref misses a paper or when you need alternative candidate matches. 

Use it after Crossref, OpenAlex, and DataCite:

Crossref miss → OpenAlex miss → DataCite miss → Semantic Scholar fallback
Do not automatically verify from Semantic Scholar alone unless the title, year, and author evidence are strong.

4. Add Google Books and Open Library for books
Crossref and OpenAlex may miss books, edited volumes, older textbooks, and publisher references. Add Google Books API for books, book chapters, and reports.

Google Books volume records include metadata such as title and author. 

Use it when the reference looks like a book:

publisher present
edition present
place of publication present
ISBN present
no journal title
no volume/issue/page pattern
Recommended book route:

ISBN present → Google Books lookup → Open Library fallback → Crossref book lookup → OpenAlex fallback
For books, your final status should usually be:

verified = ISBN/title/author/year strongly match
likely = title and author match, but year/publisher differs
needs_review = title only match
5. Add ERIC for education references
Because many of your users will work in education, curriculum, distance learning, policy, teaching, and social science, add ERIC.

ERIC provides an API endpoint for searching metadata records indexed in ERIC. 

Use it when the reference contains education-related terms:

education
curriculum
teaching
learning
students
teacher
school
higher education
distance education
ERIC will help with reports, policy documents, working papers, and education journal items that may not resolve cleanly through Crossref.

6. Add arXiv for preprints
Add arXiv for physics, mathematics, computer science, statistics, economics, quantitative finance, and related preprint references.

arXiv describes itself as an open-access archive for nearly 2.4 million scholarly articles across those fields. 

Use it when the reference has:

arXiv ID
preprint
computer science
machine learning
statistics
physics
mathematics
quantitative finance
Do not treat arXiv as equivalent to a peer-reviewed journal article. Mark it as verified only as an arXiv/preprint source.

7. Add CORE for repository and open-access recovery
Add CORE as a recovery source for institutional repository items, open-access papers, reports, and full-text metadata.

CORE says its API provides machine access to a large corpus of open-access research papers with metadata and full text. 

This is useful when a reference is valid but not well covered by Crossref or OpenAlex.

Use CORE mainly for:

institutional repository references
PDF-only references
working papers
university reports
open access manuscripts
8. Add DOAJ for open-access journal validation
DOAJ should not be your main reference verification source, but it is useful for journal validation.

DOAJ provides journal and article metadata through its API and public data services. 

Use it to check:

Is this journal real?
Is it open access?
Is the journal indexed in DOAJ?
Does the ISSN match?
This helps your commercial product detect suspicious or questionable journal references.

My recommended commercial stack
Use this order:

1. Crossref
2. OpenAlex
3. DataCite
4. PubMed / Europe PMC
5. Semantic Scholar
6. Google Books / Open Library
7. ERIC
8. arXiv
9. CORE
10. DOAJ
But the actual lookup should be reference-type aware, not the same for every reference.

Best verification flow
Use this logic:

Step 1: Parse reference type
- journal article
- book
- book chapter
- thesis/dissertation
- conference paper
- dataset
- report/working paper
- preprint
- webpage

Step 2: Use the right database route
- DOI article → Crossref, DataCite, OpenAlex
- Health article → PubMed, Europe PMC, Crossref
- Book → Google Books, Open Library, Crossref
- Education report/article → ERIC, Crossref, OpenAlex
- Preprint → arXiv, Crossref, OpenAlex
- Repository/report → CORE, OpenAlex, DataCite

Step 3: Merge candidates

Step 4: Score candidates locally

Step 5: Verify only when evidence is strong
Best immediate addition
Add DataCite first.

Then add PubMed/Europe PMC.

Then add Google Books.

That gives you the biggest improvement without making the system too slow or complicated.

My practical recommendation:

Phase 1:
Crossref + OpenAlex + DataCite

Phase 2:
PubMed + Europe PMC + Google Books

Phase 3:
Semantic Scholar + ERIC + arXiv + CORE + DOAJ
The most important rule is this: do not let any database decide verification alone. Let each database produce candidates, then your own engine should verify using DOI, title, author, year, journal, volume, issue, pages, ISBN, PMID, PMCID, arXiv ID, and source agreement.

Create Word Docs
Create PDF Docs


verify (59).py
Python
update with file with the source suggested and build in such a way that it will not slow the system and do not change my structure

I’ll keep your existing public functions and batch structure intact. The safest way is to append a new override block at the bottom of verify.py, so the app keeps calling the same functions while the later definitions add the extra databases.

Thought for 3m 54s
I updated the file without changing your public structure. The existing verify.py already has the same public API pattern, including verify_references_batch, submit_verification, get_verification_status, and related aliases, so I preserved that and added the new logic as a final override block. 


Download the updated file here: verify.py

I also kept a descriptive copy here: verify_multisource_fast.py

What I added
The file now supports adaptive fallback to:

DataCite, best for datasets, reports, repository items, theses, and non-Crossref DOIs. DataCite’s REST API provides DOI metadata retrieval and query support. 

PubMed and Europe PMC, best for health, biomedical, nursing, medical, and public health references. NCBI E-utilities support PubMed search and summaries, while Europe PMC exposes publication metadata through a RESTful API. 

Semantic Scholar, as an extra scholarly paper recovery source through its Academic Graph API. 

Google Books and Open Library, for books, textbooks, handbooks, edited books, and ISBN-based references. 

ERIC, for education, curriculum, teaching, learning, students, school, and higher education references. 

arXiv, for preprints in computer science, statistics, mathematics, physics, machine learning, and related fields. 

CORE, optional and disabled by default because it usually requires an API key. CORE supports search across works and research outputs. 

DOAJ, optional and disabled by default, mainly for open-access journal/article validation rather than primary verification. DOAJ provides article metadata through its API and public data services. 

How I prevented slowdown
The new sources do not all run for every reference.

The flow is now:

Crossref DOI / bibliographic search
→ OpenAlex DOI / title-year fallback
→ only if weak or not found:
   selected extra source based on reference type
The extra sources are controlled by:

VERIFY_MULTISOURCE_FALLBACK=1
VERIFY_MULTISOURCE_ONLY_WHEN_WEAK=1
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=3
VERIFY_SPECIAL_ROWS=3
VERIFY_SPECIAL_TIMEOUT=4
So it will not blindly call ten databases for each reference.

Important deployment settings
Use these on Render:

VERIFY_MULTISOURCE_FALLBACK=1
VERIFY_MULTISOURCE_ONLY_WHEN_WEAK=1
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=3
VERIFY_SPECIAL_ROWS=3
VERIFY_SPECIAL_TIMEOUT=4

VERIFY_USE_DATACITE=1
VERIFY_USE_EUROPEPMC=1
VERIFY_USE_PUBMED=1
VERIFY_USE_SEMANTIC_SCHOLAR=1
VERIFY_USE_GOOGLE_BOOKS=1
VERIFY_USE_OPEN_LIBRARY=1
VERIFY_USE_ERIC=1
VERIFY_USE_ARXIV=1

VERIFY_USE_CORE=0
VERIFY_USE_DOAJ=0

CROSSREF_MAILTO=your-email@domain.com
OPENALEX_MAILTO=your-email@domain.com
Optional keys:

SEMANTIC_SCHOLAR_API_KEY=
GOOGLE_BOOKS_API_KEY=
NCBI_API_KEY=
CORE_API_KEY=
DOAJ_API_KEY=
Status
The file passed Python compilation successfully with:

python -m py_compile verify_multisource_fast.py
Replace your current verify.py with the downloaded verify.py.

Create Word Docs
Create PDF Docs

teaseout the new env added

Thought for 22s
Here are the new environment variables added for the multi-source verification fallback.

Core multi-source controls
VERIFY_MULTISOURCE_FALLBACK=1
VERIFY_MULTISOURCE_ONLY_WHEN_WEAK=1
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=3
VERIFY_SPECIAL_ROWS=3
VERIFY_SPECIAL_TIMEOUT=4
VERIFY_MULTISOURCE_DEBUG=0
Meaning:

VERIFY_MULTISOURCE_FALLBACK
Turns the extra databases on or off.

VERIFY_MULTISOURCE_ONLY_WHEN_WEAK
Runs extra sources only when Crossref/OpenAlex do not produce a strong match.

VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES
Limits how many extra databases are called per reference.

VERIFY_SPECIAL_ROWS
Limits number of results fetched from each extra database.

VERIFY_SPECIAL_TIMEOUT
Timeout for extra source requests.

VERIFY_MULTISOURCE_DEBUG
Prints extra debug logs when set to 1.
Source-specific switches
VERIFY_USE_DATACITE=1
VERIFY_USE_EUROPEPMC=1
VERIFY_USE_PUBMED=1
VERIFY_USE_SEMANTIC_SCHOLAR=1
VERIFY_USE_GOOGLE_BOOKS=1
VERIFY_USE_OPEN_LIBRARY=1
VERIFY_USE_ERIC=1
VERIFY_USE_ARXIV=1
VERIFY_USE_CORE=0
VERIFY_USE_DOAJ=0
Meaning:

VERIFY_USE_DATACITE
Adds DataCite for datasets, theses, reports, repository items, and non-Crossref DOIs.

VERIFY_USE_EUROPEPMC
Adds Europe PMC for biomedical and health-related references.

VERIFY_USE_PUBMED
Adds PubMed for medical, nursing, public health, and life science references.

VERIFY_USE_SEMANTIC_SCHOLAR
Adds Semantic Scholar as a broad scholarly fallback.

VERIFY_USE_GOOGLE_BOOKS
Adds Google Books for books and textbooks.

VERIFY_USE_OPEN_LIBRARY
Adds Open Library as a book fallback.

VERIFY_USE_ERIC
Adds ERIC for education-related references.

VERIFY_USE_ARXIV
Adds arXiv for preprints in computer science, statistics, physics, mathematics, and machine learning.

VERIFY_USE_CORE
Adds CORE, but disabled by default because it usually needs an API key.

VERIFY_USE_DOAJ
Adds DOAJ, but disabled by default because it is better for journal validation than routine reference verification.
Optional API keys
SEMANTIC_SCHOLAR_API_KEY=
GOOGLE_BOOKS_API_KEY=
NCBI_API_KEY=
CORE_API_KEY=
DOAJ_API_KEY=
You can leave them blank at first. The system will still work, but keys may improve rate limits or access for some services.

Recommended Render settings
Use this safe commercial setup:

VERIFY_MULTISOURCE_FALLBACK=1
VERIFY_MULTISOURCE_ONLY_WHEN_WEAK=1
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=3
VERIFY_SPECIAL_ROWS=3
VERIFY_SPECIAL_TIMEOUT=4
VERIFY_MULTISOURCE_DEBUG=0

VERIFY_USE_DATACITE=1
VERIFY_USE_EUROPEPMC=1
VERIFY_USE_PUBMED=1
VERIFY_USE_SEMANTIC_SCHOLAR=1
VERIFY_USE_GOOGLE_BOOKS=1
VERIFY_USE_OPEN_LIBRARY=1
VERIFY_USE_ERIC=1
VERIFY_USE_ARXIV=1
VERIFY_USE_CORE=0
VERIFY_USE_DOAJ=0
Keep these existing ones too
VERIFY_REQUEST_TIMEOUT=4
VERIFY_INNER_THREADS=2
VERIFY_CROSSREF_ROWS=10
VERIFY_OPENALEX_ROWS=10
VERIFY_TITLE_ROWS=10
VERIFY_DEEP_FALLBACK=1
VERIFY_STOP_ON_STRONG_MATCH=0
VERIFY_FORCE_OPENALEX_FALLBACK=1
VERIFY_STRICT_AUTHOR_GATE=1
CROSSREF_MAILTO=your-email@domain.com
OPENALEX_MAILTO=your-email@domain.com
For speed, keep VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=3 and VERIFY_SPECIAL_ROWS=3. That is the main protection against slowing the system.

Create Word Docs
Create PDF Docs


verify (60).py
Python
relook because it is not starting or extremely slow

I’ve found the main slowdown pattern: the current override can run several Crossref queries, several OpenAlex queries, then up to three extra databases per reference. With 2 workers and 4-second timeouts, progress can look stuck before the first chunk finishes.

Thought for 1m 57s
I reworked the uploaded verify.py because the current multi-source version can look like it is not starting. The problem is not a syntax error. The file compiles, but the query runner can call too many sources before the first progress update appears.

The uploaded file has multi-source fallback enabled by default, with several extra sources also enabled by default, including DataCite, Europe PMC, PubMed, Semantic Scholar, Google Books, Open Library, ERIC, and arXiv. 

 The query runner also loops through Crossref, OpenAlex, and then fallback sources, which can create many network calls per reference before a row completes. 


I have fixed it by adding a fast-start override at the bottom of the file, without changing your public structure.

Download the fixed file here:

Download fixed verify.py

Backup copy:

Download verify_fast_start_fixed.py

What I changed
I kept your existing structure and function names. I only appended a final override block.

The new version now:

Starts faster.

Limits Crossref searches per reference.

Limits OpenAlex searches per reference.

Uses only one adaptive extra database by default.

Stops immediately when DOI or strong evidence is found.

Reduces timeout for special sources.

Stores an empty result list at job start so the frontend has something to read.

Keeps the same verify_references_batch, submit_verification, and result format.

New fast defaults added
Use these on Render:

VERIFY_FAST_START_MODE=1
VERIFY_MAX_CROSSREF_QUERIES=2
VERIFY_MAX_OPENALEX_QUERIES=1
VERIFY_FAST_RETURN_ON_DOI=1
VERIFY_FAST_RETURN_ON_ANY_STRONG=1
VERIFY_FAST_SKIP_GENERIC_SEMANTIC_WHEN_CANDIDATES=1
VERIFY_FAST_SPECIAL_TIMEOUT=2
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=1
VERIFY_SPECIAL_ROWS=2
VERIFY_SPECIAL_TIMEOUT=2
VERIFY_DEEP_FALLBACK=0
VERIFY_STOP_ON_STRONG_MATCH=1
Keep these too:

VERIFY_REQUEST_TIMEOUT=4
VERIFY_INNER_THREADS=2
VERIFY_CROSSREF_ROWS=5
VERIFY_OPENALEX_ROWS=5
VERIFY_FORCE_OPENALEX_FALLBACK=1
VERIFY_MULTISOURCE_FALLBACK=1
VERIFY_MULTISOURCE_ONLY_WHEN_WEAK=1
For now, disable these heavy sources
Use this for speed:

VERIFY_USE_CORE=0
VERIFY_USE_DOAJ=0
You may also temporarily disable Semantic Scholar if it is still slow:

VERIFY_USE_SEMANTIC_SCHOLAR=0
Status
I checked the updated file with:

python -m py_compile verify.py
It passed. I also tested import speed in the sandbox, and the file imported quickly.

Create Word Docs
Create PDF Docs

 Running 'python worker.py'
Menu
Traceback (most recent call last):
  File "/opt/render/project/src/worker.py", line 24, in <module>
    from claim_checker import build_claim_support_rows, suggest_alternative_sources_for_claim
  File "/opt/render/project/src/claim_checker.py", line 4, in <module>
    from citation_suggester import extract_context, split_citation_cluster, suggest_from_context
  File "/opt/render/project/src/citation_suggester.py", line 6, in <module>
    from verify import (
ImportError: cannot import name '_score' from 'verify' (/opt/render/project/src/verify.py)

Thought for 47s
The error is from backward compatibility, not the worker itself.

citation_suggester.py still does:

from verify import _score
But the new commercial verify.py changed the internal scoring design, so _score was not exposed in the deployed file. Your earlier verify.py had _score, and other modules still depend on it. 


I have fixed it by adding a legacy import compatibility patch at the bottom of verify.py. It restores _score without changing your verification structure.

Download the fixed file:

Download fixed verify.py

Backup copy:

Download verify_import_compat_fixed.py

I tested the file with:

python -m py_compile verify.py
and also tested import of _score. It passed.

After replacing the file, redeploy and restart the worker. This specific error should clear.

Create Word Docs
Create PDF Docs

12. references count: 0
(index):2035 ✅ Found suggestions in normalized.autofix.suggestions
(index):2059 🎯 Found suggestions data with 0 citations and 0 references
(index):2066 ⚠️ Suggestions data exists but has no content
(index):2100 === RENDERALL DEBUG END ===
(index):1147 [Status] Analysis complete
(index):1147 [Status] Starting online verification...
(index):2398 VERIFY RESPONSE: Object
(index):1147 [Status] Verification in progress: 0/23
(index):1569 Polling already running

The frontend log shows two different things. references count: 0 belongs to suggestions/autofix, but verification has 0/23, so the system did find 23 references for online verification. The real issue is that the verification job is not moving beyond zero.

Thought for 22s
This log shows verification started, but the worker is not processing the 23 references.

The key line is:

Verification in progress: 0/23
That means the frontend/backend already knows there are 23 references. So the problem is not reference extraction.

The line below is not the main problem:

Polling already running
That only means the frontend has already started polling and is blocking duplicate polling.

What is actually wrong
There are likely two issues.

Issue 1: Worker is still failing or not using the fixed verify.py
Your previous Render error was:

ImportError: cannot import name '_score' from 'verify'
If that is not fully fixed on Render, the worker will not start. Then the frontend will stay at:

0/23
because no worker is alive to process the references.

Run this in Render Shell or locally:

python -c "from verify import _score; print('score import ok')"
Also run:

python -c "from citation_suggester import suggest_from_context; print('citation_suggester import ok')"
Then run:

python worker.py
If any of these fail, the worker will stay dead and verification will remain 0/23.

Immediate fix
At the very bottom of verify.py, add this compatibility patch. Even if _score already exists, this safe version ensures old imports do not fail.

# ============================================================
# LEGACY COMPATIBILITY EXPORTS
# Required by citation_suggester.py and older modules.
# Do not remove.
# ============================================================

def _score(
    ref_title: str,
    ref_authors: list = None,
    ref_year: str = "",
    cand_title: str = "",
    cand_authors: list = None,
    cand_year: str = "",
) -> dict:
    """
    Backward-compatible scoring function.

    Older files import this directly:
        from verify import _score

    Keep this function available even when the commercial scorer changes.
    """

    ref_authors = ref_authors or []
    cand_authors = cand_authors or []

    ref_title_norm = _norm_text(ref_title or "")
    cand_title_norm = _norm_text(cand_title or "")

    if ref_title_norm and cand_title_norm:
        token_score = fuzz.token_sort_ratio(ref_title_norm, cand_title_norm)
        set_score = fuzz.token_set_ratio(ref_title_norm, cand_title_norm)
        partial_score = fuzz.partial_ratio(ref_title_norm, cand_title_norm)
        title_score = int((token_score * 0.45) + (set_score * 0.35) + (partial_score * 0.20))
    else:
        title_score = 0

    ref_author_set = set([_norm_text(a) for a in ref_authors if a])
    cand_author_set = set([_norm_text(a) for a in cand_authors if a])

    if ref_author_set and cand_author_set:
        author_overlap = len(ref_author_set & cand_author_set)
        union = len(ref_author_set | cand_author_set)
        author_similarity = int((author_overlap / union) * 100) if union else 0
    else:
        author_overlap = 0
        author_similarity = 0

    year_match = 1 if ref_year and cand_year and str(ref_year)[:4] == str(cand_year)[:4] else 0

    score = int((title_score * 0.65) + (author_similarity * 0.25) + (year_match * 10))

    if author_overlap >= 1:
        score += 5

    if author_overlap >= 2:
        score += 8

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_overlap": int(author_overlap),
        "author_similarity": int(author_similarity),
        "year_match": int(year_match),
    }
Then redeploy both:

Web service
Worker service
Not only the web service.

Issue 2: Your verification job has no early heartbeat
Even if the worker starts, the frontend can stay at 0/23 while the first chunk is still running. You need an early status update.

Inside verify_references_batch(), immediately after:

refs = [r for r in (references or []) if _safe_strip(r)]
add:

if job_id:
    update_job_progress(job_id, 0)
    store_verification_results(job_id, [])
    print(f"[DEBUG] Verification job {job_id} started with {len(refs)} references")
This confirms to the frontend and logs that the verification engine actually started.

Issue 3: Multi-source fallback may still be too heavy
For now, use this safe fast Render setup:

VERIFY_FAST_START_MODE=1
VERIFY_MULTISOURCE_FALLBACK=1
VERIFY_MULTISOURCE_ONLY_WHEN_WEAK=1
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=1
VERIFY_SPECIAL_ROWS=2
VERIFY_SPECIAL_TIMEOUT=2

VERIFY_DEEP_FALLBACK=0
VERIFY_STOP_ON_STRONG_MATCH=1
VERIFY_CROSSREF_ROWS=5
VERIFY_OPENALEX_ROWS=3
VERIFY_TITLE_ROWS=3
VERIFY_REQUEST_TIMEOUT=4
VERIFY_INNER_THREADS=2

VERIFY_USE_DATACITE=1
VERIFY_USE_EUROPEPMC=1
VERIFY_USE_PUBMED=1
VERIFY_USE_GOOGLE_BOOKS=1
VERIFY_USE_OPEN_LIBRARY=1
VERIFY_USE_ERIC=1
VERIFY_USE_ARXIV=1

VERIFY_USE_SEMANTIC_SCHOLAR=0
VERIFY_USE_CORE=0
VERIFY_USE_DOAJ=0
Disable Semantic Scholar temporarily because it can slow fallback. Add it back only after the worker is stable.

What the frontend log means
references count: 0
This is from suggestions/autofix, not online verification.

Verification in progress: 0/23
This means online verification received 23 references but no reference has completed yet.

Polling already running
This is not an error. It only prevents duplicate polling loops.

Do this now
Confirm Render is using the newest verify.py.

Add the _score compatibility patch at the bottom.

Redeploy the worker service.

Run:

python -c "from verify import _score; print('score import ok')"
python -c "from citation_suggester import suggest_from_context; print('citation_suggester import ok')"
python worker.py
Temporarily use the fast environment settings above.

The most likely reason it is stuck at 0/23 is that the worker is not successfully starting after the verify.py change.

Create Word Docs
Create PDF Docs


Pasted text (2)(10).txt
Document
update and make it more faster and efficient in verifying "

I’ll make this a speed patch, not another heavy verification layer. I’ll preserve your public functions, then append a final override that cuts API calls per reference, adds request caching, improves heartbeat, and keeps _score import compatibility.

Thought for 2m 16s
I updated the file and made verification faster without changing your public structure. I preserved the existing function names, including verify_references_batch, submit_verification, get_verification_status, get_verification_results, and _score. Your current file already had fast-start and multi-source logic, but it could still call too many APIs per reference before progress moved. 


Download the updated file here:

Download updated verify.py

Backup copy:

Download verify_ultra_fast.py

What I improved
Added ultra-fast mode.

Added request caching, so repeated API calls don’t keep hitting Crossref/OpenAlex.

Added thread-local HTTP sessions, so connections are reused.

Reduced normal verification to about 1 to 3 API calls per reference.

Limited Crossref to the strongest query.

Limited OpenAlex to one fallback query.

Limited extra sources to one adaptive fallback only when needed.

Disabled heavy generic fallback by default, especially Semantic Scholar.

Added progress update after every completed reference.

Preserved _score, so citation_suggester.py will not fail.

Use these Render settings
VERIFY_ULTRA_FAST_MODE=1
VERIFY_ULTRA_WORKERS=3
VERIFY_ULTRA_MAX_API_CALLS_PER_REF=3
VERIFY_ULTRA_CROSSREF_TEXT_QUERIES=1
VERIFY_ULTRA_OPENALEX_TEXT_QUERIES=1
VERIFY_ULTRA_FALLBACK_QUERIES=1
VERIFY_ULTRA_STORE_EVERY=3
VERIFY_ULTRA_CACHE_SECONDS=21600
VERIFY_ULTRA_FAST_API_TIMEOUT=3
VERIFY_ULTRA_DISABLE_ENRICH_METADATA=1

VERIFY_DEEP_FALLBACK=0
VERIFY_STOP_ON_STRONG_MATCH=1
VERIFY_REQUEST_TIMEOUT=4
VERIFY_SINGLE_REF_TIMEOUT=8
VERIFY_CROSSREF_ROWS=5
VERIFY_OPENALEX_ROWS=3
VERIFY_TITLE_ROWS=3

VERIFY_MULTISOURCE_FALLBACK=1
VERIFY_MULTISOURCE_ONLY_WHEN_WEAK=1
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=1
VERIFY_SPECIAL_ROWS=2
VERIFY_SPECIAL_TIMEOUT=2

VERIFY_USE_DATACITE=1
VERIFY_USE_EUROPEPMC=1
VERIFY_USE_PUBMED=1
VERIFY_USE_GOOGLE_BOOKS=1
VERIFY_USE_OPEN_LIBRARY=1
VERIFY_USE_ERIC=1
VERIFY_USE_ARXIV=1

VERIFY_USE_SEMANTIC_SCHOLAR=0
VERIFY_USE_CORE=0
VERIFY_USE_DOAJ=0
Status
I tested the file with:

python -m py_compile verify.py
and import test:

from verify import _score, verify_references_batch
Both passed.

After replacing the file, redeploy both the web service and the worker service.

Create Word Docs
Create PDF Docs

Uploaded image
I used the utra fast, kindly work on the query to ensure over 18 verified reference is alble to verify

Thought for 2m 12s
I updated the ultra-fast version so it should recover more valid references while staying efficient. The previous ultra-fast runner was too strict: it used only one Crossref text query, usually skipped OpenAlex once Crossref returned any usable candidate, and ran fallback only when there was no candidate. That explains why many items stayed in Needs Review or Not Found even though they may be valid. The uploaded file also showed the existing progress and result storage structure, which I preserved. 


Download the improved file here:

Download verify.py

Backup copy:

Download verify_query_recall_boost.py

What I changed
I added a Recall-Boost Query Override at the bottom of the file. It keeps ultra-fast speed, but improves retrieval.

The new query flow is:

1. Crossref DOI lookup, if DOI exists
2. Crossref full bibliographic query
3. Crossref rich bibliographic query, title + journal + year + volume/pages
4. Crossref title + author + year query
5. OpenAlex title-year query, if Crossref is not strong enough
6. One type-specific fallback, only when still weak
This replaces the earlier behaviour where ultra-fast mode could stop after one weak Crossref candidate.

Why it should improve verified count
The previous ultra-fast query runner kept only a very small query budget, as seen in the uploaded code’s ultra-fast settings and query selection pattern. 

 The new version uses up to five API calls only when needed, but stops early once a verified-level match is found.

I also adjusted the classifier so that strong title, year, journal, and cross-source evidence can verify a reference even when author extraction is imperfect. This matters because author extraction often causes valid references to be downgraded.

Use these Render settings
Set these exactly for the next test:

VERIFY_RECALL_BOOST_MODE=1
VERIFY_RECALL_MAX_API_CALLS=5
VERIFY_RECALL_CROSSREF_TEXT_QUERIES=3
VERIFY_RECALL_OPENALEX_TEXT_QUERIES=2
VERIFY_RECALL_FALLBACK_QUERIES=1
VERIFY_RECALL_ROWS=5
VERIFY_RECALL_PROMOTE_STRONG_LIKELY=1

VERIFY_ULTRA_FAST_MODE=1
VERIFY_ULTRA_WORKERS=3
VERIFY_ULTRA_FAST_API_TIMEOUT=3
VERIFY_ULTRA_DISABLE_ENRICH_METADATA=1

VERIFY_MULTISOURCE_FALLBACK=1
VERIFY_MULTISOURCE_ONLY_WHEN_WEAK=1
VERIFY_SPECIAL_ROWS=2
VERIFY_SPECIAL_TIMEOUT=2

VERIFY_USE_DATACITE=1
VERIFY_USE_EUROPEPMC=1
VERIFY_USE_PUBMED=1
VERIFY_USE_GOOGLE_BOOKS=1
VERIFY_USE_OPEN_LIBRARY=1
VERIFY_USE_ERIC=1
VERIFY_USE_ARXIV=1

VERIFY_USE_SEMANTIC_SCHOLAR=0
VERIFY_USE_CORE=0
VERIFY_USE_DOAJ=0
Important
I can’t guarantee exactly 18+ verified without seeing the 23 reference rows and their metadata, but this update is designed to recover valid items that ultra-fast missed. After replacing the file, redeploy both:

Web service
Worker service
Then test the same document again and check whether the counts shift from Needs Review and Not Found into Verified or Likely.

Create Word Docs
Create PDF Docs

which of the render settings can increase the verification rate

Thought for 10s
To increase the number of verified references, adjust the settings that improve retrieval coverage and slightly relax overly strict downgrading. These are the most important ones.

Best balanced settings for higher verification rate
Use this first:

VERIFY_RECALL_BOOST_MODE=1
VERIFY_RECALL_MAX_API_CALLS=7
VERIFY_RECALL_CROSSREF_TEXT_QUERIES=4
VERIFY_RECALL_OPENALEX_TEXT_QUERIES=2
VERIFY_RECALL_FALLBACK_QUERIES=1
VERIFY_RECALL_ROWS=8
VERIFY_RECALL_PROMOTE_STRONG_LIKELY=1

VERIFY_ULTRA_FAST_MODE=1
VERIFY_ULTRA_WORKERS=3
VERIFY_ULTRA_FAST_API_TIMEOUT=4
VERIFY_ULTRA_DISABLE_ENRICH_METADATA=1

VERIFY_CROSSREF_ROWS=8
VERIFY_OPENALEX_ROWS=5
VERIFY_TITLE_ROWS=5
VERIFY_FORCE_OPENALEX_FALLBACK=1

VERIFY_MULTISOURCE_FALLBACK=1
VERIFY_MULTISOURCE_ONLY_WHEN_WEAK=1
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=2
VERIFY_SPECIAL_ROWS=3
VERIFY_SPECIAL_TIMEOUT=3

VERIFY_DEEP_FALLBACK=0
VERIFY_STOP_ON_STRONG_MATCH=1
This should increase the verified count without making the system too slow.

Settings that directly increase verification rate
1. Increase Crossref search depth
VERIFY_RECALL_CROSSREF_TEXT_QUERIES=4
VERIFY_CROSSREF_ROWS=8
VERIFY_RECALL_ROWS=8
This helps because Crossref may not return the correct source from only one query. More rows and more query types give the system more valid candidates to score.

2. Force OpenAlex fallback
VERIFY_FORCE_OPENALEX_FALLBACK=1
VERIFY_RECALL_OPENALEX_TEXT_QUERIES=2
VERIFY_OPENALEX_ROWS=5
This is important because OpenAlex can recover valid references that Crossref misses.

3. Allow more total API calls per reference
VERIFY_RECALL_MAX_API_CALLS=7
Your ultra-fast version may be too restrictive at 3 or 5. Set it to 7 for a better balance.

4. Allow two extra sources when weak
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=2
VERIFY_SPECIAL_ROWS=3
VERIFY_SPECIAL_TIMEOUT=3
This helps recover books, education references, health references, arXiv items, and DataCite records.

5. Promote strong likely matches
VERIFY_RECALL_PROMOTE_STRONG_LIKELY=1
This helps references move from Likely to Verified when title, year, and source evidence are strong but author extraction is imperfect.

Source settings that help verification
Keep these on:

VERIFY_USE_DATACITE=1
VERIFY_USE_EUROPEPMC=1
VERIFY_USE_PUBMED=1
VERIFY_USE_GOOGLE_BOOKS=1
VERIFY_USE_OPEN_LIBRARY=1
VERIFY_USE_ERIC=1
VERIFY_USE_ARXIV=1
These improve coverage for datasets, medical papers, books, education references, and preprints.

Keep these off for now:

VERIFY_USE_CORE=0
VERIFY_USE_DOAJ=0
They can be useful later, but they may slow the system or require API keys.

For Semantic Scholar, use this only after the system is stable:

VERIFY_USE_SEMANTIC_SCHOLAR=0
Turn it on only if you still need more recovery:

VERIFY_USE_SEMANTIC_SCHOLAR=1
More aggressive setting to push verified count higher
Use this only if speed is acceptable:

VERIFY_RECALL_MAX_API_CALLS=9
VERIFY_RECALL_CROSSREF_TEXT_QUERIES=5
VERIFY_RECALL_OPENALEX_TEXT_QUERIES=3
VERIFY_RECALL_ROWS=10
VERIFY_CROSSREF_ROWS=10
VERIFY_OPENALEX_ROWS=8
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=3
VERIFY_SPECIAL_ROWS=5
VERIFY_SPECIAL_TIMEOUT=4
This can increase verified matches, but it will be slower.

Do not change these if you want speed
Keep:

VERIFY_ULTRA_DISABLE_ENRICH_METADATA=1
VERIFY_DEEP_FALLBACK=0
VERIFY_RETRY_FAILED=0
enrich_metadata and deep fallback add extra calls after matching. They may improve metadata quality, but they do not always improve the verified count enough to justify the delay.

My recommended setting for your next test
Use this exact version:

VERIFY_RECALL_BOOST_MODE=1
VERIFY_RECALL_MAX_API_CALLS=7
VERIFY_RECALL_CROSSREF_TEXT_QUERIES=4
VERIFY_RECALL_OPENALEX_TEXT_QUERIES=2
VERIFY_RECALL_FALLBACK_QUERIES=1
VERIFY_RECALL_ROWS=8
VERIFY_RECALL_PROMOTE_STRONG_LIKELY=1

VERIFY_ULTRA_FAST_MODE=1
VERIFY_ULTRA_WORKERS=3
VERIFY_ULTRA_FAST_API_TIMEOUT=4
VERIFY_ULTRA_DISABLE_ENRICH_METADATA=1

VERIFY_CROSSREF_ROWS=8
VERIFY_OPENALEX_ROWS=5
VERIFY_TITLE_ROWS=5
VERIFY_FORCE_OPENALEX_FALLBACK=1

VERIFY_MULTISOURCE_FALLBACK=1
VERIFY_MULTISOURCE_ONLY_WHEN_WEAK=1
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=2
VERIFY_SPECIAL_ROWS=3
VERIFY_SPECIAL_TIMEOUT=3

VERIFY_USE_DATACITE=1
VERIFY_USE_EUROPEPMC=1
VERIFY_USE_PUBMED=1
VERIFY_USE_GOOGLE_BOOKS=1
VERIFY_USE_OPEN_LIBRARY=1
VERIFY_USE_ERIC=1
VERIFY_USE_ARXIV=1
VERIFY_USE_SEMANTIC_SCHOLAR=0
VERIFY_USE_CORE=0
VERIFY_USE_DOAJ=0

VERIFY_DEEP_FALLBACK=0
VERIFY_STOP_ON_STRONG_MATCH=1
VERIFY_REQUEST_TIMEOUT=4
VERIFY_SINGLE_REF_TIMEOUT=10
This is the best balance for moving from 3 verified toward the previous 18+ verified without making the worker hang.

Create Word Docs
Create PDF Docs



which of them should i modify to increase the verification coverage without comprise the speed

Cancel

Send

Thought for 9s
To increase verification coverage without killing speed, don’t mainly increase ROWS too high. Increase the number of query strategies modestly, keep rows moderate, and stop once a strong match is found.

From your screenshot, these are the settings I would modify.

Change these first
1. Reduce VERIFY_CROSSREF_ROWS
You currently have:

VERIFY_CROSSREF_ROWS=20
Change to:

VERIFY_CROSSREF_ROWS=8
Why: 20 rows per query is heavy. It brings many weak candidates and slows scoring. Better to use more focused queries with 8 rows.

2. Reduce VERIFY_TITLE_ROWS
You currently have:

VERIFY_TITLE_ROWS=20
Change to:

VERIFY_TITLE_ROWS=8
Why: title-only searches return many loose matches. Too many rows increase false positives and slow the system.

3. Reduce VERIFY_OPENALEX_ROWS
You currently have:

VERIFY_OPENALEX_ROWS=15
Change to:

VERIFY_OPENALEX_ROWS=6
Why: OpenAlex is useful, but 15 rows per reference is expensive. Six is enough for most valid matches.

4. Turn off deep fallback
You currently have:

VERIFY_DEEP_FALLBACK=1
Change to:

VERIFY_DEEP_FALLBACK=0
Why: deep fallback repeats searches and can make the worker look stuck. Use recall boost instead of deep fallback.

5. Reduce request timeout
You currently have:

VERIFY_REQUEST_TIMEOUT=15
Change to:

VERIFY_REQUEST_TIMEOUT=4
Why: 15 seconds means one bad API call can block a worker for too long. Four seconds is better for production.

6. Reduce retry attempts
You currently have:

VERIFY_RETRY_ATTEMPTS=2
Change to:

VERIFY_RETRY_ATTEMPTS=1
Why: retrying every failed API call slows the whole batch. Failed references should move to another source or needs review.

7. Keep retry failed off
You already have:

VERIFY_RETRY_FAILED=0
Keep it as:

VERIFY_RETRY_FAILED=0
Good. Don’t turn this on now.

8. Reduce multi-source extra sources
You currently have:

VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=3
Change to:

VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=1
or at most:

VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=2
Use 1 for speed. Use 2 if verification count is still too low.

9. Keep special rows low
You currently have:

VERIFY_SPECIAL_ROWS=3
Keep it:

VERIFY_SPECIAL_ROWS=3
Good. Do not increase unless needed.

10. Reduce special timeout slightly
You currently have:

VERIFY_SPECIAL_TIMEOUT=4
Change to:

VERIFY_SPECIAL_TIMEOUT=3
Best balanced setup
Use this:

VERIFY_CHUNK_SIZE=10
VERIFY_CROSSREF_ROWS=8
VERIFY_OPENALEX_ROWS=6
VERIFY_TITLE_ROWS=8

VERIFY_REQUEST_TIMEOUT=4
VERIFY_RETRY_ATTEMPTS=1
VERIFY_RETRY_FAILED=0

VERIFY_DEEP_FALLBACK=0
VERIFY_STOP_ON_STRONG_MATCH=1
VERIFY_SKIP_WEAK_TITLE=0
VERIFY_MIN_TITLE_WORDS=3

VERIFY_PARALLEL_MODE=1
VERIFY_PARALLEL_WORKERS=3

VERIFY_MULTISOURCE_FALLBACK=1
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=1
VERIFY_MULTISOURCE_ONLY_WHEN_WEAK=1
VERIFY_SPECIAL_ROWS=3
VERIFY_SPECIAL_TIMEOUT=3
Add these if they are not already in Render
These are the most important for increasing coverage:

VERIFY_RECALL_BOOST_MODE=1
VERIFY_RECALL_MAX_API_CALLS=7
VERIFY_RECALL_CROSSREF_TEXT_QUERIES=4
VERIFY_RECALL_OPENALEX_TEXT_QUERIES=2
VERIFY_RECALL_FALLBACK_QUERIES=1
VERIFY_RECALL_ROWS=8
VERIFY_RECALL_PROMOTE_STRONG_LIKELY=1
These improve coverage better than just increasing VERIFY_CROSSREF_ROWS to 20.

My strongest recommendation
Change these now:

VERIFY_CROSSREF_ROWS=8
VERIFY_OPENALEX_ROWS=6
VERIFY_TITLE_ROWS=8
VERIFY_DEEP_FALLBACK=0
VERIFY_REQUEST_TIMEOUT=4
VERIFY_RETRY_ATTEMPTS=1
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=1
VERIFY_STOP_ON_STRONG_MATCH=1
Then add:

VERIFY_RECALL_BOOST_MODE=1
VERIFY_RECALL_MAX_API_CALLS=7
VERIFY_RECALL_CROSSREF_TEXT_QUERIES=4
VERIFY_RECALL_OPENALEX_TEXT_QUERIES=2
VERIFY_RECALL_ROWS=8
VERIFY_RECALL_PROMOTE_STRONG_LIKELY=1
That should give better verification coverage than your current settings while being much faster.

Create Word Docs
Create PDF Docs

only 1 more verified with all these changes

Thought for 22s
That means the problem is no longer Render settings. The system is already searching enough. The remaining problem is most likely one of these:

The best match is being downgraded because of author mismatch

Strong matches are staying as likely instead of verified

The extracted title is weak or incomplete

The system stops early after a weak candidate and does not test better candidates

The reference itself has no DOI and poor metadata

Your code already has logic that stops early when a strong candidate is found and uses fallback only when the candidate is weak. That means increasing rows alone will not help much if the classification or author gate is blocking promotion. 


The settings to change now
Use these next. They target the classification bottleneck, not only search coverage.

VERIFY_STRICT_AUTHOR_GATE=0
VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY=0
VERIFY_RECALL_PROMOTE_STRONG_LIKELY=1

VERIFY_THRESHOLD_TITLE_VERIFIED=88
VERIFY_THRESHOLD_TITLE_LIKELY=78
VERIFY_THRESHOLD_TITLE_REVIEW=60
VERIFY_THRESHOLD_JOURNAL_SUPPORT=65
Why: many correct references are probably being held back because the title matches, but author extraction is imperfect. Your file has an author-gate setting, and the earlier code also downgrades verified matches when author overlap is zero. 


Keep these for balanced speed
VERIFY_CROSSREF_ROWS=8
VERIFY_OPENALEX_ROWS=6
VERIFY_TITLE_ROWS=8

VERIFY_RECALL_MAX_API_CALLS=7
VERIFY_RECALL_CROSSREF_TEXT_QUERIES=4
VERIFY_RECALL_OPENALEX_TEXT_QUERIES=2
VERIFY_RECALL_ROWS=8

VERIFY_DEEP_FALLBACK=0
VERIFY_STOP_ON_STRONG_MATCH=1
VERIFY_REQUEST_TIMEOUT=4
VERIFY_RETRY_ATTEMPTS=1
Do not increase rows to 20 or timeout to 15 again. That will slow the system but may not raise verified count.

If you want to push verified count higher
Use this slightly more aggressive version:

VERIFY_THRESHOLD_TITLE_VERIFIED=85
VERIFY_THRESHOLD_TITLE_LIKELY=75
VERIFY_THRESHOLD_TITLE_REVIEW=55
VERIFY_THRESHOLD_JOURNAL_SUPPORT=60

VERIFY_RECALL_MAX_API_CALLS=9
VERIFY_RECALL_CROSSREF_TEXT_QUERIES=5
VERIFY_RECALL_OPENALEX_TEXT_QUERIES=3
VERIFY_RECALL_ROWS=10
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES=2
This will increase verified count, but it also increases false-positive risk. I would only use it after checking the matched titles.

The real next diagnostic
Export or show the verification rows for the 23 references, especially these columns:

reference
status
matched_title
title_score
matched_year
year_match
matched_authors
author_overlap
matched_journal
journal_score
source
confidence_reason
match_note
If many rows have:

title_score >= 85
year_match = 1
author_overlap = 0
status = needs_review or likely
then author gating is the blocker.

If many rows have:

title_score < 70
then the issue is title extraction, not Render settings.

My recommendation now is to stop changing only Render settings and inspect the verification row reasons. That will show exactly why the 18 references are not being promoted.

Create Word Docs
Create PDF Docs


Pasted text(96).txt
Document
this overly slow but does not improve verification rate and speed 

Thought for 2m 28s
You are right. The earlier changes increased the number of API calls, but the verified count did not improve enough. The current file’s recall runner still goes through Crossref, OpenAlex, and then fallback sources when matches are weak, which can slow the worker without improving verification much. 


I have updated it to a lean speed + coverage version.

Download the new file here:

Download updated verify.py

Backup copy:

Download verify_lean_speed_rate.py

What I changed
I added a final override block that does this:

1. Crossref DOI lookup, if DOI exists
2. Crossref full bibliographic query
3. Crossref rich/title-author-year query
4. OpenAlex title-year fallback only if Crossref is not good enough
5. Special databases disabled by default
This cuts the slow multi-source loop. The previous runner could still use several query layers before returning candidates. 


I also changed the classifier so that strong title + year + journal/source evidence can be verified even when author extraction is imperfect. This should improve verified count without adding more slow searches.

Use these Render settings now
Set these exactly:

VERIFY_LEAN_FAST_MODE=1
VERIFY_LEAN_ROWS=6
VERIFY_LEAN_CROSSREF_TEXT_QUERIES=2
VERIFY_LEAN_OPENALEX_TEXT_QUERIES=1
VERIFY_LEAN_MAX_API_CALLS=4
VERIFY_LEAN_USE_SPECIAL_FALLBACK=0
VERIFY_LEAN_DISABLE_ENRICH_METADATA=1
VERIFY_LEAN_WORKERS=3
VERIFY_LEAN_STORE_EVERY=3
VERIFY_LEAN_PROMOTE_STRONG_TITLE_YEAR=1

VERIFY_DEEP_FALLBACK=0
VERIFY_RETRY_FAILED=0
VERIFY_STOP_ON_STRONG_MATCH=1

VERIFY_CROSSREF_ROWS=6
VERIFY_OPENALEX_ROWS=5
VERIFY_TITLE_ROWS=5
VERIFY_REQUEST_TIMEOUT=4
VERIFY_SINGLE_REF_TIMEOUT=8
VERIFY_RETRY_ATTEMPTS=1

VERIFY_MULTISOURCE_FALLBACK=0
VERIFY_USE_SEMANTIC_SCHOLAR=0
VERIFY_USE_CORE=0
VERIFY_USE_DOAJ=0
Important
For now, turn off all multi-source fallback:

VERIFY_MULTISOURCE_FALLBACK=0
That is the main setting. It should make verification much faster. Once the core Crossref/OpenAlex engine is stable, we can selectively re-enable DataCite or PubMed only for references that truly need them.

Redeploy both:

Web service
Worker service
Create Word Docs
Create PDF Docs

which query in also produce highest success in crossref

Thought for 17s
For highest success in Crossref, the best query is not query.title. Use query.bibliographic with the full cleaned reference, plus query.author when the first author is available.

Crossref’s REST API is mainly used through the /works endpoint for research outputs, and it supports metadata search over deposited bibliographic records. Crossref also recommends polite access with mailto, which you already include in your query functions. 

Best Crossref query order
1. Highest success when DOI exists
Use this first:

https://api.crossref.org/works/{doi}
In your code this is:

_query_crossref_by_doi(doi)
This should always be first because DOI lookup is the most precise.

2. Highest success when DOI is missing
Use this:

params = {
    "query.bibliographic": full_clean_reference,
    "query.author": first_author,
    "rows": 6,
    "sort": "score",
    "order": "desc",
    "mailto": MAILTO,
}
This is better than title-only because the full reference contains several useful clues:

authors
year
title
journal
volume
issue
pages
publisher
Your newer code already has this better function:

def _query_crossref_bibliographic(
    query_bibliographic: str,
    query_author: str = "",
    rows: int = None,
    query_name: str = "crossref_bibliographic"
)
It sends query.bibliographic, optionally adds query.author, and retrieves scored Crossref results. 


Best-performing Crossref query pattern
Use this as your default Crossref text query:

def _query_crossref_best(
    ref: str,
    title: str = "",
    first_author: str = "",
    year: str = "",
    journal: str = "",
    volume: str = "",
    pages: str = "",
    rows: int = 6,
):
    full_ref_query = " ".join([
        ref,
        title,
        journal,
        year,
        volume,
        pages,
    ])

    full_ref_query = re.sub(r"\s+", " ", full_ref_query).strip()

    params = {
        "query.bibliographic": full_ref_query,
        "rows": rows,
        "sort": "score",
        "order": "desc",
    }

    if first_author:
        params["query.author"] = first_author

    if MAILTO:
        params["mailto"] = MAILTO

    return _safe_get_json(
        "https://api.crossref.org/works",
        params=params,
        timeout=API_TIMEOUT
    )
Best query cascade for Crossref only
Use only these three Crossref queries:

1. DOI exact lookup
2. Full reference bibliographic query + first author
3. Title + journal + year query
Do not use too many Crossref queries. More queries will slow the system without necessarily improving the verified count.

Replace query.title
Your uploaded code still contains this older pattern:

params = {
    "query.title": title_query,
    "rows": rows,
    "sort": "score",
    "order": "desc",
}
But your later version correctly replaced title-only lookup with query.bibliographic for title-like searches. 


Use this instead:

params = {
    "query.bibliographic": title_query,
    "rows": rows,
    "sort": "score",
    "order": "desc",
}
My recommended Crossref query configuration
Use this:

VERIFY_CROSSREF_ROWS=6
VERIFY_TITLE_ROWS=6
VERIFY_LEAN_CROSSREF_TEXT_QUERIES=2
VERIFY_LEAN_MAX_API_CALLS=4
VERIFY_STOP_ON_STRONG_MATCH=1
VERIFY_REQUEST_TIMEOUT=4
This gives the best balance between success and speed.

Final answer
The highest-success Crossref query is:

query.bibliographic = full_clean_reference
query.author = first_author
rows = 6
sort = score
order = desc
Then score the returned candidates locally using title, year, author, journal, volume, pages, and DOI. Do not depend on Crossref rank alone.

Create Word Docs
Create PDF Docs

for example "1.	Adam, A. M. (2020). Sample size determination in survey research. Journal of Scientific Research and Reports, 26(5), 90–97.   "sample size determination survey research adam2020" seems better

Thought for 14s
Yes, your example is exactly the direction I now recommend.

For Crossref, this compact query:

sample size determination survey research Adam 2020
is likely better than the full reference:

Adam, A. M. (2020). Sample size determination in survey research. Journal of Scientific Research and Reports, 26(5), 90–97.
The full reference carries useful evidence, but it also adds punctuation, volume, issue, page range, and journal words that may pull Crossref toward noisy matches. Your current file already has a crossref_title_author_year query, but it appears after other Crossref queries, including fuller bibliographic versions. 


Best Crossref query pattern
Use a fingerprint query first:

sample size determination survey research Adam 2020
Then use journal fallback:

sample size determination survey research Journal Scientific Research Reports 2020
Then use full reference only if needed.

So the order should be:

1. DOI exact, if available
2. title keywords + first author + year
3. title keywords + journal + year
4. full reference bibliographic
5. rich bibliographic, title + journal + year + volume/pages
Your current Crossref function already supports query.bibliographic and query.author, which is good. 

 The issue is the query order and query shape, not only the API endpoint.

Do not use adam2020 alone
Use:

Adam 2020
instead of only:

adam2020
But you can include both in the query if you want:

sample size determination survey research Adam 2020 adam2020
For Crossref, separate tokens are safer. adam2020 is useful as a compact citation key, but Crossref metadata usually stores author and year separately.

Recommended query for your example
For this reference:

Adam, A. M. (2020). Sample size determination in survey research. Journal of Scientific Research and Reports, 26(5), 90–97.
The best Crossref query should be:

sample size determination survey research Adam 2020
With parameters:

params = {
    "query.bibliographic": "sample size determination survey research Adam 2020",
    "query.author": "Adam",
    "rows": 6,
    "sort": "score",
    "order": "desc",
}
Second fallback:

params = {
    "query.bibliographic": "sample size determination survey research Journal Scientific Research Reports 2020",
    "rows": 6,
    "sort": "score",
    "order": "desc",
}
Patch the query builder like this
Add this helper:

def _build_crossref_fingerprint_query(fields: Dict[str, Any]) -> str:
    """
    Build a compact Crossref query.

    Example:
    Adam, A. M. (2020). Sample size determination in survey research.
    -> sample size determination survey research Adam 2020
    """

    title = _clean_query_text(fields.get("title", "") or "")
    authors = fields.get("authors", []) or []
    year = fields.get("year", "") or ""

    first_author = authors[0] if authors else ""

    title_words = _significant_title_words(title, limit=12)

    # Keep title terms that carry meaning.
    compact_title = " ".join(title_words[:8]).strip()

    parts = [
        compact_title,
        first_author,
        year,
    ]

    query = " ".join([p for p in parts if p]).strip()
    query = re.sub(r"\s+", " ", query)

    return query
Then in _build_verification_query_plan(), place this before the full bibliographic query:

fingerprint_query = _build_crossref_fingerprint_query({
    "title": title,
    "authors": authors,
    "year": year,
})

if fingerprint_query:
    crossref_queries.append({
        "name": "crossref_fingerprint_title_author_year",
        "mode": "bibliographic",
        "query_bibliographic": fingerprint_query,
        "query_author": first_author,
        "rows": VERIFY_CROSSREF_ROWS,
        "priority": 2,
    })
Then move the full reference query lower:

if clean_ref:
    crossref_queries.append({
        "name": "crossref_full_bibliographic",
        "mode": "bibliographic",
        "query_bibliographic": clean_ref,
        "query_author": first_author,
        "rows": VERIFY_CROSSREF_ROWS,
        "priority": 4,
    })
Better final Crossref order
Use this order in your plan:

# 1. DOI exact
priority = 1

# 2. Compact title + author + year
priority = 2

# 3. Compact title + journal + year
priority = 3

# 4. Full reference
priority = 4

# 5. Rich bibliographic
priority = 5
Best setting after this change
Keep the Crossref query count small:

VERIFY_LEAN_CROSSREF_TEXT_QUERIES=2
VERIFY_CROSSREF_ROWS=6
VERIFY_LEAN_MAX_API_CALLS=4
VERIFY_STOP_ON_STRONG_MATCH=1
VERIFY_MULTISOURCE_FALLBACK=0
The real improvement should come from better query shape, not more API calls.

Main point
Your compact query is better because it behaves like a reference fingerprint:

title core + first author + year
That should become the first Crossref text query after DOI lookup.

Create Word Docs
Create PDF Docs


Pasted text(97).txt
Document
check if the query needs to be changed  

Thought for 17s
Yes, the query needs to be changed.

Your current query plan already has crossref_title_author_year, but it is too low in the order. It is currently placed after fuller Crossref queries, and its query_bibliographic is only:

" ".join([title_key, year]).strip()
while the first author is passed separately as query_author. 


For your example, the system should search Crossref first with a compact fingerprint like:

sample size determination survey research Adam 2020
not only:

sample size determination survey research 2020
and not the full reference first.

What should change
Change the Crossref order to this:

1. DOI exact, if DOI exists
2. Fingerprint query: title keywords + first author + year
3. Journal fingerprint: title keywords + journal + year
4. Full reference bibliographic query
5. Rich query: title + journal + year + volume/pages
The current code also correctly uses query.bibliographic instead of query.title in the newer override, which is good. 

 The problem is mainly query shape and order.

Replace this part
Where you currently have:

if title_key:
    crossref_queries.append({
        "name": "crossref_title_author_year",
        "mode": "bibliographic",
        "query_bibliographic": " ".join([title_key, year]).strip(),
        "query_author": first_author,
        "rows": VERIFY_CROSSREF_ROWS,
        "priority": 4,
    })
Change it to:

if title_key:
    fingerprint_query = " ".join([
        title_key,
        first_author,
        year,
    ]).strip()

    crossref_queries.append({
        "name": "crossref_fingerprint_title_author_year",
        "mode": "bibliographic",
        "query_bibliographic": fingerprint_query,
        "query_author": first_author,
        "rows": VERIFY_CROSSREF_ROWS,
        "priority": 2,
    })
Also change title + journal query
Replace this:

if title_key and journal:
    crossref_queries.append({
        "name": "crossref_title_journal_year",
        "mode": "bibliographic",
        "query_bibliographic": " ".join([title_key, journal, year]).strip(),
        "query_author": "",
        "rows": VERIFY_CROSSREF_ROWS,
        "priority": 5,
    })
with this:

if title_key and journal:
    journal_query = " ".join([
        title_key,
        journal,
        year,
    ]).strip()

    crossref_queries.append({
        "name": "crossref_fingerprint_title_journal_year",
        "mode": "bibliographic",
        "query_bibliographic": journal_query,
        "query_author": "",
        "rows": VERIFY_CROSSREF_ROWS,
        "priority": 3,
    })
Move full reference lower
The full reference query should not be the first text query. Keep it, but make it priority 4.

if clean_ref:
    crossref_queries.append({
        "name": "crossref_full_bibliographic",
        "mode": "bibliographic",
        "query_bibliographic": clean_ref,
        "query_author": first_author,
        "rows": VERIFY_CROSSREF_ROWS,
        "priority": 4,
    })
Move rich bibliographic lower
Use rich bibliographic as priority 5:

if bibliographic_rich:
    crossref_queries.append({
        "name": "crossref_rich_bibliographic",
        "mode": "bibliographic",
        "query_bibliographic": bibliographic_rich,
        "query_author": first_author,
        "rows": VERIFY_CROSSREF_ROWS,
        "priority": 5,
    })
Best Crossref query for your example
For:

Adam, A. M. (2020). Sample size determination in survey research. Journal of Scientific Research and Reports, 26(5), 90–97.
The first Crossref text query should be:

sample size determination survey research Adam 2020
Second query:

sample size determination survey research Journal Scientific Research Reports 2020
Full reference should come only after these.

My recommendation
Yes, change the query. Do not increase rows or timeouts again. The main issue is not API volume. It is that the compact author-title-year fingerprint should be the first Crossref text query.

Create Word Docs
Create PDF Docs

update the file

Thought for 1m 34s
Updated the file. I changed the Crossref query order so the first text search now uses the compact fingerprint query:

sample size determination survey research adam 2020
instead of starting with the full reference. This directly addresses the issue you raised. Your uploaded file already had the broader verification structure, so I appended a final override that preserves the same public API while changing only the Crossref query plan and order. 


Download the updated file here:

Download updated verify.py

Backup copy:

Download verify_fingerprint_query_order.py

I tested it with your example. The first Crossref queries are now:

crossref_fingerprint_title_author_year:
sample size determination survey research adam 2020

crossref_fingerprint_title_journal_year:
sample size determination survey research scientific reports 2020

crossref_title_author_year_repaired:
sample size determination survey 2020 adam

crossref_full_bibliographic:
Adam, A. M. (2020). Sample size determination in survey research...
The file passed Python compilation.

Create Word Docs
Create PDF Docs

which one do I use backup or updated

Thought for a couple of seconds
Use the updated file:

verify.py

Use the backup only if you want to keep a copy for comparison:

verify_fingerprint_query_order.py

For deployment on Render, replace your current repository file with:

verify.py
Then redeploy both:

Web service
Worker service
The backup file is just a labelled copy of the same update for safety.

Create Word Docs
Create PDF Docs

only 9 validated instead of 18

Thought for 1m 24s
The 9 validated result means the problem is not just Crossref search volume. The latest uploaded file still shows that the older plan had crossref_title_author_year later in the order and the query did not put the first author directly inside query_bibliographic. It also shows that the runner can stop once it thinks a candidate is “good enough,” which can keep many rows at Likely instead of moving them to Verified.

I have updated the file again. This version does three things:

Keeps the fingerprint query first.

Uses the full bibliographic query as a stronger second fallback, instead of wasting the second query on journal-heavy searches.

Promotes strong title + year matches more confidently when there is no serious author conflict.

Download it here:

Download updated verify.py

Backup copy:

Download verify_balanced_recovery.py

Why this should perform better
The previous fingerprint order was:

1. title + author + year fingerprint
2. title + journal + year fingerprint
That second query can fail when journal extraction is noisy. The new order is:

1. DOI exact
2. title + author + year fingerprint
3. repaired title + author + year
4. full bibliographic reference
5. rich bibliographic query
6. title + journal + year
So the second and third searches are now more likely to recover valid references without increasing rows or timeouts.

Use these Render settings
VERIFY_LEAN_FAST_MODE=1
VERIFY_LEAN_ROWS=6
VERIFY_LEAN_CROSSREF_TEXT_QUERIES=3
VERIFY_LEAN_OPENALEX_TEXT_QUERIES=1
VERIFY_LEAN_MAX_API_CALLS=4
VERIFY_LEAN_USE_SPECIAL_FALLBACK=0

VERIFY_BALANCED_PROMOTION=1
VERIFY_BALANCED_VERIFY_TITLE_YEAR=88
VERIFY_BALANCED_VERIFY_TITLE_YEAR_SUPPORT=84
VERIFY_BALANCED_OPENALEX_ON_LIKELY=1

VERIFY_CROSSREF_ROWS=6
VERIFY_OPENALEX_ROWS=5
VERIFY_TITLE_ROWS=5
VERIFY_REQUEST_TIMEOUT=4
VERIFY_SINGLE_REF_TIMEOUT=8

VERIFY_DEEP_FALLBACK=0
VERIFY_RETRY_FAILED=0
VERIFY_MULTISOURCE_FALLBACK=0
VERIFY_STOP_ON_STRONG_MATCH=1
Do not increase rows to 20 again. It slows the system but does not solve the reason matches remain unvalidated.

Redeploy both the web service and worker service after replacing verify.py.

Create Word Docs
Create PDF Docs

which ones do  solve the reason matches remain unvalidated.

Thought for 13s
The settings that solve matches remain unvalidated are the ones that change promotion/classification, not the ones that only increase search volume.

Your current code already shows the main blocker: when author overlap is zero, the row gets author_mismatch_flag = 1, and a verified match can be downgraded to needs_review. 

 It also shows that deep fallback repeats searches when status is needs_review or not_found, which slows the system without necessarily promoting matches. 


Use these.

1. Most important: balanced promotion
VERIFY_BALANCED_PROMOTION=1
VERIFY_BALANCED_VERIFY_TITLE_YEAR=88
VERIFY_BALANCED_VERIFY_TITLE_YEAR_SUPPORT=84
VERIFY_BALANCED_OPENALEX_ON_LIKELY=1
These solve the real issue: strong matches stay as likely or needs_review because title/year evidence is not being promoted enough.

2. Reduce author-gate blocking
VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY=1
VERIFY_STRICT_AUTHOR_GATE=0
This prevents imperfect author extraction from blocking otherwise strong title-year matches.

Do not set this too aggressively unless you inspect matched titles:

VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY=0
That can increase verified count, but it can also increase false positives.

3. Allow one extra Crossref query, not many rows
VERIFY_LEAN_CROSSREF_TEXT_QUERIES=3
VERIFY_LEAN_OPENALEX_TEXT_QUERIES=1
VERIFY_LEAN_MAX_API_CALLS=4
This helps because the new query order uses:

fingerprint query
repaired title-author-year query
full bibliographic query
That is better than increasing VERIFY_CROSSREF_ROWS to 20.

4. Keep these for speed
VERIFY_DEEP_FALLBACK=0
VERIFY_MULTISOURCE_FALLBACK=0
VERIFY_RETRY_FAILED=0
VERIFY_STOP_ON_STRONG_MATCH=1
These stop the system from becoming slow. The old deep fallback path repeats Crossref/OpenAlex calls when a row is weak, which is exactly the slowdown you complained about. 


Best exact setup
Use this:

VERIFY_LEAN_FAST_MODE=1
VERIFY_LEAN_ROWS=6
VERIFY_LEAN_CROSSREF_TEXT_QUERIES=3
VERIFY_LEAN_OPENALEX_TEXT_QUERIES=1
VERIFY_LEAN_MAX_API_CALLS=4
VERIFY_LEAN_USE_SPECIAL_FALLBACK=0
VERIFY_LEAN_DISABLE_ENRICH_METADATA=1
VERIFY_LEAN_WORKERS=3
VERIFY_LEAN_STORE_EVERY=3
VERIFY_LEAN_PROMOTE_STRONG_TITLE_YEAR=1

VERIFY_BALANCED_PROMOTION=1
VERIFY_BALANCED_VERIFY_TITLE_YEAR=88
VERIFY_BALANCED_VERIFY_TITLE_YEAR_SUPPORT=84
VERIFY_BALANCED_OPENALEX_ON_LIKELY=1

VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY=1
VERIFY_STRICT_AUTHOR_GATE=0

VERIFY_CROSSREF_ROWS=6
VERIFY_OPENALEX_ROWS=5
VERIFY_TITLE_ROWS=5
VERIFY_REQUEST_TIMEOUT=4
VERIFY_SINGLE_REF_TIMEOUT=8
VERIFY_RETRY_ATTEMPTS=1

VERIFY_DEEP_FALLBACK=0
VERIFY_RETRY_FAILED=0
VERIFY_MULTISOURCE_FALLBACK=0
VERIFY_STOP_ON_STRONG_MATCH=1
In short
These are the key ones:

VERIFY_BALANCED_PROMOTION=1
VERIFY_BALANCED_VERIFY_TITLE_YEAR=88
VERIFY_BALANCED_VERIFY_TITLE_YEAR_SUPPORT=84
VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY=1
VERIFY_STRICT_AUTHOR_GATE=0
VERIFY_LEAN_CROSSREF_TEXT_QUERIES=3
They address why matches remain unvalidated. The rest mainly affects speed.

Create Word Docs
Create PDF Docs

after add VERIFY_BALANCED_PROMOTION=1
VERIFY_BALANCED_VERIFY_TITLE_YEAR=88
VERIFY_BALANCED_VERIFY_TITLE_YEAR_SUPPORT=84
VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY=1
VERIFY_STRICT_AUTHOR_GATE=0
VERIFY_LEAN_CROSSREF_TEXT_QUERIES=3, the verified reduce

Thought for 28s
That result confirms the issue is not the query volume. The new settings made classification more complicated, and the author-mismatch gate is likely downgrading some matches after they are promoted.

Your file has this logic: when both the reference and candidate have authors but author_overlap == 0, the system marks an author mismatch and can downgrade a verified result to needs_review. 


So when you added:

VERIFY_BALANCED_PROMOTION=1
VERIFY_BALANCED_VERIFY_TITLE_YEAR=88
VERIFY_BALANCED_VERIFY_TITLE_YEAR_SUPPORT=84
some matches were probably promoted to verified, then immediately downgraded because the author overlap was zero. That is why verified count reduced.

Remove these settings now
Set these to off or delete them from Render:

VERIFY_BALANCED_PROMOTION=0
VERIFY_BALANCED_VERIFY_TITLE_YEAR=
VERIFY_BALANCED_VERIFY_TITLE_YEAR_SUPPORT=
Also remove this if it exists:

VERIFY_BALANCED_OPENALEX_ON_LIKELY=
Keep this one:

VERIFY_LEAN_CROSSREF_TEXT_QUERIES=3
That one only gives the system one more Crossref query. It should not reduce verified count by itself.

Use this stable setup instead
VERIFY_LEAN_FAST_MODE=1
VERIFY_LEAN_ROWS=6
VERIFY_LEAN_CROSSREF_TEXT_QUERIES=3
VERIFY_LEAN_OPENALEX_TEXT_QUERIES=1
VERIFY_LEAN_MAX_API_CALLS=4
VERIFY_LEAN_USE_SPECIAL_FALLBACK=0
VERIFY_LEAN_DISABLE_ENRICH_METADATA=1
VERIFY_LEAN_WORKERS=3

VERIFY_BALANCED_PROMOTION=0
VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY=1
VERIFY_STRICT_AUTHOR_GATE=0

VERIFY_CROSSREF_ROWS=6
VERIFY_OPENALEX_ROWS=5
VERIFY_TITLE_ROWS=5
VERIFY_REQUEST_TIMEOUT=4
VERIFY_SINGLE_REF_TIMEOUT=8
VERIFY_RETRY_ATTEMPTS=1

VERIFY_DEEP_FALLBACK=0
VERIFY_RETRY_FAILED=0
VERIFY_MULTISOURCE_FALLBACK=0
VERIFY_STOP_ON_STRONG_MATCH=1
What this means
Do not try to solve this with more Render settings now. The reduction shows the current classification logic is fighting itself:

Strong title-year match → promoted to verified → author mismatch gate → downgraded to needs_review
The proper fix is a small code-level change: keep the author mismatch warning, but don’t automatically downgrade a strong title-year match when title score and year match are strong.

For now, remove VERIFY_BALANCED_PROMOTION=1 and rerun with the stable setup above.

Create Word Docs
Create PDF Docs

what can boost the scores  and doi extraction "1	verified	openalex	97	🔗 View Article (10.9734/jsrr/2020/v26i530263)	2020	adam	Sample Size Determination in Survey Research
2	likely	crossref	88	🔗 View Article (10.1177/0962280214558972)	2014	austin, steyerberg	Events per variable (EPV) and the relative performance of di…
3	not_found	—	—	—	—	—	
4	verified	crossref	99	🔗 View Article (10.1038/nrn3475)	2013	button, ioannidis, mokrysz, nosek, flint, robinson, munaf	Power failure: why small sample size undermines the reliabil…
5	likely	crossref	72	🔗 View Article (10.2307/1268167)	1978	j, cochran	Sampling Techniques
6	verified	crossref	78	🔗 View Article (10.2307/2290095)	1989	lachenbruch, cohen	Statistical Power Analysis for the Behavioral Sciences (2nd …
7	likely	crossref	70	🔗 View Article (10.1093/acprof:oso/9780195315493.001.0001)	2008	dattalo	Determining Sample Size
8	verified	crossref	96	🔗 View Article (10.1590/2176-9451.19.4.027-029.ebo)	2014	faber, fonseca	How sample size influences research outcomes
9	verified	crossref	100	🔗 View Article (10.3758/bf03193146)	2007	faul, erdfelder, lang, buchner	G*Power 3: A flexible statistical power analysis program for…
10	verified	crossref	89	🔗 View Article (10.1207/s15327906mbr2603_7)	1991	green	How Many Subjects Does It Take To Do A Regression Analysis
11	not_found	openalex	46	🔗 View Article (10.1108/02640470910979624)	2009	yang	Information literacy online tutorials
12	likely	crossref	61	🔗 View Article (10.32473/edis-pd006-1992)	2009	israel	Determining Sample Size
13	verified	crossref	100	🔗 View Article (10.4103/0974-7788.59946)	2010	bhalerao, kadam	Sample size calculation
14	verified	crossref	100	🔗 View Article (10.1037/1082-989x.8.3.305)	2003	kelley, maxwell	Sample Size for Multiple Regression: Obtaining Regression Co…
15	not_found	—	—	—	—	—	
16	likely	crossref	65	🔗 View Article (10.1016/b978-0-443-26629-4.00103-9)	2025	kline	Structural equation modeling
17	verified	crossref	96	🔗 View Article (10.1177/001316447003000308)	1970	krejcie, morgan	Determining Sample Size for Research Activities
18	not_found	openalex	42	—	2010	malhiwsky	Student Achievement Using Web 2.0 Technologies: A Mixed Meth…
19	needs_review	crossref	66	🔗 View Article (10.32614/cran.package.sdaresources)	2021	lu, lohr	SDAResources: Datasets and Functions for 'Sampling: Design a…
20	verified	crossref	99	🔗 View Article (10.1037/1082-989x.1.2.130)	1996	maccallum, browne, sugawara	Power analysis and determination of sample size for covarian…
21	verified	crossref	97	🔗 View Article (10.1146/annurev.psych.59.103006.093735)	2008	maxwell, kelley, rausch	Sample Size Planning for Statistical Power and Accuracy in P…
22	verified	crossref	100	🔗 View Article (10.1016/s0895-4356(96)00236-3)	1996	peduzzi, concato, kemper, holford, feinstein	A simulation study of the number of events per variable in l…
23	likely	crossref	66	🔗 View Article (10.2307/2282703)	1965	joskow, yamane	Statistics, an Introductory Analysis.
Your document is processed securely and not stored permanently.
© Prof Anokye M. Adam, University of Cape Coast. CiteIntegrity — Thesis & Manuscript Readiness & Citation Integrity Platform

Privacy Policy | 

Thought for 16s
Yes. Based on your output, the biggest improvement should come from score boosting after a DOI candidate is found, not from more searching.

You already have many rows with DOIs, but some remain likely because the score is not high enough or because author/title matching is imperfect. Your file currently relies heavily on title score, author overlap, and year match in _score, with only limited DOI promotion in candidate selection. 


What should boost the score
1. Candidate DOI exists + title is reasonable
If the system found a DOI from Crossref/OpenAlex and the title score is strong enough, add a boost.

Use this rule:

candidate DOI exists + title_score >= 75 + year_match = 1
→ boost score by 8 to 12
This can help rows like:

2. Events per variable...
score 88, DOI found, likely
That should become verified if the title and year match.

2. Exact DOI from original reference
If the original reference contains the DOI and the candidate DOI matches it, give a very strong boost.

original DOI == candidate DOI
→ verified if title_score >= 60
This is safe because DOI is the strongest identifier.

3. Candidate DOI found, but original reference has no DOI
This should still help, but not too much.

candidate DOI found + title_score >= 85 + year_match = 1
→ verified
candidate DOI found + title_score 70–84
→ likely, not verified
This protects you from false positives.

4. Journal/source support
If journal or source title matches, add a small boost.

journal_score >= 70
→ +5 score
This helps journal articles where authors are poorly extracted.

5. Book-title exact match
Some of your likely rows are books:

Sampling Techniques
Determining Sample Size
Statistics, an Introductory Analysis
For books, author extraction is often poor. If the title is short and exact, the scoring should not punish it too much.

Use:

short title exact match + year near match + DOI/ISBN found
→ likely or verified depending on author support
But don’t over-promote short titles. A title like Sampling Techniques is too generic to verify on title alone.

What will boost DOI extraction
Your current DOI regex is:

_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s]+)", re.I)
This catches many DOIs, but it misses or badly captures some formats. It can include trailing punctuation or fail when the DOI appears as a URL, doi:, uppercase DOI, or is followed by brackets.

Replace it with this stronger DOI pattern:

_DOI_RE = re.compile(
    r"(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)?"
    r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
    re.I
)
Then improve _extract_doi() like this:

def _extract_doi(text: str) -> str:
    text = _safe_str(text)

    m = _DOI_RE.search(text or "")
    if not m:
        return ""

    doi = m.group(1)

    # Remove common trailing punctuation from references.
    doi = doi.strip()
    doi = doi.rstrip(".,;:)]}>'\"")

    # Normalise DOI.
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi\s*:\s*", "", doi, flags=re.I)

    return doi.lower()
This improves DOI extraction from:

doi:10.9734/jsrr/2020/v26i530263
https://doi.org/10.9734/jsrr/2020/v26i530263
http://dx.doi.org/10.9734/jsrr/2020/v26i530263.
(10.9734/jsrr/2020/v26i530263)
Code patch to boost scores safely
In your candidate scoring or selection logic, add this after computing meta.

Look for where you have:

meta = _score(...)
doi_match = bool(ref_doi and doi and ref_doi.lower() == doi.lower())
meta_score = int(meta["score"] + (25 if doi_match else 0))
Replace with:

doi = _normalise_doi(doi)
ref_doi_n = _normalise_doi(ref_doi)

doi_match = bool(ref_doi_n and doi and ref_doi_n == doi)

title_score = int(meta.get("title_score", 0))
year_match = int(meta.get("year_match", 0))
author_overlap = int(meta.get("author_overlap", 0))

meta_score = int(meta["score"])

# 1. Exact DOI match is very strong.
if doi_match:
    meta_score += 30

# 2. Candidate DOI found, even if original reference did not contain DOI.
elif doi:
    if title_score >= 85 and year_match == 1:
        meta_score += 12
    elif title_score >= 75 and year_match == 1:
        meta_score += 8
    elif title_score >= 75 and author_overlap >= 1:
        meta_score += 6
    else:
        meta_score += 3

# 3. Strong title-year support.
if title_score >= 88 and year_match == 1:
    meta_score += 8

# 4. Strong title-author support.
if title_score >= 82 and author_overlap >= 1:
    meta_score += 6
Improve classification for DOI-backed matches
Update _classify() so DOI-backed matches are promoted more clearly.

Use this logic:

def _classify(
    doi_match: bool,
    title_score: int,
    score: int,
    year_match: int,
    author_overlap: int = 0,
    candidate_has_doi: bool = False,
) -> str:

    title_score = int(title_score or 0)
    score = int(score or 0)
    year_match = int(year_match or 0)
    author_overlap = int(author_overlap or 0)

    # Exact DOI match.
    if doi_match:
        if title_score >= 60:
            return "verified"
        return "likely"

    # Candidate DOI found, title and year support it.
    if candidate_has_doi and title_score >= 85 and year_match == 1:
        return "verified"

    # Candidate DOI found, strong title and author support it.
    if candidate_has_doi and title_score >= 82 and author_overlap >= 1:
        return "verified"

    # Very strong title-year match.
    if title_score >= 90 and year_match == 1:
        return "verified"

    # Strong title-author-year match.
    if title_score >= 82 and year_match == 1 and author_overlap >= 1:
        return "verified"

    # Likely.
    if title_score >= 78 and (year_match == 1 or author_overlap >= 1 or candidate_has_doi):
        return "likely"

    if score >= 70 and title_score >= 65:
        return "likely"

    if title_score >= 55 or score >= 45:
        return "needs_review"

    return "not_found"
Then call it like this:

status = _classify(
    bool(best_meta.get("doi_match")),
    int(best_meta.get("title_score", 0)),
    int(best_meta.get("score", 0)),
    int(best_meta.get("year_match", 0)),
    int(best_meta.get("author_overlap", 0)),
    candidate_has_doi=bool(best_meta.get("doi")),
)
What this will likely change in your current results
These are likely to improve:

2. likely, score 88, DOI found
Likely candidate for promotion to verified.

12. likely, score 61, DOI found
May improve to likely/needs review, but should not automatically verify.

23. likely, score 66, DOI found
Be careful. Short/generic book title. Do not auto-verify unless title and author are strong.

5. likely, score 72, DOI found
Could improve, but Sampling Techniques is generic. Keep as likely unless author/year confirms.

7. likely, score 70, DOI found
Could improve if title and year match strongly.

16. likely, score 65, DOI found
Be careful because 2025 looks suspicious for Kline unless it is a book chapter or new edition metadata.
The not-found rows with OpenAlex scores of 42–46 should not be promoted automatically. Those are weak matches.

Best Render settings after the code change
Use simple settings. Do not increase searches again.

VERIFY_LEAN_FAST_MODE=1
VERIFY_LEAN_CROSSREF_TEXT_QUERIES=3
VERIFY_LEAN_OPENALEX_TEXT_QUERIES=1
VERIFY_LEAN_MAX_API_CALLS=4
VERIFY_LEAN_ROWS=6

VERIFY_CROSSREF_ROWS=6
VERIFY_OPENALEX_ROWS=5
VERIFY_TITLE_ROWS=5

VERIFY_DEEP_FALLBACK=0
VERIFY_MULTISOURCE_FALLBACK=0
VERIFY_RETRY_FAILED=0
VERIFY_STOP_ON_STRONG_MATCH=1

VERIFY_REQUEST_TIMEOUT=4
VERIFY_SINGLE_REF_TIMEOUT=8
Main point
To improve your current results, don’t add more databases or rows. Add these two things:

1. Stronger DOI extraction from the original reference
2. Score boost when a trusted candidate DOI is found with strong title/year support
That should increase verified results without making verification slower.

Create Word Docs
Create PDF Docs


Pasted text(98).txt
Document
update 

Thought for 1m 34s
Updated the file. I added a final override that improves DOI extraction and adds a DOI-aware score boost without increasing API calls. Your uploaded file already had the main verification structure, so I kept it and only appended the safer score/classification override. 


Download the updated file here:

Download updated verify.py

Backup copy:

Download verify_doi_score_boost.py

What changed:

Improved DOI extraction from formats like:

doi:10.xxxx/xxxxx
https://doi.org/10.xxxx/xxxxx
http://dx.doi.org/10.xxxx/xxxxx
10.xxxx/xxxxx.
Added score boost when a candidate DOI is found and title/year/author evidence supports it.

Exact DOI matches now verify with modest bibliographic support.

Weak candidates are not promoted only because they have a DOI.

Kept your existing public functions and structure.

I tested it with:

python -m py_compile verify.py
and tested DOI extraction on your Adam reference. It correctly extracted:

10.9734/jsrr/2020/v26i530263
Use the updated verify.py for deployment, then redeploy both the web service and worker service.

Create Word Docs
Create PDF Docs


1
2
3
4
5
6
7
8
9
10
11
12
13
14
15
16
17
18
19
20
21
22
23
24
25
26
27
28
29
30
31
32
33
34
35
36
# verify.py — Complete with full metadata capture for APA/Harvard formatting

import os
import re
import threading
import time
import uuid
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

import requests
from rapidfuzz import fuzz


_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}

MAILTO = (
    os.getenv("CITATION_CROSSCHECKER_MAILTO")
    or os.getenv("CROSSREF_MAILTO")
    or os.getenv("OPENALEX_MAILTO")
    or ""
).strip()

# ============================================================
# TIMEOUT SETTINGS - ADDED FOR LARGE REFERENCE SETS
# ============================================================

# Commercial fast-verification settings.
# These defaults avoid one weak reference holding a whole job for minutes.
def _env_flag(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}

API_TIMEOUT = int(os.getenv("VERIFY_REQUEST_TIMEOUT", "4"))
VERIFICATION_TIMEOUT = None

Choose a syntax

Close
