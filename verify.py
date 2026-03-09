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

UNPAYWALL_EMAIL = (os.getenv("UNPAYWALL_EMAIL") or MAILTO or "").strip()


# -------------------------------------------------------
# utilities
# -------------------------------------------------------

def _normalize_verify_status(s: str) -> str:
    s = (s or "").strip().lower().replace(" ", "_")
    if s not in _ALLOWED_VERIFY_STATUSES:
        s = "needs_review"
    return s


def _safe(s: Any) -> str:
    if s is None:
        return ""
    return str(s).strip()


def _norm(s: str) -> str:
    s = _safe(s).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _safe_json(url: str, params=None) -> Optional[dict]:
    try:
        headers = {"User-Agent": "CitationCrosschecker/1.0"}
        r = requests.get(url, params=params, timeout=20, headers=headers)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _extract_doi(text: str) -> str:
    m = re.search(r"(10\.\d{4,9}/[^\s]+)", text or "", re.I)
    if not m:
        return ""
    return m.group(1).rstrip(".,)")


def _extract_year(text: str) -> str:
    m = re.search(r"\b(19|20)\d{2}\b", text)
    return m.group(0) if m else ""


# -------------------------------------------------------
# field extraction
# -------------------------------------------------------

def _extract_fields(ref: str):

    doi = _extract_doi(ref)
    year = _extract_year(ref)

    parts = ref.split(".")
    title = parts[1] if len(parts) > 1 else ""

    authors = []
    author_block = parts[0]

    for a in re.split(r",|and|&", author_block):
        a = a.strip()
        if not a:
            continue
        surname = a.split()[-1].lower()
        surname = re.sub(r"[^a-z\-]", "", surname)
        if surname:
            authors.append(surname)

    return {
        "title": title.strip(),
        "authors": authors[:3],
        "year": year,
        "doi": doi
    }


# -------------------------------------------------------
# crossref
# -------------------------------------------------------

def _crossref_query(q: str, authors: str):

    url = "https://api.crossref.org/works"

    params = {
        "query.bibliographic": q,
        "rows": 8,
        "sort": "score",
        "order": "desc"
    }

    if authors:
        params["query.author"] = authors

    if MAILTO:
        params["mailto"] = MAILTO

    data = _safe_json(url, params)

    if not data:
        return []

    return data.get("message", {}).get("items", [])


# -------------------------------------------------------
# openalex
# -------------------------------------------------------

def _openalex_query(q: str):

    url = "https://api.openalex.org/works"

    params = {
        "search": q,
        "per-page": 8
    }

    if MAILTO:
        params["mailto"] = MAILTO

    data = _safe_json(url, params)

    if not data:
        return []

    return data.get("results", [])


# -------------------------------------------------------
# candidate extraction
# -------------------------------------------------------

def _candidate_fields_crossref(item):

    doi = _safe(item.get("DOI"))

    title = ""
    if item.get("title"):
        title = item["title"][0]

    year = ""
    dp = item.get("published-print", {}).get("date-parts")
    if dp:
        year = str(dp[0][0])

    authors = []
    for a in item.get("author", [])[:5]:
        fam = _safe(a.get("family")).lower()
        fam = re.sub(r"[^a-z\-]", "", fam)
        if fam:
            authors.append(fam)

    return doi, title, year, authors, int(item.get("score") or 0)


def _candidate_fields_openalex(item):

    doi = _safe(item.get("doi")).replace("https://doi.org/", "")

    title = _safe(item.get("title"))
    year = _safe(item.get("publication_year"))

    authors = []
    for a in item.get("authorships", [])[:5]:
        nm = _safe(a.get("author", {}).get("display_name"))
        if nm:
            surname = nm.split()[-1].lower()
            surname = re.sub(r"[^a-z\-]", "", surname)
            authors.append(surname)

    return doi, title, year, authors, 0


# -------------------------------------------------------
# scoring
# -------------------------------------------------------

def _score(rt, ra, ry, ct, ca, cy, api_score):

    rt = _norm(rt)
    ct = _norm(ct)

    title_score = fuzz.token_set_ratio(rt, ct) if rt and ct else 0

    author_overlap = len(set(ra).intersection(set(ca)))

    year_match = 1 if ry and cy and ry[:4] == cy[:4] else 0

    score = (
        title_score * 1.35
        + author_overlap * 26
        + year_match * 8
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
    ym = meta["year_match"]
    sc = meta["score"]

    if doi_match and ts >= 55:
        return "verified"

    if ts >= 88 and (ao >= 1 or not has_authors) and (cand_has_doi or sc >= 120):
        return "verified"

    if ts >= 80 and (ao >= 1 or not has_authors):
        return "likely"

    if ts >= 70:
        return "needs_review"

    return "not_found"


# -------------------------------------------------------
# main function
# -------------------------------------------------------

def verify_references_batch(
    references: List[str],
    style: str = "apa",
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
    use_unpaywall: bool = True,
    **kwargs
) -> List[Dict[str, Any]]:

    rows = []

    for ref in references:

        ref = _safe(ref)
        fields = _extract_fields(ref)

        title = fields["title"]
        authors = fields["authors"]
        year = fields["year"]
        doi = fields["doi"]

        query = " ".join(authors + title.split()[:5] + [year])

        best = None
        best_meta = {"score": -1}

        # ---------------- Crossref ----------------

        if use_crossref:

            items = _crossref_query(query, " ".join(authors))

            for it in items:

                cdoi, ctitle, cyear, cauth, api_score = _candidate_fields_crossref(it)

                meta = _score(title, authors, year, ctitle, cauth, cyear, api_score)

                doi_match = doi and cdoi and doi.lower() == cdoi.lower()

                meta["score"] += 35 if doi_match else 0

                if meta["score"] > best_meta["score"]:
                    best_meta = meta
                    best = (ctitle, cdoi, cyear, cauth, doi_match)

            time.sleep(throttle_s)

        # ---------------- OpenAlex ----------------

        if use_openalex and best_meta["score"] < 110:

            items = _openalex_query(query)

            for it in items:

                cdoi, ctitle, cyear, cauth, api_score = _candidate_fields_openalex(it)

                meta = _score(title, authors, year, ctitle, cauth, cyear, api_score)

                doi_match = doi and cdoi and doi.lower() == cdoi.lower()

                meta["score"] += 35 if doi_match else 0

                if meta["score"] > best_meta["score"]:
                    best_meta = meta
                    best = (ctitle, cdoi, cyear, cauth, doi_match)

            time.sleep(throttle_s)

        # ---------------- classification ----------------

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

            status = _classify(
                best_meta,
                doi_match,
                bool(authors),
                bool(cdoi)
            )

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
