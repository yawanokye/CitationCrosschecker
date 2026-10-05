"""Source review grouping, grounded extraction and complete tracked corrections."""
import ast
import asyncio
import io
import re
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from docx import Document
from fastapi import HTTPException

from correction_plan import build_correction_plan, _parse_original_reference
from document_correction_pack import build_tracked_changes_document
from evidence_resolution import normalise_verification_status, reference_resolution_group
from reference_review import approved_reference_citation_edits, reference_resolution_counts

ROOT = Path(__file__).resolve().parent
REFERENCE = "smith, J. (2021). Research methods. Example Press."
SOURCE = {"authors": ["Smith, Jane"], "year": "2022", "title": "Research methods",
          "publisher": "Example Press", "url": "https://example.org/research-methods",
          "formatted_reference": "Smith, J. (2022). Research methods. Example Press.",
          "opened_by_user": True, "identity_confirmed": True}
BODY = "Results follow smith (2021).\nA second claim uses (smith, 2021)."


def result_fixture():
    return {"selected_style": "apa7", "references_raw": [REFERENCE], "main_text": BODY,
            "online_verification": {"rows": [{"status": "needs_review", "reference": REFERENCE,
                "matched_title": "Research methods", "matched_authors_full": ["Smith, Jane"],
                "matched_year": "2022", "publisher": "Example Press", "matched_url": SOURCE["url"]}]}}


def word_bytes(paragraphs):
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def document_xml(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read("word/document.xml").decode()


@pytest.mark.parametrize("status,group", [
    ("needs_review", "unresolved_verification"), ("likely", "unresolved_verification"),
    ("serious_identity_conflict", "unresolved_verification"),
    ("not_found", "references_not_found"), ("no_record", "references_not_found"),
    ("offline", "verification_lookup_failed"), ("lookup_failed", "verification_lookup_failed"),
])
def test_review_classification_is_idempotent_and_distinct(status, group):
    canonical = normalise_verification_status(status)
    assert normalise_verification_status(canonical) == canonical
    assert reference_resolution_group(status) == group


def test_plan_separates_unresolved_sources_and_preserves_ids_and_all_rows():
    result = {"online_verification": {"rows": [
        {"reference": f"Source {index}", "status": status}
        for index, status in enumerate(["needs_review", "not_found", "offline", "likely"] * 60)]}}
    plan = build_correction_plan(result)
    items = [item for item in plan["items"] if item["category"] == "source_verification"]
    assert len(items) == 240
    assert items[0]["id"] == "source_verification-1"
    groups = {group["key"]: group["total"] for group in plan["evidence_resolution_workspace"]["groups"]}
    assert groups["unresolved_verification"] == 120
    assert groups["references_not_found"] == groups["verification_lookup_failed"] == 60
    counts = reference_resolution_counts(result["online_verification"]["rows"])
    assert counts == {"verified": 0, "metadata_differences": 60, "needs_review": 60, "not_found": 60, "lookup_failed": 60}


def test_extracts_manuscript_hints_separately_from_external_candidate_and_previews_citations():
    item = next(item for item in build_correction_plan(result_fixture())["items"] if item["category"] == "source_verification")
    assert item["extracted_reference"]["year"] == "2021"
    candidate = item["source_candidates"][0]
    assert candidate["year"] == "2022" and candidate["extraction_origin"] == "online_verification"
    assert candidate["identity_fit"]["status"] == "identity_requires_manual_confirmation"
    assert len(candidate["citation_edits_preview"]) == 2
    assert candidate["citation_edits_preview"][0]["proposed_replacement"] == "Smith (2022)"
    missing = result_fixture()
    missing["online_verification"]["rows"] = [{"status": "not_found", "reference": REFERENCE}]
    item = next(item for item in build_correction_plan(missing)["items"] if item["category"] == "source_verification")
    assert item["source_candidates"] == []
    assert item["extracted_reference"]["title"] == "Research methods"
    assert item["evidence_group"] == "references_not_found"


def test_numeric_and_ambiguous_author_year_citations_are_left_unchanged():
    parsed = _parse_original_reference(REFERENCE)
    assert approved_reference_citation_edits(BODY, parsed, SOURCE, "numeric_square") == []
    peers = [parsed, _parse_original_reference("Smith, J. (2021). Another work. Other Press.")]
    assert approved_reference_citation_edits(BODY, parsed, SOURCE, "apa7", peers) == []


def accepted_item():
    edits = approved_reference_citation_edits(BODY, _parse_original_reference(REFERENCE), SOURCE, "apa7")
    return {"id": "source_verification-1", "category": "source_verification", "decision": "accepted",
            "original_text": REFERENCE, "proposed_replacement": SOURCE["formatted_reference"],
            "track_operation": "replace_reference_and_citations", "related_edits": edits}


def test_reference_and_corresponding_citations_are_all_tracked_at_their_locations():
    original = word_bytes(BODY.splitlines() + ["Unrelated Jones (2021) claim.", "References", REFERENCE])
    tracked, manifest = build_tracked_changes_document(original, {"items": [accepted_item()]})
    assert manifest["applied_count"] == 1 and not manifest["skipped"]
    xml = document_xml(tracked)
    assert xml.count("<w:del ") == xml.count("<w:ins ") == 3
    assert "Smith (2022)" in xml and "Smith, 2022" in xml
    assert "Unrelated Jones (2021) claim." in xml


def test_missing_associated_anchor_rolls_back_reference_change():
    original = word_bytes([BODY.splitlines()[0], "References", REFERENCE])
    tracked, manifest = build_tracked_changes_document(original, {"items": [accepted_item()]})
    assert manifest["applied_count"] == 0
    assert "No part" in manifest["unapplied"][0]["reason"]
    assert "<w:ins " not in document_xml(tracked)
    assert REFERENCE in document_xml(tracked)


def load_decision_route(result, original):
    source = ast.parse((ROOT / "main.py").read_text())
    node = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "save_correction_decision")
    node.decorator_list = []
    saved = Mock()
    namespace = {"Request": object, "build_correction_plan": build_correction_plan,
        "_require_commercial_access": lambda *args: None, "load_job_record_fresh": lambda job: {"result": result, "file_name": "test.docx"},
        "HTTPException": HTTPException, "re": re, "datetime": datetime,
        "_parse_original_reference": _parse_original_reference,
        "approved_reference_citation_edits": approved_reference_citation_edits,
        "_reference_style_for_result": lambda payload: payload["selected_style"],
        "redis_conn": Mock(get=Mock(return_value=original)),
        "build_tracked_changes_document": build_tracked_changes_document,
        "_bump_feature_usage": lambda *args, **kwargs: None, "_manual_save_result": saved}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace["save_correction_decision"], saved


def test_source_approval_persists_complete_changes_and_blocks_unopened_candidate():
    result = result_fixture()
    original = word_bytes(BODY.splitlines() + ["References", REFERENCE])
    route, saved = load_decision_route(result, original)
    payload = {"item_id": "source_verification-1", "decision": "accepted", "action": "replace_reference",
               "approved_source": dict(SOURCE, opened_by_user=False), "proposed_replacement": SOURCE["formatted_reference"]}
    async def json_payload():
        return payload
    request = Mock(json=json_payload)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(route("job-1", request))
    assert exc.value.status_code == 400 and not saved.called
    payload["approved_source"]["opened_by_user"] = True
    response = asyncio.run(route("job-1", request))
    assert response["ok"] and saved.called
    entry = result["correction_decisions"]["source_verification-1"]
    assert entry["application_status"] == "ready_for_track_changes"
    assert entry["track_operation"] == "replace_reference_and_citations"
    assert len(entry["related_edits"]) == 2
    tracked, manifest = build_tracked_changes_document(original, response["correction_plan"])
    assert manifest["applied_count"] == 1 and document_xml(tracked).count("<w:ins ") == 3


def test_pending_source_does_not_alter_word_document():
    item = dict(accepted_item(), decision="pending")
    original = word_bytes(BODY.splitlines() + ["References", REFERENCE])
    tracked, manifest = build_tracked_changes_document(original, {"items": [item]})
    assert manifest["accepted_count"] == 0 and "<w:ins " not in document_xml(tracked)


def test_two_approved_sources_in_one_paragraph_both_receive_tracked_edits():
    body = "smith (2021) and jones (2020) support this statement."
    other = "jones, B. (2020). Another work. Example Press."
    items = []
    for reference, source in [(REFERENCE, SOURCE), (other, {**SOURCE, "authors": ["Jones, Bob"], "year": "2020", "formatted_reference": "Jones, B. (2020). Another work. Example Press."})]:
        items.append({"id": reference[:5], "category": "source_verification", "decision": "accepted",
            "original_text": reference, "proposed_replacement": source["formatted_reference"],
            "track_operation": "replace_reference_and_citations",
            "related_edits": approved_reference_citation_edits(body, _parse_original_reference(reference), source, "apa7")})
    original = word_bytes([body, "References", REFERENCE, other])
    tracked, manifest = build_tracked_changes_document(original, {"items": items})
    assert manifest["applied_count"] == 2 and not manifest["skipped"]
    assert document_xml(tracked).count("<w:ins ") == 4


def test_approved_citation_capitalisation_is_not_offered_again_as_pending():
    result = result_fixture()
    source = {**SOURCE, "year": "2021"}
    edits = approved_reference_citation_edits(BODY, _parse_original_reference(REFERENCE), source, "apa7")
    result["correction_decisions"] = {"source_verification-1": {"decision": "accepted", "approved_source": source,
        "related_edits": edits, "track_operation": "replace_reference_and_citations"}}
    assert not any(item["category"] == "citation_case" for item in build_correction_plan(result)["items"])


def test_failed_word_placement_does_not_save_source_approval():
    result = result_fixture()
    route, saved = load_decision_route(result, word_bytes(["References", REFERENCE]))
    async def payload():
        return {"item_id": "source_verification-1", "decision": "accepted", "action": "replace_reference",
                "approved_source": SOURCE, "proposed_replacement": SOURCE["formatted_reference"]}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(route("job-1", Mock(json=payload)))
    assert exc.value.status_code == 422 and not saved.called
    assert "correction_decisions" not in result


def test_free_result_keeps_full_review_not_found_and_failure_counts():
    from test_harvard_result_release import _load_result_route
    result = {"selected_style": "apa7", "summary": {"reference_entries_found": 20},
        "online_verification": {"rows": [{"reference": f"Source {i}", "status": status}
            for i, status in enumerate(["needs_review", "not_found", "offline", "likely"] * 5)]}}
    route, _ = _load_result_route(result=result)
    response = asyncio.run(route(object(), "free-job", fresh=1))
    data = response["data"]
    assert response["status"] == "completed"
    assert len(data["online_verification"]["rows"]) < 20
    assert data["reference_resolution_summary"]["not_found"] == data["reference_resolution_summary"]["lookup_failed"] == 5
    assert data["reference_resolution_summary"]["needs_review"] == 5


def test_tracked_citation_placement_accepts_normalised_word_whitespace():
    original = word_bytes([line.replace(" ", "  ") for line in BODY.splitlines()] + ["References", REFERENCE])
    tracked, manifest = build_tracked_changes_document(original, {"items": [accepted_item()]})
    assert manifest["applied_count"] == 1 and document_xml(tracked).count("<w:ins ") == 3
