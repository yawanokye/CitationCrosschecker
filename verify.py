# verify.py
# Online reference verification module
# Supports Crossref, OpenAlex and Unpaywall

import re
import time
import requests

DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s)]+", re.I)

CROSSREF_API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"
UNPAYWALL_API = "https://api.unpaywall.org/v2/"

HEADERS = {
    "User-Agent": "CitationIntegrityChecker/1.0 (mailto:research@example.com)"
}


# ---------------------------------------------------------
# Extract DOI if present
# ---------------------------------------------------------

def extract_doi(reference):

    m = DOI_RE.search(reference)
    if m:
        return m.group(0).rstrip(".,;")

    return None


# ---------------------------------------------------------
# Crossref verification
# ---------------------------------------------------------

def check_crossref(reference):

    try:

        r = requests.get(
            CROSSREF_API,
            params={"query.bibliographic": reference, "rows": 1},
            headers=HEADERS,
            timeout=10
        )

        if r.status_code != 200:
            return None

        data = r.json()

        if data["message"]["items"]:
            return data["message"]["items"][0]

    except:
        return None

    return None


# ---------------------------------------------------------
# OpenAlex verification
# ---------------------------------------------------------

def check_openalex(reference):

    try:

        r = requests.get(
            OPENALEX_API,
            params={"search": reference, "per_page": 1},
            headers=HEADERS,
            timeout=10
        )

        if r.status_code != 200:
            return None

        data = r.json()

        if data["results"]:
            return data["results"][0]

    except:
        return None

    return None


# ---------------------------------------------------------
# Unpaywall verification
# ---------------------------------------------------------

def check_unpaywall(doi):

    if not doi:
        return None

    try:

        r = requests.get(
            UNPAYWALL_API + doi,
            params={"email": "research@example.com"},
            headers=HEADERS,
            timeout=10
        )

        if r.status_code == 200:
            return r.json()

    except:
        return None

    return None


# ---------------------------------------------------------
# Score reference match
# ---------------------------------------------------------

def score_reference(reference, crossref_result, openalex_result):

    if crossref_result and openalex_result:
        return "verified"

    if crossref_result or openalex_result:
        return "likely"

    if len(reference) > 50:
        return "needs_review"

    return "not_found"


# ---------------------------------------------------------
# Batch verification
# ---------------------------------------------------------

def verify_references_batch(
    references,
    throttle_s=0.12,
    use_crossref=True,
    use_openalex=True
):

    results = []

    counts = {
        "verified": 0,
        "likely": 0,
        "needs_review": 0,
        "not_found": 0,
        "offline": 0
    }

    for ref in references:

        try:

            doi = extract_doi(ref)

            crossref_data = None
            openalex_data = None

            if use_crossref:
                crossref_data = check_crossref(ref)

            if use_openalex:
                openalex_data = check_openalex(ref)

            score = score_reference(ref, crossref_data, openalex_data)

            counts[score] += 1

            results.append({
                "reference": ref,
                "doi": doi,
                "crossref": bool(crossref_data),
                "openalex": bool(openalex_data),
                "status": score
            })

            time.sleep(throttle_s)

        except:

            counts["offline"] += 1

            results.append({
                "reference": ref,
                "status": "offline"
            })

    return {
        "verified": counts["verified"],
        "likely": counts["likely"],
        "needs_review": counts["needs_review"],
        "not_found": counts["not_found"],
        "offline": counts["offline"],
        "total": len(references),
        "results": results
    }
