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

# simple in-process cache
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()


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


def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 16) -> Optional[dict]:
    try:
        headers = {
            "User-Agent": f"CitationCrosschecker (mailto:{MAILTO})",
            "Accept": "application/json",
        }
        r = requests.get(url, params=params, timeout=timeout, headers=headers)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _extract_year(text: str) -> str:
    m = re.search(r"(19|20)\d{2}", text or "")
    return m.group(0) if m else ""


def _extract_doi(text: str) -> str:
    m = re.search(r"(10\.\d{4,9}/[^\s]+)", text or "")
    return m.group(1).rstrip(").,;") if m else ""


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    with _CACHE_LOCK:
        v = _CACHE.get(key)
        return dict(v) if v else None


def _cache_set(key: str, value: Dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = dict(value)


# ---------------------------------------------------------
# Query Builder
# ---------------------------------------------------------

def _build_query(ref: str) -> Tuple[str, List[str], str, str, str]:
    ref = _safe_strip(ref)

    year = _extract_year(ref)
    doi = _extract_doi(ref)

    words = re.findall(r"[A-Za-z]{4,}", ref)

    # keep title words broader than before for better recall
    title_words = words[3:10]

    authors: List[str] = []
    if "," in ref:
        authors.append(ref.split(",")[0].lower())
    elif words:
        authors.append(words[0].lower())

    query = " ".join([w for w in (authors + title_words + [year]) if w]).strip()
    title_only = " ".join(title_words[:6]).strip()

    return query, authors, year, doi, title_only


# ---------------------------------------------------------
# Candidate Extraction
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
        title = _norm_text(titles[0]) if titles else ""

        issued = item.get("issued", {}) or item.get("published-print", {}) or item.get("published-online", {})
        year = str((issued.get("date-parts", [[None]])[0][0]))

        for au in (item.get("author") or [])[:6]:
            fam = _safe_strip(au.get("family")).lower()
            if fam:
                authors.append(fam)

    elif src == "openalex":
        doi = _safe_strip(item.get("doi")).replace("https://doi.org/", "")
        title = _norm_text(item.get("title"))
        year = str(item.get("publication_year"))

        for a in (item.get("authorships") or [])[:6]:
            name = a.get("author", {}).get("display_name")
            if name:
                authors.append(name.split()[-1].lower())

    return doi, title, year, authors


# ---------------------------------------------------------
# Queries
# ---------------------------------------------------------

def _query_crossref_by_doi(doi: str) -> List[Dict[str, Any]]:
    if not doi:
        return []
    url = f"https://api.crossref.org/works/{doi}"
    data = _safe_get_json(url, timeout=12)
    if not data or "message" not in data:
        return []
    return [{"source": "crossref", "item": data["message"]}]


def _query_crossref(query: str, rows: int = 5) -> List[Dict[str, Any]]:
    if not query:
        return []
    url = "https://api.crossref.org/works"
    params = {
        "query.bibliographic": query,
        "rows": rows,
        "sort": "score",
        "order": "desc",
    }
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=14)
    items = (data or {}).get("message", {}).get("items", [])
    return [{"source": "crossref", "item": it} for it in items]


def _query_crossref_title_only(title_query: str, rows: int = 8) -> List[Dict[str, Any]]:
    if not title_query:
        return []
    url = "https://api.crossref.org/works"
    params = {
        "query.title": title_query,
        "rows": rows,
        "sort": "score",
        "order": "desc",
    }
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=14)
    items = (data or {}).get("message", {}).get("items", [])
    return [{"source": "crossref", "item": it} for it in items]


def _query_openalex(query: str, rows: int = 5) -> List[Dict[str, Any]]:
    if not query:
        return []
    url = "https://api.openalex.org/works"
    params = {"search": query, "per-page": rows}
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=14)
    items = (data or {}).get("results", [])
    return [{"source": "openalex", "item": it} for it in items]


def _query_openalex_title_only(title_query: str, rows: int = 8) -> List[Dict[str, Any]]:
    if not title_query:
        return []
    url = "https://api.openalex.org/works"
    params = {"search": title_query, "per-page": rows}
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=14)
    items = (data or {}).get("results", [])
    return [{"source": "openalex", "item": it} for it in items]


# ---------------------------------------------------------
# Scoring
# ---------------------------------------------------------

def _score(
    ref_title: str,
    ref_authors: List[str],
    ref_year: str,
    cand_title: str,
    cand_authors: List[str],
    cand_year: str,
) -> Dict[str, Any]:
    title_score = fuzz.token_set_ratio(ref_title, cand_title) if ref_title and cand_title else 0
    partial_title = fuzz.partial_ratio(ref_title, cand_title) if ref_title and cand_title else 0
    blended_title = int((title_score * 0.7) + (partial_title * 0.3))

    author_overlap = len(set(ref_authors).intersection(set(cand_authors)))
    year_match = 1 if ref_year and cand_year and ref_year == cand_year else 0

    score = (blended_title * 1.45) + (author_overlap * 22) + (year_match * 10)

    return {
        "score": int(score),
        "title_score": int(blended_title),
        "author_overlap": int(author_overlap),
        "year_match": int(year_match),
    }


# ---------------------------------------------------------
# Classification
# ---------------------------------------------------------

def _classify(doi_match: bool, title_score: int, score: int) -> str:
    if doi_match:
        return "verified"

    if title_score >= 74:
        return "verified"

    if title_score >= 62:
        return "likely"

    if title_score >= 48:
        return "needs_review"

    return "not_found"


# ---------------------------------------------------------
# Candidate evaluation
# ---------------------------------------------------------

def _best_candidate(
    ref_title: str,
    ref_authors: List[str],
    ref_year: str,
    ref_doi: str,
    candidates: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    best = None
    best_score = -1
    best_meta: Dict[str, Any] = {}

    for cand in candidates:
        doi, title, year, authors = _candidate_fields(cand)

        meta = _score(ref_title, ref_authors, ref_year, title, authors, year)

        doi_match = bool(ref_doi and doi and ref_doi.lower() == doi.lower())
        meta_score = meta["score"] + (40 if doi_match else 0)

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

def _verify_single_reference(ref: str, use_crossref: bool, use_openalex: bool) -> Dict[str, Any]:
    cached = _cache_get(ref)
    if cached:
        return cached

    query, ref_authors, ref_year, ref_doi, title_only = _build_query(ref)
    ref_title = _norm_text(ref)

    row: Dict[str, Any] = {
        "reference": ref,
        "status": "offline",
        "source": "",
        "score": 0,
        "doi": "",
        "matched_title": "",
        "matched_year": "",
        "matched_authors": "",
        "title_score": 0,
        "author_overlap": 0,
        "year_match": 0,
        "query_used": query,
    }

    try:
        # stage 0: DOI shortcut
        candidates: List[Dict[str, Any]] = []
        if ref_doi and use_crossref:
            candidates.extend(_query_crossref_by_doi(ref_doi))

        # stage 1: fast search
        if use_crossref:
            candidates.extend(_query_crossref(query, rows=5))
        if use_openalex:
            candidates.extend(_query_openalex(query, rows=5))

        best, best_meta = _best_candidate(ref_title, ref_authors, ref_year, ref_doi, candidates)

        if best:
            status = _classify(
                best_meta.get("doi_match", False),
                int(best_meta.get("title_score", 0)),
                int(best_meta.get("score", 0)),
            )

            # stage 2: deep recovery only when needed
            if status in {"needs_review", "not_found"}:
                deep_candidates = list(candidates)

                if use_crossref:
                    deep_candidates.extend(_query_crossref(query, rows=12))
                    deep_candidates.extend(_query_crossref_title_only(title_only, rows=8))

                if use_openalex:
                    deep_candidates.extend(_query_openalex(query, rows=12))
                    deep_candidates.extend(_query_openalex_title_only(title_only, rows=8))

                best2, best_meta2 = _best_candidate(ref_title, ref_authors, ref_year, ref_doi, deep_candidates)
                if best2:
                    best = best2
                    best_meta = best_meta2
                    status = _classify(
                        best_meta.get("doi_match", False),
                        int(best_meta.get("title_score", 0)),
                        int(best_meta.get("score", 0)),
                    )

            row.update({
                "status": status,
                "source": best.get("source", ""),
                "score": int(best_meta.get("score", 0)),
                "doi": best_meta.get("doi", ""),
                "matched_title": best_meta.get("title", ""),
                "matched_year": best_meta.get("year", ""),
                "matched_authors": ", ".join(best_meta.get("authors", [])),
                "title_score": int(best_meta.get("title_score", 0)),
                "author_overlap": int(best_meta.get("author_overlap", 0)),
                "year_match": int(best_meta.get("year_match", 0)),
            })
        else:
            row["status"] = "not_found"

    except Exception as e:
        row["status"] = "offline"
        row["error"] = str(e)

    row["status"] = _normalize_verify_status(row.get("status"))
    _cache_set(ref, row)
    return row


# ---------------------------------------------------------
# Parallel Batch Verification
# ---------------------------------------------------------

def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.0,
    use_crossref: bool = True,
    use_openalex: bool = True,
):
    refs = [r for r in (references or []) if _safe_strip(r)]
    if not refs:
        return []

    rows: List[Dict[str, Any]] = [None] * len(refs)  # type: ignore
    workers = min(8, max(1, len(refs)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_idx = {
            executor.submit(_verify_single_reference, ref, use_crossref, use_openalex): i
            for i, ref in enumerate(refs)
        }

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                rows[idx] = future.result()
            except Exception as e:
                rows[idx] = {
                    "reference": refs[idx],
                    "status": "offline",
                    "source": "",
                    "score": 0,
                    "doi": "",
                    "matched_title": "",
                    "matched_year": "",
                    "matched_authors": "",
                    "title_score": 0,
                    "author_overlap": 0,
                    "year_match": 0,
                    "query_used": "",
                    "error": str(e),
                }

    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
