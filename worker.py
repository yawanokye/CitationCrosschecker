# worker.py - Complete with all mismatch detection (COVERS ALL TEST SCENARIOS)
import os
import sys
import json
import re
import time
import hashlib
import urllib.parse
import urllib.request
import urllib.error
import redis
import psycopg2
from collections import Counter
from difflib import SequenceMatcher
from rq import Worker, Queue, Connection
from engine import run_crosscheck_with_autofix

try:
    from engine import recover_references_for_verification
except Exception:
    recover_references_for_verification = None
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from psycopg2.extras import RealDictCursor
from datetime import datetime
from verify import verify_references_batch
from acii import compute_acii
from claim_checker import build_claim_support_rows, suggest_alternative_sources_for_claim

try:
    from claim_support_scorer import score_claim_support
except Exception:
    score_claim_support = None

try:
    from citation_suggester import suggest_for_unverified, suggest_from_context
except Exception as e:
    print(f"[DEEP ENRICHMENT] citation_suggester import failed; direct OpenAlex/Crossref fallback will be used: {e}")
    suggest_for_unverified = None
    suggest_from_context = None
# Get connection strings
REDIS_URL = os.environ.get("REDIS_URL")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not REDIS_URL or not DATABASE_URL:
    print("ERROR: Missing REDIS_URL or DATABASE_URL")
    sys.exit(1)

# Connect to Redis
redis_conn = redis.from_url(REDIS_URL)

# ============================================================
# DURABLE VERIFICATION HELPERS
# ============================================================

VERIFY_CHUNK_SIZE = int(os.environ.get("VERIFY_CHUNK_SIZE", "10"))
CLAIM_SUPPORT_TIMEOUT = int(os.environ.get("CLAIM_SUPPORT_TIMEOUT", "60"))
MAX_ALT_SOURCES_IN_VERIFY = int(os.environ.get("MAX_ALT_SOURCES_IN_VERIFY", "5"))

# Commercial performance controls
# Keep the main verification path fast and predictable. Deep Crossref/OpenAlex
# lookups should run only in the deep_enrichment queue unless deliberately enabled.
def _env_flag(name, default="0"):
    return str(os.environ.get(name, default)).strip().lower() in {"1", "true", "yes", "on"}

DEEP_LOOKUPS_IN_VERIFY = _env_flag("DEEP_LOOKUPS_IN_VERIFY", "0")
RUN_REAL_CLAIM_CHECK_IN_VERIFY = _env_flag("RUN_REAL_CLAIM_CHECK_IN_VERIFY", "0")
ENQUEUE_DEEP_ENRICHMENT_AFTER_VERIFY = _env_flag("ENQUEUE_DEEP_ENRICHMENT_AFTER_VERIFY", "0")
DEEP_ENRICHMENT_LIMIT = int(os.environ.get("DEEP_ENRICHMENT_LIMIT", "80"))
DEEP_LOOKUP_TOP_K = int(os.environ.get("DEEP_LOOKUP_TOP_K", "3"))
DEEP_ENRICHMENT_BATCH_SAVE = int(os.environ.get("DEEP_ENRICHMENT_BATCH_SAVE", "10"))

# Fast verification controls
# Parallel mode verifies individual references concurrently inside each chunk.
# This is the main speed lever for reducing 10-reference jobs from about a minute
# to a few seconds, subject to Crossref/OpenAlex latency and rate limits.
VERIFY_PARALLEL_WORKERS = int(os.environ.get("VERIFY_PARALLEL_WORKERS", "8"))
VERIFY_CACHE_TTL = int(os.environ.get("VERIFY_CACHE_TTL", "604800"))  # 7 days
VERIFY_USE_CACHE = _env_flag("VERIFY_USE_CACHE", "1")
VERIFY_PARALLEL_MODE = _env_flag("VERIFY_PARALLEL_MODE", "1")


def now_iso():
    return datetime.utcnow().isoformat()


def _safe_json_loads(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return value


def _load_job_result(job_id):
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT result FROM jobs WHERE job_id = %s", (job_id,))
        row = cursor.fetchone()

        if not row:
            raise Exception(f"Job {job_id} not found")

        return _safe_json_loads(row["result"])

    finally:
        cursor.close()
        conn.close()


def _save_job_result(job_id, result, status=None):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    try:
        if status:
            cursor.execute(
                """
                UPDATE jobs
                SET result = %s::jsonb,
                    status = %s,
                    completed_at = CASE WHEN %s = 'completed' THEN NOW() ELSE completed_at END
                WHERE job_id = %s
                """,
                (json.dumps(result), status, status, job_id)
            )
        else:
            cursor.execute(
                """
                UPDATE jobs
                SET result = %s::jsonb
                WHERE job_id = %s
                """,
                (json.dumps(result), job_id)
            )

        conn.commit()

    finally:
        cursor.close()
        conn.close()

    try:
        redis_conn.setex(f"result:{job_id}", 3600, json.dumps(result))
    except Exception as e:
        print(f"[VERIFY WORKER] Could not refresh Redis result cache: {e}")

def _set_verification_meta(result, **kwargs):
    """
    Update verification metadata inside the result object.

    This helper is required by process_verification() for running,
    finalising, completed, and error states.
    """
    if result is None:
        result = {}

    verification = result.get("verification") or {}

    for key, value in kwargs.items():
        if value is not None:
            verification[key] = value

    result["verification"] = verification
    return result

def _compute_verification_summary(rows):
    rows = rows or []
    return {
        "total": len(rows),
        "verified": sum(1 for r in rows if r and r.get("status") == "verified"),
        "likely": sum(1 for r in rows if r and r.get("status") == "likely"),
        "needs_review": sum(1 for r in rows if r and r.get("status") == "needs_review"),
        "not_found": sum(1 for r in rows if r and r.get("status") == "not_found"),
        "offline": sum(1 for r in rows if r and r.get("status") == "offline"),
    }


def _reference_cache_key(ref, style="apa", enrich_metadata=False):
    """Stable Redis cache key for a reference verification result."""
    raw = json.dumps({
        "reference": str(ref or "").strip(),
        "style": style,
        "enrich_metadata": bool(enrich_metadata),
    }, sort_keys=True)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"verify:v2:{digest}"


def _make_offline_verification_row(ref, error="Verification failed or timed out"):
    """Create a safe row if a single-reference verification fails."""
    return {
        "status": "offline",
        "reference": ref,
        "original_reference": ref,
        "source": "worker_parallel_fallback",
        "score": 0,
        "title_score": 0,
        "doi": "",
        "year": "",
        "authors": "",
        "matched_title": "",
        "message": error,
        "error": error,
    }


def _verify_single_reference_cached(ref, style="apa", enrich_metadata=False):
    """
    Verify one reference with Redis caching.

    The existing verify_references_batch() is reused for correctness, but it is
    called with a single reference so many references can be processed in
    parallel by _verify_chunk_parallel().
    """
    cache_key = _reference_cache_key(ref, style=style, enrich_metadata=enrich_metadata)

    if VERIFY_USE_CACHE:
        try:
            cached = redis_conn.get(cache_key)
            if cached:
                row = json.loads(cached)
                if isinstance(row, dict):
                    row.setdefault("cache_hit", True)
                    return row
        except Exception as e:
            print(f"[VERIFY CACHE] Cache read failed: {e}")

    try:
        rows = verify_references_batch(
            [ref],
            style=style,
            use_crossref=True,
            use_openalex=False,
            job_id=None,
            enrich_metadata=enrich_metadata
        ) or []

        row = rows[0] if rows else _make_offline_verification_row(ref, "No verification row returned")
        if isinstance(row, dict):
            row.setdefault("reference", ref)
            row.setdefault("original_reference", ref)
            row.setdefault("cache_hit", False)

        if VERIFY_USE_CACHE and isinstance(row, dict):
            try:
                redis_conn.setex(cache_key, VERIFY_CACHE_TTL, json.dumps(row))
            except Exception as e:
                print(f"[VERIFY CACHE] Cache write failed: {e}")

        return row

    except Exception as e:
        return _make_offline_verification_row(ref, str(e))


def _verify_chunk_parallel(chunk, style="apa", enrich_metadata=False):
    """
    Verify a chunk concurrently while preserving input order.

    If parallel mode is disabled or there is only one reference, it falls back
    to the existing batch verifier.
    """
    chunk = list(chunk or [])

    if not chunk:
        return []

    if not VERIFY_PARALLEL_MODE or VERIFY_PARALLEL_WORKERS <= 1 or len(chunk) == 1:
        rows = verify_references_batch(
            chunk,
            style=style,
            use_crossref=True,
            use_openalex=False,
            job_id=None,
            enrich_metadata=enrich_metadata
        ) or []
        return rows

    max_workers = max(1, min(VERIFY_PARALLEL_WORKERS, len(chunk)))
    ordered_rows = [None] * len(chunk)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_verify_single_reference_cached, ref, style, enrich_metadata): i
            for i, ref in enumerate(chunk)
        }

        for future in as_completed(future_map):
            i = future_map[future]
            ref = chunk[i]
            try:
                ordered_rows[i] = future.result()
            except Exception as e:
                ordered_rows[i] = _make_offline_verification_row(ref, str(e))

    return [r for r in ordered_rows if r is not None]

def _fallback_recovery_suggestions(row, result, target=3):
    """
    Return three review-ready suggestions when online lookup cannot generate
    three concrete alternatives. This keeps the Recovery UI populated while
    making clear that these are human-review prompts, not automatic fixes.
    """
    citation = (
        row.get("citation")
        or row.get("in_text")
        or row.get("citation_in_text")
        or ""
    )

    reference = (
        row.get("reference")
        or row.get("original_reference")
        or row.get("matched_title")
        or row.get("title")
        or row.get("source_title")
        or ""
    )

    status = row.get("status", "")
    year = row.get("matched_year") or row.get("year") or ""
    authors = row.get("matched_authors") or row.get("authors") or ""
    doi = row.get("doi") or ""

    fallback = [
        {
            "title": "Check citation-source fit",
            "year": year,
            "authors": authors,
            "doi": doi,
            "reason": (
                f"This reference has verification status '{status}'. "
                "Compare the cited sentence with the matched source before accepting it."
            ),
            "suggested": reference[:250] if reference else "Review the matched reference manually.",
            "confidence": 0.50,
            "source": "context_review_fallback",
            "citation": citation,
            "reference": reference,
        },
        {
            "title": "Verify author, year, and DOI metadata",
            "year": year,
            "authors": authors,
            "doi": doi,
            "reason": "Confirm that the author names, publication year, title, and DOI belong to the same source.",
            "suggested": reference[:250] if reference else "Search the reference title manually in Crossref, OpenAlex, or Google Scholar.",
            "confidence": 0.45,
            "source": "metadata_review_fallback",
            "citation": citation,
            "reference": reference,
        },
        {
            "title": "Confirm claim support",
            "year": year,
            "authors": authors,
            "doi": doi,
            "reason": "Read the cited sentence and confirm that the source actually supports the claim, not only that the source exists.",
            "suggested": "Review the cited sentence against the source abstract, findings, or full text.",
            "confidence": 0.40,
            "source": "claim_support_review_fallback",
            "citation": citation,
            "reference": reference,
        },
    ]

    return fallback[:target]


def _normalise_recovery_suggestion(item, row, result):
    """Convert different suggestion shapes into one UI-friendly shape."""
    if not isinstance(item, dict):
        item = {"title": str(item)}

    citation = (
        row.get("citation")
        or row.get("in_text")
        or row.get("citation_in_text")
        or item.get("citation")
        or ""
    )

    reference = (
        row.get("reference")
        or row.get("original_reference")
        or row.get("matched_title")
        or row.get("title")
        or row.get("source_title")
        or item.get("reference")
        or ""
    )

    return {
        "title": item.get("title") or item.get("suggested_title") or item.get("source_title") or "Suggested source for review",
        "year": item.get("year") or item.get("matched_year") or row.get("year") or row.get("matched_year") or "",
        "authors": item.get("authors") or item.get("matched_authors") or row.get("authors") or row.get("matched_authors") or "",
        "doi": item.get("doi") or row.get("doi") or "",
        "reason": item.get("reason") or item.get("match_note") or "Review this suggestion before making changes.",
        "suggested": item.get("suggested") or item.get("reference") or item.get("title") or reference[:250],
        "confidence": item.get("confidence") or item.get("relevance") or item.get("score") or 0.50,
        "source": item.get("source") or item.get("type") or "context_lookup",
        "citation": citation,
        "reference": reference,
    }


def _dedupe_and_pad_suggestions(suggestions, row, result, target=3):
    """Deduplicate lookup suggestions and pad to three review-ready items."""
    clean = []
    seen = set()

    for item in suggestions or []:
        norm = _normalise_recovery_suggestion(item, row, result)
        key = (
            str(norm.get("doi") or "").lower().strip(),
            str(norm.get("title") or norm.get("suggested") or "").lower().strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        clean.append(norm)
        if len(clean) >= target:
            return clean[:target]

    for item in _fallback_recovery_suggestions(row, result, target=target):
        key = (
            str(item.get("doi") or "").lower().strip(),
            str(item.get("title") or item.get("suggested") or "").lower().strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        clean.append(item)
        if len(clean) >= target:
            break

    return clean[:target]


def _lookup_context_suggestions_for_row(row, result, target=3):
    """
    Slower deep lookup path for context/reference suggestions.
    This may call Crossref/OpenAlex through citation_suggester, so it should be used
    only in the deep_enrichment queue or when DEEP_LOOKUPS_IN_VERIFY is explicitly enabled.
    """
    existing = (
        row.get("suggested_references")
        or row.get("correction_suggestions")
        or row.get("suggestions")
        or []
    )

    if existing:
        return _dedupe_and_pad_suggestions(existing, row, result, target=target)

    citation = (
        row.get("citation")
        or row.get("in_text")
        or row.get("citation_in_text")
        or ""
    )

    reference = (
        row.get("reference")
        or row.get("original_reference")
        or row.get("matched_title")
        or row.get("title")
        or row.get("source_title")
        or ""
    )

    main_text = result.get("main_text", "") or result.get("full_text", "") or ""
    context = ""

    try:
        sentences = _split_sentences(main_text)
        context = _find_sentence_for_citation(sentences, citation)
    except Exception:
        context = ""

    if not context and main_text:
        context = main_text[:1500]

    suggestions = []

    if suggest_from_context and context:
        try:
            suggestions.extend(
                suggest_from_context(
                    context=context,
                    citation=citation,
                    top_k=target,
                ) or []
            )
        except Exception as e:
            print(f"[DEEP ENRICHMENT] Context lookup failed: {e}")

    if suggest_for_unverified and reference:
        try:
            suggestions.extend(suggest_for_unverified(reference, top_k=target) or [])
        except Exception as e:
            print(f"[DEEP ENRICHMENT] Reference suggestion failed: {e}")

    return _dedupe_and_pad_suggestions(suggestions, row, result, target=target)


# ============================================================
# ROBUST ADVANCED ENRICHMENT LOOKUP HELPERS
# ============================================================
# These helpers make Advanced Enrichment independent of the citation_suggester
# import path. If citation_suggester fails to import, or if its strict filters
# return nothing, we still query Crossref/OpenAlex directly and return real
# review-only source candidates.

ENRICHMENT_HTTP_TIMEOUT = int(os.environ.get("ENRICHMENT_HTTP_TIMEOUT", "12"))
ENRICHMENT_QUERY_LIMIT = int(os.environ.get("ENRICHMENT_QUERY_LIMIT", "6"))
CROSSREF_MAILTO = os.environ.get("CROSSREF_MAILTO", "").strip()
OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO", "").strip()

_ENRICHMENT_STOPWORDS = {
    "about", "above", "after", "again", "against", "among", "because", "before",
    "being", "between", "could", "during", "either", "figure", "found", "given",
    "having", "however", "include", "including", "into", "method", "methods", "model",
    "paper", "research", "result", "results", "review", "should", "study", "table",
    "their", "there", "these", "those", "through", "using", "where", "which", "while",
    "would", "claim", "citation", "source", "evidence", "analysis", "based", "support",
    "manual", "required", "matched", "available", "extracted", "context"
}


def _safe_get_json_url(url, timeout=None):
    """Small dependency-free JSON GET helper for Crossref/OpenAlex."""
    timeout = timeout or ENRICHMENT_HTTP_TIMEOUT
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "CiteIntegrity/1.0 (advanced-enrichment)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except Exception as e:
        print(f"[DEEP ENRICHMENT] API lookup failed: {e} | {url[:180]}")
        return {}


def _clean_query_text(text, max_len=220):
    text = re.sub(r"https?://\S+", " ", str(text or ""), flags=re.I)
    text = re.sub(r"doi\s*:?\s*10\.\S+", " ", text, flags=re.I)
    text = re.sub(r"\b10\.\d{4,9}/\S+", " ", text, flags=re.I)
    text = re.sub(r"[^A-Za-z0-9\s:&,\-']", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def _extract_enrichment_keywords(text, limit=10):
    words = re.findall(r"[A-Za-z][A-Za-z\-']{3,}", str(text or "").lower())
    out = []
    seen = set()
    for word in words:
        word = word.strip("-' ")
        if len(word) < 4 or word in _ENRICHMENT_STOPWORDS or word in seen:
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= limit:
            break
    return out


def _extract_reference_title_for_lookup(reference):
    """Extract a usable title phrase from an APA-like reference string."""
    ref = re.sub(r"\s+", " ", str(reference or "")).strip()
    if not ref:
        return ""

    # Prefer title after the year, e.g. Author. (2021). Title. Journal.
    m = re.search(r"\((?:19|20)\d{2}[a-z]?\)\s*\.\s*(.+?)(?:\.\s+[A-Z][A-Za-z& ]{2,}|$)", ref)
    if m:
        title = m.group(1).strip()
        if len(title) >= 8:
            return _clean_query_text(title, max_len=180)

    # Handle references without a full stop immediately after year.
    m = re.search(r"(?:19|20)\d{2}[a-z]?\)?\s*\.\s*(.+?)(?:\.\s+[A-Z][A-Za-z& ]{2,}|$)", ref)
    if m:
        title = m.group(1).strip()
        if len(title) >= 8:
            return _clean_query_text(title, max_len=180)

    # Fallback: remove author/year leading material and use significant words.
    fallback = re.sub(r"^.{0,140}?(?:19|20)\d{2}[a-z]?\)?\s*\.\s*", "", ref)
    fallback = _clean_query_text(fallback or ref, max_len=180)
    return fallback


def _candidate_from_crossref_item(item, query=""):
    title = ""
    if isinstance(item.get("title"), list) and item.get("title"):
        title = item.get("title")[0] or ""
    elif isinstance(item.get("title"), str):
        title = item.get("title")

    if not title:
        return None

    year = ""
    for key in ("published-print", "published-online", "issued", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [])
        if parts and parts[0]:
            year = str(parts[0][0])
            break

    authors = []
    for au in item.get("author") or []:
        name = " ".join(x for x in [au.get("given"), au.get("family")] if x).strip()
        if name:
            authors.append(name)

    doi = str(item.get("DOI") or item.get("doi") or "").strip()
    url = item.get("URL") or (f"https://doi.org/{doi}" if doi else "")

    return {
        "title": title,
        "year": year,
        "authors": authors[:6],
        "doi": doi,
        "url": url,
        "source": "crossref_direct",
        "relevance": item.get("score") or 0,
        "query_used": query,
        "suggestion_type": "context_specific_source",
        "reason": "Retrieved from Crossref using Advanced Enrichment query expansion. Review before using."
    }


def _candidate_from_openalex_item(item, query=""):
    title = item.get("title") or item.get("display_name") or ""
    if not title:
        return None

    authors = []
    for auth in item.get("authorships") or []:
        au = auth.get("author") or {}
        name = au.get("display_name") or ""
        if name:
            authors.append(name)

    doi = str(item.get("doi") or "").strip()
    if doi.lower().startswith("https://doi.org/"):
        doi = doi.split("https://doi.org/", 1)[1]

    primary = item.get("primary_location") or {}
    url = primary.get("landing_page_url") or item.get("id") or (f"https://doi.org/{doi}" if doi else "")

    return {
        "title": title,
        "year": item.get("publication_year") or "",
        "authors": authors[:6],
        "doi": doi,
        "url": url,
        "source": "openalex_direct",
        "relevance": item.get("relevance_score") or 0,
        "query_used": query,
        "suggestion_type": "context_specific_source",
        "reason": "Retrieved from OpenAlex using Advanced Enrichment query expansion. Review before using."
    }


def _query_crossref_direct(query, rows=6):
    query = _clean_query_text(query)
    if not query:
        return []
    params = {
        "query.bibliographic": query,
        "rows": str(rows),
        "select": "DOI,title,author,issued,published-print,published-online,created,URL,score",
    }
    if CROSSREF_MAILTO:
        params["mailto"] = CROSSREF_MAILTO
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    data = _safe_get_json_url(url)
    items = (((data or {}).get("message") or {}).get("items") or [])
    return [c for c in (_candidate_from_crossref_item(item, query) for item in items) if c]


def _query_openalex_direct(query, rows=6):
    query = _clean_query_text(query)
    if not query:
        return []
    params = {"search": query, "per-page": str(rows)}
    if OPENALEX_MAILTO:
        params["mailto"] = OPENALEX_MAILTO
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    data = _safe_get_json_url(url)
    items = (data or {}).get("results") or []
    return [c for c in (_candidate_from_openalex_item(item, query) for item in items) if c]


def _build_deep_enrichment_queries(citation="", reference="", context="", source_title="", target=3):
    """Build several fallback queries so one strict query does not kill enrichment."""
    queries = []

    ref_title = _extract_reference_title_for_lookup(reference or source_title)
    if ref_title:
        queries.append(ref_title)

    citation_bits = []
    try:
        years = re.findall(r"(?:19|20)\d{2}[a-z]?", str(citation or ""))
        names = [n for n in re.findall(r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]{2,}", str(citation or "")) if n.lower() not in {"et", "al"}]
        citation_bits = names[:2] + years[:1]
    except Exception:
        citation_bits = []

    if ref_title and citation_bits:
        queries.append(" ".join(citation_bits + [ref_title]))

    context_keywords = _extract_enrichment_keywords(context, limit=10)
    if context_keywords:
        queries.append(" ".join(context_keywords[:8]))
        if citation_bits:
            queries.append(" ".join(citation_bits + context_keywords[:6]))

    compact_ref = _clean_query_text(reference, max_len=220)
    if compact_ref and compact_ref not in queries:
        queries.append(compact_ref)

    # Keep unique and not too many.
    out = []
    seen = set()
    for q in queries:
        q = _clean_query_text(q)
        key = q.lower()
        if len(q) < 6 or key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= ENRICHMENT_QUERY_LIMIT:
            break
    return out


def _direct_scholarly_source_lookup(citation="", reference="", context="", source_title="", target=3):
    candidates = []
    queries = _build_deep_enrichment_queries(
        citation=citation,
        reference=reference,
        context=context,
        source_title=source_title,
        target=target,
    )

    for query in queries:
        # Query OpenAlex first because it is often better for books, reports,
        # older works, and non-DOI records. Then add Crossref.
        candidates.extend(_query_openalex_direct(query, rows=max(target * 2, 6)))
        candidates.extend(_query_crossref_direct(query, rows=max(target * 2, 6)))
        if len(candidates) >= target * 3:
            break

    return candidates

def _deep_context_source_suggestions(row, result, target=3, include_reference=True):
    """
    Force Advanced Enrichment to search for real context-specific sources.

    This version uses three layers:
    1. citation_suggester context search, when the import works;
    2. citation_suggester reference correction search, when a reference exists;
    3. direct OpenAlex/Crossref query expansion from reference title, citation,
       claim/context keywords, and full reference text.

    It does not reuse Recovery Lite prompts as source suggestions.
    """
    citation = (
        row.get("citation")
        or row.get("in_text")
        or row.get("citation_in_text")
        or ""
    )

    reference = (
        row.get("reference")
        or row.get("original_reference")
        or row.get("matched_reference")
        or row.get("matched_title")
        or row.get("title")
        or row.get("source_title")
        or ""
    )

    source_title = str(
        row.get("source_title")
        or row.get("matched_source")
        or row.get("matched_title")
        or ""
    ).strip()

    main_text = result.get("main_text", "") or result.get("full_text", "") or ""

    context = (
        row.get("context")
        or row.get("claim")
        or row.get("claim_extracted")
        or row.get("extracted_claim")
        or row.get("citation_context")
        or row.get("nearby_text")
        or ""
    )

    if not context and main_text and citation:
        try:
            sentences = _split_sentences(main_text)
            context = _find_sentence_for_citation(sentences, citation)
        except Exception:
            context = ""

    # Do not rely only on the first 1500 characters for source discovery.
    # It may be unrelated to the row. Use it only as the last weak fallback.
    if not context and main_text:
        context = main_text[:1500]

    suggestions = []

    # Layer 1: existing context suggester, if available.
    if suggest_from_context and context:
        try:
            suggestions.extend(
                suggest_from_context(
                    context=context,
                    citation=citation,
                    top_k=target + 5
                ) or []
            )
        except Exception as e:
            print(f"[DEEP ENRICHMENT] citation_suggester context lookup failed: {e}")

    # Layer 2: existing reference suggester, if available.
    if include_reference and suggest_for_unverified and reference:
        try:
            suggestions.extend(
                suggest_for_unverified(reference, top_k=target + 5) or []
            )
        except Exception as e:
            print(f"[DEEP ENRICHMENT] citation_suggester reference lookup failed: {e}")

    # Layer 3: direct OpenAlex/Crossref fallback. This is the important fix
    # when citation_suggester import fails, strict title_score filters remove
    # all candidates, or the row has only claim/context text.
    try:
        suggestions.extend(
            _direct_scholarly_source_lookup(
                citation=citation,
                reference=reference if include_reference else "",
                context=context,
                source_title=source_title,
                target=target + 5,
            )
        )
    except Exception as e:
        print(f"[DEEP ENRICHMENT] Direct scholarly lookup failed: {e}")

    return _dedupe_real_source_suggestions(
        suggestions,
        target=target,
        exclude_title=source_title,
    )


def _context_suggestions_for_row(row, result):
    """
    Fast Recovery Lite path for the main verification job.
    By default, this never calls Crossref/OpenAlex. It returns existing suggestions
    if already present, otherwise three review-ready fallback prompts.
    """
    existing = (
        row.get("suggested_references")
        or row.get("correction_suggestions")
        or row.get("suggestions")
        or []
    )

    if existing:
        return _dedupe_and_pad_suggestions(existing, row, result, target=3)

    if not DEEP_LOOKUPS_IN_VERIFY:
        return _fallback_recovery_suggestions(row, result, target=3)

    return _lookup_context_suggestions_for_row(row, result, target=3)

def _dedupe_real_source_suggestions(suggestions, target=3, exclude_title=""):
    """
    Keep only real source candidates and normalise them for the UI.

    This intentionally does not create fake fallback sources. If fewer than
    three real candidates are returned by Crossref/OpenAlex, the row receives
    an enrichment_note so the reviewer understands why fewer sources appear.
    """
    clean = []
    seen = set()
    exclude_title = str(exclude_title or "").strip().lower()

    for item in suggestions or []:
        if not isinstance(item, dict):
            continue

        title = str(
            item.get("title")
            or item.get("suggested_title")
            or item.get("source_title")
            or item.get("suggested")
            or ""
        ).strip()
        doi = str(item.get("doi") or item.get("DOI") or "").strip()
        year = item.get("year") or item.get("published_year") or item.get("matched_year") or ""
        authors = item.get("authors") or item.get("matched_authors") or []

        if isinstance(authors, str):
            authors = [a.strip() for a in authors.split(",") if a.strip()]

        if not title:
            continue

        title_key = title.lower()
        if exclude_title and (
            title_key == exclude_title
            or title_key in exclude_title
            or exclude_title in title_key
        ):
            continue

        key = f"{title_key}|{year}|{doi.lower()}"
        if key in seen:
            continue

        seen.add(key)

        clean.append({
            "title": title,
            "year": year,
            "authors": authors,
            "doi": doi,
            "url": item.get("url") or item.get("source_url") or item.get("openalex_url") or "",
            "relevance": item.get("relevance") or item.get("confidence") or item.get("score") or item.get("title_score") or 0,
            "source": item.get("source") or item.get("type") or "context_source_lookup",
            "suggestion_type": item.get("suggestion_type") or "context_specific_source",
            "reason": item.get("reason") or "Suggested from manuscript context during Advanced Enrichment. Review before using."
        })

        if len(clean) >= target:
            break

    return clean[:target]


def _source_enrichment_note(items, target=3):
    count = len(items or [])
    if count >= target:
        return ""
    if count == 0:
        return "No context-specific source was returned by Crossref/OpenAlex. Manual review is required."
    return f"Only {count} context-specific source(s) were returned by Crossref/OpenAlex. Manual review is required."
def _context_suggestions_for_missing_citation(citation, result, count=1, target=3):
    """
    Fast Recovery Lite suggestions for in-text citations that are missing from
    the reference list. These are instant review prompts, not external lookups.
    Deep source suggestions can be added later by process_deep_enrichment().
    """
    citation = str(citation or "").strip()
    main_text = result.get("main_text", "") or result.get("full_text", "") or ""

    context = ""
    claim = ""

    try:
        sentences = _split_sentences(main_text)
        context = _find_sentence_for_citation(sentences, citation)
        claim = _extract_claim_from_sentence(context, citation)
    except Exception:
        context = ""
        claim = ""

    if not context and main_text:
        # Give the reviewer useful context without doing expensive search.
        context = main_text[:500]

    if not claim:
        claim = "Claim could not be extracted automatically. Review the cited sentence manually."

    return [
        {
            "title": "Add the missing reference entry",
            "authors": "",
            "year": extract_year_from_text(citation) or "",
            "doi": "",
            "reason": (
                f"The in-text citation '{citation}' appears in the manuscript "
                "but no matching reference-list entry was found. Add the full reference if the citation is valid."
            ),
            "suggested": f"Create a full reference-list entry for {citation}.",
            "confidence": 0.70,
            "source": "missing_reference_recovery_lite",
            "citation": citation,
            "claim": claim,
            "context": context,
            "count": count,
        },
        {
            "title": "Check author and year spelling",
            "authors": "",
            "year": extract_year_from_text(citation) or "",
            "doi": "",
            "reason": (
                "The citation may be unmatched because of a spelling, author-order, suffix, "
                "or year difference between the in-text citation and the reference list."
            ),
            "suggested": "Compare the author name, publication year, suffix letters such as 2020a/2020b, and punctuation with the reference list.",
            "confidence": 0.60,
            "source": "metadata_check_recovery_lite",
            "citation": citation,
            "claim": claim,
            "context": context,
            "count": count,
        },
        {
            "title": "Confirm the cited claim before adding the source",
            "authors": "",
            "year": extract_year_from_text(citation) or "",
            "doi": "",
            "reason": "A missing reference should not be added mechanically. Confirm that the source supports the cited claim.",
            "suggested": claim if claim and not claim.startswith("Claim could not") else "Review the sentence containing the citation and confirm the source supports the claim.",
            "confidence": 0.55,
            "source": "claim_context_recovery_lite",
            "citation": citation,
            "claim": claim,
            "context": context,
            "count": count,
        },
    ][:target]

def _build_recovery_payload(result, verification_rows):
    """
    Safe recovery builder for the new worker flow.
    It avoids depending on web-process memory and always returns UI-ready arrays.
    """
    missing_recovery = []
    verification_recovery = []

    for item in result.get("missing_in_references", []) or []:
        if isinstance(item, str):
            citation = item
            count = 1
        else:
            citation = item.get("citation_in_text") or item.get("citation") or item.get("in_text") or ""
            count = item.get("count_in_text") or item.get("count") or 1

        missing_recovery.append({
            "citation": citation,
            "count": count,
            "suggestions": _context_suggestions_for_missing_citation(
                citation=citation,
                result=result,
                count=count,
                target=3
            )
        })

    for row in verification_rows or []:
        status = row.get("status", "")
        if status not in {"needs_review", "not_found", "offline"}:
               continue

        suggestions = _context_suggestions_for_row(row, result)

        verification_recovery.append({
            "status": status,
            "citation": (
                row.get("citation")
                or row.get("in_text")
                or row.get("citation_in_text")
                or ""
            ),
            "reference": (
                row.get("reference")
                or row.get("original_reference")
                or row.get("matched_title")
                or row.get("title")
                or row.get("source_title")
                or ""
            ),
            "suggestions": suggestions
        })

    return {
        "missing_recovery": missing_recovery,
        "verification_recovery": verification_recovery
    }

def _split_sentences(text):
    """
    Lightweight sentence splitter for claim extraction.
    Keeps enough context for citation-bearing sentences.
    """
    text = str(text or "")
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return []

    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", text)
    return [s.strip() for s in sentences if len(s.strip()) > 20]


def _citation_variants(citation):
    """
    Build possible citation forms found in manuscript text.
    Handles:
    Cohen, 1988
    (Cohen, 1988)
    Cohen (1988)
    Button et al., 2013
    Button et al. (2013)
    """
    citation = str(citation or "").strip()
    citation = citation.strip("() ")
    variants = set()

    if not citation:
        return []

    variants.add(citation)
    variants.add(f"({citation})")

    year_match = re.search(r"\b((?:19|20)\d{2}[a-z]?)\b", citation, flags=re.I)

    if year_match:
        year = year_match.group(1)
        author_part = citation[:year_match.start()].strip(" ,;()")

        if author_part:
            variants.add(f"{author_part}, {year}")
            variants.add(f"({author_part}, {year})")
            variants.add(f"{author_part} ({year})")

            # Handle "and" and "&" variants
            if " and " in author_part.lower():
                amp_author = re.sub(r"\s+and\s+", " & ", author_part, flags=re.I)
                variants.add(f"{amp_author}, {year}")
                variants.add(f"({amp_author}, {year})")
                variants.add(f"{amp_author} ({year})")

            if "&" in author_part:
                and_author = author_part.replace("&", "and")
                variants.add(f"{and_author}, {year}")
                variants.add(f"({and_author}, {year})")
                variants.add(f"{and_author} ({year})")

    # Longest first helps locate full forms before partial forms
    return sorted(variants, key=len, reverse=True)


def _clean_extracted_claim(text):
    """
    Clean claim text without destroying its meaning.
    """
    text = str(text or "")
    text = re.sub(r"\s+", " ", text).strip()

    # Remove leftover citation brackets where possible
    text = re.sub(
        r"\([^()]*\b(?:19|20)\d{2}[a-z]?\b[^()]*\)",
        "",
        text,
        flags=re.I
    )

    text = re.sub(r"\s+", " ", text).strip(" ,;:.")
    return text


def _extract_claim_from_sentence(sentence, citation):
    """
    Extract the claim around a citation.

    For parenthetical citations, the claim is usually before the citation.
    Example: Sample size affects statistical power (Cohen, 1988).

    For narrative citations, useful claim text may come after the citation.
    Example: Cohen (1988) argued that power depends on effect size...
    """
    sentence = str(sentence or "").strip()
    variants = _citation_variants(citation)

    if not sentence:
        return ""

    lower_sentence = sentence.lower()

    for variant in variants:
        lower_variant = variant.lower()
        idx = lower_sentence.find(lower_variant)

        if idx == -1:
            continue

        before = sentence[:idx].strip(" ,;:")
        after = sentence[idx + len(variant):].strip(" ,;:")

        before_clean = _clean_extracted_claim(before)
        after_clean = _clean_extracted_claim(after)
        full_clean = _clean_extracted_claim(sentence)

        # Parenthetical citation, claim normally before citation
        if variant.startswith("(") and len(before_clean) >= 25:
            return before_clean

        # Narrative citation, claim often after citation
        if not variant.startswith("(") and "(" in variant and len(after_clean) >= 25:
            return after_clean

        # If before is meaningful, use it
        if len(before_clean) >= 25:
            return before_clean

        # If after is meaningful, use it
        if len(after_clean) >= 25:
            return after_clean

        # Fallback to full cleaned sentence
        if len(full_clean) >= 25:
            return full_clean

    # If citation form was not found exactly, return the cleaned sentence
    return _clean_extracted_claim(sentence)


def _find_sentence_for_citation(sentences, citation):
    """
    Find the sentence containing a citation variant.
    """
    variants = _citation_variants(citation)

    if not variants:
        return ""

    for sentence in sentences:
        sentence_lower = sentence.lower()

        for variant in variants:
            if variant.lower() in sentence_lower:
                return sentence

    return ""

def _safe_alternative_sources(claim, citation="", current_source_title="", top_k=3, allow_external=False):
    """
    Alternative-source lookup is expensive. In the main verification job it is
    disabled by default so Recovery and Claim Support can populate quickly.
    Set allow_external=True only from the deep_enrichment queue.
    """
    if not (allow_external or DEEP_LOOKUPS_IN_VERIFY):
        return []

    claim = str(claim or "").strip()

    if not claim or len(claim) < 20:
        return []

    if claim.lower().startswith("claim could not be extracted"):
        return []

    try:
        return suggest_alternative_sources_for_claim(
            claim=claim,
            citation=citation,
            current_source_title=current_source_title,
            top_k=top_k
        ) or []
    except Exception as e:
        print(f"[DEEP ENRICHMENT] Alternative source suggestion failed for {citation}: {e}")
        return []

def _basic_keyword_overlap_score(claim, source_title):
    """Small fallback score when only a title is available."""
    stop = {
        "this", "that", "with", "from", "using", "used", "study", "analysis",
        "method", "approach", "results", "paper", "research", "journal", "review",
        "effect", "effects", "relationship", "role", "model", "models", "findings",
    }
    claim_words = set(re.findall(r"[a-z]{4,}", str(claim or "").lower())) - stop
    title_words = set(re.findall(r"[a-z]{4,}", str(source_title or "").lower())) - stop

    if not claim_words or not title_words:
        return 0

    overlap = claim_words & title_words
    if not overlap:
        return 0

    return min(40, 10 + (len(overlap) * 10))


def _score_claim_support_for_worker(claim, source_title):
    """
    Score claim support even in fallback mode.
    Uses the main scorer when available and a conservative title-overlap fallback otherwise.
    """
    claim = str(claim or "").strip()
    source_title = str(source_title or "").strip()

    empty = {
        "score": 0,
        "status": "manual_review_required",
        "title_overlap": 0,
        "abstract_overlap": 0,
        "keyword_overlap": 0,
        "direction_overlap": 0,
        "relation_overlap": 0,
        "partial_support": False,
        "concept_matches": [],
        "score_explanation": "No usable claim or source title was available for scoring.",
        "evidence_used": "none",
    }

    if not claim or not source_title or source_title.lower().startswith(("matched source not available", "source title not available", "no source found")):
        return empty

    if score_claim_support:
        try:
            support = score_claim_support(
                claim=claim,
                source_title=source_title,
                source_abstract="",
                source_concepts=[],
                source_metadata={},
            ) or {}

            score = int(support.get("score", 0) or 0)
            if score > 0:
                support.setdefault("evidence_used", "title_only")
                support.setdefault("score_explanation", "Worker fallback used title-only support scoring.")
                return support

        except Exception as e:
            print(f"[VERIFY WORKER] Title-only claim scoring failed: {e}")

    score = _basic_keyword_overlap_score(claim, source_title)

    if score > 0:
        return {
            "score": score,
            "status": "title_overlap_review_required",
            "title_overlap": score,
            "abstract_overlap": 0,
            "keyword_overlap": score,
            "direction_overlap": 0,
            "relation_overlap": 0,
            "partial_support": score >= 30,
            "concept_matches": [],
            "score_explanation": "Conservative title-keyword overlap score. Human review is still required.",
            "evidence_used": "title_keyword_overlap",
        }

    return {
        **empty,
        "status": "insufficient_title_overlap",
        "score_explanation": "The extracted claim and source title had no meaningful keyword overlap.",
        "evidence_used": "title_only",
    }


def _enhance_claim_support_scores(rows):
    """Fill zero scores when a claim and source title allow conservative title-only scoring."""
    enhanced = []

    for row in rows or []:
        if not isinstance(row, dict):
            continue

        current_score = int(row.get("support_score", 0) or 0)
        if current_score <= 0:
            source_title = (
                row.get("source_title")
                or row.get("matched_source")
                or row.get("reference")
                or ""
            )
            support = _score_claim_support_for_worker(row.get("claim", ""), source_title)

            if int(support.get("score", 0) or 0) > 0:
                row["support_score"] = support.get("score", 0)
                row["support_status"] = support.get("status", row.get("support_status", "manual_review_required"))
                row["evidence_used"] = support.get("evidence_used", "title_only")
                row["title_overlap"] = support.get("title_overlap", row.get("title_overlap", 0))
                row["abstract_overlap"] = support.get("abstract_overlap", row.get("abstract_overlap", 0))
                row["keyword_overlap"] = support.get("keyword_overlap", row.get("keyword_overlap", 0))
                row["direction_overlap"] = support.get("direction_overlap", row.get("direction_overlap", 0))
                row["relation_overlap"] = support.get("relation_overlap", row.get("relation_overlap", 0))
                row["partial_support"] = support.get("partial_support", row.get("partial_support", False))
                row["concept_matches"] = support.get("concept_matches", row.get("concept_matches", []))
                row["score_explanation"] = support.get("score_explanation", row.get("score_explanation", ""))

        enhanced.append(row)

    return enhanced


# ============================================================
# CLAIM-SUPPORT VERIFICATION GATE
# ============================================================
# A claim-support score must never outrank the trustworthiness of the source.
# If the matched reference is needs_review/not_found/offline, the claim may be
# semantically related to the title, but it cannot be labelled strong_support.


def _norm_claim_lookup_key(text):
    """Normalise citation/reference/title text for safer verification lookup."""
    text = str(text or "").lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\bdoi\s*:?\s*10\.\S+", " ", text, flags=re.I)
    text = re.sub(r"\b10\.\d{4,9}/\S+", " ", text, flags=re.I)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _build_verification_claim_lookups(verification_rows):
    """
    Build citation and reference/title lookup tables so claim-support rows inherit
    the real online verification status of the matched source.
    """
    citation_lookup = {}
    reference_lookup = {}

    for vr in verification_rows or []:
        if not isinstance(vr, dict):
            continue

        status = str(vr.get("status") or "").strip().lower()

        citation_key = str(
            vr.get("citation")
            or vr.get("in_text")
            or vr.get("citation_in_text")
            or ""
        ).strip().lower()

        reference_value = (
            vr.get("reference")
            or vr.get("original_reference")
            or vr.get("matched_title")
            or vr.get("title")
            or vr.get("source_title")
            or ""
        )

        payload = {
            "reference": reference_value,
            "doi": vr.get("doi", ""),
            "status": status,
            "matched_title": vr.get("matched_title") or vr.get("title") or vr.get("source_title") or "",
            "matched_authors": vr.get("matched_authors") or vr.get("authors") or "",
            "matched_year": vr.get("matched_year") or vr.get("year") or "",
            "source": vr.get("source") or "",
        }

        if citation_key and citation_key not in citation_lookup:
            citation_lookup[citation_key] = payload

        for key_text in [
            vr.get("reference"),
            vr.get("original_reference"),
            vr.get("matched_title"),
            vr.get("title"),
            vr.get("source_title"),
            reference_value,
        ]:
            key = _norm_claim_lookup_key(key_text)
            if key and key not in reference_lookup:
                reference_lookup[key] = payload

    return citation_lookup, reference_lookup


def _attach_verification_status_to_claim_row(row, citation_lookup=None, reference_lookup=None):
    """Attach source verification status to a claim-support row when possible."""
    if not isinstance(row, dict):
        return row

    citation_lookup = citation_lookup or {}
    reference_lookup = reference_lookup or {}

    existing_status = str(
        row.get("verification_status")
        or row.get("source_verification_status")
        or row.get("citation_match_status")
        or ""
    ).strip().lower()

    payload = None

    citation_key = str(
        row.get("citation")
        or row.get("in_text")
        or row.get("citation_in_text")
        or ""
    ).strip().lower()

    if citation_key:
        payload = citation_lookup.get(citation_key)

    if not payload:
        for key_text in [
            row.get("matched_source"),
            row.get("reference"),
            row.get("source_title"),
            row.get("matched_title"),
            row.get("title"),
        ]:
            key = _norm_claim_lookup_key(key_text)
            if key and key in reference_lookup:
                payload = reference_lookup[key]
                break

    if payload:
        status = str(payload.get("status") or "").strip().lower()
        if status:
            row["verification_status"] = status
            row["source_verification_status"] = status
            row["citation_match_status"] = status

        row.setdefault("doi", payload.get("doi", ""))
        row.setdefault("verification_matched_title", payload.get("matched_title", ""))
        row.setdefault("verification_matched_authors", payload.get("matched_authors", ""))
        row.setdefault("verification_matched_year", payload.get("matched_year", ""))

    elif existing_status:
        row["verification_status"] = existing_status
        row["source_verification_status"] = existing_status
        row["citation_match_status"] = existing_status

    else:
        row.setdefault("verification_status", "unknown")
        row.setdefault("source_verification_status", "unknown")
        row.setdefault("citation_match_status", "unknown")

    return row


def _apply_verification_gate_to_claim_row(row):
    """
    Prevent unverified or uncertain sources from being labelled as strong claim support.
    Verification confidence must dominate claim-support confidence.
    """
    if not isinstance(row, dict):
        return row

    trusted_statuses = {"verified", "likely"}
    weak_verify_statuses = {
        "needs_review", "not_found", "offline", "error", "failed",
        "unverified", "unknown", "", "none"
    }

    verification_status = str(
        row.get("verification_status")
        or row.get("source_verification_status")
        or row.get("citation_match_status")
        or "unknown"
    ).strip().lower()

    support_status = str(row.get("support_status") or "").strip().lower()

    try:
        support_score = int(float(row.get("support_score") or row.get("score") or 0))
    except Exception:
        support_score = 0

    row["verification_status"] = verification_status or "unknown"
    row["source_verification_status"] = verification_status or "unknown"
    row["citation_match_status"] = verification_status or "unknown"

    if verification_status in trusted_statuses:
        return row

    positive_or_high = (
        support_status in {
            "strong_support",
            "moderate_support",
            "related_evidence",
            "weak_or_unclear",
            "title_overlap_review_required",
        }
        or support_score >= 50
    )

    # If the row is already negative and low-scoring, do not replace its more
    # specific no-evidence explanation. The gate is mainly to prevent false
    # confidence such as needs_review + strong_support.
    if not positive_or_high:
        return row

    if verification_status in weak_verify_statuses:
        row["ungated_support_status"] = row.get("support_status", "")
        row["ungated_support_score"] = row.get("support_score", 0)

        row["support_status"] = (
            "source_needs_review"
            if verification_status not in {"", "unknown", "none"}
            else "source_verification_unknown"
        )
        row["support_score"] = min(support_score, 49)
        row["evidence_used"] = "unverified_source_metadata"
        row["partial_support"] = False
        row["score_explanation"] = (
            "The extracted claim appears related to the matched source, but the source itself "
            f"has verification status '{verification_status or 'unknown'}'. Claim support is capped "
            "until the reference is verified or accepted after manual review."
        )
        row["note"] = (
            "Potential support detected, but the matched source is not trusted. "
            "Review the reference before accepting this claim-support result."
        )

    return row


def _apply_verification_gate_to_claim_rows(rows, verification_rows=None):
    citation_lookup, reference_lookup = _build_verification_claim_lookups(verification_rows or [])
    gated = []

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        row = _attach_verification_status_to_claim_row(row, citation_lookup, reference_lookup)
        row = _apply_verification_gate_to_claim_row(row)
        gated.append(row)

    return gated


def _fallback_claim_support_rows(result, verification_rows):
    """
    Claim-support fallback with real claim extraction from main_text.

    It extracts the claim sentence around the citation, scores title-level
    relatedness, then applies a verification gate so untrusted sources cannot
    appear as strong claim support.
    """
    rows = []

    main_text = result.get("main_text", "") or ""
    sentences = _split_sentences(main_text)

    c2r_rows = result.get("reconciliation_intext_to_reference", []) or []

    citation_lookup, reference_lookup = _build_verification_claim_lookups(verification_rows)

    for row in c2r_rows:
        citation = (
            row.get("in_text")
            or row.get("citation")
            or row.get("citation_in_text")
            or ""
        )

        citation = str(citation or "").strip()

        matched_reference = (
            row.get("matched_reference")
            or row.get("reference")
            or row.get("matched_title")
            or ""
        )

        lookup = (
            citation_lookup.get(citation.lower())
            or reference_lookup.get(_norm_claim_lookup_key(matched_reference))
            or {}
        )

        if not matched_reference:
            matched_reference = lookup.get("reference", "")

        sentence = _find_sentence_for_citation(sentences, citation)
        claim = _extract_claim_from_sentence(sentence, citation)

        if not claim:
            claim = "Claim could not be extracted automatically. Review the cited sentence manually."

        alt_sources = _safe_alternative_sources(
            claim=claim,
            citation=citation,
            current_source_title=matched_reference,
            top_k=3
        )

        support = _score_claim_support_for_worker(claim, matched_reference)

        verification_status = str(lookup.get("status") or "unknown").strip().lower()

        claim_row = {
            "citation": citation,
            "claim": claim,
            "source_title": matched_reference[:250] if matched_reference else "Matched source not available",
            "matched_source": matched_reference,
            "support_status": support.get(
                "status",
                "claim_extracted_review_required"
                if claim.startswith("Claim could not") is False
                else "manual_review_required"
            ),
            "support_score": support.get("score", 0),
            "evidence_used": support.get("evidence_used", "title_only"),
            "title_overlap": support.get("title_overlap", 0),
            "abstract_overlap": support.get("abstract_overlap", 0),
            "keyword_overlap": support.get("keyword_overlap", 0),
            "direction_overlap": support.get("direction_overlap", 0),
            "relation_overlap": support.get("relation_overlap", 0),
            "partial_support": support.get("partial_support", False),
            "concept_matches": support.get("concept_matches", []),
            "score_explanation": support.get("score_explanation", "Title-only worker fallback scoring."),
            "doi": lookup.get("doi", ""),
            "note": "Claim extracted from manuscript context. Human review is still required to confirm source support.",
            "citation_match_status": verification_status,
            "verification_status": verification_status,
            "source_verification_status": verification_status,
            "alternative_sources": alt_sources
        }

        rows.append(_apply_verification_gate_to_claim_row(claim_row))

    # If c2r rows are available, return gated rows immediately.
    if rows:
        return rows

    # If c2r rows are unavailable, fall back to verification rows.
    for row in verification_rows or []:
        citation = (
            row.get("citation")
            or row.get("in_text")
            or row.get("citation_in_text")
            or ""
        )

        reference = (
            row.get("reference")
            or row.get("original_reference")
            or row.get("matched_title")
            or row.get("title")
            or row.get("source_title")
            or ""
        )

        sentence = _find_sentence_for_citation(sentences, citation)
        claim = _extract_claim_from_sentence(sentence, citation)

        if not claim:
            claim = "Claim could not be extracted automatically. Review the cited sentence manually."

        source_title = row.get("matched_title") or reference or "Source title not available"

        alt_sources = _safe_alternative_sources(
            claim=claim,
            citation=citation,
            current_source_title=source_title,
            top_k=3
        )

        support = _score_claim_support_for_worker(claim, source_title)
        verification_status = str(row.get("status") or "unknown").strip().lower()

        claim_row = {
            "citation": citation,
            "claim": claim,
            "source_title": source_title[:250],
            "matched_source": reference,
            "support_status": support.get(
                "status",
                "claim_extracted_review_required"
                if claim.startswith("Claim could not") is False
                else "manual_review_required"
            ),
            "support_score": support.get("score", 0),
            "evidence_used": support.get("evidence_used", "title_only"),
            "title_overlap": support.get("title_overlap", 0),
            "abstract_overlap": support.get("abstract_overlap", 0),
            "keyword_overlap": support.get("keyword_overlap", 0),
            "direction_overlap": support.get("direction_overlap", 0),
            "relation_overlap": support.get("relation_overlap", 0),
            "partial_support": support.get("partial_support", False),
            "concept_matches": support.get("concept_matches", []),
            "score_explanation": support.get("score_explanation", "Title-only worker fallback scoring."),
            "doi": row.get("doi", ""),
            "note": "Claim extracted from manuscript context. Human review is still required to confirm source support.",
            "citation_match_status": verification_status,
            "verification_status": verification_status,
            "source_verification_status": verification_status,
            "alternative_sources": alt_sources
        }

        rows.append(_apply_verification_gate_to_claim_row(claim_row))

    return rows

def _normalise_references_for_verification(result):
    refs = result.get("references_raw", []) or []

    if refs:
        return refs

    if recover_references_for_verification:
        try:
            recovered = recover_references_for_verification(
                result.get("main_text", ""),
                style_hint="apa"
            )
            if recovered:
                result["references_raw"] = recovered
                result.setdefault("summary", {})["reference_entries_found"] = len(recovered)
                return recovered
        except Exception as e:
            print(f"[VERIFY WORKER] Reference recovery failed: {e}")

    return []
def _is_real_claim_row(row):
    claim = str(row.get("claim") or row.get("claim_extracted") or "").strip()

    if not claim:
        return False

    fallback_phrases = [
        "Claim extraction not available",
        "Review the cited sentence manually",
        "Fallback row generated"
    ]

    return not any(p.lower() in claim.lower() for p in fallback_phrases)


def _build_claim_support_safe(result, verification_rows):
    """
    Claim Support Lite for the main verification job.
    By default, this returns fast fallback rows with title-only scoring.
    The slower full claim checker should run in deep_enrichment, not here.
    """
    fallback_rows = _fallback_claim_support_rows(result, verification_rows)

    if not RUN_REAL_CLAIM_CHECK_IN_VERIFY:
        return _apply_verification_gate_to_claim_rows(
            _enhance_claim_support_scores(fallback_rows),
            verification_rows
        )

    executor = None

    try:
        executor = ThreadPoolExecutor(max_workers=1)

        future = executor.submit(build_claim_support_rows, result)
        claim_rows = future.result(timeout=CLAIM_SUPPORT_TIMEOUT)

        if isinstance(claim_rows, list) and claim_rows:
            real_count = sum(1 for row in claim_rows if _is_real_claim_row(row))

            if real_count > 0:
                print(f"[VERIFY WORKER] Real claim-support rows generated: {real_count}/{len(claim_rows)}")

                for row in claim_rows:
                    row.setdefault("fallback", False)

                return _apply_verification_gate_to_claim_rows(
                    _enhance_claim_support_scores(claim_rows),
                    verification_rows
                )

        print("[VERIFY WORKER] Claim-support checker returned no real extracted claims. Using fallback rows.")
        return _apply_verification_gate_to_claim_rows(
            _enhance_claim_support_scores(fallback_rows),
            verification_rows
        )

    except FutureTimeoutError:
        print(f"[VERIFY WORKER] Claim-support timed out after {CLAIM_SUPPORT_TIMEOUT}s. Using fallback rows.")
        try:
            future.cancel()
        except Exception:
            pass
        return _apply_verification_gate_to_claim_rows(
            _enhance_claim_support_scores(fallback_rows),
            verification_rows
        )

    except Exception as e:
        print(f"[VERIFY WORKER] Claim-support failed: {e}. Using fallback rows.")
        return _apply_verification_gate_to_claim_rows(
            _enhance_claim_support_scores(fallback_rows),
            verification_rows
        )

    finally:
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def similarity_ratio(a, b):
    """Calculate similarity ratio between two strings."""
    if not a or not b:
        return 0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def extract_year_from_text(text):
    """Extract year from citation or reference text, including suffixes like 2020a."""
    if not text:
        return None

    match = re.search(r'\b((?:19|20)\d{2}[a-z]?)\b', str(text), flags=re.I)
    return match.group(1) if match else None

def year_to_int(year):
    """Convert 2020a or 2020 to 2020."""
    if not year:
        return None
    m = re.search(r'(?:19|20)\d{2}', str(year))
    return int(m.group(0)) if m else None


def normalize_name_text(text):
    """Normalise author/institution names safely."""
    text = str(text or "")
    text = text.replace("‐", "-").replace("–", "-").replace("—", "-")
    text = text.replace("’", "'")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_org_author(author_part):
    """Clean organisational author names such as Bank of Ghana, OECD, IMF."""
    org = normalize_name_text(author_part)
    org = re.sub(r"\(?\s*$", "", org).strip()
    org = org.strip(" .,(;:")

    # Remove leftover year punctuation if any slipped in
    org = re.sub(r"\(\s*$", "", org).strip()
    org = org.strip(" .,(;:")

    return org


def looks_like_institutional_author(name):
    """Detect likely organisational or institutional author."""
    if not name:
        return False

    n = normalize_name_text(name)
    lower = n.lower()

    institutional_terms = {
        "bank", "reserve", "ministry", "department", "office", "bureau",
        "authority", "commission", "organisation", "organization",
        "university", "institute", "fund", "chamber", "conference",
        "nations", "monetary", "oecd", "imf", "world bank", "unctad"
    }

    if n.isupper() and len(n) >= 2:
        return True

    return any(term in lower for term in institutional_terms)


def looks_like_merged_reference(ref):
    """Detect references that appear to contain two or more joined entries."""
    if not ref:
        return False

    text = normalize_name_text(ref)

    # Multiple APA-style author-year starts inside one entry
    starts = re.findall(
        r"\b[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,7}\.\s*\((?:19|20)\d{2}",
        text
    )

    # Multiple years in brackets can indicate joined refs, especially with long text
    year_brackets = re.findall(r"\((?:19|20)\d{2}[a-z]?(?:,\s*[A-Za-z]+)?\)", text)

    return len(starts) >= 2 or (len(year_brackets) >= 2 and len(text) > 250)


def force_review_only(suggestion):
    """Ensure every suggestion is review-only and not auto-applied."""
    suggestion["fix_type"] = "review_required"
    suggestion["action"] = "review_required"
    suggestion["apply"] = None
    return suggestion


def make_suggestion(original, suggested, confidence, issue_type, reason, category, field=""):
    """Create one standard review-only suggestion."""
    return force_review_only({
        "original": original,
        "suggested": suggested,
        "confidence": confidence,
        "issue_type": issue_type,
        "reason": reason,
        "fix_type": "review_required",
        "action": "review_required",
        "category": category,
        "field": field,
        "apply": None
    })

def extract_authors_from_citation(citation):
    """
    Extract author surnames from in-text citation.
    Handles:
    (Adam, 2020)
    (Adam & Mensah, 2020)
    Adam and Mensah (2020)
    Adam et al. (2020)
    (World Bank, 2020)
    """
    if not citation:
        return []

    text = str(citation).strip()

    # Remove year and brackets
    text = re.sub(r'\b(?:19|20)\d{2}[a-z]?\b', '', text, flags=re.I)
    text = re.sub(r'[()]', ' ', text)
    text = re.sub(r'\bet\s+al\.?\b', '', text, flags=re.I)
    text = text.replace('&', ' and ')
    text = re.sub(r'\s+', ' ', text).strip(" ,;.")

    if not text:
        return []

    parts = re.split(r'\s+and\s+|;', text, flags=re.I)

    authors = []
    for part in parts:
        part = part.strip(" ,.")
        if not part:
            continue

        # For names such as "van der Merwe", keep last token as surname fallback
        tokens = part.split()
        if len(tokens) >= 2 and tokens[0].lower() in {"van", "von", "de", "da", "di", "der", "al"}:
            surname = " ".join(tokens)
        else:
            surname = tokens[-1]

        surname = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ'\- ]", "", surname).strip()
        if len(surname) >= 2:
            authors.append(surname)

    # Remove duplicates while preserving order
    seen = set()
    clean = []
    for a in authors:
        key = a.lower()
        if key not in seen:
            seen.add(key)
            clean.append(a)

    return clean


def extract_authors_from_reference(ref_text):
    """
    Extract author surnames or institutional authors from reference list entry.
    Avoids malformed outputs like 'Bank of Ghana. ('.
    """
    if not ref_text:
        return []

    text = normalize_name_text(ref_text)

    year_match = re.search(r"\b(?:19|20)\d{2}[a-z]?\b", text, flags=re.I)
    if not year_match:
        return []

    author_part = text[:year_match.start()].strip()
    author_part = re.sub(r"\s+", " ", author_part)
    author_part = author_part.strip(" .,(;:")

    # Institutional author fallback first
    if looks_like_institutional_author(author_part):
        org = clean_org_author(author_part)
        return [org] if org else []

    author_part_for_people = author_part.replace("&", ",")

    # APA personal author pattern: Surname, Initials
    surnames = re.findall(
        r"\b([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+)\s*,\s*(?:[A-Z]\.?\s*)+",
        author_part_for_people
    )

    if surnames:
        seen = set()
        clean = []
        for s in surnames:
            s = normalize_name_text(s)
            key = s.lower()
            if key not in seen:
                seen.add(key)
                clean.append(s)
        return clean

    # Final fallback, only for short organisation-like author part
    org = clean_org_author(author_part)
    if org and len(org.split()) <= 8:
        return [org]

    return []


def extract_full_author_string_from_reference(ref_text):
    """Extract the full author string from reference for replacement."""
    if not ref_text:
        return ""
    
    year_match = re.search(r'\b(19|20)\d{2}\b', ref_text)
    if not year_match:
        return ""
    
    return ref_text[:year_match.start()].strip()


def is_narrative_citation(citation):
    """Check if citation is narrative (Adam and Mensah, 2020)."""
    if not citation:
        return False
    # Narrative: Author names BEFORE the parenthesis with year inside
    return bool(re.match(r'^[A-Z][a-z]', citation)) and '(' in citation


def is_parenthetical_citation(citation):
    """Check if citation is parenthetical (Adam & Mensah, 2020)."""
    if not citation:
        return False
    return citation.startswith('(')


# ============================================================
# MISMATCH DETECTION FUNCTIONS
# ============================================================

def detect_year_mismatch(citation, reference):
    """Detect year mismatch between citation and reference."""
    citation_year = extract_year_from_text(citation)
    ref_year = extract_year_from_text(reference)
    
    if citation_year and ref_year and citation_year != ref_year:
        return {
            "citation_year": citation_year,
            "ref_year": ref_year,
            "mismatch": True
        }
    return {"mismatch": False}

def find_reference_by_author_different_year(citation, references_raw):
    """
    Conservative unmatched year-mismatch detector.
    Only flags likely year errors when:
    - first author matches very strongly
    - second author also matches when present
    - the year difference is small
    This avoids false matches in long theses.
    """
    citation_authors = extract_authors_from_citation(citation)
    citation_year = extract_year_from_text(citation)

    if not citation_authors or not citation_year:
        return None

    cit_year_int = year_to_int(citation_year)
    if not cit_year_int:
        return None

    cit_first = citation_authors[0].lower()
    cit_second = citation_authors[1].lower() if len(citation_authors) >= 2 else ""

    best = None
    best_score = 0

    for ref in references_raw or []:
        ref_authors = extract_authors_from_reference(ref)
        ref_year = extract_year_from_text(ref)

        if not ref_authors or not ref_year:
            continue

        ref_year_int = year_to_int(ref_year)
        if not ref_year_int:
            continue

        if ref_year == citation_year:
            continue

        year_diff = abs(cit_year_int - ref_year_int)

        # Only allow small year differences for unmatched year suggestions.
        # Larger differences are usually different publications, not citation-year errors.
        if year_diff > 2:
            continue

        ref_first = ref_authors[0].lower()
        ref_second = ref_authors[1].lower() if len(ref_authors) >= 2 else ""

        cit_key = re.sub(r"[^a-z0-9]", "", cit_first)
        ref_key = re.sub(r"[^a-z0-9]", "", ref_first)

        first_score = 1.0 if cit_key == ref_key else similarity_ratio(cit_first, ref_first)

        if first_score < 0.95:
            continue

        # If citation has two authors, require the second author too.
        if cit_second:
            second_score = similarity_ratio(cit_second, ref_second)
            if second_score < 0.90:
                continue

        # Do not make unmatched year suggestions for et al. citations.
        # Too risky without title/context verification.
        if re.search(r"\bet\s+al\.?\b", citation, flags=re.I):
            continue

        score = first_score

        if score > best_score:
            best_score = score
            best = {
                "reference": ref,
                "ref_year": ref_year,
                "citation_year": citation_year,
                "ref_authors": ref_authors,
                "citation_authors": citation_authors,
                "score": score
            }

    return best

def detect_author_mismatch(citation, reference):
    """
    Conservative author mismatch detection.
    Only flags clear spelling/order differences.
    Avoids institutional and ambiguous partial-match false positives.
    """
    citation_authors = extract_authors_from_citation(citation)
    ref_authors = extract_authors_from_reference(reference)

    if not citation_authors or not ref_authors:
        return {"mismatch": False}

    cit_first = citation_authors[0]
    ref_first = ref_authors[0]

    cit_first = citation_authors[0]
    ref_first = ref_authors[0]
    
    def comparable_author_key(x):
        return re.sub(r"[^a-z0-9]", "", str(x).lower())
    
    if comparable_author_key(cit_first) == comparable_author_key(ref_first):
        return {"mismatch": False}
    
    # Exact case-insensitive match
    if [a.lower() for a in citation_authors] == [a.lower() for a in ref_authors]:
        return {"mismatch": False}

    # Do not flag institutional author partial matches, e.g. Ghana vs Bank of Ghana
    if looks_like_institutional_author(cit_first) or looks_like_institutional_author(ref_first):
        if cit_first.lower() in ref_first.lower() or ref_first.lower() in cit_first.lower():
            return {"mismatch": False}

    # Avoid reducing De Silva to Silva, Olasehinde-Williams to Williams
    if cit_first.lower().endswith(ref_first.lower()) or ref_first.lower().endswith(cit_first.lower()):
        return {"mismatch": False}

    # Order mismatch, only if same number of authors and at least two authors
    if (
        len(citation_authors) == len(ref_authors)
        and len(citation_authors) >= 2
        and sorted(a.lower() for a in citation_authors) == sorted(a.lower() for a in ref_authors)
        and [a.lower() for a in citation_authors] != [a.lower() for a in ref_authors]
    ):
        return {
            "mismatch": True,
            "type": "order_mismatch",
            "citation_authors": citation_authors,
            "ref_authors": ref_authors,
            "suggested_authors": ref_authors
        }

    # Spelling error, only for strong similarity and same author count
    if len(citation_authors) == len(ref_authors):
        for i, ca in enumerate(citation_authors):
            if i < len(ref_authors):
                ratio = similarity_ratio(ca, ref_authors[i])
                if 0.88 <= ratio < 1.0:
                    return {
                        "mismatch": True,
                        "type": "spelling_error",
                        "citation_authors": citation_authors,
                        "ref_authors": ref_authors,
                        "suggested_authors": ref_authors
                    }

    return {"mismatch": False}


def detect_et_al_misuse(citation, reference, style="apa"):
    """
    Conservative et al. misuse detection.
    Only flags when the reference clearly has one or two authors.
    """
    if not citation or not reference:
        return {"misuse": False}

    if style.lower() != "apa":
        return {"misuse": False}

    citation_uses_et_al = bool(re.search(r"\bet\s+al\.?\b", citation, flags=re.I))
    if not citation_uses_et_al:
        return {"misuse": False}

    year_match = re.search(r"\b(?:19|20)\d{2}[a-z]?\b", reference, flags=re.I)
    if not year_match:
        return {"misuse": False}

    author_part = reference[:year_match.start()]
    personal_author_count = len(re.findall(
        r"\b[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+,\s*(?:[A-Z]\.?\s*)+",
        author_part
    ))

    ref_authors = extract_authors_from_reference(reference)

    # If reference clearly has 3+ personal authors, et al. is acceptable
    if personal_author_count >= 3:
        return {"misuse": False}

    # Only flag when clearly one or two authors
    if 1 <= len(ref_authors) <= 2 and personal_author_count <= 2:
        return {
            "misuse": True,
            "citation_authors": extract_authors_from_citation(citation),
            "ref_authors": ref_authors,
            "reason": "APA 7 uses 'et al.' for three or more authors. This reference appears to have one or two authors, so review the in-text citation."
        }

    return {"misuse": False}

def build_citation_string(authors, year, citation_type="parenthetical", style="apa"):
    """Build a corrected in-text citation string."""
    authors = authors or []
    year = year or ""

    if not authors or not year:
        return ""

    if style.lower() == "apa":
        if len(authors) == 1:
            author_str_parenthetical = authors[0]
            author_str_narrative = authors[0]
        elif len(authors) == 2:
            author_str_parenthetical = f"{authors[0]} & {authors[1]}"
            author_str_narrative = f"{authors[0]} and {authors[1]}"
        else:
            author_str_parenthetical = f"{authors[0]} et al."
            author_str_narrative = f"{authors[0]} et al."
    else:
        if len(authors) == 1:
            author_str_parenthetical = authors[0]
            author_str_narrative = authors[0]
        elif len(authors) == 2:
            author_str_parenthetical = f"{authors[0]} & {authors[1]}"
            author_str_narrative = f"{authors[0]} and {authors[1]}"
        else:
            author_str_parenthetical = f"{authors[0]} et al."
            author_str_narrative = f"{authors[0]} et al."

    if citation_type == "parenthetical":
        return f"({author_str_parenthetical}, {year})"

    return f"{author_str_narrative} ({year})"


# ============================================================
# MAIN DETECTION FUNCTIONS FOR EACH SCENARIO
# ============================================================

def scenario_1_year_mismatch(c2r_rows):
    """Detect year mismatches (Scenario 1)."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if not in_text or not matched_ref:
            continue
        
        year_check = detect_year_mismatch(in_text, matched_ref)
        
        if year_check["mismatch"]:
            # Preserve original format
            suggested = in_text.replace(year_check["citation_year"], year_check["ref_year"])
            
            # Determine confidence based on year difference
            cy = year_to_int(year_check["citation_year"])
            ry = year_to_int(year_check["ref_year"])
            year_diff = abs(cy - ry) if cy and ry else 99
            if year_diff == 1:
                confidence = 0.95
            elif year_diff <= 3:
                confidence = 0.90
            else:
                confidence = 0.80
            
            suggestions.append(make_suggestion(
                original=in_text,
                suggested=suggested,
                confidence=confidence,
                issue_type="year_mismatch",
                reason=f"Year mismatch: '{year_check['citation_year']}' may need review against reference year '{year_check['ref_year']}'.",
                category="citation_accuracy"
            ))
    
    return suggestions

def scenario_1b_unmatched_year_mismatch(c2r_rows, references_raw):
    """
    Detect likely year mismatches for citations that were not matched
    because the citation year differs from the reference year.
    """
    suggestions = []

    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        status = row.get("status", "")

        # Only inspect citations that normal reconciliation did not match
        if not in_text:
            continue

        if matched_ref and status == "matched":
            continue

        candidate = find_reference_by_author_different_year(in_text, references_raw)

        if not candidate:
            continue

        citation_year = candidate["citation_year"]
        ref_year = candidate["ref_year"]

        suggested = in_text.replace(citation_year, ref_year)

        cy = year_to_int(citation_year)
        ry = year_to_int(ref_year)
        year_diff = abs(cy - ry) if cy and ry else 99

        if year_diff == 1:
            confidence = 0.88
        elif year_diff <= 3:
            confidence = 0.82
        else:
            confidence = 0.72

        suggestions.append(make_suggestion(
            original=in_text,
            suggested=suggested,
            confidence=confidence,
            issue_type="possible_year_mismatch_unmatched",
            reason=(
                f"The citation was not matched, but a reference with a similar author "
                f"uses year '{ref_year}' instead of '{citation_year}'. Review manually."
            ),
            category="citation_accuracy"
        ))

    return suggestions


def scenario_2_author_name_mismatch(c2r_rows, style="apa"):
    """Detect author name mismatches (spelling, missing parts like -Koduah)."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if not in_text or not matched_ref:
            continue
        
        author_check = detect_author_mismatch(in_text, matched_ref)
        
        if author_check["mismatch"]:
            # Determine citation type
            is_narrative = is_narrative_citation(in_text)
            citation_year = extract_year_from_text(in_text)
            
            # Build corrected citation
            # Build corrected citation
            suggested = build_citation_string(
                author_check["suggested_authors"],
                citation_year,
                "narrative" if is_narrative else "parenthetical",
                style
            )
            
            # Set confidence based on mismatch type
            if author_check["type"] == "spelling_error":
                confidence = 0.90
                reason = f"Author name spelling error: '{author_check['citation_authors'][0]}' should be '{author_check['suggested_authors'][0]}'"
            elif author_check["type"] == "partial_match":
                confidence = 0.85
                reason = f"Author name incomplete: '{author_check['citation_authors'][0]}' should be '{author_check['suggested_authors'][0]}'"
            else:
                confidence = 0.75
                reason = f"Author name mismatch: Expected '{author_check['suggested_authors'][0]}'"
            
            item = make_suggestion(
                original=in_text,
                suggested=suggested,
                confidence=confidence,
                issue_type="author_mismatch",
                reason=reason + " Review before changing.",
                category="citation_accuracy"
            )
            item["mismatch_type"] = author_check["type"]
            suggestions.append(item)
    
    return suggestions


def scenario_3_author_order_mismatch(c2r_rows, style="apa"):
    """Detect author order mismatches (Scenario 3)."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if not in_text or not matched_ref:
            continue
        
        citation_authors = extract_authors_from_citation(in_text)
        ref_authors = extract_authors_from_reference(matched_ref)
        
        # Check if same authors but different order
        if (citation_authors and ref_authors and 
            sorted(citation_authors) == sorted(ref_authors) and 
            citation_authors != ref_authors):
            
            is_narrative = is_narrative_citation(in_text)
            citation_year = extract_year_from_text(in_text)
            
            suggested = build_citation_string(
                ref_authors,
                citation_year,
                "narrative" if is_narrative else "parenthetical",
                style
            )
            
            suggestions.append(make_suggestion(
                original=in_text,
                suggested=suggested,
                confidence=0.80,
                issue_type="author_order_mismatch",
                reason="Author order may differ from the reference entry. Review before changing.",
                category="citation_accuracy"
            ))
    
    return suggestions


def scenario_4_combined_mismatch(c2r_rows, style="apa"):
    """Detect combined author and year mismatches (Scenario 4)."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if not in_text or not matched_ref:
            continue
        
        year_check = detect_year_mismatch(in_text, matched_ref)
        author_check = detect_author_mismatch(in_text, matched_ref)
        
        if year_check["mismatch"] and author_check["mismatch"]:
            is_narrative = is_narrative_citation(in_text)
            citation_year = extract_year_from_text(in_text)
            
            # Build corrected citation with both fixes
            suggested_author = build_citation_string(
                author_check["suggested_authors"],
                citation_year,
                "narrative" if is_narrative else "parenthetical",
                style
            )
            suggested = suggested_author.replace(citation_year, year_check["ref_year"])
            
            confidence = 0.85
            
            suggestions.append(make_suggestion(
                original=in_text,
                suggested=suggested,
                confidence=confidence,
                issue_type="author_year_mismatch",
                reason="Both author and year appear to differ from the matched reference. Review carefully before changing.",
                category="citation_accuracy"
            ))
    
    return suggestions


def scenario_5_et_al_misuse(c2r_rows, style="apa"):
    """Detect et al. misuse (Scenario 5)."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if not in_text or not matched_ref:
            continue
        
        et_al_check = detect_et_al_misuse(in_text, matched_ref, style)
        
        if et_al_check["misuse"]:
            is_narrative = is_narrative_citation(in_text)
            citation_year = extract_year_from_text(in_text)
            
            suggested = build_citation_string(
                et_al_check["ref_authors"], 
                citation_year, 
                "narrative" if is_narrative else "parenthetical",
                style
            )
            
            suggestions.append(make_suggestion(
                original=in_text,
                suggested=suggested,
                confidence=0.75,
                issue_type="et_al_misuse",
                reason=et_al_check["reason"],
                category="citation_accuracy"
            ))
                
    return suggestions


def scenario_6_potential_wrong_reference(c2r_rows):
    """Detect potential wrong reference matches (Scenario 6 - Low confidence)."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        status = row.get("status", "")
        
        if not in_text or not matched_ref:
            continue
        
        # Check for potential wrong reference (status might be "likely" or "needs_review")
        if status in ["likely", "needs_review"]:
            citation_authors = extract_authors_from_citation(in_text)
            ref_authors = extract_authors_from_reference(matched_ref)
            citation_year = extract_year_from_text(in_text)
            ref_year = extract_year_from_text(matched_ref)
            
            # If authors match but years differ significantly
            if citation_authors and ref_authors and citation_authors[0] == ref_authors[0]:
                if citation_year and ref_year and citation_year != ref_year:
                    cy = year_to_int(citation_year)
                    ry = year_to_int(ref_year)
                    year_diff = abs(cy - ry) if cy and ry else 99
                    if year_diff > 3:
                        suggestions.append(make_suggestion(
                            original=in_text,
                            suggested="Verify the matched reference manually",
                            confidence=0.60,
                            issue_type="potential_wrong_reference",
                            reason=f"The closest matched reference has a different year ({ref_year} vs {citation_year}). Review whether this is the correct source.",
                            category="citation_accuracy"
                        ))
    
    return suggestions


def detect_reference_quality_issues(references_raw, style="apa", enable_online_suggestions=False):
    """
    Safer reference-quality checks.
    Only flags high-value, lower-risk issues:
    - merged references
    - DOI format
    - HTTP to HTTPS
    - seriously incomplete reference

    Disabled due to false positives:
    - missing_period
    - missing_journal_details
    - missing page range
    """
    suggestions = []

    for ref in references_raw:
        original = ref or ""
        ref = normalize_name_text(original)

        if not ref:
            continue

        # 1. Merged references
        if looks_like_merged_reference(ref):
            suggestions.append(make_suggestion(
                original=original,
                suggested="Split into separate reference entries",
                confidence=0.90,
                issue_type="merged_references",
                reason="This entry appears to contain two or more references joined together.",
                category="reference_quality",
                field="reference_structure"
            ))
            continue

        year = extract_year_from_text(ref)

        # Normalise broken DOI URL spacing such as "https://doi. org/"
        # 2. DOI format and DOI spacing
        ref_for_doi = ref
        
        # Fix broken DOI URL spacing such as "https://doi. org/"
        ref_for_doi = re.sub(
            r"https?://doi\.\s*org/",
            "https://doi.org/",
            ref_for_doi,
            flags=re.I
        )
        
        # Convert dx.doi.org to doi.org
        ref_for_doi = re.sub(
            r"https?://dx\.doi\.org/",
            "https://doi.org/",
            ref_for_doi,
            flags=re.I
        )
        
        # Convert DOI labels to DOI URL
        # Handles:
        # DOI: 10.xxxx
        # doi:10.xxxx
        # DOI http://dx.doi.org/10.xxxx
        # DOI: org/10.xxxx
        doi_label_match = re.search(
            r"\bdoi\s*:?\s*(?:https?://(?:dx\.)?doi\.org/)?(?:org/)?(10\.\d{4,9}/[^\s\)]*)",
            ref_for_doi,
            flags=re.I
        )
        
        if doi_label_match:
            doi = doi_label_match.group(1).rstrip(".,")
            cleaned = (
                ref_for_doi[:doi_label_match.start()]
                + f"https://doi.org/{doi}"
                + ref_for_doi[doi_label_match.end():]
            )
        
            suggestions.append(make_suggestion(
                original=original,
                suggested=cleaned,
                confidence=0.95,
                issue_type="doi_format",
                reason="The DOI format appears non-standard. Review the DOI URL before applying.",
                category="reference_quality",
                field="doi"
            ))
        
        elif ref_for_doi != ref:
            suggestions.append(make_suggestion(
                original=original,
                suggested=ref_for_doi,
                confidence=0.95,
                issue_type="doi_spacing",
                reason="The DOI URL appears to contain spacing or dx.doi.org formatting. Review before applying.",
                category="reference_quality",
                field="doi"
            ))
        
        else:
            # Raw DOI without DOI URL
            raw_doi_match = re.search(r"(?<!doi\.org/)\b10\.\d{4,9}/[^\s\)]*", ref_for_doi, flags=re.I)
        
            if raw_doi_match and style.lower() == "apa":
                doi = raw_doi_match.group(0).rstrip(".,")
                cleaned = ref_for_doi.replace(doi, f"https://doi.org/{doi}")
        
                suggestions.append(make_suggestion(
                    original=original,
                    suggested=cleaned,
                    confidence=0.95,
                    issue_type="doi_format",
                    reason="APA 7 recommends DOI in URL format. Review before applying.",
                    category="reference_quality",
                    field="doi"
                ))
        # 3. HTTP to HTTPS
        if "http://" in ref:
            suggestions.append(make_suggestion(
                original=original,
                suggested=ref.replace("http://", "https://"),
                confidence=0.90,
                issue_type="http_to_https",
                reason="The reference uses HTTP. Review whether HTTPS is available and appropriate.",
                category="reference_quality",
                field="url"
            ))

        # 4. Seriously incomplete reference only
        has_enough_length = len(ref) >= 45
        has_title_after_year = False

        if year and str(year) in ref:
            after_year = ref.split(str(year), 1)[-1]
            has_title_after_year = len(after_year.strip(" .,)")) >= 15

        if not year or not has_enough_length or not has_title_after_year:
            suggestions.append(make_suggestion(
                original=original,
                suggested="Review reference manually",
                confidence=0.80,
                issue_type="seriously_incomplete_reference",
                reason="The reference appears to be missing a year, title, or essential bibliographic content.",
                category="reference_quality",
                field="bibliographic_details"
            ))

        # Missing DOI online suggestions remain disabled for speed and safety
        # Do not add missing_period, missing_journal_details, or missing page-range suggestions here.

    return dedupe_suggestions_by_priority(suggestions)

       
def dedupe_suggestions_by_priority(suggestions):
    """
    Keep the strongest suggestion for each original text.
    Prevents combined author-year mismatch from being replaced by weaker year-only mismatch.
    """
    priority = {
        "author_year_mismatch": 1,
        "potential_wrong_reference": 2,
        "author_mismatch": 3,
        "year_mismatch": 4,
        "possible_year_mismatch_unmatched": 4,
        "author_order_mismatch": 5,
        "et_al_misuse": 6,
        "missing_doi": 7,
        "doi_format": 8,
        "doi_spacing": 8,
        "incomplete_reference": 9,
        "missing_journal_details": 10,
        "http_to_https": 11,
        "missing_period": 12,
    }

    best = {}

    for s in suggestions:
        original = s.get("original", "")
        if not original:
            continue

        issue_type = s.get("issue_type", "")
        new_rank = priority.get(issue_type, 99)
        new_conf = s.get("confidence", 0)

        if original not in best:
            best[original] = s
            continue

        old = best[original]
        old_rank = priority.get(old.get("issue_type", ""), 99)
        old_conf = old.get("confidence", 0)

        if new_rank < old_rank or (new_rank == old_rank and new_conf > old_conf):
            best[original] = s

    return list(best.values())
# ============================================================
# MAIN PROCESS_DOCUMENT FUNCTION
# ============================================================

def process_document(job_id, filename, style="apa", enable_autofix=False):
    """Process a document - runs in background"""
    print(f"🔥 Processing job {job_id}: {filename}")
    print(f"📋 enable_autofix flag received: {enable_autofix}")
    
    enable_autofix = bool(enable_autofix)
    print(f"📋 Using enable_autofix: {enable_autofix}")
    
    # Load file from Redis
    file_content = redis_conn.get(f"file:{job_id}")
    
    print(f"📦 File size from Redis: {len(file_content) if file_content else 0} bytes")
    
    if not file_content:
        raise Exception(f"❌ File not found in Redis for job {job_id}")

    try:
        # DB CONNECTION
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        
        cursor.execute(
            "UPDATE jobs SET status = 'processing', started_at = NOW() WHERE job_id = %s",
            (job_id,)
        )
        conn.commit()

        print("⚡ Running run_crosscheck_with_autofix...")

        result = run_crosscheck_with_autofix(
            file_bytes=file_content,
            filename=filename,
            style=style,
            verify_online=False,
            enable_autofix=enable_autofix
        )

        print("🔍 === RESULT DEBUG ===")
        print("🔍 'autofix' in result:", "autofix" in result)
        
        # Ensure result has proper structure
        if "autofix" not in result:
            result["autofix"] = {
                "enabled": True,
                "suggestions": {
                    "citations": [],
                    "references": []
                }
            }
        
        # Get data for analysis
        c2r_rows = result.get("reconciliation_intext_to_reference", [])
        references_raw = result.get("references_raw", [])
        
        print(f"📊 Found {len(c2r_rows)} citation-reference pairs")
        print(f"📊 Found {len(references_raw)} references")
        
        # COLLECT ALL SUGGESTIONS BY SCENARIO
        all_suggestions = []
        
        # Scenario 4 first: Combined author + year mismatch
        combined_suggestions = scenario_4_combined_mismatch(c2r_rows, style)
        all_suggestions.extend(combined_suggestions)
        print(f"🔀 Scenario 4 - Combined mismatches: {len(combined_suggestions)}")
        
        # Scenario 6: Potential wrong reference
        wrong_ref_suggestions = scenario_6_potential_wrong_reference(c2r_rows)
        all_suggestions.extend(wrong_ref_suggestions)
        print(f"⚠️ Scenario 6 - Potential wrong references: {len(wrong_ref_suggestions)}")
        
        # Scenario 2: Author name mismatch (spelling, missing parts)
        author_suggestions = scenario_2_author_name_mismatch(c2r_rows, style)
        all_suggestions.extend(author_suggestions)
        print(f"👤 Scenario 2 - Author name mismatches: {len(author_suggestions)}")
        
        # Scenario 1: Year mismatch
        year_suggestions = scenario_1_year_mismatch(c2r_rows)
        all_suggestions.extend(year_suggestions)
        print(f"📅 Scenario 1 - Year mismatches: {len(year_suggestions)}")
        # Scenario 1b: Year mismatch among unmatched citations
        unmatched_year_suggestions = scenario_1b_unmatched_year_mismatch(
            c2r_rows,
            references_raw
        )
        all_suggestions.extend(unmatched_year_suggestions)
        print(f"📅 Scenario 1b - Unmatched year mismatches: {len(unmatched_year_suggestions)}")
        
        # Scenario 3: Author order mismatch
        order_suggestions = scenario_3_author_order_mismatch(c2r_rows, style)
        all_suggestions.extend(order_suggestions)
        print(f"🔄 Scenario 3 - Author order mismatches: {len(order_suggestions)}")
        
        # Scenario 5: Et al. misuse
        # Scenario 5: Et al. misuse
        # Disabled for now because et al. suggestions require highly reliable author extraction.
        et_al_suggestions = []
        print("📝 Scenario 5 - Et al. misuse: disabled")

        # Reference quality issues
        ref_suggestions = detect_reference_quality_issues(
            references_raw,
            style=style,
            enable_online_suggestions=False
        )
        all_suggestions.extend(ref_suggestions)
        print(f"📚 Reference quality issues: {len(ref_suggestions)}")
        
        # Remove duplicates (by original text)
        unique_suggestions = dedupe_suggestions_by_priority(all_suggestions)
        unique_suggestions = [force_review_only(s) for s in unique_suggestions]
        
        print(f"💡 TOTAL UNIQUE SUGGESTIONS: {len(unique_suggestions)}")
        
        # Count by category
        categories = Counter(s.get("category", "other") for s in unique_suggestions)
        for cat, count in categories.items():
            print(f"  - {cat}: {count}")
        
        # Print details of each suggestion for debugging
        for i, s in enumerate(unique_suggestions):
            print(f"  Suggestion {i+1}: [{s.get('issue_type')}] {s.get('original')} -> {s.get('suggested')}")
        
        # Add suggestions to result
        if unique_suggestions:
            citation_suggestions = [
                s for s in unique_suggestions
                if s.get("category") not in {"reference", "reference_quality"}
            ]
        
            reference_suggestions = [
                s for s in unique_suggestions
                if s.get("category") in {"reference", "reference_quality"}
            ]
        
            result["autofix"]["suggestions"]["citations"] = citation_suggestions
            result["autofix"]["suggestions"]["references"] = reference_suggestions
        
            result["suggestions"] = result["autofix"]["suggestions"]
            
            # Add statistics
            result["autofix"]["statistics"] = {
                "total": len(unique_suggestions),
                "citation_accuracy_total": len(citation_suggestions),
                "reference_quality_total": len(reference_suggestions),
                "review_high_confidence": len([s for s in unique_suggestions if s.get("confidence", 0) >= 0.85]),
                "review_medium_confidence": len([s for s in unique_suggestions if 0.70 <= s.get("confidence", 0) < 0.85]),
                "review_low_confidence": len([s for s in unique_suggestions if s.get("confidence", 0) < 0.70]),
                "by_category": dict(categories),
                "by_issue_type": dict(Counter(s.get("issue_type", "other") for s in unique_suggestions))
            }
            
            print(f"✅ Added {len(unique_suggestions)} suggestions to result")
        else:
            print("⚠️ No suggestions generated")
        
        # Save result
        cursor.execute(
            "UPDATE jobs SET status = 'completed', result = %s, completed_at = NOW() WHERE job_id = %s",
            (json.dumps(result), job_id)
        )
        conn.commit()
        
        cursor.close()
        conn.close()
        
        # Cache result
        redis_conn.setex(f"result:{job_id}", 3600, json.dumps(result))
        redis_conn.delete(f"file:{job_id}")
        
        print(f"✅ Completed job {job_id}")
        return result
        
    except Exception as e:
        print(f"❌ Failed job {job_id}: {e}")
        import traceback
        traceback.print_exc()
        
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE jobs SET status = 'failed', error = %s WHERE job_id = %s",
                (str(e), job_id)
            )
            conn.commit()
            cursor.close()
            conn.close()
        except:
            pass
        
        raise e


# ============================================================
# COMMERCIAL DEEP ENRICHMENT JOB
# ============================================================

def _enqueue_deep_enrichment(job_id, style="apa", scope="weak_only"):
    """Queue expensive enrichment after the usable dashboard is already ready."""
    if not ENQUEUE_DEEP_ENRICHMENT_AFTER_VERIFY:
        return None

    try:
        deep_queue = Queue("deep_enrichment", connection=redis_conn)
        rq_job = deep_queue.enqueue(
            "worker.process_deep_enrichment",
            job_id,
            style,
            scope,
            job_timeout=10800,
            result_ttl=86400,
            failure_ttl=86400,
        )
        print(f"[DEEP ENRICHMENT] Queued enrichment job {rq_job.id} for {job_id}")
        return rq_job.id
    except Exception as e:
        print(f"[DEEP ENRICHMENT] Could not queue enrichment for {job_id}: {e}")
        return None


def _set_enrichment_meta(result, **kwargs):
    if result is None:
        result = {}

    enrichment = result.get("enrichment") or {}
    for key, value in kwargs.items():
        if value is not None:
            enrichment[key] = value

    result["enrichment"] = enrichment
    return result
WEAK_CLAIM_STATUSES = {
    "weak_or_unclear",
    "insufficient_evidence",
    "no_evidence_found",
    "no_source_found",
    "source_not_found",
    "matched_source_not_available",
    "manual_review_required",
    "insufficient_title_overlap",
    "title_overlap_review_required",
    "claim_not_extracted",
}

WEAK_VERIFY_STATUSES = {
    "needs_review",
    "not_found",
    "offline",
}

def _should_enrich_claim_row(row, scope="weak_only"):
    """
    Select only claim-support rows that need deeper alternative-source lookup.
    Deep enrichment should run for weak, insufficient, no-evidence, and no-source cases.
    """
    if scope == "recovery_only":
        return False

    status = str(
        row.get("support_status")
        or row.get("status")
        or ""
    ).strip().lower()

    try:
        score = float(row.get("support_score") or row.get("score") or 0)
    except Exception:
        score = 0

    source_title = str(
        row.get("source_title")
        or row.get("matched_source")
        or row.get("matched_title")
        or ""
    ).strip().lower()

    no_source = (
        not source_title
        or source_title in {"no source found", "no source title available"}
        or source_title.startswith("matched source not available")
        or source_title.startswith("source title not available")
        or source_title.startswith("no source")
    )

    if row.get("alternative_sources"):
        return False

    return (
        status in WEAK_CLAIM_STATUSES
        or score < 20
        or no_source
    )


def _should_enrich_recovery_row(row, scope="weak_only"):
    if scope == "claim_only":
        return False

    if scope == "all_problem_rows":
        return not bool(row.get("deep_suggestions")) and not bool(row.get("enriched"))

    status = str(row.get("status") or "").strip().lower()

    already_enriched = bool(row.get("deep_suggestions")) or bool(row.get("enriched"))

    if already_enriched:
        return False

    return status in WEAK_VERIFY_STATUSES
DEEP_RECOVERY_STATUSES = {
    "needs_review",
    "not_found",
    "offline",
}

DEEP_CLAIM_STATUSES = {
    "weak_or_unclear",
    "insufficient_evidence",
    "no_evidence_found",
    "no_source_found",
    "source_not_found",
    "matched_source_not_available",
    "manual_review_required",
    "insufficient_title_overlap",
    "title_overlap_review_required",
}

def _should_deep_enrich_recovery_row(row, scope="weak_only"):
    """
    Deep enrichment for verification recovery should run only on
    needs_review, not_found, and offline rows.
    """
    if scope == "claim_only":
        return False

    status = str(row.get("status") or "").strip().lower()

    if status not in DEEP_RECOVERY_STATUSES:
        return False

    if row.get("deep_suggestions") or row.get("enriched") is True:
        return False

    return True


def _should_deep_enrich_claim_row(row, scope="weak_only"):
    """
    Deep enrichment for claim support should run only where the current
    source is weak, insufficient, missing, or no evidence was found.
    """
    if scope == "recovery_only":
        return False

    status = str(
        row.get("support_status")
        or row.get("status")
        or ""
    ).strip().lower()

    try:
        score = float(row.get("support_score") or row.get("score") or 0)
    except Exception:
        score = 0

    source_title = str(
        row.get("source_title")
        or row.get("matched_source")
        or row.get("matched_title")
        or ""
    ).strip().lower()

    no_source = (
        not source_title
        or source_title.startswith("matched source not available")
        or source_title.startswith("source title not available")
        or source_title.startswith("no source")
    )

    if row.get("alternative_sources"):
        return False

    return (
        status in DEEP_CLAIM_STATUSES
        or score < 20
        or no_source
    )
def process_deep_enrichment(job_id, style="apa", scope="weak_only", limit=None):
    """
    Expensive background enrichment. This runs after the dashboard is already usable.
    It adds deep Recovery suggestions and alternative claim-support sources without
    blocking verification completion.
    """
    print(f"[DEEP ENRICHMENT] Starting for job {job_id}")
    start_time = time.time()
    limit = int(limit or DEEP_ENRICHMENT_LIMIT)
    scope = str(scope or "weak_only").strip().lower()
    if scope not in {"weak_only", "recovery_only", "claim_only", "all_problem_rows"}:
        scope = "weak_only"
    result = _load_job_result(job_id)
    result = _set_enrichment_meta(
        result,
        state="running",
        scope=scope,
        message="Advanced Recovery and Claim Support enrichment is running.",
        started_at=now_iso(),
        deep_recovery_ready=False,
        deep_claim_support_ready=False,
        progress=0,
        total=0,
    )
    _save_job_result(job_id, result, status="completed")

    recovery = result.get("recovery") or {"missing_recovery": [], "verification_recovery": []}
    verification_rows = (result.get("online_verification") or {}).get("rows") or []
    missing_rows = recovery.get("missing_recovery") or []
    recovery_rows = recovery.get("verification_recovery") or []
    claim_rows = result.get("claim_support") or []

    if scope == "claim_only":
        missing_rows_to_enrich = []
    else:
        missing_rows_to_enrich = [
            row for row in missing_rows
            if not row.get("deep_suggestions") and row.get("enriched") is not True
        ]
    
    recovery_rows_to_enrich = [
        row for row in recovery_rows
        if _should_deep_enrich_recovery_row(row, scope=scope)
    ]
    
    claim_rows_to_enrich = [
        row for row in claim_rows
        if _should_deep_enrich_claim_row(row, scope=scope)
    ]
    
    total_work = (
        min(len(missing_rows_to_enrich), limit)
        + min(len(recovery_rows_to_enrich), limit)
        + min(len(claim_rows_to_enrich), limit)
    )
    
    done = 0
    
    result = _set_enrichment_meta(
        result,
        total=total_work,
        progress=done,
        scope=scope,
        message=f"Advanced enrichment queued for {total_work} problem rows.",
    )
    _save_job_result(job_id, result, status="completed")
    # Enrich missing in-text citation recovery rows first. These rows have no
    # matched reference, so the deep lookup relies mainly on the citation context.
    for idx, rec in enumerate(missing_rows_to_enrich[:limit], start=1):
        citation = str(rec.get("citation") or "").strip()
        source_row = {
            "citation": citation,
            "in_text": citation,
            "reference": "",
            "status": "missing_reference",
            "context": rec.get("context") or rec.get("citation_context") or "",
            "claim": rec.get("claim") or rec.get("suggested") or "",
            "suggestions": rec.get("suggestions") or [],
        }

        try:
            deep_suggestions = _deep_context_source_suggestions(
                source_row,
                result,
                target=DEEP_LOOKUP_TOP_K,
                include_reference=False
            )
            rec["deep_suggestions"] = deep_suggestions
            # Advanced enrichment must replace Recovery Lite, not silently reuse it.
            rec["suggestions"] = deep_suggestions
            rec["enriched"] = bool(deep_suggestions)
            rec["enrichment_type"] = "missing_citation_context_sources"
            rec["enrichment_note"] = _source_enrichment_note(deep_suggestions, target=DEEP_LOOKUP_TOP_K)
        except Exception as e:
            rec["suggestions"] = rec.get("suggestions") or _context_suggestions_for_missing_citation(citation, result, rec.get("count") or 1, target=3)
            rec["enriched"] = False
            rec["enrichment_error"] = str(e)

        done += 1
        if done % DEEP_ENRICHMENT_BATCH_SAVE == 0:
            recovery["missing_recovery"] = missing_rows
            result["recovery"] = recovery
            result = _set_enrichment_meta(
                result,
                progress=done,
                message=f"Advanced enrichment running: {done}/{total_work}",
                last_heartbeat=now_iso(),
            )
            _save_job_result(job_id, result, status="completed")

    recovery["missing_recovery"] = missing_rows
    result["recovery"] = recovery
    _save_job_result(job_id, result, status="completed")

    # Build lookup for richer recovery suggestions.
    verify_lookup = {}
    for row in verification_rows:
        key = (
            str(row.get("citation") or row.get("in_text") or row.get("citation_in_text") or "").strip().lower(),
            str(row.get("reference") or row.get("original_reference") or row.get("matched_title") or row.get("title") or row.get("source_title") or "").strip().lower(),
        )
        verify_lookup[key] = row

    for idx, rec in enumerate(recovery_rows_to_enrich[:limit], start=1):
        citation = str(rec.get("citation") or "").strip().lower()
        reference = str(rec.get("reference") or "").strip().lower()
        source_row = verify_lookup.get((citation, reference)) or rec

        try:
            deep_suggestions = _deep_context_source_suggestions(
                source_row,
                result,
                target=DEEP_LOOKUP_TOP_K,
                include_reference=True
            )
            rec["deep_suggestions"] = deep_suggestions
            # Advanced enrichment must replace Recovery Lite, not silently reuse it.
            rec["suggestions"] = deep_suggestions
            rec["enriched"] = bool(deep_suggestions)
            rec["enrichment_type"] = "verification_recovery_context_sources"
            rec["enrichment_note"] = _source_enrichment_note(deep_suggestions, target=DEEP_LOOKUP_TOP_K)
        except Exception as e:
            rec["enriched"] = False
            rec["enrichment_error"] = str(e)

        done += 1
        if done % DEEP_ENRICHMENT_BATCH_SAVE == 0:
            recovery["verification_recovery"] = recovery_rows
            result["recovery"] = recovery
            result = _set_enrichment_meta(
                result,
                progress=done,
                message=f"Advanced enrichment running: {done}/{total_work}",
                last_heartbeat=now_iso(),
            )
            _save_job_result(job_id, result, status="completed")

    recovery["verification_recovery"] = recovery_rows
    recovery["deep_recovery_ready"] = True
    result["recovery"] = recovery
    result = _set_enrichment_meta(
        result,
        deep_recovery_ready=True,
        progress=done,
        message="Deep Recovery enrichment completed. Enriching Claim Support alternatives...",
        last_heartbeat=now_iso(),
    )
    _save_job_result(job_id, result, status="completed")

    for idx, row in enumerate(claim_rows_to_enrich[:limit], start=1):
        citation = str(row.get("citation") or "").strip()
    
        claim = str(
            row.get("claim")
            or row.get("claim_extracted")
            or row.get("extracted_claim")
            or row.get("context")
            or ""
        ).strip()
    
        source_title = str(
            row.get("source_title")
            or row.get("matched_source")
            or row.get("matched_title")
            or ""
        ).strip()
    
        try:
            alt_sources = _safe_alternative_sources(
                claim=claim,
                citation=citation,
                current_source_title=source_title,
                top_k=DEEP_LOOKUP_TOP_K + 3,
                allow_external=True,
            )

            if len(alt_sources or []) < DEEP_LOOKUP_TOP_K:
                alt_sources = (alt_sources or []) + _deep_context_source_suggestions(
                    {
                        "citation": citation,
                        "in_text": citation,
                        "claim": claim,
                        "context": claim,
                        "source_title": source_title,
                        "matched_title": source_title,
                    },
                    result,
                    target=DEEP_LOOKUP_TOP_K + 3,
                    include_reference=False,
                )

            alt_sources = _dedupe_real_source_suggestions(
                alt_sources,
                target=DEEP_LOOKUP_TOP_K,
                exclude_title=source_title,
            )

            row["alternative_sources"] = alt_sources
            row["deep_suggestions"] = alt_sources
            row["suggestions"] = alt_sources
            row["enriched"] = bool(alt_sources)
            row["enrichment_type"] = "claim_support_alternative_sources"
            row["enrichment_note"] = _source_enrichment_note(alt_sources, target=DEEP_LOOKUP_TOP_K)
    
        except Exception as e:
            row["alternative_sources"] = row.get("alternative_sources") or []
            row["deep_suggestions"] = row.get("deep_suggestions") or row.get("alternative_sources") or []
            row["suggestions"] = row.get("suggestions") or row.get("deep_suggestions") or []
            row["enriched"] = False
            row["enrichment_error"] = str(e)
            row["enrichment_note"] = "Advanced enrichment failed for this claim-support row. Manual review is required."

        done += 1
        if done % DEEP_ENRICHMENT_BATCH_SAVE == 0:
            result["claim_support"] = claim_rows
            result = _set_enrichment_meta(
                result,
                progress=done,
                message=f"Advanced enrichment running: {done}/{total_work}",
                last_heartbeat=now_iso(),
            )
            _save_job_result(job_id, result, status="completed")

    result["claim_support"] = claim_rows
    elapsed = round(time.time() - start_time, 2)
    result = _set_enrichment_meta(
        result,
        state="completed",
        progress=total_work,
        total=total_work,
        percentage=100,
        deep_recovery_ready=True,
        deep_claim_support_ready=True,
        message=f"Advanced enrichment completed for scope: {scope}.",
        scope=scope,
        completed_at=now_iso(),
        processing_time_seconds=elapsed,
    )
    _save_job_result(job_id, result, status="completed")
    print(f"[DEEP ENRICHMENT] Completed for {job_id} in {elapsed}s")
    return result

# ============================================================
# VERIFICATION WORKER FUNCTION
# ============================================================
def process_verification(job_id, style="apa", enrich_metadata=False):
    """
    Durable online verification job.

    This replaces the old verify.py daemon-thread flow.
    It runs inside the RQ worker, persists progress after every chunk,
    updates PostgreSQL and Redis, and builds final dashboard tables.
    """
    print(f"🌐 Starting durable verification for job {job_id}")

    start_time = time.time()
    all_rows = []

    try:
        result = _load_job_result(job_id)
        refs = _normalise_references_for_verification(result)
        total = len(refs)

        if not total:
            result = _set_verification_meta(
                result,
                state="completed",
                progress=0,
                total=0,
                percentage=100,
                message=result.get("reference_detection_message", "No references extracted"),
                completed_at=now_iso(),
                final_tables_ready=True
            )

            result["final_tables_ready"] = True
            result["verification_completed_at"] = now_iso()

            result["online_verification"] = {
                "rows": [],
                "summary": _compute_verification_summary([])
            }

            result["recovery"] = {
                "missing_recovery": [],
                "verification_recovery": [],
                "note": "No references were available for recovery."
            }

            result["claim_support"] = []

            _save_job_result(job_id, result)
            return result

        result = _set_verification_meta(
            result,
            state="running",
            progress=0,
            total=total,
            percentage=0,
            message=f"Verification started for {total} references",
            started_at=now_iso(),
            completed_at=None,
            error=None
        )

        result["online_verification"] = {
            "rows": [],
            "summary": _compute_verification_summary([])
        }

        result["recovery"] = {
            "missing_recovery": [],
            "verification_recovery": []
        }

        result["claim_support"] = []

        _save_job_result(job_id, result)

        chunks = [
            refs[i:i + VERIFY_CHUNK_SIZE]
            for i in range(0, total, VERIFY_CHUNK_SIZE)
        ]

        print(f"🌐 Verification split into {len(chunks)} chunks of {VERIFY_CHUNK_SIZE}")
        print(f"⚡ Parallel verification: {VERIFY_PARALLEL_MODE}, workers={VERIFY_PARALLEL_WORKERS}, cache={VERIFY_USE_CACHE}")

        for chunk_index, chunk in enumerate(chunks, start=1):
            print(f"🌐 Verifying chunk {chunk_index}/{len(chunks)} with {len(chunk)} refs")

            chunk_rows = _verify_chunk_parallel(
                chunk,
                style=style,
                enrich_metadata=enrich_metadata
            )

            all_rows.extend(chunk_rows or [])

            progress = min(len(all_rows), total)
            percentage = int((progress / total) * 100) if total else 0
            summary = _compute_verification_summary(all_rows)

            result["online_verification"] = {
                "rows": all_rows,
                "summary": summary
            }

            result = _set_verification_meta(
                result,
                state="running",
                progress=progress,
                total=total,
                percentage=percentage,
                message=f"Verifying references: {progress}/{total}",
                last_heartbeat=now_iso(),
                chunks_completed=chunk_index,
                chunks_total=len(chunks)
            )

            _save_job_result(job_id, result)

            print(f"🌐 Persisted verification progress {progress}/{total}")

        summary = _compute_verification_summary(all_rows)

        result["online_verification"] = {
            "rows": all_rows,
            "summary": summary
        }
        result = _set_verification_meta(
            result,
            state="finalising",
            progress=total,
            total=total,
            percentage=100,
            message="Verification complete. Building Recovery and Claim Support tables...",
            final_tables_ready=False,
            last_heartbeat=now_iso()
        )
        
        _save_job_result(job_id, result)
        # Build Recovery immediately using safe fallback suggestions.
        try:
            result["recovery"] = _build_recovery_payload(result, all_rows)

            if (
                not result["recovery"].get("missing_recovery")
                and not result["recovery"].get("verification_recovery")
            ):
                result["recovery"] = {
                    "missing_recovery": [],
                    "verification_recovery": [
                        {
                            "status": r.get("status", ""),
                            "citation": (
                                r.get("citation")
                                or r.get("in_text")
                                or r.get("citation_in_text")
                                or ""
                            ),
                            "reference": (
                                r.get("reference")
                                or r.get("original_reference")
                                or r.get("matched_title")
                                or r.get("title")
                                or r.get("source_title")
                                or ""
                            ),
                            "suggestions": _fallback_recovery_suggestions(r, result, target=3)
                        }
                        for r in all_rows
                        if r.get("status") in {"likely", "needs_review", "not_found", "offline"}
                    ],
                    "note": "Fallback recovery rows generated."
                }

        except Exception as e:
            print(f"[VERIFY WORKER] Recovery error: {e}")
            result["recovery"] = {
                "missing_recovery": [],
                "verification_recovery": [],
                "note": f"Recovery generation failed: {e}"
            }

       # Build claim-support rows with scoring.
       # Uses the real checker first, then a safe title-only fallback so scores are not forced to zero.
        try:
            claim_rows = _build_claim_support_safe(result, all_rows)
        
            for row in claim_rows:
                row.setdefault("alternative_sources", [])
        
            result["claim_support"] = claim_rows
        
        except Exception as e:
            print(f"[VERIFY WORKER] Claim-support scoring failed: {e}")
            result["claim_support"] = _apply_verification_gate_to_claim_rows(
                _enhance_claim_support_scores(_fallback_claim_support_rows(result, all_rows)),
                all_rows
            )

        # ACII should not block completion.
        try:
            result["acii"] = compute_acii(result, all_rows)
        except Exception as e:
            print(f"[VERIFY WORKER] ACII error: {e}")
            result["acii"] = {"error": str(e)}

        elapsed = round(time.time() - start_time, 2)

        result = _set_verification_meta(
            result,
            state="completed",
            progress=total,
            total=total,
            percentage=100,
            results_count=len(all_rows),
            summary=summary,
            message=f"Verification completed for {len(all_rows)} references",
            completed_at=now_iso(),
            processing_time_seconds=elapsed,
            final_tables_ready=True
        )

        result["final_tables_ready"] = True
        result["verification_completed_at"] = now_iso()

        result["finalise"] = {
            "state": "completed",
            "recovery_lite_ready": True,
            "claim_lite_ready": True,
            "completed_at": now_iso()
        }

        deep_job_id = None
        result = _set_enrichment_meta(
            result,
            state="queued" if deep_job_id else "not_queued",
            rq_job_id=deep_job_id,
            deep_recovery_ready=False,
            deep_claim_support_ready=False,
            message=(
                "Advanced enrichment queued. Recovery Lite and Claim Support Lite are ready."
                if deep_job_id else
                "Advanced enrichment not queued. Recovery Lite and Claim Support Lite are ready."
            )
        )

        _save_job_result(job_id, result, status="completed")

        print(f"✅ Durable verification completed for job {job_id}: {len(all_rows)} rows")
        return result

    except Exception as e:
        print(f"❌ Durable verification failed for job {job_id}: {e}")
        import traceback
        traceback.print_exc()

        try:
            result = _load_job_result(job_id)

            result["online_verification"] = {
                "rows": all_rows,
                "summary": _compute_verification_summary(all_rows)
            }

            result.setdefault("recovery", {
                "missing_recovery": [],
                "verification_recovery": []
            })

            result.setdefault("claim_support", [])

            result = _set_verification_meta(
                result,
                state="error",
                message=str(e),
                error=str(e),
                completed_at=now_iso()
            )

            _save_job_result(job_id, result)

        except Exception as db_error:
            print(f"[VERIFY WORKER] Could not persist verification error: {db_error}")

        raise


   
# Start the worker
if __name__ == "__main__":
    print("🚀 Starting worker...")
    print(f"📊 Redis: {REDIS_URL[:50]}..." if REDIS_URL else "📊 Redis: NOT SET")
    print(f"💾 PostgreSQL: {'Connected' if DATABASE_URL else 'NOT SET'}")

    queue_env = os.environ.get("WORKER_QUEUES", "document_processing,verification,deep_enrichment")
    queues_to_listen = [q.strip() for q in queue_env.split(",") if q.strip()]

    with Connection(redis_conn):
        for queue_name in queues_to_listen:
            q = Queue(queue_name, connection=redis_conn)
            print(f"📌 Queue: {q.name}, jobs waiting: {q.count}")

        worker = Worker(queues_to_listen, connection=redis_conn)

        print(f"✅ Worker ready, listening to: {queues_to_listen}")
        print("📋 Detection scenarios enabled:")
        print("   Scenario 1: Year mismatches")
        print("   Scenario 2: Author name mismatches")
        print("   Scenario 3: Author order mismatches")
        print("   Scenario 4: Combined author + year mismatches")
        print("   Scenario 5: Et al. misuse")
        print("   Scenario 6: Potential wrong references")
        print("   Reference quality issues")

        worker.work(burst=False)
