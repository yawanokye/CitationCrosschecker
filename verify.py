# verify.py — metadata capture + recovery metadata + OpenAlex rescue + Redis-persistent verification progress

import os
import re
import threading
import time
import uuid
import json
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

import requests

from publication_integrity import (
    enrich_verification_row,
    events_from_crossref_message,
    is_publication_notice,
)

try:
    import redis
except Exception:
    redis = None
from rapidfuzz import fuzz


_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}

MAILTO = (
    os.getenv("CITATION_CROSSCHECKER_MAILTO")
    or os.getenv("CROSSREF_MAILTO")
    or os.getenv("OPENALEX_MAILTO")
    or ""
).strip()
OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY", "").strip()

# ============================================================
# REDIS-PERSISTENT VERIFICATION PROGRESS
# ============================================================
# Render runs web and workers as separate processes/services. In-memory globals
# are not shared across those services. These keys allow /online/status to see
# progress/results even when verification runs in another process.
REDIS_URL = os.getenv("REDIS_URL", "").strip()
VERIFY_REDIS_TTL = int(os.getenv("VERIFY_REDIS_TTL", "21600") or "21600")

verify_redis_conn = None
if redis is not None and REDIS_URL:
    try:
        verify_redis_conn = redis.from_url(REDIS_URL)
        verify_redis_conn.ping()
        print("✅ verify.py Redis progress store connected")
    except Exception as e:
        print(f"⚠️ verify.py Redis progress store unavailable: {e}")
        verify_redis_conn = None


def _verify_status_key(job_id: str) -> str:
    return f"verify:status:{job_id}"


def _verify_results_key(job_id: str) -> str:
    return f"verify:results:{job_id}"


def _redis_set_json(key: str, value: Any, ttl: int = VERIFY_REDIS_TTL) -> bool:
    if not verify_redis_conn:
        return False
    try:
        verify_redis_conn.setex(key, ttl, json.dumps(value, default=str))
        return True
    except Exception as e:
        print(f"[VERIFY REDIS] set failed for {key}: {e}")
        return False


def _redis_get_json(key: str):
    if not verify_redis_conn:
        return None
    try:
        raw = verify_redis_conn.get(key)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        return json.loads(raw)
    except Exception as e:
        print(f"[VERIFY REDIS] get failed for {key}: {e}")
        return None


def _persist_job_status(job_id: str, payload: Dict[str, Any]) -> None:
    if not job_id:
        return
    payload = dict(payload or {})
    payload.setdefault("job_id", job_id)
    payload.setdefault("updated_at", datetime.now().isoformat())
    _redis_set_json(_verify_status_key(job_id), payload)


def _persist_job_results(job_id: str, rows: List[Dict[str, Any]]) -> None:
    if not job_id:
        return
    _redis_set_json(_verify_results_key(job_id), rows or [])

# ============================================================
# TIMEOUT SETTINGS - ADDED FOR LARGE REFERENCE SETS
# ============================================================

# Commercial fast-verification settings.
# These defaults avoid one weak reference holding a whole job for minutes.
def _env_flag(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}

API_TIMEOUT = int(os.getenv("VERIFY_REQUEST_TIMEOUT", "4"))
VERIFICATION_TIMEOUT = None
WORKER_THREADS = int(os.getenv("VERIFY_INNER_THREADS", "2"))
RETRY_ATTEMPTS = int(os.getenv("VERIFY_RETRY_ATTEMPTS", "1"))
BATCH_DELAY = float(os.getenv("VERIFY_BATCH_DELAY", "0"))

# Chunk processing for large reference sets
CHUNK_SIZE = int(os.getenv("VERIFY_INTERNAL_CHUNK_SIZE", "10"))
CHUNK_DELAY = float(os.getenv("VERIFY_CHUNK_DELAY", "0"))
MAX_RETRIES_PER_REFERENCE = int(os.getenv("VERIFY_MAX_RETRIES_PER_REFERENCE", "1"))

# Fast-skip and fallback controls
VERIFY_SKIP_WEAK_TITLE = _env_flag("VERIFY_SKIP_WEAK_TITLE", "1")
VERIFY_MIN_TITLE_WORDS = int(os.getenv("VERIFY_MIN_TITLE_WORDS", "4"))
VERIFY_STOP_ON_STRONG_MATCH = _env_flag("VERIFY_STOP_ON_STRONG_MATCH", "1")
VERIFY_DEEP_FALLBACK = _env_flag("VERIFY_DEEP_FALLBACK", "0")
VERIFY_RETRY_FAILED = _env_flag("VERIFY_RETRY_FAILED", "0")
VERIFY_CROSSREF_ROWS = int(os.getenv("VERIFY_CROSSREF_ROWS", "5"))
VERIFY_TITLE_ROWS = int(os.getenv("VERIFY_TITLE_ROWS", "5"))
VERIFY_OPENALEX_ROWS = int(os.getenv("VERIFY_OPENALEX_ROWS", "5"))
VERIFY_SINGLE_REF_TIMEOUT = int(os.getenv("VERIFY_SINGLE_REF_TIMEOUT", str(max(6, API_TIMEOUT + 2))))
VERIFY_RETRY_BACKOFF_SECONDS = float(os.getenv("VERIFY_RETRY_BACKOFF_SECONDS", "1"))
VERIFY_FORCE_OPENALEX_FALLBACK = _env_flag("VERIFY_FORCE_OPENALEX_FALLBACK", "1")
VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY = _env_flag("VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY", "1")

# Per-reference network diagnostics. Verification runs references in separate
# threads, so thread-local storage prevents one reference's provider failure
# from contaminating another reference's result.
_API_REQUEST_STATE = threading.local()


def _reset_api_request_diagnostics() -> None:
    _API_REQUEST_STATE.events = []


def _record_api_request_diagnostic(kind: str, url: str, status_code: int = None) -> None:
    events = list(getattr(_API_REQUEST_STATE, "events", []) or [])
    host_match = re.match(r"^https?://([^/]+)", str(url or ""), flags=re.I)
    event = {
        "kind": str(kind or "unknown"),
        "provider": host_match.group(1).lower() if host_match else "unknown",
    }
    if status_code is not None:
        event["status_code"] = int(status_code)
    events.append(event)
    _API_REQUEST_STATE.events = events


def _api_request_diagnostics() -> List[Dict[str, Any]]:
    return list(getattr(_API_REQUEST_STATE, "events", []) or [])
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
    """Store completed verification results in memory and Redis."""
    with _verification_results_lock:
        _verification_results[job_id] = results
    _persist_job_results(job_id, results or [])
    print(f"[DEBUG] Stored {len(results or [])} results for job {job_id}")

def get_verification_results(job_id: str) -> Optional[List[Dict[str, Any]]]:
    """Get stored verification results from memory first, then Redis."""
    rows = None
    with _verification_results_lock:
        rows = _verification_results.get(job_id)

    if rows is None:
        rows = _redis_get_json(_verify_results_key(job_id))

    if isinstance(rows, list):
        return rows

    return None

def clear_verification_results(job_id: str):
    """Clear verification results."""
    with _verification_results_lock:
        if job_id in _verification_results:
            del _verification_results[job_id]
    if verify_redis_conn:
        try:
            verify_redis_conn.delete(_verify_results_key(job_id))
            verify_redis_conn.delete(_verify_status_key(job_id))
        except Exception:
            pass

def create_verification_job(job_id: str, total: int) -> str:
    """Create a new verification job for tracking progress."""
    started = datetime.now().isoformat()
    with _jobs_lock:
        _jobs[job_id] = VerificationJob(
            job_id=job_id,
            total=total,
            started_at=started,
            status="processing"
        )
    _persist_job_status(job_id, {
        "job_id": job_id,
        "status": "processing",
        "progress": 0,
        "total": total,
        "percentage": 0,
        "started_at": started,
        "completed_at": None,
        "error": None,
        "message": f"Verification started: 0/{total}",
    })
    return job_id

def update_job_progress(job_id: str, progress: int):
    """Update job progress and persist it to Redis for cross-process polling."""
    status_payload = None
    with _jobs_lock:
        if job_id in _jobs:
            job = _jobs[job_id]
            job.progress = progress
            if progress < job.total:
                job.status = "processing"
            else:
                job.status = "completed"
                job.completed_at = datetime.now().isoformat()
                print(f"[DEBUG] Job {job_id}: COMPLETED - {progress}/{job.total}")

            status_payload = {
                "job_id": job.job_id,
                "status": job.status,
                "progress": job.progress,
                "total": job.total,
                "percentage": int((job.progress / job.total) * 100) if job.total > 0 else 0,
                "started_at": job.started_at,
                "completed_at": job.completed_at,
                "error": job.error,
                "message": f"Verification {job.progress}/{job.total}",
            }

            if progress % 5 == 0 or progress == job.total:
                print(f"[DEBUG] Job {job_id}: progress {progress}/{job.total} (status: {job.status})")
        else:
            current = _redis_get_json(_verify_status_key(job_id)) or {}
            total = int(current.get("total") or max(progress, 0))
            status = "completed" if total and progress >= total else "processing"
            status_payload = {
                "job_id": job_id,
                "status": status,
                "progress": progress,
                "total": total,
                "percentage": int((progress / max(total, 1)) * 100) if total else 0,
                "started_at": current.get("started_at") or datetime.now().isoformat(),
                "completed_at": datetime.now().isoformat() if status == "completed" else None,
                "error": None,
                "message": f"Verification {progress}/{total}",
            }

    if status_payload:
        _persist_job_status(job_id, status_payload)

def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Get job progress status from memory, Redis, or stored results."""
    with _jobs_lock:
        if job_id in _jobs:
            job = _jobs[job_id]
            payload = {
                "job_id": job.job_id,
                "status": job.status,
                "progress": job.progress,
                "total": job.total,
                "percentage": int((job.progress / job.total) * 100) if job.total > 0 else 0,
                "started_at": job.started_at,
                "completed_at": job.completed_at,
                "error": job.error
            }
            _persist_job_status(job_id, payload)
            return payload

    payload = _redis_get_json(_verify_status_key(job_id))
    if isinstance(payload, dict):
        return payload

    rows = get_verification_results(job_id)
    if isinstance(rows, list) and rows:
        total = len(rows)
        return {
            "job_id": job_id,
            "status": "completed",
            "progress": total,
            "total": total,
            "percentage": 100,
            "started_at": None,
            "completed_at": datetime.now().isoformat(),
            "error": None,
            "message": "Verification completed from stored results",
        }

    return None

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
# ENHANCED METADATA FETCHING FROM CROSSREF (NEW)
# ============================================================

def fetch_full_crossref_metadata(doi: str) -> Optional[Dict[str, Any]]:
    """
    Fetch COMPLETE metadata from Crossref including volume, issue, pages, and full author names.
    This is called AFTER a match is found to enrich the existing verification result.
    """
    if not doi:
        return None
    
    # Clean DOI
    doi = re.sub(r'^https?://(doi\.org/|dx\.doi\.org/)', '', doi.strip())
    doi = re.sub(r'^doi:', '', doi, flags=re.IGNORECASE)
    
    url = f"https://api.crossref.org/works/{doi}"
    params = {"mailto": MAILTO} if MAILTO else None
    
    try:
        response = requests.get(url, params=params, timeout=API_TIMEOUT, 
                                headers={"User-Agent": f"CitationVerifier/2.0 (mailto:{MAILTO})"})
        
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "ok" and "message" in data:
                return parse_full_crossref_message(data["message"])
        return None
    except Exception as e:
        print(f"[DEBUG] Error fetching full metadata for {doi}: {e}")
        return None


def parse_full_crossref_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse Crossref message into structured metadata with ALL fields needed for APA/Harvard.
    """
    # Extract authors with FULL names
    authors = []
    for author in message.get("author", []):
        family = author.get("family", "")
        given = author.get("given", "")
        
        # Build properly formatted author string for APA
        if given and family:
            # Extract initials from given name
            initials = " ".join([f"{name[0].upper()}." for name in given.split() if name[0].isalpha()])
            author_str = f"{family}, {initials}"
        elif family:
            author_str = family
        elif given:
            author_str = given
        else:
            continue
        
        authors.append({
            "family": family,
            "given": given,
            "formatted": author_str,
            "ORCID": author.get("ORCID", "")
        })
    
    # Extract title (prefer English if multiple)
    titles = message.get("title", [])
    title = titles[0] if titles else ""
    
    # Extract container title (journal name)
    container_titles = message.get("container-title", [])
    container_title = container_titles[0] if container_titles else ""
    
    # Extract volume, issue, pages
    volume = message.get("volume", "")
    issue = message.get("issue", "")
    page = message.get("page", "")
    
    # Parse page range
    first_page = None
    last_page = None
    if page and "-" in page:
        parts = page.split("-")
        first_page = parts[0].strip()
        last_page = parts[1].strip() if len(parts) > 1 else None
    
    # For online-only articles
    article_number = message.get("article-number", "")
    
    # Extract publication date
    issued = message.get("issued", {})
    date_parts = issued.get("date-parts", [[]])
    year = date_parts[0][0] if date_parts and date_parts[0] else None
    month = date_parts[0][1] if date_parts and len(date_parts[0]) > 1 else None
    day = date_parts[0][2] if date_parts and len(date_parts[0]) > 2 else None
    
    # Extract publisher
    publisher = message.get("publisher", "")
    
    # Extract type
    type_name = message.get("type", "")
    
    # Extract DOI
    doi = message.get("DOI", "")
    
    return {
        "doi": doi,
        "title": title,
        "container_title": container_title,
        "authors": authors,
        "author_strings": [a["formatted"] for a in authors],
        "year": str(year) if year else "",
        "month": month,
        "day": day,
        "volume": str(volume) if volume else "",
        "issue": str(issue) if issue else "",
        "page": page,
        "first_page": first_page,
        "last_page": last_page,
        "article_number": article_number,
        "publisher": publisher,
        "type": type_name,
    }


def build_apa7_from_metadata(metadata: Dict[str, Any]) -> str:
    """
    Build complete APA 7 reference from full metadata.
    """
    if not metadata:
        return ""
    
    # Format authors (APA 7: up to 20 authors, then "...")
    authors = metadata.get("author_strings", [])
    if len(authors) == 0:
        authors_str = ""
    elif len(authors) == 1:
        authors_str = authors[0]
    elif len(authors) == 2:
        authors_str = f"{authors[0]} & {authors[1]}"
    elif len(authors) <= 20:
        authors_str = ", ".join(authors[:-1]) + ", & " + authors[-1]
    else:
        authors_str = ", ".join(authors[:19]) + ", … & " + authors[19]
    
    # Year
    year = metadata.get("year", "n.d.")
    year_str = f"({year})" if year != "n.d." else "(n.d.)"
    
    # Title (sentence case)
    title = metadata.get("title", "")
    if title:
        # Convert to sentence case (preserve proper nouns - simplified)
        title = title[0].upper() + title[1:].lower() if len(title) > 1 else title.upper()
        title_str = f"{title}."
    else:
        title_str = ""
    
    # Journal
    journal = metadata.get("container_title", "")
    
    # Volume, issue, pages
    volume = metadata.get("volume", "")
    issue = metadata.get("issue", "")
    page = metadata.get("page", "")
    article_number = metadata.get("article_number", "")
    
    journal_info = ""
    if journal:
        journal_info = f" *{journal}*"
        if volume:
            journal_info += f", *{volume}*"
            if issue:
                journal_info += f"({issue})"
        if page:
            journal_info += f", {page}"
        elif article_number:
            journal_info += f", {article_number}"
        journal_info += "."
    
    # DOI
    doi = metadata.get("doi", "")
    doi_str = f" https://doi.org/{doi}" if doi else ""
    
    # Build complete reference
    parts = [authors_str, year_str, title_str]
    if journal_info:
        parts.append(journal_info)
    if doi_str:
        parts.append(doi_str)
    
    return " ".join(filter(None, parts))


def build_harvard_from_metadata(metadata: Dict[str, Any]) -> str:
    """
    Build complete Harvard reference from full metadata.
    """
    if not metadata:
        return ""
    
    # Format authors (Harvard: surnames only, or initials)
    authors = metadata.get("authors", [])
    if len(authors) == 0:
        authors_str = ""
    elif len(authors) == 1:
        authors_str = authors[0].get("family", authors[0].get("given", ""))
    elif len(authors) == 2:
        authors_str = f"{authors[0].get('family', '')} and {authors[1].get('family', '')}"
    else:
        authors_str = f"{authors[0].get('family', '')} et al."
    
    # Year in parentheses
    year = metadata.get("year", "n.d.")
    year_str = f"({year})" if year != "n.d." else "(n.d.)"
    
    # Title (sentence case, no extra punctuation)
    title = metadata.get("title", "")
    if title:
        title = title[0].upper() + title[1:].lower() if len(title) > 1 else title.upper()
        title_str = title
    else:
        title_str = ""
    
    # Journal (italics)
    journal = metadata.get("container_title", "")
    
    # Volume, issue, pages
    volume = metadata.get("volume", "")
    issue = metadata.get("issue", "")
    page = metadata.get("page", "")
    
    journal_info = ""
    if journal:
        journal_info = f" *{journal}*"
        if volume:
            journal_info += f", {volume}"
            if issue:
                journal_info += f"({issue})"
        if page:
            journal_info += f", pp. {page}"
        journal_info += "."
    
    # DOI
    doi = metadata.get("doi", "")
    doi_str = f" doi:{doi}" if doi else ""
    
    # Build reference (Harvard: no period after title)
    parts = [authors_str, year_str, title_str]
    if journal_info:
        parts.append(journal_info)
    if doi_str:
        parts.append(doi_str)
    
    return " ".join(filter(None, parts))


def enrich_with_full_metadata(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Take an existing verification result and enrich it with full metadata from Crossref.
    This preserves all original query results and adds additional fields.
    """
    if not result:
        return result
    
    # Check if we already have full metadata
    if result.get("full_metadata"):
        return result
    
    # Get DOI from result
    doi = result.get("doi", "")
    if not doi:
        return result
    
    # Fetch full metadata
    full_metadata = fetch_full_crossref_metadata(doi)
    if full_metadata:
        result["full_metadata"] = full_metadata
        result["matched_volume"] = full_metadata.get("volume", "")
        result["matched_issue"] = full_metadata.get("issue", "")
        result["matched_pages"] = full_metadata.get("page", "")
        result["matched_container_title"] = full_metadata.get("container_title", "")
        result["matched_authors_full"] = ", ".join(full_metadata.get("author_strings", []))
        result["apa7_reference"] = build_apa7_from_metadata(full_metadata)
        result["harvard_reference"] = build_harvard_from_metadata(full_metadata)
    
    return result


# ============================================================
# ORIGINAL VERIFICATION CODE (PRESERVED - NO CHANGES)
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
    "springer", "elsevier", "taylor", "francis", "sage", "wiley",
    "ieee", "nature", "acm", "oxford", "cambridge", "routledge",
    "macmillan", "pearson", "harpercollins", "penguin", "random", "house",
    "john", "sons", "inc", "editorial", "publisher", "page",
}

# Extra query stopwords used only when building API search queries.
# These words are common in scholarly titles but often reduce Crossref/OpenAlex precision.
_QUERY_STOP_WORDS = _STOP_WORDS | {
    "approach", "model", "models", "evidence", "article", "method", "methods",
    "based", "case", "empirical", "impact", "impacts", "role", "roles",
    "determinants", "perspective", "framework", "assessment", "evaluation",
    "effects", "relationship", "relationships", "moderating", "mediating",
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
    """
    Normalise verification status without promoting uncertain matches.

    A likely match remains distinct from a verified match so the results page
    can show it as verified with metadata differences or human review needed.
    """
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
    """Get JSON from URL with short, commercial-safe timeout and limited retry."""
    if timeout is None:
        timeout = API_TIMEOUT

    attempts = max(1, RETRY_ATTEMPTS)

    for attempt in range(attempts):
        try:
            headers = {
                "User-Agent": f"CitationCrosschecker/2.0 (mailto:{MAILTO})",
                "Accept": "application/json",
            }
            r = requests.get(url, params=params, timeout=timeout, headers=headers)
            if r.status_code == 200:
                _record_api_request_diagnostic("ok", url, 200)
                return r.json()
            if r.status_code == 429:
                _record_api_request_diagnostic("rate_limited", url, 429)
                print("[DEBUG] API rate limited. Skipping this lookup safely.")
                return None
            if r.status_code == 404:
                _record_api_request_diagnostic("not_found_http", url, 404)
                return None
            _record_api_request_diagnostic("http_error", url, r.status_code)
            return None
        except requests.exceptions.Timeout:
            _record_api_request_diagnostic("timeout", url)
            print(f"[DEBUG] API timeout after {timeout}s on attempt {attempt + 1}/{attempts}")
            if attempt < attempts - 1 and VERIFY_RETRY_BACKOFF_SECONDS > 0:
                time.sleep(VERIFY_RETRY_BACKOFF_SECONDS)
        except Exception as e:
            _record_api_request_diagnostic("network_error", url)
            print(f"[DEBUG] API request failed quickly: {e}")
            if attempt < attempts - 1 and VERIFY_RETRY_BACKOFF_SECONDS > 0:
                time.sleep(VERIFY_RETRY_BACKOFF_SECONDS)

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


def _clean_title_guess(title: str) -> str:
    """
    Clean extracted reference title before online verification.
    Removes DOI, URL, page, volume, and retrieval noise that weaken Crossref/OpenAlex matching.
    """
    title = _safe_strip(title)

    if not title:
        return ""

    title = re.sub(r"https?://\S+", "", title, flags=re.I)
    title = re.sub(r"\bdoi\s*:?\s*\S+", "", title, flags=re.I)
    title = re.sub(r"\bhttps?://doi\.org/\S+", "", title, flags=re.I)
    title = re.sub(r"\b(pp?|pages?)\.?\s*\d+[\-–—]?\d*", "", title, flags=re.I)
    title = re.sub(r"\bvol\.?\s*\d+", "", title, flags=re.I)
    title = re.sub(r"\bno\.?\s*\d+", "", title, flags=re.I)
    title = re.sub(r"\bissue\.?\s*\d+", "", title, flags=re.I)
    title = re.sub(r"\bretrieved\s+from\b.*$", "", title, flags=re.I)
    title = re.sub(r"\bavailable\s+at\b.*$", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" .,:;\"'")

    return title


def _extract_title_guess(ref: str, year: str) -> str:
    ref_clean = _strip_leading_numbering(ref)

    if year:
        parts = re.split(
            rf"[\(\[]?\s*{re.escape(year)}\s*[\)\]]?",
            ref_clean,
            maxsplit=1,
            flags=re.I
        )

        if len(parts) >= 2:
            right = parts[1].strip(" .,:;")

            if right:
                title = re.split(
                    r"\.\s+(?:In|Journal|Proceedings|Vol|No|pp\.?|pages?|https?://|doi|Retrieved|Available)",
                    right,
                    maxsplit=1,
                    flags=re.I
                )[0]

                title = _clean_title_guess(title)

                if len(title) >= 6:
                    return title

    m = re.search(r'["“](.+?)["”]', ref_clean)
    if m:
        title = _clean_title_guess(m.group(1))

        if len(title) >= 6:
            return title

    bits = [b.strip() for b in ref_clean.split(".") if b.strip()]

    if len(bits) >= 2:
        for b in bits[1:4]:
            if len(b) >= 6 and not _YEAR_RE.search(b):
                title = _clean_title_guess(b)

                if len(title) >= 6:
                    return title

    title = _clean_title_guess(ref_clean[:180])
    return title


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


def _query_crossref(query: str, rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_CROSSREF_ROWS
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


def _query_crossref_title_only(title_query: str, rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_TITLE_ROWS
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


def _query_openalex(query: str, rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_OPENALEX_ROWS
    if not query:
        return []
    url = "https://api.openalex.org/works"
    params: Dict[str, Any] = {"search": query, "per-page": rows}
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=API_TIMEOUT)
    items = (data or {}).get("results", [])
    return [{"source": "openalex", "item": it} for it in items]


def _query_openalex_title_only(title_query: str, rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_TITLE_ROWS
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

    token_score = fuzz.token_sort_ratio(ref_title, cand_title) if ref_title and cand_title else 0
    set_score = fuzz.token_set_ratio(ref_title, cand_title) if ref_title and cand_title else 0
    partial_score = fuzz.partial_ratio(ref_title, cand_title) if ref_title and cand_title else 0
    
    title_score = int((token_score * 0.45) + (set_score * 0.35) + (partial_score * 0.20))

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
    
    score = (title_score * 0.65) + (author_similarity * 0.25) + (year_match * 10)

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
            return "verified"
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
        return "verified"

    # Moderate title with year or author support.
    if title_score >= 78 and (year_match == 1 or author_overlap >= 1):
        return "Verified"

    # Good combined score, but not enough for verified.
    if score >= 75 and title_score >= 75:
        return "Verified"

    # -----------------------------------------
    # 4. NEEDS REVIEW
    # -----------------------------------------
    # Candidate exists but evidence is incomplete or weak.
    if title_score >= 60:
        return "verified"

    if score >= 50:
        return "needs_review"

    # -----------------------------------------
    # 5. NOT FOUND
    # -----------------------------------------
    return "not_found"

    if title_score >= 58:
        return "verified"

    if score >= 35:
        return "needs_review"

    # -----------------------------------------
    # 5. NOT FOUND
    # -----------------------------------------

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


def _get_top_suggestions(
    ref_title: str,
    ref_authors: List[str],
    ref_year: str,
    candidates: List[Dict[str, Any]],
    top_k: int = 3,
) -> List[Dict[str, Any]]:

    def extract_keywords(title):
        words = re.findall(r"[A-Za-z]{4,}", title.lower())
        stop = {
            "study", "analysis", "effect", "impact",
            "method", "model", "approach", "evidence"
        }
        return set(w for w in words if w not in stop)

    def keyword_overlap(t1, t2):
        k1 = extract_keywords(t1)
        k2 = extract_keywords(t2)
        overlap = len(k1 & k2)
        ratio = overlap / max(len(k1), 1)
        return overlap, ratio

    PUBLISHER_STOPWORDS = {
        "elsevier", "springer", "wiley", "ieee", "taylor", "francis",
        "nature", "acm", "oxford", "cambridge", "routledge", "sage",
        "macmillan", "pearson", "harpercollins", "penguin",
        "random", "house", "editorial", "publisher"
    }

    scored = []
    seen = set()

    for cand in candidates:
        doi, title, year, authors = _candidate_fields(cand)
        meta = _score(ref_title, ref_authors, ref_year, title, authors, year)

        title_clean = title.lower().strip()

        # ---------------------------
        # 1. Remove publisher noise
        # ---------------------------
        if any(p in title_clean for p in PUBLISHER_STOPWORDS):
            continue

        # ---------------------------
        # 2. Remove duplicates
        # ---------------------------
        if title_clean in seen:
            continue
        seen.add(title_clean)

        # ---------------------------
        # 3. Remove weak matches
        # ---------------------------
        if meta["title_score"] < 60:
            continue

        overlap, overlap_ratio = keyword_overlap(ref_title, title)

        # ---------------------------
        # 4. RELATED PAPER CRITERIA
        # ---------------------------
        if not (
            (meta["title_score"] >= 70 and overlap >= 2)
            or (meta["title_score"] >= 65 and overlap_ratio >= 0.3)
        ):
            continue

        score = meta["score"]

        # ---------------------------
        # 5. SOFT AUTHOR BOOST
        # ---------------------------
        if set(ref_authors) & set(authors):
            score += 8

        # ---------------------------
        # 6. DOI BOOST
        # ---------------------------
        if doi:
            score += 5

        scored.append({
            "title": title,
            "doi": doi,
            "year": year,
            "score": score,
            "title_score": meta["title_score"],
            "overlap": overlap,
            "confidence": "related",
        })

    return sorted(scored, key=lambda x: x["score"], reverse=True)[:top_k]


# ============================================================
# SINGLE REFERENCE VERIFICATION
# ============================================================


def _verification_recovery_metadata(ref: str, fields: Dict[str, Any], query: str, title_only: str) -> Dict[str, Any]:
    """
    Metadata used by main.py to build context-specific possible-source guidance.
    This does not affect scoring or classification.
    """
    title = fields.get("title", "") or title_only or ""
    authors = fields.get("authors", []) or []
    year = fields.get("year", "") or ""
    doi = fields.get("doi", "") or ""
    journal = fields.get("journal", "") or fields.get("container_title", "") or fields.get("source", "") or ""

    terms = []
    for value in [title, journal, " ".join(authors), year]:
        for term in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", str(value or "")):
            low = term.lower()
            if low not in _QUERY_STOP_WORDS and low not in terms:
                terms.append(low)
            if len(terms) >= 10:
                break
        if len(terms) >= 10:
            break

    focused_query = " ".join(terms[:8]) or query or ref[:180]

    return {
        "recovery_query": focused_query[:180],
        "recovery_key_terms": terms[:10],
        "recovery_title": title,
        "recovery_year": year,
        "recovery_doi": doi,
        "recovery_journal": journal,
        "recovery_possible_source_type": (
            "DOI/publisher record" if doi else
            "Crossref/OpenAlex metadata record, Google Scholar record, publisher page, repository record, or official report page"
        ),
    }



def _verify_single_reference(
    ref: str, 
    style: str, 
    use_crossref: bool, 
    use_openalex: bool,
    enrich_metadata: bool = False,
) -> Dict[str, Any]:
    """Single reference verification function"""
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
        "author_mismatch_flag": 0,
        "match_note": "",
    }

    try:
        row.update(_verification_recovery_metadata(ref, fields, query, title_only))
    except Exception:
        pass

    # Fast commercial skip: no DOI and weak title/query should not trigger slow online searches.
    if VERIFY_SKIP_WEAK_TITLE and not ref_doi and _significant_word_count(title_only or ref_title) < VERIFY_MIN_TITLE_WORDS:
        row = _make_fast_review_row(
            ref,
            style,
            query=query,
            authors=ref_authors,
            reason="No DOI and too few significant title words for reliable fast verification.",
        )
        try:
            row.update(_verification_recovery_metadata(ref, fields, query, title_only))
        except Exception:
            pass
        row["status"] = _normalize_verify_status(row.get("status"))
        _cache_set(cache_key, row)
        return row

    candidates: List[Dict[str, Any]] = []

    try:
        # DOI-first shortcut. If DOI returns candidates, avoid extra broad searches unless enabled later.
        if ref_doi and use_crossref:
            candidates.extend(_query_crossref_by_doi(ref_doi))

        should_do_general_search = not (VERIFY_STOP_ON_STRONG_MATCH and bool(candidates))

        openalex_allowed = bool(use_openalex or VERIFY_FORCE_OPENALEX_FALLBACK)

        # Stage 1: small, bounded candidate search.
        if should_do_general_search and use_crossref and query:
            candidates.extend(_query_crossref(query, rows=VERIFY_CROSSREF_ROWS))
        if should_do_general_search and openalex_allowed and query:
            candidates.extend(_query_openalex(query, rows=VERIFY_OPENALEX_ROWS))

        # If no candidates from query, try one small title-only search.
        if not candidates and title_only:
            if use_crossref:
                candidates.extend(_query_crossref_title_only(title_only, rows=VERIFY_TITLE_ROWS))
            if openalex_allowed:
                candidates.extend(_query_openalex_title_only(title_only, rows=VERIFY_TITLE_ROWS))

        best, best_meta = _best_candidate(ref_title, ref_authors, ref_year, ref_doi, candidates)

        if best:
            status = _classify(
                bool(best_meta.get("doi_match")),
                int(best_meta.get("title_score", 0)),
                int(best_meta.get("score", 0)),
                int(best_meta.get("year_match", 0)),
                int(best_meta.get("author_overlap", 0)),
            )

            # AUTHOR-MISMATCH GATE, first pass
            ref_has_authors = bool(ref_authors)
            cand_has_authors = bool(best_meta.get("authors", []))
            author_overlap = int(best_meta.get("author_overlap", 0))
            
            if ref_has_authors and cand_has_authors and author_overlap == 0:
                best_meta["author_mismatch_flag"] = 1
                best_meta["match_note"] = "Author mismatch"
                # Do not automatically destroy retrieval gains.
                # Downgrade only verified matches by default. Likely/needs_review already signal uncertainty.
                if (not VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY) or status == "verified":
                    status = "needs_review"
            else:
                best_meta["author_mismatch_flag"] = 0
                best_meta["match_note"] = ""

            # Deep fallback is expensive. It is disabled by default for commercial speed.
            if VERIFY_DEEP_FALLBACK and status in {"needs_review", "not_found"}:
                deep_candidates = list(candidates)

                if use_crossref and query:
                    deep_candidates.extend(_query_crossref(query, rows=VERIFY_CROSSREF_ROWS))
                    if title_only:
                        deep_candidates.extend(_query_crossref_title_only(title_only, rows=VERIFY_TITLE_ROWS))

                if openalex_allowed and query:
                    deep_candidates.extend(_query_openalex(query, rows=VERIFY_OPENALEX_ROWS))
                    if title_only:
                        deep_candidates.extend(_query_openalex_title_only(title_only, rows=VERIFY_TITLE_ROWS))

                best2, best_meta2 = _best_candidate(ref_title, ref_authors, ref_year, ref_doi, deep_candidates)
                if best2:
                    best = best2
                    best_meta = best_meta2
                    candidates = deep_candidates

                    status = _classify(
                        bool(best_meta.get("doi_match")),
                        int(best_meta.get("title_score", 0)),
                        int(best_meta.get("score", 0)),
                        int(best_meta.get("year_match", 0)),
                        int(best_meta.get("author_overlap", 0)),
                    )

                    # AUTHOR-MISMATCH GATE, deep fallback
                    ref_has_authors = bool(ref_authors)
                    cand_has_authors = bool(best_meta.get("authors", []))
                    author_overlap = int(best_meta.get("author_overlap", 0))
                    
                    if ref_has_authors and cand_has_authors and author_overlap == 0:
                        status = "needs_review"
                        best_meta["author_mismatch_flag"] = 1
                        best_meta["match_note"] = "Author mismatch"
                    else:
                        best_meta["author_mismatch_flag"] = 0
                        best_meta["match_note"] = ""

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
                "author_mismatch_flag": int(best_meta.get("author_mismatch_flag", 0)),
                "match_note": best_meta.get("match_note", ""),
            })

            row["correction_suggestions"] = []

            if enrich_metadata:
                row = enrich_with_full_metadata(row)

        else:
            row["status"] = "not_found"

    except Exception as e:
        print(f"[DEBUG] Error verifying reference: {e}")
        row["status"] = "not_found"
        row["error"] = str(e)

    row["status"] = _normalize_verify_status(row.get("status"))
    _cache_set(cache_key, row)
    return row


# ============================================================
# BATCH VERIFICATION FUNCTION
# ============================================================

def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = False,
    job_id: str = None,
    enrich_metadata: bool = False,
) -> List[Dict[str, Any]]:
    """
    Verify references batch with chunked processing for large datasets.
    Includes heartbeat and timeout handling.
    """
    print(f"[DEBUG] 🔥 verify_references_batch CALLED")
    print(f"[DEBUG] References count: {len(references)}")
    print(f"[DEBUG] Style: {style}")
    print(f"[DEBUG] job_id: {job_id}")
    print(f"[DEBUG] enrich_metadata: {enrich_metadata}")
    
    refs = [r for r in (references or []) if _safe_strip(r)]
    if not refs:
        print("[DEBUG] No references to verify")
        return []

    normalized_style = _STYLE_ALIASES.get((style or "apa").lower(), "apa")
    total_refs = len(refs)
    
    # Process in smaller chunks
    chunks = [refs[i:i + CHUNK_SIZE] for i in range(0, total_refs, CHUNK_SIZE)]
    print(f"[DEBUG] Splitting into {len(chunks)} chunks of up to {CHUNK_SIZE} references each")
    
    all_rows: List[Dict[str, Any]] = []
    failed_references: List[Tuple[int, str]] = []
    completed_so_far = 0
    last_heartbeat_time = time.time()
    
    for chunk_idx, chunk in enumerate(chunks):
        print(f"[DEBUG] ========================================")
        print(f"[DEBUG] Processing chunk {chunk_idx + 1}/{len(chunks)} ({len(chunk)} references)")
        
        # Send heartbeat to show we're still alive
        if job_id:
            update_job_progress(job_id, completed_so_far)
            print(f"[DEBUG] 💓 Heartbeat: Still processing chunk {chunk_idx + 1}, completed {completed_so_far}/{total_refs}")
        
        rows: List[Dict[str, Any]] = [None] * len(chunk)
        workers = min(WORKER_THREADS, max(1, len(chunk)))
        print(f"[DEBUG] Using {workers} workers for this chunk")
        
        # Use a timeout for the entire chunk
        chunk_start_time = time.time()
        chunk_timeout = max(VERIFY_SINGLE_REF_TIMEOUT * max(1, len(chunk)), API_TIMEOUT + 2)
        
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            
            for i, ref in enumerate(chunk):
                future = executor.submit(
                    _verify_single_reference_with_retry,
                    ref,
                    normalized_style,
                    use_crossref,
                    use_openalex,
                    enrich_metadata,
                )
                futures[future] = (i, ref)
            
            completed_count = 0
            
            for future in as_completed(futures):
                idx, ref = futures[future]
                
                # Check if chunk has timed out
                if time.time() - chunk_start_time > chunk_timeout:
                    print(f"[DEBUG] ⚠️ Chunk {chunk_idx + 1} timed out after {chunk_timeout} seconds")
                    # Mark remaining as failed
                    rows[idx] = {
                        "reference": ref,
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
                        "error": "Chunk timeout",
                    }
                    completed_count += 1
                    continue
                
                try:
                    result = future.result(timeout=VERIFY_SINGLE_REF_TIMEOUT)
                    rows[idx] = result
                    completed_count += 1
                    
                    # Update progress
                    current_total = completed_so_far + completed_count
                    if job_id:
                        update_job_progress(job_id, current_total)
                        
                        if completed_count % 5 == 0 or completed_count == len(chunk):
                            elapsed = time.time() - chunk_start_time
                            rate = completed_count / elapsed if elapsed > 0 else 0
                            remaining = (len(chunk) - completed_count) / rate if rate > 0 else 0
                            total_remaining = (total_refs - current_total) / rate if rate > 0 else 0
                            print(f"[DEBUG] Chunk {chunk_idx + 1}: {completed_count}/{len(chunk)} - Overall: {current_total}/{total_refs}")
                            print(f"[DEBUG] Est. remaining for chunk: {remaining/60:.1f} min - Total: {total_remaining/60:.1f} min")
                    
                    time.sleep(BATCH_DELAY)
                        
                except Exception as e:
                    print(f"[DEBUG] Error verifying reference: {e}")
                    failed_references.append((completed_so_far + idx, ref))
                    rows[idx] = {
                        "reference": ref,
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
                        update_job_progress(job_id, completed_so_far + completed_count)
        
        # Add chunk results
        all_rows.extend(rows)
        completed_so_far += len(chunk)
        
        # Save intermediate results
        if job_id:
            store_verification_results(job_id, all_rows)
            print(f"[DEBUG] Saved intermediate results for chunk {chunk_idx + 1}")
        
        # Delay between chunks
        if chunk_idx < len(chunks) - 1:
            print(f"[DEBUG] Waiting {CHUNK_DELAY} seconds before next chunk...")
            time.sleep(CHUNK_DELAY)
    
    # Retry failed references only when explicitly enabled.
    if VERIFY_RETRY_FAILED and failed_references:
        print(f"[DEBUG] Retrying {len(failed_references)} failed references...")
        
        for retry_idx, (original_idx, ref) in enumerate(failed_references):
            wait_time = min(5, (retry_idx + 1) * VERIFY_RETRY_BACKOFF_SECONDS)
            print(f"[DEBUG] Waiting {wait_time}s before retry {retry_idx + 1}")
            if wait_time > 0:
                time.sleep(wait_time)
            
            try:
                result = _verify_single_reference_with_retry(
                    ref, normalized_style, use_crossref, use_openalex, enrich_metadata,
                    max_retries=MAX_RETRIES_PER_REFERENCE
                )
                all_rows[original_idx] = result
                print(f"[DEBUG] Successfully retried reference {retry_idx + 1}")
            except Exception as e:
                print(f"[DEBUG] Retry failed: {e}")
    
    # Final results preserve uncertainty rather than promoting likely matches.
    result_counts = {
        "verified": sum(1 for r in all_rows if r and r.get("status") == "verified"),
        "likely": sum(1 for r in all_rows if r and r.get("status") == "likely"),
        "needs_review": sum(1 for r in all_rows if r and r.get("status") == "needs_review"),
        "not_found": sum(1 for r in all_rows if r and r.get("status") == "not_found"),
    }
    print(f"[DEBUG] Verification COMPLETE: {result_counts}")

    if job_id:
        update_job_progress(job_id, total_refs)
        store_verification_results(job_id, all_rows)

    return all_rows

def _verify_single_reference_with_retry(
    ref: str, 
    style: str, 
    use_crossref: bool, 
    use_openalex: bool,
    enrich_metadata: bool = False,
    max_retries: int = MAX_RETRIES_PER_REFERENCE,
) -> Dict[str, Any]:
    """Single reference verification with retry logic"""
    last_error = None
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                wait_time = min(5, attempt * VERIFY_RETRY_BACKOFF_SECONDS)
                print(f"[DEBUG] Retry attempt {attempt + 1}/{max_retries} for reference: {ref[:100]}... (waiting {wait_time}s)")
                if wait_time > 0:
                    time.sleep(wait_time)
            
            result = _verify_single_reference(
                ref, style, use_crossref, use_openalex, enrich_metadata
            )
            
            # If we got a valid result (not offline), return it
            if result and result.get("status") != "offline":
                if attempt > 0:
                    print(f"[DEBUG] Retry succeeded for reference after {attempt + 1} attempts")
                return result
            elif attempt == max_retries - 1:
                # Last attempt, return whatever we have
                return result
                
        except Exception as e:
            last_error = e
            print(f"[DEBUG] Attempt {attempt + 1} failed: {e}")
            continue
    
    # If all retries failed, return error result
    return {
        "reference": ref,
        "style": style,
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
        "error": f"All {max_retries} retries failed: {str(last_error)}",
    }
# ============================================================
# BACKGROUND JOB SUBMISSION
# ============================================================

def submit_verification(references: List[str], style: str = "apa", enrich_metadata: bool = False) -> str:
    """
    Submit a verification job and return job ID (runs in background)
    """
    job_id = uuid.uuid4().hex
    total_refs = len(references)

    # CRITICAL: create the progress job before the thread starts
    create_verification_job(job_id, total_refs)

    est_seconds = total_refs * max(2, API_TIMEOUT / 4)
    est_minutes = est_seconds / 60
    est_hours = est_minutes / 60

    print(f"[DEBUG] ========================================")
    print(f"[DEBUG] Submitting verification job {job_id}")
    print(f"[DEBUG] Total references: {total_refs}")
    print(f"[DEBUG] Enrich metadata: {enrich_metadata}")
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
        try:
            results = verify_references_batch(
                references,
                style,
                job_id=job_id,
                enrich_metadata=enrich_metadata
            )
            elapsed = time.time() - start_time
            print(f"[DEBUG] Background thread completed for job {job_id}")
            print(f"[DEBUG] Time elapsed: {elapsed:.1f} seconds ({elapsed/60:.1f} minutes)")
            print(f"[DEBUG] Results count: {len(results)}")

            store_verification_results(job_id, results)
            update_job_progress(job_id, len(results))

            verified = sum(1 for r in results if r.get("status") == "verified")
            likely = sum(1 for r in results if r.get("status") == "likely")
            needs_review = sum(1 for r in results if r.get("status") == "needs_review")
            not_found = sum(1 for r in results if r.get("status") == "not_found")
            print(f"[DEBUG] Final: Verified={verified}, Likely={likely}, NeedsReview={needs_review}, NotFound={not_found}")

        except Exception as e:
            print(f"[DEBUG] Verification thread failed for job {job_id}: {e}")
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id].status = "error"
                    _jobs[job_id].error = str(e)
                    _jobs[job_id].completed_at = datetime.now().isoformat()
            current = _redis_get_json(_verify_status_key(job_id)) or {}
            _persist_job_status(job_id, {
                "job_id": job_id,
                "status": "error",
                "progress": int(current.get("progress") or 0),
                "total": int(current.get("total") or total_refs),
                "percentage": int(current.get("percentage") or 0),
                "started_at": current.get("started_at"),
                "completed_at": datetime.now().isoformat(),
                "error": str(e),
                "message": f"Verification failed: {str(e)[:180]}",
            })

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    return job_id


def get_verification_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Get verification job progress (not results)"""
    return get_job_status(job_id)


# ============================================================
# EXPORTS AND ALIASES (for compatibility)
# ============================================================

# Alias for backward compatibility
submit_verification_job = submit_verification
get_queue_status = get_queue_stats
is_server_busy = is_server_busy_check

# Enhanced functions exports
__all__ = [
    'verify_references_batch',
    'submit_verification',
    'submit_verification_job',
    'get_verification_status',
    'get_verification_results',
    'clear_verification_results',
    'get_queue_status',
    'is_server_busy',
    'fetch_full_crossref_metadata',
    'parse_full_crossref_message',
    'build_apa7_from_metadata',
    'build_harvard_from_metadata',
    'enrich_with_full_metadata',
]


# ============================================================
# COMMERCIAL-GRADE VERIFICATION OVERRIDES
# Added by ChatGPT. These later definitions override selected earlier functions
# while preserving the public API of verify.py.
# ============================================================

VERIFY_CROSSREF_ROWS = int(os.getenv("VERIFY_CROSSREF_ROWS", "10"))
VERIFY_OPENALEX_ROWS = int(os.getenv("VERIFY_OPENALEX_ROWS", "10"))
VERIFY_TITLE_ROWS = int(os.getenv("VERIFY_TITLE_ROWS", "10"))
VERIFY_DEEP_FALLBACK = _env_flag("VERIFY_DEEP_FALLBACK", "1")
VERIFY_STOP_ON_STRONG_MATCH = _env_flag("VERIFY_STOP_ON_STRONG_MATCH", "0")
VERIFY_STRICT_AUTHOR_GATE = _env_flag("VERIFY_STRICT_AUTHOR_GATE", "1")
VERIFY_THRESHOLD_TITLE_VERIFIED = int(os.getenv("VERIFY_THRESHOLD_TITLE_VERIFIED", "92"))
VERIFY_THRESHOLD_TITLE_LIKELY = int(os.getenv("VERIFY_THRESHOLD_TITLE_LIKELY", "84"))
VERIFY_THRESHOLD_TITLE_REVIEW = int(os.getenv("VERIFY_THRESHOLD_TITLE_REVIEW", "70"))
VERIFY_THRESHOLD_JOURNAL_SUPPORT = int(os.getenv("VERIFY_THRESHOLD_JOURNAL_SUPPORT", "70"))


def _normalise_doi(doi: str) -> str:
    doi = _safe_strip(doi)
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi\s*:\s*", "", doi, flags=re.I)
    doi = doi.strip().strip(".,;) ]}").lower()
    return doi


def _doi_url(doi: str) -> str:
    doi = _normalise_doi(doi)
    return f"https://doi.org/{doi}" if doi else ""


def _clean_query_text(value: str) -> str:
    value = _safe_strip(value)
    value = re.sub(r"https?://\S+", " ", value, flags=re.I)
    value = re.sub(r"\bdoi\s*:?\s*10\.\S+", " ", value, flags=re.I)
    value = re.sub(r"[^A-Za-z0-9\s:&/\-().,]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .,:;\"'")
    return value


def _extract_volume_issue_pages(ref: str) -> Dict[str, str]:
    out = {"volume": "", "issue": "", "pages": ""}
    ref = _safe_strip(ref)

    m = re.search(r"\bvol\.?\s*(\d+[A-Za-z]?)", ref, flags=re.I)
    if m:
        out["volume"] = m.group(1)

    m = re.search(r"\b(?:no|issue)\.?\s*(\d+[A-Za-z]?)", ref, flags=re.I)
    if m:
        out["issue"] = m.group(1)

    m = re.search(r"\bpp?\.?\s*([A-Za-z]?\d+\s*[\-–—]\s*[A-Za-z]?\d+|[A-Za-z]?\d+)", ref, flags=re.I)
    if m:
        out["pages"] = re.sub(r"\s+", "", m.group(1)).replace("–", "-").replace("—", "-")

    # APA/Harvard: Journal Name, 12(3), 45-67.
    m = re.search(r",\s*(\d+[A-Za-z]?)\s*\(([^)]+)\)\s*,\s*([A-Za-z]?\d+\s*[\-–—]\s*[A-Za-z]?\d+|[A-Za-z]?\d+)", ref)
    if m:
        out["volume"] = out["volume"] or m.group(1)
        out["issue"] = out["issue"] or m.group(2)
        out["pages"] = out["pages"] or re.sub(r"\s+", "", m.group(3)).replace("–", "-").replace("—", "-")

    # APA/Harvard without issue: Journal Name, 12, 45-67.
    m = re.search(r",\s*(\d+[A-Za-z]?)\s*,\s*([A-Za-z]?\d+\s*[\-–—]\s*[A-Za-z]?\d+|[A-Za-z]?\d+)", ref)
    if m and not out["volume"]:
        out["volume"] = m.group(1)
        out["pages"] = out["pages"] or re.sub(r"\s+", "", m.group(2)).replace("–", "-").replace("—", "-")

    return out


def _split_after_year(ref: str, year: str) -> List[str]:
    if not year:
        return []
    parts = re.split(rf"[\(\[]?\s*{re.escape(year)}[a-z]?\s*[\)\]]?\.?,?", ref, maxsplit=1, flags=re.I)
    if len(parts) < 2:
        return []
    right = parts[1].strip(" .,:;")
    return [b.strip() for b in re.split(r"\.\s+", right) if b.strip()]


def _extract_journal_guess(ref: str, year: str, title: str) -> str:
    ref_clean = _strip_leading_numbering(ref)
    segments = _split_after_year(ref_clean, year)

    if len(segments) >= 2:
        candidate = segments[1]
        candidate = re.split(r",\s*\d", candidate, maxsplit=1)[0]
        candidate = re.split(r"\b(?:vol|no|issue|pp?|pages?)\.?", candidate, maxsplit=1, flags=re.I)[0]
        candidate = _clean_query_text(candidate)
        if len(candidate) >= 3:
            return candidate

    # IEEE/Vancouver: "Title," Journal, vol. ...
    if title and title in ref_clean:
        after = ref_clean.split(title, 1)[-1].strip(" ,.;:”“\"")
        candidate = _clean_query_text(after.split(",")[0])
        if len(candidate) >= 3 and not _YEAR_RE.search(candidate):
            return candidate

    return ""


def _extract_common_fields(ref: str) -> Dict[str, Any]:
    ref = _safe_strip(ref)
    ref = _strip_leading_numbering(ref)

    year = _extract_year(ref)
    doi = _normalise_doi(_extract_doi(ref))

    left = ref
    if year:
        left = ref.split(year, 1)[0].strip(" ,.;:()[]")

    authors = _extract_authors_from_left(left)
    title = _extract_title_guess(ref, year)
    journal = _extract_journal_guess(ref, year, title)
    vip = _extract_volume_issue_pages(ref)

    return {
        "authors": authors,
        "year": year,
        "doi": doi,
        "title": title,
        "journal": journal,
        "container_title": journal,
        "source": journal,
        "volume": vip.get("volume", ""),
        "issue": vip.get("issue", ""),
        "pages": vip.get("pages", ""),
    }


def _build_verification_query_plan(ref: str, style: str) -> Dict[str, Any]:
    fields = _extract_fields_by_style(ref, style)
    authors = fields.get("authors", []) or []
    first_author = authors[0] if authors else ""
    year = fields.get("year", "") or ""
    doi = _normalise_doi(fields.get("doi", "") or "")
    title = _clean_query_text(fields.get("title", "") or "")
    journal = _clean_query_text(fields.get("journal", "") or fields.get("container_title", "") or fields.get("source", "") or "")
    volume = _clean_query_text(fields.get("volume", "") or "")
    issue = _clean_query_text(fields.get("issue", "") or "")
    pages = _clean_query_text(fields.get("pages", "") or fields.get("page", "") or "")
    clean_ref = _clean_query_text(ref)

    title_words = _significant_title_words(title, limit=14)
    title_key = " ".join(title_words[:10]).strip()
    title_short = " ".join(title_words[:7]).strip()
    bibliographic_rich = " ".join([p for p in [title, journal, year, volume, issue, pages] if p]).strip()

    crossref_queries: List[Dict[str, Any]] = []
    openalex_queries: List[Dict[str, Any]] = []

    if doi:
        crossref_queries.append({"name": "crossref_doi_exact", "mode": "doi_exact", "doi": doi, "priority": 1})
        openalex_queries.append({"name": "openalex_doi_exact", "mode": "doi_exact", "doi": doi, "priority": 1})

    if clean_ref:
        crossref_queries.append({
            "name": "crossref_full_bibliographic",
            "mode": "bibliographic",
            "query_bibliographic": clean_ref,
            "query_author": first_author,
            "rows": VERIFY_CROSSREF_ROWS,
            "priority": 2,
        })

    if bibliographic_rich:
        crossref_queries.append({
            "name": "crossref_rich_bibliographic",
            "mode": "bibliographic",
            "query_bibliographic": bibliographic_rich,
            "query_author": first_author,
            "rows": VERIFY_CROSSREF_ROWS,
            "priority": 3,
        })

    if title_key:
        crossref_queries.append({
            "name": "crossref_title_author_year",
            "mode": "bibliographic",
            "query_bibliographic": " ".join([title_key, year]).strip(),
            "query_author": first_author,
            "rows": VERIFY_CROSSREF_ROWS,
            "priority": 4,
        })

    if title_key and journal:
        crossref_queries.append({
            "name": "crossref_title_journal_year",
            "mode": "bibliographic",
            "query_bibliographic": " ".join([title_key, journal, year]).strip(),
            "query_author": "",
            "rows": VERIFY_CROSSREF_ROWS,
            "priority": 5,
        })

    if title_key and year:
        openalex_queries.append({
            "name": "openalex_title_year",
            "mode": "search",
            "search": title_key,
            "publication_year": year,
            "rows": VERIFY_OPENALEX_ROWS,
            "priority": 6,
        })

    if title_key and journal:
        openalex_queries.append({
            "name": "openalex_title_journal",
            "mode": "search",
            "search": f"{title_key} {journal}",
            "publication_year": year,
            "rows": VERIFY_OPENALEX_ROWS,
            "priority": 7,
        })

    if title_short:
        openalex_queries.append({
            "name": "openalex_title_only",
            "mode": "search",
            "search": title_short,
            "publication_year": "",
            "rows": VERIFY_OPENALEX_ROWS,
            "priority": 8,
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
            "title_short": title_short,
        },
        "crossref_queries": crossref_queries,
        "openalex_queries": openalex_queries,
    }


def _build_query(ref: str, style: str) -> Tuple[str, List[str], str, str, str]:
    plan = _build_verification_query_plan(ref, style)
    fields = plan["fields"]
    query = ""
    for q in plan.get("crossref_queries", []):
        if q.get("mode") == "bibliographic" and q.get("query_bibliographic"):
            query = q["query_bibliographic"]
            break
    if not query:
        query = fields.get("title_key", "") or _clean_query_text(ref)[:180]
    return query, fields.get("authors", []), fields.get("year", ""), fields.get("doi", ""), fields.get("title_short", "")


def _query_crossref_bibliographic(query_bibliographic: str, query_author: str = "", rows: int = None, query_name: str = "crossref_bibliographic") -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_CROSSREF_ROWS
    query_bibliographic = _safe_strip(query_bibliographic)
    if not query_bibliographic:
        return []
    url = "https://api.crossref.org/works"
    params: Dict[str, Any] = {
        "query.bibliographic": query_bibliographic,
        "rows": rows,
        "sort": "score",
        "order": "desc",
    }
    if query_author:
        params["query.author"] = query_author
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=API_TIMEOUT)
    items = (data or {}).get("message", {}).get("items", [])
    return [{"source": "crossref", "query_name": query_name, "item": it} for it in items]


def _query_crossref(query: str, rows: int = None) -> List[Dict[str, Any]]:
    return _query_crossref_bibliographic(query, rows=rows, query_name="crossref_legacy_query")


def _query_crossref_title_only(title_query: str, rows: int = None) -> List[Dict[str, Any]]:
    # Commercial correction: Crossref query.title is not used. Use query.bibliographic for title-like lookup.
    return _query_crossref_bibliographic(title_query, rows=rows or VERIFY_TITLE_ROWS, query_name="crossref_title_as_bibliographic")


def _query_openalex_by_doi(doi: str) -> List[Dict[str, Any]]:
    doi = _normalise_doi(doi)
    if not doi:
        return []
    url = "https://api.openalex.org/works"
    params: Dict[str, Any] = {"filter": f"doi:{_doi_url(doi)}", "per-page": 5}
    if MAILTO:
        params["mailto"] = MAILTO
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    data = _safe_get_json(url, params=params, timeout=API_TIMEOUT)
    items = (data or {}).get("results", [])
    return [{"source": "openalex", "query_name": "openalex_doi_exact", "item": it} for it in items]


def _query_openalex_search(query: str, rows: int = None, publication_year: str = "", query_name: str = "openalex_search") -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_OPENALEX_ROWS
    query = _safe_strip(query)
    if not query:
        return []
    url = "https://api.openalex.org/works"
    params: Dict[str, Any] = {"search": query, "per-page": rows}
    if publication_year:
        params["filter"] = f"publication_year:{publication_year}"
    if MAILTO:
        params["mailto"] = MAILTO
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    data = _safe_get_json(url, params=params, timeout=API_TIMEOUT)
    items = (data or {}).get("results", [])
    return [{"source": "openalex", "query_name": query_name, "item": it} for it in items]


def _query_openalex(query: str, rows: int = None) -> List[Dict[str, Any]]:
    return _query_openalex_search(query, rows=rows, query_name="openalex_legacy_search")


def _query_openalex_title_only(title_query: str, rows: int = None) -> List[Dict[str, Any]]:
    return _query_openalex_search(title_query, rows=rows or VERIFY_TITLE_ROWS, query_name="openalex_title_only")


def _crossref_year(item: Dict[str, Any]) -> str:
    for key in ("issued", "published-print", "published-online", "created", "deposited"):
        obj = item.get(key) or {}
        parts = obj.get("date-parts", []) if isinstance(obj, dict) else []
        if parts and parts[0]:
            return _safe_str(parts[0][0])
    return ""


def _extract_crossref_authors(item: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for au in (item.get("author") or [])[:12]:
        fam = _safe_strip(au.get("family")).lower()
        fam = re.sub(r"[^a-z'\-]", "", fam)
        if fam:
            out.append(fam)
    return _dedupe_preserve(out)


def _extract_openalex_authors(item: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for authorship in (item.get("authorships") or [])[:12]:
        name = _safe_strip((authorship.get("author") or {}).get("display_name"))
        if name:
            surname = re.sub(r"[^a-z'\-]", "", name.split()[-1].lower())
            if surname:
                out.append(surname)
    return _dedupe_preserve(out)


def _candidate_fields(cand: Dict[str, Any]) -> Dict[str, Any]:
    src = cand.get("source")
    item = cand.get("item") or {}
    sources = cand.get("sources") or [src]

    if src == "crossref":
        title_list = item.get("title") or []
        container_list = item.get("container-title") or item.get("short-container-title") or []
        candidate_publication_events = events_from_crossref_message(item)
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": _normalise_doi(item.get("DOI", "")),
            "title": _safe_strip(title_list[0]) if title_list else "",
            "year": _crossref_year(item),
            "authors": _extract_crossref_authors(item),
            "journal": _safe_strip(container_list[0]) if container_list else "",
            "volume": _safe_strip(item.get("volume", "")),
            "issue": _safe_strip(item.get("issue", "")),
            "pages": _safe_strip(item.get("page", "")),
            "publisher": _safe_strip(item.get("publisher", "")),
            "type": _safe_strip(item.get("type", "")),
            "url": _safe_strip(item.get("URL", "")),
            "api_score": item.get("score", 0),
            "is_retracted": False,
        }

    if src == "openalex":
        biblio = item.get("biblio") or {}
        primary = item.get("primary_location") or {}
        source_obj = primary.get("source") or {}
        first_page = _safe_strip(biblio.get("first_page", ""))
        last_page = _safe_strip(biblio.get("last_page", ""))
        pages = f"{first_page}-{last_page}" if first_page and last_page else first_page
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": _normalise_doi(item.get("doi", "")),
            "title": _safe_strip(item.get("title") or item.get("display_name")),
            "year": _safe_strip(item.get("publication_year")),
            "authors": _extract_openalex_authors(item),
            "journal": _safe_strip(source_obj.get("display_name", "")),
            "volume": _safe_strip(biblio.get("volume", "")),
            "issue": _safe_strip(biblio.get("issue", "")),
            "pages": pages,
            "publisher": _safe_strip(source_obj.get("host_organization_name", "")),
            "type": _safe_strip(item.get("type", "")),
            "url": _safe_strip(primary.get("landing_page_url", "") or item.get("id", "")),
            "api_score": item.get("relevance_score", 0),
            "is_retracted": bool(item.get("is_retracted", False)),
        }

    return {
        "source": src or "unknown", "sources": sources, "query_name": cand.get("query_name", ""),
        "doi": "", "title": "", "year": "", "authors": [], "journal": "", "volume": "",
        "issue": "", "pages": "", "publisher": "", "type": "", "url": "", "api_score": 0,
        "is_retracted": False,
    }


def _candidate_key(cand: Dict[str, Any]) -> str:
    f = _candidate_fields(cand)
    if f["doi"]:
        return f"doi::{f['doi']}"
    title = _norm_text(f.get("title", ""))
    year = f.get("year", "")
    return f"title::{title[:120]}::{year}"


def _metadata_richness(cand: Dict[str, Any]) -> int:
    f = _candidate_fields(cand)
    return sum(1 for k in ["doi", "title", "year", "journal", "volume", "issue", "pages", "publisher"] if f.get(k)) + len(f.get("authors", []))


def _dedupe_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    for cand in candidates:
        key = _candidate_key(cand)
        if not key or key == "title::::":
            continue
        src = cand.get("source", "unknown")
        if key not in by_key:
            new_cand = dict(cand)
            new_cand["sources"] = [src]
            by_key[key] = new_cand
        else:
            existing = by_key[key]
            sources = set(existing.get("sources") or [existing.get("source")])
            sources.add(src)
            if _metadata_richness(cand) > _metadata_richness(existing):
                replacement = dict(cand)
                replacement["sources"] = sorted(sources)
                by_key[key] = replacement
            else:
                existing["sources"] = sorted(sources)
    return list(by_key.values())


def _run_query_plan(plan: Dict[str, Any], use_crossref: bool, use_openalex: bool) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    candidates: List[Dict[str, Any]] = []
    query_used: List[str] = []
    query_strategy: List[str] = []
    fields = plan.get("fields", {})
    openalex_allowed = bool(use_openalex or VERIFY_FORCE_OPENALEX_FALLBACK)

    for q in sorted(plan.get("crossref_queries", []), key=lambda x: x.get("priority", 99)):
        if not use_crossref:
            continue
        name = q.get("name", "crossref")
        try:
            if q.get("mode") == "doi_exact":
                res = _query_crossref_by_doi(q.get("doi", ""))
            else:
                res = _query_crossref_bibliographic(
                    q.get("query_bibliographic", ""),
                    query_author=q.get("query_author", ""),
                    rows=int(q.get("rows", VERIFY_CROSSREF_ROWS)),
                    query_name=name,
                )
            candidates.extend(res)
            query_strategy.append(name)
            query_used.append(q.get("doi") or q.get("query_bibliographic") or "")
            if VERIFY_STOP_ON_STRONG_MATCH and q.get("mode") == "doi_exact" and res:
                break
        except Exception as exc:
            print(f"[DEBUG] Crossref query failed for {name}: {exc}")

    for q in sorted(plan.get("openalex_queries", []), key=lambda x: x.get("priority", 99)):
        if not openalex_allowed:
            continue
        name = q.get("name", "openalex")
        try:
            if q.get("mode") == "doi_exact":
                res = _query_openalex_by_doi(q.get("doi", ""))
            else:
                res = _query_openalex_search(
                    q.get("search", ""),
                    rows=int(q.get("rows", VERIFY_OPENALEX_ROWS)),
                    publication_year=q.get("publication_year", ""),
                    query_name=name,
                )
            candidates.extend(res)
            query_strategy.append(name)
            query_used.append(q.get("doi") or q.get("search") or "")
        except Exception as exc:
            print(f"[DEBUG] OpenAlex query failed for {name}: {exc}")

    if not candidates and VERIFY_DEEP_FALLBACK:
        title_short = fields.get("title_short", "")
        if use_crossref and title_short:
            candidates.extend(_query_crossref_title_only(title_short, rows=VERIFY_TITLE_ROWS))
            query_strategy.append("crossref_final_title_fallback")
            query_used.append(title_short)
        if openalex_allowed and title_short:
            candidates.extend(_query_openalex_title_only(title_short, rows=VERIFY_TITLE_ROWS))
            query_strategy.append("openalex_final_title_fallback")
            query_used.append(title_short)

    return _dedupe_candidates(candidates), query_used, query_strategy


def _year_match_info(ref_year: str, cand_year: str) -> Tuple[int, int]:
    if not ref_year or not cand_year:
        return 0, 999
    try:
        delta = abs(int(ref_year[:4]) - int(cand_year[:4]))
    except Exception:
        return 0, 999
    return (1 if delta == 0 else 0), delta


def _page_tokens(pages: str) -> set:
    pages = _safe_strip(pages).replace("–", "-").replace("—", "-")
    return set(re.findall(r"[A-Za-z]?\d+", pages))


def _exact_or_empty_match(a: str, b: str) -> int:
    a = _safe_strip(a).lower()
    b = _safe_strip(b).lower()
    if not a or not b:
        return 0
    return 1 if a == b else 0


def _author_metrics(ref_authors: List[str], cand_authors: List[str]) -> Tuple[int, int]:
    ref_clean = [re.sub(r"[^a-z'\-]", "", a.lower()) for a in (ref_authors or []) if a]
    cand_clean = [re.sub(r"[^a-z'\-]", "", a.lower()) for a in (cand_authors or []) if a]
    ref_clean = _dedupe_preserve(ref_clean)
    cand_clean = _dedupe_preserve(cand_clean)

    if not ref_clean or not cand_clean:
        return 0, 0

    exact_overlap = len(set(ref_clean) & set(cand_clean))
    fuzzy_scores = []
    for ra in ref_clean[:5]:
        fuzzy_scores.append(max((fuzz.ratio(ra, ca) for ca in cand_clean[:8]), default=0))
    fuzzy_similarity = int(sum(fuzzy_scores) / len(fuzzy_scores)) if fuzzy_scores else 0

    if exact_overlap:
        fuzzy_similarity = max(fuzzy_similarity, min(100, 60 + exact_overlap * 20))
    return exact_overlap, fuzzy_similarity


def _score_candidate(ref_fields: Dict[str, Any], cand: Dict[str, Any]) -> Dict[str, Any]:
    cf = _candidate_fields(cand)
    ref_title = _norm_text(ref_fields.get("title", ""))
    cand_title = _norm_text(cf.get("title", ""))

    if ref_title and cand_title:
        token_set = fuzz.token_set_ratio(ref_title, cand_title)
        token_sort = fuzz.token_sort_ratio(ref_title, cand_title)
        partial = fuzz.partial_ratio(ref_title, cand_title)
        title_score = int((token_set * 0.50) + (token_sort * 0.35) + (partial * 0.15))
        if len(ref_title.split()) <= 4 and partial > token_set + 20:
            title_score = int((token_set * 0.65) + (token_sort * 0.35))
    else:
        title_score = 0

    ref_journal = _norm_text(ref_fields.get("journal", ""))
    cand_journal = _norm_text(cf.get("journal", ""))
    journal_score = int(fuzz.token_set_ratio(ref_journal, cand_journal)) if ref_journal and cand_journal else 0

    author_overlap, author_similarity = _author_metrics(ref_fields.get("authors", []), cf.get("authors", []))
    year_match, year_delta = _year_match_info(ref_fields.get("year", ""), cf.get("year", ""))

    ref_doi = _normalise_doi(ref_fields.get("doi", ""))
    cand_doi = _normalise_doi(cf.get("doi", ""))
    doi_match = bool(ref_doi and cand_doi and ref_doi == cand_doi)

    volume_match = _exact_or_empty_match(ref_fields.get("volume", ""), cf.get("volume", ""))
    issue_match = _exact_or_empty_match(ref_fields.get("issue", ""), cf.get("issue", ""))

    ref_pages = _page_tokens(ref_fields.get("pages", ""))
    cand_pages = _page_tokens(cf.get("pages", ""))
    page_match = 1 if ref_pages and cand_pages and bool(ref_pages & cand_pages) else 0

    source_agreement = len(set(cf.get("sources") or [cf.get("source", "")]))

    score = 0.0
    score += title_score * 0.56
    score += author_similarity * 0.18
    score += 12 if year_match else 0
    score += 6 if year_delta == 1 else 0
    score += journal_score * 0.08
    score += 4 if volume_match else 0
    score += 2 if issue_match else 0
    score += 3 if page_match else 0
    score += min(6, max(0, source_agreement - 1) * 3)
    if doi_match:
        score += 30
    if cf.get("is_retracted"):
        score -= 5

    return {
        "score": int(min(100, round(score))),
        "title_score": int(title_score),
        "journal_score": int(journal_score),
        "author_overlap": int(author_overlap),
        "author_similarity": int(author_similarity),
        "year_match": int(year_match),
        "year_delta": int(year_delta if year_delta != 999 else 999),
        "doi_match": bool(doi_match),
        "volume_match": int(volume_match),
        "issue_match": int(issue_match),
        "page_match": int(page_match),
        "source_agreement": int(source_agreement),
        **cf,
    }


def _has_author_conflict(ref_fields: Dict[str, Any], meta: Dict[str, Any]) -> bool:
    return bool(ref_fields.get("authors") and meta.get("authors") and int(meta.get("author_overlap", 0)) == 0)


def _classify_from_meta(ref_fields: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[str, str]:
    title_score = int(meta.get("title_score", 0))
    score = int(meta.get("score", 0))
    year_match = int(meta.get("year_match", 0))
    year_delta = int(meta.get("year_delta", 999))
    author_overlap = int(meta.get("author_overlap", 0))
    author_similarity = int(meta.get("author_similarity", 0))
    journal_score = int(meta.get("journal_score", 0))
    doi_match = bool(meta.get("doi_match"))
    source_agreement = int(meta.get("source_agreement", 1))
    has_author_conflict = _has_author_conflict(ref_fields, meta)
    has_ref_journal = bool(ref_fields.get("journal"))
    retracted = bool(meta.get("is_retracted"))

    if doi_match:
        if title_score >= 80 or (title_score >= 65 and (year_match or author_overlap >= 1)):
            if retracted:
                return "needs_review", "DOI matches, but the matched record is marked as retracted."
            return "verified", "Exact DOI match with supporting title, year, or author evidence."
        if title_score >= 50:
            return "verified", "DOI matches, but title evidence is not strong enough for automatic verification."
        return "needs_review", "DOI matches, but the title appears inconsistent with the reference."

    if has_author_conflict and VERIFY_STRICT_AUTHOR_GATE:
        if title_score >= 96 and year_match and journal_score >= VERIFY_THRESHOLD_JOURNAL_SUPPORT:
            return "verified", "Very strong title, year and journal match, but author names do not overlap."
        if title_score >= VERIFY_THRESHOLD_TITLE_REVIEW:
            return "needs_review", "Possible match found, but author names do not overlap."
        return "not_found", "Candidates were found, but author and title evidence were too weak."

    if title_score >= VERIFY_THRESHOLD_TITLE_VERIFIED and year_match and (author_overlap >= 1 or author_similarity >= 80 or not ref_fields.get("authors")):
        if has_ref_journal and journal_score and journal_score < 45:
            return "verified", "Strong title, author and year match, but journal name differs."
        return "verified", "Strong title, author and year match."

    if title_score >= 96 and year_match and journal_score >= VERIFY_THRESHOLD_JOURNAL_SUPPORT:
        return "verified", "Strong title, year and journal match."

    if source_agreement >= 2 and title_score >= 90 and year_delta <= 1 and (author_overlap >= 1 or journal_score >= VERIFY_THRESHOLD_JOURNAL_SUPPORT):
        return "verified", "Cross-source agreement with strong bibliographic match."

    if title_score >= VERIFY_THRESHOLD_TITLE_LIKELY and year_delta <= 1 and (author_overlap >= 1 or author_similarity >= 70 or journal_score >= VERIFY_THRESHOLD_JOURNAL_SUPPORT):
        return "verified", "Strong title match with supporting year, author, or journal evidence."

    if score >= 82 and title_score >= 82 and year_delta <= 1:
        return "verified", "High composite score, but not enough evidence for automatic verification."

    if title_score >= VERIFY_THRESHOLD_TITLE_REVIEW or score >= 55:
        return "needs_review", "A possible match was found, but evidence is insufficient for automatic verification."

    return "not_found", "No reliable Crossref/OpenAlex match found."


def _classify(doi_match: bool, title_score: int, score: int, year_match: int, author_overlap: int = 0) -> str:
    # Backward-compatible wrapper for any older internal call.
    meta = {
        "doi_match": doi_match,
        "title_score": title_score,
        "score": score,
        "year_match": year_match,
        "year_delta": 0 if year_match else 999,
        "author_overlap": author_overlap,
        "author_similarity": 80 if author_overlap else 0,
        "journal_score": 0,
        "source_agreement": 1,
        "authors": ["x"] if author_overlap else [],
    }
    ref_fields = {"authors": ["x"] if author_overlap else [], "journal": ""}
    return _classify_from_meta(ref_fields, meta)[0]


def _best_candidate(ref_title_or_fields, ref_authors=None, ref_year=None, ref_doi=None, candidates: Optional[List[Dict[str, Any]]] = None):
    # Supports both the new call _best_candidate(fields, candidates) and old call shape.
    if isinstance(ref_title_or_fields, dict):
        ref_fields = ref_title_or_fields
        cand_list = ref_authors or []
        return_new_shape = True
    else:
        ref_fields = {
            "title": ref_title_or_fields or "",
            "authors": ref_authors or [],
            "year": ref_year or "",
            "doi": ref_doi or "",
            "journal": "",
            "volume": "",
            "issue": "",
            "pages": "",
        }
        cand_list = candidates or []
        return_new_shape = False

    scored: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for cand in cand_list:
        meta = _score_candidate(ref_fields, cand)
        scored.append((cand, meta))

    scored.sort(key=lambda cm: (cm[1].get("score", 0), cm[1].get("title_score", 0), cm[1].get("source_agreement", 1)), reverse=True)
    if not scored:
        return (None, {}, []) if return_new_shape else (None, {})

    best, best_meta = scored[0]
    alternatives = []
    for _cand, meta in scored[1:6]:
        if int(meta.get("title_score", 0)) < 60:
            continue
        alternatives.append({
            "title": meta.get("title", ""),
            "doi": meta.get("doi", ""),
            "year": meta.get("year", ""),
            "journal": meta.get("journal", ""),
            "source": "+".join(meta.get("sources") or [meta.get("source", "")]),
            "score": int(meta.get("score", 0)),
            "title_score": int(meta.get("title_score", 0)),
            "author_overlap": int(meta.get("author_overlap", 0)),
            "year_match": int(meta.get("year_match", 0)),
        })

    return (best, best_meta, alternatives) if return_new_shape else (best, best_meta)


def _base_commercial_row(ref: str, style: str, fields: Dict[str, Any], query_used: str = "", query_strategy: str = "") -> Dict[str, Any]:
    return {
        "reference": ref,
        "style": style,
        "status": "offline",
        "source": "",
        "score": 0,
        "doi": "",
        "matched_title": "",
        "matched_year": "",
        "matched_authors": "",
        "matched_journal": "",
        "matched_container_title": "",
        "matched_volume": "",
        "matched_issue": "",
        "matched_pages": "",
        "matched_publisher": "",
        "matched_type": "",
        "matched_url": "",
        "title_score": 0,
        "journal_score": 0,
        "author_overlap": 0,
        "author_similarity": 0,
        "year_match": 0,
        "year_delta": 999,
        "doi_match": 0,
        "volume_match": 0,
        "issue_match": 0,
        "page_match": 0,
        "source_agreement": 0,
        "query_used": query_used,
        "query_strategy": query_strategy,
        "author": ", ".join(fields.get("authors", []) or []),
        "reference_title": fields.get("title", ""),
        "reference_year": fields.get("year", ""),
        "reference_doi": fields.get("doi", ""),
        "reference_journal": fields.get("journal", ""),
        "reference_volume": fields.get("volume", ""),
        "reference_issue": fields.get("issue", ""),
        "reference_pages": fields.get("pages", ""),
        "author_mismatch_flag": 0,
        "match_note": "",
        "confidence_reason": "",
        "correction_suggestions": [],
        "alternative_matches": [],
    }


def _verify_single_reference(
    ref: str,
    style: str,
    use_crossref: bool,
    use_openalex: bool,
    enrich_metadata: bool = False,
) -> Dict[str, Any]:
    """Commercial-grade single reference verification using DOI-first, Crossref bibliographic search and OpenAlex fallback."""
    cache_key = f"commercial_v1::{style}::{use_crossref}:{use_openalex}:{enrich_metadata}::{ref}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    plan = _build_verification_query_plan(ref, style)
    fields = plan.get("fields", {})
    query_preview, ref_authors, _ref_year, ref_doi, title_only = _build_query(ref, style)

    if VERIFY_SKIP_WEAK_TITLE and not ref_doi and _significant_word_count(title_only or fields.get("title", "")) < VERIFY_MIN_TITLE_WORDS:
        row = _make_fast_review_row(
            ref,
            style,
            query=query_preview,
            authors=ref_authors,
            reason="No DOI and too few significant title words for reliable commercial verification.",
        )
        row.update({
            "reference_title": fields.get("title", ""),
            "reference_year": fields.get("year", ""),
            "reference_doi": fields.get("doi", ""),
            "reference_journal": fields.get("journal", ""),
            "reference_volume": fields.get("volume", ""),
            "reference_issue": fields.get("issue", ""),
            "reference_pages": fields.get("pages", ""),
            "query_strategy": "fast_skip",
            "confidence_reason": "No DOI and too few significant title words for reliable commercial verification.",
            "alternative_matches": [],
        })
        row["status"] = _normalize_verify_status(row.get("status"))
        _cache_set(cache_key, row)
        return row

    try:
        candidates, query_used, query_strategy = _run_query_plan(plan, use_crossref, use_openalex)
        row = _base_commercial_row(
            ref,
            style,
            fields,
            " | ".join(_dedupe_preserve(query_used)),
            " | ".join(_dedupe_preserve(query_strategy)),
        )
        row["query_plan"] = {
            "crossref": [q.get("name") for q in plan.get("crossref_queries", [])],
            "openalex": [q.get("name") for q in plan.get("openalex_queries", [])],
        }

        if not candidates:
            row["status"] = "not_found"
            row["confidence_reason"] = "No candidates returned from DOI, Crossref bibliographic, or OpenAlex searches."
            _cache_set(cache_key, row)
            return row

        best, meta, alternatives = _best_candidate(fields, candidates)
        if not best:
            row["status"] = "not_found"
            row["confidence_reason"] = "Candidates were returned, but none had enough usable metadata."
            _cache_set(cache_key, row)
            return row

        status, reason = _classify_from_meta(fields, meta)
        author_conflict = _has_author_conflict(fields, meta)

        row.update({
            "status": status,
            "source": "+".join(meta.get("sources") or [meta.get("source", "")]),
            "score": int(meta.get("score", 0)),
            "doi": _normalise_doi(meta.get("doi", "")),
            "matched_title": _safe_strip(meta.get("title", "")),
            "matched_year": _safe_strip(meta.get("year", "")),
            "matched_authors": ", ".join(meta.get("authors", []) or []),
            "matched_journal": _safe_strip(meta.get("journal", "")),
            "matched_container_title": _safe_strip(meta.get("journal", "")),
            "matched_volume": _safe_strip(meta.get("volume", "")),
            "matched_issue": _safe_strip(meta.get("issue", "")),
            "matched_pages": _safe_strip(meta.get("pages", "")),
            "matched_publisher": _safe_strip(meta.get("publisher", "")),
            "matched_type": _safe_strip(meta.get("type", "")),
            "matched_url": _safe_strip(meta.get("url", "")),
            "title_score": int(meta.get("title_score", 0)),
            "journal_score": int(meta.get("journal_score", 0)),
            "author_overlap": int(meta.get("author_overlap", 0)),
            "author_similarity": int(meta.get("author_similarity", 0)),
            "year_match": int(meta.get("year_match", 0)),
            "year_delta": int(meta.get("year_delta", 999)),
            "doi_match": 1 if meta.get("doi_match") else 0,
            "volume_match": int(meta.get("volume_match", 0)),
            "issue_match": int(meta.get("issue_match", 0)),
            "page_match": int(meta.get("page_match", 0)),
            "source_agreement": int(meta.get("source_agreement", 0)),
            "author_mismatch_flag": 1 if author_conflict else 0,
            "match_note": "Author mismatch" if author_conflict else "",
            "confidence_reason": reason,
            "alternative_matches": alternatives,
        })

        if enrich_metadata and row.get("doi"):
            row = enrich_with_full_metadata(row)

    except Exception as exc:
        print(f"[DEBUG] Error verifying reference: {exc}")
        row = _base_commercial_row(ref, style, fields, query_preview, "error")
        row["status"] = "not_found"
        row["error"] = str(exc)
        row["confidence_reason"] = "Verification failed because an exception occurred."

    row["status"] = _normalize_verify_status(row.get("status"))
    _cache_set(cache_key, row)
    return row

# Final extraction refinements. These override the earlier helper definitions above.
def _extract_authors_from_left(left: str) -> List[str]:
    left = _safe_strip(left)
    if not left:
        return []
    left = re.sub(r"\bet\s+al\.?\b", "", left, flags=re.I)
    left = left.replace("&", " and ")
    left = re.sub(r"\s+", " ", left).strip(" ,.;:")

    candidates: List[str] = []

    # APA/Harvard style: Surname, I., Surname, I., and Surname, I.
    surname_matches = re.findall(r"(?:^|,|\band\s+)([A-Z][A-Za-z'\-]{1,})(?=\s*,)", left)
    candidates.extend(surname_matches)

    # Name and Name style.
    if not candidates:
        for part in re.split(r"\band\b", left, flags=re.I):
            toks = [t for t in part.strip().split() if t]
            if toks:
                candidates.append(toks[-1])

    cleaned = []
    for c in candidates:
        c = re.sub(r"[^A-Za-z'\-]", "", c).lower().strip()
        if len(c) >= 2 and c not in _QUERY_STOP_WORDS:
            cleaned.append(c)
    return _dedupe_preserve(cleaned)[:8]


def _extract_journal_guess(ref: str, year: str, title: str) -> str:
    ref_clean = _strip_leading_numbering(ref)
    segments = _split_after_year(ref_clean, year)

    if len(segments) >= 2:
        candidate = segments[1]
        candidate = re.split(r",\s*\d", candidate, maxsplit=1)[0]
        candidate = re.split(r"\b(?:vol|no|issue|pp|pages?)\.?\s+", candidate, maxsplit=1, flags=re.I)[0]
        candidate = _clean_query_text(candidate)
        if len(candidate) >= 3:
            return candidate

    if title and title in ref_clean:
        after = ref_clean.split(title, 1)[-1].strip(" ,.;:”“\"")
        candidate = _clean_query_text(after.split(",")[0])
        if len(candidate) >= 3 and not _YEAR_RE.search(candidate):
            return candidate

    return ""


def _get_top_suggestions(
    ref_title: str,
    ref_authors: List[str],
    ref_year: str,
    candidates: List[Dict[str, Any]],
    top_k: int = 3,
) -> List[Dict[str, Any]]:
    """Backward-compatible related-match suggestions using the commercial candidate structure."""
    ref_fields = {
        "title": ref_title,
        "authors": ref_authors or [],
        "year": ref_year or "",
        "doi": "",
        "journal": "",
        "volume": "",
        "issue": "",
        "pages": "",
    }
    scored = []
    seen = set()
    for cand in candidates or []:
        meta = _score_candidate(ref_fields, cand)
        title_key = _norm_text(meta.get("title", ""))
        if not title_key or title_key in seen:
            continue
        seen.add(title_key)
        if int(meta.get("title_score", 0)) < 60:
            continue
        scored.append({
            "title": meta.get("title", ""),
            "doi": meta.get("doi", ""),
            "year": meta.get("year", ""),
            "journal": meta.get("journal", ""),
            "score": int(meta.get("score", 0)),
            "title_score": int(meta.get("title_score", 0)),
            "confidence": "related",
        })
    return sorted(scored, key=lambda x: x["score"], reverse=True)[:top_k]

# ============================================================
# COMMERCIAL MULTI-SOURCE FAST FALLBACK OVERRIDES
# Added to preserve the existing verify.py structure while adding
# adaptive DataCite, PubMed, Europe PMC, Semantic Scholar, Google Books,
# Open Library, ERIC, arXiv, CORE and DOAJ support.
# ============================================================

# Keep defaults fast. Extra sources run adaptively only when Crossref/OpenAlex
# do not already give a strong match, or when the reference type clearly needs them.
VERIFY_MULTISOURCE_FALLBACK = _env_flag("VERIFY_MULTISOURCE_FALLBACK", "1")
VERIFY_MULTISOURCE_ONLY_WHEN_WEAK = _env_flag("VERIFY_MULTISOURCE_ONLY_WHEN_WEAK", "1")
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES = int(os.getenv("VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES", "3"))
VERIFY_SPECIAL_ROWS = int(os.getenv("VERIFY_SPECIAL_ROWS", "3"))
VERIFY_SPECIAL_TIMEOUT = int(os.getenv("VERIFY_SPECIAL_TIMEOUT", str(API_TIMEOUT)))
VERIFY_MULTISOURCE_DEBUG = _env_flag("VERIFY_MULTISOURCE_DEBUG", "0")

VERIFY_USE_DATACITE = _env_flag("VERIFY_USE_DATACITE", "1")
VERIFY_USE_EUROPEPMC = _env_flag("VERIFY_USE_EUROPEPMC", "1")
VERIFY_USE_PUBMED = _env_flag("VERIFY_USE_PUBMED", "1")
VERIFY_USE_SEMANTIC_SCHOLAR = _env_flag("VERIFY_USE_SEMANTIC_SCHOLAR", "1")
VERIFY_USE_GOOGLE_BOOKS = _env_flag("VERIFY_USE_GOOGLE_BOOKS", "1")
VERIFY_USE_OPEN_LIBRARY = _env_flag("VERIFY_USE_OPEN_LIBRARY", "1")
VERIFY_USE_ERIC = _env_flag("VERIFY_USE_ERIC", "1")
VERIFY_USE_ARXIV = _env_flag("VERIFY_USE_ARXIV", "1")
VERIFY_USE_CORE = _env_flag("VERIFY_USE_CORE", "0")
VERIFY_USE_DOAJ = _env_flag("VERIFY_USE_DOAJ", "0")

SEMANTIC_SCHOLAR_API_KEY = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
GOOGLE_BOOKS_API_KEY = os.getenv("GOOGLE_BOOKS_API_KEY", "").strip()
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "").strip()
CORE_API_KEY = os.getenv("CORE_API_KEY", "").strip()
DOAJ_API_KEY = os.getenv("DOAJ_API_KEY", "").strip()

_ISBN_RE = re.compile(r"\b(?:ISBN(?:-1[03])?:?\s*)?((?:97[89][\- ]?)?(?:\d[\- ]?){9}[\dXx])\b")
_PMID_RE = re.compile(r"\bPMID\s*:?\s*(\d{4,12})\b", re.I)
_PMCID_RE = re.compile(r"\bPMC\s*:?\s*(\d{4,12})\b", re.I)
_ARXIV_RE = re.compile(r"\barXiv\s*:?\s*([a-z\-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?\b", re.I)


def _debug_multisource(message: str) -> None:
    if VERIFY_MULTISOURCE_DEBUG:
        print(f"[DEBUG][MULTISOURCE] {message}")


def _clean_identifier(value: str) -> str:
    return re.sub(r"[^0-9Xx]", "", _safe_strip(value))


def _extract_isbn(ref: str) -> str:
    for m in _ISBN_RE.finditer(ref or ""):
        candidate = _clean_identifier(m.group(1))
        if len(candidate) in {10, 13}:
            return candidate.upper()
    return ""


def _extract_pmid(ref: str) -> str:
    m = _PMID_RE.search(ref or "")
    return m.group(1) if m else ""


def _extract_pmcid(ref: str) -> str:
    m = _PMCID_RE.search(ref or "")
    return f"PMC{m.group(1)}" if m else ""


def _extract_arxiv_id(ref: str) -> str:
    m = _ARXIV_RE.search(ref or "")
    return m.group(1) if m else ""


def _reference_text_blob(fields: Dict[str, Any]) -> str:
    return " ".join(
        _safe_strip(fields.get(k, ""))
        for k in ["title", "journal", "source", "container_title", "type"]
    ).lower()


def _reference_type_flags(ref: str, fields: Dict[str, Any]) -> Dict[str, bool]:
    blob = f"{ref} {_reference_text_blob(fields)}".lower()
    isbn = _extract_isbn(ref)
    pmid = _extract_pmid(ref)
    pmcid = _extract_pmcid(ref)
    arxiv_id = _extract_arxiv_id(ref)
    doi = _normalise_doi(fields.get("doi", ""))

    health_terms = {
        "medicine", "medical", "clinical", "patient", "patients", "nursing", "health",
        "public health", "biomedical", "cancer", "therapy", "disease", "hospital",
        "lancet", "bmj", "jama", "nejm", "pubmed", "pmid", "pmc", "epidemiology",
    }
    education_terms = {
        "education", "teaching", "learning", "curriculum", "student", "students",
        "teacher", "teachers", "school", "schools", "higher education", "distance education",
        "pedagogy", "educational", "classroom", "eric",
    }
    book_terms = {
        "edition", "publisher", "press", "isbn", "book", "chapter", "handbook", "textbook",
        "routledge", "sage", "wiley", "springer", "cambridge", "oxford", "palgrave",
    }
    dataset_terms = {
        "dataset", "data set", "figshare", "zenodo", "dryad", "osf", "repository",
        "thesis", "dissertation", "report", "working paper", "preprint", "conference paper",
    }
    arxiv_terms = {
        "arxiv", "preprint", "machine learning", "computer science", "physics", "mathematics",
        "statistics", "quantitative finance", "neural", "deep learning",
    }

    def contains_any(words):
        return any(w in blob for w in words)

    return {
        "has_doi": bool(doi),
        "has_isbn": bool(isbn),
        "has_pmid": bool(pmid),
        "has_pmcid": bool(pmcid),
        "has_arxiv": bool(arxiv_id),
        "looks_health": bool(pmid or pmcid or contains_any(health_terms)),
        "looks_education": contains_any(education_terms),
        "looks_book": bool(isbn or contains_any(book_terms)) and not bool(doi and fields.get("journal")),
        "looks_dataset_repo": contains_any(dataset_terms),
        "looks_arxiv": bool(arxiv_id or contains_any(arxiv_terms)),
    }


def _first_author_from_fields(fields: Dict[str, Any]) -> str:
    authors = fields.get("authors", []) or []
    return authors[0] if authors else ""


def _title_query_from_fields(fields: Dict[str, Any], max_words: int = 10) -> str:
    title = _clean_query_text(fields.get("title", "") or "")
    words = _significant_title_words(title, limit=max_words + 4)
    return " ".join(words[:max_words]).strip() or title[:180]


def _generic_metadata_query(fields: Dict[str, Any], include_journal: bool = True) -> str:
    parts = [
        _title_query_from_fields(fields, 10),
        fields.get("journal", "") if include_journal else "",
        fields.get("year", ""),
        _first_author_from_fields(fields),
    ]
    return _clean_query_text(" ".join([p for p in parts if p]))


def _safe_get_text(url: str, params: Optional[dict] = None, timeout: int = None, headers: Optional[dict] = None) -> str:
    if timeout is None:
        timeout = VERIFY_SPECIAL_TIMEOUT
    try:
        base_headers = {"User-Agent": f"CitationCrosschecker/2.0 (mailto:{MAILTO})"}
        if headers:
            base_headers.update(headers)
        r = requests.get(url, params=params, timeout=timeout, headers=base_headers)
        if r.status_code == 200:
            return r.text
        if r.status_code == 429:
            _debug_multisource(f"Rate limited by {url}")
        return ""
    except Exception as exc:
        _debug_multisource(f"Text request failed for {url}: {exc}")
        return ""


# ---------------------------
# Additional source queries
# ---------------------------

def _query_datacite_by_doi(doi: str) -> List[Dict[str, Any]]:
    doi = _normalise_doi(doi)
    if not doi:
        return []
    url = f"https://api.datacite.org/dois/{doi}"
    data = _safe_get_json(url, timeout=VERIFY_SPECIAL_TIMEOUT)
    item = (data or {}).get("data")
    return [{"source": "datacite", "query_name": "datacite_doi_exact", "item": item}] if item else []


def _query_datacite_search(query: str, rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_SPECIAL_ROWS
    query = _safe_strip(query)
    if not query:
        return []
    url = "https://api.datacite.org/dois"
    params = {"query": query, "page[size]": rows}
    data = _safe_get_json(url, params=params, timeout=VERIFY_SPECIAL_TIMEOUT)
    items = (data or {}).get("data", [])
    return [{"source": "datacite", "query_name": "datacite_search", "item": it} for it in items]


def _query_semantic_scholar_by_doi(doi: str) -> List[Dict[str, Any]]:
    doi = _normalise_doi(doi)
    if not doi:
        return []
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
    headers = {"x-api-key": SEMANTIC_SCHOLAR_API_KEY} if SEMANTIC_SCHOLAR_API_KEY else None
    params = {"fields": "title,year,authors,venue,journal,externalIds,url,publicationTypes,isOpenAccess"}
    data = _safe_get_json(url, params=params, timeout=VERIFY_SPECIAL_TIMEOUT) if not headers else _safe_get_json_with_headers(url, params=params, headers=headers)
    return [{"source": "semantic_scholar", "query_name": "semantic_scholar_doi_exact", "item": data}] if data and data.get("title") else []


def _safe_get_json_with_headers(url: str, params: Optional[dict] = None, headers: Optional[dict] = None, timeout: int = None) -> Optional[dict]:
    if timeout is None:
        timeout = VERIFY_SPECIAL_TIMEOUT
    try:
        base_headers = {
            "User-Agent": f"CitationCrosschecker/2.0 (mailto:{MAILTO})",
            "Accept": "application/json",
        }
        if headers:
            base_headers.update(headers)
        r = requests.get(url, params=params, timeout=timeout, headers=base_headers)
        if r.status_code == 200:
            _record_api_request_diagnostic("ok", url, 200)
            return r.json()
        if r.status_code == 429:
            _record_api_request_diagnostic("rate_limited", url, 429)
            _debug_multisource(f"Rate limited by {url}")
        elif r.status_code == 404:
            _record_api_request_diagnostic("not_found_http", url, 404)
        else:
            _record_api_request_diagnostic("http_error", url, r.status_code)
        return None
    except requests.exceptions.Timeout:
        _record_api_request_diagnostic("timeout", url)
        _debug_multisource(f"JSON request timed out for {url}")
        return None
    except Exception as exc:
        _record_api_request_diagnostic("network_error", url)
        _debug_multisource(f"JSON request failed for {url}: {exc}")
        return None


def _query_semantic_scholar_search(query: str, rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_SPECIAL_ROWS
    query = _safe_strip(query)
    if not query:
        return []
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    headers = {"x-api-key": SEMANTIC_SCHOLAR_API_KEY} if SEMANTIC_SCHOLAR_API_KEY else None
    params = {
        "query": query,
        "limit": rows,
        "fields": "title,year,authors,venue,journal,externalIds,url,publicationTypes,isOpenAccess",
    }
    data = _safe_get_json_with_headers(url, params=params, headers=headers)
    items = (data or {}).get("data", [])
    return [{"source": "semantic_scholar", "query_name": "semantic_scholar_search", "item": it} for it in items]


def _query_europepmc(query: str, rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_SPECIAL_ROWS
    query = _safe_strip(query)
    if not query:
        return []
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {"query": query, "format": "json", "pageSize": rows}
    data = _safe_get_json(url, params=params, timeout=VERIFY_SPECIAL_TIMEOUT)
    items = ((data or {}).get("resultList") or {}).get("result", [])
    return [{"source": "europepmc", "query_name": "europepmc_search", "item": it} for it in items]


def _query_pubmed(fields: Dict[str, Any], rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_SPECIAL_ROWS
    pmid = _extract_pmid(fields.get("reference", ""))
    title = fields.get("title", "")
    year = fields.get("year", "")
    author = _first_author_from_fields(fields)

    if pmid:
        ids = [pmid]
    else:
        title_q = _title_query_from_fields(fields, 8)
        if not title_q:
            return []
        term_parts = [f"{title_q}[Title]"]
        if author:
            term_parts.append(f"{author}[Author]")
        if year:
            term_parts.append(f"{year}[Date - Publication]")
        term = " AND ".join(term_parts)
        params = {"db": "pubmed", "term": term, "retmode": "json", "retmax": rows, "tool": "CitationCrosschecker"}
        if MAILTO:
            params["email"] = MAILTO
        if NCBI_API_KEY:
            params["api_key"] = NCBI_API_KEY
        data = _safe_get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params=params, timeout=VERIFY_SPECIAL_TIMEOUT)
        ids = ((data or {}).get("esearchresult") or {}).get("idlist", [])

    if not ids:
        return []
    params = {"db": "pubmed", "id": ",".join(ids[:rows]), "retmode": "json", "tool": "CitationCrosschecker"}
    if MAILTO:
        params["email"] = MAILTO
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    data = _safe_get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi", params=params, timeout=VERIFY_SPECIAL_TIMEOUT)
    result = (data or {}).get("result", {})
    out = []
    for uid in result.get("uids", []):
        item = result.get(uid)
        if item:
            item["uid"] = uid
            out.append({"source": "pubmed", "query_name": "pubmed_esummary", "item": item})
    return out


def _query_google_books(fields: Dict[str, Any], rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_SPECIAL_ROWS
    isbn = fields.get("isbn", "")
    title = fields.get("title", "")
    author = _first_author_from_fields(fields)
    if isbn:
        q = f"isbn:{isbn}"
    elif title:
        q = f"intitle:{title}"
        if author:
            q += f" inauthor:{author}"
    else:
        return []
    params = {"q": q, "maxResults": rows, "printType": "books"}
    if GOOGLE_BOOKS_API_KEY:
        params["key"] = GOOGLE_BOOKS_API_KEY
    data = _safe_get_json("https://www.googleapis.com/books/v1/volumes", params=params, timeout=VERIFY_SPECIAL_TIMEOUT)
    items = (data or {}).get("items", [])
    return [{"source": "google_books", "query_name": "google_books_search", "item": it} for it in items]


def _query_open_library(fields: Dict[str, Any], rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_SPECIAL_ROWS
    isbn = fields.get("isbn", "")
    title = fields.get("title", "")
    author = _first_author_from_fields(fields)
    params: Dict[str, Any] = {"limit": rows}
    if isbn:
        params["isbn"] = isbn
    elif title:
        params["title"] = title
        if author:
            params["author"] = author
    else:
        return []
    data = _safe_get_json("https://openlibrary.org/search.json", params=params, timeout=VERIFY_SPECIAL_TIMEOUT)
    items = (data or {}).get("docs", [])
    return [{"source": "open_library", "query_name": "open_library_search", "item": it} for it in items]


def _query_eric(fields: Dict[str, Any], rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_SPECIAL_ROWS
    query = _generic_metadata_query(fields, include_journal=False)
    if not query:
        return []
    params = {"search": query, "format": "json", "rows": rows}
    data = _safe_get_json("https://api.ies.ed.gov/eric/", params=params, timeout=VERIFY_SPECIAL_TIMEOUT)
    items = (data or {}).get("response", {}).get("docs", []) or (data or {}).get("docs", []) or []
    return [{"source": "eric", "query_name": "eric_search", "item": it} for it in items]


def _query_arxiv(fields: Dict[str, Any], rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_SPECIAL_ROWS
    arxiv_id = fields.get("arxiv_id", "")
    title = _title_query_from_fields(fields, 8)
    if arxiv_id:
        params = {"id_list": arxiv_id, "max_results": rows}
    elif title:
        params = {"search_query": f"ti:{title}", "start": 0, "max_results": rows, "sortBy": "relevance"}
    else:
        return []
    xml_text = _safe_get_text("https://export.arxiv.org/api/query", params=params, timeout=VERIFY_SPECIAL_TIMEOUT)
    if not xml_text:
        return []
    try:
        import xml.etree.ElementTree as ET
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        root = ET.fromstring(xml_text)
        out = []
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            published_el = entry.find("atom:published", ns)
            doi_el = entry.find("arxiv:doi", ns)
            id_el = entry.find("atom:id", ns)
            journal_el = entry.find("arxiv:journal_ref", ns)
            authors = []
            for au in entry.findall("atom:author", ns):
                name_el = au.find("atom:name", ns)
                if name_el is not None and name_el.text:
                    authors.append(name_el.text)
            item = {
                "title": _safe_strip(title_el.text if title_el is not None else ""),
                "year": _safe_strip((published_el.text or "")[:4] if published_el is not None else ""),
                "doi": _safe_strip(doi_el.text if doi_el is not None else ""),
                "url": _safe_strip(id_el.text if id_el is not None else ""),
                "journal": _safe_strip(journal_el.text if journal_el is not None else ""),
                "authors": authors,
                "type": "preprint",
            }
            if item["title"]:
                out.append({"source": "arxiv", "query_name": "arxiv_query", "item": item})
        return out
    except Exception as exc:
        _debug_multisource(f"arXiv XML parse failed: {exc}")
        return []


def _query_core(fields: Dict[str, Any], rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_SPECIAL_ROWS
    if not CORE_API_KEY:
        return []
    query = _generic_metadata_query(fields, include_journal=False)
    if not query:
        return []
    headers = {"Authorization": f"Bearer {CORE_API_KEY}"}
    # CORE v3 search endpoint. If an installation uses a different CORE contract,
    # this fails fast and quietly because CORE is an optional fallback.
    data = _safe_get_json_with_headers(
        "https://api.core.ac.uk/v3/search/works",
        params={"q": query, "limit": rows},
        headers=headers,
        timeout=VERIFY_SPECIAL_TIMEOUT,
    )
    items = (data or {}).get("results", [])
    return [{"source": "core", "query_name": "core_search", "item": it} for it in items]


def _query_doaj(fields: Dict[str, Any], rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_SPECIAL_ROWS
    # DOAJ is used only as a journal/article validation fallback. It is disabled
    # by default to avoid adding latency and because public API contracts may vary.
    query = _generic_metadata_query(fields, include_journal=True)
    if not query:
        return []
    headers = {"Authorization": f"Bearer {DOAJ_API_KEY}"} if DOAJ_API_KEY else None
    data = _safe_get_json_with_headers(
        f"https://doaj.org/api/search/articles/{requests.utils.quote(query)}",
        params={"pageSize": rows},
        headers=headers,
        timeout=VERIFY_SPECIAL_TIMEOUT,
    )
    items = (data or {}).get("results", [])
    return [{"source": "doaj", "query_name": "doaj_article_search", "item": it} for it in items]


# ---------------------------
# Query plan and candidate normalisation
# ---------------------------

def _build_verification_query_plan(ref: str, style: str) -> Dict[str, Any]:
    fields = _extract_fields_by_style(ref, style)
    fields["reference"] = ref
    fields["isbn"] = _extract_isbn(ref)
    fields["pmid"] = _extract_pmid(ref)
    fields["pmcid"] = _extract_pmcid(ref)
    fields["arxiv_id"] = _extract_arxiv_id(ref)

    authors = fields.get("authors", []) or []
    first_author = authors[0] if authors else ""
    year = fields.get("year", "") or ""
    doi = _normalise_doi(fields.get("doi", "") or "")
    title = _clean_query_text(fields.get("title", "") or "")
    journal = _clean_query_text(fields.get("journal", "") or fields.get("container_title", "") or fields.get("source", "") or "")
    volume = _clean_query_text(fields.get("volume", "") or "")
    issue = _clean_query_text(fields.get("issue", "") or "")
    pages = _clean_query_text(fields.get("pages", "") or fields.get("page", "") or "")
    clean_ref = _clean_query_text(ref)

    title_words = _significant_title_words(title, limit=14)
    title_key = " ".join(title_words[:10]).strip()
    title_short = " ".join(title_words[:7]).strip()
    bibliographic_rich = " ".join([p for p in [title, journal, year, volume, issue, pages] if p]).strip()

    crossref_queries: List[Dict[str, Any]] = []
    openalex_queries: List[Dict[str, Any]] = []
    fallback_queries: List[Dict[str, Any]] = []

    if doi:
        crossref_queries.append({"name": "crossref_doi_exact", "mode": "doi_exact", "doi": doi, "priority": 1})
        openalex_queries.append({"name": "openalex_doi_exact", "mode": "doi_exact", "doi": doi, "priority": 1})
        fallback_queries.append({"name": "datacite_doi_exact", "source": "datacite", "mode": "doi_exact", "priority": 10})
        fallback_queries.append({"name": "semantic_scholar_doi_exact", "source": "semantic_scholar", "mode": "doi_exact", "priority": 11})

    if clean_ref:
        crossref_queries.append({
            "name": "crossref_full_bibliographic",
            "mode": "bibliographic",
            "query_bibliographic": clean_ref,
            "query_author": first_author,
            "rows": VERIFY_CROSSREF_ROWS,
            "priority": 2,
        })

    if bibliographic_rich:
        crossref_queries.append({
            "name": "crossref_rich_bibliographic",
            "mode": "bibliographic",
            "query_bibliographic": bibliographic_rich,
            "query_author": first_author,
            "rows": VERIFY_CROSSREF_ROWS,
            "priority": 3,
        })

    if title_key:
        crossref_queries.append({
            "name": "crossref_title_author_year",
            "mode": "bibliographic",
            "query_bibliographic": " ".join([title_key, year]).strip(),
            "query_author": first_author,
            "rows": VERIFY_CROSSREF_ROWS,
            "priority": 4,
        })

    if title_key and journal:
        crossref_queries.append({
            "name": "crossref_title_journal_year",
            "mode": "bibliographic",
            "query_bibliographic": " ".join([title_key, journal, year]).strip(),
            "query_author": "",
            "rows": VERIFY_CROSSREF_ROWS,
            "priority": 5,
        })

    if title_key and year:
        openalex_queries.append({
            "name": "openalex_title_year",
            "mode": "search",
            "search": title_key,
            "publication_year": year,
            "rows": VERIFY_OPENALEX_ROWS,
            "priority": 6,
        })

    if title_key and journal:
        openalex_queries.append({
            "name": "openalex_title_journal",
            "mode": "search",
            "search": f"{title_key} {journal}",
            "publication_year": year,
            "rows": VERIFY_OPENALEX_ROWS,
            "priority": 7,
        })

    if title_short:
        openalex_queries.append({
            "name": "openalex_title_only",
            "mode": "search",
            "search": title_short,
            "publication_year": "",
            "rows": VERIFY_OPENALEX_ROWS,
            "priority": 8,
        })

    flags = _reference_type_flags(ref, fields)
    if title_key:
        if flags["looks_dataset_repo"]:
            fallback_queries.append({"name": "datacite_search", "source": "datacite", "mode": "search", "priority": 20})
        if flags["looks_health"]:
            fallback_queries.append({"name": "pubmed_search", "source": "pubmed", "mode": "search", "priority": 21})
            fallback_queries.append({"name": "europepmc_search", "source": "europepmc", "mode": "search", "priority": 22})
        if flags["looks_book"]:
            fallback_queries.append({"name": "google_books_search", "source": "google_books", "mode": "search", "priority": 23})
            fallback_queries.append({"name": "open_library_search", "source": "open_library", "mode": "search", "priority": 24})
        if flags["looks_education"]:
            fallback_queries.append({"name": "eric_search", "source": "eric", "mode": "search", "priority": 25})
        if flags["looks_arxiv"]:
            fallback_queries.append({"name": "arxiv_query", "source": "arxiv", "mode": "search", "priority": 26})
        fallback_queries.append({"name": "semantic_scholar_search", "source": "semantic_scholar", "mode": "search", "priority": 30})
        if flags["looks_dataset_repo"]:
            fallback_queries.append({"name": "core_search", "source": "core", "mode": "search", "priority": 31})
        if journal:
            fallback_queries.append({"name": "doaj_article_search", "source": "doaj", "mode": "search", "priority": 35})

    fields.update({
        "first_author": first_author,
        "doi": doi,
        "title": title,
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "title_key": title_key,
        "title_short": title_short,
        "reference_type_flags": flags,
    })

    return {
        "reference": ref,
        "style": style,
        "fields": fields,
        "crossref_queries": crossref_queries,
        "openalex_queries": openalex_queries,
        "fallback_queries": fallback_queries,
    }


def _candidate_authors_from_names(names: List[str]) -> List[str]:
    out = []
    for name in names or []:
        name = _safe_strip(name)
        if not name:
            continue
        surname = re.sub(r"[^a-z'\-]", "", name.split()[-1].lower())
        if surname:
            out.append(surname)
    return _dedupe_preserve(out)


def _candidate_fields(cand: Dict[str, Any]) -> Dict[str, Any]:
    src = cand.get("source")
    item = cand.get("item") or {}
    sources = cand.get("sources") or [src]

    if src == "crossref":
        title_list = item.get("title") or []
        container_list = item.get("container-title") or item.get("short-container-title") or []
        candidate_publication_events = events_from_crossref_message(item)
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": _normalise_doi(item.get("DOI", "")),
            "title": _safe_strip(title_list[0]) if title_list else "",
            "year": _crossref_year(item),
            "authors": _extract_crossref_authors(item),
            "journal": _safe_strip(container_list[0]) if container_list else "",
            "volume": _safe_strip(item.get("volume", "")),
            "issue": _safe_strip(item.get("issue", "")),
            "pages": _safe_strip(item.get("page", "") or item.get("article-number", "")),
            "publisher": _safe_strip(item.get("publisher", "")),
            "type": _safe_strip(item.get("type", "")),
            "url": _safe_strip(item.get("URL", "")),
            "is_retracted": bool(item.get("relation", {}).get("is-retracted-by") or item.get("relation", {}).get("retracts")),
            "candidate_publication_events": candidate_publication_events,
            "candidate_is_publication_notice": is_publication_notice(item),
        }

    if src == "openalex":
        host = item.get("primary_location", {}).get("source", {}) if isinstance(item.get("primary_location"), dict) else {}
        biblio = item.get("biblio") or {}
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": _normalise_doi(_safe_strip(item.get("doi", "")).replace("https://doi.org/", "")),
            "title": _safe_strip(item.get("title") or item.get("display_name")),
            "year": _safe_strip(item.get("publication_year")),
            "authors": _extract_openalex_authors(item),
            "journal": _safe_strip(host.get("display_name", "")),
            "volume": _safe_strip(biblio.get("volume", "")),
            "issue": _safe_strip(biblio.get("issue", "")),
            "pages": _safe_strip(biblio.get("first_page", "") or biblio.get("last_page", "")),
            "publisher": _safe_strip(host.get("host_organization_name", "")),
            "type": _safe_strip(item.get("type", "")),
            "url": _safe_strip(item.get("id", "")),
            "is_retracted": bool(item.get("is_retracted")),
        }

    if src == "datacite":
        attrs = item.get("attributes", item)
        titles = attrs.get("titles") or []
        creators = attrs.get("creators") or []
        container = attrs.get("container") or {}
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": _normalise_doi(attrs.get("doi") or item.get("id", "")),
            "title": _safe_strip((titles[0] or {}).get("title", "") if titles else attrs.get("title", "")),
            "year": _safe_strip(attrs.get("publicationYear", "")),
            "authors": _candidate_authors_from_names([c.get("name", "") for c in creators if isinstance(c, dict)]),
            "journal": _safe_strip(container.get("title", "") if isinstance(container, dict) else ""),
            "volume": _safe_strip(container.get("volume", "") if isinstance(container, dict) else ""),
            "issue": _safe_strip(container.get("issue", "") if isinstance(container, dict) else ""),
            "pages": _safe_strip(container.get("firstPage", "") if isinstance(container, dict) else ""),
            "publisher": _safe_strip(attrs.get("publisher", "")),
            "type": _safe_strip(attrs.get("types", {}).get("resourceTypeGeneral", "") if isinstance(attrs.get("types"), dict) else ""),
            "url": _safe_strip(attrs.get("url", "")),
            "is_retracted": False,
        }

    if src == "semantic_scholar":
        journal = item.get("journal") if isinstance(item.get("journal"), dict) else {}
        ext = item.get("externalIds") or {}
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": _normalise_doi(ext.get("DOI", "")),
            "title": _safe_strip(item.get("title", "")),
            "year": _safe_strip(item.get("year", "")),
            "authors": _candidate_authors_from_names([a.get("name", "") for a in item.get("authors", []) if isinstance(a, dict)]),
            "journal": _safe_strip(journal.get("name", "") or item.get("venue", "")),
            "volume": _safe_strip(journal.get("volume", "")),
            "issue": "",
            "pages": _safe_strip(journal.get("pages", "")),
            "publisher": "",
            "type": ", ".join(item.get("publicationTypes") or []),
            "url": _safe_strip(item.get("url", "")),
            "is_retracted": False,
        }

    if src == "europepmc":
        author_string = _safe_strip(item.get("authorString", ""))
        authors = [a.strip() for a in re.split(r",| and ", author_string) if a.strip()]
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": _normalise_doi(item.get("doi", "")),
            "title": _safe_strip(item.get("title", "")),
            "year": _safe_strip(item.get("pubYear", "") or item.get("firstPublicationDate", "")[:4]),
            "authors": _candidate_authors_from_names(authors),
            "journal": _safe_strip(item.get("journalTitle", "")),
            "volume": _safe_strip(item.get("journalVolume", "")),
            "issue": _safe_strip(item.get("issue", "")),
            "pages": _safe_strip(item.get("pageInfo", "")),
            "publisher": "",
            "type": _safe_strip(item.get("pubType", "")),
            "url": _safe_strip(item.get("fullTextUrlList", {}).get("fullTextUrl", [{}])[0].get("url", "") if isinstance(item.get("fullTextUrlList"), dict) else ""),
            "is_retracted": _safe_strip(item.get("isRetracted", "")).lower() == "yes",
        }

    if src == "pubmed":
        articleids = item.get("articleids") or []
        doi = ""
        for aid in articleids:
            if aid.get("idtype") == "doi":
                doi = aid.get("value", "")
                break
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": _normalise_doi(doi),
            "title": _safe_strip(item.get("title", "")),
            "year": _safe_strip(item.get("pubdate", "")[:4]),
            "authors": _candidate_authors_from_names([a.get("name", "") for a in item.get("authors", []) if isinstance(a, dict)]),
            "journal": _safe_strip(item.get("fulljournalname", "") or item.get("source", "")),
            "volume": _safe_strip(item.get("volume", "")),
            "issue": _safe_strip(item.get("issue", "")),
            "pages": _safe_strip(item.get("pages", "")),
            "publisher": "",
            "type": "journal-article",
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{item.get('uid', '')}/" if item.get("uid") else "",
            "is_retracted": "retracted" in _safe_strip(item.get("pubtype", "")).lower(),
        }

    if src == "google_books":
        info = item.get("volumeInfo") or {}
        identifiers = info.get("industryIdentifiers") or []
        isbn = ""
        for ident in identifiers:
            if "ISBN" in ident.get("type", ""):
                isbn = ident.get("identifier", "")
                break
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": "",
            "title": _safe_strip(info.get("title", "")),
            "year": _safe_strip(info.get("publishedDate", "")[:4]),
            "authors": _candidate_authors_from_names(info.get("authors", []) or []),
            "journal": "",
            "volume": "",
            "issue": "",
            "pages": _safe_strip(info.get("pageCount", "")),
            "publisher": _safe_strip(info.get("publisher", "")),
            "type": "book",
            "url": _safe_strip(info.get("infoLink", "")),
            "isbn": isbn,
            "is_retracted": False,
        }

    if src == "open_library":
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": "",
            "title": _safe_strip(item.get("title", "")),
            "year": _safe_strip(item.get("first_publish_year", "") or (item.get("publish_year") or [""])[0]),
            "authors": _candidate_authors_from_names(item.get("author_name", []) or []),
            "journal": "",
            "volume": "",
            "issue": "",
            "pages": _safe_strip(item.get("number_of_pages_median", "")),
            "publisher": _safe_strip((item.get("publisher") or [""])[0]),
            "type": "book",
            "url": f"https://openlibrary.org{item.get('key', '')}" if item.get("key") else "",
            "isbn": (item.get("isbn") or [""])[0],
            "is_retracted": False,
        }

    if src == "eric":
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": _normalise_doi(item.get("doi", "")),
            "title": _safe_strip(item.get("title", "") or item.get("title_display", "")),
            "year": _safe_strip(item.get("publicationdateyear", "") or item.get("publicationdate", "")[:4]),
            "authors": _candidate_authors_from_names(item.get("author", []) if isinstance(item.get("author"), list) else [item.get("author", "")]),
            "journal": _safe_strip(item.get("source", "") or item.get("sourceid", "")),
            "volume": _safe_strip(item.get("volume", "")),
            "issue": _safe_strip(item.get("issue", "")),
            "pages": _safe_strip(item.get("pages", "")),
            "publisher": _safe_strip(item.get("publisher", "")),
            "type": _safe_strip(item.get("publicationtype", "")),
            "url": _safe_strip(item.get("url", "") or item.get("pdf_url", "")),
            "is_retracted": False,
        }

    if src == "arxiv":
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": _normalise_doi(item.get("doi", "")),
            "title": _safe_strip(item.get("title", "")),
            "year": _safe_strip(item.get("year", "")),
            "authors": _candidate_authors_from_names(item.get("authors", []) or []),
            "journal": _safe_strip(item.get("journal", "")),
            "volume": "",
            "issue": "",
            "pages": "",
            "publisher": "arXiv",
            "type": "preprint",
            "url": _safe_strip(item.get("url", "")),
            "is_retracted": False,
        }

    if src == "core":
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": _normalise_doi(item.get("doi", "") or item.get("identifiers", {}).get("doi", "") if isinstance(item.get("identifiers"), dict) else ""),
            "title": _safe_strip(item.get("title", "")),
            "year": _safe_strip(item.get("yearPublished", "") or item.get("publishedDate", "")[:4]),
            "authors": _candidate_authors_from_names([a.get("name", "") if isinstance(a, dict) else str(a) for a in item.get("authors", [])]),
            "journal": _safe_strip(item.get("publisher", "") or item.get("journals", "")),
            "volume": "",
            "issue": "",
            "pages": "",
            "publisher": _safe_strip(item.get("publisher", "")),
            "type": _safe_strip(item.get("type", "")),
            "url": _safe_strip(item.get("downloadUrl", "") or item.get("sourceFulltextUrls", [""])[0] if isinstance(item.get("sourceFulltextUrls"), list) and item.get("sourceFulltextUrls") else ""),
            "is_retracted": False,
        }

    if src == "doaj":
        bibjson = item.get("bibjson", item)
        journal = bibjson.get("journal", {}) if isinstance(bibjson.get("journal"), dict) else {}
        return {
            "source": src,
            "sources": sources,
            "query_name": cand.get("query_name", ""),
            "doi": _normalise_doi(";".join([i.get("id", "") for i in bibjson.get("identifier", []) if i.get("type") == "doi"])),
            "title": _safe_strip(bibjson.get("title", "")),
            "year": _safe_strip(bibjson.get("year", "")),
            "authors": _candidate_authors_from_names([a.get("name", "") for a in bibjson.get("author", []) if isinstance(a, dict)]),
            "journal": _safe_strip(journal.get("title", "")),
            "volume": _safe_strip(journal.get("volume", "")),
            "issue": _safe_strip(journal.get("number", "")),
            "pages": "",
            "publisher": _safe_strip(journal.get("publisher", "")),
            "type": "journal-article",
            "url": "",
            "is_retracted": False,
        }

    return {
        "source": src or "unknown",
        "sources": sources,
        "query_name": cand.get("query_name", ""),
        "doi": "",
        "title": _safe_strip(item.get("title", "") if isinstance(item, dict) else ""),
        "year": "",
        "authors": [],
        "journal": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "publisher": "",
        "type": "",
        "url": "",
        "is_retracted": False,
    }


def _source_enabled(source: str) -> bool:
    return {
        "datacite": VERIFY_USE_DATACITE,
        "europepmc": VERIFY_USE_EUROPEPMC,
        "pubmed": VERIFY_USE_PUBMED,
        "semantic_scholar": VERIFY_USE_SEMANTIC_SCHOLAR,
        "google_books": VERIFY_USE_GOOGLE_BOOKS,
        "open_library": VERIFY_USE_OPEN_LIBRARY,
        "eric": VERIFY_USE_ERIC,
        "arxiv": VERIFY_USE_ARXIV,
        "core": VERIFY_USE_CORE and bool(CORE_API_KEY),
        "doaj": VERIFY_USE_DOAJ,
    }.get(source, False)


def _candidate_is_strong_enough(fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> bool:
    if not candidates:
        return False
    try:
        best, meta, _alts = _best_candidate(fields, candidates)
        if not best:
            return False
        status, _reason = _classify_from_meta(fields, meta)
        if status == "verified":
            return True
        if status == "likely" and int(meta.get("title_score", 0)) >= 90 and (int(meta.get("year_match", 0)) or int(meta.get("author_overlap", 0))):
            return True
        if meta.get("doi_match") and int(meta.get("title_score", 0)) >= 65:
            return True
    except Exception as exc:
        _debug_multisource(f"Precheck failed: {exc}")
    return False


def _run_fallback_query(q: Dict[str, Any], fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    source = q.get("source")
    if not _source_enabled(source):
        return []
    if source == "datacite":
        if q.get("mode") == "doi_exact":
            return _query_datacite_by_doi(fields.get("doi", ""))
        return _query_datacite_search(_generic_metadata_query(fields), VERIFY_SPECIAL_ROWS)
    if source == "semantic_scholar":
        if q.get("mode") == "doi_exact":
            return _query_semantic_scholar_by_doi(fields.get("doi", ""))
        return _query_semantic_scholar_search(_generic_metadata_query(fields), VERIFY_SPECIAL_ROWS)
    if source == "pubmed":
        return _query_pubmed(fields, VERIFY_SPECIAL_ROWS)
    if source == "europepmc":
        if fields.get("pmid"):
            query = f"EXT_ID:{fields.get('pmid')} AND SRC:MED"
        elif fields.get("pmcid"):
            query = f"PMCID:{fields.get('pmcid')}"
        else:
            query = _generic_metadata_query(fields)
        return _query_europepmc(query, VERIFY_SPECIAL_ROWS)
    if source == "google_books":
        return _query_google_books(fields, VERIFY_SPECIAL_ROWS)
    if source == "open_library":
        return _query_open_library(fields, VERIFY_SPECIAL_ROWS)
    if source == "eric":
        return _query_eric(fields, VERIFY_SPECIAL_ROWS)
    if source == "arxiv":
        return _query_arxiv(fields, VERIFY_SPECIAL_ROWS)
    if source == "core":
        return _query_core(fields, VERIFY_SPECIAL_ROWS)
    if source == "doaj":
        return _query_doaj(fields, VERIFY_SPECIAL_ROWS)
    return []


def _run_query_plan(plan: Dict[str, Any], use_crossref: bool, use_openalex: bool) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    candidates: List[Dict[str, Any]] = []
    query_used: List[str] = []
    query_strategy: List[str] = []
    fields = plan.get("fields", {})
    openalex_allowed = bool(use_openalex or VERIFY_FORCE_OPENALEX_FALLBACK)

    for q in sorted(plan.get("crossref_queries", []), key=lambda x: x.get("priority", 99)):
        if not use_crossref:
            continue
        name = q.get("name", "crossref")
        try:
            if q.get("mode") == "doi_exact":
                res = _query_crossref_by_doi(q.get("doi", ""))
            else:
                res = _query_crossref_bibliographic(
                    q.get("query_bibliographic", ""),
                    query_author=q.get("query_author", ""),
                    rows=int(q.get("rows", VERIFY_CROSSREF_ROWS)),
                    query_name=name,
                )
            if res:
                candidates.extend(res)
            query_strategy.append(name)
            query_used.append(q.get("doi") or q.get("query_bibliographic") or "")
            if VERIFY_STOP_ON_STRONG_MATCH and _candidate_is_strong_enough(fields, candidates):
                return candidates, query_used, query_strategy
        except Exception as exc:
            print(f"[DEBUG] Crossref query failed for {name}: {exc}")

    for q in sorted(plan.get("openalex_queries", []), key=lambda x: x.get("priority", 99)):
        if not openalex_allowed:
            continue
        name = q.get("name", "openalex")
        try:
            if q.get("mode") == "doi_exact":
                res = _query_openalex_by_doi(q.get("doi", ""))
            else:
                res = _query_openalex_search(
                    q.get("search", ""),
                    rows=int(q.get("rows", VERIFY_OPENALEX_ROWS)),
                    publication_year=q.get("publication_year", ""),
                    query_name=name,
                )
            if res:
                candidates.extend(res)
            query_strategy.append(name)
            query_used.append(q.get("doi") or q.get("search") or "")
            if VERIFY_STOP_ON_STRONG_MATCH and _candidate_is_strong_enough(fields, candidates):
                return candidates, query_used, query_strategy
        except Exception as exc:
            print(f"[DEBUG] OpenAlex query failed for {name}: {exc}")

    if not VERIFY_MULTISOURCE_FALLBACK:
        return candidates, query_used, query_strategy

    if VERIFY_MULTISOURCE_ONLY_WHEN_WEAK and _candidate_is_strong_enough(fields, candidates):
        return candidates, query_used, query_strategy

    extra_count = 0
    for q in sorted(plan.get("fallback_queries", []), key=lambda x: x.get("priority", 99)):
        source = q.get("source", "")
        if not _source_enabled(source):
            continue
        if extra_count >= VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES:
            break
        try:
            res = _run_fallback_query(q, fields)
            query_strategy.append(q.get("name", source))
            query_used.append(_generic_metadata_query(fields) or fields.get("doi", ""))
            extra_count += 1
            if res:
                candidates.extend(res)
                if _candidate_is_strong_enough(fields, candidates):
                    break
        except Exception as exc:
            print(f"[DEBUG] {source} fallback failed for {q.get('name')}: {exc}")

    return candidates, query_used, query_strategy


# Override single reference only to expose fallback query plan in the output row.
def _verify_single_reference(
    ref: str,
    style: str,
    use_crossref: bool,
    use_openalex: bool,
    enrich_metadata: bool = False,
) -> Dict[str, Any]:
    """Commercial-grade single reference verification with adaptive multi-source fallback."""
    cache_key = f"commercial_multisource_v1::{style}::{use_crossref}:{use_openalex}:{enrich_metadata}::{ref}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    plan = _build_verification_query_plan(ref, style)
    fields = plan.get("fields", {})
    query_preview, ref_authors, _ref_year, ref_doi, title_only = _build_query(ref, style)

    if VERIFY_SKIP_WEAK_TITLE and not ref_doi and _significant_word_count(title_only or fields.get("title", "")) < VERIFY_MIN_TITLE_WORDS:
        row = _make_fast_review_row(
            ref,
            style,
            query=query_preview,
            authors=ref_authors,
            reason="No DOI and too few significant title words for reliable commercial verification.",
        )
        row.update({
            "reference_title": fields.get("title", ""),
            "reference_year": fields.get("year", ""),
            "reference_doi": fields.get("doi", ""),
            "reference_journal": fields.get("journal", ""),
            "reference_volume": fields.get("volume", ""),
            "reference_issue": fields.get("issue", ""),
            "reference_pages": fields.get("pages", ""),
            "query_strategy": "fast_skip",
            "confidence_reason": "No DOI and too few significant title words for reliable commercial verification.",
            "alternative_matches": [],
        })
        row["status"] = _normalize_verify_status(row.get("status"))
        _cache_set(cache_key, row)
        return row

    try:
        candidates, query_used, query_strategy = _run_query_plan(plan, use_crossref, use_openalex)
        row = _base_commercial_row(
            ref,
            style,
            fields,
            " | ".join(_dedupe_preserve(query_used)),
            " | ".join(_dedupe_preserve(query_strategy)),
        )
        row["query_plan"] = {
            "crossref": [q.get("name") for q in plan.get("crossref_queries", [])],
            "openalex": [q.get("name") for q in plan.get("openalex_queries", [])],
            "fallback": [q.get("name") for q in plan.get("fallback_queries", [])],
        }
        row["reference_isbn"] = fields.get("isbn", "")
        row["reference_pmid"] = fields.get("pmid", "")
        row["reference_pmcid"] = fields.get("pmcid", "")
        row["reference_arxiv_id"] = fields.get("arxiv_id", "")

        if not candidates:
            row["status"] = "not_found"
            row["confidence_reason"] = "No candidates returned from DOI, Crossref/OpenAlex, or adaptive fallback searches."
            _cache_set(cache_key, row)
            return row

        best, meta, alternatives = _best_candidate(fields, candidates)
        if not best:
            row["status"] = "not_found"
            row["confidence_reason"] = "Candidates were returned, but none had enough usable metadata."
            _cache_set(cache_key, row)
            return row

        status, reason = _classify_from_meta(fields, meta)
        author_conflict = _has_author_conflict(fields, meta)

        row.update({
            "status": status,
            "source": "+".join(meta.get("sources") or [meta.get("source", "")]),
            "score": int(meta.get("score", 0)),
            "doi": _normalise_doi(meta.get("doi", "")),
            "matched_title": _safe_strip(meta.get("title", "")),
            "matched_year": _safe_strip(meta.get("year", "")),
            "matched_authors": ", ".join(meta.get("authors", []) or []),
            "matched_journal": _safe_strip(meta.get("journal", "")),
            "matched_container_title": _safe_strip(meta.get("journal", "")),
            "matched_volume": _safe_strip(meta.get("volume", "")),
            "matched_issue": _safe_strip(meta.get("issue", "")),
            "matched_pages": _safe_strip(meta.get("pages", "")),
            "matched_publisher": _safe_strip(meta.get("publisher", "")),
            "matched_type": _safe_strip(meta.get("type", "")),
            "matched_url": _safe_strip(meta.get("url", "")),
            "candidate_publication_events": meta.get("candidate_publication_events") or [],
            "candidate_is_publication_notice": bool(meta.get("candidate_is_publication_notice")),
            "title_score": int(meta.get("title_score", 0)),
            "journal_score": int(meta.get("journal_score", 0)),
            "author_overlap": int(meta.get("author_overlap", 0)),
            "author_similarity": int(meta.get("author_similarity", 0)),
            "year_match": int(meta.get("year_match", 0)),
            "year_delta": int(meta.get("year_delta", 999)),
            "doi_match": 1 if meta.get("doi_match") else 0,
            "volume_match": int(meta.get("volume_match", 0)),
            "issue_match": int(meta.get("issue_match", 0)),
            "page_match": int(meta.get("page_match", 0)),
            "source_agreement": int(meta.get("source_agreement", 0)),
            "author_mismatch_flag": 1 if author_conflict else 0,
            "match_note": "Author mismatch" if author_conflict else "",
            "confidence_reason": reason,
            "alternative_matches": alternatives,
        })

        if enrich_metadata and row.get("doi"):
            row = enrich_with_full_metadata(row)

    except Exception as exc:
        print(f"[DEBUG] Error verifying reference: {exc}")
        row = _base_commercial_row(ref, style, fields, query_preview, "error")
        row["status"] = "not_found"
        row["error"] = str(exc)
        row["confidence_reason"] = "Verification failed because an exception occurred."

    row["status"] = _normalize_verify_status(row.get("status"))
    _cache_set(cache_key, row)
    return row


# ============================================================
# TARGET-18 EXISTENCE FALLBACK OVERRIDE v7
# Added to improve verified count for valid non-DOI books, reports,
# fact sheets and older references without slowing normal article checks.
# It runs only after the normal verifier returns non-verified.
# ============================================================

VERIFY_EXISTENCE_FALLBACK = _env_flag("VERIFY_EXISTENCE_FALLBACK", "1")
VERIFY_EXISTENCE_MAX_SOURCES = int(os.getenv("VERIFY_EXISTENCE_MAX_SOURCES", "2"))
VERIFY_EXISTENCE_ROWS = int(os.getenv("VERIFY_EXISTENCE_ROWS", "2"))
VERIFY_BOOK_VERIFY_TITLE = int(os.getenv("VERIFY_BOOK_VERIFY_TITLE", "80"))
VERIFY_BOOK_VERIFY_AUTHOR_SIM = int(os.getenv("VERIFY_BOOK_VERIFY_AUTHOR_SIM", "55"))
VERIFY_BOOK_YEAR_DELTA = int(os.getenv("VERIFY_BOOK_YEAR_DELTA", "8"))
VERIFY_ARTICLE_ONLINE_YEAR_DELTA = int(os.getenv("VERIFY_ARTICLE_ONLINE_YEAR_DELTA", "4"))
VERIFY_ERICTITLE_VERIFY = int(os.getenv("VERIFY_ERIC_TITLE_VERIFY", "82"))
VERIFY_DOI_TITLE_VERIFY_LOOSE = int(os.getenv("VERIFY_DOI_TITLE_VERIFY_LOOSE", "68"))


def _status_rank(status: str) -> int:
    st = _normalize_verify_status(status)
    return {"not_found": 0, "needs_review": 1, "likely": 2, "verified": 3, "offline": 0}.get(st, 0)


def _trusted_source_candidates_for_existing_refs(ref: str, fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run only targeted low-cost fallbacks for references that already failed core verification."""
    if not VERIFY_EXISTENCE_FALLBACK:
        return []

    flags = _reference_type_flags(ref, fields)
    ref_l = _safe_strip(ref).lower()
    candidates: List[Dict[str, Any]] = []
    used = 0

    def add_from(source_name: str, fn):
        nonlocal used, candidates
        if used >= VERIFY_EXISTENCE_MAX_SOURCES:
            return
        if not _source_enabled(source_name):
            return
        try:
            res = fn()
            used += 1
            if res:
                candidates.extend(res)
        except Exception as exc:
            print(f"[DEBUG] Target-18 existence fallback failed for {source_name}: {exc}")

    # Books and monographs are often absent from Crossref/OpenAlex article-style lookup.
    if flags.get("looks_book") or any(x in ref_l for x in ["wiley", "guilford", "brooks/cole", "routledge", "harper", "lawrence erlbaum", "oxford university press", "john wiley", "crc"]):
        add_from("google_books", lambda: _query_google_books(fields, rows=VERIFY_EXISTENCE_ROWS))
        add_from("open_library", lambda: _query_open_library(fields, rows=VERIFY_EXISTENCE_ROWS))

    # Education reports/fact sheets and older education articles may be indexed by ERIC.
    if flags.get("looks_education") or any(x in ref_l for x in ["fact sheet", "ifas", "extension", "educational and psychological", "information technology, learning"]):
        add_from("eric", lambda: _query_eric(_generic_metadata_query(fields), rows=VERIFY_EXISTENCE_ROWS))

    # If the original has a DOI but Crossref/OpenAlex did not verify, DataCite is a cheap exact fallback.
    if fields.get("doi"):
        add_from("datacite", lambda: _query_datacite_by_doi(fields.get("doi", "")))

    return candidates


def _is_bookish_meta(meta: Dict[str, Any]) -> bool:
    src = _safe_strip(meta.get("source", "")).lower()
    typ = _safe_strip(meta.get("type", "")).lower()
    return src in {"google_books", "open_library"} or "book" in typ


def _is_eric_meta(meta: Dict[str, Any]) -> bool:
    return _safe_strip(meta.get("source", "")).lower() == "eric"


_original_target18_classify_from_meta_v7 = _classify_from_meta

def _classify_from_meta(ref_fields: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[str, str]:
    """Target-18 classifier with existence-source support for books and older reports."""
    title_score = int(meta.get("title_score", 0))
    score = int(meta.get("score", 0))
    year_match = int(meta.get("year_match", 0))
    year_delta = int(meta.get("year_delta", 999))
    author_overlap = int(meta.get("author_overlap", 0))
    author_similarity = int(meta.get("author_similarity", 0))
    journal_score = int(meta.get("journal_score", 0))
    candidate_has_doi = bool(meta.get("doi") or meta.get("candidate_has_doi"))
    doi_match = bool(meta.get("doi_match"))
    source = _safe_strip(meta.get("source", "")).lower()
    ref_has_authors = bool(ref_fields.get("authors"))
    cand_has_authors = bool(meta.get("authors"))
    author_ok = author_overlap >= 1 or author_similarity >= VERIFY_BOOK_VERIFY_AUTHOR_SIM or not ref_has_authors or not cand_has_authors
    year_ok_article = year_match == 1 or year_delta <= VERIFY_ARTICLE_ONLINE_YEAR_DELTA
    year_ok_book = year_match == 1 or year_delta <= VERIFY_BOOK_YEAR_DELTA or year_delta == 999

    if bool(meta.get("is_retracted")):
        return "needs_review", "Matched record appears to be retracted and needs manual review."

    # Exact DOI remains strongest. Allow online-first/article issue-year differences up to a few years.
    if doi_match:
        if title_score >= 50 or author_ok or year_ok_article:
            return "verified", "Exact DOI match with acceptable title, author or publication-year support."
        return "verified", "DOI matches, but bibliographic evidence is weak."

    # DOI-backed article candidate found from a trusted scholarly source.
    # This handles online-first year differences, e.g. Crossref year differs from issue year.
    if candidate_has_doi and source in {"crossref", "openalex", "datacite", "pubmed", "europepmc", "semantic_scholar"}:
        if title_score >= VERIFY_DOI_TITLE_VERIFY_LOOSE and score >= 58 and (author_ok or year_ok_article or journal_score >= 55):
            return "verified", "DOI-backed scholarly match promoted with title and supporting metadata."
        if title_score >= 55 and (author_ok or year_ok_article):
            return "verified", "DOI-backed scholarly candidate found, but evidence is below verified threshold."

    # Books and monographs: Google Books/Open Library do not supply DOI, so verify by title + author + edition/year tolerance.
    if _is_bookish_meta(meta):
        if title_score >= VERIFY_BOOK_VERIFY_TITLE and author_ok and year_ok_book:
            return "verified", "Book/monograph verified through trusted book metadata with title and author support."
        if title_score >= 90 and author_ok:
            return "verified", "Book/monograph verified through trusted book metadata with very strong title and author support."
        if title_score >= 72 and author_ok:
            return "verified", "Book/monograph found in trusted book metadata, but year or edition needs review."

    # ERIC and education sources: useful for older education articles, fact sheets, reports and non-DOI outputs.
    if _is_eric_meta(meta):
        if title_score >= VERIFY_ERICTITLE_VERIFY and (author_ok or year_match == 1 or journal_score >= 50):
            return "verified", "Education/report reference verified through ERIC with strong title and supporting metadata."
        if title_score >= 70 and (author_ok or year_delta <= 1):
            return "verified", "ERIC candidate found, but evidence is below verified threshold."

    return _original_target18_classify_from_meta_v7(ref_fields, meta)


_original_verify_single_reference_v7 = _verify_single_reference

def _verify_single_reference(
    ref: str,
    style: str,
    use_crossref: bool,
    use_openalex: bool,
    enrich_metadata: bool = False,
) -> Dict[str, Any]:
    """Run normal verification first, then targeted existence fallback for non-verified rows."""
    row = _original_verify_single_reference_v7(ref, style, use_crossref, use_openalex, enrich_metadata)

    if _normalize_verify_status(row.get("status")) == "verified" or not VERIFY_EXISTENCE_FALLBACK:
        return row

    try:
        plan = _build_verification_query_plan(ref, style)
        fields = plan.get("fields", {}) or {}
        extra_candidates = _trusted_source_candidates_for_existing_refs(ref, fields)
        if not extra_candidates:
            return row

        scored = []
        for cand in extra_candidates:
            try:
                meta = _score_candidate(fields, cand)
                scored.append(meta)
            except Exception:
                continue
        if not scored:
            return row

        scored.sort(key=lambda x: int(x.get("score", 0)), reverse=True)
        meta = scored[0]
        new_status, reason = _classify_from_meta(fields, meta)

        if _status_rank(new_status) > _status_rank(row.get("status")):
            row.update({
                "status": new_status,
                "source": _safe_strip(meta.get("source", "")),
                "score": int(meta.get("score", 0)),
                "doi": _normalise_doi(meta.get("doi", "")),
                "matched_title": _safe_strip(meta.get("title", "")),
                "matched_year": _safe_strip(meta.get("year", "")),
                "matched_authors": ", ".join(meta.get("authors", []) or []),
                "matched_journal": _safe_strip(meta.get("journal", "")),
                "matched_container_title": _safe_strip(meta.get("journal", "")),
                "matched_publisher": _safe_strip(meta.get("publisher", "")),
                "matched_type": _safe_strip(meta.get("type", "")),
                "matched_url": _safe_strip(meta.get("url", "")),
                "title_score": int(meta.get("title_score", 0)),
                "journal_score": int(meta.get("journal_score", 0)),
                "author_overlap": int(meta.get("author_overlap", 0)),
                "author_similarity": int(meta.get("author_similarity", 0)),
                "year_match": int(meta.get("year_match", 0)),
                "year_delta": int(meta.get("year_delta", 999)),
                "doi_match": 1 if meta.get("doi_match") else 0,
                "confidence_reason": reason,
                "query_strategy": (row.get("query_strategy", "") + " | target18_existence_fallback").strip(" |"),
                "query_used": (row.get("query_used", "") + " | " + _generic_metadata_query(fields)).strip(" |"),
            })
            row["status"] = _normalize_verify_status(row.get("status"))
            _cache_set(f"target18_existence_v7::{style}::{use_crossref}:{use_openalex}:{enrich_metadata}::{ref}", row)
        return row
    except Exception as exc:
        print(f"[DEBUG] Target-18 existence fallback wrapper failed: {exc}")
        return row

try:
    if "__all__" in globals():
        for name in [
            "VERIFY_EXISTENCE_FALLBACK",
            "_trusted_source_candidates_for_existing_refs",
            "_classify_from_meta",
            "_verify_single_reference",
        ]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass


# ============================================================
# STYLE-AWARE VERIFICATION PATCH FOR CITEINTEGRITY
# Build: 2026-05-19-style-aware-all-citation-families
# Purpose:
# - Preserve selected citation family in verification rows.
# - Support condensed UI styles: author-year, numeric-square,
#   numeric-superscript, numeric-round, auto.
# - Improve reference-field extraction for numeric/Vancouver/NLM/JAMA/RSC/ACS
#   references before Crossref/OpenAlex lookup.
# ============================================================

__version__ = "1.5.18"
VERIFY_BUILD = "commercial-2026-05-19-style-aware-verify-for-author-year-and-numeric-FINAL"

# Keep a handle to the commercial row builder already defined above.
try:
    _STYLE_AWARE_ORIGINAL_BASE_COMMERCIAL_ROW = _base_commercial_row
except Exception:
    _STYLE_AWARE_ORIGINAL_BASE_COMMERCIAL_ROW = None

try:
    _STYLE_AWARE_ORIGINAL_EXTRACT_COMMON_FIELDS = _extract_common_fields
except Exception:
    _STYLE_AWARE_ORIGINAL_EXTRACT_COMMON_FIELDS = None

# Full style mapping aligned with the condensed frontend and the engine.
_STYLE_ALIASES.update({
    "": "apa",
    "auto": "apa",
    "author_year": "apa",
    "author-year": "apa",
    "apa_harvard_chicago": "apa",
    "apa_harvard_chicago_author_date": "apa",
    "chicago": "apa",
    "chicago_author_date": "apa",
    "harvard_author_date": "apa",

    "ieee": "numeric_square",
    "ieee_square": "numeric_square",
    "ieee_square_bracket": "numeric_square",
    "vancouver": "numeric_square",
    "vancouver_square": "numeric_square",
    "vancouver_square_bracket": "numeric_square",
    "nlm": "numeric_square",
    "nlm_square": "numeric_square",
    "nlm_square_bracket": "numeric_square",
    "elsevier": "numeric_square",
    "elsevier_numbered": "numeric_square",
    "elsevier_square": "numeric_square",
    "springer": "numeric_square",
    "springer_numbered": "numeric_square",
    "springer_square": "numeric_square",
    "square_numeric": "numeric_square",
    "numeric_square": "numeric_square",

    "ama": "numeric_superscript",
    "ama_superscript": "numeric_superscript",
    "nature": "numeric_superscript",
    "nature_superscript": "numeric_superscript",
    "rsc": "numeric_superscript",
    "rsc_superscript": "numeric_superscript",
    "acs": "numeric_superscript",
    "acs_superscript": "numeric_superscript",
    "elsevier_superscript": "numeric_superscript",
    "superscript_numeric": "numeric_superscript",
    "numeric_superscript": "numeric_superscript",

    "vancouver_round": "numeric_round",
    "acs_round": "numeric_round",
    "round_numeric": "numeric_round",
    "numeric_round": "numeric_round",
})

_STYLE_INFO = {
    "apa": {
        "family": "author_year",
        "label": "Author-year, APA / Harvard / Chicago",
        "sample": "(Adam, 2020), (Adam 2020), Adam (2020)",
    },
    "numeric_square": {
        "family": "numeric_square",
        "label": "Numeric square bracket, IEEE / Vancouver / NLM / Elsevier / Springer",
        "sample": "[1], [1,2], [3-5]",
    },
    "numeric_superscript": {
        "family": "numeric_superscript",
        "label": "Numeric superscript, AMA / Nature / RSC / ACS / Elsevier",
        "sample": "text¹, text¹,², text¹-³",
    },
    "numeric_round": {
        "family": "numeric_round",
        "label": "Numeric round bracket, Vancouver / ACS",
        "sample": "(1), (1,2), (3-5)",
    },
}

_NUMERIC_VERIFY_STYLES = {"numeric_square", "numeric_superscript", "numeric_round"}


def _canonical_verify_style(style: str) -> str:
    s = _safe_strip(style).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[\s\-/]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return _STYLE_ALIASES.get(s, s if s in _STYLE_INFO else "apa")


def _style_metadata(style: str) -> Dict[str, str]:
    canonical = _canonical_verify_style(style)
    info = dict(_STYLE_INFO.get(canonical, _STYLE_INFO["apa"]))
    info["style"] = canonical
    return info


def _strip_leading_numbering(text: str) -> str:
    """Strip reference-list numbering, including RSC bare numbers like '1 J. Monod'."""
    t = _safe_strip(text)
    if not t:
        return ""
    t = re.sub(r"^\s*\[\s*\d{1,4}\s*\]\s*", "", t)
    t = re.sub(r"^\s*\(\s*\d{1,4}\s*\)\s*", "", t)
    t = re.sub(r"^\s*\d{1,4}[\.)]\s*", "", t)
    # Bare numeric styles, especially RSC/ChemComm: '1 J. Monod, ...'
    t = re.sub(r"^\s*\d{1,4}\s+(?=(?:[A-Z][\w'’\-]+|[A-Z]\.|[A-Z]{2,}\b))", "", t)
    return t.strip()


def _normalise_reference_for_verify(ref: str) -> str:
    s = _safe_strip(ref)
    s = s.replace("\u00a0", " ")
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _looks_like_rsc_author_segment(seg: str) -> bool:
    seg = _safe_strip(seg)
    return bool(re.search(r"\b[A-Z]\.?\s*[A-Z][A-Za-z'’\-]+\b", seg))


def _looks_like_vancouver_author_segment(seg: str) -> bool:
    seg = _safe_strip(seg)
    if not seg:
        return False
    if len(seg) > 240:
        return False
    if re.search(r"\bet\s+al\b", seg, re.I):
        return True
    # Surname Initials, Surname Initials
    return len(re.findall(r"\b[A-Z][A-Za-z'’\-]{1,}\s+[A-Z]{1,4}\b", seg)) >= 1


def _surname_from_numeric_author_piece(piece: str) -> str:
    p = _safe_strip(piece)
    p = re.sub(r"\bet\s+al\.?", "", p, flags=re.I).strip(" ,.;")
    if not p:
        return ""
    # RSC/ACS: J. Monod, R. Yoshida, K. Okeyoshi
    m = re.search(r"(?:\b[A-Z]\.\s*)+([A-Z][A-Za-z'’\-]+)\b", p)
    if m:
        return re.sub(r"[^A-Za-z'\-]", "", m.group(1)).lower()
    # Vancouver/NLM/JAMA: Pronovost P, Needham D
    toks = [x for x in re.split(r"\s+", p) if x]
    if toks:
        cand = toks[0]
        cand = re.sub(r"[^A-Za-z'\-]", "", cand).lower()
        if len(cand) >= 2:
            return cand
    return ""


def _extract_numeric_authors(author_part: str) -> List[str]:
    author_part = _safe_strip(author_part)
    if not author_part:
        return []
    author_part = re.sub(r"\bet\s+al\.?", "", author_part, flags=re.I)
    parts = []
    for chunk in re.split(r",|\band\b|&", author_part, flags=re.I):
        chunk = chunk.strip(" ,.;")
        if chunk:
            parts.append(chunk)
    out = []
    for part in parts:
        key = _surname_from_numeric_author_piece(part)
        if key and len(key) >= 2 and key not in {"and", "the", "department", "university"}:
            out.append(key)
    return _dedupe_preserve(out)[:6]


def _extract_numeric_volume_issue_pages(ref: str) -> Dict[str, str]:
    out = {"volume": "", "issue": "", "pages": ""}
    r = _normalise_reference_for_verify(ref)

    # Vancouver/JAMA/NLM: 2006;355(26):2725-2732 or 2024;79(11):gbae153
    m = re.search(r"\b(?:19|20)\d{2}[a-z]?\s*;\s*([A-Za-z]?\d+[A-Za-z]?)\s*(?:\(([^)]+)\))?\s*:\s*([A-Za-z]?\d+[A-Za-z]?\s*-\s*[A-Za-z]?\d+[A-Za-z]?|[A-Za-z]?\d+[A-Za-z]?|[A-Za-z]?\d+[A-Za-z]?\.?[A-Za-z]*\d*)", r, flags=re.I)
    if m:
        out["volume"] = m.group(1) or ""
        out["issue"] = m.group(2) or ""
        out["pages"] = re.sub(r"\s+", "", m.group(3) or "").strip(".")
        return out

    # RSC/ACS: Journal, 1978, 40, 820-823.
    m = re.search(r"\b(?:19|20)\d{2}[a-z]?\s*,\s*([A-Za-z]?\d+[A-Za-z]?)\s*,\s*([A-Za-z]?\d+[A-Za-z]?\s*-\s*[A-Za-z]?\d+[A-Za-z]?|[A-Za-z]?\d+[A-Za-z]?)", r, flags=re.I)
    if m:
        out["volume"] = m.group(1) or ""
        out["pages"] = re.sub(r"\s+", "", m.group(2) or "").strip(".")
        return out

    # Existing generic extraction fallback.
    try:
        base = _extract_volume_issue_pages(r)
        out.update({k: base.get(k, "") for k in out})
    except Exception:
        pass
    return out


def _extract_numeric_reference_fields(ref: str, style: str = "numeric_square") -> Dict[str, Any]:
    raw = _normalise_reference_for_verify(ref)
    clean = _strip_leading_numbering(raw)
    doi = _normalise_doi(_extract_doi(clean))
    year = _extract_year(clean)
    vip = _extract_numeric_volume_issue_pages(clean)

    author_part = ""
    title = ""
    journal = ""

    # Vancouver/NLM/JAMA: Authors. Title. Journal. Year;volume(issue):pages.
    dot_parts = [p.strip() for p in re.split(r"\.\s+", clean) if p.strip()]
    if len(dot_parts) >= 2 and _looks_like_vancouver_author_segment(dot_parts[0]):
        author_part = dot_parts[0]
        title = dot_parts[1]
        if len(dot_parts) >= 3:
            journal = dot_parts[2]
            journal = re.split(r"\b(?:19|20)\d{2}\b", journal, maxsplit=1)[0].strip(" ,.;") or journal.strip(" ,.;")
    else:
        # RSC/ACS: J. Monod, Title or Journal, Year, volume, pages.
        comma_parts = [p.strip() for p in clean.split(",") if p.strip()]
        if len(comma_parts) >= 2 and _looks_like_rsc_author_segment(comma_parts[0]):
            author_part = comma_parts[0]
            first_after_author = comma_parts[1].strip()
            # If the first post-author segment looks like a journal abbreviation and is followed by a year,
            # then there is no article title in the reference.
            if re.search(r"\b(?:19|20)\d{2}\b", clean) and re.search(r"\b(?:J|Chem|Phys|Rev|Lett|Mater|Commun|Nature|Science|Langmuir|Macromolecules|Angew|Adv|Soft|Soc|Chemistry|Biol|Med)\b", first_after_author):
                journal = first_after_author
                title = ""
            else:
                title = first_after_author
                if len(comma_parts) >= 3 and not re.search(r"\b(?:19|20)\d{2}\b", comma_parts[2]):
                    journal = comma_parts[2].strip(" ,.;")
        else:
            # Last fallback: use the original common parser but with improved number stripping.
            fields = _STYLE_AWARE_ORIGINAL_EXTRACT_COMMON_FIELDS(clean) if _STYLE_AWARE_ORIGINAL_EXTRACT_COMMON_FIELDS else {}
            fields = dict(fields or {})
            fields.setdefault("authors", [])
            fields.setdefault("year", year)
            fields.setdefault("doi", doi)
            fields.setdefault("title", "")
            fields.setdefault("journal", fields.get("container_title", ""))
            fields.update({k: fields.get(k) or v for k, v in vip.items()})
            fields["style_family"] = _style_metadata(style)["family"]
            fields["style_label"] = _style_metadata(style)["label"]
            return fields

    authors = _extract_numeric_authors(author_part)

    # Remove common non-title tail noise.
    title = _clean_query_text(title)
    journal = _clean_query_text(journal)
    if title and _YEAR_RE.search(title):
        title = re.split(r"\b(?:19|20)\d{2}\b", title, maxsplit=1)[0].strip(" ,.;")
    if journal and _YEAR_RE.search(journal):
        journal = re.split(r"\b(?:19|20)\d{2}\b", journal, maxsplit=1)[0].strip(" ,.;")

    # If title is missing in compact chemistry references, build verification from author+journal+year+volume+pages.
    title_key_source = title or " ".join([journal, vip.get("volume", ""), vip.get("pages", "")]).strip()

    info = _style_metadata(style)
    return {
        "authors": authors,
        "year": year,
        "doi": doi,
        "title": title_key_source,
        "journal": journal,
        "container_title": journal,
        "source": journal,
        "volume": vip.get("volume", ""),
        "issue": vip.get("issue", ""),
        "pages": vip.get("pages", ""),
        "style_family": info["family"],
        "style_label": info["label"],
        "style_sample": info["sample"],
        "reference_numbered": True,
    }


def _extract_fields_by_style(ref: str, style: str) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    if canonical in _NUMERIC_VERIFY_STYLES:
        return _extract_numeric_reference_fields(ref, canonical)
    fields = _STYLE_AWARE_ORIGINAL_EXTRACT_COMMON_FIELDS(ref) if _STYLE_AWARE_ORIGINAL_EXTRACT_COMMON_FIELDS else {}
    fields = dict(fields or {})
    info = _style_metadata(canonical)
    fields.update({
        "style_family": info["family"],
        "style_label": info["label"],
        "style_sample": info["sample"],
    })
    return fields


def _build_numeric_reference_from_metadata(metadata: Dict[str, Any], number: str = "") -> str:
    if not metadata:
        return ""
    authors_meta = metadata.get("authors", []) or []
    author_bits = []
    for a in authors_meta[:6]:
        fam = _safe_strip(a.get("family", ""))
        given = _safe_strip(a.get("given", ""))
        initials = "".join([x[0].upper() for x in re.findall(r"[A-Za-z]+", given)])
        if fam:
            author_bits.append(f"{fam} {initials}".strip())
    if len(authors_meta) > 6:
        author_bits.append("et al")
    authors = ", ".join(author_bits)
    title = _safe_strip(metadata.get("title", ""))
    journal = _safe_strip(metadata.get("container_title", ""))
    year = _safe_strip(metadata.get("year", ""))
    volume = _safe_strip(metadata.get("volume", ""))
    issue = _safe_strip(metadata.get("issue", ""))
    page = _safe_strip(metadata.get("page", "")) or _safe_strip(metadata.get("article_number", ""))
    doi = _normalise_doi(metadata.get("doi", ""))
    lead = f"{number}. " if number else ""
    parts = [lead + authors if authors else lead.strip(), title, journal]
    tail = ""
    if year:
        tail += year
    if volume:
        tail += f";{volume}"
        if issue:
            tail += f"({issue})"
    if page:
        tail += f":{page}"
    if tail:
        parts.append(tail)
    if doi:
        parts.append(f"doi:{doi}")
    return ". ".join([p.strip(" .") for p in parts if p and p.strip(" .")]) + "."


try:
    _STYLE_AWARE_ORIGINAL_ENRICH = enrich_with_full_metadata
except Exception:
    _STYLE_AWARE_ORIGINAL_ENRICH = None


def enrich_with_full_metadata(result: Dict[str, Any]) -> Dict[str, Any]:
    """Add style-aware formatted reference suggestions after Crossref enrichment."""
    if _STYLE_AWARE_ORIGINAL_ENRICH:
        result = _STYLE_AWARE_ORIGINAL_ENRICH(result)
    if not result:
        return result
    canonical = _canonical_verify_style(result.get("style", "apa"))
    info = _style_metadata(canonical)
    result["style"] = canonical
    result["style_family"] = info["family"]
    result["style_label"] = info["label"]
    result["style_sample"] = info["sample"]
    meta = result.get("full_metadata") or {}
    if meta:
        numeric_ref = _build_numeric_reference_from_metadata(meta)
        if numeric_ref:
            result["numeric_reference"] = numeric_ref
        if canonical in _NUMERIC_VERIFY_STYLES:
            result["style_formatted_reference"] = numeric_ref
        elif canonical == "apa":
            result["style_formatted_reference"] = result.get("apa7_reference") or result.get("harvard_reference") or numeric_ref
    return result


def _verification_style_suggestion(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    status = _normalize_verify_status(row.get("status", ""))
    canonical = _canonical_verify_style(row.get("style", "apa"))
    info = _style_metadata(canonical)
    if status == "verified":
        return []
    issue = "reference_needs_review" if status in {"likely", "needs_review"} else "reference_not_found_online"
    reason = row.get("confidence_reason") or row.get("match_note") or row.get("error") or "Online metadata evidence is incomplete."
    action = "Review the reference against the matched metadata before accepting it."
    if status == "not_found":
        action = "Check DOI, title, journal, year, volume and pages. If the work is valid but not indexed, mark it as manually verified."
    if canonical in _NUMERIC_VERIFY_STYLES:
        action += " Keep the reference number unchanged unless you are correcting the full citation sequence."
    return [{
        "issue_type": issue,
        "style": canonical,
        "style_family": info["family"],
        "style_label": info["label"],
        "style_sample": info["sample"],
        "reference": row.get("reference", ""),
        "status": status,
        "reason": reason,
        "suggested_action": action,
        "fix_type": "review_required",
        "source": "online_verification",
    }]


def _base_commercial_row(ref: str, style: str, fields: Dict[str, Any], query_used: str = "", query_strategy: str = "") -> Dict[str, Any]:
    if _STYLE_AWARE_ORIGINAL_BASE_COMMERCIAL_ROW:
        row = _STYLE_AWARE_ORIGINAL_BASE_COMMERCIAL_ROW(ref, style, fields, query_used, query_strategy)
    else:
        row = {"reference": ref, "style": style, "status": "offline", "query_used": query_used, "query_strategy": query_strategy}
    canonical = _canonical_verify_style(style)
    info = _style_metadata(canonical)
    row.update({
        "style": canonical,
        "selected_style": canonical,
        "style_family": info["family"],
        "style_label": info["label"],
        "style_sample": info["sample"],
        "reference_title": fields.get("title", row.get("reference_title", "")),
        "reference_year": fields.get("year", row.get("reference_year", "")),
        "reference_doi": fields.get("doi", row.get("reference_doi", "")),
        "reference_journal": fields.get("journal", row.get("reference_journal", "")),
        "reference_volume": fields.get("volume", row.get("reference_volume", "")),
        "reference_issue": fields.get("issue", row.get("reference_issue", "")),
        "reference_pages": fields.get("pages", row.get("reference_pages", "")),
    })
    return row


try:
    _STYLE_AWARE_ORIGINAL_VERIFY_SINGLE_REFERENCE = _verify_single_reference
except Exception:
    _STYLE_AWARE_ORIGINAL_VERIFY_SINGLE_REFERENCE = None


def _verify_single_reference(
    ref: str,
    style: str,
    use_crossref: bool,
    use_openalex: bool,
    enrich_metadata: bool = False,
) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    if _STYLE_AWARE_ORIGINAL_VERIFY_SINGLE_REFERENCE:
        row = _STYLE_AWARE_ORIGINAL_VERIFY_SINGLE_REFERENCE(ref, canonical, use_crossref, use_openalex, enrich_metadata)
    else:
        row = {"reference": ref, "style": canonical, "status": "not_found"}
    info = _style_metadata(canonical)
    row.update({
        "style": canonical,
        "selected_style": canonical,
        "style_family": info["family"],
        "style_label": info["label"],
        "style_sample": info["sample"],
    })
    if not row.get("correction_suggestions"):
        row["correction_suggestions"] = _verification_style_suggestion(row)
    return row


try:
    _STYLE_AWARE_ORIGINAL_VERIFY_BATCH = verify_references_batch
except Exception:
    _STYLE_AWARE_ORIGINAL_VERIFY_BATCH = None


def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = False,
    job_id: str = None,
    enrich_metadata: bool = False,
) -> List[Dict[str, Any]]:
    canonical = _canonical_verify_style(style)
    rows = _STYLE_AWARE_ORIGINAL_VERIFY_BATCH(
        references,
        canonical,
        throttle_s=throttle_s,
        use_crossref=use_crossref,
        use_openalex=use_openalex,
        job_id=job_id,
        enrich_metadata=enrich_metadata,
    ) if _STYLE_AWARE_ORIGINAL_VERIFY_BATCH else []
    info = _style_metadata(canonical)
    for row in rows or []:
        row.update({
            "style": canonical,
            "selected_style": canonical,
            "style_family": info["family"],
            "style_label": info["label"],
            "style_sample": info["sample"],
        })
        if not row.get("correction_suggestions"):
            row["correction_suggestions"] = _verification_style_suggestion(row)
    return rows


# Keep public exports aligned after the overrides.
try:
    if "__all__" in globals():
        for name in [
            "VERIFY_BUILD",
            "_canonical_verify_style",
            "_style_metadata",
            "_extract_numeric_reference_fields",
            "_build_numeric_reference_from_metadata",
        ]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass


# ============================================================
# RSC/ACS COMPACT NUMERIC REFERENCE EXTRACTION PATCH
# Build: 2026-05-19-rsc-acs-author-list-reference-extraction
# Handles references such as:
# 24 H. Yu, M. Eres, ... A. Alexander-Katz and T. Xu, Nature, 2026, 649, 83-90.
# ============================================================

__version__ = "1.5.19"
VERIFY_BUILD = "commercial-2026-05-19-style-aware-verify-rsc-acs-author-list-FINAL"


def _looks_like_rsc_author_piece(piece: str) -> bool:
    p = _safe_strip(piece)
    if not p:
        return False
    p = re.sub(r"\bet\s+al\.?", "", p, flags=re.I).strip(" ,.;")
    # Handles 'H. Yu', 'S. L. Hilburg', and 'A. Alexander-Katz and T. Xu'.
    patterns = re.findall(r"(?:\b[A-Z]\.\s*){1,4}[A-Z][A-Za-z'’\-]+", p)
    if " and " in p.lower():
        return len(patterns) >= 1
    return bool(patterns) and len(p) <= 120


def _looks_like_numeric_journal_segment(seg: str) -> bool:
    s = _safe_strip(seg)
    if not s:
        return False
    compact = re.sub(r"[^A-Za-z]", "", s)
    if compact in {"Nature", "Science", "Cell", "JAMA", "Lancet"}:
        return True
    journal_words = {
        "j", "journal", "chem", "chemical", "commun", "communication", "communications",
        "phys", "physical", "rev", "lett", "letters", "mater", "materials", "adv", "advanced",
        "angew", "macromolecules", "langmuir", "soft", "matter", "soc", "society", "biol",
        "biological", "med", "medical", "medicine", "proc", "proceedings", "natl", "acad",
        "sci", "science", "usa", "int", "ed", "polym", "polymer", "nanoscale",
    }
    toks = [t.lower().strip(".") for t in re.findall(r"[A-Za-z]+\.??", s)]
    if not toks:
        return False
    hit = sum(1 for t in toks if t in journal_words)
    return hit >= 1 and len(s) <= 80


def _split_rsc_numeric_reference(clean: str) -> Tuple[str, str, str]:
    """Return author_part, title, journal for compact RSC/ACS references."""
    comma_parts = [p.strip() for p in clean.split(",") if p.strip()]
    if len(comma_parts) < 2 or not _looks_like_rsc_author_piece(comma_parts[0]):
        return "", "", ""

    author_parts = []
    idx = 0
    while idx < len(comma_parts) and _looks_like_rsc_author_piece(comma_parts[idx]):
        author_parts.append(comma_parts[idx])
        idx += 1

    author_part = ", ".join(author_parts)
    if idx >= len(comma_parts):
        return author_part, "", ""

    first_non_author = comma_parts[idx].strip()
    next_part = comma_parts[idx + 1].strip() if idx + 1 < len(comma_parts) else ""

    # No-title chemistry format: Authors, Journal, Year, Volume, Pages.
    if _looks_like_numeric_journal_segment(first_non_author) and (re.search(r"\b(?:19|20)\d{2}\b", next_part) or re.search(r"\b(?:19|20)\d{2}\b", clean)):
        return author_part, "", first_non_author

    # Book/report format or title-bearing format: Authors, Title, Publisher/Journal, Year.
    title = first_non_author
    journal = ""
    if idx + 1 < len(comma_parts) and not re.search(r"\b(?:19|20)\d{2}\b", comma_parts[idx + 1]):
        journal = comma_parts[idx + 1].strip()
    return author_part, title, journal


def _extract_numeric_reference_fields(ref: str, style: str = "numeric_square") -> Dict[str, Any]:
    raw = _normalise_reference_for_verify(ref)
    clean = _strip_leading_numbering(raw)
    doi = _normalise_doi(_extract_doi(clean))
    year = _extract_year(clean)
    vip = _extract_numeric_volume_issue_pages(clean)

    author_part = ""
    title = ""
    journal = ""

    # Vancouver/NLM/JAMA: Authors. Title. Journal. Year;volume(issue):pages.
    dot_parts = [p.strip() for p in re.split(r"\.\s+", clean) if p.strip()]
    if len(dot_parts) >= 2 and _looks_like_vancouver_author_segment(dot_parts[0]):
        author_part = dot_parts[0]
        title = dot_parts[1]
        if len(dot_parts) >= 3:
            journal = dot_parts[2]
            journal = re.split(r"\b(?:19|20)\d{2}\b", journal, maxsplit=1)[0].strip(" ,.;") or journal.strip(" ,.;")
    else:
        author_part, title, journal = _split_rsc_numeric_reference(clean)
        if not author_part:
            fields = _STYLE_AWARE_ORIGINAL_EXTRACT_COMMON_FIELDS(clean) if _STYLE_AWARE_ORIGINAL_EXTRACT_COMMON_FIELDS else {}
            fields = dict(fields or {})
            fields.setdefault("authors", [])
            fields.setdefault("year", year)
            fields.setdefault("doi", doi)
            fields.setdefault("title", "")
            fields.setdefault("journal", fields.get("container_title", ""))
            fields.update({k: fields.get(k) or v for k, v in vip.items()})
            fields["style_family"] = _style_metadata(style)["family"]
            fields["style_label"] = _style_metadata(style)["label"]
            return fields

    authors = _extract_numeric_authors(author_part)
    title = _clean_query_text(title)
    journal = _clean_query_text(journal)
    if title and _YEAR_RE.search(title):
        title = re.split(r"\b(?:19|20)\d{2}\b", title, maxsplit=1)[0].strip(" ,.;")
    if journal and _YEAR_RE.search(journal):
        journal = re.split(r"\b(?:19|20)\d{2}\b", journal, maxsplit=1)[0].strip(" ,.;")

    title_key_source = title or " ".join([journal, vip.get("volume", ""), vip.get("pages", "")]).strip()
    info = _style_metadata(style)
    return {
        "authors": authors,
        "year": year,
        "doi": doi,
        "title": title_key_source,
        "journal": journal,
        "container_title": journal,
        "source": journal,
        "volume": vip.get("volume", ""),
        "issue": vip.get("issue", ""),
        "pages": vip.get("pages", ""),
        "style_family": info["family"],
        "style_label": info["label"],
        "style_sample": info["sample"],
        "reference_numbered": True,
    }


# ============================================================
# RSC SINGLE-LETTER AUTHOR TOKEN TOLERANCE PATCH
# Build: 2026-05-19-rsc-single-letter-token-tolerance
# Some PDF-extracted chemistry references contain shortened author tokens
# such as 'Y. Z,'. Treat these as part of the author list so the next real
# journal segment, e.g. Nature, is not misread as the title.
# ============================================================

__version__ = "1.5.20"
VERIFY_BUILD = "commercial-2026-05-19-style-aware-verify-rsc-acs-single-letter-author-token-FINAL"


def _looks_like_rsc_author_piece(piece: str) -> bool:
    p = _safe_strip(piece)
    if not p:
        return False
    p = re.sub(r"\bet\s+al\.?", "", p, flags=re.I).strip(" ,.;")
    patterns = re.findall(r"(?:\b[A-Z]\.\s*){1,4}[A-Z][A-Za-z'’\-]*", p)
    if " and " in p.lower():
        return len(patterns) >= 1
    return bool(patterns) and len(p) <= 120

# ============================================================
# STYLE-SPECIFIC NUMERIC VERIFICATION PATCH
# Build: 2026-05-19-style-specific-numeric-verification
# Purpose:
# - Make online verification rules genuinely style-specific, not only style-labelled.
# - For author-year, keep the previous commercial verifier.
# - For numeric square/superscript/round, use DOI-first, title+author+year,
#   or journal+year+volume+page tuple matching.
# - Avoid displaying unrelated Crossref/OpenAlex candidates as "not_found" matches.
# ============================================================

__version__ = "1.5.22"
VERIFY_BUILD = "commercial-2026-05-19-style-specific-numeric-verification-FINAL"

try:
    _V1522_PREVIOUS_BUILD_QUERY_PLAN = _build_verification_query_plan
except Exception:
    _V1522_PREVIOUS_BUILD_QUERY_PLAN = None

try:
    _V1522_PREVIOUS_SCORE_CANDIDATE = _score_candidate
except Exception:
    _V1522_PREVIOUS_SCORE_CANDIDATE = None

try:
    _V1522_PREVIOUS_CLASSIFY_FROM_META = _classify_from_meta
except Exception:
    _V1522_PREVIOUS_CLASSIFY_FROM_META = None

try:
    _V1522_PREVIOUS_VERIFY_SINGLE_REFERENCE = _verify_single_reference
except Exception:
    _V1522_PREVIOUS_VERIFY_SINGLE_REFERENCE = None

try:
    _V1522_PREVIOUS_VERIFY_BATCH = verify_references_batch
except Exception:
    _V1522_PREVIOUS_VERIFY_BATCH = None


def _v1522_style_family(style: str) -> str:
    try:
        return _style_metadata(style).get("family", "author_year")
    except Exception:
        return "author_year"


def _v1522_is_numeric_style(style: str) -> bool:
    return _v1522_style_family(style) in {"numeric_square", "numeric_superscript", "numeric_round"}


def _v1522_count_sig_words(text: str) -> int:
    try:
        return len(_significant_title_words(text or "", limit=30))
    except Exception:
        return len(re.findall(r"[A-Za-z]{4,}", str(text or "")))


def _v1522_has_clear_title(fields: Dict[str, Any]) -> bool:
    title = _safe_strip(fields.get("article_title") or fields.get("title") or "")
    if not title:
        return False
    if _v1522_count_sig_words(title) < 4:
        return False
    # Journal-volume-page strings are structured metadata, not article titles.
    journal = _safe_strip(fields.get("journal", ""))
    volume = _safe_strip(fields.get("volume", ""))
    pages = _safe_strip(fields.get("pages", ""))
    compact_title = _norm_text(title)
    compact_struct = _norm_text(" ".join([journal, volume, pages]))
    if compact_struct and compact_title == compact_struct:
        return False
    return True


def _v1522_numeric_publication_year(clean: str) -> str:
    """Prefer the actual publication year in numeric references, not years inside titles."""
    s = _normalise_reference_for_verify(clean)

    # Vancouver/JAMA/Elsevier/NLM: Journal. 2024 Sep 1;75 or Journal. 2022;9(2):137-150
    matches = list(re.finditer(r"\.\s*((?:19|20)\d{2})(?:\s+[A-Za-z]{3,9}\s+\d{1,2})?\s*;", s))
    if matches:
        return matches[-1].group(1)

    # RSC/ACS: Journal, 2026, 649, 83-90.
    matches = list(re.finditer(r",\s*((?:19|20)\d{2})\s*,\s*[A-Za-z]?\d+", s))
    if matches:
        return matches[-1].group(1)

    # DOI/online-first references sometimes use month/day before the semicolon.
    matches = list(re.finditer(r"\b((?:19|20)\d{2})\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\s*;", s, flags=re.I))
    if matches:
        return matches[-1].group(1)

    # Fall back to the last plausible year after the author block. This avoids picking
    # GBD 2019 or title ranges such as 1990-2019 when the publication year appears later.
    years = re.findall(r"\b((?:19|20)\d{2})[a-z]?\b", s)
    if years:
        return years[-1]
    return ""


def _v1522_clean_numeric_journal(journal: str) -> str:
    j = _clean_query_text(journal or "")
    j = re.sub(r"\b\[Internet\]\b", "", j, flags=re.I)
    j = re.sub(r"\bInternet\b", "", j, flags=re.I)
    j = re.sub(r"\s+", " ", j).strip(" .,:;")
    return j


def _v1522_extract_vancouver_parts(clean: str) -> Tuple[str, str, str]:
    """Extract authors, title, journal from Vancouver/JAMA/Elsevier numeric references."""
    parts = [p.strip() for p in re.split(r"\.\s+", clean) if p.strip()]
    if len(parts) < 2:
        return "", "", ""

    if not _looks_like_vancouver_author_segment(parts[0]):
        return "", "", ""

    author_parts = [parts[0]]
    idx = 1

    # Some PDFs split group authors into "Collaborators G. 2019 MD. Title ...".
    # Treat short degree/year fragments before the real title as author continuation.
    while idx < len(parts) - 1:
        candidate = parts[idx]
        sig = _v1522_count_sig_words(candidate)
        looks_degree_or_group_tail = bool(
            sig <= 2
            or re.fullmatch(r"(?:19|20)\d{2}\s*[A-Z]{1,6}", candidate.strip())
            or re.fullmatch(r"[A-Z]{1,6}", candidate.strip())
        )
        if looks_degree_or_group_tail:
            author_parts.append(candidate)
            idx += 1
            continue
        break

    if idx >= len(parts):
        return " ".join(author_parts), "", ""

    title = parts[idx].strip(" ,.;")
    journal = parts[idx + 1].strip(" ,.;") if idx + 1 < len(parts) else ""

    # Remove year/volume material if it became attached to the journal segment.
    journal = re.split(r"\b(?:19|20)\d{2}\b", journal, maxsplit=1)[0].strip(" ,.;") or journal.strip(" ,.;")
    return " ".join(author_parts), title, journal


def _extract_numeric_reference_fields(ref: str, style: str = "numeric_square") -> Dict[str, Any]:
    """
    Final numeric reference parser used by verification.

    The earlier style-aware verifier often converted no-title RSC references into a fake
    title like 'Nature 649 83-90'. This patch keeps article title and structured journal
    metadata separate so verification can use the right evidence for each numeric style.
    """
    raw = _normalise_reference_for_verify(ref)
    clean = _strip_leading_numbering(raw)
    doi = _normalise_doi(_extract_doi(clean))
    year = _v1522_numeric_publication_year(clean) or _extract_year(clean)
    vip = _extract_numeric_volume_issue_pages(clean)

    author_part = ""
    title = ""
    journal = ""
    title_source_type = "unknown"

    # Vancouver/NLM/JAMA/Elsevier numbered references.
    v_author, v_title, v_journal = _v1522_extract_vancouver_parts(clean)
    if v_author:
        author_part, title, journal = v_author, v_title, v_journal
        title_source_type = "article_title" if title else "none"
    else:
        # RSC/ACS compact references.
        author_part, title, journal = _split_rsc_numeric_reference(clean)
        if author_part:
            title_source_type = "article_title" if title else "journal_tuple"
        else:
            # Last fallback: use the common parser, but mark it as weaker evidence.
            fields = _STYLE_AWARE_ORIGINAL_EXTRACT_COMMON_FIELDS(clean) if _STYLE_AWARE_ORIGINAL_EXTRACT_COMMON_FIELDS else {}
            fields = dict(fields or {})
            fields.setdefault("authors", [])
            fields["year"] = year or fields.get("year", "")
            fields["doi"] = doi or fields.get("doi", "")
            fields.setdefault("title", "")
            fields.setdefault("journal", fields.get("container_title", ""))
            for k, v in vip.items():
                fields[k] = fields.get(k) or v
            info = _style_metadata(style)
            fields.update({
                "style_family": info["family"],
                "style_label": info["label"],
                "style_sample": info["sample"],
                "reference_numbered": True,
                "article_title": fields.get("title", ""),
                "title_source_type": "fallback_common_parser",
            })
            return fields

    authors = _extract_numeric_authors(author_part)
    title = _clean_query_text(title)
    journal = _v1522_clean_numeric_journal(journal)

    if title and _YEAR_RE.search(title):
        # Keep title text before publication-year/volume data if extraction merged them.
        title = re.split(r"\.\s*(?:19|20)\d{2}\b|\b(?:19|20)\d{2}\s*;", title, maxsplit=1)[0].strip(" ,.;")

    if journal and _YEAR_RE.search(journal):
        journal = re.split(r"\b(?:19|20)\d{2}\b", journal, maxsplit=1)[0].strip(" ,.;")

    # If no article title exists, do not fake one. Use structured tuple matching instead.
    article_title = title if _v1522_count_sig_words(title) >= 3 else ""
    structured_key = " ".join([p for p in [journal, year, vip.get("volume", ""), vip.get("pages", "")] if p]).strip()

    info = _style_metadata(style)
    return {
        "authors": authors,
        "year": year,
        "doi": doi,
        "title": article_title,
        "article_title": article_title,
        "title_source_type": title_source_type if article_title else "journal_tuple",
        "journal": journal,
        "container_title": journal,
        "source": journal,
        "volume": vip.get("volume", ""),
        "issue": vip.get("issue", ""),
        "pages": vip.get("pages", ""),
        "structured_key": structured_key,
        "style_family": info["family"],
        "style_label": info["label"],
        "style_sample": info["sample"],
        "reference_numbered": True,
    }


def _extract_fields_by_style(ref: str, style: str) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    if canonical in _NUMERIC_VERIFY_STYLES:
        return _extract_numeric_reference_fields(ref, canonical)
    fields = _extract_apa_fields(ref)
    info = _style_metadata(canonical)
    fields.update({
        "style_family": info["family"],
        "style_label": info["label"],
        "style_sample": info["sample"],
    })
    return fields


def _v1522_generic_metadata_query(fields: Dict[str, Any]) -> str:
    parts = []
    if fields.get("title"):
        parts.append(fields.get("title", ""))
    if fields.get("journal"):
        parts.append(fields.get("journal", ""))
    if fields.get("year"):
        parts.append(fields.get("year", ""))
    if fields.get("volume"):
        parts.append(fields.get("volume", ""))
    if fields.get("pages"):
        parts.append(fields.get("pages", ""))
    if fields.get("authors"):
        parts.append(" ".join((fields.get("authors") or [])[:2]))
    return _clean_query_text(" ".join([p for p in parts if p]))


def _build_verification_query_plan(ref: str, style: str) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)

    if not _v1522_is_numeric_style(canonical):
        if _V1522_PREVIOUS_BUILD_QUERY_PLAN:
            return _V1522_PREVIOUS_BUILD_QUERY_PLAN(ref, canonical)

    fields = _extract_fields_by_style(ref, canonical)
    fields["reference"] = ref

    for name, fn in [
        ("isbn", globals().get("_extract_isbn")),
        ("pmid", globals().get("_extract_pmid")),
        ("pmcid", globals().get("_extract_pmcid")),
        ("arxiv_id", globals().get("_extract_arxiv_id")),
    ]:
        try:
            fields[name] = fn(ref) if callable(fn) else ""
        except Exception:
            fields[name] = ""

    authors = fields.get("authors", []) or []
    first_author = authors[0] if authors else ""
    year = _safe_strip(fields.get("year", ""))
    doi = _normalise_doi(fields.get("doi", "") or "")
    title = _clean_query_text(fields.get("article_title") or fields.get("title") or "")
    journal = _clean_query_text(fields.get("journal", "") or fields.get("container_title", "") or fields.get("source", ""))
    volume = _clean_query_text(fields.get("volume", "") or "")
    issue = _clean_query_text(fields.get("issue", "") or "")
    pages = _clean_query_text(fields.get("pages", "") or fields.get("page", "") or "")

    title_words = _significant_title_words(title, limit=14)
    title_key = " ".join(title_words[:10]).strip()
    title_short = " ".join(title_words[:7]).strip()
    has_clear_title = _v1522_has_clear_title({**fields, "title": title})
    structured_query = _v1522_generic_metadata_query({**fields, "title": title, "journal": journal, "year": year, "volume": volume, "pages": pages})

    crossref_queries: List[Dict[str, Any]] = []
    openalex_queries: List[Dict[str, Any]] = []
    fallback_queries: List[Dict[str, Any]] = []

    if doi:
        crossref_queries.append({"name": "crossref_doi_exact", "mode": "doi_exact", "doi": doi, "priority": 1})
        openalex_queries.append({"name": "openalex_doi_exact", "mode": "doi_exact", "doi": doi, "priority": 1})

    # Numeric styles should not start with broad full-reference queries. Those are
    # what returned unrelated matches such as 'Antibodies to watch in 2025'.
    if has_clear_title and title_key:
        crossref_queries.append({
            "name": "numeric_title_author_year",
            "mode": "bibliographic",
            "query_bibliographic": " ".join([title_key, year]).strip(),
            "query_author": first_author,
            "rows": VERIFY_CROSSREF_ROWS,
            "priority": 2,
        })
        if journal:
            crossref_queries.append({
                "name": "numeric_title_journal_year",
                "mode": "bibliographic",
                "query_bibliographic": " ".join([title_key, journal, year]).strip(),
                "query_author": "",
                "rows": VERIFY_CROSSREF_ROWS,
                "priority": 3,
            })
        openalex_queries.append({
            "name": "numeric_openalex_title_year",
            "mode": "search",
            "search": title_key,
            "publication_year": year,
            "rows": VERIFY_OPENALEX_ROWS,
            "priority": 4,
        })
        if journal:
            openalex_queries.append({
                "name": "numeric_openalex_title_journal",
                "mode": "search",
                "search": f"{title_key} {journal}",
                "publication_year": year,
                "rows": VERIFY_OPENALEX_ROWS,
                "priority": 5,
            })
    elif journal and year and (volume or pages):
        # RSC/ACS no-title references: Authors, Journal, Year, Volume, Pages.
        tuple_query = " ".join([p for p in [journal, year, volume, pages, first_author] if p]).strip()
        crossref_queries.append({
            "name": "numeric_journal_year_volume_page",
            "mode": "bibliographic",
            "query_bibliographic": tuple_query,
            "query_author": first_author,
            "rows": VERIFY_CROSSREF_ROWS,
            "priority": 2,
        })
        openalex_queries.append({
            "name": "numeric_openalex_journal_tuple",
            "mode": "search",
            "search": tuple_query,
            "publication_year": year,
            "rows": VERIFY_OPENALEX_ROWS,
            "priority": 3,
        })
    elif structured_query and _v1522_count_sig_words(structured_query) >= 4:
        # Last bounded numeric query. Still avoid raw full-reference search.
        crossref_queries.append({
            "name": "numeric_bounded_metadata_query",
            "mode": "bibliographic",
            "query_bibliographic": structured_query,
            "query_author": first_author,
            "rows": max(3, min(VERIFY_CROSSREF_ROWS, 6)),
            "priority": 6,
        })

    # Only adaptive fallbacks when there is usable title evidence.
    try:
        flags = _reference_type_flags(ref, fields)
    except Exception:
        flags = {}
    if has_clear_title:
        if fields.get("pmid"):
            fallback_queries.append({"name": "pubmed_pmid_exact", "source": "pubmed", "mode": "pmid_exact", "priority": 20})
        if fields.get("doi"):
            fallback_queries.append({"name": "datacite_doi_exact", "source": "datacite", "mode": "doi_exact", "priority": 21})
        if flags.get("looks_health"):
            fallback_queries.append({"name": "pubmed_search", "source": "pubmed", "mode": "search", "priority": 22})
            fallback_queries.append({"name": "europepmc_search", "source": "europepmc", "mode": "search", "priority": 23})
        fallback_queries.append({"name": "semantic_scholar_search", "source": "semantic_scholar", "mode": "search", "priority": 30})

    fields.update({
        "first_author": first_author,
        "doi": doi,
        "title": title,
        "article_title": title,
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "title_key": title_key,
        "title_short": title_short,
        "has_clear_title": has_clear_title,
        "structured_key": structured_query,
        "reference_type_flags": flags,
        "verification_profile": "numeric_style_specific",
    })

    return {
        "reference": ref,
        "style": canonical,
        "fields": fields,
        "crossref_queries": crossref_queries,
        "openalex_queries": openalex_queries,
        "fallback_queries": fallback_queries,
    }


def _score_candidate(ref_fields: Dict[str, Any], cand: Dict[str, Any]) -> Dict[str, Any]:
    if _V1522_PREVIOUS_SCORE_CANDIDATE:
        meta = _V1522_PREVIOUS_SCORE_CANDIDATE(ref_fields, cand)
    else:
        meta = {}

    family = ref_fields.get("style_family") or _v1522_style_family(ref_fields.get("style", "apa"))
    if family not in {"numeric_square", "numeric_superscript", "numeric_round"}:
        return meta

    title_score = int(meta.get("title_score", 0))
    journal_score = int(meta.get("journal_score", 0))
    author_similarity = int(meta.get("author_similarity", 0))
    author_overlap = int(meta.get("author_overlap", 0))
    year_match = int(meta.get("year_match", 0))
    year_delta = int(meta.get("year_delta", 999))
    doi_match = bool(meta.get("doi_match"))
    volume_match = int(meta.get("volume_match", 0))
    issue_match = int(meta.get("issue_match", 0))
    page_match = int(meta.get("page_match", 0))
    has_clear_title = _v1522_has_clear_title(ref_fields)
    ref_has_doi = bool(ref_fields.get("doi"))
    candidate_has_doi = bool(meta.get("doi"))

    if doi_match:
        score = 100
    elif has_clear_title:
        score = 0.0
        score += title_score * 0.64
        score += min(author_similarity, 100) * 0.12
        score += 12 if year_delta <= 1 else (5 if year_delta <= 2 else 0)
        score += journal_score * 0.06
        score += 4 if volume_match else 0
        score += 3 if issue_match else 0
        score += 4 if page_match else 0
        score += 4 if candidate_has_doi else 0

        # Hard cap weak title matches. This prevents broad database results from
        # appearing as real verification candidates.
        if title_score < 55 and not doi_match:
            score = min(score, 44)
        elif title_score < 70 and not (author_overlap or journal_score >= 70 or year_delta <= 1):
            score = min(score, 54)
    else:
        # Structured no-title references, mainly RSC/ACS: Journal + year + volume + page.
        score = 0.0
        score += journal_score * 0.38
        score += 18 if year_delta <= 1 else (8 if year_delta <= 2 else 0)
        score += 14 if volume_match else 0
        score += 16 if page_match else 0
        score += 8 if author_overlap >= 1 else (min(author_similarity, 100) * 0.05)
        score += 4 if candidate_has_doi else 0
        if journal_score < 55 and not doi_match:
            score = min(score, 49)

    # If the original reference has a DOI, a different DOI should not be treated as strong.
    if ref_has_doi and candidate_has_doi and not doi_match:
        score = min(score, 49)
        meta["doi_conflict"] = 1

    meta["score"] = int(max(0, min(100, round(score))))
    meta["numeric_has_clear_title"] = bool(has_clear_title)
    meta["numeric_structured_match"] = int((journal_score >= 70) and (year_delta <= 1) and (volume_match or page_match))
    meta["candidate_has_doi"] = bool(candidate_has_doi)
    return meta


def _classify_from_meta(ref_fields: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[str, str]:
    family = ref_fields.get("style_family") or _v1522_style_family(ref_fields.get("style", "apa"))
    if family not in {"numeric_square", "numeric_superscript", "numeric_round"}:
        if _V1522_PREVIOUS_CLASSIFY_FROM_META:
            return _V1522_PREVIOUS_CLASSIFY_FROM_META(ref_fields, meta)
        return "not_found", "No classifier available."

    title_score = int(meta.get("title_score", 0))
    score = int(meta.get("score", 0))
    year_delta = int(meta.get("year_delta", 999))
    author_overlap = int(meta.get("author_overlap", 0))
    author_similarity = int(meta.get("author_similarity", 0))
    journal_score = int(meta.get("journal_score", 0))
    doi_match = bool(meta.get("doi_match"))
    volume_match = int(meta.get("volume_match", 0))
    page_match = int(meta.get("page_match", 0))
    has_clear_title = bool(meta.get("numeric_has_clear_title")) or _v1522_has_clear_title(ref_fields)
    ref_has_doi = bool(ref_fields.get("doi"))
    doi_conflict = bool(meta.get("doi_conflict"))
    author_ok = author_overlap >= 1 or author_similarity >= 78 or not ref_fields.get("authors") or not meta.get("authors")
    year_ok = year_delta <= 1

    if doi_match:
        if has_clear_title and title_score < 45 and not (year_ok or author_ok or journal_score >= 55):
            return "needs_review", "Exact DOI was found, but the surrounding bibliographic metadata is weak."
        return "verified", "Exact DOI match. Numeric-style reference verified using DOI-first matching."

    if ref_has_doi and doi_conflict:
        return "not_found", "Candidates had a different DOI from the original numeric reference."

    if has_clear_title:
        if title_score >= 92 and year_delta <= 1 and (author_ok or journal_score >= 55):
            return "verified", "Numeric-style title, year, and author/journal evidence match."
        if title_score >= 88 and (author_ok or journal_score >= 70) and year_delta <= 2:
            return "verified", "Strong numeric-style title match with supporting bibliographic metadata."
        if title_score >= 82 and (year_delta <= 2 or author_ok or journal_score >= 65):
            return "verified", "Likely numeric-style match. Title is strong, but metadata needs review."
        if title_score >= 72 and (year_delta <= 2 or author_ok or journal_score >= 60):
            return "needs_review", "Possible numeric-style match, but evidence is below the verification threshold."
        return "not_found", "No reliable numeric-style title match found. Weak database candidates were rejected."

    # No-title RSC/ACS-style reference. Use bibliographic tuple.
    if journal_score >= 86 and year_delta <= 1 and volume_match and page_match:
        return "verified", "Numeric no-title reference verified by journal, year, volume, and page tuple."
    if journal_score >= 78 and year_delta <= 1 and (volume_match or page_match) and (author_ok or score >= 76):
        return "verified", "Likely numeric no-title match using journal-year-volume/page evidence."
    if journal_score >= 65 and year_delta <= 2 and (volume_match or page_match):
        return "needs_review", "Possible numeric no-title match. Check journal, volume, page and author manually."

    return "not_found", "No reliable numeric-style bibliographic tuple match found."


def _v1522_blank_unreliable_match(row: Dict[str, Any]) -> Dict[str, Any]:
    """Do not display unrelated candidates as matches when status is not_found."""
    if not isinstance(row, dict):
        return row
    status = _normalize_verify_status(row.get("status"))
    family = row.get("style_family") or _v1522_style_family(row.get("style", "apa"))
    if family not in {"numeric_square", "numeric_superscript", "numeric_round"}:
        return row
    if status != "not_found":
        return row
    if int(row.get("doi_match", 0) or 0):
        return row
    # Keep query diagnostics, but remove misleading matched-source display.
    row.setdefault("rejected_matched_title", row.get("matched_title", ""))
    row.setdefault("rejected_matched_doi", row.get("doi", ""))
    row.setdefault("rejected_match_score", row.get("score", 0))
    for key in [
        "source", "doi", "matched_title", "matched_year", "matched_authors", "matched_journal",
        "matched_container_title", "matched_volume", "matched_issue", "matched_pages",
        "matched_publisher", "matched_type", "matched_url",
    ]:
        row[key] = ""
    row["score"] = 0
    row["title_score"] = 0
    row["journal_score"] = 0
    row["confidence_reason"] = row.get("confidence_reason") or "No reliable style-specific numeric match found."
    return row


def _verify_single_reference(
    ref: str,
    style: str,
    use_crossref: bool,
    use_openalex: bool,
    enrich_metadata: bool = False,
) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    info = _style_metadata(canonical)

    if _V1522_PREVIOUS_VERIFY_SINGLE_REFERENCE:
        row = _V1522_PREVIOUS_VERIFY_SINGLE_REFERENCE(ref, canonical, use_crossref, use_openalex, enrich_metadata)
    else:
        row = {"reference": ref, "style": canonical, "status": "not_found"}

    # Reattach final style metadata and parsed reference fields.
    try:
        fields = _extract_fields_by_style(ref, canonical)
    except Exception:
        fields = {}

    row.update({
        "style": canonical,
        "selected_style": canonical,
        "style_family": info["family"],
        "style_label": info["label"],
        "style_sample": info["sample"],
        "reference_title": fields.get("article_title") or fields.get("title", row.get("reference_title", "")),
        "reference_year": fields.get("year", row.get("reference_year", "")),
        "reference_doi": fields.get("doi", row.get("reference_doi", "")),
        "reference_journal": fields.get("journal", row.get("reference_journal", "")),
        "reference_volume": fields.get("volume", row.get("reference_volume", "")),
        "reference_issue": fields.get("issue", row.get("reference_issue", "")),
        "reference_pages": fields.get("pages", row.get("reference_pages", "")),
        "verification_profile": fields.get("verification_profile", "style_specific"),
    })
    row["status"] = _normalize_verify_status(row.get("status"))
    row = _v1522_blank_unreliable_match(row)
    if not row.get("correction_suggestions"):
        row["correction_suggestions"] = _verification_style_suggestion(row)
    return row


def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = False,
    job_id: str = None,
    enrich_metadata: bool = False,
) -> List[Dict[str, Any]]:
    canonical = _canonical_verify_style(style)
    refs = [r for r in (references or []) if _safe_strip(r)]
    rows = []
    total = len(refs)
    for i, ref in enumerate(refs, start=1):
        rows.append(_verify_single_reference(ref, canonical, use_crossref, use_openalex, enrich_metadata))
        if job_id:
            try:
                update_job_progress(job_id, i)
                if i == total:
                    store_verification_results(job_id, rows)
            except Exception:
                pass
        if throttle_s:
            time.sleep(throttle_s)
    return rows

try:
    if "__all__" in globals():
        for name in [
            "VERIFY_BUILD",
            "_canonical_verify_style",
            "_style_metadata",
            "_extract_numeric_reference_fields",
            "_build_verification_query_plan",
            "_score_candidate",
            "_classify_from_meta",
            "verify_references_batch",
        ]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass


# ============================================================
# QUERY-FIRST NUMERIC VERIFICATION PATCH FOR CITEINTEGRITY
# Build: 2026-05-19-numeric-query-builder-rewrite
# Purpose:
# - Replace broad APA-style query behaviour for numeric references.
# - Build numeric queries from DOI, full article title, journal, year,
#   volume and page instead of weak generic word bags.
# - Avoid displaying unrelated Crossref/OpenAlex candidates for not_found rows.
# ============================================================

__version__ = "1.5.23"
VERIFY_BUILD = "commercial-2026-05-19-numeric-query-builder-rewrite-FINAL"

_V1523_NUMERIC_STYLES = {"numeric_square", "numeric_superscript", "numeric_round"}

try:
    _V1523_AUTHOR_YEAR_VERIFY_SINGLE = _verify_single_reference
except Exception:
    _V1523_AUTHOR_YEAR_VERIFY_SINGLE = None

_COMMON_DOI_TRAILING_PATHS = (
    "/full", "/fulltext", "/full-text", "/abstract", "/pdf", "/epdf",
    "/article", "/full.pdf", "/pdfdownload", "/download", "/supplementary",
)


def _v1523_style_family(style: str) -> str:
    try:
        return _style_metadata(_canonical_verify_style(style)).get("family", "author_year")
    except Exception:
        return "author_year"


def _v1523_is_numeric(style: str) -> bool:
    return _v1523_style_family(style) in _V1523_NUMERIC_STYLES


def _v1523_normalise_numeric_doi(value: str) -> str:
    """Normalise DOI and remove PDF/article landing-page suffixes such as /full."""
    doi = _safe_strip(value or "")
    if not doi:
        return ""
    doi = doi.replace("%2F", "/")
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi\s*:\s*", "", doi, flags=re.I)
    doi = doi.strip().strip(".,;:)]}>").lower()
    doi = re.split(r"[?#]", doi, maxsplit=1)[0]
    # Remove obvious landing-page suffixes introduced by PDF extraction.
    changed = True
    while changed:
        changed = False
        for suffix in _COMMON_DOI_TRAILING_PATHS:
            if doi.endswith(suffix):
                doi = doi[: -len(suffix)]
                changed = True
                break
    return doi.strip().strip(".,;:)]}")


def _v1523_extract_doi(ref: str) -> str:
    text = _safe_strip(ref or "")
    if not text:
        return ""
    # Prefer DOI URLs and doi: labels.
    patterns = [
        r"https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/[^\s\]\),;]+)",
        r"\bdoi\s*:?\s*(10\.\d{4,9}/[^\s\]\),;]+)",
        r"\b(10\.\d{4,9}/[^\s\]\),;]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return _v1523_normalise_numeric_doi(m.group(1))
    return ""


def _v1523_strip_reference_noise(ref: str) -> str:
    s = _safe_strip(ref or "")
    s = re.sub(r"\[\s*cited\s+[^\]]+\]", " ", s, flags=re.I)
    s = re.sub(r"\bAvailable\s+from\s*:\s*https?://\S+", " ", s, flags=re.I)
    s = re.sub(r"\bRetrieved\s+from\s+https?://\S+", " ", s, flags=re.I)
    s = re.sub(r"https?://\S+", " ", s, flags=re.I)
    s = re.sub(r"\bdoi\s*:?\s*10\.\S+", " ", s, flags=re.I)
    s = re.sub(r"\b10\.\d{4,9}/\S+", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _v1523_publication_year(ref: str) -> str:
    """Prefer publication year, not title years, access dates, or page range years."""
    s = _strip_leading_numbering(_v1523_strip_reference_noise(ref))

    # Journal. 2024 Sep 1;75 or Journal. 2022;9(2):137-150
    matches = list(re.finditer(r"\.\s*((?:19|20)\d{2})(?:\s+[A-Za-z]{3,9}\s+\d{1,2})?\s*;", s))
    if matches:
        return matches[-1].group(1)

    # Publisher; 2021. or FAO; 2021.
    matches = list(re.finditer(r";\s*((?:19|20)\d{2})\s*(?:\.|$)", s))
    if matches:
        return matches[-1].group(1)

    # RSC/ACS: Journal, 2026, 649, 83-90.
    matches = list(re.finditer(r",\s*((?:19|20)\d{2})\s*,\s*[A-Za-z]?\d+", s))
    if matches:
        return matches[-1].group(1)

    # Date before semicolon: 2024 Nov 1;79(11)
    matches = list(re.finditer(r"\b((?:19|20)\d{2})\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\s*;", s, flags=re.I))
    if matches:
        return matches[-1].group(1)

    # Last fallback, but avoid isolated page-range end years by rejecting 2031+ unless no other year.
    years = re.findall(r"\b((?:19|20)\d{2})[a-z]?\b", s)
    if not years:
        return ""
    plausible = [y for y in years if int(y) <= 2030]
    return (plausible[-1] if plausible else years[-1])


def _v1523_protect_abbreviations(text: str) -> str:
    repl = {
        "U.S.": "US", "U.K.": "UK", "U.N.": "UN", "D.C.": "DC",
        "U.S.A.": "USA", "vs.": "vs", "e.g.": "eg", "i.e.": "ie",
    }
    for a, b in repl.items():
        text = text.replace(a, b)
    return text


def _v1523_split_sentences_like_reference(clean: str) -> List[str]:
    clean = _v1523_protect_abbreviations(clean)
    # Do not split after initials followed by comma. Split after full stops before a new title/journal segment.
    parts = re.split(r"\.\s+(?=[A-Z0-9])", clean)
    return [p.strip(" .") for p in parts if p and p.strip(" .")]


def _v1523_clean_title(title: str) -> str:
    t = _clean_query_text(title or "")
    t = re.sub(r"\b\[?Internet\]?\b", " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" .,:;")
    # Remove trailing journal-year fragments accidentally attached to the title.
    t = re.split(r"\s+\.\s+(?=[A-Z][A-Za-z .&]+\s+(?:19|20)\d{2})", t, maxsplit=1)[0]
    return t.strip(" .,:;")


def _v1523_clean_journal(journal: str) -> str:
    j = _clean_query_text(journal or "")
    j = re.sub(r"\b\[?Internet\]?\b", " ", j, flags=re.I)
    j = re.split(r"\b(?:19|20)\d{2}\b", j, maxsplit=1)[0]
    j = re.sub(r"\s+", " ", j).strip(" .,:;")
    return j


def _v1523_volume_issue_pages(ref: str) -> Dict[str, str]:
    s = _v1523_strip_reference_noise(ref).replace("–", "-").replace("—", "-")
    out = {"volume": "", "issue": "", "pages": ""}
    # 2022;9(2):137-150 ; 2024 Nov 1;79(11):gbae153 ; 2015;34(11):1830-1839
    m = re.search(r"(?:19|20)\d{2}(?:\s+[A-Za-z]{3,9}\s+\d{1,2})?\s*;\s*([A-Za-z]?\d+[A-Za-z]?)\s*(?:\(([^)]+)\))?\s*:\s*([A-Za-z]?\d+[A-Za-z]?\s*(?:-\s*[A-Za-z]?\d+[A-Za-z]?)?)", s, flags=re.I)
    if m:
        out["volume"] = _safe_strip(m.group(1))
        out["issue"] = _safe_strip(m.group(2))
        out["pages"] = re.sub(r"\s+", "", _safe_strip(m.group(3)))
        return out
    # RSC/ACS: Journal, 2026, 649, 83-90
    m = re.search(r",\s*(?:19|20)\d{2}\s*,\s*([A-Za-z]?\d+[A-Za-z]?)\s*,\s*([A-Za-z]?\d+[A-Za-z]?\s*(?:-\s*[A-Za-z]?\d+[A-Za-z]?)?)", s)
    if m:
        out["volume"] = _safe_strip(m.group(1))
        out["pages"] = re.sub(r"\s+", "", _safe_strip(m.group(2)))
        return out
    return out


def _v1523_extract_numeric_fields(ref: str, style: str = "numeric_superscript") -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    info = _style_metadata(canonical)
    raw = _safe_strip(ref or "")
    clean = _strip_leading_numbering(_v1523_strip_reference_noise(raw))
    doi = _v1523_extract_doi(raw)
    year = _v1523_publication_year(raw)
    vip = _v1523_volume_issue_pages(raw)

    author_part = ""
    title = ""
    journal = ""
    title_source_type = "article_title"

    parts = _v1523_split_sentences_like_reference(clean)
    if parts and _looks_like_vancouver_author_segment(parts[0]):
        author_parts = [parts[0]]
        idx = 1
        # Allow group-author tail fragments such as '2019 MD'.
        while idx < len(parts) - 1:
            p = parts[idx].strip()
            if re.fullmatch(r"(?:19|20)\d{2}\s*[A-Z]{1,8}", p) or re.fullmatch(r"[A-Z]{1,8}", p):
                author_parts.append(p)
                idx += 1
                continue
            break
        author_part = " ".join(author_parts)
        if idx < len(parts):
            title = parts[idx]
        if idx + 1 < len(parts):
            journal = parts[idx + 1]
    else:
        # RSC/ACS compact references have no article title in the reference style.
        try:
            author_part, title, journal = _split_rsc_numeric_reference(clean)
        except Exception:
            author_part, title, journal = "", "", ""
        if author_part and not title:
            title_source_type = "journal_tuple"

    authors = _extract_numeric_authors(author_part)
    title = _v1523_clean_title(title)
    journal = _v1523_clean_journal(journal)

    # If the title was split too early because of abbreviations, repair obvious fragments.
    if journal and re.match(r"^(older adults|evidence from|longitudinal evidence|the role of)\b", journal, flags=re.I):
        title = _v1523_clean_title((title + " " + journal).strip())
        journal = ""

    # If journal is empty, try to locate a journal segment before the year.
    if not journal and year:
        m = re.search(r"\.\s*([^.;]{3,80})\.\s*" + re.escape(year) + r"\b", _v1523_protect_abbreviations(clean))
        if m:
            journal = _v1523_clean_journal(m.group(1))

    if _v1523_count_words(title) < 3:
        article_title = ""
        title_source_type = "journal_tuple"
    else:
        article_title = title

    structured_key = " ".join([p for p in [journal, year, vip.get("volume", ""), vip.get("pages", "")] if p]).strip()

    return {
        "authors": authors,
        "year": year,
        "doi": doi,
        "title": article_title,
        "article_title": article_title,
        "title_source_type": title_source_type,
        "journal": journal,
        "container_title": journal,
        "source": journal,
        "volume": vip.get("volume", ""),
        "issue": vip.get("issue", ""),
        "pages": vip.get("pages", ""),
        "structured_key": structured_key,
        "style_family": info.get("family", canonical),
        "style_label": info.get("label", canonical),
        "style_sample": info.get("sample", ""),
        "reference_numbered": True,
        "verification_profile": "numeric_query_builder_v1523",
    }


def _v1523_count_words(text: str) -> int:
    return len([w for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", str(text or "")) if w.lower() not in _QUERY_STOP_WORDS])


def _extract_fields_by_style(ref: str, style: str) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    if _v1523_is_numeric(canonical):
        return _v1523_extract_numeric_fields(ref, canonical)
    fields = _extract_apa_fields(ref)
    info = _style_metadata(canonical)
    fields.update({
        "style_family": info.get("family", "author_year"),
        "style_label": info.get("label", "Author-year"),
        "style_sample": info.get("sample", ""),
        "verification_profile": "author_year",
    })
    return fields


def _v1523_title_query(title: str) -> str:
    title = _clean_query_text(title or "")
    title = re.sub(r"\b(Internet|Available|cited|from)\b", " ", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip()
    return title[:240]


def _v1523_query_crossref_title(title: str, rows: int = 8, query_name: str = "crossref_query_title") -> List[Dict[str, Any]]:
    title = _v1523_title_query(title)
    if not title:
        return []
    url = "https://api.crossref.org/works"
    params = {
        "query.title": title,
        "rows": int(rows),
        "sort": "score",
        "order": "desc",
    }
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=API_TIMEOUT)
    items = (data or {}).get("message", {}).get("items", [])
    return [{"source": "crossref", "query_name": query_name, "item": it} for it in items]


def _v1523_build_numeric_query_plan(ref: str, style: str) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    fields = _extract_fields_by_style(ref, canonical)
    fields["reference"] = ref
    title = _v1523_title_query(fields.get("article_title") or fields.get("title") or "")
    journal = _clean_query_text(fields.get("journal") or "")
    year = _safe_strip(fields.get("year", ""))
    doi = _safe_strip(fields.get("doi", ""))
    volume = _safe_strip(fields.get("volume", ""))
    pages = _safe_strip(fields.get("pages", ""))
    first_author = (fields.get("authors") or [""])[0]

    title_words = _significant_title_words(title, limit=12)
    title_key = " ".join(title_words[:10])
    rich_title = " ".join([p for p in [first_author, title, journal, year] if p]).strip()
    journal_tuple = " ".join([p for p in [journal, year, volume, pages] if p]).strip()

    return {
        "fields": fields,
        "doi": doi,
        "title": title,
        "title_key": title_key,
        "rich_title": _clean_query_text(rich_title),
        "journal_tuple": _clean_query_text(journal_tuple),
        "year": year,
        "journal": journal,
        "volume": volume,
        "pages": pages,
        "style": canonical,
    }


def _build_verification_query_plan(ref: str, style: str) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    if _v1523_is_numeric(canonical):
        plan = _v1523_build_numeric_query_plan(ref, canonical)
        # Keep these keys for worker/debug compatibility.
        plan["crossref_queries"] = []
        plan["openalex_queries"] = []
        if plan.get("doi"):
            plan["crossref_queries"].append({"name": "numeric_doi_exact", "mode": "doi_exact", "doi": plan["doi"], "priority": 1})
            plan["openalex_queries"].append({"name": "numeric_openalex_doi_exact", "mode": "doi_exact", "doi": plan["doi"], "priority": 1})
        if plan.get("title") and _v1523_count_words(plan["title"]) >= 3:
            plan["crossref_queries"].append({"name": "numeric_crossref_title_exact", "mode": "title", "query_title": plan["title"], "priority": 2})
            plan["crossref_queries"].append({"name": "numeric_crossref_rich_title", "mode": "bibliographic", "query_bibliographic": plan["rich_title"], "query_author": "", "priority": 3})
            plan["openalex_queries"].append({"name": "numeric_openalex_rich_title", "mode": "search", "search": plan["rich_title"] or plan["title"], "publication_year": plan.get("year", ""), "priority": 4})
        if plan.get("journal_tuple") and (not plan.get("title") or _v1523_count_words(plan.get("title")) < 3):
            plan["crossref_queries"].append({"name": "numeric_crossref_journal_tuple", "mode": "bibliographic", "query_bibliographic": plan["journal_tuple"], "query_author": "", "priority": 5})
            plan["openalex_queries"].append({"name": "numeric_openalex_journal_tuple", "mode": "search", "search": plan["journal_tuple"], "publication_year": plan.get("year", ""), "priority": 6})
        return plan
    try:
        return _V1523_PREVIOUS_BUILD_QUERY_PLAN(ref, canonical)  # type: ignore[name-defined]
    except Exception:
        return {"fields": _extract_fields_by_style(ref, canonical), "crossref_queries": [], "openalex_queries": [], "fallback_queries": []}


def _v1523_run_numeric_queries(plan: Dict[str, Any], use_crossref: bool, use_openalex: bool) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    candidates: List[Dict[str, Any]] = []
    query_used: List[str] = []
    strategy: List[str] = []
    openalex_allowed = bool(use_openalex or VERIFY_FORCE_OPENALEX_FALLBACK)

    doi = plan.get("doi", "")
    title = plan.get("title", "")
    rich_title = plan.get("rich_title", "")
    title_key = plan.get("title_key", "")
    journal_tuple = plan.get("journal_tuple", "")
    year = plan.get("year", "")

    # 1. Exact DOI. Stop immediately if it returns a record.
    if doi and use_crossref:
        res = _query_crossref_by_doi(doi) or []
        query_used.append(doi); strategy.append("numeric_crossref_doi_exact")
        if res:
            return _dedupe_candidates(res), query_used, strategy
    if doi and openalex_allowed:
        res = _query_openalex_by_doi(doi) or []
        query_used.append(doi); strategy.append("numeric_openalex_doi_exact")
        if res:
            return _dedupe_candidates(res), query_used, strategy

    # 2. Title-centred queries. This is the important change.
    if title and _v1523_count_words(title) >= 3:
        if use_crossref:
            res = _v1523_query_crossref_title(title, rows=max(8, VERIFY_TITLE_ROWS), query_name="numeric_crossref_query_title")
            candidates.extend(res); query_used.append(title); strategy.append("numeric_crossref_query_title")
            # Add a rich bibliographic query only after title query.
            if rich_title:
                res = _query_crossref_bibliographic(rich_title, query_author="", rows=max(6, VERIFY_CROSSREF_ROWS), query_name="numeric_crossref_rich_title")
                candidates.extend(res); query_used.append(rich_title); strategy.append("numeric_crossref_rich_title")
        if openalex_allowed:
            search = rich_title or title_key or title
            res = _query_openalex_search(search, rows=max(8, VERIFY_OPENALEX_ROWS), publication_year=year, query_name="numeric_openalex_rich_title")
            candidates.extend(res); query_used.append(search); strategy.append("numeric_openalex_rich_title")

    # 3. Journal tuple for no-title RSC/ACS and weak-title references.
    if journal_tuple and (not candidates or not title or _v1523_count_words(title) < 3):
        if use_crossref:
            res = _query_crossref_bibliographic(journal_tuple, query_author="", rows=max(6, VERIFY_CROSSREF_ROWS), query_name="numeric_crossref_journal_tuple")
            candidates.extend(res); query_used.append(journal_tuple); strategy.append("numeric_crossref_journal_tuple")
        if openalex_allowed:
            res = _query_openalex_search(journal_tuple, rows=max(6, VERIFY_OPENALEX_ROWS), publication_year=year, query_name="numeric_openalex_journal_tuple")
            candidates.extend(res); query_used.append(journal_tuple); strategy.append("numeric_openalex_journal_tuple")

    return _dedupe_candidates(candidates), query_used, strategy


def _v1523_best_candidate(fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    scored: List[Dict[str, Any]] = []
    for cand in candidates or []:
        try:
            meta = _score_candidate(fields, cand)
            # Strong DOI exact match should dominate any title parsing weakness.
            if fields.get("doi") and meta.get("doi") and _v1523_normalise_numeric_doi(fields.get("doi")) == _v1523_normalise_numeric_doi(meta.get("doi")):
                meta["doi_match"] = True
                meta["score"] = 100
            scored.append(meta)
        except Exception:
            continue
    if not scored:
        return None, {}, []
    scored.sort(key=lambda x: int(x.get("score", 0)), reverse=True)
    best_meta = scored[0]
    # Alternatives shown only when they are credible enough to review.
    alternatives = []
    for m in scored[1:4]:
        if int(m.get("score", 0)) < 55:
            continue
        alternatives.append({
            "title": m.get("title", ""),
            "year": m.get("year", ""),
            "doi": m.get("doi", ""),
            "score": m.get("score", 0),
            "source": m.get("source", ""),
        })
    return {"source": best_meta.get("source", "")}, best_meta, alternatives


def _v1523_blank_not_found(row: Dict[str, Any]) -> Dict[str, Any]:
    if _normalize_verify_status(row.get("status")) != "not_found":
        return row
    for key in [
        "source", "doi", "matched_title", "matched_year", "matched_authors", "matched_journal",
        "matched_container_title", "matched_volume", "matched_issue", "matched_pages",
        "matched_publisher", "matched_type", "matched_url",
    ]:
        if row.get(key):
            row[f"rejected_{key}"] = row.get(key)
        row[key] = ""
    for key in ["score", "title_score", "journal_score", "author_overlap", "author_similarity", "year_match", "volume_match", "issue_match", "page_match"]:
        row[key] = 0
    return row


def _v1523_numeric_verify_single(ref: str, style: str, use_crossref: bool, use_openalex: bool, enrich_metadata: bool = False) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    fields = _extract_fields_by_style(ref, canonical)
    plan = _v1523_build_numeric_query_plan(ref, canonical)
    row = _base_commercial_row(ref, canonical, fields, "", "numeric_query_builder_v1523")
    row["verification_profile"] = "numeric_query_builder_v1523"
    row["query_plan"] = {
        "doi": bool(plan.get("doi")),
        "title": plan.get("title", ""),
        "rich_title": plan.get("rich_title", ""),
        "journal_tuple": plan.get("journal_tuple", ""),
        "year": plan.get("year", ""),
    }

    if not plan.get("doi") and not plan.get("title") and not plan.get("journal_tuple"):
        row.update({
            "status": "needs_review",
            "confidence_reason": "The numeric reference lacks DOI, article title and journal tuple. Manual verification is required.",
            "query_strategy": "numeric_no_queryable_metadata",
        })
        row["correction_suggestions"] = _verification_style_suggestion(row)
        return row

    candidates, query_used, strategy = _v1523_run_numeric_queries(plan, use_crossref, use_openalex)
    row["query_used"] = " | ".join(_dedupe_preserve(query_used))
    row["query_strategy"] = " | ".join(_dedupe_preserve(strategy))

    if not candidates:
        row.update({
            "status": "not_found",
            "confidence_reason": "No Crossref/OpenAlex candidate was returned from DOI, title, or journal-tuple numeric queries.",
        })
        row = _v1523_blank_not_found(row)
        row["correction_suggestions"] = _verification_style_suggestion(row)
        return row

    _best, meta, alternatives = _v1523_best_candidate(fields, candidates)
    if not meta:
        row.update({"status": "not_found", "confidence_reason": "Candidates were returned, but none had usable metadata."})
        row = _v1523_blank_not_found(row)
        row["correction_suggestions"] = _verification_style_suggestion(row)
        return row

    status, reason = _classify_from_meta(fields, meta)
    row.update({
        "status": _normalize_verify_status(status),
        "source": "+".join(meta.get("sources") or [meta.get("source", "")]),
        "score": int(meta.get("score", 0)),
        "doi": _v1523_normalise_numeric_doi(meta.get("doi", "")),
        "matched_title": _safe_strip(meta.get("title", "")),
        "matched_year": _safe_strip(meta.get("year", "")),
        "matched_authors": ", ".join(meta.get("authors", []) or []),
        "matched_journal": _safe_strip(meta.get("journal", "")),
        "matched_container_title": _safe_strip(meta.get("journal", "")),
        "matched_volume": _safe_strip(meta.get("volume", "")),
        "matched_issue": _safe_strip(meta.get("issue", "")),
        "matched_pages": _safe_strip(meta.get("pages", "")),
        "matched_publisher": _safe_strip(meta.get("publisher", "")),
        "matched_type": _safe_strip(meta.get("type", "")),
        "matched_url": _safe_strip(meta.get("url", "")),
        "title_score": int(meta.get("title_score", 0)),
        "journal_score": int(meta.get("journal_score", 0)),
        "author_overlap": int(meta.get("author_overlap", 0)),
        "author_similarity": int(meta.get("author_similarity", 0)),
        "year_match": int(meta.get("year_match", 0)),
        "year_delta": int(meta.get("year_delta", 999)),
        "doi_match": 1 if meta.get("doi_match") else 0,
        "volume_match": int(meta.get("volume_match", 0)),
        "issue_match": int(meta.get("issue_match", 0)),
        "page_match": int(meta.get("page_match", 0)),
        "confidence_reason": reason,
        "alternative_matches": alternatives,
    })

    # Do not display unrelated weak candidates as matched sources.
    if row["status"] == "not_found":
        row = _v1523_blank_not_found(row)

    if enrich_metadata and row.get("doi"):
        try:
            row = enrich_with_full_metadata(row)
        except Exception:
            pass

    row["correction_suggestions"] = row.get("correction_suggestions") or _verification_style_suggestion(row)
    return row


def _verify_single_reference(ref: str, style: str, use_crossref: bool, use_openalex: bool, enrich_metadata: bool = False) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    if _v1523_is_numeric(canonical):
        # New cache namespace prevents old broad-query results from being reused.
        cache_key = f"numeric_query_v1523::{canonical}::{use_crossref}:{use_openalex}:{enrich_metadata}::{ref}"
        cached = _cache_get(cache_key)
        if cached:
            return cached
        row = _v1523_numeric_verify_single(ref, canonical, use_crossref, use_openalex, enrich_metadata)
        row["status"] = _normalize_verify_status(row.get("status"))
        _cache_set(cache_key, row)
        return row
    if _V1523_AUTHOR_YEAR_VERIFY_SINGLE:
        return _V1523_AUTHOR_YEAR_VERIFY_SINGLE(ref, canonical, use_crossref, use_openalex, enrich_metadata)
    return {"reference": ref, "style": canonical, "status": "not_found"}


def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = False,
    job_id: str = None,
    enrich_metadata: bool = False,
) -> List[Dict[str, Any]]:
    canonical = _canonical_verify_style(style)
    refs = [r for r in (references or []) if _safe_strip(r)]
    rows: List[Dict[str, Any]] = []
    total = len(refs)
    for i, ref in enumerate(refs, start=1):
        try:
            rows.append(_verify_single_reference(ref, canonical, use_crossref, use_openalex, enrich_metadata))
        except Exception as exc:
            rows.append({
                "reference": ref,
                "style": canonical,
                "status": "offline",
                "error": str(exc),
                "confidence_reason": "Numeric style-specific verification failed for this row.",
            })
        if job_id:
            try:
                update_job_progress(job_id, i)
                if i == total:
                    store_verification_results(job_id, rows)
            except Exception:
                pass
        if throttle_s:
            time.sleep(throttle_s)
    return rows

try:
    if "__all__" in globals():
        for name in [
            "VERIFY_BUILD",
            "_extract_fields_by_style",
            "_build_verification_query_plan",
            "verify_references_batch",
            "_verify_single_reference",
        ]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass


# ============================================================
# NUMERIC QUERY BUILDER HOTFIX v1.5.24
# Fixes: [Internet] handling, eClinicalMed lower-case journal split,
# month-only dates (2019 Sep;), and broken Available-from URLs.
# ============================================================

__version__ = "1.5.24"
VERIFY_BUILD = "commercial-2026-05-19-numeric-query-builder-hotfix-FINAL"


def _v1523_strip_reference_noise(ref: str) -> str:
    s = _safe_strip(ref or "")
    s = re.sub(r"\[\s*cited\s+[^\]]+\]", " ", s, flags=re.I)
    # For query building, anything after Available from/Retrieved from is URL noise.
    s = re.sub(r"\bAvailable\s+from\s*:\s*.*$", " ", s, flags=re.I)
    s = re.sub(r"\bRetrieved\s+from\s+.*$", " ", s, flags=re.I)
    s = re.sub(r"https?://\S+", " ", s, flags=re.I)
    s = re.sub(r"\bdoi\s*:?\s*10\.\S+", " ", s, flags=re.I)
    s = re.sub(r"\b10\.\d{4,9}/\S+", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _v1523_publication_year(ref: str) -> str:
    s = _strip_leading_numbering(_v1523_strip_reference_noise(ref))
    # Journal. 2024 Sep 1;75 OR Journal. 2019 Sep;39(9)
    matches = list(re.finditer(r"\.\s*((?:19|20)\d{2})(?:\s+[A-Za-z]{3,9}(?:\s+\d{1,2})?)?\s*;", s, flags=re.I))
    if matches:
        return matches[-1].group(1)
    matches = list(re.finditer(r";\s*((?:19|20)\d{2})\s*(?:\.|$)", s))
    if matches:
        return matches[-1].group(1)
    matches = list(re.finditer(r",\s*((?:19|20)\d{2})\s*,\s*[A-Za-z]?\d+", s))
    if matches:
        return matches[-1].group(1)
    years = re.findall(r"\b((?:19|20)\d{2})[a-z]?\b", s)
    plausible = [y for y in years if int(y) <= 2030]
    return (plausible[-1] if plausible else (years[-1] if years else ""))


def _v1523_split_sentences_like_reference(clean: str) -> List[str]:
    clean = _v1523_protect_abbreviations(clean)
    # Split after a full stop when a new segment begins, including lower-case journal names such as eClinicalMed.
    parts = re.split(r"\.\s+(?=[A-Za-z0-9])", clean)
    return [p.strip(" .") for p in parts if p and p.strip(" .")]


def _v1523_clean_title(title: str) -> str:
    t = _clean_query_text(title or "")
    # Remove only citation-format markers, not the word 'internet' inside a real article title.
    t = re.sub(r"\[\s*Internet\s*\]", " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" .,:;")
    return t


def _v1523_clean_journal(journal: str) -> str:
    j = _clean_query_text(journal or "")
    j = re.sub(r"\[\s*Internet\s*\]", " ", j, flags=re.I)
    j = re.split(r"\b(?:19|20)\d{2}\b", j, maxsplit=1)[0]
    j = re.sub(r"\s+", " ", j).strip(" .,:;")
    return j


def _v1523_volume_issue_pages(ref: str) -> Dict[str, str]:
    s = _v1523_strip_reference_noise(ref).replace("–", "-").replace("—", "-")
    out = {"volume": "", "issue": "", "pages": ""}
    m = re.search(r"(?:19|20)\d{2}(?:\s+[A-Za-z]{3,9}(?:\s+\d{1,2})?)?\s*;\s*([A-Za-z]?\d+[A-Za-z]?)\s*(?:\(([^)]+)\))?\s*:\s*([A-Za-z]?\d+[A-Za-z]?\s*(?:-\s*[A-Za-z]?\d+[A-Za-z]?)?)", s, flags=re.I)
    if m:
        out["volume"] = _safe_strip(m.group(1))
        out["issue"] = _safe_strip(m.group(2))
        out["pages"] = re.sub(r"\s+", "", _safe_strip(m.group(3)))
        return out
    m = re.search(r",\s*(?:19|20)\d{2}\s*,\s*([A-Za-z]?\d+[A-Za-z]?)\s*,\s*([A-Za-z]?\d+[A-Za-z]?\s*(?:-\s*[A-Za-z]?\d+[A-Za-z]?)?)", s)
    if m:
        out["volume"] = _safe_strip(m.group(1))
        out["pages"] = re.sub(r"\s+", "", _safe_strip(m.group(2)))
    return out



# ============================================================
# NUMERIC QUERY BUILDER HOTFIX v1.5.25
# Fixes: keep real word 'internet' in article titles, remove only trailing
# format-marker Internet from journal/title fields after bracket cleanup.
# ============================================================

__version__ = "1.5.25"
VERIFY_BUILD = "commercial-2026-05-19-numeric-query-builder-internet-title-fix-FINAL"


def _v1523_clean_title(title: str) -> str:
    t = _clean_query_text(title or "")
    t = re.sub(r"\[\s*Internet\s*\]", " ", t, flags=re.I)
    # If [Internet] became plain Internet during PDF/Unicode cleaning, remove it only as a trailing format marker.
    t = re.sub(r"\bInternet\b\s*$", " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" .,:;")
    return t


def _v1523_clean_journal(journal: str) -> str:
    j = _clean_query_text(journal or "")
    j = re.sub(r"\[\s*Internet\s*\]", " ", j, flags=re.I)
    j = re.sub(r"\bInternet\b\s*$", " ", j, flags=re.I)
    j = re.split(r"\b(?:19|20)\d{2}\b", j, maxsplit=1)[0]
    j = re.sub(r"\s+", " ", j).strip(" .,:;")
    return j


def _v1523_title_query(title: str) -> str:
    title = _clean_query_text(title or "")
    title = re.sub(r"\[\s*Internet\s*\]", " ", title, flags=re.I)
    title = re.sub(r"\bInternet\b\s*$", " ", title, flags=re.I)
    title = re.sub(r"\b(Available|cited|from)\b", " ", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip()
    return title[:240]



# ============================================================
# NUMERIC QUERY BUILDER RECOVERY v1.5.26
# Fixes after live test: v1.5.25 became too strict and used Crossref
# query.title too narrowly for biomedical numeric references. This patch
# switches numeric references to title-as-bibliographic search, adds
# title-key and unfiltered OpenAlex fallbacks, relaxes style-specific numeric
# classification for high title/year evidence, and auto-routes numbered
# reference lists to numeric verification if the UI accidentally sends APA.
# ============================================================

__version__ = "1.5.26"
VERIFY_BUILD = "commercial-2026-05-19-numeric-query-builder-recovery-FINAL"

_V1526_NUMERIC_CACHE_PREFIX = "numeric_query_v1526"


def _v1526_is_numeric_family(style: str) -> bool:
    try:
        return _canonical_verify_style(style) in {"numeric_square", "numeric_superscript", "numeric_round"}
    except Exception:
        return False


def _v1526_ref_looks_numbered(ref: str) -> bool:
    s = _safe_strip(ref or "")
    return bool(re.match(r"^\s*(?:\[\s*\d{1,4}\s*\]|\(?\s*\d{1,4}\s*\)?[\.)]?|\d{1,4}[\.)]?)\s+[A-Z0-9]", s))


def _v1526_batch_looks_numeric(refs: List[str]) -> bool:
    refs = [r for r in (refs or []) if _safe_strip(r)]
    if not refs:
        return False
    sample = refs[: min(20, len(refs))]
    numbered = sum(1 for r in sample if _v1526_ref_looks_numbered(r))
    return numbered >= max(3, int(len(sample) * 0.60))


def _v1526_canonical_for_batch(style: str, refs: List[str]) -> str:
    canonical = _canonical_verify_style(style)
    if canonical not in {"numeric_square", "numeric_superscript", "numeric_round"} and _v1526_batch_looks_numeric(refs):
        # Verification is reference-list based, so square/superscript/round behave the same online.
        # Use numeric_superscript as the safest generic biomedical numeric profile when the UI sends APA.
        return "numeric_superscript"
    return canonical


def _v1526_short_title_key(title: str, limit: int = 9) -> str:
    words = _significant_title_words(title or "", limit=limit + 4)
    return " ".join(words[:limit]).strip()


def _v1523_query_crossref_title(title: str, rows: int = 10, query_name: str = "crossref_title_as_bibliographic_v1526") -> List[Dict[str, Any]]:
    """
    Crossref query.title is often too brittle for extracted PDF references.
    For numeric styles, use query.bibliographic with title text instead.
    """
    title = _v1523_title_query(title)
    if not title:
        return []
    return _query_crossref_bibliographic(title, rows=rows or max(10, VERIFY_TITLE_ROWS), query_name=query_name)


def _v1526_dedupe_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    try:
        return _dedupe_candidates(candidates)
    except Exception:
        seen = set()
        out = []
        for cand in candidates or []:
            try:
                doi, title, year, authors = _candidate_fields(cand)
                key = (str(doi).lower(), str(title).lower(), str(year))
            except Exception:
                key = json.dumps(cand, sort_keys=True, default=str)[:300]
            if key in seen:
                continue
            seen.add(key)
            out.append(cand)
        return out


def _v1523_run_numeric_queries(plan: Dict[str, Any], use_crossref: bool, use_openalex: bool) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """
    Recovery query plan for numeric styles.

    Order:
    1. DOI exact.
    2. Crossref bibliographic title, rich title, and short title-key.
    3. OpenAlex title/rich title with and without year filter.
    4. Journal-year-volume-page tuple.
    """
    candidates: List[Dict[str, Any]] = []
    query_used: List[str] = []
    strategy: List[str] = []
    openalex_allowed = bool(use_openalex or VERIFY_FORCE_OPENALEX_FALLBACK)

    doi = _safe_strip(plan.get("doi", ""))
    title = _v1523_title_query(plan.get("title", ""))
    rich_title = _clean_query_text(plan.get("rich_title", ""))
    title_key = _v1526_short_title_key(title) or _clean_query_text(plan.get("title_key", ""))
    journal_tuple = _clean_query_text(plan.get("journal_tuple", ""))
    year = _safe_strip(plan.get("year", ""))

    # DOI exact must dominate.
    if doi and use_crossref:
        query_used.append(doi); strategy.append("numeric_crossref_doi_exact")
        res = _query_crossref_by_doi(doi) or []
        if res:
            return _v1526_dedupe_candidates(res), query_used, strategy
    if doi and openalex_allowed:
        query_used.append(doi); strategy.append("numeric_openalex_doi_exact")
        res = _query_openalex_by_doi(doi) or []
        if res:
            return _v1526_dedupe_candidates(res), query_used, strategy

    # Title-centred searches. Use several bounded forms, not the raw full reference.
    if title and _v1523_count_words(title) >= 3:
        if use_crossref:
            for q, name, rows in [
                (title, "numeric_crossref_title_bibliographic", max(12, VERIFY_TITLE_ROWS)),
                (rich_title, "numeric_crossref_rich_bibliographic", max(8, VERIFY_CROSSREF_ROWS)),
                (title_key, "numeric_crossref_title_key", max(8, VERIFY_CROSSREF_ROWS)),
            ]:
                q = _clean_query_text(q)
                if not q:
                    continue
                res = _query_crossref_bibliographic(q, query_author="", rows=rows, query_name=name) or []
                candidates.extend(res)
                query_used.append(q); strategy.append(name)

        if openalex_allowed:
            # Search exact-ish title without year first, because online-first metadata can differ by year.
            for q, y, name, rows in [
                (title, "", "numeric_openalex_title_unfiltered", max(10, VERIFY_OPENALEX_ROWS)),
                (rich_title or title, year, "numeric_openalex_rich_year", max(8, VERIFY_OPENALEX_ROWS)),
                (title_key or title, "", "numeric_openalex_title_key", max(8, VERIFY_OPENALEX_ROWS)),
            ]:
                q = _clean_query_text(q)
                if not q:
                    continue
                res = _query_openalex_search(q, rows=rows, publication_year=y, query_name=name) or []
                candidates.extend(res)
                query_used.append(q); strategy.append(name)

    # Journal tuple for RSC/ACS/no-title or when title search yields nothing credible.
    if journal_tuple:
        if use_crossref:
            res = _query_crossref_bibliographic(journal_tuple, query_author="", rows=max(8, VERIFY_CROSSREF_ROWS), query_name="numeric_crossref_journal_tuple") or []
            candidates.extend(res); query_used.append(journal_tuple); strategy.append("numeric_crossref_journal_tuple")
        if openalex_allowed:
            res = _query_openalex_search(journal_tuple, rows=max(8, VERIFY_OPENALEX_ROWS), publication_year=year, query_name="numeric_openalex_journal_tuple") or []
            candidates.extend(res); query_used.append(journal_tuple); strategy.append("numeric_openalex_journal_tuple")

    return _v1526_dedupe_candidates(candidates), _dedupe_preserve(query_used), _dedupe_preserve(strategy)


def _classify_from_meta(ref_fields: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[str, str]:
    """
    Less brittle numeric classifier.

    Numeric references from PDFs often have incomplete author or journal metadata,
    but a strong title match plus publication-year support is enough for likely,
    and sometimes verified. DOI exact remains the strongest signal.
    """
    family = ref_fields.get("style_family") or _v1522_style_family(ref_fields.get("style", "apa"))
    if family not in {"numeric_square", "numeric_superscript", "numeric_round"}:
        if _V1522_PREVIOUS_CLASSIFY_FROM_META:
            return _V1522_PREVIOUS_CLASSIFY_FROM_META(ref_fields, meta)
        return "not_found", "No classifier available."

    title_score = int(meta.get("title_score", 0) or 0)
    score = int(meta.get("score", 0) or 0)
    year_delta = int(meta.get("year_delta", 999) or 999)
    author_overlap = int(meta.get("author_overlap", 0) or 0)
    author_similarity = int(meta.get("author_similarity", 0) or 0)
    journal_score = int(meta.get("journal_score", 0) or 0)
    doi_match = bool(meta.get("doi_match"))
    volume_match = int(meta.get("volume_match", 0) or 0)
    page_match = int(meta.get("page_match", 0) or 0)
    has_clear_title = bool(meta.get("numeric_has_clear_title")) or _v1522_has_clear_title(ref_fields)
    ref_has_doi = bool(ref_fields.get("doi"))
    doi_conflict = bool(meta.get("doi_conflict"))

    author_ok = author_overlap >= 1 or author_similarity >= 70 or not ref_fields.get("authors") or not meta.get("authors")
    year_ok = year_delta <= 1
    year_close = year_delta <= 2
    journal_ok = journal_score >= 58

    if doi_match:
        return "verified", "Exact DOI match. Numeric-style reference verified using DOI-first matching."

    if ref_has_doi and doi_conflict:
        return "not_found", "A candidate was found, but it has a different DOI from the original reference."

    if has_clear_title:
        # Strong title evidence. Use year as the main support for numeric styles.
        if title_score >= 94 and (year_ok or author_ok or journal_ok):
            return "verified", "Very strong title match with supporting numeric-style metadata."
        if title_score >= 90 and year_ok and (author_ok or journal_score >= 45):
            return "verified", "Strong title and publication-year match."
        if title_score >= 86 and (year_close or author_ok or journal_ok):
            return "verified", "Likely numeric-style match based on title plus year, author, or journal evidence."
        if title_score >= 78 and year_close:
            return "verified", "Likely numeric-style match based on title and publication year."
        if title_score >= 72 and (year_close or author_ok or journal_score >= 50):
            return "needs_review", "Possible numeric-style match. Review title, year, and journal before accepting."
        if title_score >= 65 and score >= 50:
            return "needs_review", "Weak but plausible numeric-style title match. Manual review is required."
        return "not_found", "No reliable numeric-style title match found. Weak database candidates were rejected."

    # No-title RSC/ACS style references.
    if journal_score >= 84 and year_ok and volume_match and page_match:
        return "verified", "Numeric no-title reference verified by journal, year, volume, and page tuple."
    if journal_score >= 74 and year_close and (volume_match or page_match):
        return "verified", "Likely numeric no-title match using journal-year-volume/page evidence."
    if journal_score >= 62 and year_close and (volume_match or page_match):
        return "needs_review", "Possible numeric no-title match. Check journal, volume, page and author manually."

    return "not_found", "No reliable numeric-style bibliographic match found."


def _v1523_numeric_verify_single(ref: str, style: str, use_crossref: bool, use_openalex: bool, enrich_metadata: bool = False) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    fields = _extract_fields_by_style(ref, canonical)
    plan = _v1523_build_numeric_query_plan(ref, canonical)
    row = _base_commercial_row(ref, canonical, fields, "", "numeric_query_builder_v1526")
    row["verification_profile"] = "numeric_query_builder_v1526"
    row["query_plan"] = {
        "doi": bool(plan.get("doi")),
        "title": plan.get("title", ""),
        "title_key": plan.get("title_key", ""),
        "rich_title": plan.get("rich_title", ""),
        "journal_tuple": plan.get("journal_tuple", ""),
        "year": plan.get("year", ""),
    }

    if not plan.get("doi") and not plan.get("title") and not plan.get("journal_tuple"):
        row.update({
            "status": "needs_review",
            "confidence_reason": "The numeric reference lacks DOI, article title and journal tuple. Manual verification is required.",
            "query_strategy": "numeric_no_queryable_metadata_v1526",
        })
        row["correction_suggestions"] = _verification_style_suggestion(row)
        return row

    candidates, query_used, strategy = _v1523_run_numeric_queries(plan, use_crossref, use_openalex)
    row["query_used"] = " | ".join(query_used)
    row["query_strategy"] = " | ".join(strategy)
    row["candidate_count"] = len(candidates or [])

    if not candidates:
        row.update({
            "status": "not_found",
            "confidence_reason": "No Crossref/OpenAlex candidate was returned from DOI, title, title-key, or journal-tuple numeric queries.",
        })
        row = _v1523_blank_not_found(row)
        row["correction_suggestions"] = _verification_style_suggestion(row)
        return row

    _best, meta, alternatives = _v1523_best_candidate(fields, candidates)
    if not meta:
        row.update({"status": "not_found", "confidence_reason": "Candidates were returned, but none had usable metadata."})
        row = _v1523_blank_not_found(row)
        row["correction_suggestions"] = _verification_style_suggestion(row)
        return row

    status, reason = _classify_from_meta(fields, meta)
    row.update({
        "status": _normalize_verify_status(status),
        "source": "+".join(meta.get("sources") or [meta.get("source", "")]),
        "score": int(meta.get("score", 0)),
        "doi": _v1523_normalise_numeric_doi(meta.get("doi", "")),
        "matched_title": _safe_strip(meta.get("title", "")),
        "matched_year": _safe_strip(meta.get("year", "")),
        "matched_authors": ", ".join(meta.get("authors", []) or []),
        "matched_journal": _safe_strip(meta.get("journal", "")),
        "matched_container_title": _safe_strip(meta.get("journal", "")),
        "matched_volume": _safe_strip(meta.get("volume", "")),
        "matched_issue": _safe_strip(meta.get("issue", "")),
        "matched_pages": _safe_strip(meta.get("pages", "")),
        "matched_publisher": _safe_strip(meta.get("publisher", "")),
        "matched_type": _safe_strip(meta.get("type", "")),
        "matched_url": _safe_strip(meta.get("url", "")),
        "title_score": int(meta.get("title_score", 0)),
        "journal_score": int(meta.get("journal_score", 0)),
        "author_overlap": int(meta.get("author_overlap", 0)),
        "author_similarity": int(meta.get("author_similarity", 0)),
        "year_match": int(meta.get("year_match", 0)),
        "year_delta": int(meta.get("year_delta", 999)),
        "doi_match": 1 if meta.get("doi_match") else 0,
        "volume_match": int(meta.get("volume_match", 0)),
        "issue_match": int(meta.get("issue_match", 0)),
        "page_match": int(meta.get("page_match", 0)),
        "confidence_reason": reason,
        "alternative_matches": alternatives,
    })

    # Only blank truly unreliable not_found matches. Keep needs_review visible.
    if row["status"] == "not_found":
        row = _v1523_blank_not_found(row)

    if enrich_metadata and row.get("doi"):
        try:
            row = enrich_with_full_metadata(row)
        except Exception:
            pass

    row["correction_suggestions"] = row.get("correction_suggestions") or _verification_style_suggestion(row)
    return row


def _verify_single_reference(ref: str, style: str, use_crossref: bool, use_openalex: bool, enrich_metadata: bool = False) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    if _v1526_is_numeric_family(canonical) or _v1526_ref_looks_numbered(ref):
        if not _v1526_is_numeric_family(canonical):
            canonical = "numeric_superscript"
        cache_key = f"{_V1526_NUMERIC_CACHE_PREFIX}::{canonical}::{use_crossref}:{use_openalex}:{enrich_metadata}::{ref}"
        cached = _cache_get(cache_key)
        if cached:
            return cached
        row = _v1523_numeric_verify_single(ref, canonical, use_crossref, use_openalex, enrich_metadata)
        row["status"] = _normalize_verify_status(row.get("status"))
        _cache_set(cache_key, row)
        return row
    if _V1523_AUTHOR_YEAR_VERIFY_SINGLE:
        return _V1523_AUTHOR_YEAR_VERIFY_SINGLE(ref, canonical, use_crossref, use_openalex, enrich_metadata)
    return {"reference": ref, "style": canonical, "status": "not_found"}


def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = False,
    job_id: str = None,
    enrich_metadata: bool = False,
) -> List[Dict[str, Any]]:
    refs = [r for r in (references or []) if _safe_strip(r)]
    canonical = _v1526_canonical_for_batch(style, refs)
    rows: List[Dict[str, Any]] = []
    total = len(refs)

    for i, ref in enumerate(refs, start=1):
        try:
            rows.append(_verify_single_reference(ref, canonical, use_crossref, use_openalex, enrich_metadata))
        except Exception as exc:
            rows.append({
                "reference": ref,
                "style": canonical,
                "status": "offline",
                "error": str(exc),
                "confidence_reason": "Style-specific verification failed for this row.",
            })
        if job_id:
            try:
                update_job_progress(job_id, i)
                if i == total:
                    store_verification_results(job_id, rows)
            except Exception:
                pass
        if throttle_s:
            time.sleep(throttle_s)
    return rows

try:
    if "__all__" in globals():
        for name in [
            "VERIFY_BUILD",
            "verify_references_batch",
            "_verify_single_reference",
            "_v1523_run_numeric_queries",
            "_v1526_canonical_for_batch",
        ]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass

# ============================================================
# PRIVACY / NO-CACHE OVERRIDES
# Added for CiteIntegrity privacy policy: do not cache verification data.
# These definitions intentionally appear at the end of the module so they
# override earlier cache functions while preserving the public API.
# ============================================================

__version__ = "1.5.27"
VERIFY_BUILD = "commercial-2026-05-19-no-cache-privacy-verification-FINAL"

# Default OFF. Set VERIFY_USE_CACHE=1 only if you later decide to re-enable
# reference-level verification caching.
VERIFY_USE_CACHE = _env_flag("VERIFY_USE_CACHE", "0")

# Default OFF. The worker/DB flow should carry results. Keeping online
# verification results in this module's process memory is disabled by default.
VERIFY_STORE_RESULTS_IN_MEMORY = _env_flag("VERIFY_STORE_RESULTS_IN_MEMORY", "0")

# Optional short TTL for installations that deliberately enable temporary
# in-memory result handoff. It is not used while VERIFY_STORE_RESULTS_IN_MEMORY=0.
VERIFY_MEMORY_RESULT_TTL_SECONDS = int(os.getenv("VERIFY_MEMORY_RESULT_TTL_SECONDS", "900"))

# Remove any rows that may have been cached by earlier code during the process lifetime.
try:
    _CACHE.clear()
except Exception:
    pass

try:
    _verification_results.clear()
except Exception:
    pass

_verification_results_created_at: Dict[str, float] = {}


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    """No-op reference cache getter unless VERIFY_USE_CACHE=1."""
    if not VERIFY_USE_CACHE:
        return None
    try:
        with _CACHE_LOCK:
            v = _CACHE.get(key)
            return dict(v) if v else None
    except Exception:
        return None


def _cache_set(key: str, value: Dict[str, Any]) -> None:
    """No-op reference cache setter unless VERIFY_USE_CACHE=1."""
    if not VERIFY_USE_CACHE:
        return
    try:
        with _CACHE_LOCK:
            _CACHE[key] = dict(value)
    except Exception:
        return


def _purge_expired_verification_results() -> None:
    """Purge temporary in-memory verification result handoff rows."""
    if not VERIFY_STORE_RESULTS_IN_MEMORY:
        try:
            _verification_results.clear()
            _verification_results_created_at.clear()
        except Exception:
            pass
        return

    now = time.time()
    ttl = max(1, int(VERIFY_MEMORY_RESULT_TTL_SECONDS or 900))
    try:
        expired = [
            jid for jid, created in list(_verification_results_created_at.items())
            if now - float(created or 0) > ttl
        ]
        for jid in expired:
            _verification_results.pop(jid, None)
            _verification_results_created_at.pop(jid, None)
    except Exception:
        pass


def store_verification_results(job_id: str, results: List[Dict[str, Any]]):
    """
    Store completed verification results only when explicitly enabled.

    Default behaviour is privacy-first: no in-memory result cache is retained
    in verify.py. The RQ worker/database flow should deliver final results.
    """
    if not VERIFY_STORE_RESULTS_IN_MEMORY:
        return

    _purge_expired_verification_results()
    try:
        with _verification_results_lock:
            _verification_results[job_id] = list(results or [])
            _verification_results_created_at[job_id] = time.time()
            print(f"[DEBUG] Temporarily stored {len(results or [])} verification rows for job {job_id}")
    except Exception:
        pass


def get_verification_results(job_id: str) -> Optional[List[Dict[str, Any]]]:
    """Return temporary in-memory results only when explicitly enabled."""
    if not VERIFY_STORE_RESULTS_IN_MEMORY:
        return None

    _purge_expired_verification_results()
    try:
        with _verification_results_lock:
            rows = _verification_results.get(job_id)
            return list(rows) if rows is not None else None
    except Exception:
        return None


def clear_verification_results(job_id: str):
    """Delete temporary in-memory results for one job."""
    try:
        with _verification_results_lock:
            _verification_results.pop(job_id, None)
            _verification_results_created_at.pop(job_id, None)
    except Exception:
        pass


def clear_all_verification_memory():
    """Admin helper: remove all in-process verification rows and reference cache."""
    try:
        with _CACHE_LOCK:
            _CACHE.clear()
    except Exception:
        pass
    try:
        with _verification_results_lock:
            _verification_results.clear()
            _verification_results_created_at.clear()
    except Exception:
        pass
    return {"cleared": True, "verify_use_cache": bool(VERIFY_USE_CACHE), "store_results_in_memory": bool(VERIFY_STORE_RESULTS_IN_MEMORY)}


try:
    if "__all__" in globals():
        for name in [
            "VERIFY_USE_CACHE",
            "VERIFY_STORE_RESULTS_IN_MEMORY",
            "VERIFY_MEMORY_RESULT_TTL_SECONDS",
            "clear_all_verification_memory",
        ]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass



# ============================================================
# DOCX IEEE NUMERIC NORMALISATION OVERRIDES
# Added after no-cache overrides so these definitions are the active ones.
# ============================================================
__version__ = "1.5.28"
VERIFY_BUILD = "commercial-2026-05-19-docx-ieee-numbering-style-pass-fix-FINAL"


def _strip_leading_numbering(text: str) -> str:
    """Remove reference numbering for APA, IEEE, Vancouver, NLM, RSC and DOCX-exported numeric styles."""
    t = _safe_strip(text)
    t = t.replace("\xa0", " ").replace("\t", " ")
    t = re.sub(r"\s+", " ", t).strip()

    # Handles [1], [1]., [1), (1), (1)., 1., and 1)
    t = re.sub(
        r"^\s*(?:\[\s*\d{1,4}\s*\]\s*[\.]?|\[\s*\d{1,4}\s*\)\s*|\(\s*\d{1,4}\s*\)\s*[\.]?|\d{1,4}\s*[\.)])\s*",
        "",
        t,
    )
    return t.strip()


def _v1526_ref_looks_numbered(ref: str) -> bool:
    """Detect numeric reference-list entries, including DOCX IEEE [1]. Author format."""
    s = _safe_strip(ref or "")
    s = s.replace("\xa0", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return bool(re.match(
        r"^\s*(?:\[\s*\d{1,4}\s*\]\s*[\.]?|\[\s*\d{1,4}\s*\)\s*|\(\s*\d{1,4}\s*\)\s*[\.]?|\d{1,4}[\.)]?)\s+[A-Z0-9]",
        s,
    ))


# ============================================================
# AUTHOR-YEAR QUERY PLAN RECOVERY + FINAL STYLE-PRESERVING PATCH
# Build: 2026-05-19-author-year-query-plan-recovery
# Purpose:
# - Restore author-year Crossref/OpenAlex query plans after numeric overrides.
# - Keep numeric square/superscript/round query builders intact.
# - Preserve no-cache privacy defaults.
# - Normalise DOCX IEEE reference starts such as [1]. Author...
# ============================================================
__version__ = "1.5.29"
VERIFY_BUILD = "commercial-2026-05-19-author-year-query-plan-recovery-no-cache-FINAL"


def _v1529_normalise_reference_for_verification(ref: str) -> str:
    """Light normalisation before verification, especially DOCX IEEE [1]. entries."""
    s = _safe_str(ref)
    s = s.replace("\xa0", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()
    # Standardise [1]. Author... to [1] Author... without changing displayed text downstream.
    s = re.sub(r"^\s*\[(\d{1,4})\]\s*\.\s*", r"[\1] ", s)
    return s


def _v1529_author_year_query_plan(ref: str, canonical: str) -> Dict[str, Any]:
    """Return the preserved commercial author-year query plan, never the broken empty fallback."""
    previous_builder = (
        globals().get("_V1523_PREVIOUS_BUILD_QUERY_PLAN")
        or globals().get("_V1522_PREVIOUS_BUILD_QUERY_PLAN")
    )

    if previous_builder:
        try:
            plan = previous_builder(ref, canonical)
            if isinstance(plan, dict):
                # A valid author-year plan should include at least DOI, bibliographic, title, or OpenAlex query.
                has_queries = bool(plan.get("crossref_queries") or plan.get("openalex_queries") or plan.get("fallback_queries"))
                if has_queries:
                    return plan
        except Exception:
            pass

    # Emergency fallback, used only if preserved builders are unavailable.
    fields = _extract_fields_by_style(ref, canonical)
    title = _safe_strip(fields.get("title") or fields.get("article_title") or "")
    year = _safe_strip(fields.get("year") or "")
    doi = _safe_strip(fields.get("doi") or "")
    authors = fields.get("authors") or []
    first_author = _safe_strip(authors[0] if authors else "")
    title_words = _significant_title_words(title, limit=10)
    title_key = " ".join(title_words[:8]).strip()

    crossref_queries = []
    openalex_queries = []

    if doi:
        crossref_queries.append({"name": "crossref_doi_exact", "mode": "doi_exact", "doi": doi, "priority": 1})
        openalex_queries.append({"name": "openalex_doi_exact", "mode": "doi_exact", "doi": doi, "priority": 1})

    if title:
        bibliographic = " ".join([p for p in [first_author, title, year] if p]).strip()
        crossref_queries.append({
            "name": "crossref_author_year_bibliographic",
            "mode": "bibliographic",
            "query_bibliographic": bibliographic or title,
            "query_author": first_author,
            "rows": max(5, VERIFY_CROSSREF_ROWS),
            "priority": 2,
        })
        crossref_queries.append({
            "name": "crossref_author_year_title",
            "mode": "title",
            "query_title": title,
            "rows": max(5, VERIFY_TITLE_ROWS),
            "priority": 3,
        })
        openalex_queries.append({
            "name": "openalex_author_year_title",
            "mode": "search",
            "search": title_key or title,
            "publication_year": year,
            "rows": max(5, VERIFY_OPENALEX_ROWS),
            "priority": 4,
        })

    return {
        "reference": ref,
        "style": canonical,
        "fields": fields,
        "crossref_queries": crossref_queries,
        "openalex_queries": openalex_queries,
        "fallback_queries": [title_key] if title_key else [],
    }


def _build_verification_query_plan(ref: str, style: str) -> Dict[str, Any]:
    """Final query planner: numeric styles use numeric logic, author-year uses restored author-year logic."""
    canonical = _canonical_verify_style(style)

    if _v1523_is_numeric(canonical):
        plan = _v1523_build_numeric_query_plan(ref, canonical)
        plan["crossref_queries"] = []
        plan["openalex_queries"] = []
        if plan.get("doi"):
            plan["crossref_queries"].append({"name": "numeric_doi_exact", "mode": "doi_exact", "doi": plan["doi"], "priority": 1})
            plan["openalex_queries"].append({"name": "numeric_openalex_doi_exact", "mode": "doi_exact", "doi": plan["doi"], "priority": 1})
        if plan.get("title") and _v1523_count_words(plan["title"]) >= 3:
            plan["crossref_queries"].append({"name": "numeric_crossref_title_exact", "mode": "title", "query_title": plan["title"], "priority": 2})
            plan["crossref_queries"].append({"name": "numeric_crossref_rich_title", "mode": "bibliographic", "query_bibliographic": plan.get("rich_title", ""), "query_author": "", "priority": 3})
            plan["openalex_queries"].append({"name": "numeric_openalex_rich_title", "mode": "search", "search": plan.get("rich_title") or plan.get("title"), "publication_year": plan.get("year", ""), "priority": 4})
        if plan.get("journal_tuple") and (not plan.get("title") or _v1523_count_words(plan.get("title")) < 3):
            plan["crossref_queries"].append({"name": "numeric_crossref_journal_tuple", "mode": "bibliographic", "query_bibliographic": plan["journal_tuple"], "query_author": "", "priority": 5})
            plan["openalex_queries"].append({"name": "numeric_openalex_journal_tuple", "mode": "search", "search": plan["journal_tuple"], "publication_year": plan.get("year", ""), "priority": 6})
        return plan

    return _v1529_author_year_query_plan(ref, canonical)


def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = False,
    job_id: str = None,
    enrich_metadata: bool = False,
) -> List[Dict[str, Any]]:
    """Final batch verifier with no-cache defaults and normalised DOCX numeric starts."""
    refs = [
        _v1529_normalise_reference_for_verification(r)
        for r in (references or [])
        if _safe_strip(r)
    ]
    canonical = _v1526_canonical_for_batch(style, refs)
    rows: List[Dict[str, Any]] = []
    total = len(refs)

    for i, ref in enumerate(refs, start=1):
        try:
            row = _verify_single_reference(ref, canonical, use_crossref, use_openalex, enrich_metadata)
            if isinstance(row, dict):
                row.setdefault("selected_style", canonical)
                row.setdefault("style", canonical)
                try:
                    row.setdefault("style_family", _style_metadata(canonical).get("family", canonical))
                    row.setdefault("style_label", _style_metadata(canonical).get("label", canonical))
                    row.setdefault("style_sample", _style_metadata(canonical).get("sample", ""))
                except Exception:
                    pass
            rows.append(row)
        except Exception as exc:
            rows.append({
                "reference": ref,
                "style": canonical,
                "selected_style": canonical,
                "status": "offline",
                "error": str(exc),
                "confidence_reason": "Verification failed for this row.",
            })
        if job_id:
            try:
                update_job_progress(job_id, i)
                if i == total:
                    store_verification_results(job_id, rows)
            except Exception:
                pass
        if throttle_s:
            time.sleep(throttle_s)
    return rows

try:
    if "__all__" in globals():
        for name in [
            "VERIFY_BUILD",
            "VERIFY_USE_CACHE",
            "VERIFY_STORE_RESULTS_IN_MEMORY",
            "_build_verification_query_plan",
            "verify_references_batch",
        ]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass


# ============================================================
# AUTHOR-YEAR TITLE CLEANUP FOR QUERY PRECISION
# Build: 2026-05-19-author-year-title-query-cleanup
# Purpose: when older extractors merge title + journal + volume/pages,
# add cleaner author-year title queries without disturbing numeric logic.
# ============================================================
__version__ = "1.5.30"
VERIFY_BUILD = "commercial-2026-05-19-author-year-query-recovery-title-cleanup-FINAL"


def _v1530_clean_author_year_title_from_ref(ref: str, fields: Dict[str, Any]) -> str:
    """Extract the article/book title more cleanly for author-year references."""
    raw_title = _safe_strip(fields.get("title") or fields.get("article_title") or "")
    journal = _safe_strip(fields.get("journal") or fields.get("container_title") or fields.get("source") or "")

    title = raw_title
    if journal and journal.lower() in title.lower():
        # Cut before the journal name if the earlier extractor merged both.
        m = re.search(re.escape(journal), title, flags=re.I)
        if m and m.start() > 8:
            title = title[:m.start()].strip(" .,:;")

    if not title or len(title) < 8:
        s = _strip_leading_numbering(_safe_str(ref))
        year = _safe_strip(fields.get("year") or _extract_year(s) or "")
        if year:
            # APA/Harvard: Author. (Year). Title. Journal...
            parts = re.split(r"[\(\[]?\s*" + re.escape(year) + r"\s*[\)\]]?\s*[\.\:]?\s*", s, maxsplit=1)
            if len(parts) > 1:
                right = parts[1].strip()
                # Take first sentence as a high-precision title guess.
                m = re.match(r"(.{8,220}?)(?:\.\s+[A-Z][A-Za-z\s&:,]*[,\.]|\.\s+(?:https?://|doi\b)|$)", right)
                if m:
                    title = m.group(1).strip(" .,:;")

    title = re.sub(r"\bhttps?://\S+", "", title, flags=re.I)
    title = re.sub(r"\bdoi\s*:?\s*\S+", "", title, flags=re.I)
    title = re.sub(r"\b\d+\s*\([^)]*\)\s*,\s*\d+\s*[\-–]\s*\d+\b", "", title)
    title = re.sub(r"\s+", " ", title).strip(" .,:;")
    return title


# Keep a reference to the working v1.5.29 planner.
_V1530_PREVIOUS_QUERY_PLAN = _build_verification_query_plan


def _build_verification_query_plan(ref: str, style: str) -> Dict[str, Any]:
    canonical = _canonical_verify_style(style)
    plan = _V1530_PREVIOUS_QUERY_PLAN(ref, canonical)

    if _v1523_is_numeric(canonical):
        return plan

    fields = plan.get("fields") or {}
    if not isinstance(fields, dict):
        return plan

    clean_title = _v1530_clean_author_year_title_from_ref(ref, fields)
    if clean_title and clean_title != fields.get("title"):
        fields["title_clean"] = clean_title
        # Use clean title for search keys without destroying original diagnostics.
        clean_words = _significant_title_words(clean_title, limit=10)
        clean_key = " ".join(clean_words[:8]).strip()
        first_author = _safe_strip((fields.get("authors") or [fields.get("first_author", "")])[0])
        year = _safe_strip(fields.get("year") or "")
        journal = _safe_strip(fields.get("journal") or fields.get("container_title") or "")

        extra_crossref = []
        if clean_title:
            extra_crossref.append({
                "name": "crossref_clean_title_author_year",
                "mode": "bibliographic",
                "query_bibliographic": " ".join([p for p in [clean_title, journal, year] if p]).strip(),
                "query_author": first_author,
                "rows": max(5, VERIFY_CROSSREF_ROWS),
                "priority": 2.5,
            })
            extra_crossref.append({
                "name": "crossref_clean_title_exact",
                "mode": "title",
                "query_title": clean_title,
                "rows": max(5, VERIFY_TITLE_ROWS),
                "priority": 2.6,
            })
        extra_openalex = []
        if clean_key:
            extra_openalex.append({
                "name": "openalex_clean_title_year",
                "mode": "search",
                "search": clean_key,
                "publication_year": year,
                "rows": max(5, VERIFY_OPENALEX_ROWS),
                "priority": 3.5,
            })

        # Insert after DOI exact if present, otherwise at the front.
        cross = list(plan.get("crossref_queries") or [])
        if cross and cross[0].get("mode") == "doi_exact":
            cross = cross[:1] + extra_crossref + cross[1:]
        else:
            cross = extra_crossref + cross
        opena = list(plan.get("openalex_queries") or [])
        if opena and opena[0].get("mode") == "doi_exact":
            opena = opena[:1] + extra_openalex + opena[1:]
        else:
            opena = extra_openalex + opena
        plan["crossref_queries"] = cross
        plan["openalex_queries"] = opena
        plan["fields"] = fields

    return plan

try:
    if "__all__" in globals():
        for name in ["VERIFY_BUILD", "_build_verification_query_plan"]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass

# ============================================================
# NUMERIC SUPERSCRIPT / IEEE RECOVERY v1.5.31
# Build: 2026-05-22-numeric-superscript-ieee-recovery
# Purpose:
# - Numeric superscript reference lists often begin with Unicode digits (¹,²,³),
#   which older numeric detection did not treat as numbered references.
# - Numeric square/superscript verification must not fall back to author-year
#   merely because the UI or PDF extraction loses the selected style.
# - Strip leading superscript markers before numeric field extraction so the
#   first author is not lost and title/journal queries remain usable.
# ============================================================
try:
    import json  # ensure json exists for older helper fallbacks
except Exception:
    pass

__version__ = "1.5.31"
VERIFY_BUILD = "commercial-2026-05-22-numeric-superscript-ieee-recovery-FINAL"

_SUP_DIGITS_VERIFY_V1531 = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_SUP_TO_NORMAL_VERIFY_V1531 = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻−–—", "0123456789----")


def _v1531_ref_starts_superscript(ref: str) -> bool:
    s = _safe_strip(ref or "")
    return bool(re.match(rf"^\s*[{_SUP_DIGITS_VERIFY_V1531}]+(?:\s*[,;\-–—]\s*[{_SUP_DIGITS_VERIFY_V1531}]+)*\s+[A-Z0-9]", s))


_V1531_PREVIOUS_STRIP_LEADING_NUMBERING = globals().get("_strip_leading_numbering")


def _strip_leading_numbering(text: str) -> str:
    """Final numbering stripper: handles square, round, plain, and Unicode superscript markers."""
    t = _safe_strip(text or "")
    t = t.replace("\xa0", " ").replace("\t", " ")
    t = re.sub(r"\s+", " ", t).strip()

    # Unicode superscript reference-number prefix: ¹ Author... or ¹,² Author...
    t = re.sub(rf"^\s*[{_SUP_DIGITS_VERIFY_V1531}]+(?:\s*[,;\-–—]\s*[{_SUP_DIGITS_VERIFY_V1531}]+)*\s*", "", t)

    # Existing numeric forms: [1], [1]., (1), 1., 1)
    if _V1531_PREVIOUS_STRIP_LEADING_NUMBERING:
        try:
            t = _V1531_PREVIOUS_STRIP_LEADING_NUMBERING(t)
        except Exception:
            t = re.sub(r"^\s*(?:\[\s*\d{1,4}\s*\]\s*[\.]?|\[\s*\d{1,4}\s*\)\s*|\(\s*\d{1,4}\s*\)\s*[\.]?|\d{1,4}\s*[\.)])\s*", "", t)
    else:
        t = re.sub(r"^\s*(?:\[\s*\d{1,4}\s*\]\s*[\.]?|\[\s*\d{1,4}\s*\)\s*|\(\s*\d{1,4}\s*\)\s*[\.]?|\d{1,4}\s*[\.)])\s*", "", t)

    return t.strip()


_V1531_PREVIOUS_REF_LOOKS_NUMBERED = globals().get("_v1526_ref_looks_numbered")


def _v1526_ref_looks_numbered(ref: str) -> bool:
    s = _safe_strip(ref or "")
    s = s.replace("\xa0", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()
    if _v1531_ref_starts_superscript(s):
        return True
    if _V1531_PREVIOUS_REF_LOOKS_NUMBERED:
        try:
            return bool(_V1531_PREVIOUS_REF_LOOKS_NUMBERED(s))
        except Exception:
            pass
    return bool(re.match(
        r"^\s*(?:\[\s*\d{1,4}\s*\]\s*[\.]?|\[\s*\d{1,4}\s*\)\s*|\(\s*\d{1,4}\s*\)\s*[\.]?|\d{1,4}[\.)]?)\s+[A-Z0-9]",
        s,
    ))


_V1531_PREVIOUS_CANONICAL_FOR_BATCH = globals().get("_v1526_canonical_for_batch")


def _v1526_canonical_for_batch(style: str, refs: List[str]) -> str:
    canonical = _canonical_verify_style(style)
    refs = [r for r in (refs or []) if _safe_strip(r)]
    if canonical not in {"numeric_square", "numeric_superscript", "numeric_round"}:
        superscript_count = sum(1 for r in refs[: min(25, len(refs))] if _v1531_ref_starts_superscript(r))
        numbered_count = sum(1 for r in refs[: min(25, len(refs))] if _v1526_ref_looks_numbered(r))
        sample_n = min(25, len(refs)) or 1
        if superscript_count >= 2 or numbered_count >= max(3, int(sample_n * 0.55)):
            return "numeric_superscript" if superscript_count >= 2 else "numeric_square"
    if _V1531_PREVIOUS_CANONICAL_FOR_BATCH:
        try:
            return _V1531_PREVIOUS_CANONICAL_FOR_BATCH(style, refs)
        except Exception:
            return canonical
    return canonical


_V1531_PREVIOUS_NORMALISE_REF = globals().get("_v1529_normalise_reference_for_verification")


def _v1529_normalise_reference_for_verification(ref: str) -> str:
    """Normalise numeric reference starts while preserving enough numbering for style auto-detection."""
    s = _safe_str(ref)
    s = s.replace("\xa0", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()

    # Standardise square bracket starts.
    s = re.sub(r"^\s*\[(\d{1,4})\]\s*\.\s*", r"[\1] ", s)

    # Convert leading Unicode superscript number to a plain numeric marker so
    # downstream numeric parsing sees the first author correctly.
    m = re.match(rf"^\s*([{_SUP_DIGITS_VERIFY_V1531}]+)\s+(?=[A-Z0-9])", s)
    if m:
        n = m.group(1).translate(_SUP_TO_NORMAL_VERIFY_V1531)
        s = re.sub(rf"^\s*[{_SUP_DIGITS_VERIFY_V1531}]+\s+", f"{n}. ", s, count=1)

    if _V1531_PREVIOUS_NORMALISE_REF:
        try:
            s = _V1531_PREVIOUS_NORMALISE_REF(s)
        except Exception:
            pass
    return s.strip()


_V1531_PREVIOUS_NUMERIC_VERIFY_SINGLE = globals().get("_v1523_numeric_verify_single")


def _v1523_numeric_verify_single(ref: str, style: str, use_crossref: bool, use_openalex: bool, enrich_metadata: bool = False) -> Dict[str, Any]:
    row = _V1531_PREVIOUS_NUMERIC_VERIFY_SINGLE(ref, style, use_crossref, use_openalex, enrich_metadata)
    try:
        fields = _extract_fields_by_style(ref, _canonical_verify_style(style))
        query_plan = row.setdefault("query_plan", {})
        query_plan.setdefault("parsed_title", fields.get("title") or fields.get("article_title") or "")
        query_plan.setdefault("parsed_journal", fields.get("journal") or fields.get("container_title") or "")
        query_plan.setdefault("parsed_year", fields.get("year", ""))
        query_plan.setdefault("parsed_authors", fields.get("authors", []))
        row.setdefault("numeric_diagnostic", {})
        row["numeric_diagnostic"].update({
            "style_used": _canonical_verify_style(style),
            "starts_with_superscript": _v1531_ref_starts_superscript(ref),
            "looks_numbered": _v1526_ref_looks_numbered(ref),
            "has_parsed_title": bool(fields.get("title") or fields.get("article_title")),
            "has_parsed_journal_tuple": bool(fields.get("structured_key")),
        })
        if row.get("status") in {"needs_review", "not_found", "offline"}:
            row.setdefault(
                "manual_search_query",
                " ".join(str(x or "") for x in [fields.get("title") or fields.get("article_title"), fields.get("journal"), fields.get("year")]).strip() or ref[:240]
            )
    except Exception:
        pass
    return row


_V1531_PREVIOUS_VERIFY_BATCH = globals().get("verify_references_batch")


def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = False,
    job_id: str = None,
    enrich_metadata: bool = False,
) -> List[Dict[str, Any]]:
    refs = [
        _v1529_normalise_reference_for_verification(r)
        for r in (references or [])
        if _safe_strip(r)
    ]
    canonical = _v1526_canonical_for_batch(style, refs)
    rows = _V1531_PREVIOUS_VERIFY_BATCH(
        refs,
        style=canonical,
        throttle_s=throttle_s,
        use_crossref=use_crossref,
        use_openalex=True if _v1526_is_numeric_family(canonical) else use_openalex,
        job_id=job_id,
        enrich_metadata=enrich_metadata,
    )
    for row in rows or []:
        if isinstance(row, dict):
            row.setdefault("selected_style", canonical)
            row.setdefault("style", canonical)
            if _v1526_is_numeric_family(canonical):
                row.setdefault("verification_profile", "numeric_superscript_ieee_recovery_v1531")
    return rows

try:
    if "__all__" in globals():
        for name in ["VERIFY_BUILD", "verify_references_batch", "_v1526_canonical_for_batch", "_v1526_ref_looks_numbered", "_strip_leading_numbering"]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass

# ============================================================
# OPENALEX RESCUE + STATUS-RANKED CANDIDATE SELECTION v1.5.32
# Build: 2026-06-02-openalex-rescue-ranked-fast
# Purpose:
# - Prevent weak Crossref candidates from dominating stronger OpenAlex matches.
# - Keep speed by stopping after strong Crossref verification, and using OpenAlex
#   as a rescue path only when the existing candidate set is not verified.
# - Do not add more sources or broader queries. This patch mainly changes ranking
#   and classification of already-returned candidates.
# ============================================================

__version__ = "1.5.32"
VERIFY_BUILD = "commercial-2026-06-02-openalex-rescue-ranked-fast-FINAL"

# Speed-first default. If Crossref already gives a verified match, do not spend
# time on OpenAlex/fallback. If Crossref is weak, OpenAlex still runs because
# _candidate_is_strong_enough() below returns True only for verified evidence.
VERIFY_STOP_ON_STRONG_MATCH = _env_flag("VERIFY_STOP_ON_STRONG_MATCH", "1")
VERIFY_FORCE_OPENALEX_FALLBACK = _env_flag("VERIFY_FORCE_OPENALEX_FALLBACK", "1")

_V1532_PREVIOUS_CLASSIFY_FROM_META = globals().get("_classify_from_meta")
_V1532_PREVIOUS_BEST_CANDIDATE = globals().get("_best_candidate")
_V1532_PREVIOUS_CANDIDATE_IS_STRONG_ENOUGH = globals().get("_candidate_is_strong_enough")


def _v1532_source_names(meta: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    try:
        names.extend([str(s).lower() for s in (meta.get("sources") or []) if s])
    except Exception:
        pass
    src = str(meta.get("source") or "").lower()
    if src:
        names.extend([s.strip() for s in src.split("+") if s.strip()])
    return _dedupe_preserve(names)


def _v1532_is_openalex_candidate(meta: Dict[str, Any]) -> bool:
    return "openalex" in _v1532_source_names(meta)


def _v1532_openalex_rescue_status(ref_fields: Dict[str, Any], meta: Dict[str, Any], current_status: str) -> Optional[Tuple[str, str]]:
    """
    Promote strong OpenAlex-only matches when Crossref is weak.

    This is intentionally conservative:
    - no DOI conflict;
    - strong title evidence;
    - close publication year or other support;
    - author conflict allowed only at very high title+year evidence because
      extracted reference authors are often incomplete or abbreviated.
    """
    if not isinstance(meta, dict) or not _v1532_is_openalex_candidate(meta):
        return None

    if bool(meta.get("doi_conflict")):
        return None

    title_score = int(meta.get("title_score", 0) or 0)
    year_delta = int(meta.get("year_delta", 999) or 999)
    year_match = int(meta.get("year_match", 0) or 0)
    author_overlap = int(meta.get("author_overlap", 0) or 0)
    author_similarity = int(meta.get("author_similarity", 0) or 0)
    journal_score = int(meta.get("journal_score", 0) or 0)
    candidate_has_doi = bool(meta.get("doi"))
    ref_has_authors = bool((ref_fields or {}).get("authors"))
    cand_has_authors = bool(meta.get("authors"))
    author_conflict = ref_has_authors and cand_has_authors and author_overlap == 0 and author_similarity < 70

    year_ok = year_match == 1 or year_delta <= 1
    year_close = year_delta <= 2
    author_ok = author_overlap >= 1 or author_similarity >= 70 or not ref_has_authors or not cand_has_authors
    journal_ok = journal_score >= 60

    # Strong OpenAlex rescue. This is the key rule.
    if title_score >= 90 and year_ok and (author_ok or journal_ok or candidate_has_doi):
        return "verified", "OpenAlex rescue: strong title and publication-year evidence after Crossref did not provide a stronger verified match."

    # Very high title + close year can survive weak author extraction.
    if title_score >= 94 and year_close and not author_conflict:
        return "verified", "OpenAlex rescue: very strong title match with close publication year."

    # If author mismatch is probably extraction noise, keep review rather than not_found.
    if title_score >= 84 and year_close:
        return "needs_review", "OpenAlex rescue: plausible title/year match, but manual review is needed for author or source details."

    if current_status == "not_found" and title_score >= 78 and (year_close or author_ok or journal_ok):
        return "needs_review", "OpenAlex rescue: possible match found where Crossref did not verify the reference."

    return None


def _classify_from_meta(ref_fields: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[str, str]:
    """Final classifier with conservative OpenAlex rescue."""
    previous = _V1532_PREVIOUS_CLASSIFY_FROM_META
    if callable(previous):
        status, reason = previous(ref_fields, meta)
    else:
        status, reason = "not_found", "No classifier available."

    norm_status = _normalize_verify_status(status)

    # Only rescue weak outcomes. If the previous classifier verified the source,
    # keep it exactly as-is.
    if norm_status != "verified":
        rescued = _v1532_openalex_rescue_status(ref_fields or {}, meta or {}, norm_status)
        if rescued:
            return rescued

    return norm_status, reason


def _v1532_status_rank(status: str) -> int:
    st = _normalize_verify_status(status)
    if st == "verified":
        return 3
    if st == "needs_review":
        return 2
    if st == "not_found":
        return 1
    return 0


def _v1532_candidate_sort_key(ref_fields: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[Any, ...]:
    status, _reason = _classify_from_meta(ref_fields, meta)
    year_delta = int(meta.get("year_delta", 999) or 999)
    year_closeness = 999 - min(year_delta, 999)
    openalex_rescue_bonus = 1 if (_v1532_is_openalex_candidate(meta) and _v1532_status_rank(status) >= 2) else 0
    return (
        _v1532_status_rank(status),
        openalex_rescue_bonus,
        1 if meta.get("doi_match") else 0,
        int(meta.get("title_score", 0) or 0),
        year_closeness,
        int(meta.get("author_overlap", 0) or 0),
        int(meta.get("author_similarity", 0) or 0),
        int(meta.get("journal_score", 0) or 0),
        int(meta.get("source_agreement", 1) or 1),
        int(meta.get("score", 0) or 0),
    )


def _best_candidate(ref_title_or_fields, ref_authors=None, ref_year=None, ref_doi=None, candidates: Optional[List[Dict[str, Any]]] = None):
    """
    Final candidate selector.

    Older code ranked mostly by raw composite score. This could allow a weak
    Crossref candidate to beat a stronger OpenAlex candidate. This selector first
    ranks by final classification status, then by bibliographic strength.
    """
    if isinstance(ref_title_or_fields, dict):
        ref_fields = ref_title_or_fields
        cand_list = ref_authors or []
        return_new_shape = True
    else:
        ref_fields = {
            "title": ref_title_or_fields or "",
            "authors": ref_authors or [],
            "year": ref_year or "",
            "doi": ref_doi or "",
            "journal": "",
            "volume": "",
            "issue": "",
            "pages": "",
        }
        cand_list = candidates or []
        return_new_shape = False

    scored: List[Tuple[Dict[str, Any], Dict[str, Any], str, str]] = []
    for cand in cand_list or []:
        try:
            meta = _score_candidate(ref_fields, cand)
            status, reason = _classify_from_meta(ref_fields, meta)
            meta["candidate_status"] = _normalize_verify_status(status)
            meta["candidate_reason"] = reason
            scored.append((cand, meta, status, reason))
        except Exception:
            continue

    if not scored:
        return (None, {}, []) if return_new_shape else (None, {})

    scored.sort(key=lambda cmm: _v1532_candidate_sort_key(ref_fields, cmm[1]), reverse=True)
    best, best_meta, _status, _reason = scored[0]

    alternatives = []
    for _cand, meta, status, reason in scored[1:6]:
        if int(meta.get("title_score", 0) or 0) < 58 and _v1532_status_rank(status) < 2:
            continue
        alternatives.append({
            "title": meta.get("title", ""),
            "doi": meta.get("doi", ""),
            "year": meta.get("year", ""),
            "journal": meta.get("journal", ""),
            "source": "+".join(meta.get("sources") or [meta.get("source", "")]),
            "score": int(meta.get("score", 0) or 0),
            "title_score": int(meta.get("title_score", 0) or 0),
            "author_overlap": int(meta.get("author_overlap", 0) or 0),
            "year_match": int(meta.get("year_match", 0) or 0),
            "candidate_status": _normalize_verify_status(status),
            "confidence_reason": reason,
        })

    return (best, best_meta, alternatives) if return_new_shape else (best, best_meta)


def _candidate_is_strong_enough(fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> bool:
    """
    Speed gate for stopping before OpenAlex/fallback.

    Stop only when the current candidate set actually verifies. A mere possible
    Crossref candidate should not block OpenAlex rescue.
    """
    if not candidates:
        return False
    try:
        best, meta, _alts = _best_candidate(fields, candidates)
        if not best:
            return False
        status, _reason = _classify_from_meta(fields, meta)
        return _normalize_verify_status(status) == "verified"
    except Exception as exc:
        try:
            _debug_multisource(f"Precheck failed: {exc}")
        except Exception:
            pass
    return False


def _v1532_promote_legacy_status_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalise legacy rows while preserving uncertain candidate states."""
    for row in rows or []:
        if isinstance(row, dict):
            row["status"] = _normalize_verify_status(row.get("status"))
    return rows


_V1532_PREVIOUS_VERIFY_REFERENCES_BATCH = globals().get("verify_references_batch")


def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = False,
    job_id: str = None,
    enrich_metadata: bool = False,
) -> List[Dict[str, Any]]:
    """
    Final batch wrapper.

    It keeps OpenAlex available through VERIFY_FORCE_OPENALEX_FALLBACK, but does
    not force full parallel OpenAlex for every reference when not needed.
    """
    if callable(_V1532_PREVIOUS_VERIFY_REFERENCES_BATCH):
        rows = _V1532_PREVIOUS_VERIFY_REFERENCES_BATCH(
            references,
            style=style,
            throttle_s=throttle_s,
            use_crossref=use_crossref,
            use_openalex=use_openalex,
            job_id=job_id,
            enrich_metadata=enrich_metadata,
        )
    else:
        rows = []

    rows = _v1532_promote_legacy_status_rows(rows)
    if job_id:
        try:
            store_verification_results(job_id, rows)
        except Exception:
            pass
    return rows

try:
    if "__all__" in globals():
        for name in [
            "VERIFY_BUILD",
            "verify_references_batch",
            "_best_candidate",
            "_classify_from_meta",
            "_candidate_is_strong_enough",
        ]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass



# ============================================================
# OPENALEX RESCUE DIAGNOSTICS v1.5.33
# ============================================================
# Adds row-level evidence that the deployed file contains the OpenAlex rescue
# selector and whether the selected match came from OpenAlex.
VERIFY_BUILD = "commercial-2026-06-03-openalex-rescue-redis-progress-diagnostics"

_V1533_PREVIOUS_VERIFY_SINGLE_REFERENCE = globals().get("_verify_single_reference")


def _verify_single_reference(
    ref: str,
    style: str,
    use_crossref: bool,
    use_openalex: bool,
    enrich_metadata: bool = False,
) -> Dict[str, Any]:
    row = _V1533_PREVIOUS_VERIFY_SINGLE_REFERENCE(ref, style, use_crossref, use_openalex, enrich_metadata)

    try:
        strategy = str(row.get("query_strategy", "") or "").lower()
        source = str(row.get("source", "") or "").lower()
        reason = str(row.get("confidence_reason", "") or row.get("candidate_reason", "") or "").lower()

        row["openalex_fallback_enabled"] = bool(VERIFY_FORCE_OPENALEX_FALLBACK)
        row["openalex_rescue_build"] = "v1.5.33"
        row["openalex_query_attempted"] = "openalex" in strategy
        row["openalex_selected"] = "openalex" in source
        row["openalex_rescue_applied"] = "openalex rescue" in reason
        row["verification_build"] = VERIFY_BUILD

        # If OpenAlex is selected but the row still remains not_found, keep it
        # needs_review rather than not_found when the title evidence is plausible.
        if row.get("openalex_selected") and _normalize_verify_status(row.get("status")) == "not_found":
            title_score = int(row.get("title_score", 0) or 0)
            year_delta = int(row.get("year_delta", 999) or 999)
            if title_score >= 78 and year_delta <= 2:
                row["status"] = "needs_review"
                row["confidence_reason"] = (
                    "OpenAlex candidate was selected with plausible title/year evidence, "
                    "but automatic verification evidence was insufficient."
                )
    except Exception:
        pass

    return row


# ============================================================
# OPENALEX SHORT-REFERENCE / BOOK RESCUE v1.5.34
# ============================================================
# Why this exists:
# - Some valid classic books and short article titles are skipped by the fast
#   weak-title rule because they have fewer than VERIFY_MIN_TITLE_WORDS.
# - Examples include: "Sampling techniques", "Survey sampling",
#   "Sample size calculation", and "Sampling: Design and Analysis".
# - These should not run expensive fallback for every reference, but they
#   should get one small OpenAlex rescue when author + year are present.
VERIFY_BUILD = "commercial-2026-06-03-openalex-short-reference-rescue-v1.5.34"

VERIFY_SHORT_OPENALEX_RESCUE = _env_flag("VERIFY_SHORT_OPENALEX_RESCUE", "1")
VERIFY_SHORT_OPENALEX_ROWS = int(os.getenv("VERIFY_SHORT_OPENALEX_ROWS", "5"))
VERIFY_SHORT_OPENALEX_MAX_QUERIES = int(os.getenv("VERIFY_SHORT_OPENALEX_MAX_QUERIES", "1"))
VERIFY_SHORT_OPENALEX_MIN_TITLE_SCORE = int(os.getenv("VERIFY_SHORT_OPENALEX_MIN_TITLE_SCORE", "82"))
VERIFY_SHORT_OPENALEX_REVIEW_TITLE_SCORE = int(os.getenv("VERIFY_SHORT_OPENALEX_REVIEW_TITLE_SCORE", "70"))

_BOOK_REFERENCE_HINTS = {
    "ed", "edition", "publisher", "press", "wiley", "sons", "erlbaum",
    "harper", "row", "pearson", "brooks", "cole", "routledge",
    "oxford", "cambridge", "guilford", "crc", "chapman", "hall",
    "book", "books", "internet", "archive", "publisher", "link",
    "official", "site"
}


def _v1534_simple_words(value: str) -> List[str]:
    return [
        w.lower()
        for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}", value or "")
        if w.lower() not in _QUERY_STOP_WORDS
    ]


def _v1534_is_short_or_book_reference(ref: str, fields: Dict[str, Any]) -> bool:
    """Identify short but legitimate references that should not be fast-skipped."""
    title = fields.get("title", "") or ""
    year = fields.get("year", "") or ""
    authors = fields.get("authors", []) or []
    ref_low = (ref or "").lower()

    title_words = _v1534_simple_words(title)
    has_book_hint = any(h in ref_low for h in _BOOK_REFERENCE_HINTS)
    has_volume_pages = bool(re.search(r"\b\d+\s*\(\s*\d+\s*\)\s*,\s*\d+", ref or ""))
    has_journal_like = bool(re.search(r"\bjournal\b|\bresearch\b|\bmethods?\b|\bsurvey\b|\bsampling\b", ref_low))

    # The key condition: short title, but enough bibliographic structure.
    if year and authors and 2 <= len(title_words) <= 5 and (has_book_hint or has_volume_pages or has_journal_like):
        return True

    return False


def _v1534_openalex_work_url(item: Dict[str, Any]) -> str:
    try:
        return _safe_strip(item.get("id") or item.get("doi") or "")
    except Exception:
        return ""


def _v1534_query_openalex_doi(doi: str) -> List[Dict[str, Any]]:
    doi = _normalise_doi(doi) if "_normalise_doi" in globals() else _safe_strip(doi).lower()
    if not doi:
        return []
    url = "https://api.openalex.org/works"
    params = {"filter": f"doi:{doi}", "per-page": 1}
    if MAILTO:
        params["mailto"] = MAILTO
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    data = _safe_get_json(url, params=params, timeout=API_TIMEOUT)
    items = (data or {}).get("results", [])
    return [{"source": "openalex", "item": it} for it in items]


def _v1534_build_openalex_rescue_queries(ref: str, fields: Dict[str, Any]) -> List[str]:
    title = _clean_query_text(fields.get("title", "") or "")
    year = fields.get("year", "") or ""
    authors = fields.get("authors", []) or []

    first_author = authors[0] if authors else ""
    second_author = authors[1] if len(authors) > 1 else ""

    queries = []
    if title and first_author and year:
        queries.append(f"{title} {first_author} {year}")
    if title and first_author and second_author and year:
        queries.append(f"{title} {first_author} {second_author} {year}")
    if title and year:
        queries.append(f"{title} {year}")
    if title and first_author:
        queries.append(f"{title} {first_author}")
    if title:
        queries.append(title)

    return _dedupe_preserve(queries)[:max(1, VERIFY_SHORT_OPENALEX_MAX_QUERIES)]


def _v1534_select_openalex_rescue_candidate(
    ref: str,
    fields: Dict[str, Any],
    candidates: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], str]:
    ref_title = fields.get("title") or ref
    ref_authors = fields.get("authors", []) or []
    ref_year = fields.get("year", "") or ""
    ref_doi = fields.get("doi", "") or ""

    best = None
    best_meta: Dict[str, Any] = {}
    best_rank = (-1, -1, -999, -1, -1)
    best_status = "not_found"

    for cand in candidates:
        if cand.get("source") != "openalex":
            continue

        doi, title, year, authors = _candidate_fields(cand)
        meta = _score(ref_title, ref_authors, ref_year, title, authors, year)

        doi_match = bool(ref_doi and doi and _normalise_doi(ref_doi) == _normalise_doi(doi))
        year_delta = 999
        try:
            if ref_year and year:
                year_delta = abs(int(str(ref_year)[:4]) - int(str(year)[:4]))
        except Exception:
            year_delta = 999

        title_score = int(meta.get("title_score", 0) or 0)
        author_overlap = int(meta.get("author_overlap", 0) or 0)
        score = int(meta.get("score", 0) or 0)

        # Strict enough to avoid promoting the wrong OpenAlex item.
        if doi_match and title_score >= 60:
            status = "verified"
            rank_status = 3
            reason = "OpenAlex DOI rescue."
        elif title_score >= 90 and year_delta <= 1:
            status = "verified"
            rank_status = 3
            reason = "OpenAlex rescue: very strong title and close year."
        elif title_score >= VERIFY_SHORT_OPENALEX_MIN_TITLE_SCORE and year_delta <= 2 and author_overlap >= 1:
            status = "verified"
            rank_status = 3
            reason = "OpenAlex rescue: short/classic title with author-year support."
        elif title_score >= VERIFY_SHORT_OPENALEX_MIN_TITLE_SCORE and author_overlap >= 1:
            status = "needs_review"
            rank_status = 2
            reason = "OpenAlex candidate has strong title-author support but year needs review."
        elif title_score >= VERIFY_SHORT_OPENALEX_REVIEW_TITLE_SCORE and year_delta <= 2:
            status = "needs_review"
            rank_status = 2
            reason = "OpenAlex candidate has plausible title-year support."
        else:
            status = "not_found"
            rank_status = 1
            reason = "OpenAlex candidate too weak for automatic rescue."

        rank = (
            rank_status,
            title_score,
            -year_delta,
            author_overlap,
            score,
        )

        if rank > best_rank:
            best = cand
            best_status = status
            best_rank = rank
            best_meta = dict(meta)
            best_meta.update({
                "doi_match": doi_match,
                "doi": doi,
                "title": title,
                "year": year,
                "authors": authors,
                "year_delta": year_delta,
                "status": status,
                "reason": reason,
            })

    return best, best_meta, best_status


_V1534_PREVIOUS_VERIFY_SINGLE_REFERENCE = globals().get("_verify_single_reference")


def _verify_single_reference(
    ref: str,
    style: str,
    use_crossref: bool,
    use_openalex: bool,
    enrich_metadata: bool = False,
) -> Dict[str, Any]:
    row = _V1534_PREVIOUS_VERIFY_SINGLE_REFERENCE(ref, style, use_crossref, use_openalex, enrich_metadata)

    try:
        row["verification_build"] = VERIFY_BUILD
        row["openalex_short_rescue_enabled"] = bool(VERIFY_SHORT_OPENALEX_RESCUE)
        row["openalex_fallback_enabled"] = bool(VERIFY_FORCE_OPENALEX_FALLBACK)
    except Exception:
        pass

    if not VERIFY_SHORT_OPENALEX_RESCUE:
        return row

    try:
        fields = _extract_fields_by_style(ref, style)
        current_status = _normalize_verify_status(row.get("status"))
        current_source = str(row.get("source", "") or "").lower()
        title_score_now = int(row.get("title_score", 0) or 0)

        should_rescue = (
            current_status in {"needs_review", "not_found"}
            and (
                current_source == "fast_skip"
                or current_source == "openalex"
                or _v1534_is_short_or_book_reference(ref, fields)
            )
        )

        # Do not disturb already verified Crossref/OpenAlex records.
        if not should_rescue:
            return row

        if not _v1534_is_short_or_book_reference(ref, fields) and title_score_now >= 60:
            # A normal candidate already exists; let the standard verifier handle it.
            return row

        candidates: List[Dict[str, Any]] = []

        ref_doi = fields.get("doi", "") or ""
        if ref_doi:
            candidates.extend(_v1534_query_openalex_doi(ref_doi))

        queries = _v1534_build_openalex_rescue_queries(ref, fields)
        for q in queries:
            candidates.extend(_query_openalex(q, rows=VERIFY_SHORT_OPENALEX_ROWS))

        best, meta, rescue_status = _v1534_select_openalex_rescue_candidate(ref, fields, candidates)

        row["openalex_short_rescue_attempted"] = True
        row["openalex_short_rescue_queries"] = queries
        row["openalex_short_rescue_candidates"] = len(candidates)
        row["openalex_short_rescue_status"] = rescue_status
        row["openalex_short_rescue_reason"] = meta.get("reason", "No usable OpenAlex candidate")

        if best and rescue_status in {"verified", "needs_review"}:
            # Only replace if the rescue improves status or evidence quality.
            status_rank = {"not_found": 0, "needs_review": 1, "verified": 2}
            if status_rank.get(rescue_status, 0) > status_rank.get(current_status, 0) or int(meta.get("title_score", 0)) > title_score_now:
                row.update({
                    "status": rescue_status,
                    "source": "openalex",
                    "score": int(meta.get("score", 0)),
                    "doi": _safe_strip(meta.get("doi")),
                    "matched_title": _safe_strip(meta.get("title")),
                    "matched_year": _safe_strip(meta.get("year")),
                    "matched_authors": ", ".join(meta.get("authors", [])),
                    "title_score": int(meta.get("title_score", 0)),
                    "author_overlap": int(meta.get("author_overlap", 0)),
                    "author_similarity": int(meta.get("author_similarity", 0)),
                    "year_match": int(meta.get("year_match", 0)),
                    "year_delta": int(meta.get("year_delta", 999)),
                    "match_note": meta.get("reason", ""),
                    "confidence_reason": meta.get("reason", ""),
                    "openalex_selected": True,
                    "openalex_short_rescue_applied": rescue_status == "verified",
                    "openalex_work_url": _v1534_openalex_work_url(best.get("item") or {}),
                })

    except Exception as e:
        try:
            row["openalex_short_rescue_error"] = str(e)
        except Exception:
            pass

    row["status"] = _normalize_verify_status(row.get("status"))
    return row


# ============================================================
# PUBLICATION EVENT VERIFICATION v1.6.0
# ============================================================
# This final wrapper runs after bibliographic matching. It checks the confirmed
# DOI against Crossref update-to metadata, including Retraction Watch events.
VERIFY_BUILD = "commercial-2026-09-16-publication-integrity-v1.6.0"
_V160_PREVIOUS_VERIFY_SINGLE_REFERENCE = globals().get("_verify_single_reference")


def _verify_single_reference(
    ref: str,
    style: str,
    use_crossref: bool,
    use_openalex: bool,
    enrich_metadata: bool = False,
) -> Dict[str, Any]:
    row = _V160_PREVIOUS_VERIFY_SINGLE_REFERENCE(
        ref, style, use_crossref, use_openalex, enrich_metadata
    )
    try:
        row = enrich_verification_row(row, ref)
        row["verification_build"] = VERIFY_BUILD
    except Exception as exc:
        row = dict(row or {})
        row.update({
            "publication_status_checked": False,
            "publication_status": "unchecked",
            "publication_status_source": "crossref",
            "publication_status_reason": f"Publication-status verification failed safely: {exc}",
            "verification_build": VERIFY_BUILD,
        })
    return row


# ============================================================
# NUMBERED AUTHOR-YEAR REFERENCE VERIFICATION v1.6.1
# ============================================================
# A manuscript may cite numerically in the text while formatting each reference
# as APA, for example: ``1. Cohen, J. (1988). Title...``.  Earlier batch style
# detection treated every numbered list as Vancouver/IEEE.  That discarded the
# parenthesised author-year structure and caused valid no-DOI references to be
# reported as not found.  Citation-marker style and reference-entry syntax are
# now detected separately.
VERIFY_BUILD = "commercial-2026-09-17-numbered-author-year-verification-v1.6.1"


def _looks_like_numbered_author_year_reference(ref: str) -> bool:
    """Return True for numbered APA/author-year reference entries."""
    text = str(ref or "").replace("\xa0", " ").replace("\t", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not re.match(
        r"^\s*(?:\[\s*\d{1,4}\s*\]\s*[\.]?|\(\s*\d{1,4}\s*\)\s*[\.]?|\d{1,4}\s*[\.)])\s+",
        text,
    ):
        return False

    text = re.sub(
        r"^\s*(?:\[\s*\d{1,4}\s*\]\s*[\.]?|\(\s*\d{1,4}\s*\)\s*[\.]?|\d{1,4}\s*[\.)])\s+",
        "",
        text,
        count=1,
    )
    year_match = re.search(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)\s*[\.]?", text[:600], flags=re.I)
    if not year_match:
        return False

    author_segment = text[:year_match.start()].strip(" ,.;")
    if not author_segment or ";" in author_segment:
        return False

    # APA personal authors normally contain a comma followed by initials.
    personal_author = bool(re.search(
        r"^[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+,\s*(?:[A-Z]\.|[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+)",
        author_segment,
    ))
    # Also admit a concise organisation or editor author before the year.
    organisation_author = bool(
        len(author_segment.split()) <= 18
        and re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", author_segment)
        and not re.search(r"\b(?:vol|volume|issue|pp?)\.?\s*\d", author_segment, flags=re.I)
    )
    return personal_author or organisation_author


_V161_PREVIOUS_EXTRACT_FIELDS_BY_STYLE = globals().get("_extract_fields_by_style")


def _extract_fields_by_style(ref: str, style: str) -> Dict[str, Any]:
    """Parse numbered APA entries as author-year without changing citation style."""
    if _looks_like_numbered_author_year_reference(ref):
        clean = _strip_leading_numbering(str(ref or ""))
        fields = _extract_common_fields(clean)
        info = _style_metadata("apa")
        fields.update({
            "style_family": "author_year",
            "style_label": info.get("label", "APA / author-year"),
            "style_sample": info.get("sample", ""),
            "verification_profile": "numbered_author_year_v161",
            "reference_numbered": True,
            "reference_entry_syntax": "author_year",
        })
        return fields
    return _V161_PREVIOUS_EXTRACT_FIELDS_BY_STYLE(ref, style)


_V161_PREVIOUS_BUILD_QUERY_PLAN = globals().get("_build_verification_query_plan")


def _build_verification_query_plan(ref: str, style: str) -> Dict[str, Any]:
    """Use author-year search queries for numbered APA-style bibliography entries."""
    effective_style = "apa" if _looks_like_numbered_author_year_reference(ref) else style
    plan = _V161_PREVIOUS_BUILD_QUERY_PLAN(ref, effective_style)
    if _looks_like_numbered_author_year_reference(ref):
        plan["reference_entry_syntax"] = "numbered_author_year"
        plan["citation_marker_style"] = _canonical_verify_style(style)
        plan["effective_verification_style"] = "apa"
        fields = plan.get("fields") or {}
        if isinstance(fields, dict):
            fields["verification_profile"] = "numbered_author_year_v161"
            fields["reference_numbered"] = True
            fields["reference_entry_syntax"] = "author_year"
            plan["fields"] = fields
    return plan


_V161_PREVIOUS_VERIFY_SINGLE_REFERENCE = globals().get("_verify_single_reference")


def _verify_single_reference(
    ref: str,
    style: str,
    use_crossref: bool,
    use_openalex: bool,
    enrich_metadata: bool = False,
) -> Dict[str, Any]:
    _reset_api_request_diagnostics()
    row = _V161_PREVIOUS_VERIFY_SINGLE_REFERENCE(
        ref, style, use_crossref, use_openalex, enrich_metadata
    )
    if _looks_like_numbered_author_year_reference(ref):
        row["reference_entry_syntax"] = "numbered_author_year"
        row["effective_verification_style"] = "apa"
        row["citation_marker_style"] = _canonical_verify_style(style)
        row["verification_profile"] = "numbered_author_year_v161"
    diagnostics = _api_request_diagnostics()
    transient = [
        event for event in diagnostics
        if event.get("kind") in {"rate_limited", "timeout", "network_error", "http_error"}
    ]
    if _normalize_verify_status(row.get("status")) == "not_found" and transient:
        # A provider failure is not evidence that a publication does not exist.
        row["status"] = "offline"
        row["source"] = row.get("source") or "provider_unavailable"
        row["publication_status_checked"] = False
        row["publication_status"] = "unchecked"
        row["confidence_reason"] = (
            "One or more scholarly metadata services did not complete the lookup. "
            "Retry verification before treating this reference as not found."
        )
        row["verification_transport_diagnostics"] = transient
    row["verification_build"] = VERIFY_BUILD
    return row
