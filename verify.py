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


def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 18) -> Optional[dict]:
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


def _strip_leading_numbering(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)
    return t.strip()


def _extract_first_author_surname(text: str) -> str:
    """
    Light heuristic:
      - If "Surname, Initial": take token before comma
      - Else take first word token
    """
    t = _strip_leading_numbering(text)
    if not t:
        return ""
    if "," in t:
        first = t.split(",", 1)[0].strip()
        return re.sub(r"[^A-Za-z\-']", "", first).lower()
    first = re.split(r"\s+", t)[0].strip()
    return re.sub(r"[^A-Za-z\-']", "", first).lower()


def _extract_doi(text: str) -> str:
    t = text or ""
    m = re.search(r"(10\.\d{4,9}/[^\s]+)", t, flags=re.I)
    if not m:
        return ""
    doi = m.group(1).strip().rstrip(").,;")
    return doi


def _extract_title_guess(ref: str) -> str:
    """
    Better title guess:
    - remove numbering
    - remove leading author block up to year
    - pick first long sentence chunk after year
    """
    t = _strip_leading_numbering(ref)
    t = re.sub(r"\s+", " ", t).strip()

    # remove DOI
    t = re.sub(r"(doi\s*:\s*)?10\.\d{4,9}/\S+", "", t, flags=re.I)
    t = re.sub(r"https?://doi\.org/10\.\d{4,9}/\S+", "", t, flags=re.I)

    # remove author block up to year
    # e.g. "Ochieng, E. ... (2013). The effect of ..."
    t2 = re.split(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?", t, maxsplit=1, flags=re.I)
    if len(t2) >= 3:
        after = t2[2].strip()
    else:
        # fallback: split on year anywhere
        m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", t, flags=re.I)
        after = t[m.end():].strip() if m else t

    # title usually ends at first period
    # keep only first sentence chunk
    after = after.lstrip(". ").strip()
    if "." in after:
        title = after.split(".", 1)[0].strip()
    else:
        title = after.strip()

    # if too short fallback to whole
    if len(title) < 12:
        title = t.strip()

    return title


def _query_crossref(title: str, author: str, year: str, raw_ref: str) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"

    # Prefer title search, Crossref works better when title is present
    q = title if len(title) >= 12 else raw_ref
    params = {
        "query.bibliographic": q,
        "rows": 7,
    }
    if year[:4].isdigit():
        params["filter"] = f"from-pub-date:{year[:4]}-01-01,until-pub-date:{year[:4]}-12-31"

    data = _safe_get_json(url, params=params, timeout=18)
    if not data:
        return []
    items = (data.get("message") or {}).get("items") or []
    return [{"source": "crossref", "item": it, "query_used": q} for it in items]


def _query_openalex(title: str, author: str, year: str, raw_ref: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"

    q = title if len(title) >= 12 else raw_ref
    params = {
        "search": q,
        "per-page": 7
    }

    data = _safe_get_json(url, params=params, timeout=18)
    if not data:
        return []
    results = data.get("results") or []
    return [{"source": "openalex", "item": it, "query_used": q} for it in results]


def _query_semantic_scholar(title: str, author: str, year: str, raw_ref: str) -> List[Dict[str, Any]]:
    """
    Semantic Scholar Graph API (no key required for light use)
    Endpoint: /paper/search
    """
    url = "https://api.semanticscholar.org/graph/v1/paper/search"

    q = title if len(title) >= 12 else raw_ref
    # fields limited for speed
    params = {
        "query": q,
        "limit": 7,
        "fields": "title,year,authors,externalIds,venue"
    }

    data = _safe_get_json(url, params=params, timeout=18)
    if not data:
        return []
    results = data.get("data") or []
    return [{"source": "semantic_scholar", "item": it, "query_used": q} for it in results]


def _candidate_fields(cand: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """
    Returns (doi, title, year, first_author_surname)
    """
    src = cand.get("source")
    item = cand.get("item", {}) or {}

    doi = ""
    title = ""
    year = ""
    first_author = ""

    if src == "crossref":
        doi = (item.get("DOI") or "").strip()
        titles = item.get("title") or []
        title = (titles[0] if titles else "") or ""
        year = str((item.get("published-print", {}).get("date-parts") or [[None]])[0][0] or
                   (item.get("published-online", {}).get("date-parts") or [[None]])[0][0] or "")
        authors = item.get("author") or []
        if authors:
            first_author = (authors[0].get("family") or "").lower()

    elif src == "openalex":
        doi = (item.get("doi") or "").replace("https://doi.org/", "").strip()
        title = (item.get("title") or "") or ""
        year = str(item.get("publication_year") or "")
        auths = item.get("authorships") or []
        if auths and auths[0].get("author"):
            first_author = (auths[0]["author"].get("display_name") or "").split()[-1].lower()

    elif src == "semantic_scholar":
        title = (item.get("title") or "") or ""
        year = str(item.get("year") or "")
        ext = item.get("externalIds") or {}
        doi = (ext.get("DOI") or "").strip() if isinstance(ext, dict) else ""
        authors = item.get("authors") or []
        if authors and isinstance(authors, list):
            # semantic scholar author object has "name"
            nm = (authors[0].get("name") or "").strip()
            if nm:
                first_author = nm.split()[-1].lower()

    return doi, title, year, first_author


def _score(ref_title: str, ref_author: str, ref_year: str, cand_title: str, cand_author: str, cand_year: str) -> Dict[str, Any]:
    title_score = fuzz.token_set_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0
    author_match = 1 if (ref_author and cand_author and ref_author == cand_author) else 0
    year_match = 1 if (ref_year and cand_year and ref_year[:4] == str(cand_year)[:4]) else 0

    # Stronger emphasis on title match
    score = (title_score * 1.2) + (25 * author_match) + (12 * year_match)

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_match": int(author_match),
        "year_match": int(year_match),
    }


def _classify(score: int, author_match: int, year_match: int, title_score: int) -> str:
    # tuned for higher precision
    if title_score >= 92 and (author_match or year_match) and score >= 130:
        return "verified"
    if title_score >= 86 and score >= 118:
        return "likely"
    if title_score >= 75 and score >= 95:
        return "needs_review"
    return "not_found"


def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
    use_semantic_scholar: bool = True,
) -> List[Dict[str, Any]]:
    refs = [r for r in (references or []) if (r or "").strip()]
    if not refs:
        return []

    if max_to_check and max_to_check > 0:
        refs = refs[: max_to_check]

    rows: List[Dict[str, Any]] = []

    for ref in refs:
        ref_raw = (ref or "").strip()

        ref_year = _extract_year(ref_raw)
        ref_author = _extract_first_author_surname(ref_raw)
        ref_doi = _extract_doi(ref_raw)
        ref_title = _extract_title_guess(ref_raw)

        row: Dict[str, Any] = {
            "reference": ref_raw,
            "status": "offline",
            "source": "",
            "score": 0,
            "doi": "",
            "matched_year": "",
            "matched_authors": "",
            "matched_title": "",
            "query_used": "",
            "error": "",
        }

        try:
            # If DOI exists, Crossref is best. We still allow others for fallback.
            candidates: List[Dict[str, Any]] = []

            # Build queries with title rather than full ref to improve OpenAlex and SS
            if use_crossref:
                candidates.extend(_query_crossref(ref_title, ref_author, ref_year, ref_raw))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            if use_openalex:
                candidates.extend(_query_openalex(ref_title, ref_author, ref_year, ref_raw))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            if use_semantic_scholar:
                candidates.extend(_query_semantic_scholar(ref_title, ref_author, ref_year, ref_raw))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            if not candidates:
                row["status"] = "not_found"
                rows.append(row)
                continue

            best = None
            best_meta = None
            best_score = -1

            for cand in candidates:
                cand_doi, cand_title, cand_year, cand_author = _candidate_fields(cand)

                # If ref DOI exists and candidate DOI matches, huge win
                doi_bonus = 0
                if ref_doi and cand_doi and ref_doi.lower() == cand_doi.lower():
                    doi_bonus = 40

                meta = _score(ref_title, ref_author, ref_year, cand_title, cand_author, cand_year)
                meta["score"] = int(meta["score"] + doi_bonus)

                if meta["score"] > best_score:
                    best_score = meta["score"]
                    best = cand
                    best_meta = meta

            # Fill output
            src = best.get("source") if best else ""
            cand_doi, cand_title, cand_year, cand_author = _candidate_fields(best) if best else ("", "", "", "")

            row["source"] = src
            row["score"] = int(best_meta["score"] if best_meta else 0)
            row["doi"] = cand_doi or ""
            row["matched_year"] = str(cand_year or "")
            row["matched_authors"] = cand_author or ""
            row["matched_title"] = cand_title or ""
            row["query_used"] = (best.get("query_used") if best else "") or ref_title or ref_raw

            status = _classify(
                score=int(row["score"]),
                author_match=int(best_meta.get("author_match", 0) if best_meta else 0),
                year_match=int(best_meta.get("year_match", 0) if best_meta else 0),
                title_score=int(best_meta.get("title_score", 0) if best_meta else 0),
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
