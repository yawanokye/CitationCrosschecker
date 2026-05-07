# verify.py — Cleaned, production-ready verification engine

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

# ============================================================
# CONFIGURATION - Load from environment
# ============================================================

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}

MAILTO = (
    os.getenv("CITATION_CROSSCHECKER_MAILTO")
    or os.getenv("CROSSREF_MAILTO")
    or os.getenv("OPENALEX_MAILTO")
    or "verification@citationcrosschecker.com"
).strip()

def _env_flag(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}

# Timeout settings - INCREASED for reliability
API_TIMEOUT = int(os.getenv("VERIFY_REQUEST_TIMEOUT", "15"))
VERIFICATION_TIMEOUT = None
WORKER_THREADS = int(os.getenv("VERIFY_INNER_THREADS", "4"))
RETRY_ATTEMPTS = int(os.getenv("VERIFY_RETRY_ATTEMPTS", "2"))
BATCH_DELAY = float(os.getenv("VERIFY_BATCH_DELAY", "0.1"))

# Chunk processing
CHUNK_SIZE = int(os.getenv("VERIFY_INTERNAL_CHUNK_SIZE", "20"))
CHUNK_DELAY = float(os.getenv("VERIFY_CHUNK_DELAY", "0.5"))
MAX_RETRIES_PER_REFERENCE = int(os.getenv("VERIFY_MAX_RETRIES_PER_REFERENCE", "2"))

# Fast-skip controls
VERIFY_SKIP_WEAK_TITLE = _env_flag("VERIFY_SKIP_WEAK_TITLE", "1")
VERIFY_MIN_TITLE_WORDS = int(os.getenv("VERIFY_MIN_TITLE_WORDS", "4"))
VERIFY_STOP_ON_STRONG_MATCH = _env_flag("VERIFY_STOP_ON_STRONG_MATCH", "1")
VERIFY_DEEP_FALLBACK = _env_flag("VERIFY_DEEP_FALLBACK", "1")
VERIFY_RETRY_FAILED = _env_flag("VERIFY_RETRY_FAILED", "1")
VERIFY_CROSSREF_ROWS = int(os.getenv("VERIFY_CROSSREF_ROWS", "10"))
VERIFY_TITLE_ROWS = int(os.getenv("VERIFY_TITLE_ROWS", "10"))
VERIFY_OPENALEX_ROWS = int(os.getenv("VERIFY_OPENALEX_ROWS", "10"))
VERIFY_SINGLE_REF_TIMEOUT = int(os.getenv("VERIFY_SINGLE_REF_TIMEOUT", "20"))
VERIFY_RETRY_BACKOFF_SECONDS = float(os.getenv("VERIFY_RETRY_BACKOFF_SECONDS", "1"))
VERIFY_FORCE_OPENALEX_FALLBACK = _env_flag("VERIFY_FORCE_OPENALEX_FALLBACK", "1")
VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY = _env_flag("VERIFY_AUTHOR_GATE_FOR_VERIFIED_ONLY", "1")

# Classification thresholds
VERIFY_THRESHOLD_TITLE_VERIFIED = int(os.getenv("VERIFY_THRESHOLD_TITLE_VERIFIED", "92"))
VERIFY_THRESHOLD_TITLE_LIKELY = int(os.getenv("VERIFY_THRESHOLD_TITLE_LIKELY", "84"))
VERIFY_THRESHOLD_TITLE_REVIEW = int(os.getenv("VERIFY_THRESHOLD_TITLE_REVIEW", "70"))
VERIFY_THRESHOLD_JOURNAL_SUPPORT = int(os.getenv("VERIFY_THRESHOLD_JOURNAL_SUPPORT", "70"))
VERIFY_STRICT_AUTHOR_GATE = _env_flag("VERIFY_STRICT_AUTHOR_GATE", "1")

# Cache settings
_MAX_CACHE_SIZE = int(os.getenv("VERIFY_CACHE_SIZE", "5000"))
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()

# Session for connection pooling
_session = None
_session_lock = threading.Lock()

def _get_session() -> requests.Session:
    """Get or create a session with connection pooling"""
    global _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
            _session.headers.update({
                "User-Agent": f"CitationVerifier/2.0 (mailto:{MAILTO})",
                "Accept": "application/json",
            })
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=10,
                pool_maxsize=20,
                max_retries=1
            )
            _session.mount("http://", adapter)
            _session.mount("https://", adapter)
        return _session

# ============================================================
# PROGRESS TRACKING
# ============================================================

@dataclass
class VerificationJob:
    job_id: str
    total: int
    progress: int = 0
    status: str = "pending"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None

_jobs: Dict[str, VerificationJob] = {}
_jobs_lock = threading.Lock()
_verification_results: Dict[str, List[Dict[str, Any]]] = {}
_verification_results_lock = threading.Lock()

# ============================================================
# UTILITY FUNCTIONS
# ============================================================

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

# ============================================================
# CACHE MANAGEMENT
# ============================================================

def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    with _CACHE_LOCK:
        v = _CACHE.get(key)
        return dict(v) if v else None

def _cache_set(key: str, value: Dict[str, Any]) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _MAX_CACHE_SIZE:
            # Remove oldest 20% of entries
            keys_to_remove = list(_CACHE.keys())[:_MAX_CACHE_SIZE // 5]
            for k in keys_to_remove:
                del _CACHE[k]
        _CACHE[key] = dict(value)

# ============================================================
# API REQUEST FUNCTIONS
# ============================================================

def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = None) -> Optional[dict]:
    """Safe JSON GET with connection pooling and timeout"""
    if timeout is None:
        timeout = API_TIMEOUT

    attempts = max(1, RETRY_ATTEMPTS)
    session = _get_session()

    for attempt in range(attempts):
        try:
            response = session.get(url, params=params, timeout=timeout)
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429:
                # Rate limited - wait and retry
                time.sleep(VERIFY_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            if response.status_code >= 500:
                # Server error - retry
                time.sleep(VERIFY_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            return None
        except requests.exceptions.Timeout:
            print(f"[DEBUG] API timeout after {timeout}s on attempt {attempt + 1}/{attempts}")
            if attempt < attempts - 1:
                time.sleep(VERIFY_RETRY_BACKOFF_SECONDS * (attempt + 1))
        except Exception as e:
            print(f"[DEBUG] API request failed: {e}")
            if attempt < attempts - 1:
                time.sleep(VERIFY_RETRY_BACKOFF_SECONDS * (attempt + 1))

    return None

def _safe_get_json_with_headers(url: str, params: Optional[dict] = None, 
                                 headers: Optional[dict] = None, timeout: int = None) -> Optional[dict]:
    """JSON GET with custom headers"""
    if timeout is None:
        timeout = API_TIMEOUT

    session = _get_session()
    request_headers = dict(session.headers)
    if headers:
        request_headers.update(headers)

    attempts = max(1, RETRY_ATTEMPTS)

    for attempt in range(attempts):
        try:
            response = session.get(url, params=params, headers=request_headers, timeout=timeout)
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429:
                time.sleep(VERIFY_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            return None
        except Exception as e:
            print(f"[DEBUG] Request failed: {e}")
            if attempt < attempts - 1:
                time.sleep(VERIFY_RETRY_BACKOFF_SECONDS * (attempt + 1))

    return None

# ============================================================
# REFERENCE PARSING
# ============================================================

_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})(?:[a-z])?\b", re.I)
_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s]+)", re.I)
_ISBN_RE = re.compile(r"\b(?:ISBN(?:-1[03])?:?\s*)?((?:97[89][\- ]?)?(?:\d[\- ]?){9}[\dXx])\b")
_PMID_RE = re.compile(r"\bPMID\s*:?\s*(\d{4,12})\b", re.I)
_PMCID_RE = re.compile(r"\bPMC\s*:?\s*(\d{4,12})\b", re.I)
_ARXIV_RE = re.compile(r"\barXiv\s*:?\s*([a-z\-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?\b", re.I)

_STOP_WORDS = {
    "the", "and", "of", "to", "in", "for", "on", "with", "by", "at", "from",
    "into", "using", "that", "this", "these", "those", "study", "analysis",
    "research", "paper", "journal", "review", "proceedings", "conference",
}

_QUERY_STOP_WORDS = _STOP_WORDS | {
    "approach", "model", "evidence", "article", "method", "based", "case",
    "empirical", "impact", "role", "determinants", "perspective", "framework",
}

def _extract_year(text: str) -> str:
    m = _YEAR_RE.search(text or "")
    return m.group(1) if m else ""

def _extract_doi(text: str) -> str:
    m = _DOI_RE.search(text or "")
    if m:
        doi = m.group(1).rstrip(").,;")
        return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    return ""

def _extract_isbn(ref: str) -> str:
    for m in _ISBN_RE.finditer(ref or ""):
        isbn = re.sub(r"[^0-9Xx]", "", m.group(1))
        if len(isbn) in {10, 13}:
            return isbn.upper()
    return ""

def _normalise_doi(doi: str) -> str:
    if not doi:
        return ""
    doi = _safe_strip(doi)
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi\s*:\s*", "", doi, flags=re.I)
    return doi.strip().strip(".,;) ]}").lower()

def _strip_leading_numbering(text: str) -> str:
    t = _safe_strip(text)
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\(?\d+\)?[\.\)])\s*", "", t)
    return t.strip()

def _extract_authors_from_left(left: str) -> List[str]:
    left = _safe_strip(left)
    if not left:
        return []

    left = re.sub(r"\bet\s+al\.?\b", "", left, flags=re.I)
    left = left.replace("&", " and ")
    left = re.sub(r"\s+", " ", left).strip(" ,.;:")

    candidates: List[str] = []

    # APA/Harvard: Surname, I., Surname, I.
    surname_matches = re.findall(r"(?:^|,|\band\s+)([A-Z][A-Za-z'\-]{1,})(?=\s*,)", left)
    candidates.extend(surname_matches)

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

def _significant_word_count(text: str) -> int:
    return len(_significant_title_words(text or "", limit=20))

def _clean_title_guess(title: str) -> str:
    title = _safe_strip(title)
    if not title:
        return ""

    title = re.sub(r"https?://\S+", "", title, flags=re.I)
    title = re.sub(r"\bdoi\s*:?\s*\S+", "", title, flags=re.I)
    title = re.sub(r"\b(pp?|pages?)\.?\s*\d+[\-–—]?\d*", "", title, flags=re.I)
    title = re.sub(r"\bvol\.?\s*\d+", "", title, flags=re.I)
    title = re.sub(r"\bretrieved\s+from\b.*$", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" .,:;\"'")
    return title

def _extract_title_guess(ref: str, year: str) -> str:
    ref_clean = _strip_leading_numbering(ref)

    if year:
        parts = re.split(rf"[\(\[]?\s*{re.escape(year)}\s*[\)\]]?", ref_clean, maxsplit=1, flags=re.I)
        if len(parts) >= 2:
            right = parts[1].strip(" .,:;")
            if right:
                title = re.split(
                    r"\.\s+(?:In|Journal|Proceedings|Vol|No|pp\.?|pages?|https?://|doi|Retrieved|Available)",
                    right, maxsplit=1, flags=re.I
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

    return _clean_title_guess(ref_clean[:180])

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

    return out

def _extract_fields_by_style(ref: str, style: str) -> Dict[str, Any]:
    ref = _safe_strip(ref)
    ref = _strip_leading_numbering(ref)

    year = _extract_year(ref)
    doi = _normalise_doi(_extract_doi(ref))

    left = ref
    if year:
        left = ref.split(year, 1)[0].strip(" ,.;:()[]")

    authors = _extract_authors_from_left(left)
    title = _extract_title_guess(ref, year)
    vip = _extract_volume_issue_pages(ref)

    return {
        "authors": authors,
        "year": year,
        "doi": doi,
        "title": title,
        "volume": vip.get("volume", ""),
        "issue": vip.get("issue", ""),
        "pages": vip.get("pages", ""),
        "isbn": _extract_isbn(ref),
        "pmid": _extract_pmid(ref),
        "pmcid": _extract_pmcid(ref),
        "arxiv_id": _extract_arxiv_id(ref),
    }

def _extract_pmid(ref: str) -> str:
    m = _PMID_RE.search(ref or "")
    return m.group(1) if m else ""

def _extract_pmcid(ref: str) -> str:
    m = _PMCID_RE.search(ref or "")
    return f"PMC{m.group(1)}" if m else ""

def _extract_arxiv_id(ref: str) -> str:
    m = _ARXIV_RE.search(ref or "")
    return m.group(1) if m else ""

# ============================================================
# API QUERY FUNCTIONS
# ============================================================

def _query_crossref_by_doi(doi: str) -> List[Dict[str, Any]]:
    if not doi:
        return []
    url = f"https://api.crossref.org/works/{doi}"
    params = {"mailto": MAILTO} if MAILTO else None
    data = _safe_get_json(url, params=params)
    if not data or "message" not in data:
        return []
    return [{"source": "crossref", "item": data["message"]}]

def _query_crossref_bibliographic(query: str, rows: int = None) -> List[Dict[str, Any]]:
    if rows is None:
        rows = VERIFY_CROSSREF_ROWS
    query = _safe_strip(query)
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
    data = _safe_get_json(url, params=params)
    items = (data or {}).get("message", {}).get("items", [])
    return [{"source": "crossref", "item": it} for it in items]

def _query_crossref(query: str, rows: int = None) -> List[Dict[str, Any]]:
    return _query_crossref_bibliographic(query, rows)

def _query_crossref_title_only(title_query: str, rows: int = None) -> List[Dict[str, Any]]:
    return _query_crossref_bibliographic(title_query, rows or VERIFY_TITLE_ROWS)

def _query_openalex_by_doi(doi: str) -> List[Dict[str, Any]]:
    if not doi:
        return []
    url = "https://api.openalex.org/works"
    params: Dict[str, Any] = {"filter": f"doi:{doi}"}
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params)
    items = (data or {}).get("results", [])
    return [{"source": "openalex", "item": it} for it in items]

def _query_openalex_search(query: str, rows: int = None, publication_year: str = "") -> List[Dict[str, Any]]:
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
    data = _safe_get_json(url, params=params)
    items = (data or {}).get("results", [])
    return [{"source": "openalex", "item": it} for it in items]

def _query_openalex(query: str, rows: int = None) -> List[Dict[str, Any]]:
    return _query_openalex_search(query, rows)

def _query_openalex_title_only(title_query: str, rows: int = None) -> List[Dict[str, Any]]:
    return _query_openalex_search(title_query, rows or VERIFY_TITLE_ROWS)

# ============================================================
# CANDIDATE PROCESSING
# ============================================================

def _crossref_year(item: Dict[str, Any]) -> str:
    for key in ("issued", "published-print", "published-online"):
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
        if fam and len(fam) >= 2:
            out.append(fam)
    return _dedupe_preserve(out)

def _extract_openalex_authors(item: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for authorship in (item.get("authorships") or [])[:12]:
        name = _safe_strip((authorship.get("author") or {}).get("display_name"))
        if name:
            surname = re.sub(r"[^a-z'\-]", "", name.split()[-1].lower())
            if surname and len(surname) >= 2:
                out.append(surname)
    return _dedupe_preserve(out)

def _candidate_fields(cand: Dict[str, Any]) -> Dict[str, Any]:
    src = cand.get("source")
    item = cand.get("item") or {}

    if src == "crossref":
        title_list = item.get("title") or []
        container_list = item.get("container-title") or []
        return {
            "source": src,
            "doi": _normalise_doi(item.get("DOI", "")),
            "title": _safe_strip(title_list[0]) if title_list else "",
            "year": _crossref_year(item),
            "authors": _extract_crossref_authors(item),
            "journal": _safe_strip(container_list[0]) if container_list else "",
            "volume": _safe_strip(item.get("volume", "")),
            "issue": _safe_strip(item.get("issue", "")),
            "pages": _safe_strip(item.get("page", "")),
        }

    if src == "openalex":
        host = item.get("primary_location", {}).get("source", {})
        biblio = item.get("biblio") or {}
        doi = _safe_strip(item.get("doi", "")).replace("https://doi.org/", "")
        return {
            "source": src,
            "doi": _normalise_doi(doi),
            "title": _safe_strip(item.get("title") or item.get("display_name")),
            "year": _safe_strip(item.get("publication_year")),
            "authors": _extract_openalex_authors(item),
            "journal": _safe_strip(host.get("display_name", "")),
            "volume": _safe_strip(biblio.get("volume", "")),
            "issue": _safe_strip(biblio.get("issue", "")),
            "pages": _safe_strip(biblio.get("first_page", "")),
        }

    return {
        "source": src or "unknown",
        "doi": "",
        "title": "",
        "year": "",
        "authors": [],
        "journal": "",
        "volume": "",
        "issue": "",
        "pages": "",
    }

# ============================================================
# SCORING AND CLASSIFICATION
# ============================================================

def _score_candidate(ref_fields: Dict[str, Any], cand_fields: Dict[str, Any]) -> Dict[str, Any]:
    ref_title = _norm_text(ref_fields.get("title", ""))
    cand_title = _norm_text(cand_fields.get("title", ""))

    if ref_title and cand_title:
        token_set = fuzz.token_set_ratio(ref_title, cand_title)
        token_sort = fuzz.token_sort_ratio(ref_title, cand_title)
        partial = fuzz.partial_ratio(ref_title, cand_title)
        title_score = int((token_set * 0.50) + (token_sort * 0.35) + (partial * 0.15))
    else:
        title_score = 0

    ref_authors = [a.lower() for a in ref_fields.get("authors", [])]
    cand_authors = [a.lower() for a in cand_fields.get("authors", [])]
    
    if ref_authors and cand_authors:
        ref_set = set(ref_authors)
        cand_set = set(cand_authors)
        author_overlap = len(ref_set & cand_set)
        author_similarity = int((author_overlap / max(len(ref_set), 1)) * 100)
    else:
        author_overlap = 0
        author_similarity = 0

    ref_year = ref_fields.get("year", "")
    cand_year = cand_fields.get("year", "")
    year_match = 1 if ref_year and cand_year and ref_year[:4] == cand_year[:4] else 0

    ref_doi = _normalise_doi(ref_fields.get("doi", ""))
    cand_doi = _normalise_doi(cand_fields.get("doi", ""))
    doi_match = bool(ref_doi and cand_doi and ref_doi == cand_doi)

    score = (title_score * 0.65) + (author_similarity * 0.25) + (year_match * 10)
    if doi_match:
        score += 25

    return {
        "score": int(min(100, score)),
        "title_score": title_score,
        "author_overlap": author_overlap,
        "author_similarity": author_similarity,
        "year_match": year_match,
        "doi_match": doi_match,
        **cand_fields,
    }

def _classify(meta: Dict[str, Any], ref_fields: Dict[str, Any]) -> Tuple[str, str]:
    title_score = int(meta.get("title_score", 0))
    year_match = int(meta.get("year_match", 0))
    author_overlap = int(meta.get("author_overlap", 0))
    author_similarity = int(meta.get("author_similarity", 0))
    doi_match = bool(meta.get("doi_match", False))
    score = int(meta.get("score", 0))

    ref_has_authors = bool(ref_fields.get("authors"))
    cand_has_authors = bool(meta.get("authors"))
    has_author_conflict = ref_has_authors and cand_has_authors and author_overlap == 0

    # DOI match
    if doi_match:
        if title_score >= 75:
            return "verified", "Exact DOI match with sufficient title evidence"
        if title_score >= 55:
            return "likely", "DOI matches, but title evidence is moderate"
        return "needs_review", "DOI matches, but title appears inconsistent"

    # Author conflict gate
    if has_author_conflict and VERIFY_STRICT_AUTHOR_GATE:
        if title_score >= 96 and year_match:
            return "likely", "Very strong title match, but no author overlap"
        if title_score >= VERIFY_THRESHOLD_TITLE_REVIEW:
            return "needs_review", "Possible match found, but author names do not overlap"
        return "not_found", "Candidate found but author names do not match"

    # Verified
    if title_score >= VERIFY_THRESHOLD_TITLE_VERIFIED and year_match:
        return "verified", "Strong title, author, and year match"

    if title_score >= 96 and year_match:
        return "verified", "Very strong title and year match"

    # Likely
    if title_score >= VERIFY_THRESHOLD_TITLE_LIKELY:
        if year_match or author_overlap >= 1:
            return "likely", "Strong title match with supporting evidence"

    if score >= 75 and title_score >= 75:
        return "likely", "Good composite score, further review recommended"

    # Needs review
    if title_score >= VERIFY_THRESHOLD_TITLE_REVIEW or score >= 50:
        return "needs_review", "Possible match found, insufficient for auto-verification"

    return "not_found", "No reliable match found"

def _best_candidate(ref_fields: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    best = None
    best_meta = {}
    best_score = -1

    for cand in candidates:
        cand_fields = _candidate_fields(cand)
        meta = _score_candidate(ref_fields, cand_fields)
        
        if meta["score"] > best_score:
            best_score = meta["score"]
            best = cand
            best_meta = meta

    return best, best_meta

def _make_fast_review_row(ref: str, style: str, reason: str) -> Dict[str, Any]:
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
        "confidence_reason": reason,
        "error": reason,
    }

# ============================================================
# METADATA ENRICHMENT
# ============================================================

def fetch_full_crossref_metadata(doi: str) -> Optional[Dict[str, Any]]:
    if not doi:
        return None
    
    doi = _normalise_doi(doi)
    url = f"https://api.crossref.org/works/{doi}"
    params = {"mailto": MAILTO} if MAILTO else None
    
    try:
        data = _safe_get_json(url, params=params)
        if data and data.get("status") == "ok" and "message" in data:
            msg = data["message"]
            return {
                "doi": msg.get("DOI", ""),
                "title": (msg.get("title") or [""])[0],
                "container_title": (msg.get("container-title") or [""])[0],
                "volume": msg.get("volume", ""),
                "issue": msg.get("issue", ""),
                "page": msg.get("page", ""),
                "publisher": msg.get("publisher", ""),
                "type": msg.get("type", ""),
            }
    except Exception as e:
        print(f"[DEBUG] Error fetching metadata for {doi}: {e}")
    
    return None

def enrich_with_full_metadata(result: Dict[str, Any]) -> Dict[str, Any]:
    if not result or result.get("full_metadata"):
        return result
    
    doi = result.get("doi", "")
    if doi:
        metadata = fetch_full_crossref_metadata(doi)
        if metadata:
            result["full_metadata"] = metadata
            result["matched_volume"] = metadata.get("volume", "")
            result["matched_issue"] = metadata.get("issue", "")
            result["matched_pages"] = metadata.get("page", "")
            result["matched_container_title"] = metadata.get("container_title", "")
    
    return result

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
    """Single reference verification with retry support"""
    cache_key = f"v2::{style}::{use_crossref}:{use_openalex}::{ref}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    fields = _extract_fields_by_style(ref, style)
    
    # Fast skip for weak references
    if VERIFY_SKIP_WEAK_TITLE and not fields.get("doi"):
        word_count = _significant_word_count(fields.get("title", ""))
        if word_count < VERIFY_MIN_TITLE_WORDS:
            row = _make_fast_review_row(ref, style, "No DOI and insufficient title words")
            row["status"] = _normalize_verify_status(row["status"])
            _cache_set(cache_key, row)
            return row

    try:
        candidates: List[Dict[str, Any]] = []
        
        # DOI-first lookup
        if fields.get("doi") and use_crossref:
            candidates.extend(_query_crossref_by_doi(fields["doi"]))
        
        # Title/author query
        query = f"{fields.get('title', '')} {fields.get('year', '')}".strip()
        if query and use_crossref and not candidates:
            candidates.extend(_query_crossref(query))
        
        if query and use_openalex and not candidates:
            candidates.extend(_query_openalex(query))
        
        # Title-only fallback
        title_query = fields.get("title", "")
        if title_query and not candidates:
            if use_crossref:
                candidates.extend(_query_crossref_title_only(title_query))
            if use_openalex:
                candidates.extend(_query_openalex_title_only(title_query))
        
        best, meta = _best_candidate(fields, candidates)
        
        if best:
            status, reason = _classify(meta, fields)
            
            row = {
                "reference": ref,
                "style": style,
                "status": status,
                "source": meta.get("source", ""),
                "score": meta.get("score", 0),
                "doi": meta.get("doi", ""),
                "matched_title": meta.get("title", ""),
                "matched_year": meta.get("year", ""),
                "matched_authors": ", ".join(meta.get("authors", [])),
                "matched_journal": meta.get("journal", ""),
                "matched_volume": meta.get("volume", ""),
                "matched_issue": meta.get("issue", ""),
                "matched_pages": meta.get("pages", ""),
                "title_score": meta.get("title_score", 0),
                "author_overlap": meta.get("author_overlap", 0),
                "author_similarity": meta.get("author_similarity", 0),
                "year_match": meta.get("year_match", 0),
                "doi_match": 1 if meta.get("doi_match") else 0,
                "confidence_reason": reason,
                "reference_title": fields.get("title", ""),
                "reference_year": fields.get("year", ""),
                "reference_doi": fields.get("doi", ""),
                "reference_authors": ", ".join(fields.get("authors", [])),
                "alternative_matches": [],
            }
            
            if enrich_metadata and row.get("doi"):
                row = enrich_with_full_metadata(row)
            
            row["status"] = _normalize_verify_status(row["status"])
            _cache_set(cache_key, row)
            return row
        
        row = {
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
            "doi_match": 0,
            "confidence_reason": "No matching candidates found in Crossref or OpenAlex",
            "reference_title": fields.get("title", ""),
            "reference_year": fields.get("year", ""),
            "reference_doi": fields.get("doi", ""),
            "reference_authors": ", ".join(fields.get("authors", [])),
        }
        row["status"] = _normalize_verify_status(row["status"])
        _cache_set(cache_key, row)
        return row
        
    except Exception as e:
        print(f"[DEBUG] Error verifying reference: {e}")
        row = {
            "reference": ref,
            "style": style,
            "status": "not_found",
            "error": str(e),
            "confidence_reason": f"Verification failed: {str(e)}",
        }
        row["status"] = _normalize_verify_status(row["status"])
        _cache_set(cache_key, row)
        return row

def _verify_single_with_retry(
    ref: str,
    style: str,
    use_crossref: bool,
    use_openalex: bool,
    enrich_metadata: bool = False,
    max_retries: int = None,
) -> Dict[str, Any]:
    if max_retries is None:
        max_retries = MAX_RETRIES_PER_REFERENCE
    
    last_error = None
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                wait = min(5, attempt * VERIFY_RETRY_BACKOFF_SECONDS)
                time.sleep(wait)
            
            result = _verify_single_reference(ref, style, use_crossref, use_openalex, enrich_metadata)
            if result and result.get("status") != "offline":
                return result
        except Exception as e:
            last_error = e
            continue
    
    return {
        "reference": ref,
        "style": style,
        "status": "not_found",
        "error": f"All {max_retries} retries failed: {str(last_error)}",
    }

# ============================================================
# BATCH VERIFICATION
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
    """Batch verify references with progress tracking"""
    
    refs = [r for r in (references or []) if _safe_strip(r)]
    if not refs:
        return []
    
    total_refs = len(refs)
    chunks = [refs[i:i + CHUNK_SIZE] for i in range(0, total_refs, CHUNK_SIZE)]
    
    all_results: List[Dict[str, Any]] = []
    completed = 0
    
    for chunk_idx, chunk in enumerate(chunks):
        results = [None] * len(chunk)
        workers = min(WORKER_THREADS, len(chunk))
        
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for i, ref in enumerate(chunk):
                future = executor.submit(
                    _verify_single_with_retry,
                    ref, style, use_crossref, use_openalex, enrich_metadata
                )
                futures[future] = i
            
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    results[idx] = future.result(timeout=VERIFY_SINGLE_REF_TIMEOUT)
                except Exception as e:
                    results[idx] = {
                        "reference": chunk[idx],
                        "style": style,
                        "status": "not_found",
                        "error": str(e),
                    }
                
                completed += 1
                if job_id:
                    update_job_progress(job_id, completed)
                
                if throttle_s:
                    time.sleep(throttle_s)
        
        all_results.extend(results)
        
        if chunk_idx < len(chunks) - 1 and CHUNK_DELAY:
            time.sleep(CHUNK_DELAY)
    
    if job_id:
        update_job_progress(job_id, total_refs)
        store_verification_results(job_id, all_results)
    
    return all_results

# ============================================================
# JOB MANAGEMENT
# ============================================================

def create_verification_job(job_id: str, total: int) -> str:
    with _jobs_lock:
        _jobs[job_id] = VerificationJob(
            job_id=job_id,
            total=total,
            started_at=datetime.now().isoformat(),
            status="processing"
        )
    return job_id

def update_job_progress(job_id: str, progress: int):
    with _jobs_lock:
        if job_id in _jobs:
            job = _jobs[job_id]
            job.progress = progress
            if progress >= job.total:
                job.status = "completed"
                job.completed_at = datetime.now().isoformat()
            else:
                job.status = "processing"

def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
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
        }

def store_verification_results(job_id: str, results: List[Dict[str, Any]]):
    with _verification_results_lock:
        _verification_results[job_id] = results

def get_verification_results(job_id: str) -> Optional[List[Dict[str, Any]]]:
    with _verification_results_lock:
        return _verification_results.get(job_id)

def clear_verification_results(job_id: str):
    with _verification_results_lock:
        if job_id in _verification_results:
            del _verification_results[job_id]

def submit_verification(
    references: List[str], 
    style: str = "apa", 
    enrich_metadata: bool = False
) -> str:
    """Submit verification job and return job ID"""
    job_id = uuid.uuid4().hex
    create_verification_job(job_id, len(references))
    
    def run():
        results = verify_references_batch(
            references, style, job_id=job_id, enrich_metadata=enrich_metadata
        )
        store_verification_results(job_id, results)
    
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return job_id

def get_verification_status(job_id: str) -> Optional[Dict[str, Any]]:
    return get_job_status(job_id)

def get_queue_stats() -> Dict[str, Any]:
    with _jobs_lock:
        processing = sum(1 for j in _jobs.values() if j.status == "processing")
        completed = sum(1 for j in _jobs.values() if j.status == "completed")
    
    return {
        "pending_jobs": 0,
        "processing_jobs": processing,
        "completed_jobs": completed,
        "total_jobs": len(_jobs),
    }

def is_server_busy() -> bool:
    stats = get_queue_stats()
    return stats["processing_jobs"] > 10

# ============================================================
# EXPORTS
# ============================================================

__all__ = [
    'verify_references_batch',
    'submit_verification',
    'get_verification_status',
    'get_verification_results',
    'clear_verification_results',
    'get_queue_stats',
    'is_server_busy',
    'fetch_full_crossref_metadata',
    'enrich_with_full_metadata',
]
