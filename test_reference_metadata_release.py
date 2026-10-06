"""Author/date safeguards using controlled provider payloads, not live claims."""
import asyncio
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException

import verify
from citation_suggester import _strict_reference_candidate_pass, _candidate_publication_metadata
from correction_plan import _parse_original_reference, _reference_identity_gate
from reference_formatter import parse_authors, format_verified_reference_list
from reference_metadata import (provider_metadata, prepare_reference_candidate, row_candidate,
                                reference_approval_error, format_candidate_reference)
from test_reference_review_release import (load_decision_route, word_bytes, document_xml,
                                          result_fixture, SOURCE, REFERENCE, BODY)
from document_correction_pack import build_tracked_changes_document
from test_verification_coverage_release import load_functions


def payload():
    return {"DOI": "10.1234/same-work", "title": ["Reference metadata and author preservation"],
            "author": [{"family": "García", "given": "Ana"}, {"family": "Smith", "given": "Jane"},
                       {"family": "Smith", "given": "James"}], "container-title": ["Example Journal"],
            "published-online": {"date-parts": [[2017, 6, 7]]},
            "published-print": {"date-parts": [[2018, 12]]}, "issued": {"date-parts": [[2017]]},
            "created": {"date-parts": [[2016]]}, "deposited": {"date-parts": [[2026]]},
            "volume": "48", "page": "1273-1296", "type": "journal-article"}


def reference(year="2018"):
    return f"García, A., Smith, J., & Smith, J. ({year}). Reference metadata and author preservation. Example Journal, 48, 1273–1296."


def candidate():
    return {"source": "crossref", "item": payload()}


def test_dates_never_use_registration_timestamps_and_names_are_not_deduplicated():
    data = provider_metadata("crossref", payload())
    assert data["year"] == "2018" and data["year_basis"] == "published-print"
    assert data["publication_years"] == ["2017", "2018"]
    assert data["author_count"] == 3 and data["authors_complete"]
    assert data["authors_display"] == ["García, A.", "Smith, J.", "Smith, J."]
    assert "created" not in data["publication_dates"] and "deposited" not in data["publication_dates"]


def test_registration_dates_cannot_be_used_as_missing_publication_year():
    data = {"created": {"date-parts": [[2020]]}, "deposited": {"date-parts": [[2026]]}}
    assert verify._crossref_year(data) == ""
    assert provider_metadata("crossref", data)["publication_years"] == []


@pytest.mark.parametrize("year", ["2017", "2018"])
def test_real_verifier_keeps_full_authors_and_recognises_both_recorded_years(year):
    with patch.object(verify, "_query_crossref_bibliographic", return_value=[candidate()]) as lookup, \
         patch.object(verify, "_query_openalex_search", return_value=[]):
        row = verify._verify_single_reference(reference(year), "apa", True, False)
    assert row["status"] == "verified" and row["year_match"] == 1
    assert row["matched_author_count"] == 3
    assert row["matched_authors_full"] == provider_metadata("crossref", payload())["authors_full"]
    assert row["matched_publication_years"] == ["2017", "2018"]
    assert lookup.call_count == 1
    original = _parse_original_reference(reference(year))
    assert _reference_identity_gate(original, row)["accepted"]
    proposed = prepare_reference_candidate(row_candidate(row), original)
    assert proposed["year"] == year and proposed["date_review_required"]
    formatted = format_candidate_reference(proposed, "apa7")
    assert len(parse_authors(_parse_original_reference(formatted)["authors"])) == 3


def test_exact_doi_alone_does_not_verify_wrong_title_or_authors():
    fields = {"doi": "10.1234/same-work", "title": "Reference metadata and author preservation",
              "authors": ["jones"], "year": "2018"}
    meta = verify._score_candidate(fields, candidate())
    assert meta["doi_match"]
    assert verify._classify_from_meta(fields, meta)[0] == "needs_review"
    fields["authors"] = ["garcia"]
    fields["title"] = "An entirely different clinical trial"
    assert verify._classify_from_meta(fields, verify._score_candidate(fields, candidate()))[0] == "needs_review"


def test_discovery_metadata_preserves_full_31_author_payload():
    item = payload()
    item["author"] = [{"family": f"Author{i}", "given": "Jane"} for i in range(31)]
    data = _candidate_publication_metadata({"source": "crossref", "item": item})
    assert len(data["authors_full"]) == 31 and data["authors_complete"]
    formatted = format_candidate_reference({**data, "authors": data["authors_full"], "title": "Long author list"}, "apa7")
    assert "Author0" in formatted and "Author30" in formatted
    # APA's style-specific display limit must not truncate stored metadata.
    assert len(data["authors_full"]) == 31


def test_openalex_truncation_is_visible_and_prevents_approval():
    data = provider_metadata("openalex", {"publication_year": 2020, "is_authors_truncated": True,
        "authorships": [{"author": {"display_name": "Jane Smith"}}]})
    assert data["authors_truncated"] and not data["authors_complete"]
    assert "complete author list" in reference_approval_error(dict(data, authors=data["authors_full"]))


def test_datacite_organisation_and_diacritics_are_preserved():
    data = provider_metadata("datacite", {"attributes": {"publicationYear": 2023, "creators": [
        {"name": "World Health Organization", "nameType": "Organizational"},
        {"familyName": "Aragón-Correa", "givenName": "Juan Alberto"}]}})
    assert data["authors_complete"]
    assert parse_authors(data["authors_full"]) == ["World Health Organization", "Aragón-Correa, J. A."]


def test_legacy_surname_csv_does_not_become_one_author_with_invented_initials():
    proposed = row_candidate({"matched_authors": "garcia, smith, brown", "matched_year": "2020"})
    assert parse_authors(proposed["authors"]) == ["Garcia", "Smith", "Brown"]
    assert not proposed["authors_complete"]
    assert reference_approval_error(proposed)


def test_incomplete_metadata_cannot_erase_manuscript_authors_in_export():
    row = {"reference": reference(), "status": "verified", "matched_title": payload()["title"][0],
           "matched_authors": "garcia", "matched_year": "2017", "matched_publication_years": ["2017", "2018"]}
    exported = format_verified_reference_list([row])[0]
    assert len(exported["authors"]) == 3 and exported["year"] == "2018"
    assert exported["metadata_warnings"]


def test_wrong_candidate_cannot_replace_manuscript_title_in_export():
    row = {"reference": REFERENCE, "status": "needs_review", "matched_title": "A different experiment",
           "matched_authors_full": ["Jones, Bob"], "matched_year": "2025"}
    exported = format_verified_reference_list([row])[0]
    assert exported["title"] == "Research methods" and exported["year"] == "2021"
    assert exported["authors"] == ["Smith, J."]


def test_proposed_author_reduction_requires_explicit_review():
    original = _parse_original_reference(reference())
    proposed = {"authors": ["García, Ana"], "title": original["title"], "year": "2018", "authors_complete": True}
    assert prepare_reference_candidate(proposed, original)["author_review_required"]
    assert "every author" in prepare_reference_candidate(proposed, original)["metadata_warnings"][0]
    assert "complete author list" in reference_approval_error(proposed, original)


def test_online_final_choice_is_explicit_and_updates_citations_as_tracked_edits():
    result = result_fixture()
    original = word_bytes(BODY.splitlines() + ["References", REFERENCE])
    route, saved = load_decision_route(result, original)
    source = dict(SOURCE, publication_years=["2021", "2022"], year_selected_by_user=True,
                  year_choice_confirmed=False, authors_complete=True)
    request_payload = {"item_id": "source_verification-1", "decision": "accepted", "action": "replace_reference",
                       "approved_source": source, "proposed_replacement": SOURCE["formatted_reference"]}
    async def json_payload():
        return request_payload
    request = Mock(json=json_payload)
    with pytest.raises(HTTPException) as error:
        asyncio.run(route("job", request))
    assert error.value.status_code == 422 and not saved.called
    source["year_choice_confirmed"] = True
    response = asyncio.run(route("job", request))
    tracked, manifest = build_tracked_changes_document(original, response["correction_plan"])
    assert manifest["applied_count"] == 1 and document_xml(tracked).count("<w:ins ") == 3
    assert "Smith (2022)" in document_xml(tracked)


@pytest.mark.parametrize("overrides,message", [
    ({"title": "Another unrelated publication"}, "different publication"),
    ({"authors_complete": False}, "complete author list"),
    ({"year_choice_confirmed": False}, "publication year"),
])
def test_server_blocks_unsafe_reference_replacements_before_save(overrides, message):
    result = result_fixture()
    route, saved = load_decision_route(result, word_bytes(BODY.splitlines() + ["References", REFERENCE]))
    data = {"item_id": "source_verification-1", "decision": "accepted", "action": "replace_reference",
            "approved_source": dict(SOURCE, **overrides), "proposed_replacement": "Smith only"}
    async def json_payload():
        return data
    with pytest.raises(HTTPException) as error:
        asyncio.run(route("job", Mock(json=json_payload)))
    assert error.value.status_code == 422 and message in error.value.detail and not saved.called


def test_similar_topic_or_different_doi_does_not_pass_strict_identity_search():
    fields = {"title": "Reference metadata and author preservation", "year": "2018", "authors": ["garcia"]}
    assert not _strict_reference_candidate_pass(fields, fields["title"], "2018", ["Jones"], "", {"author_similarity": 0})["strict_pass"]
    fields["doi"] = "10.1234/original"
    assert not _strict_reference_candidate_pass(fields, fields["title"], "2018", ["García"], "10.1234/other", {"author_similarity": 100})["strict_pass"]


def test_direct_worker_and_manual_candidates_do_not_cap_author_lists():
    item = payload()
    item["author"] = [{"family": f"Author{i}", "given": "Jane"} for i in range(15)]
    namespace = load_functions("worker.py", {"_candidate_from_crossref_item"}, {})
    assert len(namespace["_candidate_from_crossref_item"](item)["authors"]) == 15
    namespace = load_functions("main.py", {"_manual_candidate_from_source"}, {
        "Dict": dict, "Any": object, "Optional": __import__('typing').Optional,
        "_manual_norm": lambda value: value, "_manual_candidate_url": lambda *args: "https://example.org"})
    assert len(namespace["_manual_candidate_from_source"]("crossref", item, "query")["authors"]) == 15


def test_packed_and_hyphenated_initials_are_preserved():
    assert parse_authors([{"family": "Smith", "given": "J.A."}, {"family": "García", "given": "Jean-Paul"}]) == ["Smith, J. A.", "García, J.-P."]


def test_server_rebuilds_stale_reference_preview_without_losing_authors():
    original_ref = reference("2017")
    manuscript = "García et al. (2017) support the findings."
    row = {"reference": original_ref, "status": "needs_review", "matched_title": payload()["title"][0]}
    result = {"selected_style": "apa7", "references_raw": [original_ref], "main_text": manuscript,
              "online_verification": {"rows": [row]}}
    original = word_bytes([manuscript, "References", original_ref])
    route, saved = load_decision_route(result, original)
    info = _candidate_publication_metadata(candidate())
    source = {**info, "authors": info["authors_full"], "title": payload()["title"][0],
              "url": "https://example.org/same-work", "year": "2018", "year_selected_by_user": True,
              "opened_by_user": True, "identity_confirmed": True, "year_choice_confirmed": True}
    request_data = {"item_id": "source_verification-1", "decision": "accepted", "action": "replace_reference",
                    "approved_source": source, "proposed_replacement": "García (2018). Broken short preview."}
    async def json_payload():
        return request_data
    response = asyncio.run(route("job", Mock(json=json_payload)))
    recorded = result["correction_decisions"]["source_verification-1"]
    assert len(parse_authors(_parse_original_reference(recorded["proposed_replacement"])["authors"])) == 3
    tracked, manifest = build_tracked_changes_document(original, response["correction_plan"])
    assert manifest["applied_count"] == 1 and document_xml(tracked).count("<w:ins ") == 2
    assert "García et al. (2018)" in document_xml(tracked) and "Broken short preview" not in document_xml(tracked)


def test_preview_endpoint_accepts_complete_transcription_and_rebuilds_citation_preview():
    result = result_fixture()
    namespace = load_functions("main.py", {"preview_correction_source"}, {
        "Request": object, "re": __import__('re'), "HTTPException": HTTPException,
        "_require_commercial_access": lambda *args: None, "load_job_record_fresh": lambda *args: {"result": result},
        "build_correction_plan": __import__('correction_plan').build_correction_plan,
        "_parse_original_reference": _parse_original_reference,
        "_reference_style_for_result": lambda *args: "apa7",
        "_style_aware_candidate_text": lambda candidate, *args: format_candidate_reference(candidate, "apa7"),
        "_style_aware_candidate_citation": lambda candidate, *args: __import__('reference_metadata').format_candidate_citation(candidate),
        "approved_reference_citation_edits": __import__('reference_review').approved_reference_citation_edits})
    async def json_payload():
        return {"item_id": "source_verification-1", "source_index": 0, "authors": ["Smith, Jane"], "year": "2021"}
    response = asyncio.run(namespace["preview_correction_source"](Mock(json=json_payload), "job"))
    info = response["candidate"]
    assert info["year_selected_by_user"] and info["authors_complete"] and not info["year_choice_confirmed"]
    assert info["citation_text"] == "(Smith, 2021)"
    assert all("2022" not in edit["proposed_replacement"] for edit in info["citation_edits_preview"])


def test_raw_year_change_cannot_bypass_confirmation_by_preserving_the_preview_year():
    original = _parse_original_reference(reference("2018"))
    proposed = {"authors": ["García, Ana", "Smith, Jane", "Smith, James"], "authors_complete": True,
                "title": original["title"], "year": "2017", "publication_years": ["2018"]}
    assert "confirm the publication year" in reference_approval_error(proposed, original)


def test_unparseable_manuscript_reference_is_preserved_in_export():
    raw = "A source entry whose authors and publication year were not recovered"
    exported = format_verified_reference_list([{"reference": raw, "status": "needs_review", "matched_title": "Another study"}])[0]
    assert exported["formatted_reference"] == raw and exported["metadata_warnings"]


def test_report_unrelated_yin_replacement_is_withheld_from_stored_suggestions_and_citation_edits():
    from correction_plan import build_correction_plan
    raw = "Yin, R. K. (2018). Case study research and applications: Design and methods (6th ed.). SAGE Publications."
    wrong = {"authors": ["Jung, Jane", "Oh, John"], "year": "2015", "title": "An Analysis of Design Application Method with Case Study of Synesthesia Research",
             "journal": "The Study of Culture & Art", "url": "https://openalex.org/W2782492000"}
    result = {"selected_style": "apa7", "main_text": "The approach follows Yin (2018).", "references_raw": [raw],
              "online_verification": {"rows": [{"reference": raw, "status": "needs_review", "suggestions": [wrong]}]},
              "correction_source_candidates": {"source_verification-1": [wrong]}}
    item = next(item for item in build_correction_plan(result)["items"] if item["id"] == "source_verification-1")
    assert not item["source_candidates"] and "different publication" in item["withheld_source_reason"]


def test_related_topic_is_not_an_extracted_replacement():
    from reference_review import reference_review_details
    raw = "University of Cape Coast (2024). National competitive tendering via GHANEPS. Retrieved from https://ucc.edu.gh"
    row = {"status": "needs_review", "matched_authors_full": ["Agbodza, James"], "matched_authors_complete": True,
           "matched_title": "Building competitive advantage through supplier debriefing quality and tendering capabilities",
           "matched_year": "2026", "matched_container_title": "Modern Supply Chain Research and Applications",
           "matched_doi": "10.1108/mscra-02-2026-0020"}
    details = reference_review_details(row, _parse_original_reference(raw), "apa7")
    assert not details["extracted_source_candidates"] and details["withheld_source_reason"]
