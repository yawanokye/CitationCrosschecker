# acii.py

import re
from collections import Counter
from typing import Dict, List, Any

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


# ---------------------------------------------------------
# Helper extractors
# ---------------------------------------------------------

def extract_year(ref: str):

    if not ref:
        return None

    m = YEAR_RE.search(ref)
    return int(m.group()) if m else None


def extract_first_author(ref: str):

    if not ref:
        return ""

    left = ref.split("(")[0]

    tokens = re.findall(r"[A-Z][a-zA-Z\-']+", left)

    return tokens[0].lower() if tokens else ""


# ---------------------------------------------------------
# ACII Calculation
# ---------------------------------------------------------

def compute_acii(engine_result: Dict[str, Any],
                 verify_rows: List[Dict[str, Any]]) -> Dict[str, Any]:

    r2c = engine_result.get("reconciliation_reference_to_intext", [])

    citation_counts = [r.get("times_cited", 0) for r in r2c]

    total_citations = sum(citation_counts)
    total_refs = len(r2c)

    if total_refs == 0:
        return {"ACII": 0}

    # -----------------------------------------------------
    # 1 Citation Concentration Index
    # -----------------------------------------------------

    if total_citations > 0:

        max_share = max(citation_counts) / total_citations

        cci = 1 - max_share

    else:
        cci = 0


    # -----------------------------------------------------
    # 2 Author Diversity
    # -----------------------------------------------------

    authors = []

    for r in r2c:

        a = extract_first_author(r.get("reference"))

        if a:
            authors.append(a)

    author_freq = Counter(authors)

    if author_freq:

        max_author_share = max(author_freq.values()) / len(authors)

        author_diversity = 1 - max_author_share

    else:

        author_diversity = 0


    # -----------------------------------------------------
    # 3 Temporal Balance
    # -----------------------------------------------------

    years = []

    for r in r2c:

        y = extract_year(r.get("reference"))

        if y:
            years.append(y)

    if years:

        year_freq = Counter(years)

        max_year_share = max(year_freq.values()) / len(years)

        temporal_balance = 1 - max_year_share

    else:

        temporal_balance = 0


    # -----------------------------------------------------
    # 4 Verification Integrity
    # -----------------------------------------------------

    verified = sum(1 for r in verify_rows if r.get("status") == "verified")

    likely = sum(1 for r in verify_rows if r.get("status") == "likely")

    needs_review = sum(1 for r in verify_rows if r.get("status") == "needs_review")

    total_checked = len(verify_rows)

    if total_checked > 0:

        verification_integrity = (
            verified +
            0.5 * likely +
            0.25 * needs_review
        ) / total_checked

    else:

        verification_integrity = 0


    # -----------------------------------------------------
    # Final ACII Score
    # -----------------------------------------------------

    acii_score = (
        0.40 * verification_integrity +
        0.25 * cci +
        0.20 * author_diversity +
        0.15 * temporal_balance
    )


    # convert to percentage
    ACII = round(acii_score * 100, 2)


    return {

        "ACII": ACII,

        "components": {

            "verification_integrity": round(verification_integrity * 100, 2),

            "citation_concentration_index": round(cci * 100, 2),

            "author_diversity": round(author_diversity * 100, 2),

            "temporal_balance": round(temporal_balance * 100, 2),

        },

        "stats": {

            "total_references": total_refs,

            "total_citations": total_citations,

            "verified_references": verified,

        }
    }
