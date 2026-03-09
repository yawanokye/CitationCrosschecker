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


# ------------------------------------------------
# utilities
# ------------------------------------------------

def _normalize_verify_status(s: str) -> str:
    s = (s or "").strip().lower().replace(" ", "_")
    if s not in _ALLOWED_VERIFY_STATUSES:
        s = "needs_review"
    return s


def _safe(x):
    return "" if x is None else str(x)


def _norm(s: str) -> str:
    s = _safe(s).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _safe_json(url, params=None):
    try:
        headers = {"User-Agent": "CitationCrosschecker/1.0"}
        r = requests.get(url, params=params, timeout=20, headers=headers)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


# ------------------------------------------------
# extraction
# ------------------------------------------------

def _extract_doi(ref):
    m = re.search(r"(10\.\d{4,9}/[^\s]+)", ref, re.I)
    return m.group(1).rstrip(".,)") if m else ""


def _extract_year(ref):
    m = re.search(r"\b(19|20)\d{2}\b", ref)
    return m.group(0) if m else ""


def _extract_authors(ref):

    block = ref.split("(")[0]
    authors = []

    for a in re.split(r",|and|&", block):

        a = a.strip()
        if not a:
            continue

        surname = a.split()[-1].lower()
        surname = re.sub(r"[^a-z\-]", "", surname)

        if surname:
            authors.append(surname)

    return authors[:3]


def _extract_title(ref):

    parts = ref.split(".")

    if len(parts) >= 2:
        return parts[1].strip()

    return ""


# ------------------------------------------------
# crossref
# ------------------------------------------------

def _crossref(query, authors):

    params = {
        "query.bibliographic": query,
        "rows": 10,
        "sort": "score",
        "order": "desc"
    }

    if authors:
        params["query.author"] = authors

    if MAILTO:
        params["mailto"] = MAILTO

    data = _safe_json("https://api.crossref.org/works", params)

    if not data:
        return []

    return data.get("message", {}).get("items", [])


# ------------------------------------------------
# openalex
# ------------------------------------------------

def _openalex(query):

    params = {
        "search": query,
        "per-page": 10
    }

    if MAILTO:
        params["mailto"] = MAILTO

    data = _safe_json("https://api.openalex.org/works", params)

    if not data:
        return []

    return data.get("results", [])


# ------------------------------------------------
# candidate fields
# ------------------------------------------------

def _cand_crossref(it):

    doi = _safe(it.get("DOI"))

    title = ""
    if it.get("title"):
        title = it["title"][0]

    year = ""

    dp = it.get("published-print", {}).get("date-parts")
    if dp:
        year = str(dp[0][0])

    authors = []

    for a in it.get("author", [])[:5]:
        fam = _safe(a.get("family")).lower()
        fam = re.sub(r"[^a-z\-]", "", fam)
        authors.append(fam)

    score = int(it.get("score") or 0)

    return doi, title, year, authors, score


def _cand_openalex(it):

    doi = _safe(it.get("doi")).replace("https://doi.org/", "")
    title = _safe(it.get("title"))
    year = _safe(it.get("publication_year"))

    authors = []

    for a in it.get("authorships", [])[:5]:

        nm = _safe(a.get("author", {}).get("display_name"))

        if nm:
            surname = nm.split()[-1].lower()
            surname = re.sub(r"[^a-z\-]", "", surname)
            authors.append(surname)

    return doi, title, year, authors, 0


# ------------------------------------------------
# scoring
# ------------------------------------------------

def _score(rt, ra, ry, ct, ca, cy, api_score):

    rt = _norm(rt)
    ct = _norm(ct)

    title_score = fuzz.token_set_ratio(rt, ct) if rt and ct else 0

    author_overlap = len(set(ra).intersection(set(ca)))

    year_match = 1 if ry and cy and ry[:4] == cy[:4] else 0

    score = (
        title_score * 1.35 +
        author_overlap * 26 +
        year_match * 8
    )

    score += min(20, int(api_score / 10))

    return {
        "score": int(score),
        "title_score": title_score,
        "author_overlap": author_overlap,
        "year_match": year_match
    }


def _classify(meta, doi_match, has_authors, cand_has_doi):

    ts = meta["title_score"]
    ao = meta["author_overlap"]
    sc = meta["score"]

    if doi_match and ts >= 55:
        return "verified"

    if ts >= 88 and (ao >= 1 or not has_authors):
        return "verified"

    if ts >= 80:
        return "likely"

    if ts >= 70:
        return "needs_review"

    return "not_found"


# ------------------------------------------------
# main
# ------------------------------------------------

def verify_references_batch(
    references: List[str],
    style="apa",
    throttle_s=0.12,
    use_crossref=True,
    use_openalex=True,
    **kwargs
):

    rows = []

    for ref in references:

        ref = ref.strip()

        doi = _extract_doi(ref)
        year = _extract_year(ref)
        authors = _extract_authors(ref)
        title = _extract_title(ref)

        query = " ".join(authors + title.split()[:6] + [year])

        best = None
        best_meta = {"score": -1}

        if use_crossref:

            for it in _crossref(query, " ".join(authors)):

                cdoi, ctitle, cyear, cauth, api_score = _cand_crossref(it)

                meta = _score(title, authors, year, ctitle, cauth, cyear, api_score)

                doi_match = doi and cdoi and doi.lower() == cdoi.lower()

                meta["score"] += 35 if doi_match else 0

                if meta["score"] > best_meta["score"]:
                    best_meta = meta
                    best = (ctitle, cdoi, cyear, cauth, doi_match)

            time.sleep(throttle_s)

        if use_openalex and best_meta["score"] < 120:

            for it in _openalex(query):

                cdoi, ctitle, cyear, cauth, api_score = _cand_openalex(it)

                meta = _score(title, authors, year, ctitle, cauth, cyear, api_score)

                doi_match = doi and cdoi and doi.lower() == cdoi.lower()

                meta["score"] += 35 if doi_match else 0

                if meta["score"] > best_meta["score"]:
                    best_meta = meta
                    best = (ctitle, cdoi, cyear, cauth, doi_match)

            time.sleep(throttle_s)

        row = {
            "reference": ref,
            "style": style,
            "status": "not_found",
            "source": "",
            "score": 0,
            "doi": "",
            "matched_title": "",
            "matched_year": "",
            "matched_authors": "",
            "query_used": query
        }

        if best:

            ctitle, cdoi, cyear, cauth, doi_match = best

            status = _classify(best_meta, doi_match, bool(authors), bool(cdoi))

            row.update({
                "status": _normalize_verify_status(status),
                "source": "crossref" if use_crossref else "openalex",
                "score": best_meta["score"],
                "doi": cdoi,
                "matched_title": ctitle,
                "matched_year": cyear,
                "matched_authors": ", ".join(cauth)
            })

        rows.append(row)

    return rows
