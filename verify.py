# verify.py — Complete with proper progress tracking and debugging

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

# Timeout settings (in seconds)
API_TIMEOUT = 60  # Increased from 45 to 60 seconds per API call
VERIFICATION_TIMEOUT = None  # No timeout for the overall verification (None = infinite)
WORKER_THREADS = 2  # Reduce to 2 workers to avoid rate limiting
RETRY_ATTEMPTS = 2  # Number of retries for failed API calls
BATCH_DELAY = 0.5  # Delay between references to avoid rate limits

# ============================================================
# PROGRESS TRACKING (Lightweight)
# ============================================================

@dataclass
class VerificationJob:
    """Simple job tracking - does NOT store results (to avoid duplication)"""
    job_id: str
    total: int
    progress: int = 0
    status: str = "pending"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None

# Simple in-memory job storage - only for progress, NOT for results
_jobs: Dict[str, VerificationJob] = {}
_jobs_lock = threading.Lock()

# Store verification RESULTS (different from progress tracking)
_verification_results: Dict[str, List[Dict[str, Any]]] = {}
_verification_results_lock = threading.Lock()

def store_verification_results(job_id: str, results: List[Dict[str, Any]]):
    """Store completed verification results"""
    with _verification_results_lock:
        _verification_results[job_id] = results
        print(f"[DEBUG] Stored {len(results)} results for job {job_id}")

def get_verification_results(job_id: str) -> Optional[List[Dict[str, Any]]]:
    """Get stored verification results"""
    with _verification_results_lock:
        return _verification_results.get(job_id)

def clear_verification_results(job_id: str):
    """Clear verification results (optional cleanup)"""
    with _verification_results_lock:
        if job_id in _verification_results:
            del _verification_results[job_id]

def create_verification_job(job_id: str, total: int) -> str:
    """Create a new verification job for tracking progress only"""
    with _jobs_lock:
        _jobs[job_id] = VerificationJob(
            job_id=job_id,
            total=total,
            started_at=datetime.now().isoformat(),
            status="processing"
        )
    return job_id

def update_job_progress(job_id: str, progress: int):
    """Update job progress (does NOT store results)"""
    with _jobs_lock:
        if job_id in _jobs:
            job = _jobs[job_id]
            job.progress = progress
            if progress >= job.total:
                job.status = "completed"
                job.completed_at = datetime.now().isoformat()
                print(f"[DEBUG] Job {job_id}: COMPLETED - {progress}/{job.total}")
            else:
                # Print progress every 10 references to avoid spam
                if progress % 10 == 0:
                    print(f"[DEBUG] Job {job_id}: progress {progress}/{job.total}")

def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Get job progress status"""
    with _jobs_lock:
        if job_id not in _jobs:
            return None
        job = _jobs[job_id]
        return {
            "job_id": job.job_id,
            "status": job.status,
            "progress": job.progress,
            "total": job.total,
            "percentage": int((job.progress / job.total) * 100) if job.total > 0 else 0,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "error": job.error
        }

def get_queue_stats() -> Dict[str, Any]:
    """Get queue statistics"""
    with _jobs_lock:
        pending = sum(1 for j in _jobs.values() if j.status == "pending")
        processing = sum(1 for j in _jobs.values() if j.status == "processing")
        completed = sum(1 for j in _jobs.values() if j.status == "completed")
    
    return {
        "queue_size": 0,
        "pending_jobs": pending,
        "processing_jobs": processing,
        "total_jobs": len(_jobs),
        "is_busy": processing > 10
    }

def is_server_busy_check() -> bool:
    """Check if server is busy"""
    stats = get_queue_stats()
    return stats["processing_jobs"] > 20


# ============================================================
# ORIGINAL VERIFICATION CODE (PRESERVED)
# ============================================================

_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()

_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})(?:[a-z])?\b", re.I)
_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s]+)", re.I)

_STOP_WORDS = {
    "and", "the", "with", "from", "into", "using", "that", "this", "their",
    "these", "those", "among", "across", "study", "studies", "analysis",
    "journal", "review", "research", "paper", "available", "retrieved",
    "accessed", "conference", "proceedings", "press", "university",
}

_STYLE_ALIASES = {
    "apa": "apa",
    "harvard": "apa",
    "ieee": "ieee",
    "vancouver": "vancouver",
}


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


def _safe_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    try:
        return str(x)
    except Exception:
        return ""


def _safe_strip(x: Any) -> str:
    return _safe_str(x).strip()


def _norm_text(s: str) -> str:
    s = _safe_strip(s).lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s\-:/]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = None) -> Optional[dict]:
    """Get JSON from URL with configurable timeout"""
    if timeout is None:
        timeout = API_TIMEOUT  # Use the global timeout setting
    
    try:
        headers = {
            "User-Agent": f"CitationCrosschecker/2.0 (mailto:{MAILTO})",
            "Accept": "application/json",
        }
        r = requests.get(url, params=params, timeout=timeout, headers=headers)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        print(f"[DEBUG] API request failed: {e}")
        return None


def _extract_year(text: str) -> str:
    m = _YEAR_RE.search(text or "")
    return m.group(1) if m else ""


def _extract_doi(text: str) -> str:
    m = _DOI_RE.search(text or "")
    return m.group(1).rstrip(").,;") if m else ""


def _strip_leading_numbering(text: str) -> str:
    t = _safe_strip(text)
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\(?\d+\)?[\.\)])\s*", "", t)
    return t.strip()


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    with _CACHE_LOCK:
        v = _CACHE.get(key)
        return dict(v) if v else None


def _cache_set(key: str, value: Dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = dict(value)


def _dedupe_preserve(seq: List[str]) -> List[str]:
    seen = set()
    out = []
    for x in seq:
        k = _safe_strip(x).lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(_safe_strip(x))
    return out


# ---------------------------------------------------------
# Reference field extraction
# ---------------------------------------------------------

def _extract_authors_from_left(left: str) -> List[str]:
    left = _safe_strip(left)
    if not left:
        return []

    left = re.sub(r"\bet\s+al\.?\b", "", left, flags=re.I)
    left = left.replace("&", " and ")
    left = re.sub(r"\s+", " ", left).strip()

    candidates: List[str] = []

    if "," in left:
        parts = [p.strip() for p in left.split(",") if p.strip()]
        if parts:
            candidates.append(parts[0].split()[-1])
            for p in parts[1:]:
                bits = p.split()
                if bits and len(bits[0]) > 1 and bits[0][0].isupper():
                    candidates.append(bits[0])
    else:
        parts = re.split(r"\band\b", left, flags=re.I)
        for p in parts:
            toks = p.strip().split()
            if toks:
                candidates.append(toks[-1])

    cleaned = []
    for c in candidates:
        c = re.sub(r"[^A-Za-z'\-]", "", c).lower().strip()
        if len(c) >= 2:
            cleaned.append(c)

    return _dedupe_preserve(cleaned)[:4]


def _extract_title_guess(ref: str, year: str) -> str:
    ref_clean = _strip_leading_numbering(ref)

    if year:
        parts = re.split(rf"[\(\[]?\s*{re.escape(year)}\s*[\)\]]?", ref_clean, maxsplit=1, flags=re.I)
        if len(parts) >= 2:
            right = parts[1].strip(" .,:;")
            if right:
                title = re.split(r"\.\s+(?:In|Journal|Proceedings|Vol|No|pp\.?|https?://|doi)", right, maxsplit=1, flags=re.I)[0]
                title = title.strip(" .,:;\"'")
                if len(title) >= 6:
                    return title

    m = re.search(r'["“](.+?)["”]', ref_clean)
    if m:
        title = m.group(1).strip()
        if len(title) >= 6:
            return title

    bits = [b.strip() for b in ref_clean.split(".") if b.strip()]
    if len(bits) >= 2:
        for b in bits[1:3]:
            if len(b) >= 6 and not _YEAR_RE.search(b):
                return b

    return ref_clean[:180]


def _extract_common_fields(ref: str) -> Dict[str, Any]:
    ref = _safe_strip(ref)
    ref = _strip_leading_numbering(ref)

    year = _extract_year(ref)
    doi = _extract_doi(ref)

    left = ref
    if year:
        left = ref.split(year, 1)[0].strip(" ,.;:()[]")

    authors = _extract_authors_from_left(left)
    title = _extract_title_guess(ref, year)

    return {
        "authors": authors,
        "year": year,
        "doi": doi,
        "title": title,
    }


def _extract_apa_fields(ref: str) -> Dict[str, Any]:
    return _extract_common_fields(ref)


def _extract_fields_by_style(ref: str, style: str) -> Dict[str, Any]:
    style = _STYLE_ALIASES.get((style or "apa").lower(), "apa")
    return _extract_apa_fields(ref)


# ---------------------------------------------------------
# Query building
# ---------------------------------------------------------

def _significant_title_words(title: str, limit: int = 6) -> List[str]:
    words = re.findall(r"[A-Za-z]{3,}", title or "")
    out = []
    for w in words:
        wl = w.lower()
        if wl in _STOP_WORDS:
            continue
        out.append(wl)
    return out[:limit]


def _build_query(ref: str, style: str) -> Tuple[str, List[str], str, str, str]:
    fields = _extract_fields_by_style(ref, style)

    authors = fields.get("authors", []) or []
    year = fields.get("year", "") or ""
    doi = fields.get("doi", "") or ""
    title = fields.get("title", "") or ""

    title_words = _significant_title_words(title, limit=7)
    title_only = " ".join(title_words[:6]).strip()

    query_parts: List[str] = []

    if authors:
        query_parts.extend(authors[:2])

    if title_words:
        query_parts.extend(title_words)

    if year:
        query_parts.append(year)

    query = " ".join(query_parts).strip()

    if not query:
        raw_words = re.findall(r"[A-Za-z]{3,}", ref or "")
        query = " ".join(raw_words[:10] + ([year] if year else []))

    return query, authors, year, doi, title_only


# ---------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------

def _candidate_fields(cand: Dict[str, Any]) -> Tuple[str, str, str, List[str]]:
    src = cand.get("source")
    item = cand.get("item") or {}

    doi = ""
    title = ""
    year = ""
    authors: List[str] = []

    if src == "crossref":
        doi = _safe_strip(item.get("DOI"))

        titles = item.get("title") or []
        title = _safe_strip(titles[0]) if titles else ""

        issued = item.get("issued") or item.get("published-print") or item.get("published-online") or {}
        year = _safe_str((issued.get("date-parts", [[None]])[0][0])).strip()

        for au in (item.get("author") or [])[:6]:
            fam = _safe_strip(au.get("family")).lower()
            fam = re.sub(r"[^a-z'\-]", "", fam)
            if fam:
                authors.append(fam)

    elif src == "openalex":
        doi = _safe_strip(item.get("doi")).replace("https://doi.org/", "")
        title = _safe_strip(item.get("title"))
        year = _safe_strip(item.get("publication_year"))

        for a in (item.get("authorships") or [])[:6]:
            name = _safe_strip(a.get("author", {}).get("display_name"))
            if name:
                surname = re.sub(r"[^a-z'\-]", "", name.split()[-1].lower())
                if surname:
                    authors.append(surname)

    return doi, _norm_text(title), year, authors


# ---------------------------------------------------------
# External queries with retry logic
# ---------------------------------------------------------

def _query_with_retry(query_func, *args, max_retries=None, delay=2):
    """Execute query with retry logic"""
    if max_retries is None:
        max_retries = RETRY_ATTEMPTS
    
    for attempt in range(max_retries):
        try:
            result = query_func(*args)
            if result:
                return result
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
        except Exception as e:
            print(f"[DEBUG] Query attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
    return []


def _query_crossref_by_doi(doi: str) -> List[Dict[str, Any]]:
    if not doi:
        return []
    url = f"https://api.crossref.org/works/{doi}"
    params = {"mailto": MAILTO} if MAILTO else None
    data = _safe_get_json(url, params=params, timeout=API_TIMEOUT)
    if not data or "message" not in data:
        return []
    return [{"source": "crossref", "item": data["message"]}]


def _query_crossref(query: str, rows: int = 10) -> List[Dict[str, Any]]:
    if not query:
        return []
    url = "https://api.crossref.org/works"
    params: Dict[str, Any] = {
        "query.bibliographic": query,
        "rows": rows,
        "sort": "score",
        "order": "desc",
    }
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=API_TIMEOUT)
    items = (data or {}).get("message", {}).get("items", [])
    return [{"source": "crossref", "item": it} for it in items]


def _query_crossref_title_only(title_query: str, rows: int = 12) -> List[Dict[str, Any]]:
    if not title_query:
        return []
    url = "https://api.crossref.org/works"
    params: Dict[str, Any] = {
        "query.title": title_query,
        "rows": rows,
        "sort": "score",
        "order": "desc",
    }
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=API_TIMEOUT)
    items = (data or {}).get("message", {}).get("items", [])
    return [{"source": "crossref", "item": it} for it in items]


def _query_openalex(query: str, rows: int = 10) -> List[Dict[str, Any]]:
    if not query:
        return []
    url = "https://api.openalex.org/works"
    params: Dict[str, Any] = {"search": query, "per-page": rows}
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=API_TIMEOUT)
    items = (data or {}).get("results", [])
    return [{"source": "openalex", "item": it} for it in items]


def _query_openalex_title_only(title_query: str, rows: int = 12) -> List[Dict[str, Any]]:
    if not title_query:
        return []
    url = "https://api.openalex.org/works"
    params: Dict[str, Any] = {"search": title_query, "per-page": rows}
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=API_TIMEOUT)
    items = (data or {}).get("results", [])
    return [{"source": "openalex", "item": it} for it in items]


# ---------------------------------------------------------
# Scoring and classification
# ---------------------------------------------------------

def _score(
    ref_title: str,
    ref_authors: List[str],
    ref_year: str,
    cand_title: str,
    cand_authors: List[str],
    cand_year: str,
) -> Dict[str, Any]:
    ref_title = _norm_text(ref_title)
    cand_title = _norm_text(cand_title)

    token_score = fuzz.token_set_ratio(ref_title, cand_title) if ref_title and cand_title else 0
    partial_score = fuzz.partial_ratio(ref_title, cand_title) if ref_title and cand_title else 0
    title_score = int((token_score * 0.7) + (partial_score * 0.3))

    if ref_authors and cand_authors:
        ref_author_set = set(ref_authors)
        cand_author_set = set(cand_authors)
        
        intersection = len(ref_author_set & cand_author_set)
        union = len(ref_author_set | cand_author_set)
        
        if union > 0:
            author_similarity = (intersection / union) * 100
        else:
            author_similarity = 0
            
        author_overlap = intersection
    else:
        author_similarity = 0
        author_overlap = 0
    
    year_match = 1 if ref_year and cand_year and ref_year[:4] == cand_year[:4] else 0
    
    score = (title_score * 0.6) + (author_similarity * 0.3) + (year_match * 10)

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_overlap": int(author_overlap),
        "author_similarity": int(author_similarity),
        "year_match": int(year_match),
    }


def _classify(doi_match: bool, title_score: int, score: int, year_match: int) -> str:
    if doi_match:
        return "verified"
    
    if score >= 90:
        return "verified"
    
    if title_score >= 65 and year_match:
        return "verified"
    
    if score >= 70:
        return "likely"
    
    if title_score >= 60 and year_match:
        return "likely"
    
    if title_score >= 85:
        return "likely"
    
    if score >= 50:
        return "needs_review"
    
    if title_score >= 60:
        return "needs_review"
    
    return "not_found"


# ---------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------

def _best_candidate(
    ref_title: str,
    ref_authors: List[str],
    ref_year: str,
    ref_doi: str,
    candidates: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    best = None
    best_meta: Dict[str, Any] = {}
    best_score = -1

    for cand in candidates:
        doi, title, year, authors = _candidate_fields(cand)
        meta = _score(ref_title, ref_authors, ref_year, title, authors, year)

        doi_match = bool(ref_doi and doi and ref_doi.lower() == doi.lower())
        meta_score = int(meta["score"] + (25 if doi_match else 0))

        if meta_score > best_score:
            best_score = meta_score
            best = cand
            best_meta = dict(meta)
            best_meta["doi_match"] = doi_match
            best_meta["doi"] = doi
            best_meta["title"] = title
            best_meta["year"] = year
            best_meta["authors"] = authors

    return best, best_meta


# ---------------------------------------------------------
# Main verification function with improved error handling
# ---------------------------------------------------------
def _get_top_suggestions(
    ref_title: str,
    ref_authors: List[str],
    ref_year: str,
    candidates: List[Dict[str, Any]],
    top_k: int = 3,
) -> List[Dict[str, Any]]:

    scored = []

    for cand in candidates:
        doi, title, year, authors = _candidate_fields(cand)
        meta = _score(ref_title, ref_authors, ref_year, title, authors, year)

        # Only keep meaningful matches
        if meta["score"] >= 60 or meta["title_score"] >= 70:
            scored.append({
                "title": title,
                "doi": doi,
                "year": year,
                "authors": authors,
                "score": meta["score"],
                "title_score": meta["title_score"],
            })

    scored_sorted = sorted(scored, key=lambda x: x["score"], reverse=True)

    return scored_sorted[:top_k]
    
def _verify_single_reference(ref: str, style: str, use_crossref: bool, use_openalex: bool) -> Dict[str, Any]:
    """Original fast verification function with improved error handling"""
    cache_key = f"{style}::{ref}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    query, ref_authors, ref_year, ref_doi, title_only = _build_query(ref, style)
    fields = _extract_fields_by_style(ref, style)
    ref_title = fields.get("title") or ref

    row: Dict[str, Any] = {
        "reference": ref,
        "style": style,
        "status": "offline",
        "source": "",
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
        "author": ", ".join(ref_authors),
    }

    candidates: List[Dict[str, Any]] = []

    try:
        # DOI-first shortcut
        if ref_doi and use_crossref:
            candidates.extend(_query_crossref_by_doi(ref_doi))

        # stage 1
        if use_crossref and query:
            candidates.extend(_query_crossref(query, rows=10))
        if use_openalex and query:
            candidates.extend(_query_openalex(query, rows=10))

        # If no candidates from query, try title-only search
        if not candidates and title_only:
            if use_crossref:
                candidates.extend(_query_crossref_title_only(title_only, rows=8))
            if use_openalex:
                candidates.extend(_query_openalex_title_only(title_only, rows=8))

        best, best_meta = _best_candidate(ref_title, ref_authors, ref_year, ref_doi, candidates)

        if best:
            status = _classify(
                bool(best_meta.get("doi_match")),
                int(best_meta.get("title_score", 0)),
                int(best_meta.get("score", 0)),
                int(best_meta.get("year_match", 0)),
            )

            # deep fallback only for weak cases
            if status in {"needs_review", "not_found"}:
                deep_candidates = list(candidates)

                if use_crossref and query:
                    deep_candidates.extend(_query_crossref(query, rows=20))
                    if title_only:
                        deep_candidates.extend(_query_crossref_title_only(title_only, rows=15))

                if use_openalex and query:
                    deep_candidates.extend(_query_openalex(query, rows=20))
                    if title_only:
                        deep_candidates.extend(_query_openalex_title_only(title_only, rows=15))

                best2, best_meta2 = _best_candidate(ref_title, ref_authors, ref_year, ref_doi, deep_candidates)
                if best2:
                    best = best2
                    best_meta = best_meta2
                    status = _classify(
                        bool(best_meta.get("doi_match")),
                        int(best_meta.get("title_score", 0)),
                        int(best_meta.get("score", 0)),
                        int(best_meta.get("year_match", 0)),
                    )

            row.update({
                "status": status,
                "source": _safe_strip(best.get("source")),
                "score": int(best_meta.get("score", 0)),
                "doi": _safe_strip(best_meta.get("doi")),
                "matched_title": _safe_strip(best_meta.get("title")),
                "matched_year": _safe_strip(best_meta.get("year")),
                "matched_authors": ", ".join(best_meta.get("authors", [])),
                "title_score": int(best_meta.get("title_score", 0)),
                "author_overlap": int(best_meta.get("author_overlap", 0)),
                "author_similarity": int(best_meta.get("author_similarity", 0)),
                "year_match": int(best_meta.get("year_match", 0)),
            })

            # -------------------------------------------------
            # ADD SUGGESTED REFERENCES (REFINED)
            # -------------------------------------------------
            if status in {"likely", "needs_review", "not_found"} and candidates:

                suggestions = _get_top_suggestions(
                    ref_title,
                    ref_authors,
                    ref_year,
                    candidates,
                    top_k=3,
                )

                # Avoid returning the same match as suggestion
                filtered_suggestions = []
                for s in suggestions:
                    if _safe_strip(s.get("title")) != _safe_strip(row.get("matched_title")):
                        filtered_suggestions.append(s)

                # Only attach meaningful suggestions
                if filtered_suggestions:
                    row["suggested_references"] = filtered_suggestions

        else:
            row["status"] = "not_found"

    except Exception as e:
        print(f"[DEBUG] Error verifying reference: {e}")
        row["status"] = "not_found"
        row["error"] = str(e)

    row["status"] = _normalize_verify_status(row.get("status"))
    _cache_set(cache_key, row)
    return row

# ---------------------------------------------------------
# Public API - Returns ALL results (NO TIME LIMITS)
# ---------------------------------------------------------

def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = True,
    job_id: str = None,
) -> List[Dict[str, Any]]:
    """
    Verify references batch with optional progress tracking.
    ALWAYS returns ALL results. NO TIME LIMITS - processes all references.
    """
    refs = [r for r in (references or []) if _safe_strip(r)]
    if not refs:
        print("[DEBUG] No references to verify")
        return []

    normalized_style = _STYLE_ALIASES.get((style or "apa").lower(), "apa")
    total_refs = len(refs)
    
    # Calculate estimated time
    est_seconds = total_refs * (API_TIMEOUT / 2)  # Estimate based on API timeout
    est_minutes = est_seconds / 60
    est_hours = est_minutes / 60
    
    print(f"[DEBUG] ========================================")
    print(f"[DEBUG] Starting verification for {total_refs} references")
    print(f"[DEBUG] Style: {normalized_style}")
    if est_hours >= 1:
        print(f"[DEBUG] Estimated time: ~{est_hours:.1f} hours ({est_minutes:.0f} minutes)")
    elif est_minutes >= 1:
        print(f"[DEBUG] Estimated time: ~{est_minutes:.1f} minutes")
    else:
        print(f"[DEBUG] Estimated time: ~{est_seconds:.0f} seconds")
    print(f"[DEBUG] ========================================")

    # Create job for progress tracking if job_id provided
    if job_id:
        create_verification_job(job_id, total_refs)
        print(f"[DEBUG] Created verification job {job_id}")

    rows: List[Dict[str, Any]] = [None] * total_refs
    # Use WORKER_THREADS to control concurrency
    workers = min(WORKER_THREADS, max(1, total_refs))
    print(f"[DEBUG] Using {workers} workers (to avoid rate limits)")

    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        
        for i, ref in enumerate(refs):
            future = executor.submit(
                _verify_single_reference,
                ref,
                normalized_style,
                use_crossref,
                use_openalex,
            )
            futures[future] = i

        completed_count = 0
        for future in as_completed(futures):
            idx = futures[future]
            try:
                rows[idx] = future.result(timeout=API_TIMEOUT + 10)  # Add buffer to API timeout
                completed_count += 1
                
                # Update progress if tracking
                if job_id:
                    update_job_progress(job_id, completed_count)
                    
                    # Print progress every 10 references or at completion
                    if completed_count % 10 == 0 or completed_count == total_refs:
                        elapsed = time.time() - start_time
                        rate = completed_count / elapsed if elapsed > 0 else 0
                        remaining = (total_refs - completed_count) / rate if rate > 0 else 0
                        print(f"[DEBUG] Progress: {completed_count}/{total_refs} ({completed_count*100//total_refs}%) - Rate: {rate:.1f}/sec - Est. remaining: {remaining/60:.1f} min")
                
                # Small delay to avoid rate limiting
                time.sleep(BATCH_DELAY)
                    
            except Exception as e:
                print(f"[DEBUG] Error verifying reference {refs[idx][:100]}: {e}")
                rows[idx] = {
                    "reference": refs[idx],
                    "style": normalized_style,
                    "status": "not_found",
                    "source": "",
                    "score": 0,
                    "doi": "",
                    "matched_title": "",
                    "matched_year": "",
                    "matched_authors": "",
                    "title_score": 0,
                    "author_overlap": 0,
                    "author_similarity": 0,
                    "year_match": 0,
                    "query_used": "",
                    "author": "",
                    "error": str(e),
                }
                completed_count += 1
                if job_id:
                    update_job_progress(job_id, completed_count)

    # Count results for debugging
    result_counts = {
        "verified": sum(1 for r in rows if r and r.get("status") == "verified"),
        "likely": sum(1 for r in rows if r and r.get("status") == "likely"),
        "needs_review": sum(1 for r in rows if r and r.get("status") == "needs_review"),
        "not_found": sum(1 for r in rows if r and r.get("status") == "not_found"),
        "offline": sum(1 for r in rows if r and r.get("status") == "offline"),
    }
    print(f"[DEBUG] ========================================")
    print(f"[DEBUG] Verification COMPLETE for {total_refs} references")
    print(f"[DEBUG] Results: {result_counts}")
    print(f"[DEBUG] Total processed: {len([r for r in rows if r is not None])}")
    print(f"[DEBUG] ========================================")

    for r in rows:
        if r:
            r["status"] = _normalize_verify_status(r.get("status"))

    # Store results if job_id was provided
    if job_id:
        update_job_progress(job_id, total_refs)  # Final progress update
        store_verification_results(job_id, rows)  # Store the actual results
        print(f"[DEBUG] Stored verification results for job {job_id}, got {len(rows)} results")

    return rows


# ---------------------------------------------------------
# Background job submission (NO TIME LIMITS)
# ---------------------------------------------------------

def submit_verification(references: List[str], style: str = "apa") -> str:
    """
    Submit a verification job and return job ID (runs in background)
    NO TIME LIMITS - will process all references regardless of count
    """
    job_id = uuid.uuid4().hex
    total_refs = len(references)
    est_seconds = total_refs * (API_TIMEOUT / 2)
    est_minutes = est_seconds / 60
    est_hours = est_minutes / 60
    
    print(f"[DEBUG] ========================================")
    print(f"[DEBUG] Submitting verification job {job_id}")
    print(f"[DEBUG] Total references: {total_refs}")
    if est_hours >= 1:
        print(f"[DEBUG] Estimated time: ~{est_hours:.1f} hours ({est_minutes:.0f} minutes)")
    elif est_minutes >= 1:
        print(f"[DEBUG] Estimated time: ~{est_minutes:.1f} minutes")
    else:
        print(f"[DEBUG] Estimated time: ~{est_seconds:.0f} seconds")
    print(f"[DEBUG] ========================================")
    
    def run():
        print(f"[DEBUG] Starting background thread for job {job_id}")
        start_time = time.time()
        results = verify_references_batch(references, style, job_id=job_id)
        elapsed = time.time() - start_time
        print(f"[DEBUG] Background thread completed for job {job_id}")
        print(f"[DEBUG] Time elapsed: {elapsed:.1f} seconds ({elapsed/60:.1f} minutes)")
        print(f"[DEBUG] Results count: {len(results)}")
        
        # Print final summary
        verified = sum(1 for r in results if r.get("status") == "verified")
        likely = sum(1 for r in results if r.get("status") == "likely")
        needs_review = sum(1 for r in results if r.get("status") == "needs_review")
        not_found = sum(1 for r in results if r.get("status") == "not_found")
        print(f"[DEBUG] Final: Verified={verified}, Likely={likely}, NeedsReview={needs_review}, NotFound={not_found}")
    
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    
    return job_id


def get_verification_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Get verification job progress (not results)"""
    return get_job_status(job_id)


# Alias for compatibility
submit_verification_job = submit_verification
get_queue_status = get_queue_stats
is_server_busy = is_server_busy_check
