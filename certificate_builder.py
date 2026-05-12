"""
certificate_builder.py
CiteIntegrity Citation Integrity Review Certificate.

This module builds a careful, defensible certificate from an existing CiteIntegrity
job result. It does not claim institutional approval, journal acceptance,
plagiarism clearance, or source authenticity.

Certificate includes:
- CiteIntegrity logo and brand name
- timestamp/date generated
- total in-text citations
- total references
- ACII score
- clearance status
- automatically verified references
- manually verified references
- not indexed but plausible references
- references still needing review
- not found references
- missing references
- uncited references
- claim-support issues
- citation-needed claims
- required corrections
"""

from __future__ import annotations

import hashlib
import html
from datetime import datetime
from typing import Any, Dict, List, Tuple


CLAIM_CRITICAL_STATUSES = {
    "insufficient_evidence",
    "no_evidence_found",
    "no_source_found",
    "source_not_found",
    "matched_source_not_available",
}
CLAIM_MODERATE_STATUSES = {
    "weak_or_unclear",
    "related_evidence",
    "source_needs_review",
    "manual_review_required",
    "insufficient_title_overlap",
    "title_overlap_review_required",
}


CITEINTEGRITY_LOGO_SVG = """
<svg class="ci-logo" viewBox="0 0 80 50" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <text x="2" y="38" font-size="44" font-weight="800" fill="#0f172a" font-family="Georgia, serif">C</text>
    <text x="40" y="38" font-size="44" font-weight="800" fill="#0f172a" font-family="Georgia, serif">I</text>
    <path d="M60 32 L66 40 L76 25" stroke="#19b36b" stroke-width="6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
""".strip()


def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except Exception:
        return default


def _norm(value: Any) -> str:
    return _safe_str(value).strip().lower()


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _html(value: Any) -> str:
    return html.escape(_safe_str(value), quote=True)


def _utc_timestamp() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _display_timestamp(timestamp: str) -> str:
    if not timestamp:
        return ""
    return timestamp.replace("T", " ").replace("Z", " UTC")


def _certificate_id(job_id: str) -> str:
    today = datetime.utcnow().strftime("%Y%m%d")
    digest = hashlib.sha1(_safe_str(job_id).encode("utf-8")).hexdigest()[:8].upper()
    return f"CI-{today}-{digest}"


def _summary_counts(result: Dict[str, Any]) -> Dict[str, int]:
    summary = _dict(result.get("summary"))

    total_references = (
        _safe_int(summary.get("reference_entries_found"))
        or len(_list(result.get("references_raw")))
        or len(_list(result.get("references")))
    )

    total_citations = (
        _safe_int(summary.get("in_text_citations_found"))
        or len(_list(result.get("in_text_citations")))
        or len(_list(result.get("citations")))
        or len(_list(result.get("reconciliation_intext_to_reference")))
    )

    missing_count = (
        _safe_int(summary.get("missing_in_references"))
        or len(_list(result.get("missing_in_references")))
    )

    uncited_count = (
        _safe_int(summary.get("uncited_references"))
        or len(_list(result.get("uncited_references")))
    )

    return {
        "total_references": total_references,
        "total_in_text_citations": total_citations,
        "missing_references": missing_count,
        "uncited_references": uncited_count,
    }


def _verification_counts(result: Dict[str, Any]) -> Dict[str, int]:
    ov = _dict(result.get("online_verification"))
    rows = _list(ov.get("rows"))

    out = {
        "automatically_verified_references": 0,
        "likely_references": 0,
        "references_still_needing_review": 0,
        "not_found_references": 0,
        "offline_references": 0,
        "manually_verified_references": 0,
        "manual_not_verified_references": 0,
        "not_indexed_but_plausible_references": 0,
        "manual_keep_needs_review": 0,
        "total_verification_rows": len(rows),
    }

    for row in rows:
        if not isinstance(row, dict):
            continue

        status = _norm(row.get("status") or row.get("automated_status") or "offline")
        manual = _norm(row.get("manual_decision"))

        if status == "verified":
            out["automatically_verified_references"] += 1
        elif status == "likely":
            out["likely_references"] += 1
        elif status == "needs_review":
            out["references_still_needing_review"] += 1
        elif status == "not_found":
            out["not_found_references"] += 1
        elif status == "offline":
            out["offline_references"] += 1

        if manual == "manual_verified":
            out["manually_verified_references"] += 1
        elif manual == "manual_not_verified":
            out["manual_not_verified_references"] += 1
        elif manual == "not_indexed_but_plausible":
            out["not_indexed_but_plausible_references"] += 1
        elif manual == "keep_needs_review":
            out["manual_keep_needs_review"] += 1

    # Prefer deduplicated backend summary if available.
    ms = _dict(result.get("manual_verification_summary"))
    if ms:
        out["manually_verified_references"] = _safe_int(
            ms.get("manual_verified"),
            out["manually_verified_references"],
        )
        out["manual_not_verified_references"] = _safe_int(
            ms.get("manual_not_verified"),
            out["manual_not_verified_references"],
        )
        out["not_indexed_but_plausible_references"] = _safe_int(
            ms.get("not_indexed_but_plausible"),
            out["not_indexed_but_plausible_references"],
        )
        out["manual_keep_needs_review"] = _safe_int(
            ms.get("keep_needs_review"),
            out["manual_keep_needs_review"],
        )
        out["unique_manual_decisions"] = _safe_int(
            ms.get("unique_manual_decisions") or ms.get("total_manual_decisions")
        )
        out["total_manual_clicks"] = _safe_int(ms.get("total_manual_clicks"))

    return out


def _claim_counts(result: Dict[str, Any]) -> Dict[str, int]:
    rows = _list(result.get("claim_support"))

    out = {
        "claim_support_rows": len(rows),
        "strong_support": 0,
        "moderate_support": 0,
        "related_evidence": 0,
        "weak_or_unclear": 0,
        "insufficient_evidence": 0,
        "no_evidence_found": 0,
        "claim_support_issues": 0,
        "critical_claim_support_issues": 0,
        "moderate_claim_support_issues": 0,
    }

    for row in rows:
        if not isinstance(row, dict):
            continue

        status = _norm(row.get("support_status") or row.get("status") or "offline")

        if status in out:
            out[status] += 1

        if status in CLAIM_CRITICAL_STATUSES:
            out["critical_claim_support_issues"] += 1
            out["claim_support_issues"] += 1
        elif status in CLAIM_MODERATE_STATUSES:
            out["moderate_claim_support_issues"] += 1
            out["claim_support_issues"] += 1

        source_status = _norm(row.get("source_verification_status") or row.get("verification_status"))
        if source_status in {"needs_review", "not_found", "offline", "manual_not_verified"}:
            out["moderate_claim_support_issues"] += 1
            out["claim_support_issues"] += 1

    return out


def _citation_needed_counts(result: Dict[str, Any]) -> Dict[str, int]:
    rows = (
        _list(result.get("citation_needed_claims"))
        or _list(result.get("citation_needed"))
        or _list(result.get("claims_needing_citation"))
    )

    high = 0
    medium = 0
    low = 0

    for row in rows:
        if not isinstance(row, dict):
            medium += 1
            continue

        priority = _norm(row.get("priority") or row.get("risk") or row.get("severity") or "medium")
        if priority in {"high", "critical"}:
            high += 1
        elif priority in {"low", "minor"}:
            low += 1
        else:
            medium += 1

    return {
        "citation_needed_claims": len(rows),
        "high_priority_citation_needed": high,
        "medium_priority_citation_needed": medium,
        "low_priority_citation_needed": low,
    }


def _acii(result: Dict[str, Any]) -> Tuple[float, str]:
    acii = _dict(result.get("acii"))
    score = _safe_float(acii.get("ACII") or acii.get("score") or acii.get("value"), 0.0)

    if score >= 90:
        rating = "Excellent"
    elif score >= 80:
        rating = "Very Good"
    elif score >= 70:
        rating = "Good"
    elif score >= 60:
        rating = "Moderate"
    elif score >= 50:
        rating = "Weak"
    elif score > 0:
        rating = "Poor"
    else:
        rating = "Not available"

    return round(score, 2), rating


def _risk_counts(summary: Dict[str, int], verify: Dict[str, int], claim: Dict[str, int], cite_needed: Dict[str, int]) -> Dict[str, int]:
    critical = (
        verify["not_found_references"]
        + verify["offline_references"]
        + verify["manual_not_verified_references"]
        + summary["missing_references"]
        + claim["critical_claim_support_issues"]
        + cite_needed["high_priority_citation_needed"]
    )

    moderate = (
        verify["references_still_needing_review"]
        + verify["not_indexed_but_plausible_references"]
        + verify["manual_keep_needs_review"]
        + claim["moderate_claim_support_issues"]
        + cite_needed["medium_priority_citation_needed"]
    )

    minor = (
        verify["likely_references"]
        + summary["uncited_references"]
        + cite_needed["low_priority_citation_needed"]
    )

    return {
        "critical": max(critical, 0),
        "moderate": max(moderate, 0),
        "minor": max(minor, 0),
    }


def _clearance_status(score: float, summary: Dict[str, int], verify: Dict[str, int], claim: Dict[str, int], cite_needed: Dict[str, int]) -> str:
    risk = _risk_counts(summary, verify, claim, cite_needed)
    critical = risk["critical"]
    moderate = risk["moderate"]

    if score >= 85 and critical == 0 and moderate <= 5:
        return "Clearance Recommended"

    if score >= 70 and critical <= 5:
        return "Conditional Clearance"

    if score >= 55 or moderate > 0:
        return "Limited Clearance"

    return "Revision Required"


def _requirements(summary: Dict[str, int], verify: Dict[str, int], claim: Dict[str, int], cite_needed: Dict[str, int]) -> List[str]:
    reqs = []

    if verify["not_found_references"]:
        reqs.append(f"Resolve {verify['not_found_references']} reference(s) marked not_found.")
    if verify["offline_references"]:
        reqs.append(f"Recheck {verify['offline_references']} reference(s) that could not be verified online.")
    if verify["references_still_needing_review"]:
        reqs.append(f"Review {verify['references_still_needing_review']} reference(s) still needing review.")
    if verify["manual_not_verified_references"]:
        reqs.append(f"Correct or remove {verify['manual_not_verified_references']} reference(s) marked not verified after manual search.")
    if summary["missing_references"]:
        reqs.append(f"Add full reference-list entries for {summary['missing_references']} missing in-text citation(s).")
    if summary["uncited_references"]:
        reqs.append(f"Review {summary['uncited_references']} uncited reference(s) and either cite or remove them.")
    if claim["critical_claim_support_issues"]:
        reqs.append(f"Strengthen or correct {claim['critical_claim_support_issues']} critical claim-support issue(s).")
    if claim["moderate_claim_support_issues"]:
        reqs.append(f"Review {claim['moderate_claim_support_issues']} claim-support row(s) marked weak, related, or requiring manual review.")
    if cite_needed["citation_needed_claims"]:
        reqs.append(f"Add citations for {cite_needed['citation_needed_claims']} uncited claim(s) that appear to require source support.")

    if not reqs:
        reqs.append("No major citation integrity correction was identified by the automated review. Conduct final human review before submission.")

    reqs.append("Re-run CiteIntegrity after corrections to update this certificate.")
    return reqs


def build_citation_integrity_certificate(
    result: Dict[str, Any],
    *,
    job_id: str = "",
    access: Dict[str, Any] | None = None,
    package_label: str = "",
) -> Dict[str, Any]:
    result = result or {}
    access = access or {}

    generated_at = _utc_timestamp()
    summary = _summary_counts(result)
    verify = _verification_counts(result)
    claim = _claim_counts(result)
    cite_needed = _citation_needed_counts(result)
    score, rating = _acii(result)
    risk = _risk_counts(summary, verify, claim, cite_needed)
    status = _clearance_status(score, summary, verify, claim, cite_needed)

    purchase = access.get("purchase") if isinstance(access.get("purchase"), dict) else {}
    analyses_total = _safe_int(purchase.get("analyses_total"))
    analyses_used = _safe_int(purchase.get("analyses_used"))

    package_name = (
        package_label
        or access.get("tier_key")
        or purchase.get("package_key")
        or result.get("package")
        or "Full Review"
    )

    certificate = {
        "logo_svg": CITEINTEGRITY_LOGO_SVG,
        "brand_name": "CiteIntegrity",
        "certificate_title": "Citation Integrity Certificate",
        "certificate_id": _certificate_id(job_id),
        "generated_at": generated_at,
        "generated_at_display": _display_timestamp(generated_at),
        "timestamp": generated_at,
        "job_id": job_id,
        "package": package_name,
        "analysis_run": (
            f"{analyses_used} of {analyses_total}" if analyses_total else "Not available"
        ),
        "total_in_text_citations": summary["total_in_text_citations"],
        "total_references": summary["total_references"],
        "acii_score": score if score else None,
        "acii_rating": rating,
        "clearance_status": status,
        "summary": {
            **summary,
            **verify,
            **claim,
            **cite_needed,
            "acii_score": score if score else None,
            "clearance_status": status,
        },
        "risk_counts": risk,
        "clearance_requirements": _requirements(summary, verify, claim, cite_needed),
        "coverage_note": (
            "References not found in Crossref, OpenAlex, Semantic Scholar, DataCite, or related discovery sources are not automatically invalid. "
            "They are reported for manual review unless other evidence indicates a higher citation integrity risk."
        ),
        "validity_note": (
            "This certificate summarises automated and user-recorded manual citation integrity review in CiteIntegrity. "
            "It does not replace academic supervision, institutional examination, journal peer review, plagiarism screening, or independent source verification."
        ),
    }

    return certificate


def render_certificate_html(certificate: Dict[str, Any]) -> str:
    c = certificate or {}
    s = c.get("summary") or {}
    r = c.get("risk_counts") or {}
    reqs = c.get("clearance_requirements") or []

    def row(label: str, value: Any) -> str:
        return f"<tr><td>{_html(label)}</td><td><strong>{_html(value)}</strong></td></tr>"

    requirements_html = "".join(f"<li>{_html(item)}</li>" for item in reqs)

    metrics = [
        ("Total in-text citations", c.get("total_in_text_citations", s.get("total_in_text_citations", 0))),
        ("Total references", c.get("total_references", s.get("total_references", 0))),
        ("ACII score", c.get("acii_score", "Not available")),
        ("Clearance status", c.get("clearance_status", "")),
        ("Automatically verified references", s.get("automatically_verified_references", 0)),
        ("Manually verified references", s.get("manually_verified_references", 0)),
        ("Not indexed but plausible references", s.get("not_indexed_but_plausible_references", 0)),
        ("References still needing review", s.get("references_still_needing_review", 0)),
        ("Not found references", s.get("not_found_references", 0)),
        ("Missing references", s.get("missing_references", 0)),
        ("Uncited references", s.get("uncited_references", 0)),
        ("Claim-support issues", s.get("claim_support_issues", 0)),
        ("Citation-needed claims", s.get("citation_needed_claims", 0)),
    ]

    metrics_html = "".join(row(k, v) for k, v in metrics)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Citation Integrity Certificate</title>
<style>
    body {{ font-family: Arial, sans-serif; color: #0f172a; margin: 34px; line-height: 1.5; }}
    .cert {{ border: 3px solid #0f172a; padding: 28px; }}
    .header {{ text-align: center; border-bottom: 1px solid #cbd5e1; padding-bottom: 18px; margin-bottom: 22px; }}
    .brand-row {{ display: inline-flex; align-items: center; justify-content: center; gap: 12px; margin-bottom: 8px; }}
    .ci-logo {{ width: 78px; height: 50px; display: inline-block; vertical-align: middle; }}
    .brand {{ font-size: 30px; font-weight: 900; letter-spacing: -0.6px; }}
    .title {{ font-size: 22px; font-weight: 800; margin-top: 4px; }}
    .subtitle {{ color: #475569; font-size: 14px; }}
    .timestamp {{ color: #475569; font-size: 13px; margin-top: 6px; }}
    .status {{ display: inline-block; padding: 8px 14px; border-radius: 999px; background: #ecfdf5; color: #047857; font-weight: 800; margin-top: 10px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 14px 0; }}
    td, th {{ border: 1px solid #e2e8f0; padding: 9px; vertical-align: top; }}
    th {{ background: #f8fafc; text-align: left; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
    .box {{ border: 1px solid #e2e8f0; border-radius: 12px; padding: 14px; background: #f8fafc; }}
    .note {{ color: #475569; font-size: 13px; margin-top: 16px; }}
    h1, h2, h3 {{ margin-bottom: 8px; }}
</style>
</head>
<body>
<div class="cert">
    <div class="header">
        <div class="brand-row">
            {CITEINTEGRITY_LOGO_SVG}
            <div class="brand">CiteIntegrity</div>
        </div>
        <div class="title">Citation Integrity Certificate</div>
        <div class="subtitle">Automated and manually recorded citation integrity review</div>
        <div class="timestamp">Generated: {_html(c.get("generated_at_display") or c.get("generated_at") or "")}</div>
        <div class="status">{_html(c.get("clearance_status", ""))}</div>
    </div>

    <div class="grid">
        <div class="box">
            <h3>Certificate Details</h3>
            <table>
                {row("Certificate ID", c.get("certificate_id", ""))}
                {row("Timestamp", c.get("generated_at_display") or c.get("generated_at", ""))}
                {row("Package", c.get("package", ""))}
                {row("Analysis run", c.get("analysis_run", ""))}
                {row("Total in-text citations", c.get("total_in_text_citations", 0))}
                {row("Total references", c.get("total_references", 0))}
                {row("ACII score", c.get("acii_score", "Not available"))}
                {row("ACII rating", c.get("acii_rating", ""))}
            </table>
        </div>
        <div class="box">
            <h3>Risk Summary</h3>
            <table>
                {row("Critical risks", r.get("critical", 0))}
                {row("Moderate risks", r.get("moderate", 0))}
                {row("Minor risks", r.get("minor", 0))}
            </table>
        </div>
    </div>

    <h3>Required Certificate Metrics</h3>
    <table>
        <tr><th>Metric</th><th>Value</th></tr>
        {metrics_html}
    </table>

    <h3>Required Corrections</h3>
    <ol>{requirements_html}</ol>

    <div class="note"><strong>Coverage note:</strong> {_html(c.get("coverage_note", ""))}</div>
    <div class="note"><strong>Validity note:</strong> {_html(c.get("validity_note", ""))}</div>
</div>
</body>
</html>"""
