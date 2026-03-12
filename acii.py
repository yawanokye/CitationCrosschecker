# acii.py
# Anokye Citation Integrity Index (ACII)

from typing import Dict, List, Any
from collections import Counter
import math


# -----------------------------
# CATEGORY CLASSIFICATION
# -----------------------------

def _classify(score: float) -> str:

    if score >= 90:
        return "Excellent"
    elif score >= 80:
        return "Very Good"
    elif score >= 70:
        return "Good"
    elif score >= 60:
        return "Moderate"
    elif score >= 50:
        return "Weak"
    else:
        return "Poor"


# -----------------------------
# TOOLTIP EXPLANATIONS
# -----------------------------

EXPLANATIONS = {
    "ACII":
        "The Anokye Citation Integrity Index combines four indicators "
        "to assess the overall integrity and balance of a manuscript’s "
        "citation system.",

    "verification_integrity":
        "Measures the percentage of references that can be verified "
        "in scholarly databases such as Crossref or OpenAlex.",

    "citation_concentration":
        "Measures whether citations are concentrated among a few "
        "sources or distributed across many references.",

    "author_diversity":
        "Measures the diversity of authors represented in the reference list.",

    "temporal_balance":
        "Measures how well the references are distributed across publication years."
}


# -----------------------------
# SAFE RATIO
# -----------------------------

def _safe_ratio(a, b):
    return 0 if b == 0 else a / b


# -----------------------------
# INDICATOR CALCULATIONS
# -----------------------------

def _verification_integrity(rows: List[Dict[str, Any]]) -> float:

    total = len(rows)

    if total == 0:
        return 0

    verified = sum(1 for r in rows if r.get("status") == "verified")
    likely = sum(1 for r in rows if r.get("status") == "likely")

    return round((verified + 0.5 * likely) / total * 100, 2)


def _citation_concentration(rows):

    authors = []

    for r in rows:

        a = r.get("author") or r.get("matched_authors") or ""

        if a:
            authors.extend([x.strip() for x in a.split(",") if x.strip()])

    if not authors:
        return 100

    counts = Counter(authors)

    n = len(authors)

    hhi = sum((c / n) ** 2 for c in counts.values())

    concentration = min(1, hhi * 5)

    return round((1 - concentration) * 100, 2)


def _author_diversity(rows):

    authors = []

    for r in rows:

        a = r.get("author") or r.get("matched_authors") or ""

        if a:
            authors.extend([x.strip() for x in a.split(",") if x.strip()])

    if not authors:
        return 0

    counts = Counter(authors)

    n = len(authors)

    entropy = -sum((c / n) * math.log(c / n) for c in counts.values())

    max_entropy = math.log(len(counts)) if len(counts) > 1 else 1

    return round((entropy / max_entropy) * 100, 2)


def _temporal_balance(rows):

    years = []

    for r in rows:

        y = r.get("matched_year")

        if y:
            try:
                years.append(int(str(y)[:4]))
            except:
                pass

    if len(years) < 2:
        return 50

    span = max(years) - min(years)

    score = min(1, span / 20)

    return round(score * 100, 2)


# -----------------------------
# REMARK GENERATION
# -----------------------------

def _remark(metric, score, rows):

    total = len(rows)

    if metric == "verification_integrity":

        verified = sum(1 for r in rows if r.get("status") == "verified")
        pct = round(_safe_ratio(verified, total) * 100, 2)

        return f"{pct}% of references were verified in scholarly databases."

    if metric == "citation_concentration":

        return "Measures whether citations rely heavily on a few sources."

    if metric == "author_diversity":

        authors = set()

        for r in rows:

            a = r.get("author") or r.get("matched_authors") or ""

            if a:
                authors.update([x.strip() for x in a.split(",") if x.strip()])

        return f"The references include {len(authors)} unique authors."

    if metric == "temporal_balance":

        years = [r.get("matched_year") for r in rows if r.get("matched_year")]

        return f"References span {len(set(years))} publication years."

    if metric == "ACII":

        return "Composite score summarizing citation integrity across all indicators."

    return ""


# -----------------------------
# MAIN ACII COMPUTATION
# -----------------------------

def compute_acii(engine_result: Dict[str, Any], rows: List[Dict[str, Any]]) -> Dict[str, Any]:

    rows_copy = [dict(r) for r in rows]

    v = _verification_integrity(rows_copy)
    c = _citation_concentration(rows_copy)
    d = _author_diversity(rows_copy)
    t = _temporal_balance(rows_copy)

    acii = round(
        0.4 * v +
        0.2 * c +
        0.2 * d +
        0.2 * t,
        2
    )

    return {
        "ACII": {
            "score": acii,
            "category": _classify(acii),
            "explanation": EXPLANATIONS["ACII"],
            "remark": _remark("ACII", acii, rows_copy)
        },

        "components": {

            "verification_integrity": {
                "score": v,
                "category": _classify(v),
                "explanation": EXPLANATIONS["verification_integrity"],
                "remark": _remark("verification_integrity", v, rows_copy)
            },

            "citation_concentration": {
                "score": c,
                "category": _classify(c),
                "explanation": EXPLANATIONS["citation_concentration"],
                "remark": _remark("citation_concentration", c, rows_copy)
            },

            "author_diversity": {
                "score": d,
                "category": _classify(d),
                "explanation": EXPLANATIONS["author_diversity"],
                "remark": _remark("author_diversity", d, rows_copy)
            },

            "temporal_balance": {
                "score": t,
                "category": _classify(t),
                "explanation": EXPLANATIONS["temporal_balance"],
                "remark": _remark("temporal_balance", t, rows_copy)
            }
        },

        "stats": {
            "total_references": len(rows_copy)
        }
    }
