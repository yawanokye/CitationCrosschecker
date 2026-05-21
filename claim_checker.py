# claim_checker.py

from typing import List, Dict, Any
from citation_suggester import extract_context, split_citation_cluster, suggest_from_context, build_claim_validation_queries
from claim_support_scorer import score_claim_support, fetch_openalex_metadata_by_doi

CLAIM_CHECKER_VERSION = "1.5.34"
CLAIM_CHECKER_BUILD = "commercial-2026-05-21-smart-claim-alternative-source-query-FINAL"

def clean_extracted_claim_text(claim: str) -> str:
    """
    Remove citation residue from extracted claim text.
    Keeps the manuscript claim but removes fragments such as:
    'Button et al., 2013).' or 'Lohr, 2010).'
    """
    import re

    claim = claim or ""
    claim = re.sub(r"\s+", " ", claim).strip()

    # Remove leading broken closing punctuation from citation clusters
    claim = re.sub(r"^[\s\)\]\.,;:]+", "", claim)

    # Remove leading single citation fragment
    claim = re.sub(
        r"^[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+et\s+al\.?)?\s*,?\s*(?:19|20)\d{2}[a-z]?\)?[\s\.,;:]*",
        "",
        claim,
        flags=re.I
    )

    # Remove leading multiple citation fragments
    claim = re.sub(
        r"^(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s*(?:&|and)\s*[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+)?\s*,?\s*(?:19|20)\d{2}[a-z]?\s*;?\s*)+\)?[\s\.,;:]*",
        "",
        claim,
        flags=re.I
    )

    return claim.strip(" ,;:-")


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
        pattern = rf"{re.escape(author)}[^.?!;]{{0,180}}(?:\(\s*)?{re.escape(year[:4])}[a-z]?(?:\s*\))?"
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
    return (
        f"Claim could not be extracted from the manuscript context. Citation searched: {citation}",
        "extraction_failed"
    )

def suggest_alternative_sources_for_claim(
    claim: str,
    citation: str = "",
    current_source_title: str = "",
    top_k: int = 3
):
    """
    Suggest alternative sources when the current matched source gives no evidence,
    weak evidence, or is excluded.

    Commercial logic:
    - The query is claim-first, not author/year-first. This avoids simply finding
      the same weak cited source again.
    - Author/year from the existing citation is not allowed to dominate the search.
    - Candidates are kept only when the smart suggester reports a usable relevance
      signal from title/claim concept overlap or strong citation metadata.
    - Output remains review-only and does not replace citations automatically.
    """
    claim = (claim or "").strip()
    citation = (citation or "").strip()
    current_source_title = (current_source_title or "").strip().lower()

    if len(claim) < 20:
        return []

    # Do not create alternatives from extraction-failure placeholders.
    if claim.lower().startswith("claim could not be extracted"):
        return []

    try:
        candidates = suggest_from_context(
            context=claim,
            citation=citation,
            top_k=top_k + 8,
            use_citation_hint=False,   # important: search by claim, not by the weak source's author/year
            min_relevance=55,
        )
    except Exception as e:
        print(f"[ALT SOURCE ERROR] {citation}: {e}")
        return []

    suggestions = []
    seen = set()

    for cand in candidates or []:
        title = (cand.get("title", "") or "").strip()
        doi = (cand.get("doi", "") or "").strip()
        year = cand.get("year", "")
        authors = cand.get("authors", []) or []
        relevance = float(cand.get("relevance", 0) or 0)

        if not title or relevance < 55:
            continue

        title_key = title.lower()

        # Avoid suggesting the same weak/current source again.
        if current_source_title and (
            title_key == current_source_title
            or title_key in current_source_title
            or current_source_title in title_key
        ):
            continue

        key = f"{title_key}|{year}|{doi.lower()}"
        if key in seen:
            continue
        seen.add(key)

        suggestions.append({
            "title": title,
            "year": year,
            "authors": authors,
            "doi": doi,
            "url": cand.get("url", ""),
            "relevance": relevance,
            "candidate_quality": cand.get("candidate_quality", "possible_candidate"),
            "query_used": cand.get("query_used", ""),
            "query_strategy": cand.get("query_strategy", "claim_keyword_query"),
            "match_basis": cand.get("match_basis", {}),
            "suggestion_type": "alternative_source",
            "review_required": True,
            "reason": (
                "Suggested from the manuscript claim because the current matched source "
                "gave weak, insufficient, or no evidence. Review before using."
            ),
        })

        if len(suggestions) >= top_k:
            break

    return suggestions


def build_claim_validation_query_plan(claim: str, source_title: str = "", doi: str = "", citation: str = "") -> Dict[str, Any]:
    """
    Expose the deep validation search plan for debugging and UI transparency.
    The heavy lookup can still run later in the deep enrichment/payment path.
    """
    return {
        "claim": (claim or "")[:500],
        "source_title": source_title or "",
        "doi": doi or "",
        "citation": citation or "",
        "queries": build_claim_validation_queries(
            claim=claim,
            source_title=source_title,
            doi=doi,
            citation=citation,
        ),
        "note": "Queries are used for deep claim-support validation; they are not run in the fast path unless enabled by the worker.",
    }

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
    MAX_ALT_SOURCE_ROWS = 100
    alt_source_count = 0
    
    full_text = (
        result.get("main_text", "")
        or result.get("full_text", "")
        or result.get("data", {}).get("main_text", "")
        or result.get("data", {}).get("full_text", "")
        or ""
    )
    print("[CLAIM DEBUG] full_text length:", len(full_text or ""))
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
            claim = ""
            claim_source = "mapping_incomplete"
        
            if citation_text:
                claim, claim_source = force_claim_candidate(
                    full_text=full_text,
                    citation=citation_text,
                    row=row,
                    window=600
                )
                claim = clean_extracted_claim_text(claim)
        
            if not claim or claim_source == "extraction_failed":
                claim = "Claim not extracted because citation-reference mapping was incomplete."
        
            out.append({
                "citation": citation_text,
                "claim": claim,
                "claim_source": claim_source,
                "reference": matched_ref,
                "source_title": "No source found",
                "doi": "",
                "support_score": 0,
                "support_status": "mapping_incomplete",
                "evidence_used": "none",
                "alternative_sources": [],
                "title_overlap": 0,
                "abstract_overlap": 0,
                "keyword_overlap": 0,
                "direction_overlap": 0,
                "relation_overlap": 0,
                "partial_support": False,
                "concept_matches": [],
                "score_explanation": "Citation-reference mapping was incomplete, so source-support checking could not be performed."
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
        
            claim, claim_source = force_claim_candidate(
                full_text=full_text,
                citation=citation_text,
                row=row,
                window=600
            )
            claim = clean_extracted_claim_text(claim)
        
            alternative_sources = []
        
            
        
            out.append({
                "citation": citation_text,
                "claim": claim,
                "claim_source": claim_source,
                "reference": matched_ref,
                "source_title": source_label,
                "doi": fallback_vr.get("doi", "") or "",
                "support_score": 0,
                "support_status": "no_evidence_found",
                "evidence_used": "none",
                "alternative_sources": alternative_sources,
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

        # First extract claim using the full citation or full citation cluster.
        # This is important because individual split citations may not appear
        # as standalone text in the manuscript.
        cluster_claim = extract_context(full_text, citation_text, window=600)
        cluster_claim = (cluster_claim or "").strip()
        
        for cit in citation_items:
            claim = cluster_claim
            claim_source = "cluster_context" if claim and len(claim) >= 10 else ""
            claim = clean_extracted_claim_text(claim)
        
            # If cluster-level extraction fails, use the forced claim candidate fallback.
            if not claim or len(claim) < 10:
                claim, claim_source = force_claim_candidate(
                    full_text=full_text,
                    citation=cit,
                    row=row,
                    window=600
                )
                claim = clean_extracted_claim_text(claim)
                            
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
                alternative_sources = suggest_alternative_sources_for_claim(
                    claim=claim,
                    citation=cit,
                    current_source_title=source_title,
                    top_k=3
                )
            
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
                    "alternative_sources": alternative_sources,
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
            support_status = support.get("status", "insufficient_evidence")
            support_score = support.get("score", 0)
            
            alternative_sources = []
            
            alternative_sources = []
            out.append({
                "citation": cit,
                "claim": claim,
                "claim_source": claim_source,
                "reference": matched_ref,
                "source_title": source_title,
                "doi": doi,
                "support_score": support_score,
                "support_status": support_status,
                "alternative_sources": alternative_sources,
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

    # Add transparent query plans for deep claim validation.
    # These are not executed in the fast path; they explain what the deep layer
    # should search when the row is paid/enriched or manually triggered.
    for _row in out:
        try:
            _row.setdefault(
                "validation_query_plan",
                build_claim_validation_query_plan(
                    claim=_row.get("claim", ""),
                    source_title=_row.get("source_title", ""),
                    doi=_row.get("doi", ""),
                    citation=_row.get("citation", ""),
                )
            )
            _row.setdefault("review_required", True)
        except Exception:
            pass

    return out
