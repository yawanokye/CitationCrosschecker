# verify.py
import re
import time
from typing import List, Dict, Any, Optional

import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {
    "verified",
    "likely",
    "needs_review",
    "not_found",
    "offline",
}


# -----------------------------------------
# Utilities
# -----------------------------------------

def normalize_status(s: str) -> str:
    s = (s or "").lower().strip().replace(" ", "_")
    if s not in _ALLOWED_VERIFY_STATUSES:
        return "needs_review"
    return s


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def extract_year(ref: str) -> str:
    m = re.search(r"\b(19\d{2}|20\d attaching", "", t)
    t = re.sub(r"\d+\.", "", t)
    return t.strip()


def extract_author(ref: str) -> str:
    if "," in ref:
        return ref.split(",")[0].strip()
    return ref.split()[0].strip()


# -----------------------------------------
# Crossref Query
# -----------------------------------------

def query_crossref(author, year, title):

    url = "https://api.crossref.org/works"

    params = {
        "query.author": author,
        "query.title": title,
        "filter": f"from-pub-date:{year},until-pub-date:{year}",
        "rows": 5,
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return []

        return r.json()["message"]["items"]

    except:
        return []


# -----------------------------------------
# OpenAlex Query
# -----------------------------------------

def query_openalex(author, year, title):

    url = "https://api.openalex.org/works"

    params = {
        "search": title,
        "filter": f"publication_year:{year}",
        "per_page": 5,
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return []

        return r.json()["results"]

    except:
        return []


# -----------------------------------------
# Scoring
# -----------------------------------------

def score_candidate(ref, title, year, author, cand):

    cand_title = cand.get("title", "")

    title_score = fuzz.token_set_ratio(title, cand_title)

    score = title_score

    if str(cand.get("publication_year", "")) == year:
        score += 20

    return score


# -----------------------------------------
# Main Verification Function
# -----------------------------------------

def verify_references_batch(
    references: List[str],
    throttle_s: float = 0.1,
    use_crossref=True,
    use_openalex=True,
):

    results = []

    for ref in references:

        year = extract_year(ref)
        author = extract_author(ref)
        title = extract_title(ref)

        best = None
        best_score = 0

        if use_crossref:

            items = query_crossref(author, year, title)

            for it in items:

                score = score_candidate(ref, title, year, author, it)

                if score > best_score:
                    best_score = score
                    best = it

        if use_openalex:

            items = query_openalex(author, year, title)

            for it in items:

                score = score_candidate(ref, title, year, author, it)

                if score > best_score:
                    best_score = score
                    best = it

        if best_score > 90:
            status = "verified"
        elif best_score > 70:
            status = "likely"
        elif best_score > 50:
            status = "needs_review"
        else:
            status = "not_found"

        results.append({

            "reference": ref,
            "status": normalize_status(status),
            "score": best_score,
            "matched_title": best.get("title", "") if best else "",
            "matched_year": best.get("publication_year", "") if best else "",
            "doi": best.get("doi", "") if best else "",
            "source": "crossref/openalex"

        })

        time.sleep(throttle_s)

    return results
