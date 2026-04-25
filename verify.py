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

# Timeout settings (in seconds)
API_TIMEOUT = 120  # Increased from 60 to 120 seconds per API call
VERIFICATION_TIMEOUT = None  # No timeout for the overall verification
WORKER_THREADS = 2  # Reduce to 2 workers to avoid rate limiting and memory issues
RETRY_ATTEMPTS = 2  # Increase retries for failed API calls
BATCH_DELAY = 0.5  # Increase delay between references to avoid rate limits

# NEW: Chunk processing for large reference sets
CHUNK_SIZE = 50  # Process references in chunks of 50
CHUNK_DELAY = 10  # Delay between chunks to allow system to recover
MAX_RETRIES_PER_REFERENCE = 3  # Retry failed references up to 3 times
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
        timeout = API_TIMEOUT
    
    for attempt in range(3):  # Add retry loop
        try:
            headers = {
                "User-Agent": f"CitationCrosschecker/2.0 (mailto:{MAILTO})",
                "Accept": "application/json",
            }
            r = requests.get(url, params=params, timeout=timeout, headers=headers)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:  # Rate limited
                wait_time = (attempt + 1) * 5
                print(f"[DEBUG] Rate limited, waiting {wait_time} seconds...")
                time.sleep(wait_time)
                continue
            else:
                return None
        except requests.exceptions.Timeout:
            print(f"[DEBUG] Timeout on attempt {attempt + 1}, retrying...")
            if attempt < 2:
                time.sleep(2)
                continue
            return None
        except Exception as e:
            print(f"[DEBUG] API request failed: {e}")
            if attempt < 2:
                time.sleep(2)
                continue
            return None
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


def _classify(
    doi_match: bool,
    title_score: int,
    score: int,
    year_match: int,
    author_overlap: int = 0,
) -> str:

    # -------------------------------------------------
    # 1. STRICT VERIFIED (IDENTITY ONLY)
    # -------------------------------------------------

    # DOI must agree with strong title
    if doi_match and title_score >= 80:
        return "verified"

    # Near-exact title match (independent of DOI)
    if title_score >= 100:
        return "verified"

    # -------------------------------------------------
    # 2. LIKELY (STRONG BUT NOT EXACT)
    # -------------------------------------------------

    if title_score >= 85:
        return "likely"

    if score >= 85:
        return "likely"

    # -------------------------------------------------
    # 3. NEEDS REVIEW (SUSPICIOUS / PARTIAL MATCH)
    # -------------------------------------------------

    if title_score >= 75:
        return "needs_review"

    if score >= 60:
        return "needs_review"

    # DOI exists but title mismatch → suspicious
    if doi_match:
        return "needs_review"

    # -------------------------------------------------
    # 4. NOT FOUND
    # -------------------------------------------------

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
                int(best_meta.get("author_overlap", 0)),
            )

            # AUTHOR-MISMATCH GATE, first pass
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
        chunk_timeout = len(chunk) * (API_TIMEOUT / 2) + 300  # Add 5 minute buffer
        
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
                    result = future.result(timeout=60)  # 60 second timeout per reference
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
    
    # Retry failed references
    if failed_references:
        print(f"[DEBUG] Retrying {len(failed_references)} failed references...")
        
        for retry_idx, (original_idx, ref) in enumerate(failed_references):
            wait_time = min(30, (retry_idx + 1) * 5)
            print(f"[DEBUG] Waiting {wait_time}s before retry {retry_idx + 1}")
            time.sleep(wait_time)
            
            try:
                result = _verify_single_reference_with_retry(
                    ref, normalized_style, use_crossref, use_openalex, enrich_metadata,
                    max_retries=2
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
    max_retries: int = 3,
) -> Dict[str, Any]:
    """Single reference verification with retry logic"""
    last_error = None
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                wait_time = min(30, attempt * 10)  # Cap at 30 seconds
                print(f"[DEBUG] Retry attempt {attempt + 1}/{max_retries} for reference: {ref[:100]}... (waiting {wait_time}s)")
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
