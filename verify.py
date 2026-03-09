# verify.py
# Online citation verification

import re
import time
import requests

DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s)]+", re.I)

CROSSREF = "https://api.crossref.org/works"
OPENALEX = "https://api.openalex.org/works"

HEADERS = {
    "User-Agent": "CitationCrosschecker/1.0"
}


def extract_doi(reference):

    m = DOI_RE.search(reference)
    if m:
        return m.group(0).rstrip(".,;")

    return None


def crossref_lookup(reference):

    try:

        r = requests.get(
            CROSSREF,
            params={"query.bibliographic": reference, "rows": 1},
            headers=HEADERS,
            timeout=10
        )

        if r.status_code != 200:
            return None

        data = r.json()

        items = data.get("message", {}).get("items", [])

        if not items:
            return None

        item = items[0]

        title = ""
        if item.get("title"):
            title = item["title"][0]

        doi = item.get("DOI")

        return {
            "title": title,
            "doi": doi
        }

    except:
        return None


def openalex_lookup(reference):

    try:

        r = requests.get(
            OPENALEX,
            params={"search": reference, "per_page": 1},
            headers=HEADERS,
            timeout=10
        )

        if r.status_code != 200:
            return None

        data = r.json()

        results = data.get("results", [])

        if not results:
            return None

        item = results[0]

        return {
            "title": item.get("display_name"),
            "doi": item.get("doi")
        }

    except:
        return None


def score_match(reference, result):

    if not result:
        return 0

    ref = reference.lower()
    title = (result.get("title") or "").lower()

    common = 0

    for word in ref.split():
        if word in title:
            common += 1

    return min(common * 10, 100)


def classify(score):

    if score >= 80:
        return "verified"

    if score >= 50:
        return "likely"

    if score >= 30:
        return "needs_review"

    return "not_found"


def verify_references_batch(
    references,
    style="apa",
    throttle_s=0.12,
    use_crossref=True,
    use_openalex=True,
):

    rows = []

    for ref in references:

        doi = extract_doi(ref)

        result = None
        source = ""

        if use_crossref:
            result = crossref_lookup(ref)
            source = "crossref"

        if not result and use_openalex:
            result = openalex_lookup(ref)
            source = "openalex"

        score = score_match(ref, result)

        status = classify(score)

        rows.append({
            "reference": ref,
            "status": status,
            "doi": doi or (result.get("doi") if result else ""),
            "matched_title": (result.get("title") if result else ""),
            "score": score,
            "source": source,
            "style": style
        })

        time.sleep(throttle_s)

    return rows
