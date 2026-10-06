"""Regression checks for partial workers, misleading completion and Full View."""
import ast
import asyncio
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import Mock

import pytest

from entitlements import apply_entitlements_to_result
from verification_coverage import ensure_reference_outcomes, reconcile_verification_meta, verification_coverage

ROOT = Path(__file__).resolve().parent


def references(count=118):
    return [f"Author{i}, J. (2021). Research methods {i}. Example Press." for i in range(count)]


def result_with_rows(count=118, returned=40, state="running"):
    refs = references(count)
    return {"references_raw": refs, "summary": {"reference_entries_found": count},
            "verification": {"state": state, "total": count, "progress": returned},
            "online_verification": {"rows": [
                {"reference": ref, "status": "verified" if i < 6 else "needs_review"}
                for i, ref in enumerate(refs[:returned])], "summary": {"total": returned}}}


def load_functions(file, names, namespace):
    tree = ast.parse((ROOT / file).read_text())
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), file, "exec"), namespace)
    return namespace


def test_six_matches_can_have_full_118_reference_processing_coverage():
    result = result_with_rows(returned=118, state="completed")
    count = verification_coverage(result)
    assert (count["expected"], count["processed"], count["matched"], count["pending"]) == (118, 118, 6, 0)
    assert count["complete"]
    assert reconcile_verification_meta(result)["state"] == "completed"


def test_first_chunk_is_running_and_preserves_expected_total():
    result = result_with_rows()
    meta = reconcile_verification_meta(result)
    assert meta["state"] == "running" and meta["total"] == 118
    assert meta["progress"] == 40 and meta["percentage"] == 33


def test_old_completed_partial_run_becomes_retryable_incomplete():
    result = result_with_rows(returned=6, state="completed")
    result["verification"].update(total=6, progress=6, percentage=100)
    meta = reconcile_verification_meta(result)
    assert meta["state"] == "incomplete" and meta["total"] == 118
    assert meta["progress"] == 6 and result["verification_coverage"]["pending"] == 112


def test_preview_sampling_preserves_full_counts_and_does_not_mutate_saved_rows():
    result = result_with_rows(returned=118, state="completed")
    reconcile_verification_meta(result)
    preview = apply_entitlements_to_result(result, paid=False)
    assert len(preview["online_verification"]["rows"]) == 10
    assert len(result["online_verification"]["rows"]) == 118
    assert preview["verification_coverage"]["processed"] == 118


def test_full_view_does_not_sample_verification_rows():
    result = result_with_rows(returned=118, state="completed")
    reconcile_verification_meta(result)
    full = apply_entitlements_to_result(result, tier_key="thesis", paid=True)
    assert len(full["online_verification"]["rows"]) == 118
    assert full["verification_coverage"]["processed"] == 118


def test_missing_middle_result_preserves_reference_identity_order_and_failure():
    refs = references(3)
    rows = [{"reference": refs[2], "status": "verified", "matched_title": "Third work"},
            {"reference": refs[0], "status": "not_found"}]
    outcomes = ensure_reference_outcomes(refs, rows, start_index=40)
    assert [row["reference_index"] for row in outcomes] == [41, 42, 43]
    assert [row["status"] for row in outcomes] == ["not_found", "offline", "verified"]
    assert outcomes[1]["verification_attempted"] is False
    assert "matched_title" not in outcomes[1] and outcomes[2]["matched_title"] == "Third work"
    payload = {"references_raw": refs, "online_verification": {"rows": outcomes}}
    assert verification_coverage(payload)["processed"] == 2
    assert verification_coverage(payload)["pending"] == 1


def test_duplicate_reference_occurrences_each_receive_a_row():
    refs = [references(1)[0]] * 2
    rows = [{"reference": refs[0], "status": "verified"}, {"reference": refs[0], "status": "needs_review"}]
    assert [row["status"] for row in ensure_reference_outcomes(refs, rows)] == ["verified", "needs_review"]


@pytest.mark.parametrize("input_marker,returned_marker", [("[1].", "[1]"), ("(1).", "(1)"), ("¹", "1."), ("¹²", "12.")])
def test_normalised_numeric_markers_do_not_drop_matching_results(input_marker, returned_marker):
    ref = references(1)[0]
    outcome = ensure_reference_outcomes([input_marker + " " + ref],
        [{"reference": returned_marker + " " + ref, "status": "verified"}])[0]
    assert outcome["status"] == "verified"


@pytest.mark.parametrize("rows", [[None], [{}], [{"reference": "A different source", "status": "verified"}]])
def test_invalid_or_unrelated_results_never_verify_the_input(rows):
    outcome = ensure_reference_outcomes(references(1), rows)[0]
    assert outcome["status"] == "offline" and outcome["verification_attempted"] is False


def test_partial_batch_without_identity_is_not_assigned_to_the_wrong_reference():
    outcomes = ensure_reference_outcomes(references(3), [{"status": "verified"}])
    assert all(row["status"] == "offline" for row in outcomes)


def test_api_timeout_is_a_processed_failure_not_a_missing_result_or_match():
    ref = references(1)[0]
    result = {"references_raw": [ref], "online_verification": {"rows": [{"reference": ref, "status": "offline"}]}}
    count = verification_coverage(result)
    assert count["processed"] == 1 and count["matched"] == 0
    assert count["outcomes"]["lookup_failed"] == 1


def test_saved_extraction_count_disagreement_remains_visible():
    result = result_with_rows(count=6, returned=6, state="completed")
    result["summary"]["reference_entries_found"] = 118
    meta = reconcile_verification_meta(result)
    assert meta["state"] == "incomplete" and meta["total"] == 118
    assert result["verification_coverage"]["extraction_count_mismatch"]


def loader_namespace(result):
    cursor = Mock()
    cursor.fetchone.return_value = {"status": "completed", "result": copy.deepcopy(result), "error": None}
    connection = Mock()
    connection.cursor.return_value = cursor
    return {"Optional": Optional, "Dict": Dict, "Any": Any, "json": json,
            "_lock": __import__('threading').RLock(), "_store": {}, "DATABASE_URL": "test",
            "psycopg2": SimpleNamespace(connect=Mock(return_value=connection)), "RealDictCursor": object,
            "redis_conn": None, "reconcile_verification_meta": reconcile_verification_meta}


@pytest.mark.parametrize("loader", ["load_job_record", "load_job_record_fresh"])
def test_real_job_loaders_do_not_turn_partial_rows_into_completed_runs(loader):
    ns = load_functions("main.py", {loader}, loader_namespace(result_with_rows()))
    record = ns[loader]("job-118")
    assert record["verification"]["state"] == "running"
    assert record["verification"]["total"] == 118 and record["verification"]["progress"] == 40


@pytest.mark.parametrize("returned,force,expected_queued", [(6, False, True), (118, False, False), (118, True, True)])
def test_real_verify_endpoint_requeues_incomplete_saved_runs(returned, force, expected_queued):
    result = result_with_rows(returned=returned, state="completed")
    queue = Mock()
    ns = {"Form": lambda *args: None, "load_job_record_fresh": lambda job: {"result": result},
          "reconcile_verification_meta": reconcile_verification_meta, "_style_for_job_result": lambda result: "apa",
          "verification_queue": queue, "update_verification_status": Mock(),
          "now": lambda: "2026-10-05T00:00:00Z", "uuid": __import__('uuid')}
    load_functions("main.py", {"verify_online"}, ns)
    response = asyncio.run(ns["verify_online"]("job-118", force=force))
    assert response["total_references"] == 118
    assert queue.enqueue.called is expected_queued
    assert response["started"] is expected_queued


@pytest.mark.parametrize("parallel", [False, True])
def test_real_chunk_verifier_keeps_missing_rows_in_sequential_and_parallel_modes(parallel):
    refs = references(3)
    ns = {"VERIFY_PARALLEL_MODE": parallel, "VERIFY_PARALLEL_WORKERS": 3,
          "ThreadPoolExecutor": __import__('concurrent.futures').futures.ThreadPoolExecutor,
          "as_completed": __import__('concurrent.futures').futures.as_completed,
          "ensure_reference_outcomes": ensure_reference_outcomes,
          "_make_offline_verification_row": lambda ref, error, style: {"reference": ref, "status": "offline"},
          "verify_references_batch": lambda chunk, **kwargs: [{"reference": chunk[0], "status": "verified"}],
          "_verify_single_reference_cached": lambda ref, *args: None if ref == refs[1] else {"reference": ref, "status": "verified"}}
    load_functions("worker.py", {"_verify_chunk_parallel"}, ns)
    rows = ns["_verify_chunk_parallel"](refs)
    assert len(rows) == 3
    assert rows[1]["status"] == "offline" and rows[1]["reference"] == refs[1]


@pytest.mark.parametrize("omit_middle", [False, True])
def test_real_durable_worker_keeps_all_118_outcomes_across_three_chunks(omit_middle):
    result = result_with_rows(returned=0)
    result["final_tables_ready"] = True
    result["verification_completed_at"] = "previous-run"
    result["verification"].update(final_tables_ready=True, completed_at="previous-run", error="previous-error")
    saves = []
    def save(job, payload, **kwargs):
        reconcile_verification_meta(payload)
        saves.append(copy.deepcopy(payload))
    def chunk(refs, **kwargs):
        return [{"reference": ref, "status": "verified", "publication_status_checked": True}
                for ref in refs if not (omit_middle and ref == result["references_raw"][60])]
    ns = {"time": __import__('time'), "re": re, "VERIFY_CHUNK_SIZE": 40,
          "VERIFY_PARALLEL_MODE": True, "VERIFY_PARALLEL_WORKERS": 8, "VERIFY_USE_CACHE": True,
          "_load_job_result": lambda job: copy.deepcopy(result), "_save_job_result": save,
          "_worker_style_family": lambda style: "author_year", "now_iso": lambda: "now",
          "_normalise_references_for_verification": lambda payload, **kwargs: payload["references_raw"],
          "_verify_chunk_parallel": chunk, "ensure_reference_outcomes": ensure_reference_outcomes,
          "_add_worker_style_metadata_to_rows": lambda rows, style: rows,
          "_numeric_style_diagnostic_note": lambda *args: "", "_build_recovery_payload": lambda *args: {},
          "_build_claim_support_safe": lambda *args: [], "_build_citation_needed_claims": lambda *args: [],
          "compute_acii": lambda *args: {}, "_enqueue_deep_enrichment": lambda *args, **kwargs: None,
          "_set_enrichment_meta": lambda payload, **kwargs: payload}
    load_functions("worker.py", {"process_verification", "_compute_verification_summary", "_set_verification_meta"}, ns)
    final = ns["process_verification"]("job-118")
    assert saves[0]["final_tables_ready"] is False
    assert saves[0]["verification"]["final_tables_ready"] is False
    assert saves[0]["verification"]["completed_at"] is None
    assert saves[0]["verification"]["error"] is None
    assert "verification_completed_at" not in saves[0]
    assert len(final["online_verification"]["rows"]) == 118
    assert [row["reference_index"] for row in final["online_verification"]["rows"]] == list(range(1, 119))
    assert any(row["verification"]["state"] == "running" and row["verification"]["progress"] == 40 for row in saves)
    assert final["verification"]["total"] == 118
    assert final["verification"]["state"] == ("incomplete" if omit_middle else "completed")
    assert final["verification_coverage"]["processed"] == (117 if omit_middle else 118)


def test_harvard_search_title_excludes_journal_volume_and_page_range():
    import verify
    from test_reference_extraction_release import SCREENSHOT_REFERENCES
    from correction_plan import _parse_original_reference
    for ref in SCREENSHOT_REFERENCES:
        fields = verify._extract_fields_by_style(ref, "harvard")
        assert fields["title"] == _parse_original_reference(ref)["title"]
        assert fields["year"] and fields["authors"]


def test_real_search_plan_uses_clean_harvard_title_without_changing_style():
    import verify
    from test_reference_extraction_release import SCREENSHOT_REFERENCES
    ref = SCREENSHOT_REFERENCES[2]
    plan = verify._v1523_build_numeric_query_plan(ref, "harvard")
    assert plan["title"] == "Efficient take-back legislation"
    assert plan["year"] == "2009"
    fields = verify._extract_fields_by_style(ref, "harvard")
    assert fields["style_family"] == "author_year"
