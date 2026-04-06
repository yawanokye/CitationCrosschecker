# claim_support_scorer.py

import re
import numpy as np
from datetime import datetime
from typing import Dict, List, Any, Optional, Set, Tuple
from rapidfuzz import fuzz

# ============================================================
# ENHANCED CONCEPT MAP FOR SEMANTIC MATCHING
# ============================================================

CONCEPT_MAP = {
    "improves": "improve",
    "improve": "improve",
    "improved": "improve",
    "enhances": "improve",
    "enhance": "improve",
    "enhanced": "improve",
    "boosts": "improve",
    "boost": "improve",
    "strengthens": "improve",
    "strengthen": "improve",
    "optimises": "improve",
    "optimizes": "improve",
    "optimise": "improve",
    "optimize": "improve",
    
    "reduces": "reduce",
    "reduce": "reduce",
    "reduced": "reduce",
    "mitigates": "reduce",
    "mitigate": "reduce",
    "lowers": "reduce",
    "lower": "reduce",
    "decreases": "reduce",
    "decrease": "reduce",
    "minimizes": "reduce",
    "minimize": "reduce",
    
    "increases": "increase",
    "increase": "increase",
    "increased": "increase",
    "raises": "increase",
    "raise": "increase",
    "elevates": "increase",
    "elevate": "increase",
    
    "adoption": "adopt",
    "adopted": "adopt",
    "adopt": "adopt",
    "implementation": "adopt",
    "implement": "adopt",
    "use": "adopt",
    "usage": "adopt",
    "uptake": "adopt",
    "acceptance": "adopt",
    
    "performance": "performance",
    "productivity": "performance",
    "efficiency": "performance",
    "effectiveness": "performance",
    "outcome": "performance",
    "outcomes": "performance",
    
    "transparency": "transparency",
    "accountability": "transparency",
    "openness": "transparency",
    
    "trust": "trust",
    "credibility": "trust",
    "confidence": "trust",
    
    "risk": "risk",
    "uncertainty": "risk",
    "exposure": "risk",
    
    "error": "error",
    "errors": "error",
    "mistake": "error",
    "mistakes": "error",
    
    "corruption": "corruption",
    "fraud": "corruption",
    "misconduct": "corruption",
    "unethical": "corruption",
    
    "reliability": "reliability",
    "robustness": "reliability",
    "stability": "reliability",
    "consistency": "reliability",
    
    "positive": "positive",
    "beneficial": "positive",
    "advantageous": "positive",
    "favorable": "positive",
    
    "negative": "negative",
    "harmful": "negative",
    "detrimental": "negative",
    "adverse": "negative",
}

# ============================================================
# RELATION PATTERNS FOR CLAIM-SOURCE MATCHING
# ============================================================

RELATION_PATTERNS = {
    "improve": [r"\bimprov\w*\b", r"\benhanc\w*\b", r"\bboost\w*\b", r"\bstrength\w*\b"],
    "reduce": [r"\breduc\w*\b", r"\bmitigat\w*\b", r"\blower\w*\b", r"\bdecreas\w*\b"],
    "increase": [r"\bincreas\w*\b", r"\brais\w*\b", r"\belevat\w*\b"],
    "influence": [r"\binfluenc\w*\b", r"\baffect\w*\b", r"\bimpact\w*\b"],
    "predict": [r"\bpredict\w*\b", r"\bdetermin\w*\b", r"\bexplain\w*\b"],
    "associate": [r"\bassociat\w*\b", r"\brelat\w*\b", r"\blink\w*\b"],
    "cause": [r"\bcaus\w*\b", r"\blead\w*\b", r"\bresult\w*\b", r"\bproduc\w*\b"],
}

# ============================================================
# CLAIM TYPE DETECTION
# ============================================================

CLAIM_TYPES = {
    "causal": {
        "patterns": [r"\b(causes|leads to|results in|affects|influences|impacts)\b"],
        "bonus": 15
    },
    "comparative": {
        "patterns": [r"\b(better than|worse than|superior to|inferior to|compared to)\b"],
        "bonus": 10
    },
    "quantitative": {
        "patterns": [r"\b(\d+%|percent|percentage|increase of|decrease of)\b"],
        "bonus": 10
    },
    "theoretical": {
        "patterns": [r"\b(theory|framework|model|proposes|suggests that)\b"],
        "bonus": 5
    },
    "methodological": {
        "patterns": [r"\b(method|approach|technique|procedure|protocol)\b"],
        "bonus": 8
    }
}

# ============================================================
# ENHANCED STOPWORDS
# ============================================================

STOPWORDS = {
    "this", "that", "these", "those", "there", "their", "they", "them",
    "with", "from", "using", "used", "use", "study", "analysis", "method",
    "approach", "results", "table", "figure", "paper", "research",
    "journal", "review", "would", "could", "should", "might", "what",
    "when", "where", "which", "while", "about", "into", "through",
    "during", "without", "between", "among", "within", "across",
    "article", "articles", "author", "authors", "evidence", "model",
    "models", "framework", "frameworks", "effect", "effects", "role",
    "roles", "impact", "impacts", "relationship", "relationships",
    "factor", "factors", "based", "examines", "examined", "investigates",
    "investigated", "analysis", "analyses", "data", "study", "studies",
    "paper", "result", "results", "finding", "findings", "conclusion",
    "conclusions", "discussion", "section", "chapter", "appendix"
}

# ============================================================
# CLAIM SIMPLIFIER
# ============================================================

def simplify_claim(claim: str) -> str:
    """Reduce claim to its core proposition terms."""
    if not claim:
        return ""
    
    claim = claim.strip()
    
    # Remove weak framing expressions
    claim = re.sub(r"\b(this study|this paper|the study|the findings show that|results show that|it was found that|we find that|we show that)\b", "", claim, flags=re.I)
    
    # Remove hedge words and weak modifiers
    claim = re.sub(r"\b(significantly|generally|often|may|might|can|could|appears to|tends to|seems to|potentially|possibly|approximately|roughly|about)\b", "", claim, flags=re.I)
    
    # Remove citation markers
    claim = re.sub(r"\s*\([^)]*\)\s*", " ", claim)
    claim = re.sub(r"\s*\[[^\]]*\]\s*", " ", claim)
    
    # Collapse spaces
    claim = re.sub(r"\s+", " ", claim).strip(" ,;:-")
    
    return claim

# ============================================================
# CONCEPT EXTRACTION WITH MAPPING
# ============================================================

def extract_concepts(text: str) -> List[str]:
    """Extract and normalize concepts from text using concept map."""
    if not text:
        return []
    
    text = text.lower()
    
    # Extract words (3+ chars, alphabetic)
    words = re.findall(r"[a-z]{3,}", text)
    
    concepts = []
    for w in words:
        if w in STOPWORDS:
            continue
        
        # Map to normalized concept
        concept = CONCEPT_MAP.get(w, w)
        concepts.append(concept)
    
    # Deduplicate while preserving order
    seen = set()
    out = []
    for c in concepts:
        if c not in seen:
            seen.add(c)
            out.append(c)
    
    return out

def get_concept_weights(claim_concepts: List[str], all_claims: List[str] = None) -> Dict[str, float]:
    """Give higher weight to rare, specific concepts."""
    if not all_claims:
        # Default weights: rare concepts = higher weight
        common_concepts = {"improve", "reduce", "increase", "effect", "impact", "result"}
        weights = {}
        for c in claim_concepts:
            if c in common_concepts:
                weights[c] = 0.5
            elif len(c) > 8:
                weights[c] = 1.5
            else:
                weights[c] = 1.0
        return weights
    
    # Calculate inverse document frequency across all claims
    doc_freq = {}
    for claim in all_claims:
        unique_concepts = set(extract_concepts(claim))
        for c in unique_concepts:
            doc_freq[c] = doc_freq.get(c, 0) + 1
    
    n_docs = len(all_claims)
    weights = {}
    for c in claim_concepts:
        df = doc_freq.get(c, 1)
        idf = np.log(n_docs / df) + 1
        weights[c] = min(idf, 2.0)
    
    return weights

def weighted_concept_overlap(claim_concepts: List[str], source_concepts: List[str], weights: Dict[str, float]) -> float:
    """Calculate weighted overlap where rare concepts count more."""
    if not claim_concepts:
        return 0.0
    
    claim_set = set(claim_concepts)
    source_set = set(source_concepts)
    
    total_weight = sum(weights.get(c, 1.0) for c in claim_set)
    matched_weight = sum(weights.get(c, 1.0) for c in (claim_set & source_set))
    
    denominator = max(min(len(claim_set), 4), 1)
    return min(matched_weight / (denominator * 1.5), 1.0)

# ============================================================
# RELATION AND DIRECTION DETECTION
# ============================================================

def relation_hits(text: str) -> Set[str]:
    """Detect relation types present in text."""
    if not text:
        return set()
    
    text = text.lower()
    hits = set()
    
    for rel, patterns in RELATION_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text):
                hits.add(rel)
                break
    
    return hits

def direction_overlap_score(claim: str, source_text: str) -> float:
    """Check if direction (positive/negative) is consistent."""
    claim = claim.lower()
    source = source_text.lower()
    
    positive_words = ["improve", "enhance", "increase", "boost", "strengthen", "beneficial", "positive", "advantage"]
    negative_words = ["reduce", "decrease", "mitigate", "lower", "minimize", "negative", "harmful", "detrimental"]
    
    claim_pos = any(w in claim for w in positive_words)
    claim_neg = any(w in claim for w in negative_words)
    source_pos = any(w in source for w in positive_words)
    source_neg = any(w in source for w in negative_words)
    
    if claim_pos and source_pos:
        return 1.0
    if claim_neg and source_neg:
        return 1.0
    if (claim_pos or claim_neg) and not (source_pos or source_neg):
        return 0.3
    if (claim_pos and source_neg) or (claim_neg and source_pos):
        return 0.0
    
    return 0.5

# ============================================================
# CLAIM TYPE CLASSIFICATION
# ============================================================

def classify_claim(claim: str) -> List[str]:
    """Identify claim types present in the text."""
    claim_lower = claim.lower()
    types = []
    for claim_type, info in CLAIM_TYPES.items():
        for pattern in info["patterns"]:
            if re.search(pattern, claim_lower):
                types.append(claim_type)
                break
    return types

def get_claim_type_bonus(claim_types: List[str]) -> float:
    """Calculate bonus based on claim types."""
    if not claim_types:
        return 0
    
    total_bonus = sum(CLAIM_TYPES[ct]["bonus"] for ct in claim_types if ct in CLAIM_TYPES)
    return total_bonus / len(claim_types) if claim_types else 0

# ============================================================
# SOURCE AUTHORITY BOOST
# ============================================================

def get_source_authority_boost(source_metadata: Dict = None) -> float:
    """Calculate boost based on source quality indicators."""
    if not source_metadata:
        return 0
    
    boost = 0
    
    # Journal impact indicators
    if source_metadata.get("is_peer_reviewed"):
        boost += 5
    
    # Citation count
    cited_by = source_metadata.get("cited_by_count", 0)
    if cited_by > 100:
        boost += 10
    elif cited_by > 50:
        boost += 5
    elif cited_by > 10:
        boost += 2
    
    # Recent publication
    year = source_metadata.get("publication_year", 0)
    current_year = datetime.now().year
    if year and current_year - year <= 3:
        boost += 3
    
    # Open access
    if source_metadata.get("is_oa"):
        boost += 2
    
    return min(boost, 15)

# ============================================================
# SEMANTIC SIMILARITY (N-GRAM BASED)
# ============================================================

def semantic_similarity(claim: str, source_text: str) -> float:
    """Calculate semantic similarity using n-gram overlap."""
    if not claim or not source_text:
        return 0.0
    
    claim_ngrams = set()
    source_ngrams = set()
    
    # Add unigrams (concepts)
    claim_concepts = extract_concepts(claim)
    source_concepts = extract_concepts(source_text)
    claim_ngrams.update(claim_concepts)
    source_ngrams.update(source_concepts)
    
    # Add bigrams
    claim_words = claim.lower().split()
    for i in range(len(claim_words) - 1):
        if len(claim_words[i]) > 2 and len(claim_words[i+1]) > 2:
            if claim_words[i] not in STOPWORDS and claim_words[i+1] not in STOPWORDS:
                claim_ngrams.add(f"{claim_words[i]}_{claim_words[i+1]}")
    
    source_words = source_text.lower().split()
    for i in range(len(source_words) - 1):
        if len(source_words[i]) > 2 and len(source_words[i+1]) > 2:
            if source_words[i] not in STOPWORDS and source_words[i+1] not in STOPWORDS:
                source_ngrams.add(f"{source_words[i]}_{source_words[i+1]}")
    
    if not claim_ngrams:
        return 0.0
    
    overlap = len(claim_ngrams & source_ngrams)
    ratio = overlap / min(len(claim_ngrams), 10)
    return min(ratio, 1.0)

# ============================================================
# MAIN SCORING FUNCTION (ENHANCED)
# ============================================================

def score_claim_support(
    claim: str,
    source_title: str,
    source_abstract: str = "",
    source_concepts: List[str] = None,
    source_metadata: Dict = None,
    all_claims: List[str] = None
) -> Dict[str, Any]:
    """
    Enhanced scoring with multiple evidence layers for academic paraphrasing.
    
    Returns:
        Dictionary with score, status, and detailed components
    """
    if not claim or not source_title:
        return {
            "score": 0,
            "status": "insufficient_evidence",
            "title_overlap": 0,
            "abstract_overlap": 0,
            "keyword_overlap": 0,
            "direction_overlap": 0,
            "relation_overlap": 0,
            "partial_support": False
        }
    
    # Step 1: Simplify the claim
    claim_simplified = simplify_claim(claim)
    if not claim_simplified:
        claim_simplified = claim
    
    # Step 2: Build enriched source text
    source_parts = [source_title]
    if source_abstract:
        source_parts.append(source_abstract)
    if source_concepts:
        source_parts.append(" ".join(source_concepts))
    source_text = " ".join(source_parts).lower()
    
    # Step 3: Extract concepts with weights
    claim_concepts = extract_concepts(claim_simplified)
    source_concepts_extracted = extract_concepts(source_text)
    
    weights = get_concept_weights(claim_concepts, all_claims)
    concept_score = weighted_concept_overlap(claim_concepts, source_concepts_extracted, weights)
    
    # Step 4: Relation matching
    claim_rels = relation_hits(claim_simplified)
    source_rels = relation_hits(source_text)
    relation_score = len(claim_rels & source_rels) / max(len(claim_rels), 1) if claim_rels else 0
    
    # Step 5: Direction/valence consistency
    direction_score = direction_overlap_score(claim_simplified, source_text)
    
    # Step 6: Semantic similarity (n-gram)
    semantic_score = semantic_similarity(claim_simplified, source_text)
    
    # Step 7: Claim type bonus
    claim_types = classify_claim(claim_simplified)
    type_bonus = get_claim_type_bonus(claim_types)
    
    # Step 8: Source authority boost
    authority_boost = get_source_authority_boost(source_metadata)
    
    # Step 9: Partial evidence accumulation
    claim_set = set(claim_concepts)
    source_set = set(source_concepts_extracted)
    partial_bonus = 0
    if len(claim_set & source_set) >= 2:
        partial_bonus += 10
    if relation_score > 0 and len(claim_set & source_set) >= 1:
        partial_bonus += 10
    
    # Step 10: Calculate final score with adjusted weights
    base_score = (
        40 * concept_score +      # Concept overlap (reduced from 55)
        15 * relation_score +      # Relation matching (increased from 0)
        10 * direction_score +     # Direction consistency
        20 * semantic_score        # Semantic similarity (new)
    )
    
    final_score = base_score + type_bonus + authority_boost + partial_bonus
    final_score = round(min(final_score, 100), 1)
    
    # Step 11: Relaxed thresholds for academic writing
    if final_score >= 60:
        status = "strong_support"
    elif final_score >= 45:
        status = "moderate_support"
    elif final_score >= 30:
        status = "related_evidence"
    elif final_score >= 18:
        status = "weak_or_unclear"
    else:
        status = "insufficient_evidence"
    
    return {
        "score": final_score,
        "status": status,
        "title_overlap": len(claim_set & source_set),
        "abstract_overlap": len(claim_set & source_set),
        "keyword_overlap": round(concept_score * 100, 1),
        "direction_overlap": round(direction_score * 100, 1),
        "relation_overlap": len(claim_rels & source_rels),
        "partial_support": partial_bonus > 0,
        "claim_simplified": claim_simplified[:200],
        "concept_matches": list(claim_set & source_set)[:10],
        "claim_types": claim_types,
        "semantic_score": round(semantic_score * 100, 1),
        "type_bonus": type_bonus,
        "authority_boost": authority_boost
    }

# ============================================================
# FETCH OPENALEX METADATA WITH CONCEPTS
# ============================================================

def fetch_openalex_metadata_by_doi(doi: str) -> Dict[str, Any]:
    """Fetch OpenAlex metadata including concepts for better matching."""
    if not doi:
        return {}
    
    import requests
    from verify import _safe_get_json
    
    doi = doi.replace("https://doi.org/", "").strip()
    url = f"https://api.openalex.org/works/https://doi.org/{doi}"
    data = _safe_get_json(url)
    
    if not data:
        return {}
    
    result = {
        "title": data.get("title", ""),
        "abstract": data.get("abstract", ""),
        "publication_year": data.get("publication_year", ""),
        "cited_by_count": data.get("cited_by_count", 0),
        "is_oa": data.get("open_access", {}).get("is_oa", False),
        "concepts": []
    }
    
    # Extract concept display names
    for concept in data.get("concepts", [])[:8]:
        concept_name = concept.get("display_name", "")
        if concept_name:
            result["concepts"].append(concept_name)
    
    return result

def build_source_text_from_metadata(metadata: Dict[str, Any]) -> str:
    """Build enriched source text from title, abstract, and concepts."""
    parts = []
    
    title = metadata.get("title", "")
    if title:
        parts.append(title)
    
    abstract = metadata.get("abstract", "")
    if abstract:
        parts.append(abstract)
    
    concepts = metadata.get("concepts", [])
    if concepts:
        parts.append(" ".join(concepts))
    
    return " ".join(parts)
