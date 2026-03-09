# verify.py
# CitationCrosschecker Online Verification Engine

import re
import time
import requests

from ai_reconstruct import reconstruct_reference


CROSSREF_API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"

HEADERS = {
    "User-Agent": "CitationCrosschecker/1.0"
}


DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s]+", re.I)


# -----------------------------
# DOI Detection
# -----------------------------
def extract_doi(reference):

    if not reference:
        return None

    m = DOI_RE.search(reference)

    if m:
        return m.group(0).rstrip(".,;")

    return None


# -----------------------------
# Title Extraction
# -----------------------------
def extract_title(reference):

    if not reference:
        return ""

    ref = reference

    # remove year
    ref = re.sub(r"\(\d{4}\)", "", ref)

    # remove author block
    parts = re.split(r"\.\s", ref, 1)

    if len(parts) > 1:
        ref = parts[1]

    # keep first sentence
    ref = re.split(r"\.\s", ref)[0]

    return ref.strip()


# -----------------------------
# Text Normalization
# -----------------------------
def normalize_text(text):

    if not text:
        return ""

    text = text.lower()

    text = re.sub(r"[^a-z0-9\s]", " ", text)

    text = re.sub(r"\s+", " ", text)

    return text.strip()


# -----------------------------
# Similarity Scoring
# -----------------------------
def similarity_score(reference, candidate_title):

    ref_title = extract_title(reference)

    ref = normalize_text(ref_title)
    cand = normalize_text(candidate_title)

    if not ref or not cand:
        return 0

    ref_words = set(ref.split())
    cand_words = set(cand.split())

    common = ref_words.intersection(cand_words)

    score = int((len(common) / max(len(ref_words), 1)) * 100)

    return score


# -----------------------------
# Classification
# -----------------------------
def classify(score):

    if score >= 85:
        return "verified"

    if score >= 65:
        return "likely"

    if score >= 40:
        return "needs_review"

    return "not_found"


# -----------------------------
# Crossref Lookup
# -----------------------------
def crossref_lookup(reference):

    try:

        title = extract_title(reference)

        if not title:
            return None

        r = requests.get(
            CROSSREF_API,
            params={
                "query.title": title,
                "rows": 3
            },
            headers=HEADERS,
            timeout=10
        )

        if r.status_code != 200:
            return None

        items = r.json().get("message", {}).get("items", [])

        if not items:
            return None

        item = items[0]

        title = ""

        if item.get("title"):
            title = item["title"][0]

        doi = item.get("DOI")

        return {
            "title": title,
            "doi": doi,
            "source": "crossref"
        }

    except:
        return None


# -----------------------------
# OpenAlex Lookup
# -----------------------------
def openalex_lookup(reference):

    try:

        title = extract_title(reference)

        if not title:
            return None

        r = requests.get(
            OPENALEX_API,
            params={
                "search": title,
                "per_page": 3
            },
            headers=HEADERS,
            timeout=10
        )

        if r.status_code != 200:
            return None

        results = r.json().get("results", [])

        if not results:
            return None

        item = results[0]

        return {
            "title": item.get("display_name"),
            "doi": item.get("doi"),
            "source": "openalex"
        }

    except:
        return None


# -----------------------------
# Main Batch Verification
# -----------------------------
def verify_references_batch(
    references,
    style="apa",
    throttle_s=0.12,
    use_crossref=True,
    use_openalex=True
):

    rows = []

    AI_LIMIT = 10
    ai_used = 0

    for ref in references:

        ref = ref.strip()

        doi = extract_doi(ref)

        # -----------------------------
        # DOI verification
        # -----------------------------
        if doi:

            rows.append({
                "reference": ref,
                "status": "verified",
                "doi": doi,
                "matched_title": "",
                "score": 100,
                "source": "doi",
                "style": style
            })

            continue


        result = None

        # -----------------------------
        # Crossref search
        # -----------------------------
        if use_crossref:
            result = crossref_lookup(ref)

        # -----------------------------
        # OpenAlex fallback
        # -----------------------------
        if not result and use_openalex:
            result = openalex_lookup(ref)

        # -----------------------------
        # If result found
        # -----------------------------
        if result:

            matched_title = result.get("title", "")

            score = similarity_score(ref, matched_title)

            status = classify(score)

            rows.append({
                "reference": ref,
                "status": status,
                "doi": result.get("doi"),
                "matched_title": matched_title,
                "score": score,
                "source": result.get("source"),
                "style": style
            })


        # -----------------------------
        # AI Reconstruction fallback
        # -----------------------------
        else:

            if ai_used < AI_LIMIT:

                repaired = reconstruct_reference(ref)

                result = crossref_lookup(repaired)

                if not result:
                    result = openalex_lookup(repaired)

                ai_used += 1

                if result:

                    matched_title = result.get("title", "")

                    score = similarity_score(repaired, matched_title)

                    status = classify(score)

                    rows.append({
                        "reference": ref,
                        "status": status,
                        "doi": result.get("doi"),
                        "matched_title": matched_title,
                        "score": score,
                        "source": "ai_reconstruct",
                        "style": style
                    })

                    continue

            rows.append({
                "reference": ref,
                "status": "not_found",
                "doi": "",
                "matched_title": "",
                "score": 0,
                "source": "",
                "style": style
            })

        time.sleep(throttle_s)

    return rows
