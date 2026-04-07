# claim_checker.py

from typing import List, Dict, Any
from citation_suggester import extract_context, split_citation_cluster
from claim_support_scorer import score_claim_support, fetch_openalex_metadata_by_doi

def build_claim_support_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build claim-to-source support rows.
    Never silently drops a row. If no meaningful claim or no usable source evidence
    is found, classify it as no_evidence_found.
    """
    out = []

    full_text = result.get("main_text", "") or result.get("full_text", "")
    c2r_rows = result.get("reconciliation_intext_to_reference", []) or []

    # Build verified lookup from online_verification
    online_verification = result.get("online_verification", {}) or {}
    verify_rows = online_verification.get("rows", []) or []

    verified_lookup = {}
    for row in verify_rows:
        ref = row.get("reference", "") or ""
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
                "claim": "",
                "reference": matched_ref,
                "source_title": "",
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
                "score_explanation": "No evidence found. Citation-reference mapping was incomplete."
            })
            continue

        vr = verified_lookup.get(matched_ref)
        if not vr:
            out.append({
                "citation": citation_text,
                "claim": "",
                "reference": matched_ref,
                "source_title": "",
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
                "score_explanation": "No evidence found. The matched reference was not verified or likely, so no usable source evidence was available."
            })
            continue

        citation_items = split_citation_cluster(citation_text)

        for cit in citation_items:
            claim = extract_context(full_text, cit, window=300)

            if not claim or len(claim.strip()) < 10:
                out.append({
                    "citation": cit,
                    "claim": "",
                    "reference": matched_ref,
                    "source_title": vr.get("matched_title", "") or "",
                    "doi": vr.get("doi", "") or "",
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
                    "score_explanation": "No evidence found. No meaningful claim could be extracted from the citation context."
                })
                continue

            source_title = vr.get("matched_title", "") or ""
            doi = vr.get("doi", "") or ""

            metadata = fetch_openalex_metadata_by_doi(doi) if doi else {}
            source_abstract = metadata.get("abstract", "")
            source_concepts = metadata.get("concepts", [])

            if not source_title and not source_abstract and not source_concepts:
                out.append({
                    "citation": cit,
                    "claim": claim,
                    "reference": matched_ref,
                    "source_title": "",
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
                    "score_explanation": "No evidence found. No usable source title, abstract, or concepts were available for support checking."
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
                "score_explanation": (
                    f"title={support.get('title_overlap', 0)}, "
                    f"abstract={support.get('abstract_overlap', 0)}, "
                    f"keyword={support.get('keyword_overlap', 0)}, "
                    f"direction={support.get('direction_overlap', 0)}, "
                    f"relation={support.get('relation_overlap', 0)}"
                )
            })

    return out
