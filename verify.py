# verify.py
import re
import time
from typing import List, Dict, Any, Optional

import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower()
    st = st.replace(" ", "_")  # "not found" -> "not_found"
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 15) -> Optional[dict]:
    try:
        r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "CitationCrosschecker/1.0"})
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _extract_year(text: str) -> str:
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", text or "", flags=re.I)
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""


def _extract_first_author_surname(text: str) -> str:
    # Very lightweight heuristic:
    # - If "Surname, Initial" style: take token before comma
    # - Else take first token that looks like a name
    t = (text or "").strip()
    if not t:
        return ""
    if "," in t:
        first = t.split(",", 1)[0].strip()
        return re.sub(r"[^A-Za-z\-']", "", first).lower()
    first = re.split(r"\s+", t)[0].strip()
    return re.sub(r"[^A-Za-z\-']", "", first).lower()


def _extract_title_guess(text: str) -> str:
    # Not perfect, but helps scoring
    t = re.sub(r"\s+", " ", (text or "").strip())
    # remove leading numbering like [12] or 12. or 12)
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)
    # remove year in parentheses
    t = re.sub(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)", "", t, flags=re.I)
    return t.strip()


def _score_candidate(ref_raw: str, cand: dict) -> Dict[str, Any]:
    ref_year = _extract_year(ref_raw)
    ref_author = _extract_first_author_surname(ref_raw)
    ref_title = _extract_title_guess(ref_raw)

    # Candidate fields vary by API
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

    # Similarity scores
    title_score = fuzz.token_set_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0
    author_match = 1 if (ref_author and cand_author and ref_author == cand_author) else 0
    year_match = 1 if (ref_year and cand_year and ref_year[:4] == str(cand_year)[:4]) else 0

    # Overall score (simple, stable)
    score = title_score + (20 * author_match) + (10 * year_match)

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_match": int(author_match),
        "year_match": int(year_match),
        "doi": cand_doi,
        "matched_title": cand_title,
        "matched_year": cand_year,
        "matched_first_author": cand_author,
    }


def _classify(score: int, author_match: int, year_match: int) -> str:
    # Conservative thresholds
    if score >= 120 and author_match and year_match:
        return "verified"
    if score >= 105 and (author_match or year_match):
        return "likely"
    if score >= 85:
        return "needs_review"
    return "not_found"


def _query_crossref(ref_raw: str) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"
    params = {"query.bibliographic": ref_raw, "rows": 5}
    data = _safe_get_json(url, params=params, timeout=15)
    if not data:
        return []
    items = (data.get("message") or {}).get("items") or []
    return [{"source": "crossref", "item": it} for it in items]


def _query_openalex(ref_raw: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"
    params = {"search": ref_raw, "per-page": 5}
    data = _safe_get_json(url, params=params, timeout=15)
    if not data:
        return []
    results = data.get("results") or []
    return [{"source": "openalex", "item": it} for it in results]


def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.25,
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

    for ref in refs:
        row: Dict[str, Any] = {
            "reference": ref,
            "status": "offline",      # overwritten below if online works
            "source": "",
            "score": 0,
            "doi": "",
            "matched_year": "",
            "matched_first_author": "",
            "matched_title": "",
            "query_used": ref,
            "error": "",
        }

        try:
            candidates: List[Dict[str, Any]] = []
            if use_crossref:
                candidates.extend(_query_crossref(ref))
                time.sleep(max(0.0, float(throttle_s or 0.0)))
            if use_openalex:
                candidates.extend(_query_openalex(ref))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            if not candidates:
                row["status"] = "not_found"
                rows.append(row)
                continue

            best = None
            best_score = -1
            best_meta = None

            for cand in candidates:
                meta = _score_candidate(ref, cand)
                if meta["score"] > best_score:
                    best_score = meta["score"]
                    best = cand
                    best_meta = meta

            # fill row from best
            row["source"] = best.get("source") if best else ""
            row["score"] = int(best_meta["score"] if best_meta else 0)
            row["doi"] = best_meta.get("doi", "") if best_meta else ""
            row["matched_year"] = str(best_meta.get("matched_year", "") if best_meta else "")
            row["matched_first_author"] = best_meta.get("matched_first_author", "") if best_meta else ""
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

    # Final enforcement (guarantee)
    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
