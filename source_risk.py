"""Conservative source-risk interpretation over existing verification metadata."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List
import os
from reference_safety import plain_metadata


def assess_source_risks(result: Dict[str, Any]) -> Dict[str, Any]:
    online = result.get("online_verification") or {}
    rows = online.get("rows") if isinstance(online, dict) else online
    rows = rows if isinstance(rows, list) else []
    risks: List[Dict[str, Any]] = []
    current_year = datetime.now(timezone.utc).year
    seen_dois: Dict[str, int] = {}
    questionable = {name.strip().lower() for name in os.getenv("QUESTIONABLE_JOURNALS", "").split("|") if name.strip()}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").lower()
        title = plain_metadata(row.get("matched_title") or row.get("title") or row.get("reference") or f"Reference {index + 1}")
        publication_status = str(row.get("publication_status") or "unchecked").lower()
        publication_events = row.get("publication_events") if isinstance(row.get("publication_events"), list) else []
        event_context = {
            "reference_index": index + 1,
            "original_reference": row.get("reference") or "",
            "publication_status": publication_status,
            "events": publication_events,
            "source": row.get("publication_status_source") or "",
            "checked_at": row.get("publication_status_checked_at") or "",
            "data_version": row.get("publication_status_data_version") or "",
            "doi": row.get("publication_status_doi") or row.get("reference_doi") or row.get("doi") or "",
        }
        if status in {"not_found", "unverified", "failed"}:
            risks.append({
                "priority":"important", "risk":"metadata_not_found", "reference":title,
                "action":"Check DOI, title, authors, year and publisher manually. Not found does not mean fabricated.",
                "qualification":"This is an indexing or metadata risk, not an authorship or misconduct finding.",
                **event_context,
            })
        if row.get("is_retracted") is True or publication_status in {"retracted", "withdrawn"}:
            risks.append({"priority":"critical", "risk":"retracted_or_withdrawn", "reference":title, "action":"Open the publisher record and replace the source or explicitly justify why the retracted or withdrawn work is cited.", **event_context})
        elif row.get("expression_of_concern") is True or publication_status == "expression_of_concern":
            risks.append({"priority":"critical", "risk":"expression_of_concern", "reference":title, "action":"Review the expression of concern and do not rely on this source without explicit justification.", **event_context})
        elif publication_status == "publication_notice" or row.get("work_role") == "publication_notice":
            risks.append({"priority":"optional", "risk":"publication_notice", "reference":title, "action":"This is a publication notice. Citing the notice itself is legitimate. Confirm that it is being cited for the intended purpose.", **event_context})
        elif publication_status == "corrected" or (row.get("is_corrected") is True and publication_status != "reinstated" and row.get("is_reinstated") is not True):
            risks.append({"priority":"important", "risk":"corrected_publication", "reference":title, "action":"Open the correction and confirm that the corrected record and claims are used.", **event_context})
        elif publication_status == "reinstated" or row.get("is_reinstated") is True:
            risks.append({"priority":"optional", "risk":"reinstated_publication", "reference":title, "action":"This work has been reinstated and must not be treated as actively retracted. Review the event history for context.", **event_context})
        elif row.get("publication_status_checked") is not True:
            risks.append({"priority":"important", "risk":"publication_status_unchecked", "reference":title, "action":"Publication status could not be checked. Verify the DOI and inspect the publisher record before relying on this source.", "qualification":"Unchecked is not the same as clear.", **event_context})
        elif publication_events:
            risks.append({"priority":"optional", "risk":"other_publication_update", "reference":title, "action":"Review the recorded publication update and its event history.", **event_context})
        update_type = str(row.get("update_type") or row.get("relation_type") or "").lower()
        if publication_status not in {"corrected", "reinstated", "publication_notice"} and (update_type in {"correction", "corrigendum", "erratum", "update", "is-corrected-by", "is-superseded-by"} or row.get("is_superseded") is True):
            risks.append({"priority":"important", "risk":"corrected_or_superseded", "reference":title, "action":"Open the latest publisher record and use the corrected or current version where appropriate.", "url":row.get("url") or row.get("evidence_url") or ""})
        year = row.get("matched_year") or row.get("year")
        try:
            age = current_year - int(str(year)[:4])
        except Exception:
            age = None
        if age is not None and age > 10:
            risks.append({"priority":"optional", "risk":"older_source", "reference":title, "action":"Confirm that the source remains appropriate for the claim and add recent evidence where the topic changes rapidly."})
        source_type = str(row.get("type") or row.get("publication_type") or "").lower()
        if source_type in {"web", "webpage", "blog", "news", "social-media"}:
            risks.append({"priority":"important", "risk":"non_scholarly_source", "reference":title, "action":"Confirm that this source type is appropriate and support scholarly claims with peer-reviewed or authoritative evidence where possible."})
        ref_doi = str(row.get("reference_doi") or row.get("input_doi") or "").lower().replace("https://doi.org/", "")
        match_doi = str(row.get("doi") or row.get("matched_doi") or "").lower().replace("https://doi.org/", "")
        if ref_doi and match_doi and ref_doi != match_doi:
            risks.append({"priority":"critical", "risk":"doi_mismatch", "reference":title, "action":"The supplied DOI and matched DOI differ. Verify the bibliographic record before submission."})
        doi_key = match_doi or ref_doi
        if doi_key:
            if doi_key in seen_dois:
                risks.append({"priority":"important", "risk":"duplicate_publication_entry", "reference":title, "action":f"This DOI also appears in reference {seen_dois[doi_key]}. Merge duplicate entries and retain one accurate record."})
            else:
                seen_dois[doi_key] = index + 1
        journal = str(row.get("matched_journal") or row.get("journal") or "").strip()
        if journal and journal.lower() in questionable:
            risks.append({"priority":"critical", "risk":"journal_on_configured_review_list", "reference":title, "action":"This journal appears on the institution-configured review list. Verify its status using transparent institutional criteria before relying on it.", "qualification":"A list match is a review signal, not proof that the journal is predatory."})
        link_status = row.get("link_status") or row.get("http_status")
        if link_status and str(link_status) not in {"200", "201", "202", "204"}:
            risks.append({"priority":"optional", "risk":"reference_link_unavailable", "reference":title, "action":"Check and update the DOI or URL. Prefer a persistent DOI or official repository link."})
    counts = Counter(r["priority"] for r in risks)
    return {
        "publication_summary": publication_summary(result),
        "method":"conservative_metadata_risk_rules",
        "disclaimer":"These are review signals. They do not establish fabrication, misconduct, journal quality, or authorship.",
        "counts":{"critical":counts["critical"], "important":counts["important"], "optional":counts["optional"], "total":len(risks)},
        "risks":risks,
    }


def publication_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """Count current statuses per reference, not event records or severity."""
    online = result.get("online_verification") or {}
    rows = online.get("rows", []) if isinstance(online, dict) else online
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    expected = max(len(rows), int((result.get("summary") or {}).get("reference_entries_found") or 0))
    counts = {key: 0 for key in ("retracted_or_withdrawn", "expression_of_concern", "corrected",
                                  "reinstated", "publication_notice", "other_update", "no_recorded_event")}
    checked = 0
    versions = set()
    for row in rows:
        if row.get("publication_status_checked") is not True:
            continue
        checked += 1
        if row.get("publication_status_data_version"):
            versions.add(str(row["publication_status_data_version"]))
        status = row.get("publication_status")
        if status in {"retracted", "withdrawn"}:
            counts["retracted_or_withdrawn"] += 1
        elif status in counts:
            counts[status] += 1
        elif status == "clear" and row.get("publication_event_count"):
            counts["other_update"] += 1
        elif status == "clear":
            counts["no_recorded_event"] += 1
        else:
            checked -= 1
    return {**counts, "checked": checked, "total": expected, "unchecked": max(0, expected - checked),
            "complete": bool(expected and checked == expected), "data_versions": sorted(versions),
            "count_basis": "Current status per reference; historical events are shown separately.",
            "coverage_note": "No recorded event is not proof of safety. Retraction Watch corrections and concerns are not exhaustive."}
