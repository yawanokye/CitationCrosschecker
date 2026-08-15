"""Convert detailed engine results into a student-friendly correction plan."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List
import re


def _rows(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("citation", "citation_in_text", "reference", "claim", "claim_text", "sentence", "passage"):
            if value.get(key):
                return str(value[key])
    return str(value or "")


def _main_text(result: Dict[str, Any]) -> str:
    return str(result.get("main_text") or result.get("full_text") or result.get("document_text") or result.get("text") or "")


def _locate(text: str, needle: str, supplied: Any = None) -> Dict[str, Any]:
    if isinstance(supplied, dict) and supplied:
        return supplied
    needle = re.sub(r"\s+", " ", str(needle or "")).strip()
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n+", text or "") if p.strip()]
    section = "Document body"
    words_before = 0
    for p_index, paragraph in enumerate(paragraphs):
        if len(paragraph.split()) <= 14 and (paragraph.isupper() or re.match(r"^(chapter|section|\d+(?:\.\d+)*)\b", paragraph, re.I)):
            section = paragraph[:160]
        if needle and (needle.lower() in paragraph.lower() or paragraph.lower() in needle.lower()):
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", paragraph) if s.strip()]
            sentence_number = next((i + 1 for i, sentence in enumerate(sentences) if needle[:80].lower() in sentence.lower() or sentence[:80].lower() in needle.lower()), 1)
            return {
                "page_estimate": words_before // 500 + 1,
                "section": section,
                "paragraph": p_index + 1,
                "sentence": sentence_number,
                "page_note": "Estimated from extracted text. Confirm against the annotated manuscript.",
            }
        words_before += len(paragraph.split())
    return {"section": section, "location_note": "Exact text location was not recovered. Use the evidence text to search the manuscript."}


def _metadata(row: Any) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    keys = ("source", "status", "support_status", "citation", "source_title", "doi", "matched_doi", "matched_title", "matched_authors", "matched_year", "matched_journal", "matched_volume", "matched_issue", "matched_pages", "url", "evidence_url", "confidence_reason", "score", "support_score")
    return {key: row.get(key) for key in keys if row.get(key) not in (None, "", [])}


def build_correction_plan(result: Dict[str, Any]) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    manuscript_text = _main_text(result)
    saved_decisions = result.get("correction_decisions") or {}
    saved_candidates = result.get("correction_source_candidates") or {}

    def add(priority: str, category: str, title: str, rows: List[Any], action: str, why: str, confidence: str = "high", limit: int = 200):
        for i, row in enumerate(rows[:limit]):
            item_id = f"{category}-{i + 1}"
            if category == "claim_support" and isinstance(row, dict):
                evidence = str(row.get("claim") or row.get("claim_text") or row.get("context") or row.get("sentence") or "")[:900]
            else:
                evidence = _text(row)[:900]
            decision = saved_decisions.get(item_id) or {}
            candidates = saved_candidates.get(item_id) or (row.get("suggestions") if isinstance(row, dict) else []) or (row.get("suggested_sources") if isinstance(row, dict) else []) or []
            proposed = decision.get("proposed_replacement") or ((row.get("proposed_replacement") or row.get("suggested_reference") or row.get("formatted_reference") or "") if isinstance(row, dict) else "")
            available_actions = {
                "missing_reference": ["find_source", "add_reference"],
                "citation_needed": ["find_source", "insert_citation"],
                "uncited_reference": ["cite_reference", "delete_reference"],
                "claim_support": ["find_source", "add_supporting_citation", "revise_claim"],
            }.get(category, ["accept", "reject", "ignore"])
            items.append({
                "id": item_id,
                "priority": priority,
                "category": category,
                "title": title,
                "what_is_wrong": title,
                "why_it_matters": why,
                "evidence": evidence,
                "location": _locate(manuscript_text, evidence, row.get("location") if isinstance(row, dict) else None),
                "recommended_action": action,
                "coach_explanation": f"{why} {action}",
                "supporting_metadata": _metadata(row),
                "confidence": (row.get("confidence") or row.get("score") or confidence) if isinstance(row, dict) else confidence,
                "evidence_link": (row.get("url") or row.get("evidence_url") or row.get("matched_url") or "") if isinstance(row, dict) else "",
                "proposed_replacement": proposed,
                "secondary_replacement": decision.get("secondary_replacement") or "",
                "original_text": decision.get("original_text") or evidence,
                "source_candidates": candidates,
                "approved_source": decision.get("approved_source") or {},
                "approved_action": decision.get("action") or "",
                "track_operation": decision.get("track_operation") or "replace",
                "available_actions": available_actions,
                "auto_apply_allowed": bool(isinstance(row, dict) and category in {"reference_metadata", "formatting"} and float(row.get("confidence", 0) or 0) >= .95),
                "decision": decision.get("decision", "pending"),
                "decision_note": decision.get("note", ""),
            })

    add("critical", "missing_reference", "In-text citation has no matching reference", _rows(result.get("missing_in_references")), "Add and verify the complete reference or correct the in-text citation.", "Readers cannot identify or verify the cited source.")
    add("important", "uncited_reference", "Reference is not cited in the manuscript", _rows(result.get("uncited_references")), "Cite the source where it supports the argument or remove it from the reference list.", "An unused reference weakens reference-list accuracy and may suggest padding or an editing oversight.")

    verification = result.get("online_verification") or {}
    verification_rows = _rows(verification.get("rows") if isinstance(verification, dict) else verification)
    risky = [r for r in verification_rows if str((r or {}).get("status", "")).lower() in {"not_found", "unverified", "failed", "needs_review", "possible"}]
    add("critical", "source_verification", "Reference requires source verification", risky, "Check the DOI, title, authors, year, journal, volume and pages against the linked evidence.", "Incorrect bibliographic metadata can prevent readers from locating the source. Not found does not automatically mean fabricated.", "medium")

    claim_rows = _rows(result.get("claim_support"))
    weak_claims = [r for r in claim_rows if str((r or {}).get("support_status", (r or {}).get("status", ""))).lower() in {"weak_or_unclear", "insufficient_evidence", "no_support", "source_needs_review"}]
    add("critical", "claim_support", "Claim has weak or unclear support", weak_claims, "Revise or qualify the claim, and confirm that the cited source directly supports it.", "A citation must support the specific claim beside it, not merely discuss a related topic.", "medium")
    add("critical", "citation_needed", "Claim may require a citation", _rows(result.get("citation_needed_claims")), "Add an appropriate source, qualify the statement, or identify it as a result of the present study.", "Unsupported factual, empirical or causal claims reduce scholarly credibility.", "medium")

    autofix = result.get("autofix") or {}
    suggestions = autofix.get("suggestions") or {}
    seen_autofixes = set()
    for kind, category in (("citations", "citation_formatting"), ("references", "reference_metadata")):
        for i, row in enumerate(_rows(suggestions.get(kind))):
            if not isinstance(row, dict) or not row.get("original") or not row.get("suggested"):
                continue
            original = re.sub(r"\s+", " ", str(row.get("original") or "")).strip()
            suggested = re.sub(r"\s+", " ", str(row.get("suggested") or "")).strip()
            confidence_value = float(row.get("confidence", 0) or 0)
            reason_lower = str(row.get("reason") or "").lower()
            speculative = any(marker in reason_lower for marker in (
                "may be a typo", "possible author-name variation", "unique reference year",
                "review before changing", "citation was not matched", "similar author",
            ))
            # Do not turn fuzzy author/year guesses into correction actions.
            # They belong in manual verification only after external metadata
            # confirms that both forms identify the same source.
            if original.lower() == suggested.lower() or confidence_value < .80 or speculative:
                continue
            dedupe_key = (category, original.lower(), suggested.lower())
            if dedupe_key in seen_autofixes:
                continue
            seen_autofixes.add(dedupe_key)
            item_id = f"{category}-{i + 1}"
            decision = saved_decisions.get(item_id) or {}
            bibliographic_safe = category == "reference_metadata" and confidence_value >= .95 and row.get("fix_type") not in {"review_required", "source_replacement", "new_citation"}
            items.append({
                "id": item_id,
                "priority": "important" if confidence_value >= .85 else "optional",
                "category": category,
                "title": "High-confidence bibliographic correction" if bibliographic_safe else "Formatting correction requires review",
                "what_is_wrong": row.get("reason") or row.get("issue_type") or "The citation or reference differs from the recommended form.",
                "why_it_matters": "Accurate and consistent bibliographic details help readers retrieve the intended source.",
                "evidence": original[:900],
                "location": _locate(manuscript_text, original),
                "recommended_action": f"Replace with: {row.get('suggested')}",
                "coach_explanation": "Compare the original and suggested forms. Accept only when they refer to the same source and preserve the author's intended citation.",
                "supporting_metadata": _metadata(row),
                "confidence": confidence_value,
                "evidence_link": row.get("url") or "",
                "proposed_replacement": suggested,
                "secondary_replacement": decision.get("secondary_replacement") or "",
                "original_text": original,
                "approved_action": decision.get("action") or "",
                "track_operation": decision.get("track_operation") or "replace",
                "auto_apply_allowed": bibliographic_safe,
                "decision": decision.get("decision", "pending"),
                "decision_note": decision.get("note", ""),
            })

    source_risks = (result.get("source_risk_review") or {}).get("risks") or []
    for row in source_risks:
        item_id = f"source-risk-{len(items) + 1}"
        decision = saved_decisions.get(item_id) or {}
        items.append({
            "id": item_id,
            "priority": row.get("priority", "important"),
            "category": "source_risk",
            "title": str(row.get("risk", "Source requires review")).replace("_", " ").title(),
            "what_is_wrong": str(row.get("risk", "Source requires review")).replace("_", " ").title(),
            "why_it_matters": row.get("qualification") or "This metadata signal may affect the reliability or suitability of the cited source.",
            "evidence": str(row.get("reference") or "")[:900],
            "location": _locate(manuscript_text, str(row.get("reference") or "")),
            "recommended_action": row.get("action", "Review the source metadata manually."),
            "coach_explanation": row.get("action", "Review the source metadata manually."),
            "supporting_metadata": row.get("metadata") or {},
            "confidence": row.get("confidence", "medium"),
            "evidence_link": row.get("url", ""),
            "auto_apply_allowed": False,
            "decision": decision.get("decision", "pending"),
            "decision_note": decision.get("note", ""),
        })

    voice = result.get("academic_voice_review") or {}
    voice_items_added = 0
    for voice_index, row in enumerate(_rows(voice.get("signals") if isinstance(voice, dict) else [])):
        # Citation-support questions belong to the dedicated citation-needed and
        # claim-support checks. Repeating low-confidence voice heuristics in the
        # correction plan creates duplicate, high-volume false positives.
        if row.get("signal") in {"claim_without_nearby_citation", "possible_claim_needing_source_review"}:
            continue
        if voice_items_added >= 12:
            break
        item_id = f"voice-{voice_index + 1}"
        decision = saved_decisions.get(item_id) or {}
        items.append({
            "id": item_id,
            "priority": "optional",
            "category": "academic_voice",
            "title": row.get("signal", "Writing pattern requires review").replace("_", " ").title(),
            "what_is_wrong": row.get("why_flagged", "The passage contains a writing pattern that needs review."),
            "why_it_matters": "Clear, specific and well-supported prose helps readers evaluate the author's own argument.",
            "evidence": row.get("passage", "")[:900],
            "location": row.get("location"),
            "recommended_action": row.get("recommended_action", "Review the passage for clarity and accurate support."),
            "coach_explanation": row.get("recommended_action", "Review the passage for clarity and accurate support."),
            "supporting_metadata": {},
            "confidence": row.get("confidence", "medium"),
            "evidence_link": "",
            "auto_apply_allowed": False,
            "decision": decision.get("decision", "pending"),
            "decision_note": decision.get("note", ""),
        })
        voice_items_added += 1

    # Every student-approved voice revision becomes a first-class correction
    # item so the existing Track Changes generator can apply it safely.
    for revision_id, revision in (result.get("academic_voice_revisions") or {}).items():
        if not isinstance(revision, dict) or not revision.get("original_text") or not revision.get("proposed_replacement"):
            continue
        items.append({
            "id": revision_id,
            "priority": "optional",
            "category": "academic_voice_revision",
            "title": "Approved academic voice revision",
            "what_is_wrong": revision.get("reason") or "The passage was selected for clarity and natural-voice revision.",
            "why_it_matters": "The student reviewed and approved this wording change.",
            "evidence": revision.get("original_text"),
            "location": _locate(manuscript_text, revision.get("original_text")),
            "recommended_action": "Replace with the approved revision using Track Changes.",
            "coach_explanation": "This revision was approved explicitly and must remain reviewable in Word.",
            "supporting_metadata": {"model": revision.get("model"), "confidence": revision.get("confidence")},
            "confidence": revision.get("confidence", "reviewed"),
            "evidence_link": "",
            "original_text": revision.get("original_text"),
            "proposed_replacement": revision.get("proposed_replacement"),
            "secondary_replacement": "",
            "track_operation": "replace",
            "auto_apply_allowed": False,
            "decision": "accepted",
            "decision_note": "Student approved this revision for insertion with Track Changes.",
        })

    coach = result.get("citation_improvement_coach") or {}
    for row in _rows(coach.get("lessons") if isinstance(coach, dict) else []):
        item_id = f"coach-{len(items) + 1}"
        decision = saved_decisions.get(item_id) or {}
        items.append({
            "id":item_id, "priority":row.get("priority","important"), "category":"citation_coach",
            "title":str(row.get("pattern","Citation practice issue")).replace("_"," ").title(),
            "what_is_wrong":row.get("explanation"), "why_it_matters":row.get("explanation"),
            "evidence":row.get("passage","")[:900], "location":row.get("location"),
            "recommended_action":row.get("recommended_action"), "coach_explanation":row.get("explanation"),
            "supporting_metadata":{}, "confidence":"medium", "evidence_link":"", "auto_apply_allowed":False,
            "decision":decision.get("decision","pending"), "decision_note":decision.get("note","")
        })

    rank = {"critical": 0, "important": 1, "optional": 2}
    items.sort(key=lambda item: (rank.get(item["priority"], 9), item["category"]))
    pending_items = [item for item in items if item.get("decision") not in {"accepted", "rejected", "ignored", "resolved"}]
    counts = Counter(item["priority"] for item in pending_items)
    status = "ready" if not counts["critical"] and counts["important"] <= 2 else "not_ready"
    return {
        "readiness": status,
        "headline": (
            "No critical citation-integrity issues were identified. Complete the remaining review items before submission."
            if status == "ready" else
            f"Complete {counts['critical']} critical and {counts['important']} important corrections before submission."
        ),
        "counts": {"critical": counts["critical"], "important": counts["important"], "optional": counts["optional"], "total": len(pending_items), "all_items": len(items), "decided": len(items) - len(pending_items)},
        "items": items,
        "human_review_required": True,
    }


def compare_revision_results(original: Dict[str, Any], revised: Dict[str, Any]) -> Dict[str, Any]:
    original_plan = build_correction_plan(original)
    revised_plan = build_correction_plan(revised)
    before = original_plan["counts"]
    after = revised_plan["counts"]
    original_acii = (original.get("acii") or {}).get("ACII", (original.get("acii") or {}).get("score"))
    revised_acii = (revised.get("acii") or {}).get("ACII", (revised.get("acii") or {}).get("score"))
    def metrics(data: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, int]:
        verification = data.get("online_verification") or {}
        verification_rows = _rows(verification.get("rows") if isinstance(verification, dict) else verification)
        claim_rows = _rows(data.get("claim_support"))
        return {
            "missing_references": len(_rows(data.get("missing_in_references"))),
            "uncited_references": len(_rows(data.get("uncited_references"))),
            "verified_sources": sum(str((row or {}).get("status", "")).lower() in {"verified", "matched", "valid"} for row in verification_rows),
            "unsupported_claims": sum(str((row or {}).get("support_status", (row or {}).get("status", ""))).lower() in {"weak_or_unclear", "insufficient_evidence", "no_support", "source_needs_review"} for row in claim_rows),
            "formatting_issues": sum(item.get("category") in {"citation_formatting", "reference_metadata"} and item.get("decision") not in {"resolved", "accepted"} for item in plan.get("items") or []),
            "remaining_submission_risks": plan.get("counts", {}).get("critical", 0) + plan.get("counts", {}).get("important", 0),
        }
    before_metrics = metrics(original, original_plan)
    after_metrics = metrics(revised, revised_plan)
    return {
        "journey": {"run_1": "Diagnose the manuscript", "correction_period_days": 90, "run_2": "Verify the revised manuscript", "final_output": "Before-and-after submission-readiness report"},
        "before": {"corrections": before, "acii": original_acii, **before_metrics},
        "after": {"corrections": after, "acii": revised_acii, **after_metrics},
        "change": {
            "critical_resolved": max(0, before["critical"] - after["critical"]),
            "important_resolved": max(0, before["important"] - after["important"]),
            "total_resolved": max(0, before["total"] - after["total"]),
            "acii_change": (revised_acii - original_acii) if isinstance(original_acii, (int, float)) and isinstance(revised_acii, (int, float)) else None,
            "missing_references_resolved": max(0, before_metrics["missing_references"] - after_metrics["missing_references"]),
            "uncited_references_resolved": max(0, before_metrics["uncited_references"] - after_metrics["uncited_references"]),
            "verified_sources_increased": after_metrics["verified_sources"] - before_metrics["verified_sources"],
            "unsupported_claims_resolved": max(0, before_metrics["unsupported_claims"] - after_metrics["unsupported_claims"]),
            "formatting_errors_resolved": max(0, before_metrics["formatting_issues"] - after_metrics["formatting_issues"]),
        },
        "readiness": revised_plan["readiness"],
        "remaining_plan": revised_plan,
    }
