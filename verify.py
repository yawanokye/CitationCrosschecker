# verify.py
import re
import time
from typing import List, Dict, Any, Optional

import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower()
    st = st.replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


def _normalize_doi(doi: str) -> str:
    d = (doi or "").strip()
    if not d:
        return ""
    d = d.replace("https://doi.org/", "").replace("http://doi.org/", "")
    d = d.replace("https://dx.doi.org/", "").replace("http://dx.doi.org/", "")
    return d.strip()


def _safe_get_json(
    session: requests.Session,
    url: str,
    params: Optional[dict] = None,
    timeout: int = 12,
    retries: int = 2,
    backoff_s: float = 0.6,
) -> Optional[dict]:
    headers = {"User-Agent": "CitationCrosschecker/1.0"}
    for i in range(max(0, int(retries)) + 1):
        try:
            r = session.get(url, params=params, timeout=timeout, headers=headers)
            if r.status_code == 200:
                return r.json()

            if r.status_code in (429, 500, 502, 503, 504):
                sleep_for = backoff_s * (2 ** i)
                ra = r.headers.get("Retry-After")
                if ra and ra.isdigit():
                    sleep_for = max(sleep_for, float(ra))
                time.sleep(min(8.0, sleep_for))
                continue

            return None
        except Exception:
            time.sleep(min(8.0, backoff_s * (2 ** i)))
    return None


def _extract_year(text: str) -> str:
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", text or "", flags=re.I)
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""


def _extract_first_author_surname(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    # remove leading numbering
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
    return t.strip()


def _score_candidate(ref_raw: str, cand: dict) -> Dict[str, Any]:
    ref_year = _extract_year(ref_raw)
    ref_author = _extract_first_author_surname(ref_raw)
    ref_title = _extract_title_guess(ref_raw)

    cand_title = ""
    cand_year = ""
    cand_author = ""
    cand_authors = ""
    cand_doi = ""

    if cand.get("source") == "crossref":
        item = cand.get("item", {}) or {}
        cand_doi = _normalize_doi((item.get("DOI") or "").strip())
        titles = item.get("title") or []
        cand_title = (titles[0] if titles else "") or ""
        cand_year = str((item.get("published-print", {}).get("date-parts") or [[None]])[0][0] or
                        (item.get("published-online", {}).get("date-parts") or [[None]])[0][0] or "")
        authors = item.get("author") or []
        if authors:
            cand_author = (authors[0].get("family") or "").lower()
            cand_authors = ", ".join([(a.get("family") or "").strip() for a in authors[:6] if (a.get("family") or "").strip()])

    if cand.get("source") == "openalex":
        item = cand.get("item", {}) or {}
        cand_doi = _normalize_doi((item.get("doi") or "").replace("https://doi.org/", "").strip())
        cand_title = (item.get("title") or "") or ""
        cand_year = str(item.get("publication_year") or "")
        auths = item.get("authorships") or []
        if auths and auths[0].get("author"):
            cand_author = (auths[0]["author"].get("display_name") or "").split()[-1].lower()
        cand_authors = ", ".join(
            [((a.get("author") or {}).get("display_name") or "").strip() for a in auths[:6] if ((a.get("author") or {}).get("display_name") or "").strip()]
        )

    title_score = fuzz.token_set_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0
    author_match = 1 if (ref_author and cand_author and ref_author == cand_author) else 0
    year_match = 1 if (ref_year and cand_year and ref_year[:4] == str(cand_year)[:4]) else 0

    score = int(title_score) + (20 * author_match) + (10 * year_match)

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_match": int(author_match),
        "year_match": int(year_match),
        "doi": cand_doi,
        "matched_title": cand_title,
        "matched_year": cand_year,
        "matched_first_author": cand_author,
        "matched_authors": cand_authors,
    }


def _classify(score: int, author_match: int, year_match: int) -> str:
    # Stable conservative thresholds
    if score >= 120 and author_match and year_match:
        return "verified"
    if score >= 105 and (author_match or year_match):
        return "likely"
    if score >= 85:
        return "needs_review"
    return "not_found"


def _query_crossref(session: requests.Session, ref_raw: str) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"
    params = {"query.bibliographic": ref_raw, "rows": 5}
    data = _safe_get_json(session, url, params=params, timeout=12, retries=2)
    if not data:
        return []
    items = (data.get("message") or {}).get("items") or []
    return [{"source": "crossref", "item": it} for it in items]


def _query_openalex(session: requests.Session, ref_raw: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"
    # ✅ FIX: correct OpenAlex param is per_page, not per-page
    params = {"search": ref_raw, "per_page": 5}
    data = _safe_get_json(session, url, params=params, timeout=12, retries=2)
    if not data:
        return []
    results = data.get("results") or []
    return [{"source": "openalex", "item": it} for it in results]


def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.15,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> List[Dict[str, Any]]:
    """
    Returns rows with status strictly in:
      verified, likely, needs_review, not_found, offline
    """
    refs = [r for r in (references or []) if (r or "").strip()]
    if not refs:
        return []

    if max_to_check and max_to_check > 0:
        refs = refs[: max_to_check]

    rows: List[Dict[str, Any]] = []
    session = requests.Session()

    for ref in refs:
        row: Dict[str, Any] = {
            "reference": ref,
            "status": "offline",
            "source": "",
            "score": 0,
            "doi": "",
            "matched_year": "",
            "matched_first_author": "",
            "matched_authors": "",
            "matched_title": "",
            "query_used": ref,
            "error": "",
        }

        try:
            candidates: List[Dict[str, Any]] = []

            if use_crossref:
                candidates.extend(_query_crossref(session, ref))
                if throttle_s:
                    time.sleep(max(0.0, float(throttle_s)))

            if use_openalex:
                candidates.extend(_query_openalex(session, ref))
                if throttle_s:
                    time.sleep(max(0.0, float(throttle_s)))

            if not candidates:
                row["status"] = "not_found"
                rows.append(row)
                continue

            best_score = -1
            best = None
            best_meta = None

            for cand in candidates:
                meta = _score_candidate(ref, cand)
                if meta["score"] > best_score:
                    best_score = meta["score"]
                    best = cand
                    best_meta = meta

            row["source"] = best.get("source") if best else ""
            row["score"] = int(best_meta["score"] if best_meta else 0)
            row["doi"] = best_meta.get("doi", "") if best_meta else ""
            row["matched_year"] = str(best_meta.get("matched_year", "") if best_meta else "")
            row["matched_first_author"] = best_meta.get("matched_first_author", "") if best_meta else ""
            row["matched_authors"] = best_meta.get("matched_authors", "") if best_meta else ""
            row["matched_title"] = best_meta.get("matched_title", "") if best_meta else ""

            status = _classify(
                score=int(row["score"]),
                author_match=int(best_meta.get("author_match", 0) if best_meta else 0),
                year_match=int(best_meta.get("year_match", 0) if best_meta else 0),
            )
            row["status"] = _normalize_verify_status(status)
            rows.append(row)

        except Exception as e:
            row["status"] = "offline"
            row["error"] = str(e)
            rows.append(row)

    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
