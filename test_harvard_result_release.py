"""Regression coverage for Harvard books/reports in saved results and exports.

The result route and its real preparation function are loaded without starting
web/queue services. Only database, cache and access lookups are simulated.
"""

import ast
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import Mock

import pytest
from docx import Document

from academic_voice import analyse_academic_voice
from citation_coach import build_citation_coach
from correction_plan import _reference_audit, build_correction_plan
from entitlements import apply_entitlements_to_result
from payment_control import open_access_payload
from privacy_lifecycle import attach_privacy_status
from reference_formatter import (
    export_references_to_docx,
    export_references_to_html,
    format_reference,
    format_verified_reference_list,
)
from source_risk import assess_source_risks
from reference_review import reference_resolution_counts
from verification_coverage import reconcile_verification_meta


ROOT = Path(__file__).resolve().parent
BOOK = "Smith, J. 2021. Research methods (2nd ed.). Example Press."
REPORT = "World Health Organization. 2024. Research guidance. World Health Organization."
ARTICLE = "Adam, A. 2024. Digital integrity. Journal of Integrity, 2(1), 1-9."


@pytest.mark.parametrize("doi", [None, "", "10.5555/example", "doi:10.5555/example", "https://doi.org/10.5555/example"])
def test_harvard_book_formatting_preserves_identity_edition_and_optional_doi(doi):
    formatted = format_reference({
        "authors": ["Smith, Jane"], "year": "2021", "title": "Research methods",
        "edition": "2nd ed.", "publisher": "Example Press", "doi": doi,
    }, "harvard")
    assert formatted.startswith("Smith, J. (2021). *Research methods* (2nd ed.). Example Press.")
    if doi:
        assert formatted.endswith("doi:10.5555/example")
        assert formatted.count("doi:") == 1
    else:
        assert "doi:" not in formatted and "None" not in formatted


def test_harvard_audit_covers_mixed_book_report_article_before_online_verification():
    result = {"selected_style": "harvard", "references_raw": [BOOK, REPORT, ARTICLE]}
    audited = _reference_audit(result)
    assert len(audited) == 3
    assert all(not row["missing"] for row in audited)
    assert "Example Press" in audited[0]["formatted"]
    assert "World Health Organization" in audited[1]["formatted"]
    assert "Journal of Integrity" in audited[2]["formatted"]
    plan = build_correction_plan(result)
    originals = {item["original_text"] for item in plan["items"] if item["category"] == "reference_style"}
    assert originals == {BOOK, REPORT, ARTICLE}


def _load_result_route(cache=None, result=None):
    """Exercise saved-result rendering through the production preparation path."""
    stored = copy.deepcopy(result)
    cursor = Mock()
    cursor.fetchone.return_value = {"status": "completed", "result": stored, "error": None}
    connection = Mock()
    connection.cursor.return_value = cursor
    connect = Mock(return_value=connection)
    namespace = {
        "Request": object, "Dict": Dict, "Any": Any, "Optional": Optional,
        "json": json, "redis_conn": cache, "DATABASE_URL": "test-database",
        "psycopg2": SimpleNamespace(connect=connect), "RealDictCursor": object,
        "_demo_prepare_full_review_result": lambda job, payload, **kwargs: payload,
        "attach_privacy_status": attach_privacy_status,
        "analyse_academic_voice": analyse_academic_voice,
        "assess_source_risks": assess_source_risks,
        "reference_resolution_counts": reference_resolution_counts,
        "reconcile_verification_meta": reconcile_verification_meta,
        "build_citation_coach": build_citation_coach,
        "build_correction_plan": build_correction_plan,
        "get_access_mode": lambda *args: {"mode": "payment_required"},
        "open_access_payload": open_access_payload,
        "_apply_developer_testing_access": lambda payload, request: payload,
        "purchase_is_paid_for_job": lambda *args, **kwargs: {"paid": False},
        "apply_entitlements_to_result": apply_entitlements_to_result,
    }
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in {"get_result", "_prepare_student_result"}]
    assert len(functions) == 2
    for node in functions:
        node.decorator_list = []
    exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), namespace)
    return namespace["get_result"], connect


@pytest.mark.parametrize("use_cache", [False, True])
def test_completed_harvard_result_loads_from_database_or_cache_on_free_tier(use_cache):
    result = {
        "selected_style": "harvard", "references_raw": [BOOK, REPORT, ARTICLE],
        "main_text": "Smith (2021) explains research methods.",
        "summary": {"reference_entries_found": 3, "in_text_citations_found": 1},
    }
    cache = Mock() if use_cache else None
    if cache is not None:
        cache.get.return_value = json.dumps(result)
    route, connect = _load_result_route(cache, result)
    response = asyncio.run(route(object(), "saved-harvard-job", fresh=int(not use_cache)))
    assert response["status"] == "completed"
    assert response["data"]["summary"]["reference_entries_found"] == 3
    assert response["data"]["correction_plan"]["items"]
    assert "evidence_resolution_workspace" in response["data"]
    assert response["data"]["payment_required"] is True
    assert connect.call_count == int(not use_cache)


def test_harvard_verified_reference_exports_include_books_and_reports():
    rows = [
        {"status": "verified", "reference": BOOK, "matched_authors_full": ["Smith, Jane"],
         "matched_year": "2021", "matched_title": "Research methods", "edition": "2nd ed.",
         "publisher": "Example Press", "doi": "10.5555/example", "type": "book"},
        {"status": "needs_review", "reference": REPORT,
         "matched_authors_full": ["World Health Organization"], "matched_year": "2024",
         "matched_title": "Research guidance", "publisher": "World Health Organization", "type": "report"},
    ]
    formatted = format_verified_reference_list(rows, "harvard")
    assert len(formatted) == 2
    html = export_references_to_html(formatted, "harvard")
    assert "Example Press" in html and "Research guidance" in html
    document = Document(export_references_to_docx(formatted, "harvard"))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "Research methods" in text and "Research guidance" in text
    assert "doi:10.5555/example" in text
