# acii.py
import re
from collections import Counter
from typing import Dict, List, Any


YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def extract_year(ref: str):
    m = YEAR_RE.search(ref or "")
    return int(m.group()) if m else None


def extract_first_author(ref: str):
    if not ref:
        return ""
    left = ref.split("(")[0]
    tokens = re.findall(r"[A-Z][a-zA-Z\-']+", left)
    return tokens[0].lower() if tokens else ""


def compute_acii(engine_result: Dict[str, Any], verify_rows: List[Dict[str, Any]]) -> Dict[str, Any]:

    r2c = engine_result.get("reconciliation_reference_to_intext", [])

    citation_counts = [r.get("times_cited", 0) for r in r2c]

    total_citations = sum(citation_counts)
    total_refs = len(r2c)

    if total_refs == 0:
        return {"ACII": 0}

    # --------------------------------------------------
    # Citation Concentration Index (CCI)
    # --------------------------------------------------

    if total_citations > 0:
        max_share = max(citation_counts) / total_citations
        cci = 1 - max_share
    else:
        cci = 0

    # --------------------------------------------------
    # Author Concentration
    # --------------------------------------------------

    authors = [extract_first_author(r["reference"]) for r in r2c if r.get("reference")]

    author_freq = Counter(authors)

    if author_freq:
        max_author_share = max(author_freq.values()) / len(authors)
        author_div = 1 - max_author_share
    else:
        author_div = 0

    # --------------------------------------------------
    # Temporal Diversity
    # --------------------------------------------------

    years = [extract_year(r["reference"]) for r in r2c if extract_year(r["reference"])]

    if years:
        year_freq = Counter(years)
        max_year_share = max(year_freq.values()) / len(years)
        temporal_balance = 1 - max_year_share
    else:
        temporal_balance = 0

    # --------------------------------------------------
    # Verification Integrity
    # --------------------------------------------------

    verified = sum(1 for r in verify_rows if r.get("status") == "verified")
    likely = sum(1 for r in verify_rows if r.get("status") == "likely")

    if verify_rows:
        verification_score = (verified + 0.5 * likely) / len(verify_rows)
    else:
        verification_score = 0

    # --------------------------------------------------
    # Final ACII Score
    # --------------------------------------------------

    acii = (
        0.35 * verification_score +
        0.25 * cci +
        0.20 * author_div +
        0.20 * temporal_balance
    )

    return {
        "ACII": round(acii * 100, 2),
        "verification_integrity": round(verification_score * 100, 2),
        "citation_concentration_index": round(cci * 100, 2),
        "author_diversity": round(author_div * 100, 2),
        "temporal_balance": round(temporal_balance * 100, 2),
        "total_references": total_refs,
        "total_citations": total_citations,
    }
