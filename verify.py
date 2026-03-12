# verify.py (UPDATED HIGH-VERIFICATION VERSION)

import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple

import requests
from rapidfuzz import fuzz


_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}

MAILTO = (
    os.getenv("CITATION_CROSSCHECKER_MAILTO")
    or os.getenv("CROSSREF_MAILTO")
    or os.getenv("OPENALEX_MAILTO")
    or ""
).strip()


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


def _safe_get_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    try:
        headers = {
            "User-Agent": f"CitationCrosschecker (mailto:{MAILTO})",
            "Accept": "application/json",
        }
        r = requests.get(url, params=params, timeout=20, headers=headers)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _extract_year(text: str) -> str:
    m = re.search(r"(19|20)\d{2}", text)
    return m.group(0) if m else ""


def _extract_doi(text: str) -> str:
    m = re.search(r"(10\.\d{4,9}/[^\s]+)", text)
    return m.group(1).rstrip(").,;") if m else ""


# ---------------------------------------------------------
# Query Builder
# ---------------------------------------------------------

def _build_query(ref: str) -> Tuple[str, List[str], str]:

    ref = _safe_strip(ref)

    year = _extract_year(ref)
    doi = _extract_doi(ref)

    words = re.findall(r"[A-Za-z]{4,}", ref)

    title_words = words[3:8]

    authors = []
    if "," in ref:
        authors.append(ref.split(",")[0].lower())

    query = " ".join(authors + title_words + [year])

    return query, authors, year, doi


# ---------------------------------------------------------
# Candidate Extraction
# ---------------------------------------------------------

def _candidate_fields(cand: Dict[str, Any]) -> Tuple[str, str, str, List[str]]:

    src = cand.get("source")
    item = cand.get("item") or {}

    doi = ""
    title = ""
    year = ""
    authors = []

    if src == "crossref":

        doi = _safe_strip(item.get("DOI"))

        titles = item.get("title") or []
        title = _norm_text(titles[0]) if titles else ""

        year = str((item.get("issued", {}).get("date-parts", [[None]])[0][0]))

        for au in (item.get("author") or [])[:5]:
            fam = _safe_strip(au.get("family")).lower()
            authors.append(fam)

    elif src == "openalex":

        doi = _safe_strip(item.get("doi")).replace("https://doi.org/", "")
        title = _norm_text(item.get("title"))
        year = str(item.get("publication_year"))

        for a in (item.get("authorships") or [])[:5]:
            name = a.get("author", {}).get("display_name")
            if name:
                authors.append(name.split()[-1].lower())

    return doi, title, year, authors


# ---------------------------------------------------------
# Scoring
# ---------------------------------------------------------

def _score(ref_title, ref_authors, ref_year, cand_title, cand_authors, cand_year):

    title_score = fuzz.token_set_ratio(ref_title, cand_title)

    author_overlap = len(set(ref_authors).intersection(set(cand_authors)))

    year_match = 1 if ref_year and cand_year and ref_year == cand_year else 0

    score = (title_score * 1.6) + (author_overlap * 20) + (year_match * 10)

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_overlap": author_overlap,
        "year_match": year_match,
    }


# ---------------------------------------------------------
# Classification (Lowered Thresholds)
# ---------------------------------------------------------

def _classify(doi_match, title_score, score):

    if doi_match:
        return "verified"

    if title_score >= 72:
        return "verified"

    if title_score >= 60:
        return "likely"

    if title_score >= 45:
        return "needs_review"

    return "not_found"


# ---------------------------------------------------------
# Main Verification
# ---------------------------------------------------------

def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
):

    rows = []

    for ref in references:

        query, ref_authors, ref_year, ref_doi = _build_query(ref)

        ref_title = _norm_text(ref)

        row = {
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

        candidates = []

        try:

            if use_crossref:

                url = "https://api.crossref.org/works"

                params = {
                    "query.bibliographic": query,
                    "rows": 5,
                    "sort": "score",
                }

                data = _safe_get_json(url, params)

                for it in (data or {}).get("message", {}).get("items", []):
                    candidates.append({"source": "crossref", "item": it})

            if use_openalex:

                url = "https://api.openalex.org/works"

                params = {"search": query, "per-page": 5}

                data = _safe_get_json(url, params)

                for it in (data or {}).get("results", []):
                    candidates.append({"source": "openalex", "item": it})

            best_score = -1
            best = None
            best_meta = {}

            for cand in candidates:

                doi, title, year, authors = _candidate_fields(cand)

                meta = _score(ref_title, ref_authors, ref_year, title, authors, year)

                doi_match = ref_doi and doi == ref_doi

                meta_score = meta["score"] + (40 if doi_match else 0)

                if meta_score > best_score:
                    best_score = meta_score
                    best = cand
                    best_meta = meta
                    best_meta["doi_match"] = doi_match
                    best_meta["doi"] = doi
                    best_meta["title"] = title
                    best_meta["year"] = year
                    best_meta["authors"] = authors

            if best:

                status = _classify(
                    best_meta["doi_match"],
                    best_meta["title_score"],
                    best_meta["score"],
                )

                row.update({
                    "status": status,
                    "source": best["source"],
                    "score": best_meta["score"],
                    "doi": best_meta["doi"],
                    "matched_title": best_meta["title"],
                    "matched_year": best_meta["year"],
                    "matched_authors": ", ".join(best_meta["authors"]),
                    "title_score": best_meta["title_score"],
                    "author_overlap": best_meta["author_overlap"],
                    "year_match": best_meta["year_match"],
                })

        except Exception as e:
            row["status"] = "offline"
            row["error"] = str(e)

        rows.append(row)

        time.sleep(throttle_s)

    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
