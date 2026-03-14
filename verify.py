import os
import re
import threading
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from rapidfuzz import fuzz


_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}

MAILTO = (
    os.getenv("CITATION_CROSSCHECKER_MAILTO")
    or os.getenv("CROSSREF_MAILTO")
    or os.getenv("OPENALEX_MAILTO")
    or ""
).strip()

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
            # APA/Harvard, Vancouver surname-first patterns
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

    # fallback: quoted title
    m = re.search(r'["“](.+?)["”]', ref_clean)
    if m:
        title = m.group(1).strip()
        if len(title) >= 6:
            return title

    # fallback: use body after first period
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

    # fallback if parsing is poor
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
# External queries
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
# Scoring and classification - UPDATED with LIKELY category
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

    # Author overlap as percentage
    if ref_authors and cand_authors:
        ref_author_set = set(ref_authors)
        cand_author_set = set(cand_authors)
        
        # Calculate Jaccard similarity for authors
        intersection = len(ref_author_set & cand_author_set)
        union = len(ref_author_set | cand_author_set)
        
        if union > 0:
            author_similarity = (intersection / union) * 100
        else:
            author_similarity = 0
            
        # Also count exact matches for bonus
        author_overlap = intersection
    else:
        author_similarity = 0
        author_overlap = 0
    
    year_match = 1 if ref_year and cand_year and ref_year[:4] == cand_year[:4] else 0
    
    # Calculate overall score (weighted)
    # Title is most important (60%), author similarity (30%), year match (10%)
    score = (title_score * 0.6) + (author_similarity * 0.3) + (year_match * 10)

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_overlap": int(author_overlap),
        "author_similarity": int(author_similarity),
        "year_match": int(year_match),
    }


def _classify(doi_match: bool, title_score: int, score: int, year_match: int) -> str:
    """
    Classification thresholds:
    - verified: High confidence match (≥85 overall OR ≥90 title with year)
    - likely: Good match but needs quick check (70-84 overall OR ≥80 title with year)
    - needs_review: Possible match but needs verification (50-69 overall)
    - not_found: Poor match (<50 overall)
    """
    
    # DOI match is always verified
    if doi_match:
        return "verified"
    
    # ===== VERIFIED =====
    # High confidence matches
    if score >= 85:
        return "verified"
    
    if title_score >= 90 and year_match:
        return "verified"
    
    # ===== LIKELY =====
    # Good matches that are probably correct but worth a quick check
    if score >= 70:
        return "likely"
    
    if title_score >= 80 and year_match:
        return "likely"
    
    if title_score >= 85:
        return "likely"
    
    # ===== NEEDS REVIEW =====
    # Possible matches that need human verification
    if score >= 50:
        return "needs_review"
    
    if title_score >= 60:
        return "needs_review"
    
    # ===== NOT FOUND =====
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
        meta_score = int(meta["score"] + (25 if doi_match else 0))  # DOI bonus

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
# Worker
# ---------------------------------------------------------

def _verify_single_reference(ref: str, style: str, use_crossref: bool, use_openalex: bool) -> Dict[str, Any]:
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
        if use_crossref:
            candidates.extend(_query_crossref(query, rows=10))
        if use_openalex:
            candidates.extend(_query_openalex(query, rows=10))

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

                if use_crossref:
                    deep_candidates.extend(_query_crossref(query, rows=20))
                    deep_candidates.extend(_query_crossref_title_only(title_only, rows=12))

                if use_openalex:
                    deep_candidates.extend(_query_openalex(query, rows=20))
                    deep_candidates.extend(_query_openalex_title_only(title_only, rows=12))

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
        else:
            row["status"] = "not_found"

    except Exception as e:
        row["status"] = "offline"
        row["error"] = str(e)

    row["status"] = _normalize_verify_status(row.get("status"))
    _cache_set(cache_key, row)
    return row


# ---------------------------------------------------------
# Public API
# ---------------------------------------------------------

def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> List[Dict[str, Any]]:
    refs = [r for r in (references or []) if _safe_strip(r)]
    if not refs:
        return []

    normalized_style = _STYLE_ALIASES.get((style or "apa").lower(), "apa")

    rows: List[Dict[str, Any]] = [None] * len(refs)  # type: ignore
    workers = min(8, max(1, len(refs)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_idx = {
            executor.submit(
                _verify_single_reference,
                ref,
                normalized_style,
                use_crossref,
                use_openalex,
            ): i
            for i, ref in enumerate(refs)
        }

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                rows[idx] = future.result()
            except Exception as e:
                rows[idx] = {
                    "reference": refs[idx],
                    "style": normalized_style,
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
                    "query_used": "",
                    "author": "",
                    "error": str(e),
                }

    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
