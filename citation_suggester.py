# claim_support_scorer.py

import re
from typing import Dict, List, Any, Optional, Tuple
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
    """
    Reduce claim to its core proposition terms.
    Removes weak framing expressions and hedge words.
    """
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

def relation_hits(text: str) -> set:
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

def keyword_overlap_score(claim: str, source_text: str) -> float:
    """Calculate keyword overlap with better semantic handling."""
    claim_keywords = extract_concepts(claim)
    source_keywords = extract_concepts(source_text)
    
    if not claim_keywords:
        return 0.0
    
    # Count matches using set intersection
    claim_set = set(claim_keywords)
    source_set = set(source_keywords)
    
    matched = len(claim_set & source_set)
    
    # Soft denominator: cap at 4 core concepts
    denominator = min(len(claim_set), 4)
    ratio = matched / denominator if denominator > 0 else 0
    
    return min(ratio, 1.0)

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
        return 0.3  # Neutral source
    if (claim_pos and source_neg) or (claim_neg and source_pos):
        return 0.0  # Contradictory
    
    return 0.5  # No clear direction

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

# ============================================================
# MAIN SCORING FUNCTION (IMPROVED)
# ============================================================

def score_claim_support(
    claim: str,
    source_title: str,
    source_abstract: str = "",
    source_concepts: List[str] = None
) -> Dict[str, Any]:
    """
    Score how well a source supports a claim.
    
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
    
    # Step 3: Extract concepts
    claim_concepts = extract_concepts(claim_simplified)
    source_concepts_extracted = extract_concepts(source_text)
    
    # Step 4: Calculate overlaps
    claim_set = set(claim_concepts)
    source_set = set(source_concepts_extracted)
    
    title_overlap = len(claim_set & source_set)
    abstract_overlap = title_overlap  # For compatibility, but we use enriched source
    
    # Step 5: Weighted concept ratio (cap at 4 core concepts)
    weighted_overlap = title_overlap  # Title and abstract already combined
    denominator = max(min(len(claim_set), 4), 1)
    concept_ratio = min(weighted_overlap / denominator, 1.0)
    
    # Step 6: Keyword overlap
    keyword_ratio = keyword_overlap_score(claim_simplified, source_text)
    
    # Step 7: Direction overlap
    direction_ratio = direction_overlap_score(claim_simplified, source_text)
    
    # Step 8: Relation overlap
    claim_rels = relation_hits(claim_simplified)
    source_rels = relation_hits(source_text)
    relation_overlap = len(claim_rels & source_rels)
    relation_bonus = 10 if relation_overlap > 0 else 0
    
    # Step 9: Partial support bonus
    partial_support_bonus = 0
    if title_overlap >= 2:
        partial_support_bonus += 10
    if relation_overlap > 0 and title_overlap >= 1:
        partial_support_bonus += 10
    
    # Step 10: Calculate final score
    # Weights: 55% concept, 15% direction, 10% keyword, plus bonuses
    base_score = (
        55 * concept_ratio +
        15 * direction_ratio +
        10 * keyword_ratio
    )
    
    final_score = base_score + relation_bonus + partial_support_bonus
    final_score = round(min(final_score, 100), 1)
    
    # Step 11: Determine status with relaxed thresholds
    if final_score >= 70:
        status = "strong_support"
    elif final_score >= 55:
        status = "moderate_support"
    elif final_score >= 38:
        status = "related_evidence"
    elif final_score >= 22:
        status = "weak_or_unclear"
    else:
        status = "insufficient_evidence"
    
    return {
        "score": final_score,
        "status": status,
        "title_overlap": title_overlap,
        "abstract_overlap": abstract_overlap,
        "keyword_overlap": round(keyword_ratio * 100, 1),
        "direction_overlap": round(direction_ratio * 100, 1),
        "relation_overlap": relation_overlap,
        "partial_support": partial_support_bonus > 0,
        "claim_simplified": claim_simplified[:200],
        "concept_matches": list(claim_set & source_set)[:10]
    }

# ============================================================
# BATCH PROCESSING WITH CLUSTER AWARENESS
# ============================================================

def score_claim_support_batch(
    claims: List[str],
    source_title: str,
    source_abstract: str = "",
    source_concepts: List[str] = None
) -> List[Dict[str, Any]]:
    """Score multiple claims against the same source."""
    results = []
    for claim in claims:
        if claim:
            results.append(score_claim_support(claim, source_title, source_abstract, source_concepts))
        else:
            results.append({
                "score": 0,
                "status": "insufficient_evidence",
                "title_overlap": 0,
                "abstract_overlap": 0,
                "keyword_overlap": 0,
                "direction_overlap": 0,
                "relation_overlap": 0,
                "partial_support": False
            })
    return results
