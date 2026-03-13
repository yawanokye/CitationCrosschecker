# acii.py
# Anokye Citation Integrity Index (ACII)

from typing import Dict, List, Any
from collections import Counter
import math

# --------------------------------------------------
# CATEGORY AND REMARK HELPERS
# --------------------------------------------------

def _category(score):

    if score >= 90:
        return "Excellent"
    if score >= 80:
        return "Very Good"
    if score >= 70:
        return "Good"
    if score >= 60:
        return "Moderate"
    if score >= 50:
        return "Weak"

    return "Poor"


def _remark(metric, score):

    if metric == "verification_integrity":
        return f"{score}% of references verified in scholarly databases"

    if metric == "citation_concentration":
        return "Indicates whether citations rely heavily on a few authors"

    if metric == "author_diversity":
        return "Measures diversity of authors represented in the reference list"

    if metric == "temporal_balance":
        return "Measures spread of references across publication years"

    return ""
def _safe_ratio(a, b):
    return 0 if b == 0 else a / b


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

    "ACII": acii,
    "category": _category(acii),

    "components": {

        "verification_integrity": {
            "score": v,
            "category": _category(v),
            "remark": _remark("verification_integrity", v)
        },

        "citation_concentration": {
            "score": c,
            "category": _category(c),
            "remark": _remark("citation_concentration", c)
        },

        "author_diversity": {
            "score": d,
            "category": _category(d),
            "remark": _remark("author_diversity", d)
        },

        "temporal_balance": {
            "score": t,
            "category": _category(t),
            "remark": _remark("temporal_balance", t)
        }
    },

    "stats": {
        "total_references": len(rows_copy)
    }
}
        "stats": {
            "total_references": len(rows_copy)
        }
    }
