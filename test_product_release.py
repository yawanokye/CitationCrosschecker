import os

os.environ.setdefault("CONTENT_TTL_SECONDS", "86400")

from academic_voice import analyse_academic_voice
from correction_plan import build_correction_plan, compare_revision_results
from privacy_lifecycle import build_report_package, purge_result_content
from source_risk import assess_source_risks
from document_correction_pack import build_annotated_document, build_tracked_changes_document
from payment_control import default_mode
from docx import Document
import io
import zipfile


def sample_result():
    return {
        "main_text": (
            "It is important to note that digital transformation plays a crucial role in performance. "
            "Research shows that integrated systems significantly increase transparency."
        ),
        "missing_in_references": [{"citation": "Adam, 2024"}],
        "uncited_references": [{"reference": "Boateng (2020). Example."}],
        "summary": {"in_text_citations_found": 1, "reference_entries_found": 1},
        "acii": {"score": 62},
    }


def test_voice_review_is_not_authorship_detector():
    review = analyse_academic_voice(sample_result()["main_text"])
    assert review["authorship_inference"] is False
    assert review["signals"]


def test_correction_plan_prioritises_missing_reference():
    plan = build_correction_plan(sample_result())
    assert plan["counts"]["critical"] >= 1
    assert plan["items"][0]["priority"] == "critical"


def test_purge_removes_content_but_keeps_summary():
    purged = purge_result_content(sample_result())
    assert "main_text" not in purged
    assert purged["summary"]["in_text_citations_found"] == 1
    assert purged["privacy"]["content_deleted"] is True


def test_report_package_is_zip():
    data = build_report_package("job-123", sample_result())
    assert data[:2] == b"PK"


def test_revision_comparison():
    original = sample_result()
    revised = {"summary": original["summary"], "acii": {"score": 80}}
    comparison = compare_revision_results(original, revised)
    assert comparison["change"]["total_resolved"] >= 1
    assert comparison["change"]["acii_change"] == 18


def test_not_found_source_is_qualified_not_labelled_fake():
    review = assess_source_risks({"online_verification":{"rows":[{"status":"not_found","reference":"Example"}]}})
    assert review["risks"][0]["risk"] == "metadata_not_found"
    assert "not mean fabricated" in review["risks"][0]["action"].lower()


def test_decision_removes_item_from_pending_counts():
    result = sample_result()
    result["correction_decisions"] = {"missing_reference-1":{"decision":"resolved"}}
    plan = build_correction_plan(result)
    assert plan["counts"]["decided"] >= 1


def test_annotated_and_track_changes_documents_are_generated():
    doc = Document(); doc.add_paragraph("Adam (2024) reported improved transparency.")
    buf = io.BytesIO(); doc.save(buf)
    plan = {"headline":"Review", "items":[{
        "id":"reference_metadata-1", "priority":"important", "evidence":"Adam (2024)",
        "original_text":"Adam (2024)", "proposed_replacement":"Adam and Boateng (2024)",
        "what_is_wrong":"Incomplete author list", "why_it_matters":"Retrieval", "recommended_action":"Correct it",
        "confidence":.99, "decision":"accepted", "auto_apply_allowed":True,
    }]}
    annotated = build_annotated_document(buf.getvalue(), plan)
    tracked, manifest = build_tracked_changes_document(buf.getvalue(), plan)
    assert annotated[:2] == b"PK" and tracked[:2] == b"PK"
    assert manifest["applied_count"] == 1


def test_maintenance_is_a_valid_environment_mode(monkeypatch):
    monkeypatch.setenv("GLOBAL_ACCESS_MODE", "maintenance")
    assert default_mode() == "maintenance"


def test_maintenance_gate_and_developer_bypass_are_present():
    source = open("main.py", encoding="utf-8").read()
    assert "async def maintenance_gate" in source
    assert "developer_request_is_authorized(request)" in source
    assert 'status_code=503' in source
    assert 'automatic_reopening' in source


def test_http_error_handler_preserves_basic_auth_challenge():
    source = open("main.py", encoding="utf-8").read()
    assert "headers=exc.headers" in source
    assert 'WWW-Authenticate' in source


def test_developer_full_testing_session_and_button_are_present():
    source = open("main.py", encoding="utf-8").read()
    assert "Open Full Access" in source
    assert "Open Full Review Unlocked" in source
    assert "developer_session_access_level(request)" in source
    assert "create_developer_session_token(level)" in source
    assert 'path="/"' in source
    assert 'httponly=True' in source and 'secure=True' in source


def test_developer_review_entitlement_is_request_scoped():
    source = open("main.py", encoding="utf-8").read()
    assert "DEVELOPER_ACCESS_LEVELS" in source
    assert "_apply_developer_testing_access" in source
    assert '"developer_access_level": access_level' in source
    assert 'result["payment_required"] = False' in source


def test_payment_notices_are_hidden_for_developer_and_temporary_open_access():
    source = open("templates/new_results.html", encoding="utf-8").read()
    assert "shouldHideAccessPanel" in source
    assert "developer_unlocked" in source
    assert "global_open_access" in source
    assert 'global_access_control?.mode === "open_access"' in source


def test_verification_refresh_preserves_correction_guidance_and_active_tab():
    source = open("templates/new_results.html", encoding="utf-8").read()
    assert "mergeResultPreservingCorrections" in source
    assert "restoreActiveResultTab" in source
    assert 'activeResultTab = target' in source


def test_voice_claim_heuristics_are_conservative_and_not_critical_corrections():
    voice_source = open("academic_voice.py", encoding="utf-8").read()
    plan_source = open("correction_plan.py", encoding="utf-8").read()
    assert "paragraph_has_citation" in voice_source
    assert "PRESENT_STUDY_MARKER" in voice_source
    assert "NON_PROSE_MARKERS" in voice_source
    assert '"priority": "optional"' in voice_source
    assert "possible_claim_needing_source_review" not in voice_source
    assert '"possible_claim_needing_source_review"' in plan_source
    assert "type_limits" in voice_source
    assert "voice_items_added >= 12" in plan_source


def test_academic_voice_revision_requires_approval_and_becomes_track_change():
    main_source = open("main.py", encoding="utf-8").read()
    ui_source = open("templates/new_results.html", encoding="utf-8").read()
    plan_source = open("correction_plan.py", encoding="utf-8").read()
    assert '"/api/academic-voice/{job_id}/approve"' in main_source
    assert "Approve revision for Track Changes" in ui_source
    assert '"track_operation": "replace"' in plan_source
    assert '"decision": "accepted"' in plan_source


def test_approved_citation_reference_addition_and_deletion_become_track_changes():
    doc = Document()
    doc.add_paragraph("Digital systems improve transparency.")
    doc.add_paragraph("Boateng (2020). Unused source.")
    buf = io.BytesIO(); doc.save(buf)
    plan = {"items":[
        {"id":"citation-needed-1", "decision":"accepted", "track_operation":"insert_after", "original_text":"Digital systems improve transparency.", "proposed_replacement":"(Adam, 2024)"},
        {"id":"uncited-reference-1", "decision":"accepted", "track_operation":"delete", "original_text":"Boateng (2020). Unused source."},
        {"id":"missing-reference-1", "decision":"accepted", "track_operation":"append_reference", "proposed_replacement":"Adam, A. (2024). Digital transparency. https://doi.org/10.1000/example"},
    ]}
    tracked, manifest = build_tracked_changes_document(buf.getvalue(), plan)
    assert manifest["applied_count"] == 3
    with zipfile.ZipFile(io.BytesIO(tracked)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "<w:ins" in xml and "<w:del" in xml


def test_source_approval_and_ai_status_endpoints_are_present():
    source = open("main.py", encoding="utf-8").read()
    assert '"/api/corrections/{job_id}/sources/{item_id}"' in source
    assert '"/api/ai/status"' in source
    assert "Select and open a scholarly source" in source


def test_context_source_search_handles_current_candidate_metadata_and_unverified_references():
    suggester = open("citation_suggester.py", encoding="utf-8").read()
    main_source = open("main.py", encoding="utf-8").read()
    ui_source = open("templates/new_results.html", encoding="utf-8").read()
    assert "def _safe_candidate_fields" in suggester
    assert 'isinstance(fields, dict)' in suggester
    assert '"source_verification"' in main_source
    assert "suggest_for_unverified" in main_source
    assert "_reference_citation_context" in main_source
    assert "Find and verify reference" in ui_source


def test_candidate_links_actions_and_non_prose_false_positive_filters():
    ui_source = open("templates/new_results.html", encoding="utf-8").read()
    plan_source = open("correction_plan.py", encoding="utf-8").read()
    worker_source = open("worker.py", encoding="utf-8").read()
    voice_source = open("academic_voice.py", encoding="utf-8").read()
    suggester = open("citation_suggester.py", encoding="utf-8").read()
    assert "Open candidate source" in ui_source
    assert "correction-action-status" in ui_source
    assert "Action recorded" in ui_source
    assert "Open the candidate source link before approving it" in ui_source
    assert "speculative" in plan_source and "unique reference year" in plan_source
    assert "table_or_result" in worker_source and "equation_like" in worker_source
    assert "numeric_density" in voice_source
    assert "strict_citation_identity" in suggester


def test_style_aware_references_voice_redlines_and_claim_support_actions():
    main_source = open("main.py", encoding="utf-8").read()
    ui_source = open("templates/new_results.html", encoding="utf-8").read()
    doc_source = open("document_correction_pack.py", encoding="utf-8").read()
    plan_source = open("correction_plan.py", encoding="utf-8").read()
    formatter = open("reference_formatter.py", encoding="utf-8").read()
    assert "_reference_style_for_result" in main_source
    assert "_style_aware_candidate_text" in main_source
    assert "_style_aware_candidate_citation" in main_source
    assert "insert_after_and_append_reference" in main_source
    assert "_normalised_raw_span" in doc_source
    assert "insert_after_and_append_reference" in doc_source
    assert "Revise or qualify claim" in ui_source
    assert "Find supporting source" in ui_source
    assert "Suggested academic-voice revision" in ui_source
    assert '"claim_support": ["find_source", "add_supporting_citation", "revise_claim"]' in plan_source
    assert 'return f"{formatted[0]}, & {formatted[1]}"' in formatter
