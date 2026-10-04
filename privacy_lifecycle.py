"""Temporary-content lifecycle utilities for CiteIntegrity.

Transactional and entitlement metadata may be retained separately, but uploaded
bytes, extracted manuscript text, references, claims and generated files are
removed after download or expiry.
"""

from __future__ import annotations

import copy
import csv
import html
import io
import json
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable


CONTENT_TTL_SECONDS = int(os.getenv("CONTENT_TTL_SECONDS", "86400"))
DELETE_AFTER_PACKAGE_DOWNLOAD = os.getenv("DELETE_AFTER_PACKAGE_DOWNLOAD", "true").lower() in {"1", "true", "yes", "on"}

SENSITIVE_TOP_LEVEL_KEYS = {
    "main_text", "full_text", "text", "document_text", "paragraphs", "sentences",
    "references_raw", "reference_entries", "in_text_citations", "citations",
    "missing_in_references", "uncited_references", "reconciliation_intext_to_reference",
    "reconciliation_reference_to_intext", "claim_support", "citation_needed_claims",
    "recovery", "advanced_enrichment", "academic_voice_review", "correction_plan",
    "source_risk_review", "citation_improvement_coach", "correction_decisions",
    "correction_source_candidates", "correction_source_search_reports", "academic_voice_revisions",
    "document_topic_profile", "claim_fingerprints", "evidence_resolution_workspace",
    "autofix", "fixed_document", "extracted_text", "document_bytes",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def deletion_deadline(created_at: Any = None) -> str:
    base = utc_now()
    if isinstance(created_at, datetime):
        base = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=CONTENT_TTL_SECONDS)).replace(microsecond=0).isoformat()


def privacy_status(result: Dict[str, Any]) -> Dict[str, Any]:
    privacy = copy.deepcopy((result or {}).get("privacy") or {})
    privacy.setdefault("temporary_storage", True)
    privacy.setdefault("content_ttl_seconds", CONTENT_TTL_SECONDS)
    privacy.setdefault("delete_after_download", DELETE_AFTER_PACKAGE_DOWNLOAD)
    privacy.setdefault("scheduled_deletion_at", deletion_deadline())
    privacy.setdefault("content_deleted", False)
    privacy.setdefault("message", "Manuscript content is temporary. Download the complete report package, then delete immediately, or allow automatic expiry.")
    return privacy


def attach_privacy_status(result: Dict[str, Any]) -> Dict[str, Any]:
    result = result or {}
    result["privacy"] = privacy_status(result)
    return result


def purge_result_content(result: Dict[str, Any], reason: str = "user_requested") -> Dict[str, Any]:
    result = copy.deepcopy(result or {})
    privacy = privacy_status(result)
    for key in SENSITIVE_TOP_LEVEL_KEYS:
        result.pop(key, None)
    result.pop("online_verification", None)
    # Older worker builds may nest extracted content inside payload/result/data.
    # Remove sensitive keys recursively while retaining operational metadata.
    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            cleaned = {}
            for key, child in value.items():
                key_lower = str(key).lower()
                if key_lower in SENSITIVE_TOP_LEVEL_KEYS or key_lower in {
                    "raw_document", "raw_text", "source_text", "reference_list",
                    "verification_rows", "claim_rows", "recovery_rows",
                }:
                    continue
                cleaned[key] = scrub(child)
            return cleaned
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value
    result = scrub(result)
    result["result_deleted"] = True
    privacy.update({
        "content_deleted": True,
        "deleted_at": utc_now().replace(microsecond=0).isoformat(),
        "deletion_reason": reason,
        "scheduled_deletion_at": None,
        "message": "Manuscript content and detailed results have been permanently removed.",
    })
    result["privacy"] = privacy
    return result


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "citeintegrity")
    return value.strip("._")[:80] or "citeintegrity"


def build_report_package(job_id: str, result: Dict[str, Any], extra_files: Dict[str, bytes] | None = None) -> bytes:
    """Build one in-memory ZIP so no generated report needs persistent storage."""
    result = attach_privacy_status(copy.deepcopy(result or {}))
    summary = result.get("summary") or {}
    plan = result.get("correction_plan") or {}
    voice = result.get("academic_voice_review") or {}
    verification = result.get("online_verification") or {}
    manifest = {
        "job_id": job_id,
        "generated_at": utc_now().replace(microsecond=0).isoformat(),
        "privacy": result.get("privacy"),
        "summary": summary,
        "correction_plan": plan,
        "academic_voice_review": voice,
        "online_verification": verification,
        "source_risk_review": result.get("source_risk_review") or {},
        "citation_improvement_coach": result.get("citation_improvement_coach") or {},
        "missing_in_references": result.get("missing_in_references") or [],
        "uncited_references": result.get("uncited_references") or [],
        "claim_support": result.get("claim_support") or [],
        "citation_needed_claims": result.get("citation_needed_claims") or [],
        "track_changes_application": result.get("track_changes_application") or {},
    }
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["Priority", "Category", "Title", "Location", "Evidence", "Recommended action", "Decision", "Track Changes application"])
    for item in plan.get("items") or []:
        writer.writerow([
            item.get("priority", ""), item.get("category", ""), item.get("title", ""),
            json.dumps(item.get("location"), ensure_ascii=False) if item.get("location") else "",
            item.get("evidence", ""), item.get("recommended_action", ""), item.get("decision", "pending"),
            "Applied" if item.get("id") in (result.get("track_changes_application") or {}).get("applied", []) else ("Not applied" if item.get("decision") == "accepted" else "Not approved"),
        ])
    counts = plan.get("counts") or {}
    changes = result.get("track_changes_application") or {}
    change_note = "CiteIntegrity change-control note: Citations, recovered references, claim revisions, academic-voice revisions and uncited-reference actions are applied as tracked changes only after explicit approval and a successful placement check. No scholarly source or claim change was applied silently. Review the application count and any exceptions below."
    change_rows = "".join(f"<tr><td>{html.escape(str(row.get('id','')))}</td><td>{html.escape(str(row.get('reason','')))}</td></tr>" for row in changes.get("unapplied") or [])
    change_status = f"<p><strong>{changes.get('applied_count', 0)} of {changes.get('accepted_count', 0)} accepted actions applied.</strong></p>" + (f"<p class='critical'>Some approved actions could not be placed in the Word file. Review the following items before submission.</p><table><tr><th>Item</th><th>Reason</th></tr>{change_rows}</table>" if change_rows else "")
    report_html = f"""<!doctype html><html><head><meta charset="utf-8"><title>CiteIntegrity Report</title>
<style>body{{font-family:Arial,sans-serif;max-width:1000px;margin:36px auto;color:#172033;line-height:1.5}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #dbe3ed;padding:8px;text-align:left;vertical-align:top}}th{{background:#eef5f1}}.critical{{color:#b91c1c}}.important{{color:#b45309}}.change-note{{background:#fef3c7;border-left:6px solid #d97706;padding:14px}}</style></head><body>
<h1>CiteIntegrity Submission-Readiness Report</h1>
<p><strong>Job:</strong> {html.escape(job_id)}<br><strong>Generated:</strong> {html.escape(manifest['generated_at'])}</p>
<h2>Word Track Changes application</h2><div class="change-note">{html.escape(change_note)}<br><strong>This note is for the report only; it is not inserted into the manuscript.</strong></div>{change_status}
<h2>Prioritized correction plan</h2><p>{html.escape(str(plan.get('headline') or 'Human review required.'))}</p>
<p>Critical: {counts.get('critical', 0)} · Important: {counts.get('important', 0)} · Optional: {counts.get('optional', 0)}</p>
<table><thead><tr><th>Priority</th><th>Issue</th><th>Evidence</th><th>Recommended action</th></tr></thead><tbody>
{''.join(f"<tr><td class='{html.escape(str(i.get('priority','')))}'>{html.escape(str(i.get('priority','')))}</td><td>{html.escape(str(i.get('title','')))}</td><td>{html.escape(str(i.get('evidence','')))}</td><td>{html.escape(str(i.get('recommended_action','')))}</td></tr>" for i in (plan.get('items') or []))}
</tbody></table><h2>Important qualification</h2><p>A source marked not found is not automatically fabricated. All findings require human review.</p></body></html>"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("submission_readiness_report.html", report_html)
        zf.writestr("correction_plan.csv", csv_buffer.getvalue())
        for file_name, file_bytes in (extra_files or {}).items():
            if file_name and isinstance(file_bytes, (bytes, bytearray)):
                zf.writestr(_safe_name(file_name), bytes(file_bytes))
    return output.getvalue()


def redis_content_keys(job_id: str) -> Iterable[str]:
    return (f"file:{job_id}", f"original:{job_id}", f"result:{job_id}", f"fixed:{job_id}", f"document:{job_id}")
