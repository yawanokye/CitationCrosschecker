# claim_checker.py

from typing import List, Dict, Any
from citation_suggester import extract_context, split_citation_cluster
from claim_support_scorer import score_claim_support, fetch_openalex_metadata_by_doi

def force_claim_candidate(full_text: str, citation: str, row: Dict[str, Any], window: int = 600):
    """
    Always return a claim candidate once a citation exists.
    Priority:
    1. Use extract_context()
    2. Use context fields from reconciliation row
    3. Locate author-year in full_text and extract sentence window
    4. Return extraction_failed marker
    """
    citation = (citation or "").strip()

    # 1. Normal extractor
    claim = extract_context(full_text, citation, window=window)
    claim = (claim or "").strip()

    if len(claim) >= 10:
        return claim, "extract_context"

    # 2. Reconciliation row fallback
    for key in ["context", "sentence", "citation_context", "nearby_text", "left_context", "right_context"]:
        val = (row.get(key, "") or "").strip()
        if len(val) >= 10:
            return val, f"row_{key}"

    # 3. Last manuscript-text fallback using author-year pieces
    # This handles cases where citation is stored as "Xue, 2002"
    # but appears in text as "(Xue, 2002)" or "Xue (2002)".
    import re

    years = re.findall(r"(?:19|20)\d{2}[a-z]?", citation)
    year = years[0] if years else ""

    tokens = re.findall(r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]{2,}", citation)
    stop = {"And", "Et", "Al"}
    authors = [t for t in tokens if t not in stop]
    author = authors[0] if authors else ""

    if full_text and author and year:
        pattern = rf"{re.escape(author)}[^.?!;]{{0,80}}{re.escape(year[:4])}[a-z]?"
        m = re.search(pattern, full_text, flags=re.I)

        if m:
            pos = m.start()

            left = max(
                full_text.rfind(".", 0, pos),
                full_text.rfind("?", 0, pos),
                full_text.rfind("!", 0, pos),
                full_text.rfind(";", 0, pos),
                full_text.rfind("\n", 0, pos)
            )
            left = 0 if left == -1 else left + 1

            rights = [
                full_text.find(".", pos),
                full_text.find("?", pos),
                full_text.find("!", pos),
                full_text.find(";", pos),
                full_text.find("\n", pos)
            ]
            rights = [r for r in rights if r != -1]
            right = min(rights) + 1 if rights else min(len(full_text), pos + window)

            candidate = full_text[left:right]
            candidate = re.sub(r"\s+", " ", candidate).strip(" ,;:-")

            if len(candidate) >= 10:
                return candidate, "fallback_sentence_window"

    # 4. Final forced output
    return "Claim could not be extracted from the manuscript context.", "extraction_failed"

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
            claim, claim_source = force_claim_candidate(
                full_text=full_text,
                citation=cit,
                row=row,
                window=600
            )
            
            source_title = vr.get("matched_title", "") or ""
            doi = vr.get("doi", "") or ""
            
            if claim_source == "extraction_failed":
                out.append({
                    "citation": cit,
                    "claim": claim,
                    "claim_source": claim_source,
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
                    "score_explanation": "A citation was detected, but the system could not extract a meaningful manuscript claim around it."
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
                    "claim_source": claim_source,
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
                "claim_source": claim_source,
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
