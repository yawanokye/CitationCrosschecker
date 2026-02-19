# verify.py
import re
import time
from typing import List, Dict, Any, Optional, Tuple

import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\]\)>,;]+)", re.I)


def _extract_doi(ref: str) -> str:
    if not ref:
        return ""
    m = _DOI_RE.search(ref)
    if not m:
        return ""
    doi = m.group(1).strip().rstrip(".")
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    doi = doi.replace("doi:", "").strip()
    return doi


def _extract_year(text: str) -> str:
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", text or "", flags=re.I)
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""


def _extract_first_author_surname(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    # remove leading numbering like [12] or 12. or 12)
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)
    if "," in t:
        first = t.split(",", 1)[0].strip()
        return re.sub(r"[^A-Za-z\-']", "", first).lower()
    first = re.split(r"\s+", t)[0].strip()
    return re.sub(r"[^A-Za-z\-']", "", first).lower()


def _extract_title_guess(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)
    t = re.sub(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)", "", t, flags=re.I)
    # remove DOI strings to keep title clean
    t = _DOI_RE.sub("", t)
    t = t.replace("doi:", " ")
    t = re.sub(r"\s+", " ", t).strip()
    # keep it short for API search
    if len(t) > 180:
        t = t[:180]
    return t.strip()


def _build_query_used(ref_raw: str) -> str:
    """
    Mimics your better CSV approach:
    Surname YEAR + title guess
    """
    sname = _extract_first_author_surname(ref_raw)
    year = _extract_year(ref_raw)
    title = _extract_title_guess(ref_raw)

    parts = []
    if sname:
        parts.append(sname.capitalize())
    if year:
        parts.append(year[:4])
    if title:
        parts.append(title)

    q = " ".join(parts).strip()
    return q if q else (ref_raw or "").strip()


def _safe_get_json(session: requests.Session, url: str, params: Optional[dict] = None, timeout: int = 12) -> Optional[dict]:
    try:
        r = session.get(url, params=params, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _crossref_by_doi(session: requests.Session, doi: str) -> Optional[dict]:
    url = f"https://api.crossref.org/works/{doi}"
    data = _safe_get_json(session, url, params=None, timeout=12)
    if not data:
        return None
    item = (data.get("message") or {})
    return {"source": "crossref", "item": item}


def _openalex_by_doi(session: requests.Session, doi: str) -> Optional[dict]:
    # OpenAlex: works/doi:10.xxxx/yyy
    url = f"https://api.openalex.org/works/doi:{doi}"
    data = _safe_get_json(session, url, params=None, timeout=12)
    if not data:
        return None
    return {"source": "openalex", "item": data}


def _query_crossref(session: requests.Session, query: str, rows: int = 4) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"
    params = {"query.bibliographic": query, "rows": rows}
    data = _safe_get_json(session, url, params=params, timeout=12)
    if not data:
        return []
    items = (data.get("message") or {}).get("items") or []
    return [{"source": "crossref", "item": it} for it in items]


def _query_openalex(session: requests.Session, query: str, rows: int = 4) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"
    params = {"search": query, "per-page": rows}
    data = _safe_get_json(session, url, params=params, timeout=12)
    if not data:
        return []
    results = data.get("results") or []
    return [{"source": "openalex", "item": it} for it in results]


def _candidate_fields(cand: dict) -> Tuple[str, str, str, str]:
    cand_title = ""
    cand_year = ""
    cand_author = ""
    cand_doi = ""

    if cand.get("source") == "crossref":
        item = cand.get("item", {}) or {}
        cand_doi = (item.get("DOI") or "").strip()
        titles = item.get("title") or []
        cand_title = (titles[0] if titles else "") or ""
        cand_year = str((item.get("published-print", {}).get("date-parts") or [[None]])[0][0] or
                        (item.get("published-online", {}).get("date-parts") or [[None]])[0][0] or "")
        authors = item.get("author") or []
        if authors:
            cand_author = (authors[0].get("family") or "").lower()

    if cand.get("source") == "openalex":
        item = cand.get("item", {}) or {}
        cand_doi = (item.get("doi") or "").replace("https://doi.org/", "").strip()
        cand_title = (item.get("title") or "") or ""
        cand_year = str(item.get("publication_year") or "")
        auths = item.get("authorships") or []
        if auths and auths[0].get("author"):
            cand_author = (auths[0]["author"].get("display_name") or "").split()[-1].lower()

    return cand_title, cand_year, cand_author, cand_doi


def _score(ref_raw: str, cand: dict) -> Dict[str, Any]:
    ref_year = _extract_year(ref_raw)
    ref_author = _extract_first_author_surname(ref_raw)
    ref_title = _extract_title_guess(ref_raw)
    ref_doi = _extract_doi(ref_raw)

    cand_title, cand_year, cand_author, cand_doi = _candidate_fields(cand)

    # Similarities
    title_score = fuzz.token_set_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0
    author_match = 1 if (ref_author and cand_author and ref_author == cand_author) else 0
    year_match = 1 if (ref_year and cand_year and ref_year[:4] == str(cand_year)[:4]) else 0
    doi_match = 1 if (ref_doi and cand_doi and ref_doi.lower() == cand_doi.lower()) else 0

    # Weighted score, tuned to be stable
    score = (
        (1.25 * title_score) +
        (25 * author_match) +
        (12 * year_match) +
        (40 * doi_match)
    )

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_match": int(author_match),
        "year_match": int(year_match),
        "doi_match": int(doi_match),
        "doi": cand_doi,
        "matched_title": cand_title,
        "matched_year": cand_year,
        "matched_first_author": cand_author,
    }


def _classify(meta: Dict[str, Any]) -> str:
    score = int(meta.get("score", 0))
    doi_match = int(meta.get("doi_match", 0))
    author_match = int(meta.get("author_match", 0))
    year_match = int(meta.get("year_match", 0))
    title_score = int(meta.get("title_score", 0))

    if doi_match:
        return "verified"
    if title_score >= 90 and author_match and year_match and score >= 140:
        return "verified"
    if title_score >= 80 and (author_match or year_match) and score >= 115:
        return "likely"
    if title_score >= 65 and score >= 95:
        return "needs_review"
    return "not_found"


def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> List[Dict[str, Any]]:
    refs = [r for r in (references or []) if (r or "").strip()]
    if not refs:
        return []

    if max_to_check and max_to_check > 0:
        refs = refs[: max_to_check]

    rows: List[Dict[str, Any]] = []

    session = requests.Session()
    session.headers.update({"User-Agent": "CitationCrosschecker/1.0 (UCC)"})


    for ref in refs:
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
            doi = _extract_doi(ref)
            query_used = _build_query_used(ref)
            row["query_used"] = query_used

            candidates: List[Dict[str, Any]] = []

            # 1) DOI-first (best precision)
            if doi:
                if use_crossref:
                    hit = _crossref_by_doi(session, doi)
                    if hit:
                        candidates.append(hit)
                if use_openalex:
                    hit = _openalex_by_doi(session, doi)
                    if hit:
                        candidates.append(hit)

            # 2) Search if DOI absent or DOI lookup failed
            if not candidates:
                if use_crossref:
                    candidates.extend(_query_crossref(session, query_used, rows=4))
                    time.sleep(max(0.0, float(throttle_s or 0.0)))
                if use_openalex:
                    candidates.extend(_query_openalex(session, query_used, rows=4))
                    time.sleep(max(0.0, float(throttle_s or 0.0)))

            if not candidates:
                row["status"] = "not_found"
                rows.append(row)
                continue

            best = None
            best_meta = None
            best_score = -1

            for cand in candidates:
                meta = _score(ref, cand)
                if meta["score"] > best_score:
                    best_score = meta["score"]
                    best = cand
                    best_meta = meta

            row["source"] = best.get("source") if best else ""
            row["score"] = int(best_meta["score"] if best_meta else 0)
            row["doi"] = best_meta.get("doi", "") if best_meta else ""
            row["matched_year"] = str(best_meta.get("matched_year", "") if best_meta else "")
            row["matched_first_author"] = best_meta.get("matched_first_author", "") if best_meta else ""
            row["matched_title"] = best_meta.get("matched_title", "") if best_meta else ""

            status = _classify(best_meta or {})
            row["status"] = _normalize_verify_status(status)
            rows.append(row)

        except Exception as e:
            row["status"] = "offline"
            row["error"] = str(e)
            rows.append(row)

    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
