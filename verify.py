import os
import re
import threading
import time
import uuid
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from collections import deque
import asyncio

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
# QUEUE SYSTEM FOR CITATION VERIFICATION
# ============================================================

@dataclass
class VerificationJob:
    """Represents a verification job"""
    job_id: str
    citations: List[str]
    style: str
    status: str = "pending"  # pending, processing, completed, failed
    progress: int = 0
    total: int = 0
    results: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    queue_position: int = 0

# Global job storage
_jobs: Dict[str, VerificationJob] = {}
_job_lock = threading.Lock()
_request_queue = deque()
_queue_lock = threading.Lock()
_processing = False

# Rate limiter for API calls
class RateLimiter:
    """Simple rate limiter to protect APIs"""
    def __init__(self, requests_per_second: float = 8):
        self.rate = requests_per_second
        self.tokens = requests_per_second
        self.last_update = time.time()
        self.lock = threading.Lock()
        self._waiting = 0
    
    def acquire(self) -> bool:
        """Acquire a token, returns True if available"""
        with self.lock:
            now = time.time()
            elapsed = now - self.last_update
            self.tokens += elapsed * self.rate
            self.tokens = min(self.tokens, self.rate)
            self.last_update = now
            
            if self.tokens < 1:
                return False
            
            self.tokens -= 1
            return True
    
    def wait_and_acquire(self, timeout: float = 30) -> bool:
        """Wait until a token is available"""
        start = time.time()
        while time.time() - start < timeout:
            if self.acquire():
                return True
            time.sleep(0.1)
        return False

# Global rate limiters
_openalex_limiter = RateLimiter(8)   # 8 req/sec safe margin
_crossref_limiter = RateLimiter(40)  # 40 req/sec safe margin

# Track API health
_api_health = {
    "openalex": {"success_rate": 1.0, "failures": 0, "last_check": None},
    "crossref": {"success_rate": 1.0, "failures": 0, "last_check": None}
}

# ============================================================
# QUEUE MANAGEMENT FUNCTIONS
# ============================================================

def get_queue_stats() -> Dict[str, Any]:
    """Get current queue statistics"""
    with _queue_lock:
        queue_size = len(_request_queue)
    
    with _job_lock:
        pending_jobs = sum(1 for j in _jobs.values() if j.status == "pending")
        processing_jobs = sum(1 for j in _jobs.values() if j.status == "processing")
    
    return {
        "queue_size": queue_size,
        "pending_jobs": pending_jobs,
        "processing_jobs": processing_jobs,
        "total_jobs": len(_jobs),
        "api_health": _api_health,
        "is_busy": queue_size > 50 or pending_jobs > 10
    }

def is_server_busy() -> bool:
    """Check if server is too busy to accept new jobs"""
    stats = get_queue_stats()
    # Busy if queue > 100 or pending > 20
    return stats["queue_size"] > 100 or stats["pending_jobs"] > 20

def submit_verification_job(citations: List[str], style: str = "apa") -> str:
    """Submit a verification job to the queue"""
    job_id = uuid.uuid4().hex
    
    with _job_lock:
        _jobs[job_id] = VerificationJob(
            job_id=job_id,
            citations=citations,
            style=style,
            total=len(citations)
        )
    
    with _queue_lock:
        _request_queue.append(job_id)
    
    # Start processing if not already running
    _start_worker_thread()
    
    return job_id

def _start_worker_thread():
    """Start background worker thread if not running"""
    global _processing
    if _processing:
        return
    
    _processing = True
    thread = threading.Thread(target=_process_queue_worker, daemon=True)
    thread.start()

def _process_queue_worker():
    """Background worker that processes verification jobs"""
    global _processing
    
    while True:
        try:
            # Get next job
            job_id = None
            with _queue_lock:
                if _request_queue:
                    job_id = _request_queue.popleft()
            
            if not job_id:
                time.sleep(0.5)
                continue
            
            # Process the job
            _process_job(job_id)
            
        except Exception as e:
            print(f"Queue worker error: {e}")
            time.sleep(1)
        
        # Check if queue is empty
        with _queue_lock:
            if not _request_queue:
                _processing = False
                break

def _process_job(job_id: str):
    """Process a single verification job"""
    with _job_lock:
        if job_id not in _jobs:
            return
        job = _jobs[job_id]
        job.status = "processing"
        job.started_at = datetime.now().isoformat()
    
    try:
        results = []
        total = len(job.citations)
        
        for i, citation in enumerate(job.citations):
            # Update progress
            with _job_lock:
                if job_id in _jobs:
                    _jobs[job_id].progress = i + 1
            
            # Verify citation with rate limiting
            result = _verify_single_citation_with_queue(
                citation, 
                job.style,
                _openalex_limiter,
                _crossref_limiter
            )
            results.append(result)
        
        with _job_lock:
            if job_id in _jobs:
                _jobs[job_id].results = results
                _jobs[job_id].status = "completed"
                _jobs[job_id].completed_at = datetime.now().isoformat()
                _jobs[job_id].progress = total
                
    except Exception as e:
        with _job_lock:
            if job_id in _jobs:
                _jobs[job_id].status = "failed"
                _jobs[job_id].error = str(e)
                _jobs[job_id].completed_at = datetime.now().isoformat()

def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Get status of a verification job"""
    with _job_lock:
        if job_id not in _jobs:
            return None
        
        job = _jobs[job_id]
        
        return {
            "job_id": job.job_id,
            "status": job.status,
            "progress": job.progress,
            "total": job.total,
            "percentage": int((job.progress / job.total) * 100) if job.total > 0 else 0,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "error": job.error,
            "results": job.results if job.status == "completed" else None
        }

def _verify_single_citation_with_queue(
    citation: str, 
    style: str, 
    openalex_limiter: RateLimiter,
    crossref_limiter: RateLimiter
) -> Dict[str, Any]:
    """Verify single citation with queue-based rate limiting"""
    
    # First try OpenAlex (better quality)
    openalex_available = openalex_limiter.wait_and_acquire(timeout=5)
    
    if openalex_available:
        result = _verify_with_openalex(citation, style)
        if result and result.get("score", 0) >= 70:
            return result
    
    # Fallback to Crossref
    crossref_available = crossref_limiter.wait_and_acquire(timeout=5)
    
    if crossref_available:
        result = _verify_with_crossref(citation, style)
        if result:
            return result
    
    # Return offline status if both fail
    return {
        "status": "offline",
        "citation": citation,
        "score": 0,
        "message": "Service temporarily busy, please try again"
    }

def _verify_with_openalex(citation: str, style: str) -> Optional[Dict[str, Any]]:
    """Verify with OpenAlex API"""
    try:
        query, authors, year, doi, title_only = _build_query(citation, style)
        
        if not query:
            return None
        
        url = "https://api.openalex.org/works"
        params = {"search": query, "per-page": 3}
        if MAILTO:
            params["mailto"] = MAILTO
        
        data = _safe_get_json(url, params=params, timeout=12)
        
        if not data:
            _api_health["openalex"]["failures"] += 1
            _api_health["openalex"]["success_rate"] = max(0, _api_health["openalex"]["success_rate"] - 0.05)
            return None
        
        _api_health["openalex"]["failures"] = max(0, _api_health["openalex"]["failures"] - 1)
        _api_health["openalex"]["success_rate"] = min(1.0, _api_health["openalex"]["success_rate"] + 0.02)
        
        items = data.get("results", [])
        
        if not items:
            return None
        
        best = items[0]
        
        # Extract fields
        cand_title = best.get("title", "")
        cand_year = best.get("publication_year", "")
        cand_authors = []
        for a in best.get("authorships", [])[:3]:
            name = a.get("author", {}).get("display_name", "")
            if name:
                surname = name.split()[-1].lower() if name.split() else ""
                cand_authors.append(surname)
        
        # Calculate score
        score_data = _score(
            citation, authors, year, 
            cand_title, cand_authors, cand_year
        )
        
        status = _classify(
            False,
            score_data["title_score"],
            score_data["score"],
            score_data["year_match"]
        )
        
        return {
            "status": status,
            "source": "openalex",
            "score": score_data["score"],
            "doi": best.get("doi", "").replace("https://doi.org/", ""),
            "matched_title": cand_title,
            "matched_year": cand_year,
            "matched_authors": ", ".join(cand_authors[:3]),
            "title_score": score_data["title_score"],
            "author_overlap": score_data["author_overlap"],
            "author_similarity": score_data["author_similarity"],
            "year_match": score_data["year_match"]
        }
        
    except Exception as e:
        _api_health["openalex"]["failures"] += 1
        return None

def _verify_with_crossref(citation: str, style: str) -> Optional[Dict[str, Any]]:
    """Verify with Crossref API"""
    try:
        query, authors, year, doi, title_only = _build_query(citation, style)
        
        if not query:
            return None
        
        url = "https://api.crossref.org/works"
        params = {"query.bibliographic": query, "rows": 3, "sort": "score"}
        if MAILTO:
            params["mailto"] = MAILTO
        
        data = _safe_get_json(url, params=params, timeout=12)
        
        if not data:
            _api_health["crossref"]["failures"] += 1
            _api_health["crossref"]["success_rate"] = max(0, _api_health["crossref"]["success_rate"] - 0.05)
            return None
        
        _api_health["crossref"]["failures"] = max(0, _api_health["crossref"]["failures"] - 1)
        _api_health["crossref"]["success_rate"] = min(1.0, _api_health["crossref"]["success_rate"] + 0.02)
        
        items = data.get("message", {}).get("items", [])
        
        if not items:
            return None
        
        best = items[0]
        
        # Extract fields
        cand_title = best.get("title", [""])[0] if best.get("title") else ""
        cand_year = ""
        issued = best.get("issued") or best.get("published-print") or {}
        cand_year = str(issued.get("date-parts", [[None]])[0][0]) if issued else ""
        
        cand_authors = []
        for a in best.get("author", [])[:3]:
            fam = a.get("family", "")
            if fam:
                cand_authors.append(fam.lower())
        
        # Calculate score
        score_data = _score(
            citation, authors, year, 
            cand_title, cand_authors, cand_year
        )
        
        # Check DOI match
        doi_match = doi and best.get("DOI", "").lower() == doi.lower()
        
        status = _classify(
            doi_match,
            score_data["title_score"],
            score_data["score"],
            score_data["year_match"]
        )
        
        return {
            "status": status,
            "source": "crossref",
            "score": score_data["score"],
            "doi": best.get("DOI", ""),
            "matched_title": cand_title,
            "matched_year": cand_year,
            "matched_authors": ", ".join(cand_authors[:3]),
            "title_score": score_data["title_score"],
            "author_overlap": score_data["author_overlap"],
            "author_similarity": score_data["author_similarity"],
            "year_match": score_data["year_match"]
        }
        
    except Exception as e:
        _api_health["crossref"]["failures"] += 1
        return None


# ============================================================
# EXISTING FUNCTIONS (keep as is)
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
# Helpers (keep existing)
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


def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 14) -> Optional[dict]:
    try:
        headers = {
            "User-Agent": f"CitationCrosschecker/2.0 (mailto:{MAILTO})",
            "Accept": "application/json",
        }
        r = requests.get(url, params=params, timeout=timeout, headers=headers)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
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
# Reference field extraction (keep existing)
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


def _extract_ieee_fields(ref: str) -> Dict[str, Any]:
    fields = _extract_common_fields(ref)

    m = re.search(r'["“](.+?)["”]', ref)
    if m:
        fields["title"] = m.group(1).strip()

    if not fields["authors"]:
        clean = _strip_leading_numbering(ref)
        left = clean.split('"', 1)[0]
        fields["authors"] = _extract_authors_from_left(left)

    return fields


def _extract_vancouver_fields(ref: str) -> Dict[str, Any]:
    fields = _extract_common_fields(ref)

    if not fields["authors"]:
        clean = _strip_leading_numbering(ref)
        left = clean.split(".", 1)[0]
        fields["authors"] = _extract_authors_from_left(left)

    return fields


def _extract_fields_by_style(ref: str, style: str) -> Dict[str, Any]:
    style = _STYLE_ALIASES.get((style or "apa").lower(), "apa")
    if style == "ieee":
        return _extract_ieee_fields(ref)
    if style == "vancouver":
        return _extract_vancouver_fields(ref)
    return _extract_apa_fields(ref)


# ---------------------------------------------------------
# Query building (keep existing)
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
# Candidate extraction (keep existing)
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
# External queries (keep existing)
# ---------------------------------------------------------

def _query_crossref_by_doi(doi: str) -> List[Dict[str, Any]]:
    if not doi:
        return []
    url = f"https://api.crossref.org/works/{doi}"
    params = {"mailto": MAILTO} if MAILTO else None
    data = _safe_get_json(url, params=params, timeout=10)
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
    data = _safe_get_json(url, params=params, timeout=12)
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
    data = _safe_get_json(url, params=params, timeout=12)
    items = (data or {}).get("message", {}).get("items", [])
    return [{"source": "crossref", "item": it} for it in items]


def _query_openalex(query: str, rows: int = 10) -> List[Dict[str, Any]]:
    if not query:
        return []
    url = "https://api.openalex.org/works"
    params: Dict[str, Any] = {"search": query, "per-page": rows}
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=12)
    items = (data or {}).get("results", [])
    return [{"source": "openalex", "item": it} for it in items]


def _query_openalex_title_only(title_query: str, rows: int = 12) -> List[Dict[str, Any]]:
    if not title_query:
        return []
    url = "https://api.openalex.org/works"
    params: Dict[str, Any] = {"search": title_query, "per-page": rows}
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=12)
    items = (data or {}).get("results", [])
    return [{"source": "openalex", "item": it} for it in items]


# ---------------------------------------------------------
# Scoring and classification (keep existing)
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
# Candidate selection (keep existing)
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
# Public API - UPDATED to use queue
# ---------------------------------------------------------

def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> List[Dict[str, Any]]:
    """
    Legacy synchronous batch verification - use queue for better performance
    """
    refs = [r for r in (references or []) if _safe_strip(r)]
    if not refs:
        return []

    # Submit as job and wait
    job_id = submit_verification_job(refs, style)
    
    # Poll for completion (with timeout)
    timeout = 300  # 5 minutes max
    start = time.time()
    
    while time.time() - start < timeout:
        status = get_job_status(job_id)
        if status and status["status"] in ["completed", "failed"]:
            if status["status"] == "completed":
                return status["results"]
            else:
                return [{"reference": r, "status": "offline", "error": status.get("error", "Job failed")} for r in refs]
        time.sleep(0.5)
    
    return [{"reference": r, "status": "offline", "error": "Verification timeout"} for r in refs]


# New async-friendly functions
def submit_verification(references: List[str], style: str = "apa") -> str:
    """Submit verification job and return job ID"""
    return submit_verification_job(references, style)


def get_verification_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Get status of verification job"""
    return get_job_status(job_id)


def get_queue_status() -> Dict[str, Any]:
    """Get current queue status"""
    return get_queue_stats()


def is_server_busy_check() -> bool:
    """Check if server is too busy to accept new requests"""
    return is_server_busy()
