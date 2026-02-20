# verify.py
import re
import time
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "CitationCrosschecker/1.0 (UCC)"})
    return s


def _safe_get_json(sess: requests.Session, url: str, params: Optional[dict] = None, timeout: int = 15) -> Optional[dict]:
    try:
        r = sess.get(url, params=params, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _extract_year(text: str) -> str:
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", text or "", flags=re.I)
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""


def _extract_doi(text: str) -> str:
    t = text or ""
    t = t.replace("https://doi.org/", "").replace("http://doi.org/", "")
    m = re.search(r"\b(10\.\d{4,9}/[^\s]+)\b", t, flags=re.I)
    if not m:
        return ""
    doi = m.group(1).strip().rstrip(").,;")
    return doi.lower()


def _extract_first_author_surname(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    # remove leading numbering
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)
    # surname before comma
    if "," in t:
        first = t.split(",", 1)[0].strip()
        first = re.sub(r"\bJr\.?\b", "", first, flags=re.I).strip()
        return re.sub(r"[^A-Za-z\-']", "", first).lower()
    first = re.split(r"\s+", t)[0].strip()
    return re.sub(r"[^A-Za-z\-']", "", first).lower()


def _extract_title_guess(ref: str) -> str:
    """
    Better title guess:
    - If APA: "... (2013). Title. Journal ..."
    - Use the text after year parenthesis up to next period.
    """
    t = re.sub(r"\s+", " ", (ref or "").strip())
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)

    m = re.search(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\s*\.?\s*(.+)$", t, flags=re.I)
    if m:
        rest = m.group(3).strip()
        # title until next period (avoid tiny)
        parts = rest.split(". ")
        if parts:
            title = parts[0].strip().strip(".")
            if len(title) >= 8:
                return title
    # fallback: remove year then return remainder
    t = re.sub(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)", "", t, flags=re.I)
    return t.strip()


def _build_query(ref_raw: str) -> Tuple[str, str, str, str]:
    """
    Return (doi, year, author, title)
    """
    doi = _extract_doi(ref_raw)
    year = _extract_year(ref_raw)
    author = _extract_first_author_surname(ref_raw)
    title = _extract_title_guess(ref_raw)
    return doi, year, author, title


# -----------------------------
# Source queries
# -----------------------------
def _crossref_by_doi(sess: requests.Session, doi: str) -> List[Dict[str, Any]]:
    url = f"https://api.crossref.org/works/{doi}"
    data = _safe_get_json(sess, url, params=None, timeout=15)
    if not data:
        return []
    item = (data.get("message") or {})
    return [{"source": "crossref", "item": item, "query_used": f"doi:{doi}"}]


def _query_crossref(sess: requests.Session, q: str) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"
    params = {"query.bibliographic": q, "rows": 6}
    data = _safe_get_json(sess, url, params=params, timeout=15)
    if not data:
        return []
    items = (data.get("message") or {}).get("items") or []
    return [{"source": "crossref", "item": it, "query_used": q} for it in items]


def _openalex_by_doi(sess: requests.Session, doi: str) -> List[Dict[str, Any]]:
    # OpenAlex canonical: /works/https://doi.org/<doi>
    url = "https://api.openalex.org/works/" + "https://doi.org/" + doi
    data = _safe_get_json(sess, url, params=None, timeout=15)
    if not data:
        return []
    return [{"source": "openalex", "item": data, "query_used": f"doi:{doi}"}]


def _query_openalex(sess: requests.Session, title: str, author: str, year: str) -> List[Dict[str, Any]]:
    # FIX: parameter is per_page (not per-page)
    url = "https://api.openalex.org/works"
    q = title if title else ""
    params = {"search": q, "per_page": 8}
    # restrict by year if available
    if year and year[:4].isdigit():
        params["filter"] = f"publication_year:{year[:4]}"
    data = _safe_get_json(sess, url, params=params, timeout=15)
    if not data:
        return []
    results = data.get("results") or []
    return [{"source": "openalex", "item": it, "query_used": q} for it in results]


def _semantic_by_doi(sess: requests.Session, doi: str) -> List[Dict[str, Any]]:
    url = "https://api.semanticscholar.org/graph/v1/paper/DOI:" + doi
    params = {"fields": "title,year,authors,externalIds,venue"}
    data = _safe_get_json(sess, url, params=params, timeout=15)
    if not data:
        return []
    return [{"source": "semantic", "item": data, "query_used": f"doi:{doi}"}]


def _query_semantic(sess: requests.Session, q: str) -> List[Dict[str, Any]]:
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {"query": q, "limit": 8, "fields": "title,year,authors,externalIds,venue"}
    data = _safe_get_json(sess, url, params=params, timeout=15)
    if not data:
        return []
    results = data.get("data") or []
    return [{"source": "semantic", "item": it, "query_used": q} for it in results]


# -----------------------------
# Scoring
# -----------------------------
def _cand_fields(cand: Dict[str, Any]) -> Tuple[str, str, str, str]:
    src = cand.get("source")
    it = cand.get("item", {}) or {}

    title = ""
    year = ""
    doi = ""
    author0 = ""

    if src == "crossref":
        titles = it.get("title") or []
        title = (titles[0] if titles else "") or ""
        doi = (it.get("DOI") or "").strip().lower()
        year = ""
        dp = (it.get("published-print", {}).get("date-parts") or [[None]])[0][0]
        if not dp:
            dp = (it.get("published-online", {}).get("date-parts") or [[None]])[0][0]
        year = str(dp or "")
        auths = it.get("author") or []
        if auths:
            author0 = (auths[0].get("family") or "").lower()

    elif src == "openalex":
        title = (it.get("title") or "") or ""
        year = str(it.get("publication_year") or "")
        doi = (it.get("doi") or "").replace("https://doi.org/", "").strip().lower()
        auths = it.get("authorships") or []
        if auths and auths[0].get("author"):
            author0 = (auths[0]["author"].get("display_name") or "").split()[-1].lower()

    elif src == "semantic":
        title = (it.get("title") or "") or ""
        year = str(it.get("year") or "")
        ext = it.get("externalIds") or {}
        doi = (ext.get("DOI") or ext.get("doi") or "").strip().lower()
        auths = it.get("authors") or []
        if auths and auths[0].get("name"):
            author0 = (auths[0]["name"].split()[-1] or "").lower()

    return title, year, doi, author0


def _score(ref_raw: str, cand: Dict[str, Any]) -> Dict[str, Any]:
    doi_r, year_r, author_r, title_r = _build_query(ref_raw)
    c_title, c_year, c_doi, c_author = _cand_fields(cand)

    # DOI match is king
    doi_match = 1 if (doi_r and c_doi and doi_r == c_doi) else 0

    title_score = fuzz.token_set_ratio(title_r, c_title) if (title_r and c_title) else 0
    author_match = 1 if (author_r and c_author and author_r == c_author) else 0
    year_match = 1 if (year_r and c_year and year_r[:4] == str(c_year)[:4]) else 0

    # Overall score weights
    score = 0
    score += 120 if doi_match else 0
    score += int(title_score)
    score += 25 * author_match
    score += 12 * year_match

    return {
        "score": int(score),
        "doi_match": int(doi_match),
        "title_score": int(title_score),
        "author_match": int(author_match),
        "year_match": int(year_match),
        "doi": c_doi,
        "matched_title": c_title,
        "matched_year": c_year,
        "matched_first_author": c_author,
    }


def _classify(meta: Dict[str, Any]) -> str:
    score = int(meta.get("score", 0))
    doi_match = int(meta.get("doi_match", 0))
    a = int(meta.get("author_match", 0))
    y = int(meta.get("year_match", 0))
    t = int(meta.get("title_score", 0))

    if doi_match:
        return "verified"
    if score >= 130 and (a or y) and t >= 80:
        return "verified"
    if score >= 110 and (a or y) and t >= 70:
        return "likely"
    if score >= 90:
        return "needs_review"
    return "not_found"


# -----------------------------
# Batch verification
# -----------------------------
def _verify_one(
    ref: str,
    throttle_s: float,
    use_crossref: bool,
    use_openalex: bool,
    use_semantic: bool,
) -> Dict[str, Any]:
    sess = _session()
    doi, year, author, title = _build_query(ref)

    row: Dict[str, Any] = {
        "reference": ref,
        "status": "offline",
        "source": "",
        "score": 0,
        "doi": "",
        "matched_year": "",
        "matched_first_author": "",
        "matched_title": "",
        "query_used": "",
        "error": "",
    }

    try:
        candidates: List[Dict[str, Any]] = []

        # 1) DOI-first
        if doi:
            if use_crossref:
                candidates.extend(_crossref_by_doi(sess, doi))
                time.sleep(max(0.0, float(throttle_s)))
            if use_openalex:
                candidates.extend(_openalex_by_doi(sess, doi))
                time.sleep(max(0.0, float(throttle_s)))
            if use_semantic:
                candidates.extend(_semantic_by_doi(sess, doi))
                time.sleep(max(0.0, float(throttle_s)))

        # 2) Structured query: prefer title, else full ref
        q = title if title else ref

        # Run remaining sources
        if use_crossref:
            candidates.extend(_query_crossref(sess, q))
            time.sleep(max(0.0, float(throttle_s)))
        if use_openalex:
            candidates.extend(_query_openalex(sess, title=title, author=author, year=year))
            time.sleep(max(0.0, float(throttle_s)))
        if use_semantic:
            candidates.extend(_query_semantic(sess, q))
            time.sleep(max(0.0, float(throttle_s)))

        if not candidates:
            row["status"] = "not_found"
            return row

        best = None
        best_meta = None
        best_score = -1

        for cand in candidates:
            meta = _score(ref, cand)
            if meta["score"] > best_score:
                best_score = meta["score"]
                best = cand
                best_meta = meta

        src = best.get("source") if best else ""
        row["source"] = src
        row["score"] = int(best_meta.get("score", 0) if best_meta else 0)
        row["doi"] = (best_meta.get("doi", "") if best_meta else "") or ""
        row["matched_year"] = str(best_meta.get("matched_year", "") if best_meta else "")
        row["matched_first_author"] = best_meta.get("matched_first_author", "") if best_meta else ""
        row["matched_title"] = best_meta.get("matched_title", "") if best_meta else ""

        # pick query_used from winning candidate if present
        row["query_used"] = best.get("query_used", "") if isinstance(best, dict) else ""

        status = _classify(best_meta or {})
        row["status"] = _normalize_verify_status(status)
        return row

    except Exception as e:
        row["status"] = "offline"
        row["error"] = str(e)
        return row


def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.08,
    use_crossref: bool = True,
    use_openalex: bool = True,
    use_semantic: bool = True,
) -> List[Dict[str, Any]]:
    refs = [r for r in (references or []) if (r or "").strip()]
    if not refs:
        return []

    if max_to_check and max_to_check > 0:
        refs = refs[: max_to_check]

    # modest concurrency speeds up verification a lot
    rows: List[Dict[str, Any]] = []
    workers = 6 if len(refs) >= 6 else max(2, len(refs))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [
            ex.submit(_verify_one, ref, float(throttle_s or 0.0), bool(use_crossref), bool(use_openalex), bool(use_semantic))
            for ref in refs
        ]
        for f in as_completed(futs):
            rows.append(f.result())

    # Keep original order
    order = {ref: i for i, ref in enumerate(refs)}
    rows.sort(key=lambda r: order.get(r.get("reference", ""), 10**9))

    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
