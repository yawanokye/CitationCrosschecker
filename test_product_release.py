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
