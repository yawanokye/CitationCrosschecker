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

    if title_score >= 58:
        return "LIKELY"

    if score >= 45:
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
