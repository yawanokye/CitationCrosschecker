"""
entitlements.py
CiteIntegrity entitlement, pricing, and currency rules.

Commercial launch model:
- Free Preview requires no account.
- Paid review requires email only, no password.
- Every paid package is a Full Integrity Review.
- Each paid purchase includes 2 analysis runs.
- Ghana is priced in GHS, Nigeria in NGN, and other countries in USD.
- Country, payment provider, currency, and price are resolved server-side.
- Prices are fixed by market, not live-converted at checkout.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_CURRENCY = "GHS"
SUPPORTED_CURRENCIES = {"GHS", "NGN", "USD"}
ANALYSES_PER_PURCHASE = 2
PURCHASE_VALIDITY_DAYS = 14

DOCUMENT_TIERS: Dict[str, Dict[str, Any]] = {
    "article": {
        "name": "Article Full Review",
        "description": "For journal articles, essays, assignments, and short manuscripts.",
        "max_words": 15000,
        "max_references": 100,
        "max_citations": 300,
        "prices": {"GHS": 10.00, "NGN": 1500.00, "USD": 2.99},
        "analysis_runs": ANALYSES_PER_PURCHASE,
        "advanced_enrichment_cap": 30,
        "display_order": 1,
    },
    "research_paper": {
        "name": "Research Paper Full Review",
        "description": "For long papers, research proposals, dissertation chapters, and working papers.",
        "max_words": 35000,
        "max_references": 200,
        "max_citations": 700,
        "prices": {"GHS": 20.00, "NGN": 2500.00, "USD": 4.99},
        "analysis_runs": ANALYSES_PER_PURCHASE,
        "advanced_enrichment_cap": 80,
        "display_order": 2,
    },
    "thesis": {
        "name": "Thesis Full Review",
        "description": "For master's theses, dissertations, and substantial research reports.",
        "max_words": 75000,
        "max_references": 350,
        "max_citations": 1400,
        "prices": {"GHS": 35.00, "NGN": 4000.00, "USD": 7.99},
        "analysis_runs": ANALYSES_PER_PURCHASE,
        "advanced_enrichment_cap": 200,
        "display_order": 3,
    },
    "phd": {
        "name": "PhD / Large Document Full Review",
        "description": "For PhD theses, books, large reports, and very large manuscripts.",
        "max_words": 120000,
        "max_references": 600,
        "max_citations": 2400,
        "prices": {"GHS": 50.00, "NGN": 6000.00, "USD": 12.99},
        "analysis_runs": ANALYSES_PER_PURCHASE,
        "advanced_enrichment_cap": 400,
        "display_order": 4,
    },
}

FREE_PREVIEW_FEATURES: Dict[str, Any] = {
    "name": "Free Preview",
    "is_paid": False,
    "preview_fraction": 0.25,
    "preview_max_rows": 10,
    "show_summary": True,
    "show_acii": "preview",
    "show_missing": False,
    "show_uncited": False,
    "show_c2r": "limited",
    "show_r2c": "limited",
    "show_verification": "limited",
    "show_recovery": "limited",
    "show_claim_support": "limited",
    "show_citation_needed": "limited",
    "show_advanced_enrichment": False,
    "show_certificate": False,
    "allow_export": False,
}

PAID_FULL_REVIEW_FEATURES: Dict[str, Any] = {
    "name": "Full Integrity Review",
    "is_paid": True,
    "show_summary": True,
    "show_acii": True,
    "show_missing": True,
    "show_uncited": True,
    "show_c2r": True,
    "show_r2c": True,
    "show_verification": True,
    "show_recovery": True,
    "show_claim_support": True,
    "show_citation_needed": True,
    "show_advanced_enrichment": True,
    "show_certificate": True,
    "allow_export": True,
}

def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except Exception:
        return default

def normalise_currency(currency: str = "") -> str:
    currency = str(currency or DEFAULT_CURRENCY).strip().upper()
    if currency not in SUPPORTED_CURRENCIES:
        currency = DEFAULT_CURRENCY
    return currency

def ordered_document_tiers() -> List[Tuple[str, Dict[str, Any]]]:
    return sorted(DOCUMENT_TIERS.items(), key=lambda item: item[1].get("display_order", 999))

def recommend_document_tier(reference_count: int, citation_count: int) -> str:
    reference_count = _as_int(reference_count)
    citation_count = _as_int(citation_count)
    for tier_key, tier in ordered_document_tiers():
        if reference_count <= tier["max_references"] and citation_count <= tier["max_citations"]:
            return tier_key
    return "custom_large"


def recommend_document_tier_with_words(reference_count: int, citation_count: int, word_count: int = 0) -> str:
    """Recommend from the largest measured dimension, including upload preflight words."""
    reference_count = _as_int(reference_count)
    citation_count = _as_int(citation_count)
    word_count = _as_int(word_count)
    for tier_key, tier in ordered_document_tiers():
        if (
            reference_count <= tier["max_references"]
            and citation_count <= tier["max_citations"]
            and (word_count <= 0 or word_count <= tier["max_words"])
        ):
            return tier_key
    return "custom_large"

def get_document_tier(tier_key: str) -> Optional[Dict[str, Any]]:
    tier = DOCUMENT_TIERS.get(str(tier_key or "").strip().lower())
    return deepcopy(tier) if tier else None

def get_price(tier_key: str, currency: str = DEFAULT_CURRENCY) -> Dict[str, Any]:
    tier = get_document_tier(tier_key)
    if not tier:
        raise ValueError(f"Unknown document tier: {tier_key}")
    currency = normalise_currency(currency)
    amount = float(tier["prices"][currency])
    return {"amount": amount, "currency": currency, "display": f"{currency} {amount:,.2f}"}

def get_package(tier_key: str, paid: bool = True, currency: str = DEFAULT_CURRENCY) -> Dict[str, Any]:
    tier_key = str(tier_key or "").strip().lower()
    currency = normalise_currency(currency)
    if not paid:
        features = deepcopy(FREE_PREVIEW_FEATURES)
        features.update({"package_key": "free_preview", "document_tier": tier_key or None, "amount": 0.00, "currency": currency, "analysis_runs": 1, "validity_days": 0})
        return features
    tier = get_document_tier(tier_key)
    if not tier:
        raise ValueError(f"Unknown document tier: {tier_key}")
    price = get_price(tier_key, currency)
    package = deepcopy(PAID_FULL_REVIEW_FEATURES)
    package.update({
        "package_key": f"{tier_key}_full_review",
        "document_tier": tier_key,
        "document_tier_name": tier["name"],
        "description": tier["description"],
        "max_words": tier["max_words"],
        "max_references": tier["max_references"],
        "max_citations": tier["max_citations"],
        "amount": price["amount"],
        "currency": price["currency"],
        "price_display": price["display"],
        "prices": deepcopy(tier["prices"]),
        "analysis_runs": tier.get("analysis_runs", ANALYSES_PER_PURCHASE),
        "validity_days": PURCHASE_VALIDITY_DAYS,
        "advanced_enrichment_cap": tier.get("advanced_enrichment_cap", 0),
    })
    return package

def validate_paid_package_for_document(tier_key: str, reference_count: int, citation_count: int, word_count: int = 0) -> Dict[str, Any]:
    tier_key = str(tier_key or "").strip().lower()
    tier = get_document_tier(tier_key)
    if not tier:
        return {"allowed": False, "reason": "unknown_tier", "message": "The selected package is not recognised.", "recommended_tier": recommend_document_tier_with_words(reference_count, citation_count, word_count)}
    reference_count = _as_int(reference_count)
    citation_count = _as_int(citation_count)
    word_count = _as_int(word_count)
    if reference_count <= tier["max_references"] and citation_count <= tier["max_citations"] and (word_count <= 0 or word_count <= tier["max_words"]):
        return {"allowed": True, "reason": "within_limits", "message": "The selected package can process this document.", "selected_tier": tier_key, "recommended_tier": recommend_document_tier_with_words(reference_count, citation_count, word_count)}
    recommended = recommend_document_tier_with_words(reference_count, citation_count, word_count)
    return {
        "allowed": False,
        "reason": "document_exceeds_selected_tier",
        "message": f"This document has {word_count or 'an unmeasured number of'} words, {reference_count} references and {citation_count} in-text citations. It exceeds the {tier['name']} limit of {tier['max_words']} words, {tier['max_references']} references or {tier['max_citations']} in-text citations.",
        "selected_tier": tier_key,
        "recommended_tier": recommended,
        "selected_limits": {"max_words": tier["max_words"], "max_references": tier["max_references"], "max_citations": tier["max_citations"]},
    }

def build_plan_selection_payload(reference_count: int, citation_count: int, selected_currency: str = DEFAULT_CURRENCY, word_count: int = 0) -> Dict[str, Any]:
    reference_count = _as_int(reference_count)
    citation_count = _as_int(citation_count)
    selected_currency = normalise_currency(selected_currency)
    word_count = _as_int(word_count)
    recommended = recommend_document_tier_with_words(reference_count, citation_count, word_count)
    tiers = []
    for key, tier in ordered_document_tiers():
        selected_price = get_price(key, selected_currency)
        tiers.append({
            "tier_key": key,
            "package_key": f"{key}_full_review",
            "name": tier["name"],
            "description": tier["description"],
            "max_words": tier["max_words"],
            "max_references": tier["max_references"],
            "max_citations": tier["max_citations"],
            "prices": deepcopy(tier["prices"]),
            "selected_currency": selected_currency,
            "selected_amount": selected_price["amount"],
            "selected_price_display": selected_price["display"],
            "analysis_runs": tier.get("analysis_runs", ANALYSES_PER_PURCHASE),
            "validity_days": PURCHASE_VALIDITY_DAYS,
            "advanced_enrichment_cap": tier.get("advanced_enrichment_cap", 0),
            "recommended": key == recommended,
            "allowed_for_document": reference_count <= tier["max_references"] and citation_count <= tier["max_citations"] and (word_count <= 0 or word_count <= tier["max_words"]),
        })
    return {
        "reference_count": reference_count,
        "citation_count": citation_count,
        "word_count": word_count,
        "recommended_tier": recommended,
        "recommended_package": None if recommended == "custom_large" else f"{recommended}_full_review",
        "selected_currency": selected_currency,
        "supported_currencies": sorted(SUPPORTED_CURRENCIES),
        "analysis_runs_per_purchase": ANALYSES_PER_PURCHASE,
        "validity_days": PURCHASE_VALIDITY_DAYS,
        "tiers": tiers,
        "message": "Every paid package includes Full Integrity Review and one recheck of the same document within 14 days.",
    }

def get_processing_flags(paid: bool = False) -> Dict[str, bool]:
    if not paid:
        return {"run_verification": True, "run_recovery": True, "run_claim_support": True, "run_citation_needed": True, "run_advanced_enrichment": False, "run_certificate": False, "allow_export": False}
    return {"run_verification": True, "run_recovery": True, "run_claim_support": True, "run_citation_needed": True, "run_advanced_enrichment": True, "run_certificate": True, "allow_export": True}

def limit_rows(rows: Any, limit: int = 5) -> Any:
    return rows[:limit] if isinstance(rows, list) else rows


def preview_rows(rows: Any, fraction: float = 0.25, max_rows: int = 10) -> Tuple[Any, Dict[str, Any]]:
    """Return a first-quarter sample capped at ``max_rows`` and explicit coverage."""
    if not isinstance(rows, list):
        return rows, {"total": 0, "shown": 0, "fraction": fraction, "max_rows": max_rows}
    total = len(rows)
    shown = min(total, max_rows, max(1, ceil(total * fraction))) if total else 0
    return rows[:shown], {
        "total": total,
        "shown": shown,
        "fraction": fraction,
        "max_rows": max_rows,
        "capped": bool(total and ceil(total * fraction) > max_rows),
    }


def preview_grouped_rows(groups: Dict[str, Any], fraction: float = 0.25, max_rows: int = 10) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Sample related row groups together so their combined preview never exceeds the cap."""
    clean_groups = {key: value if isinstance(value, list) else [] for key, value in groups.items()}
    total = sum(len(value) for value in clean_groups.values())
    target = min(total, max_rows, max(1, ceil(total * fraction))) if total else 0
    sampled = {key: [] for key in clean_groups}
    nonempty = [key for key, value in clean_groups.items() if value]

    # Give each non-empty subgroup one row where the overall preview budget permits it.
    for key in nonempty:
        if sum(len(value) for value in sampled.values()) >= target:
            break
        sampled[key].append(clean_groups[key][0])

    indexes = {key: len(sampled[key]) for key in clean_groups}
    while sum(len(value) for value in sampled.values()) < target:
        progressed = False
        for key in nonempty:
            if sum(len(value) for value in sampled.values()) >= target:
                break
            index = indexes[key]
            if index < len(clean_groups[key]):
                sampled[key].append(clean_groups[key][index])
                indexes[key] += 1
                progressed = True
        if not progressed:
            break

    shown = sum(len(value) for value in sampled.values())
    return sampled, {
        "total": total,
        "shown": shown,
        "fraction": fraction,
        "max_rows": max_rows,
        "capped": bool(total and ceil(total * fraction) > max_rows),
        "groups": {key: {"total": len(clean_groups[key]), "shown": len(sampled[key])} for key in clean_groups},
    }

def locked_payload(feature_name: str, required_plan: str = "Full Review") -> Dict[str, Any]:
    return {"locked": True, "feature": feature_name, "required_plan": required_plan, "message": f"Unlock {required_plan} to view {feature_name}."}

def _limit_online_verification(result: Dict[str, Any], fraction: float = 0.25, max_rows: int = 10) -> Dict[str, Any]:
    ov = result.get("online_verification") or {}
    if not isinstance(ov, dict):
        return {"total": 0, "shown": 0, "fraction": fraction, "max_rows": max_rows}
    rows = ov.get("rows") or []
    coverage = {"total": 0, "shown": 0, "fraction": fraction, "max_rows": max_rows}
    if isinstance(rows, list):
        sampled, coverage = preview_rows(rows, fraction, max_rows)
        ov["total_rows_available"] = coverage["total"]
        ov["rows"] = sampled
        ov["limited"] = coverage["shown"] < coverage["total"]
        ov["preview_coverage"] = coverage
        ov["limit_message"] = f"Free Preview shows {coverage['shown']} of {coverage['total']} verification rows."
    result["online_verification"] = ov
    return coverage

def apply_entitlements_to_result(result: Dict[str, Any], tier_key: str = "", paid: bool = False, currency: str = DEFAULT_CURRENCY) -> Dict[str, Any]:
    safe = deepcopy(result or {})
    summary = safe.get("summary", {}) if isinstance(safe.get("summary"), dict) else {}
    inferred_tier = tier_key or recommend_document_tier(summary.get("reference_entries_found", 0), summary.get("in_text_citations_found", 0))
    package = get_package(inferred_tier, paid=paid, currency=currency)
    safe["access"] = {"paid": bool(paid), "package": package}
    if paid:
        safe["advanced_enrichment_cap"] = package.get("advanced_enrichment_cap", 0)
        return safe
    preview_fraction = float(FREE_PREVIEW_FEATURES["preview_fraction"])
    preview_max_rows = int(FREE_PREVIEW_FEATURES["preview_max_rows"])
    coverage: Dict[str, Any] = {}
    # Reconciliation samples must not reveal individual locked missing or uncited findings.
    c2r_rows = safe.get("reconciliation_intext_to_reference")
    if isinstance(c2r_rows, list):
        safe["reconciliation_intext_to_reference"] = [
            row for row in c2r_rows
            if not isinstance(row, dict) or str(row.get("status") or "").lower() not in {"not_found", "missing", "missing_reference", "unmatched"}
        ]
    r2c_rows = safe.get("reconciliation_reference_to_intext")
    if isinstance(r2c_rows, list):
        safe["reconciliation_reference_to_intext"] = [
            row for row in r2c_rows
            if not isinstance(row, dict) or _as_int(row.get("times_cited"), len(row.get("cited_by") or [])) > 0
        ]

    for key in (
        "reconciliation_intext_to_reference", "reconciliation_reference_to_intext",
        "claim_support", "citation_needed_claims",
    ):
        safe[key], coverage[key] = preview_rows(safe.get(key), preview_fraction, preview_max_rows)

    # These findings reveal the core reconciliation outcome and remain fully payment-gated.
    safe["missing_in_references"] = locked_payload("Missing Citations")
    safe["uncited_references"] = locked_payload("Uncited References")
    coverage["missing_in_references"] = {"locked": True, "total": None, "shown": 0}
    coverage["uncited_references"] = {"locked": True, "total": None, "shown": 0}
    coverage["online_verification"] = _limit_online_verification(safe, preview_fraction, preview_max_rows)

    recovery = safe.get("recovery") if isinstance(safe.get("recovery"), dict) else {}
    if recovery and not recovery.get("locked"):
        sampled_recovery, recovery_coverage = preview_grouped_rows({
            "verification_recovery": recovery.get("verification_recovery"),
        }, preview_fraction, preview_max_rows)
        recovery.update(sampled_recovery)
        recovery["missing_recovery"] = locked_payload("Missing Citation Recovery")
        coverage["recovery"] = recovery_coverage
        coverage["recovery_missing"] = {"locked": True, "total": None, "shown": 0}
        coverage["recovery_verification"] = recovery_coverage["groups"]["verification_recovery"]
        recovery["preview_read_only"] = True
        safe["recovery"] = recovery
    else:
        coverage["recovery"] = {"total": 0, "shown": 0, "fraction": preview_fraction, "max_rows": preview_max_rows}

    plan = safe.get("correction_plan") if isinstance(safe.get("correction_plan"), dict) else {}
    if plan and not plan.get("locked"):
        sensitive_categories = {"missing_reference", "missing_citation", "uncited_reference", "uncited_references"}
        eligible_items = [
            item for item in (plan.get("items") or [])
            if isinstance(item, dict) and str(item.get("category") or "").lower() not in sensitive_categories
        ]
        plan["items"], coverage["correction_plan"] = preview_rows(eligible_items, preview_fraction, preview_max_rows)
        pending = sum(1 for item in eligible_items if str(item.get("decision") or "pending").lower() == "pending")
        plan["counts"] = {"total": len(eligible_items), "pending": pending}
        original_workspace = plan.get("evidence_resolution_workspace") if isinstance(plan.get("evidence_resolution_workspace"), dict) else {}
        plan["evidence_resolution_workspace"] = {
            "headline": original_workspace.get("headline") or plan.get("headline") or "Review the available sample findings.",
            "counts": {"total": len(eligible_items), "pending": pending},
            "groups": [],
            "item_ids": [item.get("id") for item in plan["items"] if item.get("id")],
            "workflow": original_workspace.get("workflow") or [],
        }
        plan["preview_read_only"] = True
        safe["correction_plan"] = plan
        safe["evidence_resolution_workspace"] = plan["evidence_resolution_workspace"]
    else:
        safe["correction_plan"] = locked_payload("Submission-Ready Correction Plan")
        safe["evidence_resolution_workspace"] = locked_payload("Evidence Resolution Workspace")
        coverage["correction_plan"] = {"total": 0, "shown": 0, "fraction": preview_fraction, "max_rows": preview_max_rows}

    voice = safe.get("academic_voice_review") if isinstance(safe.get("academic_voice_review"), dict) else {}
    if voice and not voice.get("locked"):
        voice["signals"], coverage["academic_voice"] = preview_rows(voice.get("signals"), preview_fraction, preview_max_rows)
        voice["preview_read_only"] = True
        safe["academic_voice_review"] = voice
    else:
        safe["academic_voice_review"] = locked_payload("Academic Voice and Writing Signals")
        coverage["academic_voice"] = {"total": 0, "shown": 0, "fraction": preview_fraction, "max_rows": preview_max_rows}

    autofix = safe.get("autofix") if isinstance(safe.get("autofix"), dict) else {}
    autofix_suggestions = autofix.get("suggestions") if isinstance(autofix.get("suggestions"), dict) else {}
    sampled_autofix, autofix_coverage = preview_grouped_rows({
        "citations": autofix_suggestions.get("citations"),
        "references": autofix_suggestions.get("references"),
    }, preview_fraction, preview_max_rows)
    for key in ("citations", "references"):
        if key in autofix_suggestions:
            autofix_suggestions[key] = sampled_autofix[key]
            coverage[f"autofix_{key}"] = autofix_coverage["groups"][key]
    coverage["autofix"] = autofix_coverage
    if autofix_suggestions:
        autofix["suggestions"] = autofix_suggestions
        autofix["preview_read_only"] = True
        safe["autofix"] = autofix

    safe["advanced_enrichment"] = locked_payload("Advanced Enrichment")
    safe["citation_integrity_certificate"] = locked_payload("Citation Integrity Certificate")
    safe["export"] = locked_payload("Export Report")
    safe["citation_improvement_coach"] = locked_payload("Citation Improvement Coach")
    # Publication-safety warnings are never payment-gated. Commercial access
    # unlocks depth, workflow, export, and remediation, not knowledge that a
    # cited work is retracted or otherwise carries a publication event.
    source_risk = safe.get("source_risk_review") if isinstance(safe.get("source_risk_review"), dict) else {}
    source_rows = source_risk.get("risks") if isinstance(source_risk.get("risks"), list) else []
    safety_risks = {
        "retracted_or_withdrawn", "expression_of_concern", "corrected_publication",
        "reinstated_publication", "publication_notice", "publication_status_unchecked", "other_publication_update",
    }
    visible_source_rows = [
        row for row in source_rows
        if isinstance(row, dict) and str(row.get("risk") or "") in safety_risks
    ]
    source_risk["risks"] = visible_source_rows
    source_risk["counts"] = {
        "critical": sum(1 for row in visible_source_rows if row.get("priority") == "critical"),
        "important": sum(1 for row in visible_source_rows if row.get("priority") == "important"),
        "optional": sum(1 for row in visible_source_rows if row.get("priority") == "optional"),
        "total": len(visible_source_rows),
    }
    source_risk["safety_information_free"] = True
    source_risk["limited"] = True
    source_risk["limit_message"] = "Publication-status safety alerts are shown in full. Full Review unlocks all other source-risk findings, remediation, and export."
    safe["source_risk_review"] = source_risk
    safe_summary = safe.get("summary") if isinstance(safe.get("summary"), dict) else {}
    safe_summary["preview_fraction"] = preview_fraction
    safe_summary["preview_max_rows"] = preview_max_rows
    safe_summary["missing_in_references"] = None
    safe_summary["uncited_references"] = None
    safe_summary["match_rate"] = None
    safe_summary["locked_indicators"] = ["missing_in_references", "uncited_references", "match_rate"]
    safe["summary"] = safe_summary
    safe["preview_coverage"] = coverage
    safe["preview_notice"] = "Free Preview keeps Missing Citations, Uncited References, and Match Rate locked. Other available result categories show a complete 25% sample capped at 10 rows. Unlock Full Review for all findings, actions, exports, certificate, and recheck."
    return safe

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def expiry_iso(days: int = PURCHASE_VALIDITY_DAYS) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()

def purchase_has_remaining_analysis(purchase: Dict[str, Any]) -> bool:
    total = _as_int(purchase.get("analyses_total"), ANALYSES_PER_PURCHASE)
    used = _as_int(purchase.get("analyses_used"), 0)
    status = str(purchase.get("status") or "").lower()
    return status in {"paid", "active"} and used < total

def purchase_remaining_analyses(purchase: Dict[str, Any]) -> int:
    return max(_as_int(purchase.get("analyses_total"), ANALYSES_PER_PURCHASE) - _as_int(purchase.get("analyses_used"), 0), 0)

def can_use_purchase_for_document(purchase: Dict[str, Any], reference_count: int, citation_count: int, word_count: int = 0) -> Dict[str, Any]:
    if not purchase_has_remaining_analysis(purchase):
        return {"allowed": False, "reason": "no_analysis_credit_remaining", "message": "This purchase has no analysis runs remaining."}
    tier_check = validate_paid_package_for_document(purchase.get("document_tier") or "", reference_count, citation_count, word_count)
    if not tier_check.get("allowed"):
        return tier_check
    return {"allowed": True, "reason": "purchase_valid", "message": f"This purchase has {purchase_remaining_analyses(purchase)} analysis run(s) remaining.", "remaining_analyses": purchase_remaining_analyses(purchase)}

PURCHASES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS purchases (
    id SERIAL PRIMARY KEY,
    user_email TEXT NOT NULL,
    payment_type TEXT DEFAULT 'one_off',
    package_key TEXT,
    document_tier TEXT,
    review_type TEXT DEFAULT 'full',
    amount NUMERIC,
    currency TEXT DEFAULT 'GHS',
    status TEXT,
    payment_provider TEXT,
    provider_reference TEXT UNIQUE,
    access_token_hash TEXT,
    preview_job_id TEXT,
    preview_file_name TEXT,
    preview_reference_count INTEGER DEFAULT 0,
    preview_citation_count INTEGER DEFAULT 0,
    market TEXT,
    billing_country TEXT,
    analyses_total INTEGER DEFAULT 2,
    analyses_used INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    paid_at TIMESTAMPTZ,
    expires_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS purchase_runs (
    id SERIAL PRIMARY KEY,
    purchase_id INTEGER REFERENCES purchases(id) ON DELETE CASCADE,
    job_id TEXT UNIQUE,
    file_name TEXT,
    reference_count INTEGER DEFAULT 0,
    citation_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_purchases_user_email ON purchases(user_email);
CREATE INDEX IF NOT EXISTS idx_purchases_provider_reference ON purchases(provider_reference);
CREATE INDEX IF NOT EXISTS idx_purchases_access_token_hash ON purchases(access_token_hash);
CREATE INDEX IF NOT EXISTS idx_purchase_runs_purchase_id ON purchase_runs(purchase_id);
CREATE INDEX IF NOT EXISTS idx_purchase_runs_job_id ON purchase_runs(job_id);
"""
