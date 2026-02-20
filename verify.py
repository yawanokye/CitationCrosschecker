# verify.py
import os
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


def _s(x: Any) -> str:
    if x is None:
        return ""
    try:
        return str(x)
    except Exception:
        return ""


def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 18) -> Optional[dict]:
    mailto = (os.getenv("CITATION_CROSSCHECKER_MAILTO") or "").strip()
    headers = {
        "User-Agent": "CitationCrosschecker/1.2 (+https://citationcrosschecker.onrender.com)"
    }
    if mailto:
        headers["From"] = mailto

    try:
        r = requests.get(url, params=params, timeout=timeout, headers=headers)
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
    return m.group(1).strip().rstrip(").,;")


def _extract_title_guess(ref: str) -> str:
    t = _strip_leading_numbering(ref)
    t = re.sub(r"\s+", " ", t).strip()

    t = re.sub(r"(doi\s*:\s*)?10\.\d{4,9}/\S+", "", t, flags=re.I)
    t = re.sub(r"https?://doi\.org/10\.\d{4,9}/\S+", "", t, flags=re.I)

    t2 = re.split(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?", t, maxsplit=1, flags=re.I)
    if len(t2) >= 3:
        after = (t2[2] or "").strip()
    else:
        m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", t, flags=re.I)
        after = (t[m.end():].strip() if m else t)

    after = after.lstrip(". ").strip()
    title = after.split(".", 1)[0].strip() if "." in after else after.strip()
    if len(title) < 12:
        title = t.strip()
    return title


def _extract_container_guess(ref: str) -> str:
    """
    Light heuristic: look for patterns like:
      ". Journal Name," or ". Journal Name (" or ". Journal Name."
    It's OK if empty, it only helps when present.
    """
    t = re.sub(r"\s+", " ", (ref or "")).strip()
    # remove DOI links
    t = re.sub(r"(doi\s*:\s*)?10\.\d{4,9}/\S+", "", t, flags=re.I)
    t = re.sub(r"https?://doi\.org/10\.\d{4,9}/\S+", "", t, flags=re.I)

    # try "Title. Container, ..."
    parts = t.split(". ")
    if len(parts) >= 3:
        cand = parts[2].strip()
        cand = re.split(r",|\(|\d{1,4}\(", cand, maxsplit=1)[0].strip()
        if 5 <= len(cand) <= 120:
            return cand
    return ""


def _crossref_items(data: Optional[dict]) -> List[dict]:
    return (((data or {}).get("message") or {}).get("items") or []) if isinstance(data, dict) else []


def _crossref_query_gold(ref_raw: str, title: str, author: str, year: str) -> Tuple[List[Dict[str, Any]], str]:
    """
    Golden structure:
      query.bibliographic = citation string
      query.title, query.author, query.container-title
      filter = from/until pub date + type when possible
      rows > 1 so we can do score-gap check
    """
    url = "https://api.crossref.org/works"
    mailto = (os.getenv("CITATION_CROSSCHECKER_MAILTO") or "").strip()

    container = _extract_container_guess(ref_raw)

    # A) strict pass with year + type
    params1: Dict[str, Any] = {
        "query.bibliographic": ref_raw if ref_raw else title,
        "rows": 5,
    }
    if title:
        params1["query.title"] = title
    if author:
        params1["query.author"] = author
    if container:
        params1["query.container-title"] = container
    if mailto:
        params1["mailto"] = mailto

    if year[:4].isdigit():
        params1["filter"] = f"from-pub-date:{year[:4]}-01-01,until-pub-date:{year[:4]}-12-31,type:journal-article"
    else:
        params1["filter"] = "type:journal-article"

    data1 = _safe_get_json(url, params=params1, timeout=18)
    items1 = _crossref_items(data1)
    if items1:
        return ([{"source": "crossref", "item": it, "query_used": params1.get("query.bibliographic", "")} for it in items1], "gold_year_type")

    # B) relaxed pass without year filter (higher recall)
    params2 = dict(params1)
    params2.pop("filter", None)
    params2["rows"] = 8

    data2 = _safe_get_json(url, params=params2, timeout=18)
    items2 = _crossref_items(data2)
    return ([{"source": "crossref", "item": it, "query_used": params2.get("query.bibliographic", "")} for it in items2], "gold_relaxed")


def _query_openalex(title: str, raw_ref: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"
    q = title if len(title) >= 12 else raw_ref
    params: Dict[str, Any] = {"search": q, "per-page": 12}

    data = _safe_get_json(url, params=params, timeout=18)
    results = (data or {}).get("results") or []
    return [{"source": "openalex", "item": it, "query_used": q} for it in results]


def _candidate_fields(cand: Dict[str, Any]) -> Tuple[str, str, str, str, float]:
    """
    Returns (doi, title, year, first_author_surname, api_score)
    api_score is Crossref's item.score when available.
    """
    src = cand.get("source")
    item = cand.get("item", {}) or {}

    doi = ""
    title = ""
    year = ""
    first_author = ""
    api_score = 0.0

    if src == "crossref":
        doi = _s(item.get("DOI")).strip()
        titles = item.get("title") or []
        title = _s(titles[0] if titles else "").strip()
        y = (
            (item.get("published-print", {}).get("date-parts") or [[None]])[0][0]
            or (item.get("published-online", {}).get("date-parts") or [[None]])[0][0]
            or ""
        )
        year = _s(y).strip()
        authors = item.get("author") or []
        if authors and isinstance(authors, list):
            first_author = _s((authors[0] or {}).get("family")).lower().strip()
        try:
            api_score = float(item.get("score") or 0.0)
        except Exception:
            api_score = 0.0

    elif src == "openalex":
        doi = _s(item.get("doi")).replace("https://doi.org/", "").strip()
        title = _s(item.get("title")).strip()
        year = _s(item.get("publication_year")).strip()
        auths = item.get("authorships") or []
        if auths and isinstance(auths, list):
            a0 = auths[0] or {}
            au = a0.get("author") or {}
            dn = _s(au.get("display_name")).strip()
            if dn:
                parts = dn.split()
                first_author = (parts[-1].lower().strip() if parts else "")
        api_score = 0.0

    return doi, title, year, first_author, api_score


def _score(ref_title: str, ref_author: str, ref_year: str, cand_title: str, cand_author: str, cand_year: str) -> Dict[str, Any]:
    title_score = fuzz.token_set_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0
    author_match = 1 if (ref_author and cand_author and ref_author == cand_author) else 0
    year_match = 1 if (ref_year and cand_year and ref_year[:4] == str(cand_year)[:4]) else 0
    score = (title_score * 1.25) + (28 * author_match) + (10 * year_match)
    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_match": int(author_match),
        "year_match": int(year_match),
    }


def _score_gap_ok(crossref_candidates: List[Dict[str, Any]], min_gap: float = 15.0) -> bool:
    """
    Uses Crossref item.score if present.
    If score0 is much higher than score1, it's likely a match.
    """
    scores = []
    for c in crossref_candidates[:3]:
        _, _, _, _, api_score = _candidate_fields(c)
        if api_score > 0:
            scores.append(api_score)

    if len(scores) >= 2:
        return (scores[0] - scores[1]) >= float(min_gap)
    return False


def _classify(score: int, author_match: int, year_match: int, title_score: int, gap_ok: bool) -> str:
    if title_score >= 92 and (author_match or year_match) and score >= 132:
        return "verified"
    if gap_ok and title_score >= 88 and score >= 120:
        return "verified"
    if title_score >= 85 and score >= 118:
        return "likely"
    if title_score >= 74 and score >= 95:
        return "needs_review"
    return "not_found"


def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> List[Dict[str, Any]]:
    refs = [r for r in (references or []) if (_s(r).strip())]
    if not refs:
        return []

    if max_to_check and max_to_check > 0:
        refs = refs[: max_to_check]

    rows: List[Dict[str, Any]] = []

    for ref in refs:
        ref_raw = _s(ref).strip()

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
        }

        try:
            candidates: List[Dict[str, Any]] = []
            crossref_candidates: List[Dict[str, Any]] = []
            crossref_mode = ""

            if use_crossref:
                crossref_candidates, crossref_mode = _crossref_query_gold(
                    ref_raw=ref_raw,
                    title=ref_title,
                    author=ref_author,
                    year=ref_year,
                )
                candidates.extend(crossref_candidates)
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            if use_openalex:
                candidates.extend(_query_openalex(ref_title, ref_raw))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            if not candidates:
                row["status"] = "not_found"
                rows.append(row)
                continue

            best = None
            best_meta = None
            best_score = -1

            for cand in candidates:
                cand_doi, cand_title, cand_year, cand_author, _ = _candidate_fields(cand)

                doi_bonus = 0
                if ref_doi and cand_doi and ref_doi.lower() == cand_doi.lower():
                    doi_bonus = 50

                meta = _score(ref_title, ref_author, ref_year, cand_title, cand_author, cand_year)
                meta["score"] = int(meta["score"] + doi_bonus)

                if meta["score"] > best_score:
                    best_score = meta["score"]
                    best = cand
                    best_meta = meta

            gap_ok = _score_gap_ok(crossref_candidates) if crossref_candidates else False

            src = _s(best.get("source") if best else "").strip()
            cand_doi, cand_title, cand_year, cand_author, _ = _candidate_fields(best) if best else ("", "", "", "", 0.0)

            row["source"] = src
            row["score"] = int(best_meta["score"] if best_meta else 0)
            row["doi"] = cand_doi or ""
            row["matched_year"] = _s(cand_year or "")
            row["matched_authors"] = cand_author or ""
            row["matched_title"] = cand_title or ""
            row["query_used"] = _s(best.get("query_used") if best else "") or ref_raw
            if src == "crossref" and crossref_mode:
                row["query_used"] = f"{row['query_used']} [{crossref_mode}]"

            status = _classify(
                score=int(row["score"]),
                author_match=int(best_meta.get("author_match", 0) if best_meta else 0),
                year_match=int(best_meta.get("year_match", 0) if best_meta else 0),
                title_score=int(best_meta.get("title_score", 0) if best_meta else 0),
                gap_ok=gap_ok,
            )
            row["status"] = _normalize_verify_status(status)
            rows.append(row)

        except Exception:
            row["status"] = "offline"
            rows.append(row)

    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
