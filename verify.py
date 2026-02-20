# verify.py
import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple

import requests
from rapidfuzz import fuzz


_ALLOWED_VERIFY_STATUSES = {
    "verified",
    "likely",
    "needs_review",
    "not_found",
    "offline",
}

# polite pool email
MAILTO = (
    os.getenv("CITATION_CROSSCHECKER_MAILTO")
    or os.getenv("CROSSREF_MAILTO")
    or ""
)
MAILTO = str(MAILTO or "").strip()


# -------------------------
# SAFE STRING HELPERS
# -------------------------
def _safe_str(x) -> str:
    if x is None:
        return ""
    return str(x)


def _safe_strip(x) -> str:
    return _safe_str(x).strip()


def _normalize_verify_status(s: str) -> str:
    st = _safe_strip(s).lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


# -------------------------
# SAFE HTTP
# -------------------------
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


# -------------------------
# EXTRACTORS
# -------------------------
def _extract_year(text: str) -> str:
    text = _safe_str(text)
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", text, flags=re.I)
    if not m:
        return ""
    return (m.group(1) + (m.group(2) or "")).lower()


def _strip_leading_numbering(text: str) -> str:
    t = _safe_strip(text)
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)
    return t.strip()


def _extract_first_author_surname(text: str) -> str:

    t = _strip_leading_numbering(text)

    if not t:
        return ""

    if "," in t:
        first = t.split(",", 1)[0]
    else:
        first = re.split(r"\s+", t)[0]

    return re.sub(r"[^A-Za-z\-']", "", _safe_str(first)).lower()


def _extract_doi(text: str) -> str:

    text = _safe_str(text)

    m = re.search(r"(10\.\d{4,9}/[^\s]+)", text, flags=re.I)

    if not m:
        return ""

    return m.group(1).rstrip(").,;")


def _extract_title_guess(ref: str) -> str:

    ref = _safe_str(ref)

    t = _strip_leading_numbering(ref)

    t = re.sub(r"\s+", " ", t)

    t = re.sub(r"(doi\s*:\s*)?10\.\d{4,9}/\S+", "", t, flags=re.I)

    parts = re.split(
        r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?",
        t,
        maxsplit=1,
        flags=re.I,
    )

    if len(parts) >= 3:
        after = parts[2]
    else:
        m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", t, flags=re.I)
        after = t[m.end():] if m else t

    after = _safe_strip(after)

    title = after.split(".", 1)[0] if "." in after else after

    title = _safe_strip(title)

    if len(title) < 12:
        title = _safe_strip(t)

    return title


# -------------------------
# QUERY CLEANER
# -------------------------
def _clean_query_string(s: str) -> str:

    s = _safe_strip(s)

    s = re.sub(r"\s+", " ", s)

    if len(s) > 280:
        s = s[:280]

    return s


# -------------------------
# CROSSREF GOLD QUERY
# -------------------------
def _query_crossref_gold(title, author, year, raw_ref):

    url = "https://api.crossref.org/works"

    q = title if len(title) >= 12 else raw_ref

    q = _clean_query_string(q)

    params = {
        "query.bibliographic": q,
        "rows": 5,
    }

    author = _safe_strip(author)

    if author:
        params["query.author"] = author

    if MAILTO:
        params["mailto"] = MAILTO

    y4 = _safe_str(year)[:4]

    if y4.isdigit():
        params["filter"] = f"from-pub-date:{y4}-01-01,until-pub-date:{y4}-12-31"

    data = _safe_get_json(url, params)

    if not data:
        return []

    items = data.get("message", {}).get("items", [])

    return [
        {
            "source": "crossref",
            "item": item,
            "query_used": q,
        }
        for item in items
    ]


# -------------------------
# OPENALEX QUERY
# -------------------------
def _query_openalex(title, author, year, raw_ref):

    url = "https://api.openalex.org/works"

    q = title if len(title) >= 12 else raw_ref

    q = _clean_query_string(q)

    params = {
        "search": q,
        "per-page": 7,
    }

    if MAILTO:
        params["mailto"] = MAILTO

    y4 = _safe_str(year)[:4]

    if y4.isdigit():
        params["filter"] = f"publication_year:{y4}"

    data = _safe_get_json(url, params)

    if not data:
        return []

    results = data.get("results", [])

    return [
        {
            "source": "openalex",
            "item": item,
            "query_used": q,
        }
        for item in results
    ]


# -------------------------
# EXTRACT FIELDS SAFELY
# -------------------------
def _candidate_fields(cand) -> Tuple[str, str, str, str]:

    src = _safe_strip(cand.get("source"))

    item = cand.get("item") or {}

    doi = ""
    title = ""
    year = ""
    first_author = ""

    if src == "crossref":

        doi = _safe_strip(item.get("DOI"))

        titles = item.get("title") or []

        title = _safe_strip(titles[0] if titles else "")

        date = (
            item.get("published-print", {})
            .get("date-parts", [[None]])[0][0]
            or item.get("published-online", {})
            .get("date-parts", [[None]])[0][0]
        )

        year = _safe_str(date)

        authors = item.get("author") or []

        if authors:
            first_author = _safe_strip(authors[0].get("family")).lower()

    elif src == "openalex":

        doi = _safe_strip(item.get("doi")).replace("https://doi.org/", "")

        title = _safe_strip(item.get("title"))

        year = _safe_str(item.get("publication_year"))

        auths = item.get("authorships") or []

        if auths and auths[0].get("author"):

            nm = _safe_strip(auths[0]["author"].get("display_name"))

            if nm:
                first_author = nm.split()[-1].lower()

    return doi, title, year, first_author


# -------------------------
# SCORE
# -------------------------
def _score(rt, ra, ry, ct, ca, cy):

    title_score = fuzz.token_set_ratio(rt, ct) if rt and ct else 0

    author_match = 1 if ra and ca and ra == ca else 0

    year_match = 1 if ry and cy and ry[:4] == cy[:4] else 0

    score = (title_score * 1.25) + (25 * author_match) + (14 * year_match)

    return {
        "score": int(score),
        "title_score": title_score,
        "author_match": author_match,
        "year_match": year_match,
    }


# -------------------------
# CLASSIFY
# -------------------------
def _classify(score, am, ym, ts):

    if ts >= 92 and (am or ym) and score >= 132:
        return "verified"

    if ts >= 86 and score >= 120:
        return "likely"

    if ts >= 75 and score >= 96:
        return "needs_review"

    return "not_found"


# -------------------------
# MAIN VERIFY
# -------------------------
def verify_references_batch(
    references,
    max_to_check=0,
    throttle_s=0.12,
    use_crossref=True,
    use_openalex=True,
    use_semantic_scholar=False,
):

    refs = [_safe_strip(r) for r in references if _safe_strip(r)]

    if max_to_check:
        refs = refs[:max_to_check]

    rows = []

    for ref in refs:

        ref_year = _extract_year(ref)

        ref_author = _extract_first_author_surname(ref)

        ref_doi = _extract_doi(ref)

        ref_title = _extract_title_guess(ref)

        row = {
            "reference": ref,
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

            candidates = []

            if use_crossref:
                candidates += _query_crossref_gold(
                    ref_title,
                    ref_author,
                    ref_year,
                    ref,
                )
                time.sleep(throttle_s)

            if use_openalex:
                candidates += _query_openalex(
                    ref_title,
                    ref_author,
                    ref_year,
                    ref,
                )
                time.sleep(throttle_s)

            if not candidates:
                row["status"] = "not_found"
                rows.append(row)
                continue

            best = None
            best_meta = None
            best_score = -1

            for cand in candidates:

                doi, title, year, author = _candidate_fields(cand)

                bonus = 45 if ref_doi and doi and ref_doi.lower() == doi.lower() else 0

                meta = _score(
                    ref_title,
                    ref_author,
                    ref_year,
                    title,
                    author,
                    year,
                )

                meta["score"] += bonus

                if meta["score"] > best_score:
                    best_score = meta["score"]
                    best = cand
                    best_meta = meta

            doi, title, year, author = _candidate_fields(best)

            row["source"] = _safe_strip(best.get("source"))

            row["score"] = best_meta["score"]

            row["doi"] = doi

            row["matched_year"] = year

            row["matched_authors"] = author

            row["matched_title"] = title

            row["query_used"] = _safe_strip(best.get("query_used"))

            row["status"] = _normalize_verify_status(
                _classify(
                    row["score"],
                    best_meta["author_match"],
                    best_meta["year_match"],
                    best_meta["title_score"],
                )
            )

        except Exception as e:

            row["status"] = "offline"

            row["error"] = _safe_str(e)

        rows.append(row)

    return rows
