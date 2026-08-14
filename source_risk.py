"""Conservative source-risk interpretation over existing verification metadata."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Dict, List
import os


def assess_source_risks(result: Dict[str, Any]) -> Dict[str, Any]:
    online = result.get("online_verification") or {}
    rows = online.get("rows") if isinstance(online, dict) else online
    rows = rows if isinstance(rows, list) else []
    risks: List[Dict[str, Any]] = []
    current_year = datetime.utcnow().year
    seen_dois: Dict[str, int] = {}
    questionable = {name.strip().lower() for name in os.getenv("QUESTIONABLE_JOURNALS", "").split("|") if name.strip()}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").lower()
        title = row.get("matched_title") or row.get("title") or row.get("reference") or f"Reference {index + 1}"
        if row.get("is_retracted") is True or str(row.get("publication_status") or "").lower() in {"retracted", "withdrawn"}:
            risks.append({"priority":"critical", "risk":"retracted_or_withdrawn", "reference":title, "action":"Open the publisher record and replace or discuss the retracted source as academically appropriate."})
        if row.get("expression_of_concern") is True:
            risks.append({"priority":"critical", "risk":"expression_of_concern", "reference":title, "action":"Review the expression of concern and do not rely on this source without explicit justification."})
        update_type = str(row.get("update_type") or row.get("relation_type") or "").lower()
        if update_type in {"correction", "corrigendum", "erratum", "update", "is-corrected-by", "is-superseded-by"} or row.get("is_superseded") is True:
            risks.append({"priority":"important", "risk":"corrected_or_superseded", "reference":title, "action":"Open the latest publisher record and use the corrected or current version where appropriate.", "url":row.get("url") or row.get("evidence_url") or ""})
        if status in {"not_found", "unverified", "failed"}:
            risks.append({
                "priority":"important", "risk":"metadata_not_found", "reference":title,
                "action":"Check DOI, title, authors, year and publisher manually. Not found does not mean fabricated.",
                "qualification":"This is an indexing or metadata risk, not an authorship or misconduct finding.",
            })
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
        "method":"conservative_metadata_risk_rules",
        "disclaimer":"These are review signals. They do not establish fabrication, misconduct, journal quality, or authorship.",
        "counts":{"critical":counts["critical"], "important":counts["important"], "optional":counts["optional"], "total":len(risks)},
        "risks":risks,
    }
