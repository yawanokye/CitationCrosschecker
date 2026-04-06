# claim_checker.py

from typing import List, Dict, Any
from citation_suggester import extract_context, split_citation_cluster
from claim_support_scorer import score_claim_support, fetch_openalex_metadata_by_doi

def build_claim_support_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build claim-to-source support rows with relaxed thresholds.
    """
    out = []
    
    full_text = result.get("main_text", "") or result.get("full_text", "")
    c2r_rows = result.get("reconciliation_intext_to_reference", []) or []
    
    # Build verified lookup from online_verification
    online_verification = result.get("online_verification", {})
    verify_rows = online_verification.get("rows", []) or []
    
    verified_lookup = {}
    for row in verify_rows:
        ref = row.get("reference", "") or ""
        status = row.get("status", "")
        if status in ["verified", "likely"] and ref:
            verified_lookup[ref] = row
    
    for row in c2r_rows:
        matched_ref = row.get("matched_reference", "") or ""
        citation_text = row.get("in_text", "") or row.get("citation", "") or row.get("citation_in_text", "") or ""
        
        if not matched_ref or not citation_text:
            continue
        
        vr = verified_lookup.get(matched_ref)
        if not vr:
            continue
        
        # Split clustered citations
        citation_items = split_citation_cluster(citation_text)
        
        for cit in citation_items:
            # Extract claim from context
            claim = extract_context(full_text, cit, window=300)
            if not claim or len(claim) < 10:
                continue
            
            source_title = vr.get("matched_title", "") or ""
            doi = vr.get("doi", "") or ""
            
            # Fetch metadata for better scoring
            metadata = fetch_openalex_metadata_by_doi(doi) if doi else {}
            source_abstract = metadata.get("abstract", "")
            source_concepts = metadata.get("concepts", [])
            
            if not source_title and not source_abstract:
                continue
            
            # Score with relaxed parameters
            support = score_claim_support(
                claim=claim,
                source_title=source_title,
                source_abstract=source_abstract,
                source_concepts=source_concepts,
                source_metadata=metadata
            )
            
            # Get the score and status
            final_score = support.get("score", 0)
            support_status = support.get("status", "insufficient_evidence")
            
            out.append({
                "citation": cit,
                "claim": claim,
                "reference": matched_ref,
                "source_title": source_title,
                "doi": doi,
                "support_score": final_score,
                "support_status": support_status,
                "evidence_used": "title+abstract+concepts" if source_concepts else ("title+abstract" if source_abstract else "title_only"),
                "title_overlap": support.get("title_overlap", 0),
                "abstract_overlap": support.get("abstract_overlap", 0),
                "keyword_overlap": support.get("keyword_overlap", 0),
                "direction_overlap": support.get("direction_overlap", 0),
                "relation_overlap": support.get("relation_overlap", 0),
                "partial_support": support.get("partial_support", False),
                "concept_matches": support.get("concept_matches", [])
            })
    
    return out
