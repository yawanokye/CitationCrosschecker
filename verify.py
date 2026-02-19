# verify.py
import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple

import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}

CROSSREF_URL = "https://api.crossref.org/works"
OPENALEX_URL = "https://api.openalex.org/works"
SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

UA = "CitationCrosschecker/1.1 (+https://citationcrosschecker.onrender.com)"


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    return st if st in _ALLOWED_VERIFY_STATUSES else "needs_review"


def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 20, headers: Optional[dict] = None) -> Optional[dict]:
    try:
        h = {"User-Agent": UA}
        if headers:
            h.update(headers)
        r = requests.get(url, params=params, timeout=timeout, headers=h)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _extract_year(text: str) -> str:
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", text or "", flags=re.I)
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""


def _extract_doi(text: str) -> str:
    if not text:
        return ""
    # DOI patterns in many styles
    m = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", text, flags=re.I)
    if not m:
        return ""
    doi = m.group(1).rstrip(").,; ")
    return doi.lower()


def _strip_leading_numbering(t: str) -> str:
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)
    return t.strip()


def _extract_first_author_surname(text: str) -> str:
    t = _strip_leading_numbering((text or "").strip())
    if not t:
        return ""
    # "Surname, Initial" -> Surname
    if "," in t:
        first = t.split(",", 1)[0].strip()
        first = re.sub(r"[^A-Za-z\-']", "", first)
        return first.lower()
    # fallback
    first = re.split(r"\s+", t)[0].strip()
    first = re.sub(r"[^A-Za-z\-']", "", first)
    return first.lower()


def _extract_title_guess(text: str) -> str:
    """
    Best-effort title extraction to build queries:
    - remove numbering
    - remove leading authors chunk up to (year).
    - remove DOI blocks
    """
    t = _strip_leading_numbering((text or "").strip())
    if not t:
        return ""

    # remove DOI
    t = re.sub(r"(doi\s*:?\s*)?10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", "", t, flags=re.I)
    t = re.sub(r"https?://doi\.org/10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", "", t, flags=re.I)

    # split around year marker
    m = re.search(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)", t, flags=re.I)
    if m:
        after = t[m.end():].strip()
    else:
        # if no (year), use whole
        after = t

    # many styles: title is first sentence-like chunk
    # keep up to first period if it looks long enough
    after = re.sub(r"\s+", " ", after).strip()
    if len(after) > 10 and "." in after:
        head = after.split(".", 1)[0].strip()
        if len(head) >= 12:
            return head

    return after


def _build_query(ref_raw: str) -> Tuple[str, str, str, str]:
    """
    Returns (query, title_guess, author_surname, year)
    """
    year = _extract_year(ref_raw)
    author = _extract_first_author_surname(ref_raw)
    title = _extract_title_guess(ref_raw)

    bits = []
    if title:
        bits.append(title)
    if author:
        bits.append(author)
    if year:
        bits.append(year[:4])

    q = " ".join(bits).strip()
    if not q:
        q = ref_raw.strip()

    return q, title, author, year


def _score_candidate(ref_raw: str, cand: dict, title_guess: str, author_guess: str, year_guess: str) -> Dict[str, Any]:
    """
    Stronger, more stable scoring:
    - title similarity dominates
    - author overlap bonus
    - year match bonus
    - DOI bonus if we extracted DOI and candidate has DOI match
    """
    ref_doi = _extract_doi(ref_raw)
    ref_title = title_guess or _extract_title_guess(ref_raw)
    ref_author = author_guess or _extract_first_author_surname(ref_raw)
    ref_year = year_guess or _extract_year(ref_raw)

    cand_title = ""
    cand_year = ""
    cand_authors = []
    cand_doi = ""

    src = cand.get("source")

    if src == "crossref":
        item = cand.get("item", {}) or {}
        cand_doi = (item.get("DOI") or "").strip().lower()
        titles = item.get("title") or []
        cand_title = (titles[0] if titles else "") or ""
        cand_year = str(
            (item.get("published-print", {}).get("date-parts") or [[None]])[0][0]
            or (item.get("published-online", {}).get("date-parts") or [[None]])[0][0]
            or ""
        )
        authors = item.get("author") or []
        for a in authors[:6]:
            fam = (a.get("family") or "").strip()
            if fam:
                cand_authors.append(fam.lower())

    elif src == "openalex":
        item = cand.get("item", {}) or {}
        cand_doi = (item.get("doi") or "").replace("https://doi.org/", "").strip().lower()
        cand_title = (item.get("title") or "") or ""
        cand_year = str(item.get("publication_year") or "")
        auths = item.get("authorships") or []
        for a in auths[:6]:
            au = (a.get("author") or {}).get("display_name") or ""
            if au:
                cand_authors.append(au.split()[-1].lower())

    elif src == "semantic_scholar":
        item = cand.get("item", {}) or {}
        cand_title = (item.get("title") or "") or ""
        cand_year = str(item.get("year") or "")
        # Semantic Scholar DOI is in externalIds sometimes
        ext = item.get("externalIds") or {}
        cand_doi = (ext.get("DOI") or "").strip().lower()
        authors = item.get("authors") or []
        for a in authors[:6]:
            nm = (a.get("name") or "").strip()
            if nm:
                cand_authors.append(nm.split()[-1].lower())

    # Similarities
    title_score = fuzz.token_set_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0

    author_overlap = 0
    if ref_author and cand_authors:
        author_overlap = 1 if ref_author.lower() in set(cand_authors) else 0

    year_match = 0
    if ref_year and cand_year:
        year_match = 1 if ref_year[:4] == str(cand_year)[:4] else 0

    doi_bonus = 0
    if ref_doi and cand_doi and ref_doi.lower() == cand_doi.lower():
        doi_bonus = 50

    # total score
    score = (
        (title_score * 1.2)  # title dominates
        + (25 * author_overlap)
        + (12 * year_match)
        + doi_bonus
    )

    return {
        "score": int(round(score)),
        "title_score": int(title_score),
        "author_match": int(author_overlap),
        "year_match": int(year_match),
        "doi": cand_doi,
        "matched_title": cand_title,
        "matched_year": cand_year,
        "matched_first_author": cand_authors[0] if cand_authors else "",
    }


def _classify(score: int, title_score: int, author_match: int, year_match: int, doi_hit: bool) -> str:
    # DOI hit is decisive
    if doi_hit and score >= 120:
        return "verified"

    # Strong title + at least one of author/year
    if title_score >= 90 and (author_match or year_match):
        return "verified"

    if title_score >= 85 and (author_match or year_match):
        return "likely"

    if title_score >= 75:
        return "needs_review"

    return "not_found"


def _query_crossref(query: str, year: str) -> List[Dict[str, Any]]:
    params = {"query.bibliographic": query, "rows": 7}
    y4 = (year or "")[:4]
    if y4.isdigit():
        # light filter to reduce noise
        params["filter"] = f"from-pub-date:{y4}-01-01,until-pub-date:{y4}-12-31"
    data = _safe_get_json(CROSSREF_URL, params=params, timeout=20)
    if not data:
        return []
    items = (data.get("message") or {}).get("items") or []
    return [{"source": "crossref", "item": it} for it in items]


def _query_openalex(query: str, year: str) -> List[Dict[str, Any]]:
    params = {"search": query, "per-page": 7}
    y4 = (year or "")[:4]
    if y4.isdigit():
        params["filter"] = f"from_publication_date:{y4}-01-01,to_publication_date:{y4}-12-31"
    data = _safe_get_json(OPENALEX_URL, params=params, timeout=20)
    if not data:
        return []
    results = data.get("results") or []
    return [{"source": "openalex", "item": it} for it in results]


def _query_semantic_scholar(query: str) -> List[Dict[str, Any]]:
    # API key is optional. If you have one, set env var SEMANTIC_SCHOLAR_API_KEY
    key = (os.getenv("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
    headers = {}
    if key:
        headers["x-api-key"] = key

    params = {
        "query": query,
        "limit": 7,
        "fields": "title,year,authors,externalIds,venue,url",
    }
    data = _safe_get_json(SEMANTIC_SCHOLAR_SEARCH_URL, params=params, timeout=20, headers=headers)
    if not data:
        return []
    results = data.get("data") or []
    return [{"source": "semantic_scholar", "item": it} for it in results]


def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
    use_semantic_scholar: bool = True,
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
        query, title_guess, author_guess, year_guess = _build_query(ref)
        ref_doi = _extract_doi(ref)

        row: Dict[str, Any] = {
            "reference": ref,
            "status": "offline",
            "source": "",
            "score": 0,
            "doi": ref_doi,
            "matched_year": "",
            "matched_first_author": "",
            "matched_title": "",
            "query_used": query,
            "error": "",
        }

        try:
            candidates: List[Dict[str, Any]] = []

            # If DOI exists, push DOI into query strongly
            q = query
            if ref_doi:
                q = f"{ref_doi} {query}".strip()

            if use_crossref:
                candidates.extend(_query_crossref(q, year_guess))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            if use_openalex:
                candidates.extend(_query_openalex(q, year_guess))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            if use_semantic_scholar:
                candidates.extend(_query_semantic_scholar(q))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            if not candidates:
                row["status"] = "not_found"
                rows.append(row)
                continue

            best = None
            best_meta = None
            best_score = -1

            for cand in candidates:
                meta = _score_candidate(ref, cand, title_guess, author_guess, year_guess)
                if meta["score"] > best_score:
                    best_score = meta["score"]
                    best = cand
                    best_meta = meta

            src = best.get("source") if best else ""
            row["source"] = src
            row["score"] = int(best_meta["score"] if best_meta else 0)

            cand_doi = (best_meta.get("doi", "") if best_meta else "") or ""
            row["doi"] = ref_doi or cand_doi
            row["matched_year"] = str(best_meta.get("matched_year", "") if best_meta else "")
            row["matched_first_author"] = best_meta.get("matched_first_author", "") if best_meta else ""
            row["matched_title"] = best_meta.get("matched_title", "") if best_meta else ""

            doi_hit = bool(ref_doi and cand_doi and ref_doi.lower() == cand_doi.lower())
            status = _classify(
                score=int(row["score"]),
                title_score=int(best_meta.get("title_score", 0) if best_meta else 0),
                author_match=int(best_meta.get("author_match", 0) if best_meta else 0),
                year_match=int(best_meta.get("year_match", 0) if best_meta else 0),
                doi_hit=doi_hit,
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
