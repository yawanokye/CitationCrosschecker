# verify.py
import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple

import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}

MAILTO = os.getenv("CITATION_CROSSCHECKER_MAILTO") or os.getenv("CROSSREF_MAILTO") or ""
MAILTO = str(MAILTO).strip() if MAILTO is not None else ""


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 22) -> Optional[dict]:
    try:
        headers = {
            "User-Agent": "CitationCrosschecker/1.0",
            "Accept": "application/json",
        }
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

    # remove DOI forms
    t = re.sub(r"(doi\s*:\s*)?10\.\d{4,9}/\S+", "", t, flags=re.I)
    t = re.sub(r"https?://doi\.org/10\.\d{4,9}/\S+", "", t, flags=re.I)

    # take first sentence after (YEAR)
    parts = re.split(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?", t, maxsplit=1, flags=re.I)
    if len(parts) >= 3:
        after = parts[2].strip()
    else:
        m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", t, flags=re.I)
        after = t[m.end():].strip() if m else t

    after = after.lstrip(". ").strip()
    title = after.split(".", 1)[0].strip() if "." in after else after.strip()
    if len(title) < 12:
        title = t.strip()
    return title


def _clean_query_string(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) > 280:
        s = s[:280].rstrip()
    return s


def _query_crossref_gold(title: str, author: str, year: str, raw_ref: str, rows: int = 5) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"

    q = title if len(title or "") >= 12 else (raw_ref or "")
    q = _clean_query_string(q)

    a = (author or "").strip()

    params: Dict[str, Any] = {
        "query.bibliographic": q,
        "rows": int(rows),
        "select": "DOI,title,author,issued,published-print,published-online,container-title,score,type",
    }

    if a:
        params["query.author"] = a

    if MAILTO:
        params["mailto"] = MAILTO

    y4 = (year or "")[:4]
    if y4.isdigit():
        # year filter is the single biggest precision boost
        params["filter"] = f"from-pub-date:{y4}-01-01,until-pub-date:{y4}-12-31"

    data = _safe_get_json(url, params=params, timeout=22)
    if not data:
        return []

    items = (data.get("message") or {}).get("items") or []
    out = []
    for it in items:
        out.append({"source": "crossref", "item": it, "query_used": q})
    return out


def _query_openalex(title: str, year: str, raw_ref: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"

    q = title if len(title or "") >= 12 else (raw_ref or "")
    q = _clean_query_string(q)

    params: Dict[str, Any] = {
        "search": q,
        "per-page": 7,
    }

    if MAILTO:
        params["mailto"] = MAILTO

    y4 = (year or "")[:4]
    if y4.isdigit():
        params["filter"] = f"publication_year:{y4}"

    data = _safe_get_json(url, params=params, timeout=22)
    if not data:
        return []
    results = data.get("results") or []
    out = []
    for it in results:
        out.append({"source": "openalex", "item": it, "query_used": q})
    return out


def _candidate_fields(cand: Dict[str, Any]) -> Tuple[str, str, str, str]:
    # defend against None everywhere
    src = (cand.get("source") or "")
    src = str(src).strip()

    item = cand.get("item") or {}
    if not isinstance(item, dict):
        item = {}

    doi = ""
    title = ""
    year = ""
    first_author = ""

    if src == "crossref":
        doi = (item.get("DOI") or "")
        doi = str(doi).strip()

        titles = item.get("title") or []
        title = titles[0] if (isinstance(titles, list) and titles) else ""
        title = str(title or "")

        year_val = (
            (item.get("published-print", {}).get("date-parts") or [[None]])[0][0]
            or (item.get("published-online", {}).get("date-parts") or [[None]])[0][0]
            or (item.get("issued", {}).get("date-parts") or [[None]])[0][0]
            or ""
        )
        year = str(year_val or "")

        authors = item.get("author") or []
        if isinstance(authors, list) and authors:
            fam = (authors[0].get("family") or "")
            first_author = str(fam).lower()

    elif src == "openalex":
        doi_val = item.get("doi") or ""
        doi = str(doi_val).replace("https://doi.org/", "").strip()

        title = str(item.get("title") or "")

        year = str(item.get("publication_year") or "")

        auths = item.get("authorships") or []
        if isinstance(auths, list) and auths and isinstance(auths[0], dict):
            aobj = auths[0].get("author") or {}
            nm = ""
            if isinstance(aobj, dict):
                nm = str(aobj.get("display_name") or "").strip()
            first_author = nm.split()[-1].lower() if nm else ""

    return doi, title, year, first_author


def _score(ref_title: str, ref_author: str, ref_year: str, cand_title: str, cand_author: str, cand_year: str) -> Dict[str, Any]:
    title_score = fuzz.token_set_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0
    author_match = 1 if (ref_author and cand_author and ref_author == cand_author) else 0
    year_match = 1 if (ref_year and cand_year and ref_year[:4] == str(cand_year)[:4]) else 0

    score = (title_score * 1.25) + (25 * author_match) + (14 * year_match)

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_match": int(author_match),
        "year_match": int(year_match),
    }


def _classify(score: int, author_match: int, year_match: int, title_score: int) -> str:
    if title_score >= 88 and (author_match or year_match) and score >= 110:
        return "verified"
    if title_score >= 80 and score >= 100:
        return "likely"
    if title_score >= 72 and score >= 90:
        return "needs_review"
    return "not_found"


def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
    use_semantic_scholar: bool = False,  # ignored, kept for compatibility
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
            candidates: List[Dict[str, Any]] = []

            # Stage A: title-based
            if use_crossref:
                candidates.extend(_query_crossref_gold(ref_title, ref_author, ref_year, ref_raw, rows=5))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            if use_openalex:
                candidates.extend(_query_openalex(ref_title, ref_year, ref_raw))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            # Stage B fallback: if nothing came back, try using full cleaned ref as bibliographic query
            if not candidates and use_crossref:
                full_q = _clean_query_string(re.sub(r"[^\w\s\-]", " ", ref_raw))
                candidates.extend(_query_crossref_gold(full_q, ref_author, ref_year, ref_raw, rows=10))
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

                doi_bonus = 0
                if ref_doi and cand_doi and ref_doi.lower() == cand_doi.lower():
                    doi_bonus = 45

                meta = _score(ref_title, ref_author, ref_year, cand_title, cand_author, cand_year)
                meta["score"] = int(meta["score"] + doi_bonus)

                if meta["score"] > best_score:
                    best_score = meta["score"]
                    best = cand
                    best_meta = meta

            src = (best.get("source") if best else "") or ""
            cand_doi, cand_title, cand_year, cand_author = _candidate_fields(best) if best else ("", "", "", "")

            row["source"] = str(src or "")
            row["score"] = int(best_meta["score"] if best_meta else 0)
            row["doi"] = str(cand_doi or "")
            row["matched_year"] = str(cand_year or "")
            row["matched_authors"] = str(cand_author or "")
            row["matched_title"] = str(cand_title or "")
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
