# claim_checker.py

from typing import List, Dict, Any
from citation_suggester import extract_context, split_citation_cluster
from claim_support_scorer import score_claim_support, fetch_openalex_metadata_by_doi

def build_claim_support_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build claim-to-source support rows.
    Never silently drops a row.

    Clear separation:
    - claim field: what was extracted, or "No claim extracted"
    - source_title field: matched source, or reason source is unavailable
    - support_status field: support decision, including "no_evidence_found"
    """
    out = []

    full_text = result.get("main_text", "") or result.get("full_text", "")
    c2r_rows = result.get("reconciliation_intext_to_reference", []) or []

    online_verification = result.get("online_verification", {}) or {}
    verify_rows = online_verification.get("rows", []) or []

    verified_lookup = {}
    all_verify_lookup = {}

    for row in verify_rows:
        ref = row.get("reference", "") or ""
        if ref:
            all_verify_lookup[ref] = row

        status = row.get("status", "")
        if status in ["verified", "likely"] and ref:
            verified_lookup[ref] = row

    for row in c2r_rows:
        matched_ref = row.get("matched_reference", "") or ""
        citation_text = (
            row.get("in_text", "")
            or row.get("citation", "")
            or row.get("citation_in_text", "")
            or ""
        )

        if not matched_ref or not citation_text:
            out.append({
                "citation": citation_text,
                "claim": "No claim extracted",
                "reference": matched_ref,
                "source_title": "No source found",
                "doi": "",
                "support_score": 0,
                "support_status": "no_evidence_found",
                "evidence_used": "none",
                "title_overlap": 0,
                "abstract_overlap": 0,
                "keyword_overlap": 0,
                "direction_overlap": 0,
                "relation_overlap": 0,
                "partial_support": False,
                "concept_matches": [],
                "score_explanation": "Citation-reference mapping was incomplete."
            })
            continue

        vr = verified_lookup.get(matched_ref)

        if not vr:
            fallback_vr = all_verify_lookup.get(matched_ref, {})
            mismatch_flag = int(fallback_vr.get("author_mismatch_flag", 0))

            source_label = "No source found"
            note = "No trusted source evidence was available."

            if fallback_vr:
                source_label = fallback_vr.get("matched_title", "") or "No source found"
                if mismatch_flag == 1:
                    source_label = "Source excluded, author mismatch"
                    note = "Matched source was excluded because of author mismatch."
                elif fallback_vr.get("status") in {"needs_review", "not_found"}:
                    note = f"Matched source was not trusted because verification status is {fallback_vr.get('status')}."

            out.append({
                "citation": citation_text,
                "claim": "No claim extracted",
                "reference": matched_ref,
                "source_title": source_label,
                "doi": fallback_vr.get("doi", "") or "",
                "support_score": 0,
                "support_status": "no_evidence_found",
                "evidence_used": "none",
                "title_overlap": 0,
                "abstract_overlap": 0,
                "keyword_overlap": 0,
                "direction_overlap": 0,
                "relation_overlap": 0,
                "partial_support": False,
                "concept_matches": [],
                "match_note": fallback_vr.get("match_note", ""),
                "score_explanation": note
            })
            continue

        citation_items = split_citation_cluster(citation_text)

        for cit in citation_items:
            claim = extract_context(full_text, cit, window=600)
            claim = (claim or "").strip()

            source_title = vr.get("matched_title", "") or ""
            doi = vr.get("doi", "") or ""

            if not claim or len(claim) < 10:
                out.append({
                    "citation": cit,
                    "claim": "No claim extracted",
                    "reference": matched_ref,
                    "source_title": source_title or "No source found",
                    "doi": doi,
                    "support_score": 0,
                    "support_status": "claim_not_extracted",
                    "evidence_used": "claim_extraction_failed",
                    "title_overlap": 0,
                    "abstract_overlap": 0,
                    "keyword_overlap": 0,
                    "direction_overlap": 0,
                    "relation_overlap": 0,
                    "partial_support": False,
                    "concept_matches": [],
                    "match_note": vr.get("match_note", ""),
                    "score_explanation": "No meaningful claim could be extracted from the citation context."
                })
                continue

            # Fast mode: do not fetch OpenAlex metadata during the main verification flow.
            # Claim support will use the already-verified source title only.
            metadata = {}
            source_abstract = ""
            source_concepts = []

            if not source_title and not source_abstract and not source_concepts:
                out.append({
                    "citation": cit,
                    "claim": claim,
                    "reference": matched_ref,
                    "source_title": "No source found",
                    "doi": doi,
                    "support_score": 0,
                    "support_status": "no_evidence_found",
                    "evidence_used": "none",
                    "title_overlap": 0,
                    "abstract_overlap": 0,
                    "keyword_overlap": 0,
                    "direction_overlap": 0,
                    "relation_overlap": 0,
                    "partial_support": False,
                    "concept_matches": [],
                    "match_note": vr.get("match_note", ""),
                    "score_explanation": "No usable source title, abstract, or concepts were available for support checking."
                })
                continue

            support = score_claim_support(
                claim=claim,
                source_title=source_title,
                source_abstract=source_abstract,
                source_concepts=source_concepts,
                source_metadata=metadata
            )

            out.append({
                "citation": cit,
                "claim": claim,
                "reference": matched_ref,
                "source_title": source_title,
                "doi": doi,
                "support_score": support.get("score", 0),
                "support_status": support.get("status", "insufficient_evidence"),
                "evidence_used": (
                    "title+abstract+concepts"
                    if source_concepts else
                    ("title+abstract" if source_abstract else "title_only")
                ),
                "title_overlap": support.get("title_overlap", 0),
                "abstract_overlap": support.get("abstract_overlap", 0),
                "keyword_overlap": support.get("keyword_overlap", 0),
                "direction_overlap": support.get("direction_overlap", 0),
                "relation_overlap": support.get("relation_overlap", 0),
                "partial_support": support.get("partial_support", False),
                "concept_matches": support.get("concept_matches", []),
                "match_note": vr.get("match_note", ""),
                "score_explanation": (
                    f"title={support.get('title_overlap', 0)}, "
                    f"abstract={support.get('abstract_overlap', 0)}, "
                    f"keyword={support.get('keyword_overlap', 0)}, "
                    f"direction={support.get('direction_overlap', 0)}, "
                    f"relation={support.get('relation_overlap', 0)}"
                )
            })

    return out
