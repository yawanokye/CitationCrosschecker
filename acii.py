# acii.py
# Citation Integrity Index (ACII) - Enhanced with Recency + Recommendations

from typing import Dict, List, Any
from collections import Counter
import math
from datetime import datetime


def _safe_ratio(a, b):
    return 0 if b == 0 else a / b


# -----------------------------------------------------
# CATEGORY + REMARK HELPERS
# -----------------------------------------------------

def _category(score: float) -> str:
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


def _get_acii_description(score: float) -> str:
    if score >= 90:
        return "Outstanding citation integrity with excellent recency and balance."
    if score >= 80:
        return "Strong citation integrity with good recency and diversity."
    if score >= 70:
        return "Satisfactory citation integrity with acceptable recency."
    if score >= 60:
        return "Adequate citation integrity. Improvements needed in recency or diversity."
    if score >= 50:
        return "Below average citation integrity."
    return "Low citation integrity. Significant improvement required."


# -----------------------------------------------------
# COMPONENTS
# -----------------------------------------------------

def _verification_integrity(rows: List[Dict[str, Any]]) -> float:
    """Percentage of references verified in scholarly databases"""
    total = len(rows)
    if total == 0:
        return 0

    verified = sum(1 for r in rows if r.get("status") == "verified")
    likely = sum(1 for r in rows if r.get("status") == "likely")

    return round((verified + 0.5 * likely) / total * 100, 2)


def _citation_concentration(rows: List[Dict[str, Any]]) -> float:
    """Measures whether citations rely heavily on few authors (HHI-based)"""
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
    # Convert HHI to 0-100 scale (lower HHI = better distribution)
    concentration_score = (1 - min(1, hhi * 5)) * 100
    return round(concentration_score, 2)


def _author_diversity(rows: List[Dict[str, Any]]) -> float:
    """Measures diversity of authors using Shannon entropy"""
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


def _temporal_balance(rows: List[Dict[str, Any]]) -> float:
    """Measures spread of references across publication years"""
    years = []
    for r in rows:
        y = r.get("matched_year")
        if y:
            try:
                years.append(int(str(y)[:4]))
            except (ValueError, TypeError):
                pass

    if len(years) < 2:
        return 50  # Neutral score for insufficient data

    span = max(years) - min(years)
    # Cap at 20 years for optimal balance
    return round(min(1, span / 20) * 100, 2)


# ---------------- NEW ----------------

def _recency_score(rows: List[Dict[str, Any]], window: int = 5) -> float:
    """Percentage of references published within the last N years"""
    current_year = datetime.now().year

    years = []
    for r in rows:
        y = r.get("matched_year")
        if y:
            try:
                years.append(int(str(y)[:4]))
            except (ValueError, TypeError):
                pass

    if not years:
        return 0

    recent = sum(1 for y in years if current_year - y <= window)
    return round((recent / len(years)) * 100, 2)


def _temporal_quality(rows: List[Dict[str, Any]]) -> float:
    """Combined measure of recency and balance (60% recency, 40% balance)"""
    recency = _recency_score(rows)
    balance = _temporal_balance(rows)
    return round(0.6 * recency + 0.4 * balance, 2)


# -----------------------------------------------------
# RECOMMENDATION ENGINE
# -----------------------------------------------------

def _recency_recommendation(rows: List[Dict[str, Any]], recency_score: float, window: int = 5) -> str:
    """Generate actionable recommendation for improving recency"""
    total = len(rows)

    if total == 0:
        return "No references available for analysis."

    # Target: 70% of references within last 5 years
    target = 70.0
    current = recency_score

    if current >= target:
        return f"Recency is strong ({current:.0f}% ≤{window} years). Maintain balance with foundational studies."

    # Calculate how many recent references needed
    current_recent = int(round((current / 100) * total))
    needed_recent = int(round((target / 100) * total)) - current_recent

    if needed_recent <= 0:
        return f"Recency is acceptable ({current:.0f}% ≤{window} years). Consider adding a few more recent sources."

    return f"Add approximately {needed_recent} recent source(s) (≤{window} years) to improve scholarly relevance (current: {current:.0f}%, target: {target:.0f}%)."


def _verification_recommendation(verification_score: float, total: int) -> str:
    """Generate recommendation for improving verification rate"""
    if verification_score >= 80:
        return "Verification rate is excellent. Continue maintaining high standards."
    if verification_score >= 60:
        missing = int(round((1 - 0.8) * total)) if total > 0 else 0
        return f"Verification rate is good ({verification_score:.0f}%). Consider verifying {missing} more reference(s) to reach 80%."
    if verification_score >= 40:
        return f"Verification rate needs improvement ({verification_score:.0f}%). Run online verification for unverified references."
    return f"Low verification rate ({verification_score:.0f}%). Strongly recommend running online verification for all references."


def _diversity_recommendation(diversity_score: float, concentration_score: float) -> str:
    """Generate recommendation for improving author diversity"""
    if diversity_score >= 70:
        return "Excellent author diversity. Citations are well-distributed."
    if diversity_score >= 50:
        return f"Good author diversity ({diversity_score:.0f}%). Consider citing from a broader range of research groups."
    return f"Limited author diversity ({diversity_score:.0f}%). Citation concentration is high. Expand to include more varied sources."


# -----------------------------------------------------
# GLOBAL REMARK ENGINE
# -----------------------------------------------------

def _get_acii_remark(acii: float, recency: float, balance: float, verification: float, diversity: float) -> str:
    """Generate a comprehensive remark about the citation quality"""
    strengths = []
    weaknesses = []

    # Recency assessment
    if recency >= 70:
        strengths.append("excellent recency")
    elif recency >= 50:
        strengths.append("good recency")
    else:
        weaknesses.append("outdated references")

    # Balance assessment
    if balance >= 70:
        strengths.append("good temporal spread")
    elif balance >= 50:
        pass  # neutral
    else:
        weaknesses.append("poor temporal balance")

    # Verification assessment
    if verification >= 70:
        strengths.append("strong verification rate")
    elif verification >= 50:
        pass  # neutral
    else:
        weaknesses.append("low verification rate")

    # Diversity assessment
    if diversity >= 70:
        strengths.append("excellent author diversity")
    elif diversity >= 50:
        strengths.append("good author diversity")
    else:
        weaknesses.append("limited author diversity")

    # Build remark
    remark_parts = []
    if strengths:
        remark_parts.append(f"✓ {', '.join(strengths)}")
    if weaknesses:
        remark_parts.append(f"⚠️ {', '.join(weaknesses)}")

    if not remark_parts:
        return "Citation quality is moderate. Room for improvement in all areas."

    return " | ".join(remark_parts)


# -----------------------------------------------------
# MAIN ACII COMPUTATION
# -----------------------------------------------------

def compute_acii(engine_result: Dict[str, Any], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compute the Academic Citation Integrity Index (ACII)
    
    Components:
    - Verification Integrity (35%): How many references are verified
    - Citation Concentration (20%): Whether citations rely on few authors
    - Author Diversity (20%): Diversity of authors cited
    - Temporal Quality (25%): Recency + temporal balance of references
    """
    rows_copy = [dict(r) for r in rows]

    # Core components
    v = _verification_integrity(rows_copy)
    c = _citation_concentration(rows_copy)
    d = _author_diversity(rows_copy)

    # Temporal components
    recency = _recency_score(rows_copy)
    balance = _temporal_balance(rows_copy)
    tq = _temporal_quality(rows_copy)

    # Weighted ACII score
    acii = round(
        0.35 * v +
        0.20 * c +
        0.20 * d +
        0.25 * tq,
        2
    )

    # Generate recommendations
    recency_recommendation = _recency_recommendation(rows_copy, recency)
    verification_recommendation = _verification_recommendation(v, len(rows_copy))
    diversity_recommendation = _diversity_recommendation(d, c)

    # Compile recommendations
    recommendations = {
        "recency": recency_recommendation,
        "verification": verification_recommendation,
        "diversity": diversity_recommendation
    }

    # Add priority recommendation
    priority_areas = []
    if v < 60:
        priority_areas.append("verification")
    if recency < 50:
        priority_areas.append("recency")
    if d < 50:
        priority_areas.append("diversity")
    if balance < 40:
        priority_areas.append("temporal balance")

    if priority_areas:
        recommendations["priority"] = f"Focus on improving: {', '.join(priority_areas)}"

    # Generate remark
    acii_remark = _get_acii_remark(acii, recency, balance, v, d)

    return {
        "ACII": acii,
        "category": _category(acii),
        "description": _get_acii_description(acii),
        "remark": acii_remark,

        "components": {
            "verification_integrity": {
                "score": v,
                "category": _category(v),
                "remark": _get_verification_remark(v)
            },
            "citation_concentration": {
                "score": c,
                "category": _category(c),
                "remark": _get_concentration_remark(c)
            },
            "author_diversity": {
                "score": d,
                "category": _category(d),
                "remark": _get_diversity_remark(d)
            },
            "recency": {
                "score": recency,
                "category": _category(recency),
                "remark": _get_recency_remark(recency)
            },
            "temporal_balance": {
                "score": balance,
                "category": _category(balance),
                "remark": _get_temporal_remark(balance)
            },
            "temporal_quality": {
                "score": tq,
                "category": _category(tq),
                "remark": f"Combined score from recency (60%) and balance (40%)"
            }
        },

        "recommendations": recommendations,

        "stats": {
            "total_references": len(rows_copy)
        }
    }


# -----------------------------------------------------
# HELPER REMARKS FOR COMPONENTS
# -----------------------------------------------------

def _get_verification_remark(score: float) -> str:
    if score >= 80:
        return f"Excellent - {score:.0f}% of references verified in scholarly databases"
    if score >= 60:
        return f"Good - {score:.0f}% of references verified"
    if score >= 40:
        return f"Moderate - {score:.0f}% of references verified"
    if score >= 20:
        return f"Low - only {score:.0f}% of references verified"
    return f"Poor - {score:.0f}% of references verified. Consider verifying sources."


def _get_concentration_remark(score: float) -> str:
    if score >= 80:
        return "Excellent distribution - citations spread across many authors"
    if score >= 60:
        return "Good distribution - citations reasonably spread"
    if score >= 40:
        return "Moderate concentration - some authors cited frequently"
    return "High concentration - few authors dominate citations"


def _get_diversity_remark(score: float) -> str:
    if score >= 80:
        return "Excellent - references include diverse authors"
    if score >= 60:
        return "Good - reasonable author diversity"
    if score >= 40:
        return "Moderate - some diversity in authors"
    return "Low - limited author diversity"


def _get_temporal_remark(score: float) -> str:
    if score >= 80:
        return "Excellent temporal distribution across years"
    if score >= 60:
        return "Good spread of publication years"
    if score >= 40:
        return "Moderate temporal spread"
    return "Poor - references concentrated in few years"


def _get_recency_remark(score: float) -> str:
    if score >= 70:
        return f"Excellent - {score:.0f}% of references are recent (≤5 years)"
    if score >= 50:
        return f"Good - {score:.0f}% of references are recent"
    if score >= 30:
        return f"Moderate - only {score:.0f}% of references are recent"
    return f"Poor - {score:.0f}% of references are recent. Add more current sources."
