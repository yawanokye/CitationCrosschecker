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
WORKER_THREADS = int(os.getenv("VERIFY_INNER_THREADS", "2"))
RETRY_ATTEMPTS = int(os.getenv("VERIFY_RETRY_ATTEMPTS", "1"))
BATCH_DELAY = float(os.getenv("VERIFY_BATCH_DELAY", "0"))

# Chunk processing for large reference sets
CHUNK_SIZE = int(os.getenv("VERIFY_INTERNAL_CHUNK_SIZE", "5"))
CHUNK_DELAY = float(os.getenv("VERIFY_CHUNK_DELAY", "0"))
MAX_RETRIES_PER_REFERENCE = int(os.getenv("VERIFY_MAX_RETRIES_PER_REFERENCE", "2"))

# Fast-skip and fallback controls
VERIFY_SKIP_WEAK_TITLE = _env_flag("VERIFY_SKIP_WEAK_TITLE", "1")
VERIFY_MIN_TITLE_WORDS = int(os.getenv("VERIFY_MIN_TITLE_WORDS", "3"))
VERIFY_STOP_ON_STRONG_MATCH = _env_flag("VERIFY_STOP_ON_STRONG_MATCH", "1")
VERIFY_DEEP_FALLBACK = _env_flag("VERIFY_DEEP_FALLBACK", "0")
VERIFY_RETRY_FAILED = _env_flag("VERIFY_RETRY_FAILED", "0")
VERIFY_CROSSREF_ROWS = int(os.getenv("VERIFY_CROSSREF_ROWS", "10"))
VERIFY_TITLE_ROWS = int(os.getenv("VERIFY_TITLE_ROWS", "10"))
VERIFY_OPENALEX_ROWS = int(os.getenv("VERIFY_OPENALEX_ROWS", "5"))
VERIFY_SINGLE_REF_TIMEOUT = int(os.getenv("VERIFY_SINGLE_REF_TIMEOUT", str(max(6, API_TIMEOUT + 2))))
VERIFY_RETRY_BACKOFF_SECONDS = float(os.getenv("VERIFY_RETRY_BACKOFF_SECONDS", "1"))
VERIFY_FORCE_OPENALEX_FALLBACK = _env_flag("VERIFY_FORCE_OPENALEX_FALLBACK", "1")
VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY = _env_flag("VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY", "1")
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
            # 🔥 Ensure status is "processing" while in progress
            if progress < job.total:
                job.status = "processing"
            else:
                job.status = "completed"
                job.completed_at = datetime.now().isoformat()
                print(f"[DEBUG] Job {job_id}: COMPLETED - {progress}/{job.total}")
            
            # Print every update for debugging
            if progress % 5 == 0 or progress == job.total:
                print(f"[DEBUG] Job {job_id}: progress {progress}/{job.total} (status: {job.status})")

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
                return r.json()
            if r.status_code == 429:
                # Do not hold the job for long API backoffs. Mark the row for review instead.
                print("[DEBUG] API rate limited. Skipping this lookup quickly.")
                return None
            return None
        except requests.exceptions.Timeout:
            print(f"[DEBUG] API timeout after {timeout}s on attempt {attempt + 1}/{attempts}")
            if attempt < attempts - 1 and VERIFY_RETRY_BACKOFF_SECONDS > 0:
                time.sleep(VERIFY_RETRY_BACKOFF_SECONDS)
        except Exception as e:
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
            return "likely"
        return "needs_review"

    # -----------------------------------------
    # 2. VERIFIED
    # -----------------------------------------
    # Very strong title + year support.
    if title_score >= 92 and year_match == 1:
        return "verified"

    # Strong title + author support.
    if title_score >= 70 and author_overlap >= 1:
        return "verified"

    # Strong title + both year and author support.
    if title_score >= 69 and year_match == 1 and author_overlap >= 1:
        return "verified"

    # High combined score, but still requires title strength and at least one external support.
    if score >= 90 and title_score >= 85 and (year_match == 1 or author_overlap >= 1):
        return "verified"

    # -----------------------------------------
    # 3. LIKELY
    # -----------------------------------------
    # Strong title but missing author or year support.
    if title_score >= 85:
        return "Verified"

    # Moderate title with year or author support.
    if title_score >= 78 and (year_match == 1 or author_overlap >= 1):
        return "Verified"

    # Good combined score, but not enough for verified.
    if score >= 70 and title_score >= 70:
        return "Verified"

    # -----------------------------------------
    # 4. NEEDS REVIEW
    # -----------------------------------------
    # Candidate exists but evidence is incomplete or weak.
    if title_score >= 50:
        return "Likely"

    if score >= 20:
        return "needs_review"

    # -----------------------------------------
    # 5. NOT FOUND
    # -----------------------------------------
    return "not_found"

    if title_score >= 50:
        return "LIKELY"

    if score >= 20:
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

    # Fast commercial skip: no DOI and weak title/query should not trigger slow online searches.
    if VERIFY_SKIP_WEAK_TITLE and not ref_doi and _significant_word_count(title_only or ref_title) < VERIFY_MIN_TITLE_WORDS:
        row = _make_fast_review_row(
            ref,
            style,
            query=query,
            authors=ref_authors,
            reason="No DOI and too few significant title words for reliable fast verification.",
        )
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
    
    # Final results
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
            return "likely", "DOI matches, but title evidence is not strong enough for automatic verification."
        return "needs_review", "DOI matches, but the title appears inconsistent with the reference."

    if has_author_conflict and VERIFY_STRICT_AUTHOR_GATE:
        if title_score >= 96 and year_match and journal_score >= VERIFY_THRESHOLD_JOURNAL_SUPPORT:
            return "likely", "Very strong title, year and journal match, but author names do not overlap."
        if title_score >= VERIFY_THRESHOLD_TITLE_REVIEW:
            return "needs_review", "Possible match found, but author names do not overlap."
        return "not_found", "Candidates were found, but author and title evidence were too weak."

    if title_score >= VERIFY_THRESHOLD_TITLE_VERIFIED and year_match and (author_overlap >= 1 or author_similarity >= 80 or not ref_fields.get("authors")):
        if has_ref_journal and journal_score and journal_score < 45:
            return "likely", "Strong title, author and year match, but journal name differs."
        return "verified", "Strong title, author and year match."

    if title_score >= 96 and year_match and journal_score >= VERIFY_THRESHOLD_JOURNAL_SUPPORT:
        return "verified", "Strong title, year and journal match."

    if source_agreement >= 2 and title_score >= 90 and year_delta <= 1 and (author_overlap >= 1 or journal_score >= VERIFY_THRESHOLD_JOURNAL_SUPPORT):
        return "verified", "Cross-source agreement with strong bibliographic match."

    if title_score >= VERIFY_THRESHOLD_TITLE_LIKELY and year_delta <= 1 and (author_overlap >= 1 or author_similarity >= 70 or journal_score >= VERIFY_THRESHOLD_JOURNAL_SUPPORT):
        return "likely", "Strong title match with supporting year, author, or journal evidence."

    if score >= 82 and title_score >= 82 and year_delta <= 1:
        return "likely", "High composite score, but not enough evidence for automatic verification."

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
            return r.json()
        if r.status_code == 429:
            _debug_multisource(f"Rate limited by {url}")
        return None
    except Exception as exc:
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
# FAST-START MULTI-SOURCE OVERRIDE
# Added to fix verification jobs that appear not to start or run too slowly.
# This keeps the public structure and function names unchanged.
# ============================================================

VERIFY_FAST_START_MODE = _env_flag("VERIFY_FAST_START_MODE", "1")
VERIFY_MAX_CROSSREF_QUERIES = int(os.getenv("VERIFY_MAX_CROSSREF_QUERIES", "2"))
VERIFY_MAX_OPENALEX_QUERIES = int(os.getenv("VERIFY_MAX_OPENALEX_QUERIES", "1"))
VERIFY_FAST_RETURN_ON_DOI = _env_flag("VERIFY_FAST_RETURN_ON_DOI", "1")
VERIFY_FAST_RETURN_ON_ANY_STRONG = _env_flag("VERIFY_FAST_RETURN_ON_ANY_STRONG", "1")
VERIFY_FAST_SKIP_GENERIC_SEMANTIC_WHEN_CANDIDATES = _env_flag("VERIFY_FAST_SKIP_GENERIC_SEMANTIC_WHEN_CANDIDATES", "1")
VERIFY_FAST_SPECIAL_TIMEOUT = int(os.getenv("VERIFY_FAST_SPECIAL_TIMEOUT", "2"))

# Re-tighten defaults for production speed. The user can still override these,
# but FAST_START_MODE budgets the number of API calls even if older env values are broad.
VERIFY_DEEP_FALLBACK = _env_flag("VERIFY_DEEP_FALLBACK", "0")
VERIFY_STOP_ON_STRONG_MATCH = _env_flag("VERIFY_STOP_ON_STRONG_MATCH", "1")
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES = int(os.getenv("VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES", "1"))
VERIFY_SPECIAL_ROWS = int(os.getenv("VERIFY_SPECIAL_ROWS", "2"))
VERIFY_SPECIAL_TIMEOUT = min(
    int(os.getenv("VERIFY_SPECIAL_TIMEOUT", str(VERIFY_FAST_SPECIAL_TIMEOUT))),
    VERIFY_FAST_SPECIAL_TIMEOUT,
)


def _fast_candidate_has_doi_match(fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> bool:
    """Return True when an exact DOI lookup produced a usable DOI match."""
    ref_doi = _normalise_doi(fields.get("doi", ""))
    if not ref_doi or not candidates:
        return False
    try:
        for cand in candidates:
            meta = _candidate_fields(cand)
            if _normalise_doi(meta.get("doi", "")) == ref_doi:
                return True
    except Exception:
        return False
    return False


def _fast_should_return(fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> bool:
    """Fast stopping rule used after each small query batch."""
    if not candidates:
        return False
    if VERIFY_FAST_RETURN_ON_DOI and _fast_candidate_has_doi_match(fields, candidates):
        return True
    if VERIFY_FAST_RETURN_ON_ANY_STRONG and _candidate_is_strong_enough(fields, candidates):
        return True
    return False


def _limited_queries(queries: List[Dict[str, Any]], max_non_doi: int) -> List[Dict[str, Any]]:
    """Keep DOI searches plus a small number of non-DOI searches."""
    ordered = sorted(queries or [], key=lambda x: x.get("priority", 99))
    out: List[Dict[str, Any]] = []
    non_doi = 0
    for q in ordered:
        if q.get("mode") == "doi_exact":
            out.append(q)
            continue
        if non_doi >= max(0, max_non_doi):
            continue
        out.append(q)
        non_doi += 1
    return out


def _prioritise_fallback_queries(queries: List[Dict[str, Any]], fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Select only the most useful fallback sources.
    This prevents every weak reference from calling many external databases.
    """
    if not queries:
        return []

    flags = fields.get("reference_type_flags", {}) or {}
    doi = bool(fields.get("doi"))
    has_candidates = bool(candidates)

    priority_source_order: List[str] = []

    if doi:
        priority_source_order.extend(["datacite", "semantic_scholar"])
    if flags.get("looks_health"):
        priority_source_order.extend(["pubmed", "europepmc"])
    if flags.get("looks_book") or fields.get("isbn"):
        priority_source_order.extend(["google_books", "open_library"])
    if flags.get("looks_education"):
        priority_source_order.append("eric")
    if flags.get("looks_arxiv"):
        priority_source_order.append("arxiv")
    if flags.get("looks_dataset_repo"):
        priority_source_order.extend(["datacite", "core"])

    # Generic fallback only when Crossref/OpenAlex returned nothing.
    if not has_candidates:
        priority_source_order.append("semantic_scholar")

    seen_sources = set()
    selected: List[Dict[str, Any]] = []

    for source in priority_source_order:
        if source in seen_sources:
            continue
        seen_sources.add(source)
        for q in sorted(queries, key=lambda x: x.get("priority", 99)):
            if q.get("source") == source and _source_enabled(source):
                if VERIFY_FAST_SKIP_GENERIC_SEMANTIC_WHEN_CANDIDATES and has_candidates and q.get("name") == "semantic_scholar_search":
                    continue
                selected.append(q)
                break
        if len(selected) >= VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES:
            break

    return selected


def _run_query_plan(plan: Dict[str, Any], use_crossref: bool, use_openalex: bool) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """
    Fast-start query runner.

    It preserves the existing verification structure but prevents the job from
    making 8 to 12 network calls per reference before progress appears.
    """
    candidates: List[Dict[str, Any]] = []
    query_used: List[str] = []
    query_strategy: List[str] = []
    fields = plan.get("fields", {}) or {}
    openalex_allowed = bool(use_openalex or VERIFY_FORCE_OPENALEX_FALLBACK)

    # 1. Crossref first, with DOI plus only the strongest bibliographic queries.
    crossref_queries = plan.get("crossref_queries", [])
    if VERIFY_FAST_START_MODE:
        crossref_queries = _limited_queries(crossref_queries, VERIFY_MAX_CROSSREF_QUERIES)
    else:
        crossref_queries = sorted(crossref_queries, key=lambda x: x.get("priority", 99))

    for q in crossref_queries:
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
                    rows=min(int(q.get("rows", VERIFY_CROSSREF_ROWS)), VERIFY_CROSSREF_ROWS),
                    query_name=name,
                )
            if res:
                candidates.extend(res)
            query_strategy.append(name)
            query_used.append(q.get("doi") or q.get("query_bibliographic") or "")
            if _fast_should_return(fields, candidates):
                return candidates, query_used, query_strategy
        except Exception as exc:
            print(f"[DEBUG] Crossref query failed for {name}: {exc}")

    # 2. OpenAlex fallback, also budgeted. Usually one title-year query is enough.
    openalex_queries = plan.get("openalex_queries", [])
    if VERIFY_FAST_START_MODE:
        openalex_queries = _limited_queries(openalex_queries, VERIFY_MAX_OPENALEX_QUERIES)
    else:
        openalex_queries = sorted(openalex_queries, key=lambda x: x.get("priority", 99))

    for q in openalex_queries:
        if not openalex_allowed:
            continue
        name = q.get("name", "openalex")
        try:
            if q.get("mode") == "doi_exact":
                res = _query_openalex_by_doi(q.get("doi", ""))
            else:
                res = _query_openalex_search(
                    q.get("search", ""),
                    rows=min(int(q.get("rows", VERIFY_OPENALEX_ROWS)), VERIFY_OPENALEX_ROWS),
                    publication_year=q.get("publication_year", ""),
                    query_name=name,
                )
            if res:
                candidates.extend(res)
            query_strategy.append(name)
            query_used.append(q.get("doi") or q.get("search") or "")
            if _fast_should_return(fields, candidates):
                return candidates, query_used, query_strategy
        except Exception as exc:
            print(f"[DEBUG] OpenAlex query failed for {name}: {exc}")

    # 3. Extra databases only when still weak, and only selected by reference type.
    if not VERIFY_MULTISOURCE_FALLBACK:
        return candidates, query_used, query_strategy

    if VERIFY_MULTISOURCE_ONLY_WHEN_WEAK and _candidate_is_strong_enough(fields, candidates):
        return candidates, query_used, query_strategy

    fallback_queries = plan.get("fallback_queries", [])
    if VERIFY_FAST_START_MODE:
        fallback_queries = _prioritise_fallback_queries(fallback_queries, fields, candidates)
    else:
        fallback_queries = sorted(fallback_queries, key=lambda x: x.get("priority", 99))[:VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES]

    extra_count = 0
    for q in fallback_queries:
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
                if _fast_should_return(fields, candidates):
                    break
        except Exception as exc:
            print(f"[DEBUG] {source} fallback failed for {q.get('name')}: {exc}")

    return candidates, query_used, query_strategy


# Make the batch heartbeat more visible without changing its public signature.
_original_verify_references_batch_fast_start = verify_references_batch

def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = False,
    job_id: str = None,
    enrich_metadata: bool = False,
) -> List[Dict[str, Any]]:
    if job_id:
        try:
            update_job_progress(job_id, 0)
            store_verification_results(job_id, [])
        except Exception:
            pass
    return _original_verify_references_batch_fast_start(
        references=references,
        style=style,
        throttle_s=throttle_s,
        use_crossref=use_crossref,
        use_openalex=use_openalex,
        job_id=job_id,
        enrich_metadata=enrich_metadata,
    )

# ============================================================
# LEGACY IMPORT COMPATIBILITY PATCH
# Keeps older modules such as citation_suggester.py working.
# Some modules still import _score directly from verify.py.
# The commercial scorer uses richer metadata internally, but this wrapper
# preserves the original six-argument _score contract.
# ============================================================

def _score(
    ref_title: str,
    ref_authors: List[str],
    ref_year: str,
    cand_title: str,
    cand_authors: List[str],
    cand_year: str,
) -> Dict[str, Any]:
    ref_title_n = _norm_text(ref_title)
    cand_title_n = _norm_text(cand_title)

    token_score = fuzz.token_sort_ratio(ref_title_n, cand_title_n) if ref_title_n and cand_title_n else 0
    set_score = fuzz.token_set_ratio(ref_title_n, cand_title_n) if ref_title_n and cand_title_n else 0
    partial_score = fuzz.partial_ratio(ref_title_n, cand_title_n) if ref_title_n and cand_title_n else 0

    title_score = int((token_score * 0.45) + (set_score * 0.35) + (partial_score * 0.20))

    ref_author_set = {re.sub(r"[^a-z'\-]", "", _safe_strip(a).lower()) for a in (ref_authors or []) if _safe_strip(a)}
    cand_author_set = {re.sub(r"[^a-z'\-]", "", _safe_strip(a).lower()) for a in (cand_authors or []) if _safe_strip(a)}
    ref_author_set.discard("")
    cand_author_set.discard("")

    if ref_author_set and cand_author_set:
        intersection = len(ref_author_set & cand_author_set)
        union = len(ref_author_set | cand_author_set)
        author_similarity = int((intersection / union) * 100) if union else 0
        author_overlap = intersection
    else:
        author_similarity = 0
        author_overlap = 0

    year_match = 1 if ref_year and cand_year and _safe_str(ref_year)[:4] == _safe_str(cand_year)[:4] else 0

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

try:
    if "__all__" in globals() and "_score" not in __all__:
        __all__.append("_score")
except Exception:
    pass


# ============================================================
# ULTRA-FAST PRODUCTION OVERRIDE
# Added to make verification start faster and finish with fewer API calls.
# Public API is preserved: verify_references_batch, submit_verification,
# get_verification_status, get_verification_results and _score remain available.
# ============================================================

VERIFY_ULTRA_FAST_MODE = _env_flag("VERIFY_ULTRA_FAST_MODE", "1")
VERIFY_ULTRA_WORKERS = int(os.getenv("VERIFY_ULTRA_WORKERS", os.getenv("VERIFY_INNER_THREADS", "3")))
VERIFY_ULTRA_MAX_API_CALLS_PER_REF = int(os.getenv("VERIFY_ULTRA_MAX_API_CALLS_PER_REF", "3"))
VERIFY_ULTRA_CROSSREF_TEXT_QUERIES = int(os.getenv("VERIFY_ULTRA_CROSSREF_TEXT_QUERIES", "1"))
VERIFY_ULTRA_OPENALEX_TEXT_QUERIES = int(os.getenv("VERIFY_ULTRA_OPENALEX_TEXT_QUERIES", "1"))
VERIFY_ULTRA_FALLBACK_QUERIES = int(os.getenv("VERIFY_ULTRA_FALLBACK_QUERIES", "1"))
VERIFY_ULTRA_STORE_EVERY = int(os.getenv("VERIFY_ULTRA_STORE_EVERY", "3"))
VERIFY_ULTRA_CACHE_SECONDS = int(os.getenv("VERIFY_ULTRA_CACHE_SECONDS", "21600"))
VERIFY_ULTRA_FAST_API_TIMEOUT = float(os.getenv("VERIFY_ULTRA_FAST_API_TIMEOUT", "3"))
VERIFY_ULTRA_DISABLE_ENRICH_METADATA = _env_flag("VERIFY_ULTRA_DISABLE_ENRICH_METADATA", "1")
VERIFY_ULTRA_SKIP_OPENALEX_WHEN_CROSSREF_CANDIDATE = _env_flag("VERIFY_ULTRA_SKIP_OPENALEX_WHEN_CROSSREF_CANDIDATE", "1")
VERIFY_ULTRA_FALLBACK_ONLY_IF_NO_CANDIDATE = _env_flag("VERIFY_ULTRA_FALLBACK_ONLY_IF_NO_CANDIDATE", "1")

# Production-fast defaults. Env values can still override these before import.
VERIFY_DEEP_FALLBACK = _env_flag("VERIFY_DEEP_FALLBACK", "0")
VERIFY_STOP_ON_STRONG_MATCH = _env_flag("VERIFY_STOP_ON_STRONG_MATCH", "1")
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES = min(
    int(os.getenv("VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES", str(VERIFY_ULTRA_FALLBACK_QUERIES))),
    max(0, VERIFY_ULTRA_FALLBACK_QUERIES),
)
VERIFY_SPECIAL_ROWS = min(int(os.getenv("VERIFY_SPECIAL_ROWS", "2")), 2)
VERIFY_SPECIAL_TIMEOUT = min(int(os.getenv("VERIFY_SPECIAL_TIMEOUT", "2")), 2)
VERIFY_CROSSREF_ROWS = min(int(os.getenv("VERIFY_CROSSREF_ROWS", "5")), 5)
VERIFY_OPENALEX_ROWS = min(int(os.getenv("VERIFY_OPENALEX_ROWS", "3")), 3)
VERIFY_TITLE_ROWS = min(int(os.getenv("VERIFY_TITLE_ROWS", "3")), 3)

# Semantic Scholar can be slow or rate-limited. Keep it opt-in unless explicitly set.
VERIFY_USE_SEMANTIC_SCHOLAR = _env_flag("VERIFY_USE_SEMANTIC_SCHOLAR", "0")
VERIFY_USE_CORE = _env_flag("VERIFY_USE_CORE", "0")
VERIFY_USE_DOAJ = _env_flag("VERIFY_USE_DOAJ", "0")

_JSON_CACHE: Dict[str, Tuple[float, Optional[dict]]] = {}
_JSON_CACHE_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()


def _json_cache_key(url: str, params: Optional[dict]) -> str:
    if not params:
        return url
    try:
        items = sorted((str(k), str(v)) for k, v in params.items())
        return url + "?" + "&".join(f"{k}={v}" for k, v in items)
    except Exception:
        return url + "?" + str(params)


def _get_http_session():
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        try:
            adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
        except Exception:
            pass
        _THREAD_LOCAL.session = session
    return session


def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = None) -> Optional[dict]:
    """
    Faster cached HTTP getter.
    - Reuses connections per worker thread.
    - Caches identical API calls during and across jobs.
    - Uses a short timeout in ultra-fast mode.
    - Does not sleep on failures.
    """
    if timeout is None:
        timeout = min(float(API_TIMEOUT), VERIFY_ULTRA_FAST_API_TIMEOUT) if VERIFY_ULTRA_FAST_MODE else API_TIMEOUT

    key = _json_cache_key(url, params)
    now = time.time()

    if VERIFY_ULTRA_CACHE_SECONDS > 0:
        with _JSON_CACHE_LOCK:
            cached = _JSON_CACHE.get(key)
            if cached and now - cached[0] <= VERIFY_ULTRA_CACHE_SECONDS:
                return cached[1]

    try:
        headers = {
            "User-Agent": f"CitationCrosschecker/3.0 (mailto:{MAILTO})",
            "Accept": "application/json",
        }
        response = _get_http_session().get(url, params=params, timeout=timeout, headers=headers)
        if response.status_code == 200:
            data = response.json()
        else:
            data = None
    except Exception as exc:
        print(f"[DEBUG] Fast API request failed: {exc}")
        data = None

    if VERIFY_ULTRA_CACHE_SECONDS > 0:
        with _JSON_CACHE_LOCK:
            if len(_JSON_CACHE) > 5000:
                _JSON_CACHE.clear()
            _JSON_CACHE[key] = (now, data)

    return data


def _ultra_candidate_is_useful(fields: Dict[str, Any], candidates: List[Dict[str, Any]], likely_threshold: int = 82) -> bool:
    if not candidates:
        return False
    try:
        _best, meta, _alts = _best_candidate(fields, candidates)
        if not meta:
            return False
        if meta.get("doi_match"):
            return True
        if int(meta.get("title_score", 0)) >= likely_threshold and (
            int(meta.get("year_match", 0)) == 1
            or int(meta.get("author_overlap", 0)) >= 1
            or int(meta.get("journal_score", 0)) >= VERIFY_THRESHOLD_JOURNAL_SUPPORT
        ):
            return True
    except Exception:
        return False
    return False


def _ultra_select_queries(queries: List[Dict[str, Any]], max_text: int) -> List[Dict[str, Any]]:
    """Keep DOI exact queries and only the strongest text queries."""
    ordered = sorted(queries or [], key=lambda q: q.get("priority", 99))
    doi_queries = [q for q in ordered if q.get("mode") == "doi_exact"]
    text_queries = [q for q in ordered if q.get("mode") != "doi_exact"]

    preferred_names = [
        "crossref_full_bibliographic",
        "crossref_rich_bibliographic",
        "crossref_title_author_year",
        "crossref_title_journal_year",
        "openalex_title_year",
        "openalex_title_only",
    ]

    selected_text: List[Dict[str, Any]] = []
    for name in preferred_names:
        for q in text_queries:
            if q.get("name") == name and q not in selected_text:
                selected_text.append(q)
                break
        if len(selected_text) >= max_text:
            break

    for q in text_queries:
        if len(selected_text) >= max_text:
            break
        if q not in selected_text:
            selected_text.append(q)

    return doi_queries + selected_text[:max_text]


def _ultra_select_fallback_queries(queries: List[Dict[str, Any]], fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not queries or VERIFY_ULTRA_FALLBACK_QUERIES <= 0:
        return []
    if VERIFY_ULTRA_FALLBACK_ONLY_IF_NO_CANDIDATE and candidates:
        return []
    if VERIFY_MULTISOURCE_ONLY_WHEN_WEAK and _ultra_candidate_is_useful(fields, candidates, likely_threshold=78):
        return []

    flags = fields.get("reference_type_flags", {}) or {}
    source_order: List[str] = []

    if fields.get("doi"):
        source_order.append("datacite")
    if flags.get("looks_health") or fields.get("pmid") or fields.get("pmcid"):
        source_order.extend(["pubmed", "europepmc"])
    if flags.get("looks_book") or fields.get("isbn"):
        source_order.extend(["google_books", "open_library"])
    if flags.get("looks_arxiv") or fields.get("arxiv_id"):
        source_order.append("arxiv")
    if flags.get("looks_education"):
        source_order.append("eric")
    if flags.get("looks_dataset_repo"):
        source_order.append("datacite")

    # Generic fallback is last and only when explicitly enabled.
    if VERIFY_USE_SEMANTIC_SCHOLAR and not candidates:
        source_order.append("semantic_scholar")

    seen = set()
    selected: List[Dict[str, Any]] = []
    for source in source_order:
        if source in seen or not _source_enabled(source):
            continue
        seen.add(source)
        for q in sorted(queries, key=lambda x: x.get("priority", 99)):
            if q.get("source") == source:
                selected.append(q)
                break
        if len(selected) >= VERIFY_ULTRA_FALLBACK_QUERIES:
            break
    return selected


def _run_query_plan(plan: Dict[str, Any], use_crossref: bool, use_openalex: bool) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """
    Ultra-fast query runner.
    Normal path is 1 to 3 API calls per reference:
      1. DOI lookup if DOI exists, or one Crossref bibliographic query.
      2. One OpenAlex query only if Crossref is weak.
      3. One type-specific fallback only when still needed.
    """
    candidates: List[Dict[str, Any]] = []
    query_used: List[str] = []
    query_strategy: List[str] = []
    fields = plan.get("fields", {}) or {}
    api_calls = 0
    openalex_allowed = bool(use_openalex or VERIFY_FORCE_OPENALEX_FALLBACK)

    def budget_left() -> bool:
        return api_calls < max(1, VERIFY_ULTRA_MAX_API_CALLS_PER_REF)

    # Crossref, DOI plus one bibliographic query.
    for q in _ultra_select_queries(plan.get("crossref_queries", []), VERIFY_ULTRA_CROSSREF_TEXT_QUERIES):
        if not use_crossref or not budget_left():
            break
        name = q.get("name", "crossref")
        try:
            if q.get("mode") == "doi_exact":
                res = _query_crossref_by_doi(q.get("doi", ""))
            else:
                res = _query_crossref_bibliographic(
                    q.get("query_bibliographic", ""),
                    query_author=q.get("query_author", ""),
                    rows=min(int(q.get("rows", VERIFY_CROSSREF_ROWS)), VERIFY_CROSSREF_ROWS),
                    query_name=name,
                )
            api_calls += 1
            if res:
                candidates.extend(res)
            query_strategy.append(name)
            query_used.append(q.get("doi") or q.get("query_bibliographic") or "")
            if _ultra_candidate_is_useful(fields, candidates, likely_threshold=84):
                return _dedupe_candidates(candidates), query_used, query_strategy
        except Exception as exc:
            print(f"[DEBUG] Ultra Crossref query failed for {name}: {exc}")

    # Skip OpenAlex when Crossref already returned a decent candidate and the user wants speed.
    if VERIFY_ULTRA_SKIP_OPENALEX_WHEN_CROSSREF_CANDIDATE and candidates:
        if _ultra_candidate_is_useful(fields, candidates, likely_threshold=75):
            return _dedupe_candidates(candidates), query_used, query_strategy

    # OpenAlex, one query only.
    for q in _ultra_select_queries(plan.get("openalex_queries", []), VERIFY_ULTRA_OPENALEX_TEXT_QUERIES):
        if not openalex_allowed or not budget_left():
            break
        name = q.get("name", "openalex")
        try:
            if q.get("mode") == "doi_exact":
                res = _query_openalex_by_doi(q.get("doi", ""))
            else:
                res = _query_openalex_search(
                    q.get("search", ""),
                    rows=min(int(q.get("rows", VERIFY_OPENALEX_ROWS)), VERIFY_OPENALEX_ROWS),
                    publication_year=q.get("publication_year", ""),
                    query_name=name,
                )
            api_calls += 1
            if res:
                candidates.extend(res)
            query_strategy.append(name)
            query_used.append(q.get("doi") or q.get("search") or "")
            if _ultra_candidate_is_useful(fields, candidates, likely_threshold=84):
                return _dedupe_candidates(candidates), query_used, query_strategy
        except Exception as exc:
            print(f"[DEBUG] Ultra OpenAlex query failed for {name}: {exc}")

    # One adaptive fallback only when useful.
    if VERIFY_MULTISOURCE_FALLBACK and budget_left():
        fallback_queries = _ultra_select_fallback_queries(plan.get("fallback_queries", []), fields, candidates)
        for q in fallback_queries[:VERIFY_ULTRA_FALLBACK_QUERIES]:
            if not budget_left():
                break
            source = q.get("source", "")
            if not _source_enabled(source):
                continue
            try:
                res = _run_fallback_query(q, fields)
                api_calls += 1
                if res:
                    candidates.extend(res)
                query_strategy.append(q.get("name", source))
                query_used.append(_generic_metadata_query(fields) or fields.get("doi", ""))
                if _ultra_candidate_is_useful(fields, candidates, likely_threshold=82):
                    break
            except Exception as exc:
                print(f"[DEBUG] Ultra fallback failed for {q.get('name', source)}: {exc}")

    return _dedupe_candidates(candidates), query_used, query_strategy


_original_verify_single_reference_ultra = _verify_single_reference

def _verify_single_reference(
    ref: str,
    style: str,
    use_crossref: bool,
    use_openalex: bool,
    enrich_metadata: bool = False,
) -> Dict[str, Any]:
    """Ultra-fast wrapper around the commercial verifier."""
    effective_enrich = bool(enrich_metadata and not VERIFY_ULTRA_DISABLE_ENRICH_METADATA)
    return _original_verify_single_reference_ultra(ref, style, use_crossref, use_openalex, effective_enrich)


def _make_timeout_row(ref: str, style: str, error: str = "Verification timeout") -> Dict[str, Any]:
    return {
        "reference": ref,
        "style": style,
        "status": "not_found",
        "source": "timeout",
        "score": 0,
        "doi": "",
        "matched_title": "",
        "matched_year": "",
        "matched_authors": "",
        "matched_journal": "",
        "title_score": 0,
        "journal_score": 0,
        "author_overlap": 0,
        "author_similarity": 0,
        "year_match": 0,
        "query_used": "",
        "query_strategy": "timeout",
        "author": "",
        "author_mismatch_flag": 0,
        "match_note": error,
        "confidence_reason": error,
        "error": error,
        "alternative_matches": [],
        "correction_suggestions": [],
    }


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
    Ultra-fast batch verifier.
    It preserves the public signature but avoids slow chunk waits.
    Progress is updated after every completed reference.
    """
    refs = [r for r in (references or []) if _safe_strip(r)]
    normalized_style = _STYLE_ALIASES.get((style or "apa").lower(), "apa")
    total = len(refs)

    print(f"[DEBUG] ⚡ ultra verify_references_batch called: {total} references, style={normalized_style}, job_id={job_id}")

    if job_id:
        try:
            update_job_progress(job_id, 0)
            store_verification_results(job_id, [])
        except Exception:
            pass

    if not refs:
        return []

    rows: List[Optional[Dict[str, Any]]] = [None] * total
    workers = max(1, min(VERIFY_ULTRA_WORKERS, total))
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                _verify_single_reference_with_retry,
                ref,
                normalized_style,
                use_crossref,
                use_openalex,
                bool(enrich_metadata and not VERIFY_ULTRA_DISABLE_ENRICH_METADATA),
            ): (idx, ref)
            for idx, ref in enumerate(refs)
        }

        for future in as_completed(future_map):
            idx, ref = future_map[future]
            try:
                rows[idx] = future.result(timeout=max(3, int(VERIFY_SINGLE_REF_TIMEOUT)))
            except Exception as exc:
                print(f"[DEBUG] Ultra verification failed for ref {idx + 1}: {exc}")
                rows[idx] = _make_timeout_row(ref, normalized_style, str(exc))

            completed += 1
            if job_id:
                try:
                    update_job_progress(job_id, completed)
                    if completed % max(1, VERIFY_ULTRA_STORE_EVERY) == 0 or completed == total:
                        store_verification_results(job_id, [r for r in rows if r is not None])
                except Exception:
                    pass

            if throttle_s:
                time.sleep(float(throttle_s))

    final_rows = [r if r is not None else _make_timeout_row(refs[i], normalized_style, "No result returned") for i, r in enumerate(rows)]

    counts = {
        "verified": sum(1 for r in final_rows if r.get("status") == "verified"),
        "likely": sum(1 for r in final_rows if r.get("status") == "likely"),
        "needs_review": sum(1 for r in final_rows if r.get("status") == "needs_review"),
        "not_found": sum(1 for r in final_rows if r.get("status") == "not_found"),
    }
    print(f"[DEBUG] ⚡ ultra verification complete: {counts}")

    if job_id:
        try:
            update_job_progress(job_id, total)
            store_verification_results(job_id, final_rows)
        except Exception:
            pass

    return final_rows


# Keep legacy direct import working for citation_suggester.py.
def _score(
    ref_title: str,
    ref_authors: List[str],
    ref_year: str,
    cand_title: str,
    cand_authors: List[str],
    cand_year: str,
) -> Dict[str, Any]:
    ref_title_n = _norm_text(ref_title)
    cand_title_n = _norm_text(cand_title)

    token_score = fuzz.token_sort_ratio(ref_title_n, cand_title_n) if ref_title_n and cand_title_n else 0
    set_score = fuzz.token_set_ratio(ref_title_n, cand_title_n) if ref_title_n and cand_title_n else 0
    partial_score = fuzz.partial_ratio(ref_title_n, cand_title_n) if ref_title_n and cand_title_n else 0
    title_score = int((token_score * 0.45) + (set_score * 0.35) + (partial_score * 0.20))

    ref_author_set = {re.sub(r"[^a-z'\-]", "", _safe_strip(a).lower()) for a in (ref_authors or []) if _safe_strip(a)}
    cand_author_set = {re.sub(r"[^a-z'\-]", "", _safe_strip(a).lower()) for a in (cand_authors or []) if _safe_strip(a)}
    ref_author_set.discard("")
    cand_author_set.discard("")

    if ref_author_set and cand_author_set:
        intersection = len(ref_author_set & cand_author_set)
        union = len(ref_author_set | cand_author_set)
        author_similarity = int((intersection / union) * 100) if union else 0
        author_overlap = intersection
    else:
        author_similarity = 0
        author_overlap = 0

    year_match = 1 if ref_year and cand_year and _safe_str(ref_year)[:4] == _safe_str(cand_year)[:4] else 0
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

try:
    if "__all__" in globals():
        for name in ["_score", "verify_references_batch"]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass

# ============================================================
# RECALL-BOOST QUERY OVERRIDE
# Added to improve verified counts in ultra-fast mode without returning to the
# very slow all-database strategy. Public API is unchanged.
# ============================================================

VERIFY_RECALL_BOOST_MODE = _env_flag("VERIFY_RECALL_BOOST_MODE", "1")
VERIFY_RECALL_MAX_API_CALLS = int(os.getenv("VERIFY_RECALL_MAX_API_CALLS", "5"))
VERIFY_RECALL_CROSSREF_TEXT_QUERIES = int(os.getenv("VERIFY_RECALL_CROSSREF_TEXT_QUERIES", "3"))
VERIFY_RECALL_OPENALEX_TEXT_QUERIES = int(os.getenv("VERIFY_RECALL_OPENALEX_TEXT_QUERIES", "2"))
VERIFY_RECALL_FALLBACK_QUERIES = int(os.getenv("VERIFY_RECALL_FALLBACK_QUERIES", "1"))
VERIFY_RECALL_ROWS = int(os.getenv("VERIFY_RECALL_ROWS", "5"))
VERIFY_RECALL_PROMOTE_STRONG_LIKELY = _env_flag("VERIFY_RECALL_PROMOTE_STRONG_LIKELY", "1")


def _recall_best_meta(fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        _best, meta, _alts = _best_candidate(fields, candidates)
        return meta or {}
    except Exception:
        return {}


def _recall_is_verified_like(fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> bool:
    """Stop early only when the current candidates are strong enough to verify."""
    if not candidates:
        return False
    meta = _recall_best_meta(fields, candidates)
    if not meta:
        return False

    title_score = int(meta.get("title_score", 0))
    year_match = int(meta.get("year_match", 0))
    year_delta = int(meta.get("year_delta", 999))
    author_overlap = int(meta.get("author_overlap", 0))
    author_similarity = int(meta.get("author_similarity", 0))
    journal_score = int(meta.get("journal_score", 0))
    source_agreement = int(meta.get("source_agreement", 1))

    if meta.get("doi_match"):
        return True
    if title_score >= 90 and year_delta <= 1 and (author_overlap >= 1 or author_similarity >= 70 or journal_score >= 65):
        return True
    if title_score >= 88 and year_match and source_agreement >= 2:
        return True
    if title_score >= 86 and year_match and journal_score >= 75:
        return True
    return False


def _recall_query_name(q: Dict[str, Any]) -> str:
    return _safe_strip(q.get("name", ""))


def _recall_select_crossref_queries(queries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Recall-preserving Crossref order.
    Uses full reference plus compact title-author/year forms. This fixes the
    ultra-fast problem where one weak full-reference query could miss a valid item.
    """
    ordered = sorted(queries or [], key=lambda q: q.get("priority", 99))
    doi_queries = [q for q in ordered if q.get("mode") == "doi_exact"]
    text_queries = [q for q in ordered if q.get("mode") != "doi_exact"]

    preferred = [
        "crossref_full_bibliographic",
        "crossref_rich_bibliographic",
        "crossref_title_author_year",
        "crossref_title_journal_year",
    ]
    selected: List[Dict[str, Any]] = []
    for name in preferred:
        for q in text_queries:
            if _recall_query_name(q) == name and q not in selected:
                selected.append(q)
                break
        if len(selected) >= VERIFY_RECALL_CROSSREF_TEXT_QUERIES:
            break

    for q in text_queries:
        if len(selected) >= VERIFY_RECALL_CROSSREF_TEXT_QUERIES:
            break
        if q not in selected:
            selected.append(q)

    return doi_queries + selected[:VERIFY_RECALL_CROSSREF_TEXT_QUERIES]


def _recall_select_openalex_queries(queries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(queries or [], key=lambda q: q.get("priority", 99))
    doi_queries = [q for q in ordered if q.get("mode") == "doi_exact"]
    text_queries = [q for q in ordered if q.get("mode") != "doi_exact"]
    preferred = ["openalex_title_year", "openalex_title_journal", "openalex_title_only"]

    selected: List[Dict[str, Any]] = []
    for name in preferred:
        for q in text_queries:
            if _recall_query_name(q) == name and q not in selected:
                selected.append(q)
                break
        if len(selected) >= VERIFY_RECALL_OPENALEX_TEXT_QUERIES:
            break

    for q in text_queries:
        if len(selected) >= VERIFY_RECALL_OPENALEX_TEXT_QUERIES:
            break
        if q not in selected:
            selected.append(q)

    return doi_queries + selected[:VERIFY_RECALL_OPENALEX_TEXT_QUERIES]


def _recall_select_fallback_queries(queries: List[Dict[str, Any]], fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Type-aware fallback. Unlike the earlier ultra-fast selector, this can still
    run one fallback when Crossref/OpenAlex returned weak candidates.
    """
    if not queries or VERIFY_RECALL_FALLBACK_QUERIES <= 0:
        return []
    if _recall_is_verified_like(fields, candidates):
        return []

    flags = fields.get("reference_type_flags", {}) or {}
    source_order: List[str] = []

    if fields.get("doi"):
        source_order.extend(["datacite", "semantic_scholar"])
    if flags.get("looks_health") or fields.get("pmid") or fields.get("pmcid"):
        source_order.extend(["pubmed", "europepmc"])
    if flags.get("looks_book") or fields.get("isbn"):
        source_order.extend(["google_books", "open_library"])
    if flags.get("looks_education"):
        source_order.append("eric")
    if flags.get("looks_arxiv") or fields.get("arxiv_id"):
        source_order.append("arxiv")
    if flags.get("looks_dataset_repo"):
        source_order.extend(["datacite", "core"])

    # Generic scholarly fallback only when enabled and candidates are still poor.
    if VERIFY_USE_SEMANTIC_SCHOLAR and not _ultra_candidate_is_useful(fields, candidates, likely_threshold=75):
        source_order.append("semantic_scholar")

    selected: List[Dict[str, Any]] = []
    seen_sources = set()
    for source in source_order:
        if source in seen_sources or not _source_enabled(source):
            continue
        seen_sources.add(source)
        for q in sorted(queries, key=lambda x: x.get("priority", 99)):
            if q.get("source") == source:
                selected.append(q)
                break
        if len(selected) >= VERIFY_RECALL_FALLBACK_QUERIES:
            break
    return selected


def _run_query_plan(plan: Dict[str, Any], use_crossref: bool, use_openalex: bool) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """
    Recall-boost query runner.
    Target: restore high verified counts while staying fast.
    Normal upper budget: DOI + 3 Crossref text queries + 1 OpenAlex/fallback.
    """
    candidates: List[Dict[str, Any]] = []
    query_used: List[str] = []
    query_strategy: List[str] = []
    fields = plan.get("fields", {}) or {}
    openalex_allowed = bool(use_openalex or VERIFY_FORCE_OPENALEX_FALLBACK)
    api_calls = 0
    max_calls = max(1, VERIFY_RECALL_MAX_API_CALLS if VERIFY_RECALL_BOOST_MODE else VERIFY_ULTRA_MAX_API_CALLS_PER_REF)

    def budget_left() -> bool:
        return api_calls < max_calls

    # 1. Crossref first. Do not rely on only one full-reference query.
    for q in _recall_select_crossref_queries(plan.get("crossref_queries", [])):
        if not use_crossref or not budget_left():
            break
        name = q.get("name", "crossref")
        try:
            if q.get("mode") == "doi_exact":
                res = _query_crossref_by_doi(q.get("doi", ""))
            else:
                res = _query_crossref_bibliographic(
                    q.get("query_bibliographic", ""),
                    query_author=q.get("query_author", ""),
                    rows=min(max(3, int(q.get("rows", VERIFY_CROSSREF_ROWS))), max(3, VERIFY_RECALL_ROWS)),
                    query_name=name,
                )
            api_calls += 1
            if res:
                candidates.extend(res)
            query_strategy.append(name)
            query_used.append(q.get("doi") or q.get("query_bibliographic") or "")
            if _recall_is_verified_like(fields, candidates):
                return _dedupe_candidates(candidates), query_used, query_strategy
        except Exception as exc:
            print(f"[DEBUG] Recall Crossref query failed for {name}: {exc}")

    # 2. OpenAlex is no longer skipped just because Crossref returned weak candidates.
    for q in _recall_select_openalex_queries(plan.get("openalex_queries", [])):
        if not openalex_allowed or not budget_left():
            break
        name = q.get("name", "openalex")
        try:
            if q.get("mode") == "doi_exact":
                res = _query_openalex_by_doi(q.get("doi", ""))
            else:
                res = _query_openalex_search(
                    q.get("search", ""),
                    rows=min(max(3, int(q.get("rows", VERIFY_OPENALEX_ROWS))), max(3, VERIFY_RECALL_ROWS)),
                    publication_year=q.get("publication_year", ""),
                    query_name=name,
                )
            api_calls += 1
            if res:
                candidates.extend(res)
            query_strategy.append(name)
            query_used.append(q.get("doi") or q.get("search") or "")
            if _recall_is_verified_like(fields, candidates):
                return _dedupe_candidates(candidates), query_used, query_strategy
        except Exception as exc:
            print(f"[DEBUG] Recall OpenAlex query failed for {name}: {exc}")

    # 3. One type-specific fallback when the match is still weak.
    if VERIFY_MULTISOURCE_FALLBACK and budget_left():
        for q in _recall_select_fallback_queries(plan.get("fallback_queries", []), fields, candidates):
            if not budget_left():
                break
            source = q.get("source", "")
            if not _source_enabled(source):
                continue
            try:
                res = _run_fallback_query(q, fields)
                api_calls += 1
                if res:
                    candidates.extend(res)
                query_strategy.append(q.get("name", source))
                query_used.append(_generic_metadata_query(fields) or fields.get("doi", ""))
                if _recall_is_verified_like(fields, candidates):
                    break
            except Exception as exc:
                print(f"[DEBUG] Recall fallback failed for {q.get('name', source)}: {exc}")

    return _dedupe_candidates(candidates), query_used, query_strategy


def _classify_from_meta(ref_fields: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[str, str]:
    """
    Balanced recall classifier.
    This is less pessimistic than the previous ultra-fast gate, but still requires
    bibliographic support before promoting a match to verified.
    """
    title_score = int(meta.get("title_score", 0))
    score = int(meta.get("score", 0))
    year_match = int(meta.get("year_match", 0))
    year_delta = int(meta.get("year_delta", 999))
    author_overlap = int(meta.get("author_overlap", 0))
    author_similarity = int(meta.get("author_similarity", 0))
    journal_score = int(meta.get("journal_score", 0))
    doi_match = bool(meta.get("doi_match"))
    source_agreement = int(meta.get("source_agreement", 1))
    retracted = bool(meta.get("is_retracted"))

    has_ref_authors = bool(ref_fields.get("authors"))
    has_cand_authors = bool(meta.get("authors"))
    has_author_conflict = bool(has_ref_authors and has_cand_authors and author_overlap == 0)
    has_ref_journal = bool(ref_fields.get("journal"))

    author_ok = author_overlap >= 1 or author_similarity >= 72 or not has_ref_authors or not has_cand_authors
    year_ok = year_match == 1 or year_delta <= 1
    journal_ok = journal_score >= 68 or not has_ref_journal
    cross_source_ok = source_agreement >= 2

    if retracted:
        return "needs_review", "Matched record appears to be retracted and needs manual review."

    if doi_match:
        if title_score >= 60 or year_match or author_overlap >= 1:
            return "verified", "Exact DOI match with acceptable title, year, or author support."
        if title_score >= 45:
            return "likely", "DOI matches, but title evidence is weak."
        return "needs_review", "DOI matches, but title evidence conflicts with the reference."

    # Do not let imperfect author extraction block strong title/year/journal evidence.
    if has_author_conflict:
        if title_score >= 94 and year_ok and (journal_score >= 60 or cross_source_ok):
            return "verified", "Very strong title and year evidence despite author-name extraction mismatch."
        if title_score >= 88 and year_ok and journal_score >= 70:
            return "verified", "Strong title, year and journal evidence despite author-name extraction mismatch."
        if title_score >= 82 and year_ok:
            return "likely", "Strong title and year evidence, but author names do not overlap."
        if title_score >= 70:
            return "needs_review", "Possible match found, but author names do not overlap."
        return "not_found", "Candidates were found, but author and title evidence were too weak."

    if title_score >= 92 and year_ok and (author_ok or journal_ok or cross_source_ok):
        return "verified", "Very strong title match with supporting year and bibliographic evidence."

    if title_score >= 88 and year_ok and (author_overlap >= 1 or author_similarity >= 65 or journal_score >= 60 or cross_source_ok):
        return "verified", "Strong title match with year and author, journal, or cross-source support."

    if VERIFY_RECALL_PROMOTE_STRONG_LIKELY and title_score >= 86 and year_match and (author_ok or journal_score >= 65 or cross_source_ok):
        return "verified", "Strong title and year evidence promoted under recall-boost verification."

    if score >= 86 and title_score >= 84 and year_ok and (author_ok or journal_ok or cross_source_ok):
        return "verified", "High composite score with title, year and supporting evidence."

    if title_score >= 82 and year_ok and (author_ok or journal_ok or cross_source_ok):
        return "likely", "Good title and year evidence, but not enough support for verified."

    if title_score >= 78 and (year_ok or author_overlap >= 1 or journal_score >= 70):
        return "likely", "Moderate-to-strong bibliographic evidence."

    if score >= 72 and title_score >= 72:
        return "likely", "Moderate composite evidence from retrieved metadata."

    if title_score >= 62 or score >= 50:
        return "needs_review", "Possible match found, but evidence is insufficient for automatic verification."

    return "not_found", "No reliable metadata match found after recall-boost queries."


# ============================================================
# LEAN SPEED + COVERAGE OVERRIDE v2
# Added to fix slow verification that does not improve verified rate.
# This final block intentionally overrides earlier ultra/recall/multisource
# runners while preserving the public API and result schema.
# ============================================================

VERIFY_LEAN_FAST_MODE = _env_flag("VERIFY_LEAN_FAST_MODE", "1")
VERIFY_LEAN_ROWS = int(os.getenv("VERIFY_LEAN_ROWS", "6"))
VERIFY_LEAN_CROSSREF_TEXT_QUERIES = int(os.getenv("VERIFY_LEAN_CROSSREF_TEXT_QUERIES", "2"))
VERIFY_LEAN_OPENALEX_TEXT_QUERIES = int(os.getenv("VERIFY_LEAN_OPENALEX_TEXT_QUERIES", "1"))
VERIFY_LEAN_MAX_API_CALLS = int(os.getenv("VERIFY_LEAN_MAX_API_CALLS", "4"))
VERIFY_LEAN_USE_SPECIAL_FALLBACK = _env_flag("VERIFY_LEAN_USE_SPECIAL_FALLBACK", "0")
VERIFY_LEAN_SPECIAL_FALLBACK_ONLY_IF_EMPTY = _env_flag("VERIFY_LEAN_SPECIAL_FALLBACK_ONLY_IF_EMPTY", "1")
VERIFY_LEAN_STORE_EVERY = int(os.getenv("VERIFY_LEAN_STORE_EVERY", "3"))
VERIFY_LEAN_WORKERS = int(os.getenv("VERIFY_LEAN_WORKERS", os.getenv("VERIFY_INNER_THREADS", "3")))
VERIFY_LEAN_DISABLE_ENRICH_METADATA = _env_flag("VERIFY_LEAN_DISABLE_ENRICH_METADATA", "1")
VERIFY_LEAN_PROMOTE_STRONG_TITLE_YEAR = _env_flag("VERIFY_LEAN_PROMOTE_STRONG_TITLE_YEAR", "1")
VERIFY_LEAN_DEBUG = _env_flag("VERIFY_LEAN_DEBUG", "0")

# Hard speed guards. These override previous broad settings at runtime.
VERIFY_DEEP_FALLBACK = _env_flag("VERIFY_DEEP_FALLBACK", "0")
VERIFY_RETRY_FAILED = _env_flag("VERIFY_RETRY_FAILED", "0")
VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES = int(os.getenv("VERIFY_MULTISOURCE_MAX_EXTRA_SOURCES", "1"))
VERIFY_SPECIAL_ROWS = min(int(os.getenv("VERIFY_SPECIAL_ROWS", "2")), 2)
VERIFY_SPECIAL_TIMEOUT = min(int(os.getenv("VERIFY_SPECIAL_TIMEOUT", "2")), 2)


def _lean_debug(msg: str) -> None:
    if VERIFY_LEAN_DEBUG:
        print(f"[LEAN_VERIFY] {msg}")


def _lean_sort_queries(queries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Prefer high-signal queries and avoid loose title-only searches."""
    if not queries:
        return []

    preferred = {
        "crossref_doi_exact": 0,
        "crossref_full_bibliographic": 1,
        "crossref_rich_bibliographic": 2,
        "crossref_title_author_year": 3,
        "openalex_doi_exact": 0,
        "openalex_title_year": 1,
        "openalex_title_only": 4,
    }

    def rank(q: Dict[str, Any]) -> Tuple[int, int]:
        name = q.get("name", "")
        return (preferred.get(name, 50), int(q.get("priority", 99)))

    return sorted(queries, key=rank)


def _lean_select_non_doi(queries: List[Dict[str, Any]], max_text: int) -> List[Dict[str, Any]]:
    """Keep DOI query plus a small number of high-signal text queries."""
    out: List[Dict[str, Any]] = []
    text_count = 0
    for q in _lean_sort_queries(queries):
        if q.get("mode") == "doi_exact":
            out.append(q)
            continue
        name = q.get("name", "")
        # Avoid very loose title-only searches in the normal path.
        if "title_only" in name and text_count > 0:
            continue
        if text_count >= max(0, max_text):
            continue
        out.append(q)
        text_count += 1
    return out


def _lean_best_meta(fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        _best, meta, _alts = _best_candidate(fields, _dedupe_candidates(candidates or []))
        return meta or {}
    except Exception:
        return {}


def _lean_is_verified_like(fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> bool:
    meta = _lean_best_meta(fields, candidates)
    if not meta:
        return False
    status, _reason = _classify_from_meta(fields, meta)
    return status == "verified"


def _lean_is_good_enough(fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> bool:
    meta = _lean_best_meta(fields, candidates)
    if not meta:
        return False
    status, _reason = _classify_from_meta(fields, meta)
    if status == "verified":
        return True
    title_score = int(meta.get("title_score", 0))
    year_delta = int(meta.get("year_delta", 999))
    return status == "likely" and title_score >= 82 and year_delta <= 1


def _run_query_plan(plan: Dict[str, Any], use_crossref: bool, use_openalex: bool) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """
    Lean speed + coverage query runner.

    Default budget:
      1. Crossref DOI if DOI exists.
      2. Crossref full bibliographic query.
      3. Crossref rich/title-author-year query.
      4. OpenAlex title-year fallback only when Crossref is not already useful.

    This avoids slow multi-source loops that did not improve the verified count.
    """
    candidates: List[Dict[str, Any]] = []
    query_used: List[str] = []
    query_strategy: List[str] = []
    fields = plan.get("fields", {}) or {}
    openalex_allowed = bool(use_openalex or VERIFY_FORCE_OPENALEX_FALLBACK)
    api_calls = 0
    max_calls = max(1, VERIFY_LEAN_MAX_API_CALLS)

    def budget_left() -> bool:
        return api_calls < max_calls

    # Crossref first. It gives the highest precision for most journal articles.
    for q in _lean_select_non_doi(plan.get("crossref_queries", []), VERIFY_LEAN_CROSSREF_TEXT_QUERIES):
        if not use_crossref or not budget_left():
            break
        name = q.get("name", "crossref")
        try:
            if q.get("mode") == "doi_exact":
                res = _query_crossref_by_doi(q.get("doi", ""))
            else:
                res = _query_crossref_bibliographic(
                    q.get("query_bibliographic", ""),
                    query_author=q.get("query_author", ""),
                    rows=min(max(3, VERIFY_LEAN_ROWS), max(3, int(q.get("rows", VERIFY_CROSSREF_ROWS)))),
                    query_name=name,
                )
            api_calls += 1
            if res:
                candidates.extend(res)
            query_strategy.append(name)
            query_used.append(q.get("doi") or q.get("query_bibliographic") or "")

            # DOI match or verified-level candidate, stop early.
            if _lean_is_verified_like(fields, candidates):
                return _dedupe_candidates(candidates), query_used, query_strategy
        except Exception as exc:
            api_calls += 1
            _lean_debug(f"Crossref query failed for {name}: {exc}")

    # OpenAlex only if Crossref has not already produced a good candidate.
    if openalex_allowed and budget_left() and not _lean_is_good_enough(fields, candidates):
        for q in _lean_select_non_doi(plan.get("openalex_queries", []), VERIFY_LEAN_OPENALEX_TEXT_QUERIES):
            if not budget_left():
                break
            name = q.get("name", "openalex")
            try:
                if q.get("mode") == "doi_exact":
                    # Avoid duplicate DOI lookup when Crossref DOI already produced candidates.
                    if candidates and fields.get("doi"):
                        continue
                    res = _query_openalex_by_doi(q.get("doi", ""))
                else:
                    res = _query_openalex_search(
                        q.get("search", ""),
                        rows=min(max(3, VERIFY_LEAN_ROWS), max(3, int(q.get("rows", VERIFY_OPENALEX_ROWS)))),
                        publication_year=q.get("publication_year", ""),
                        query_name=name,
                    )
                api_calls += 1
                if res:
                    candidates.extend(res)
                query_strategy.append(name)
                query_used.append(q.get("doi") or q.get("search") or "")
                if _lean_is_verified_like(fields, candidates):
                    return _dedupe_candidates(candidates), query_used, query_strategy
            except Exception as exc:
                api_calls += 1
                _lean_debug(f"OpenAlex query failed for {name}: {exc}")

    # Special fallback is off by default. It was the main speed cost with little gain.
    # Enable VERIFY_LEAN_USE_SPECIAL_FALLBACK=1 only after the core engine is stable.
    if VERIFY_LEAN_USE_SPECIAL_FALLBACK and budget_left():
        if (not VERIFY_LEAN_SPECIAL_FALLBACK_ONLY_IF_EMPTY) or not candidates:
            for q in _prioritise_fallback_queries(plan.get("fallback_queries", []), fields, candidates)[:1]:
                source = q.get("source", "")
                if not _source_enabled(source):
                    continue
                try:
                    res = _run_fallback_query(q, fields)
                    api_calls += 1
                    if res:
                        candidates.extend(res)
                    query_strategy.append(q.get("name", source))
                    query_used.append(_generic_metadata_query(fields) or fields.get("doi", ""))
                    break
                except Exception as exc:
                    api_calls += 1
                    _lean_debug(f"Fallback failed for {q.get('name', source)}: {exc}")

    return _dedupe_candidates(candidates), query_used, query_strategy


def _classify_from_meta(ref_fields: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[str, str]:
    """
    Lean commercial classifier.

    The previous settings made the system slower without increasing verified count.
    This classifier relies on high-signal bibliographic evidence and avoids harsh
    author downgrades when title/year/journal evidence is strong.
    """
    title_score = int(meta.get("title_score", 0))
    score = int(meta.get("score", 0))
    year_match = int(meta.get("year_match", 0))
    year_delta = int(meta.get("year_delta", 999))
    author_overlap = int(meta.get("author_overlap", 0))
    author_similarity = int(meta.get("author_similarity", 0))
    journal_score = int(meta.get("journal_score", 0))
    doi_match = bool(meta.get("doi_match"))
    source_agreement = int(meta.get("source_agreement", 1))
    retracted = bool(meta.get("is_retracted"))

    has_ref_authors = bool(ref_fields.get("authors"))
    has_cand_authors = bool(meta.get("authors"))
    has_ref_journal = bool(ref_fields.get("journal"))
    author_conflict = bool(has_ref_authors and has_cand_authors and author_overlap == 0 and author_similarity < 60)

    year_ok = year_match == 1 or year_delta <= 1
    author_ok = author_overlap >= 1 or author_similarity >= 70 or not has_ref_authors or not has_cand_authors
    journal_ok = journal_score >= 60 or not has_ref_journal
    cross_source_ok = source_agreement >= 2

    if retracted:
        return "needs_review", "Matched record appears to be retracted and needs manual review."

    if doi_match:
        if title_score >= 55 or year_ok or author_overlap >= 1:
            return "verified", "Exact DOI match with acceptable bibliographic support."
        return "likely", "DOI matches, but title evidence is weak."

    # Do not let imperfect author extraction suppress a strong bibliographic match.
    if VERIFY_LEAN_PROMOTE_STRONG_TITLE_YEAR and title_score >= 90 and year_ok:
        if journal_ok or author_ok or cross_source_ok:
            return "verified", "Strong title and year evidence with supporting bibliographic signal."
        return "likely", "Strong title and year evidence, but support is incomplete."

    if title_score >= 86 and year_ok and (author_ok or journal_score >= 55 or cross_source_ok):
        return "verified", "Strong title-year match with author, journal, or cross-source support."

    if title_score >= 82 and year_ok and not author_conflict and (journal_ok or author_ok or cross_source_ok):
        return "verified", "Good title-year match with no serious bibliographic conflict."

    if title_score >= 80 and year_ok:
        return "likely", "Good title and year evidence, but not enough support for automatic verification."

    if title_score >= 76 and (author_overlap >= 1 or journal_score >= 65 or year_ok):
        return "likely", "Moderate-to-strong bibliographic evidence."

    if score >= 70 and title_score >= 70:
        return "likely", "Moderate composite metadata evidence."

    if title_score >= 58 or score >= 48:
        return "needs_review", "Possible match found, but evidence is insufficient for automatic verification."

    return "not_found", "No reliable metadata match found from lean verification queries."


# Simple fast batch runner. It updates progress after every completed reference and
# does not run extra retry loops. This keeps the frontend from sitting at 0/N.
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
    normalized_style = _STYLE_ALIASES.get((style or "apa").lower(), "apa")
    total = len(refs)

    print(f"[DEBUG] LEAN verify_references_batch called: {total} references")

    if job_id:
        try:
            update_job_progress(job_id, 0)
            store_verification_results(job_id, [])
        except Exception:
            pass

    if not refs:
        return []

    rows: List[Optional[Dict[str, Any]]] = [None] * total
    completed = 0
    workers = max(1, min(VERIFY_LEAN_WORKERS, total))
    start = time.time()

    def run_one(idx_ref: Tuple[int, str]) -> Tuple[int, Dict[str, Any]]:
        idx, ref = idx_ref
        try:
            row = _verify_single_reference(
                ref,
                normalized_style,
                use_crossref,
                use_openalex,
                False if VERIFY_LEAN_DISABLE_ENRICH_METADATA else enrich_metadata,
            )
            return idx, row
        except Exception as exc:
            return idx, {
                "reference": ref,
                "style": normalized_style,
                "status": "not_found",
                "source": "error",
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
                "error": str(exc),
            }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(run_one, item): item[0] for item in enumerate(refs)}
        for fut in as_completed(future_map):
            idx, row = fut.result()
            row["status"] = _normalize_verify_status(row.get("status"))
            rows[idx] = row
            completed += 1

            if job_id:
                try:
                    update_job_progress(job_id, completed)
                    if completed % max(1, VERIFY_LEAN_STORE_EVERY) == 0 or completed == total:
                        store_verification_results(job_id, [r for r in rows if r is not None])
                except Exception:
                    pass

            if throttle_s:
                time.sleep(throttle_s)

    final_rows = [r for r in rows if r is not None]
    counts = {
        "verified": sum(1 for r in final_rows if r.get("status") == "verified"),
        "likely": sum(1 for r in final_rows if r.get("status") == "likely"),
        "needs_review": sum(1 for r in final_rows if r.get("status") == "needs_review"),
        "not_found": sum(1 for r in final_rows if r.get("status") == "not_found"),
    }
    print(f"[DEBUG] LEAN verification complete in {time.time() - start:.1f}s: {counts}")

    if job_id:
        try:
            update_job_progress(job_id, total)
            store_verification_results(job_id, final_rows)
        except Exception:
            pass

    return final_rows


# Legacy scorer export, kept for citation_suggester.py imports.
def _score(
    ref_title: str,
    ref_authors: list = None,
    ref_year: str = "",
    cand_title: str = "",
    cand_authors: list = None,
    cand_year: str = "",
) -> dict:
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

# ============================================================
# CROSSREF FINGERPRINT QUERY ORDER OVERRIDE v3
# Added to improve Crossref success without increasing rows/timeouts.
# Public API is unchanged. This makes the first Crossref text query:
#   title core keywords + first author + year
# Example:
#   sample size determination survey research adam 2020
# ============================================================

try:
    _previous_build_verification_query_plan_fingerprint = _build_verification_query_plan
except Exception:
    _previous_build_verification_query_plan_fingerprint = None


def _fingerprint_title_key_for_crossref(title: str, limit: int = 8) -> str:
    """Compact high-signal title key for Crossref query.bibliographic.

    This deliberately keeps words such as "research" when they are part of
    title phrases like "survey research". It avoids the broader query stopword
    list because that list can remove useful title fingerprints.
    """
    title = _clean_query_text(title or "") if "_clean_query_text" in globals() else _safe_strip(title or "")
    raw_words = re.findall(r"[A-Za-z0-9]{3,}", title)

    # Smaller stoplist for fingerprint queries. Keep research, survey, sample,
    # determination, etc., because they can distinguish a specific title.
    fingerprint_stop = {
        "and", "the", "with", "from", "into", "using", "that", "this",
        "their", "these", "those", "among", "across", "journal",
        "article", "paper", "available", "retrieved", "accessed",
        "press", "university", "publisher", "page", "pages",
    }

    selected = []
    for w in raw_words:
        wl = _safe_strip(w).lower()
        if not wl or wl in fingerprint_stop:
            continue
        selected.append(wl)
        if len(selected) >= limit:
            break
    return " ".join(selected).strip()


def _build_crossref_fingerprint_query(fields: Dict[str, Any], include_journal: bool = False) -> str:
    """
    Build compact Crossref query.

    Preferred example:
        Adam, A. M. (2020). Sample size determination in survey research.
    becomes:
        sample size determination survey research adam 2020
    """
    title = fields.get("title", "") or ""
    authors = fields.get("authors", []) or []
    year = fields.get("year", "") or ""
    journal = fields.get("journal", "") or fields.get("container_title", "") or fields.get("source", "") or ""

    first_author = _safe_strip(authors[0]) if authors else ""
    title_key = _fingerprint_title_key_for_crossref(title, limit=8)

    if include_journal:
        journal_words = _significant_title_words(journal, limit=6)
        journal_key = " ".join(journal_words[:5]).strip()
        parts = [title_key, journal_key, year]
    else:
        parts = [title_key, first_author, year]

    query = " ".join([_safe_strip(p) for p in parts if _safe_strip(p)]).strip()
    query = re.sub(r"\s+", " ", query)
    return query


def _dedupe_query_plan_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for q in items or []:
        key = (
            q.get("name", ""),
            q.get("mode", ""),
            q.get("doi", ""),
            q.get("query_bibliographic", ""),
            q.get("query_author", ""),
            q.get("search", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def _build_verification_query_plan(ref: str, style: str) -> Dict[str, Any]:
    """
    Override the previous plan by putting Crossref fingerprint searches first.
    This improves success rate more than adding rows or more databases.
    """
    if _previous_build_verification_query_plan_fingerprint:
        plan = _previous_build_verification_query_plan_fingerprint(ref, style)
    else:
        # Safe fallback, should rarely be used.
        fields = _extract_fields_by_style(ref, style)
        plan = {"reference": ref, "style": style, "fields": fields, "crossref_queries": [], "openalex_queries": [], "fallback_queries": []}

    fields = plan.get("fields", {}) or {}
    authors = fields.get("authors", []) or []
    first_author = _safe_strip(authors[0]) if authors else ""
    year = _safe_strip(fields.get("year", "") or "")
    doi = _normalise_doi(fields.get("doi", "") or "") if "_normalise_doi" in globals() else _safe_strip(fields.get("doi", ""))
    title = fields.get("title", "") or ""
    journal = fields.get("journal", "") or fields.get("container_title", "") or fields.get("source", "") or ""

    old_crossref = plan.get("crossref_queries", []) or []
    doi_queries = [q for q in old_crossref if q.get("mode") == "doi_exact"]
    old_text = [q for q in old_crossref if q.get("mode") != "doi_exact"]

    # Ensure DOI query exists and remains first when DOI is available.
    if doi and not any(q.get("mode") == "doi_exact" and _safe_strip(q.get("doi")) == doi for q in doi_queries):
        doi_queries.insert(0, {"name": "crossref_doi_exact", "mode": "doi_exact", "doi": doi, "priority": 0})

    fingerprint = _build_crossref_fingerprint_query(fields, include_journal=False)
    journal_fingerprint = _build_crossref_fingerprint_query(fields, include_journal=True)

    new_text: List[Dict[str, Any]] = []

    if fingerprint:
        new_text.append({
            "name": "crossref_fingerprint_title_author_year",
            "mode": "bibliographic",
            "query_bibliographic": fingerprint,
            "query_author": first_author,
            "rows": VERIFY_CROSSREF_ROWS,
            "priority": 1,
        })

    if journal_fingerprint and journal:
        new_text.append({
            "name": "crossref_fingerprint_title_journal_year",
            "mode": "bibliographic",
            "query_bibliographic": journal_fingerprint,
            "query_author": "",
            "rows": VERIFY_CROSSREF_ROWS,
            "priority": 2,
        })

    # Keep the older title-author query, but repair it by adding the first author
    # inside query.bibliographic, not only in query.author.
    for q in old_text:
        name = q.get("name", "")
        if name == "crossref_title_author_year":
            repaired = dict(q)
            repaired["name"] = "crossref_title_author_year_repaired"
            old_bib = _safe_strip(repaired.get("query_bibliographic", ""))
            if first_author and first_author.lower() not in old_bib.lower().split():
                old_bib = " ".join([old_bib, first_author]).strip()
            if year and year not in old_bib:
                old_bib = " ".join([old_bib, year]).strip()
            repaired["query_bibliographic"] = re.sub(r"\s+", " ", old_bib).strip()
            repaired["query_author"] = first_author
            repaired["priority"] = 3
            new_text.append(repaired)
            break

    # Full and rich bibliographic are useful fallbacks but should not run before
    # the compact fingerprint in lean mode.
    for wanted_name, priority in [
        ("crossref_full_bibliographic", 4),
        ("crossref_rich_bibliographic", 5),
        ("crossref_title_journal_year", 6),
    ]:
        for q in old_text:
            if q.get("name") == wanted_name:
                qq = dict(q)
                qq["priority"] = priority
                new_text.append(qq)
                break

    # Preserve any other Crossref query as last-resort, but after the new order.
    used_names = {q.get("name") for q in new_text}
    for q in old_text:
        if q.get("name") in used_names:
            continue
        qq = dict(q)
        qq["priority"] = int(qq.get("priority", 99)) + 20
        new_text.append(qq)

    plan["crossref_queries"] = _dedupe_query_plan_items(doi_queries + sorted(new_text, key=lambda q: q.get("priority", 99)))
    plan.setdefault("fields", fields)
    plan["fields"]["crossref_fingerprint_query"] = fingerprint
    plan["fields"]["crossref_journal_fingerprint_query"] = journal_fingerprint
    return plan


def _lean_sort_queries(queries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Final lean order, fingerprint first after DOI."""
    if not queries:
        return []

    preferred = {
        "crossref_doi_exact": 0,
        "crossref_fingerprint_title_author_year": 1,
        "crossref_fingerprint_title_journal_year": 2,
        "crossref_title_author_year_repaired": 3,
        "crossref_title_author_year": 4,
        "crossref_full_bibliographic": 5,
        "crossref_rich_bibliographic": 6,
        "crossref_title_journal_year": 7,
        "openalex_doi_exact": 0,
        "openalex_title_year": 1,
        "openalex_title_journal": 2,
        "openalex_title_only": 5,
    }

    def rank(q: Dict[str, Any]) -> Tuple[int, int]:
        name = q.get("name", "")
        return (preferred.get(name, 50), int(q.get("priority", 99)))

    return sorted(queries, key=rank)


# Also support recall mode if enabled instead of lean mode.
def _recall_select_crossref_queries(queries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(queries or [], key=lambda q: q.get("priority", 99))
    doi_queries = [q for q in ordered if q.get("mode") == "doi_exact"]
    text_queries = [q for q in ordered if q.get("mode") != "doi_exact"]

    preferred = [
        "crossref_fingerprint_title_author_year",
        "crossref_fingerprint_title_journal_year",
        "crossref_title_author_year_repaired",
        "crossref_title_author_year",
        "crossref_full_bibliographic",
        "crossref_rich_bibliographic",
        "crossref_title_journal_year",
    ]

    selected: List[Dict[str, Any]] = []
    for name in preferred:
        for q in text_queries:
            if _safe_strip(q.get("name")) == name and q not in selected:
                selected.append(q)
                break
        if len(selected) >= VERIFY_RECALL_CROSSREF_TEXT_QUERIES:
            break

    for q in text_queries:
        if len(selected) >= VERIFY_RECALL_CROSSREF_TEXT_QUERIES:
            break
        if q not in selected:
            selected.append(q)

    return doi_queries + selected[:VERIFY_RECALL_CROSSREF_TEXT_QUERIES]

try:
    if "__all__" in globals():
        for name in ["_build_crossref_fingerprint_query", "_build_verification_query_plan"]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass


# ============================================================
# FINGERPRINT BALANCED QUERY + PROMOTION OVERRIDE v4
# Added after testing showed validation plateaued at 9 instead of the
# previous 18. The earlier fingerprint patch worked, but in lean mode the
# second Crossref call was often the journal fingerprint. Journal extraction is
# noisy, so it wasted the second query. This final override keeps the fast
# fingerprint first, then tries the repaired author-year and/or full reference
# before journal-heavy queries. It also avoids stopping on a weak "likely"
# Crossref candidate before OpenAlex can provide source agreement.
# ============================================================


VERIFY_BALANCED_OPENALEX_ON_LIKELY = _env_flag("VERIFY_BALANCED_OPENALEX_ON_LIKELY", "1")


def _lean_sort_queries(queries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Balanced final lean order.

    Order is designed for high Crossref hit-rate without more calls:
      1. DOI exact
      2. compact title + first author + year
      3. repaired title + first author + year
      4. full bibliographic reference
      5. rich bibliographic
      6. title + journal + year
      7. journal fingerprint
    """
    if not queries:
        return []

    preferred = {
        "crossref_doi_exact": 0,
        "crossref_fingerprint_title_author_year": 1,
        "crossref_title_author_year_repaired": 2,
        "crossref_title_author_year": 3,
        "crossref_full_bibliographic": 4,
        "crossref_rich_bibliographic": 5,
        "crossref_title_journal_year": 6,
        "crossref_fingerprint_title_journal_year": 7,
        "openalex_doi_exact": 0,
        "openalex_title_year": 1,
        "openalex_title_journal": 2,
        "openalex_title_only": 5,
    }

    def rank(q: Dict[str, Any]) -> Tuple[int, int]:
        return (preferred.get(_safe_strip(q.get("name", "")), 50), int(q.get("priority", 99)))

    return sorted(queries or [], key=rank)


def _recall_select_crossref_queries(queries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Recall selector using the same balanced Crossref order as lean mode."""
    ordered = _lean_sort_queries(queries or [])
    doi_queries = [q for q in ordered if q.get("mode") == "doi_exact"]
    text_queries = [q for q in ordered if q.get("mode") != "doi_exact"]
    selected = text_queries[:max(0, VERIFY_RECALL_CROSSREF_TEXT_QUERIES)]
    return doi_queries + selected


def _lean_is_good_enough(fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> bool:
    """Do not stop on ordinary likely matches.

    The previous lean gate skipped OpenAlex when Crossref returned a likely
    candidate. That kept many good records at likely instead of verified. Stop
    only for verified, or for extremely strong likely evidence.
    """
    meta = _lean_best_meta(fields, candidates)
    if not meta:
        return False
    status, _reason = _classify_from_meta(fields, meta)
    if status == "verified":
        return True
    if not VERIFY_BALANCED_OPENALEX_ON_LIKELY:
        title_score = int(meta.get("title_score", 0))
        year_delta = int(meta.get("year_delta", 999))
        return status == "likely" and title_score >= 90 and year_delta <= 1
    return False


def _classify_from_meta(ref_fields: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[str, str]:
    """Balanced classifier to recover valid references without making searches slower."""
    title_score = int(meta.get("title_score", 0))
    score = int(meta.get("score", 0))
    year_match = int(meta.get("year_match", 0))
    year_delta = int(meta.get("year_delta", 999))
    author_overlap = int(meta.get("author_overlap", 0))
    author_similarity = int(meta.get("author_similarity", 0))
    journal_score = int(meta.get("journal_score", 0))
    doi_match = bool(meta.get("doi_match"))
    source_agreement = int(meta.get("source_agreement", 1))
    retracted = bool(meta.get("is_retracted"))

    has_ref_authors = bool(ref_fields.get("authors"))
    has_cand_authors = bool(meta.get("authors"))
    author_conflict = bool(has_ref_authors and has_cand_authors and author_overlap == 0 and author_similarity < 55)

    year_ok = year_match == 1 or year_delta <= 1
    author_ok = author_overlap >= 1 or author_similarity >= 68 or not has_ref_authors or not has_cand_authors
    journal_ok = journal_score >= 58
    cross_source_ok = source_agreement >= 2

    if retracted:
        return "needs_review", "Matched record appears to be retracted and needs manual review."

    if doi_match:
        if title_score >= 50 or year_ok or author_overlap >= 1:
            return "verified", "Exact DOI match with acceptable bibliographic support."
        return "likely", "DOI matches, but title evidence is weak."

    # Author extraction is often the weak point. Only treat it as blocking when
    # title/year evidence is not strong enough.
    if author_conflict:
        if title_score >= 92 and year_ok and (journal_ok or cross_source_ok):
            return "verified", "Very strong title-year evidence with journal or cross-source support despite author mismatch."
        if title_score >= 86 and year_ok and journal_score >= 70:
            return "verified", "Strong title, year and journal evidence despite author mismatch."
        if title_score >= 80 and year_ok:
            return "likely", "Strong title-year evidence, but author names do not overlap."
        if title_score >= 65 or score >= 55:
            return "needs_review", "Possible match found, but author names do not overlap."
        return "not_found", "Candidates were found, but author and title evidence were too weak."

    if VERIFY_BALANCED_PROMOTION and title_score >= VERIFY_BALANCED_VERIFY_TITLE_YEAR and year_ok:
        return "verified", "Strong title and year evidence."

    if title_score >= VERIFY_BALANCED_VERIFY_TITLE_YEAR_SUPPORT and year_ok and (author_ok or journal_ok or cross_source_ok):
        return "verified", "Good title-year match with supporting author, journal, or source evidence."

    if title_score >= 82 and year_match == 1 and (author_ok or journal_ok or cross_source_ok):
        return "verified", "Good title and exact year evidence with supporting metadata."

    if score >= 84 and title_score >= 80 and year_ok and (author_ok or journal_ok or cross_source_ok):
        return "verified", "High composite bibliographic evidence."

    if title_score >= 78 and year_ok:
        return "likely", "Good title and year evidence, but not enough support for automatic verification."

    if title_score >= 74 and (author_overlap >= 1 or journal_score >= 65 or year_ok):
        return "likely", "Moderate-to-strong bibliographic evidence."

    if score >= 68 and title_score >= 68:
        return "likely", "Moderate composite metadata evidence."

    if title_score >= 56 or score >= 45:
        return "needs_review", "Possible match found, but evidence is insufficient for automatic verification."

    return "not_found", "No reliable metadata match found from balanced verification queries."

try:
    if "__all__" in globals():
        for name in ["_lean_sort_queries", "_recall_select_crossref_queries", "_classify_from_meta"]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass


# ============================================================
# DOI EXTRACTION + DOI-AWARE SCORE BOOST OVERRIDE v5
# Added to increase validation without increasing API calls.
# Key changes:
#   1. Better DOI extraction from raw references.
#   2. Candidate DOI presence boosts score only when title/year/author evidence supports it.
#   3. Exact DOI match verifies with modest title support.
#   4. Weak OpenAlex/Crossref candidates are not promoted just because a DOI exists.
# ============================================================

_DOI_RE = re.compile(
    r"(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)?"
    r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
    re.I,
)


def _extract_doi(text: str) -> str:
    """Extract and normalise DOI from common reference formats.

    Handles:
      doi:10.xxxx/xxxxx
      https://doi.org/10.xxxx/xxxxx
      http://dx.doi.org/10.xxxx/xxxxx
      bare DOI strings with trailing punctuation.
    """
    text = _safe_str(text)
    m = _DOI_RE.search(text or "")
    if not m:
        return ""

    doi = _safe_strip(m.group(1))
    doi = doi.rstrip(".,;:)]}>'\"")
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi\s*:\s*", "", doi, flags=re.I)
    return doi.lower()


def _score_candidate(ref_fields: Dict[str, Any], cand: Dict[str, Any]) -> Dict[str, Any]:
    """DOI-aware candidate scoring.

    This replaces the earlier score with a safer DOI boost:
    - exact DOI match gives a strong boost;
    - candidate DOI found gives a smaller boost only when title/year/author supports it;
    - weak candidates are not promoted merely because they have a DOI.
    """
    cf = _candidate_fields(cand)

    ref_title = _norm_text(ref_fields.get("title", ""))
    cand_title = _norm_text(cf.get("title", ""))

    if ref_title and cand_title:
        token_set = fuzz.token_set_ratio(ref_title, cand_title)
        token_sort = fuzz.token_sort_ratio(ref_title, cand_title)
        partial = fuzz.partial_ratio(ref_title, cand_title)
        title_score = int((token_set * 0.50) + (token_sort * 0.35) + (partial * 0.15))
        # For short titles, partial matching can overstate similarity.
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
    candidate_has_doi = bool(cand_doi)

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

    # DOI-aware boost.
    if doi_match:
        score += 30
    elif candidate_has_doi:
        # The candidate DOI is useful evidence, but only when the bibliographic
        # evidence is already reasonably strong.
        if title_score >= 85 and (year_match or year_delta <= 1):
            score += 12
        elif title_score >= 75 and (year_match or year_delta <= 1):
            score += 8
        elif title_score >= 75 and author_overlap >= 1:
            score += 6
        elif title_score >= 65 and (author_overlap >= 1 or journal_score >= 65):
            score += 4
        else:
            score += 2

    # Extra safe boosts for high-signal bibliographic support.
    if title_score >= 88 and (year_match or year_delta <= 1):
        score += 6
    if title_score >= 82 and author_overlap >= 1:
        score += 5
    if title_score >= 78 and journal_score >= 70:
        score += 4

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
        "candidate_has_doi": bool(candidate_has_doi),
        "reference_has_doi": bool(ref_doi),
        "volume_match": int(volume_match),
        "issue_match": int(issue_match),
        "page_match": int(page_match),
        "source_agreement": int(source_agreement),
        **cf,
    }


def _classify_from_meta(ref_fields: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[str, str]:
    """DOI-aware classifier.

    This classifier improves promotion for DOI-backed candidates while preserving
    guardrails for weak or generic matches.
    """
    title_score = int(meta.get("title_score", 0))
    score = int(meta.get("score", 0))
    year_match = int(meta.get("year_match", 0))
    year_delta = int(meta.get("year_delta", 999))
    author_overlap = int(meta.get("author_overlap", 0))
    author_similarity = int(meta.get("author_similarity", 0))
    journal_score = int(meta.get("journal_score", 0))
    doi_match = bool(meta.get("doi_match"))
    candidate_has_doi = bool(meta.get("candidate_has_doi") or meta.get("doi"))
    source_agreement = int(meta.get("source_agreement", 1))
    retracted = bool(meta.get("is_retracted"))

    has_ref_authors = bool(ref_fields.get("authors"))
    has_cand_authors = bool(meta.get("authors"))
    has_ref_journal = bool(ref_fields.get("journal"))

    year_ok = year_match == 1 or year_delta <= 1
    author_ok = author_overlap >= 1 or author_similarity >= 68 or not has_ref_authors or not has_cand_authors
    journal_ok = journal_score >= 60 or not has_ref_journal
    cross_source_ok = source_agreement >= 2
    author_conflict = bool(has_ref_authors and has_cand_authors and author_overlap == 0 and author_similarity < 55)

    if retracted:
        return "needs_review", "Matched record appears to be retracted and needs manual review."

    # Exact DOI match is the strongest available evidence.
    if doi_match:
        if title_score >= 55 or year_ok or author_overlap >= 1:
            return "verified", "Exact DOI match with acceptable bibliographic support."
        return "likely", "DOI matches, but title evidence is weak."

    # Candidate DOI found from a trusted metadata source.
    # Promote only when title/year or title/author support it.
    if candidate_has_doi and title_score >= 85 and year_ok:
        if not author_conflict or journal_ok or cross_source_ok:
            return "verified", "Candidate DOI found with strong title and year support."
        return "likely", "Candidate DOI and title-year evidence are strong, but author names do not overlap."

    if candidate_has_doi and title_score >= 82 and author_ok and (year_ok or journal_ok):
        return "verified", "Candidate DOI found with strong title and supporting author/year/journal evidence."

    # Strong non-DOI bibliographic evidence.
    if title_score >= 90 and year_ok and (author_ok or journal_ok or cross_source_ok):
        return "verified", "Strong title and year evidence with supporting bibliographic signal."

    if title_score >= 84 and year_ok and author_overlap >= 1:
        return "verified", "Strong title, year and author match."

    if score >= 88 and title_score >= 80 and year_ok and (author_ok or journal_ok or candidate_has_doi):
        return "verified", "High composite bibliographic score with title-year support."

    # Do not automatically verify weak/generic matches.
    if title_score >= 78 and (year_ok or author_ok or candidate_has_doi):
        return "likely", "Good bibliographic evidence, but not enough for automatic verification."

    if score >= 70 and title_score >= 65:
        return "likely", "Moderate composite metadata evidence."

    if title_score >= 55 or score >= 45:
        return "needs_review", "Possible match found, but evidence is insufficient for automatic verification."

    return "not_found", "No reliable metadata match found from DOI-aware verification queries."


def _classify(
    doi_match: bool,
    title_score: int,
    score: int,
    year_match: int,
    author_overlap: int = 0,
    candidate_has_doi: bool = False,
) -> str:
    """Backward-compatible wrapper for older internal calls."""
    meta = {
        "doi_match": bool(doi_match),
        "candidate_has_doi": bool(candidate_has_doi),
        "doi": "10.fake/placeholder" if candidate_has_doi else "",
        "title_score": int(title_score or 0),
        "score": int(score or 0),
        "year_match": int(year_match or 0),
        "year_delta": 0 if year_match else 999,
        "author_overlap": int(author_overlap or 0),
        "author_similarity": 80 if author_overlap else 0,
        "journal_score": 0,
        "source_agreement": 1,
        "authors": ["x"] if author_overlap else [],
    }
    ref_fields = {"authors": ["x"] if author_overlap else [], "journal": ""}
    return _classify_from_meta(ref_fields, meta)[0]

try:
    if "__all__" in globals():
        for name in ["_extract_doi", "_score_candidate", "_classify_from_meta", "_classify"]:
            if name not in __all__:
                __all__.append(name)
except Exception:
    pass

