"""2.0.6 regression tests. Provider responses here are simulated, not live proof.

Publication events are checked against the bundled official RW snapshot.
Benchmark data is used ONLY in tests, never by production matching code.
"""
import io
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from docx import Document

import verify
import publication_integrity as pi
from correction_plan import build_correction_plan, _parse_original_reference, _reference_audit
from document_correction_pack import build_tracked_changes_document
from entitlements import apply_entitlements_to_result
from reference_formatter import parse_authors, format_reference
from reference_safety import notice_kind, plain_metadata
from source_risk import assess_source_risks, publication_summary
from acii import compute_acii
from test_integrity_corrective_release import BENCHMARK_REFERENCES


REFERENCES = BENCHMARK_REFERENCES.splitlines()
EXPECTED = ["retracted"] * 5 + ["expression_of_concern", "corrected", "reinstated", "publication_notice"] + ["clear"] * 11


def fixture_candidate(reference):
    """Independent reference-format parser generates a controlled search response."""
    parsed = _parse_original_reference(reference)
    authors = parse_authors(parsed["authors"])
    return {"source": "crossref", "item": {
        "DOI": pi.extract_doi(reference), "title": [parsed["title"]],
        "issued": {"date-parts": [[int(parsed["year"])]]},
        "author": [{"family": author.split(",")[0]} for author in authors],
        "container-title": [parsed.get("source") or parsed.get("publisher") or ""],
        "volume": parsed.get("volume", ""), "page": parsed.get("pages", ""),
        "type": "journal-article",
    }}


@pytest.mark.parametrize("with_doi", [True, False])
def test_benchmark_matching_through_real_verifier_with_simulated_provider(with_doi):
    rows = []
    for reference in REFERENCES:
        candidate = fixture_candidate(reference)
        submitted = reference if with_doi else re.sub(r"\s+doi:.*", "", reference)
        with patch.object(verify, "_query_crossref_by_doi", return_value=[candidate]) as doi_query, \
             patch.object(verify, "_query_crossref_bibliographic", return_value=[candidate]) as title_query, \
             patch.object(verify, "_query_openalex_search", return_value=[]) as fallback:
            row = verify._verify_single_reference(submitted, "numeric_superscript", True, True)
        assert row["status"] == "verified", (submitted, row)
        assert row["doi"] == pi.extract_doi(reference)
        assert doi_query.call_count + title_query.call_count == 1
        assert fallback.call_count == 0
        rows.append(row)
    assert [r["publication_status"] for r in rows] == EXPECTED
    assert rows[4]["publication_event_count"] == 3
    assert not rows[7]["is_retracted"]
    result = {"summary": {"reference_entries_found": 20}, "online_verification": {"rows": rows}}
    result["source_risk_review"] = assess_source_risks(result)
    summary = result["source_risk_review"]["publication_summary"]
    assert summary["retracted_or_withdrawn"] == 5
    assert summary["expression_of_concern"] == 1
    assert summary["corrected"] == summary["reinstated"] == summary["publication_notice"] == 1
    assert summary["no_recorded_event"] == 11
    assert summary["checked"] == 20 and summary["unchecked"] == 0
    score = compute_acii(result, rows)
    assert score["score_available"] and score["category"] == "Critical Review"
    assert score["ACII"] <= 49


def test_notice_never_substituted_for_original_article():
    reference = REFERENCES[7]
    article = fixture_candidate(reference)
    notice = fixture_candidate(reference)
    notice["item"].update({"DOI": "10.1001/jamainternmed.2025.2160", "title": ["Notice of Retraction and Replacement. " + article["item"]["title"][0]]})
    submitted = re.sub(r"\s+doi:.*", "", reference)
    with patch.object(verify, "_query_crossref_bibliographic", return_value=[notice, article]), \
         patch.object(verify, "_query_openalex_search", return_value=[]):
        row = verify._verify_single_reference(submitted, "numeric_superscript", True, True)
    assert row["doi"] == pi.extract_doi(reference)
    assert row["publication_status"] == "reinstated"
    assert row["work_role"] != "publication_notice"


def test_notice_only_search_does_not_verify_original():
    article = fixture_candidate(REFERENCES[7])
    article["item"].update({"DOI": "10.1001/jamainternmed.2025.2160", "title": ["Correction: " + article["item"]["title"][0]]})
    with patch.object(verify, "_query_crossref_bibliographic", return_value=[article]), \
         patch.object(verify, "_query_openalex_search", return_value=[]):
        row = verify._verify_single_reference(re.sub(r"\s+doi:.*", "", REFERENCES[7]), "numeric_superscript", True, True)
    assert row["status"] != "verified"
    assert row["publication_status"] == "unchecked"


def test_input_doi_safety_survives_metadata_outage():
    def outage(*args, **kwargs):
        verify._record_api_request_diagnostic("timeout", "https://api.crossref.org/works")
        return []
    with patch.object(verify, "_query_crossref_by_doi", side_effect=outage), \
         patch.object(verify, "_query_crossref_bibliographic", side_effect=outage), \
         patch.object(verify, "_query_openalex_by_doi", return_value=[]), \
         patch.object(verify, "_query_openalex_search", return_value=[]):
        row = verify._verify_single_reference(REFERENCES[0], "numeric_superscript", True, True)
    assert row["status"] == "offline"
    assert row["publication_status_checked"] and row["publication_status"] == "retracted"


def test_no_doi_outage_is_not_reported_as_absent_publication():
    def outage(*args, **kwargs):
        verify._record_api_request_diagnostic("rate_limited", "https://api.crossref.org/works", 429)
        return []
    with patch.object(verify, "_query_crossref_bibliographic", side_effect=outage) as query:
        row = verify._verify_single_reference(re.sub(r"\s+doi:.*", "", REFERENCES[0]), "numeric_superscript", True, False)
    assert row["status"] == "offline" and row["publication_status"] == "unchecked"
    assert query.call_count == 1


def test_false_doi_candidate_cannot_poison_input_status():
    fake = {"type": "retraction", "doi": "10.5555/notice", "date": "2026-09-17"}
    row = pi.enrich_verification_row({"status": "not_found", "doi": "10.5555/wrong", "candidate_is_publication_notice": True, "candidate_publication_events": [fake]}, REFERENCES[7])
    assert row["publication_status"] == "reinstated"
    assert row["work_role"] == "research_work"


def test_reinstatement_with_correction_history_has_correct_current_risk():
    row = {"status": "verified", "reference": "Example", "publication_status_checked": True,
           "publication_status": "reinstated", "is_corrected": True, "is_reinstated": True}
    result = {"online_verification": {"rows": [row]}}
    risks = assess_source_risks(result)
    assert [r["risk"] for r in risks["risks"]] == ["reinstated_publication"]
    assert risks["publication_summary"]["reinstated"] == 1
    assert risks["publication_summary"]["corrected"] == 0


def test_summary_never_counts_concerns_as_retractions_and_counts_missing_rows():
    result = {"summary": {"reference_entries_found": 5}, "online_verification": {"rows": [
        {"publication_status_checked": True, "publication_status": "retracted", "publication_event_count": 3},
        {"publication_status_checked": True, "publication_status": "expression_of_concern"},
        {"publication_status_checked": False, "publication_status": "unchecked"}]}}
    summary = publication_summary(result)
    assert summary["retracted_or_withdrawn"] == summary["expression_of_concern"] == 1
    assert summary["unchecked"] == 3 and not summary["complete"]


def test_free_preview_preserves_full_safety_counts_before_sampling():
    rows = [pi.enrich_verification_row({"status": "verified", "doi": pi.extract_doi(r)}, r) for r in REFERENCES]
    result = {"summary": {"reference_entries_found": 20}, "online_verification": {"rows": rows}}
    result["source_risk_review"] = assess_source_risks(result)
    free = apply_entitlements_to_result(result, False)
    assert len(free["online_verification"]["rows"]) == 5
    assert free["source_risk_review"]["publication_summary"]["checked"] == 20
    assert free["source_risk_review"]["publication_summary"]["retracted_or_withdrawn"] == 5
    assert len(free["source_risk_review"]["risks"]) == 9


def test_unchecked_status_is_not_hidden_when_bibliographic_match_is_verified():
    result = {"online_verification": {"rows": [{"status": "verified", "reference": "Author, A. (2024). A complete source title. Test Publisher.", "publication_status_checked": False}]}}
    result["source_risk_review"] = assess_source_risks(result)
    plan = build_correction_plan(result)
    assert any(i["category"] == "source_risk" and "Unchecked" in i["title"] for i in plan["items"])


def test_review_instruction_is_not_a_proposed_manuscript_edit():
    result = {"autofix": {"suggestions": {"references": [{"original": "Valid reference", "suggested": "Review reference manually", "confidence": .99, "fix_type": "review_required"}]}}}
    assert not any(i.get("proposed_replacement") == "Review reference manually" for i in build_correction_plan(result)["items"])
    doc = Document(); doc.add_paragraph("Valid reference"); buf = io.BytesIO(); doc.save(buf)
    _, manifest = build_tracked_changes_document(buf.getvalue(), {"items": [{"id": "legacy", "original_text": "Valid reference", "proposed_replacement": "Review reference manually", "decision": "accepted"}]})
    assert manifest["applied_count"] == 0 and manifest["skipped"] == ["legacy"]


def test_numbered_apa_query_preserves_title_without_publisher_and_edition():
    reference = "1. Cohen, J. (1988). Statistical power analysis for the behavioral sciences (2nd ed.). Lawrence Erlbaum Associates."
    fields = verify._extract_fields_by_style(reference, "numeric_square")
    assert fields["title"] == "Statistical power analysis for the behavioral sciences"
    parsed = _parse_original_reference(reference)
    for style in ("apa7", "numeric_square"):
        assert "2nd ed." in format_reference(parsed, style)


def test_author_formatting_preserves_hyphenated_initials_and_unicode():
    assert parse_authors("Faul, F., Erdfelder, E., Lang, A.-G., & Buchner, A.") == ["Faul, F.", "Erdfelder, E.", "Lang, A.-G.", "Buchner, A."]
    assert parse_authors("Özden D, Yılmaz İ, Sönmez S") == ["Özden, D.", "Yılmaz, İ.", "Sönmez, S."]
    assert parse_authors("PLoS One Editors") == ["PLoS One Editors"]


def test_provider_markup_is_plain_text_and_notice_detection_is_specific():
    assert plain_metadata("Vitamin K<sub>2</sub> &amp; <i>health</i>") == "Vitamin K2 & health"
    assert notice_kind("Notice of Retraction and Replacement. A trial")
    assert not notice_kind("Retraction practices in medical journals")


def test_reverse_publication_relation_is_not_event_on_notice():
    assert pi.events_from_crossref_message({"relation": {"retracts": [{"id": "10.5555/work"}]}}) == []
    assert pi.events_from_crossref_message({"relation": {"is-retracted-by": [{"id": "10.5555/notice"}]}})[0]["type"] == "retraction"


def test_same_rw_record_from_api_and_snapshot_does_not_duplicate_history():
    local = pi._retraction_watch_lookup("10.1136/bmjopen-2021-058801")
    candidates = [{**event, "doi": "10.1136/bmjopen-2021-058801", "date": event["date"] + "T00:00:00Z"} for event in local["events"]]
    result = pi.fetch_crossref_publication_status("10.1136/bmjopen-2021-058801", candidate_events=candidates)
    assert result["publication_event_count"] == 3


def test_short_manuscript_heading_is_not_ignored_by_recovery_parser():
    import engine
    text = "Short manuscript.\nReferences\n" + re.sub(r" doi:[^\n]+", "", BENCHMARK_REFERENCES)
    assert len(engine.extract_references_enhanced(text)) == 20


def test_batch_entry_point_keeps_order_and_publication_history():
    candidates = [fixture_candidate(r) for r in REFERENCES]
    submitted = [re.sub(r"\s+doi:.*", "", r) for r in REFERENCES]
    with patch.object(verify, "_query_crossref_bibliographic", return_value=candidates), \
         patch.object(verify, "_query_openalex_search", return_value=[]):
        rows = verify.verify_references_batch(submitted, style="numeric_square", use_crossref=True, use_openalex=True)
    assert len(rows) == 20
    assert [r["publication_status"] for r in rows] == EXPECTED
    assert all(r["status"] == "verified" for r in rows)


def test_acii_withheld_for_metadata_outage_even_when_input_doi_safety_known():
    row = pi.enrich_verification_row({"status": "offline"}, REFERENCES[0])
    score = compute_acii({"summary": {"reference_entries_found": 1}}, [row])
    assert score["score_available"] is False
