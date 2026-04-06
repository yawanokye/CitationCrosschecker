# claim_support_scorer.py

import re
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
# RELATION PATTERNS
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
# STOPWORDS
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
# HELPER FUNCTIONS
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

def extract_concepts(text: str) -> List[str]:
    """Extract and normalize concepts from text using concept map."""
    if not text:
        return []
    
    text = text.lower()
    words = re.findall(r"[a-z]{3,}", text)
    
    concepts = []
    for w in words:
        if w in STOPWORDS:
            continue
        concept = CONCEPT_MAP.get(w, w)
        concepts.append(concept)
    
    # Deduplicate
    seen = set()
    out = []
    for c in concepts:
        if c not in seen:
            seen.add(c)
            out.append(c)
    
    return out

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
        return 0.4  # Increased from 0.3
    if (claim_pos and source_neg) or (claim_neg and source_pos):
        return 0.0
    
    return 0.6  # Increased from 0.5

# ============================================================
# MAIN SCORING FUNCTION (RELAXED THRESHOLDS + PARTIAL CREDIT)
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
    Enhanced scoring with relaxed thresholds and partial credit.
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
    
    # Simplify claim
    claim_simplified = simplify_claim(claim)
    if not claim_simplified:
        claim_simplified = claim
    
    # Build enriched source text
    source_parts = [source_title]
    if source_abstract:
        source_parts.append(source_abstract)
    if source_concepts:
        source_parts.append(" ".join(source_concepts))
    source_text = " ".join(source_parts).lower()
    
    # Extract concepts
    claim_concepts = extract_concepts(claim_simplified)
    source_concepts_extracted = extract_concepts(source_text)
    
    claim_set = set(claim_concepts)
    source_set = set(source_concepts_extracted)
    
    # Concept overlap (cap at 4 core concepts)
    concept_overlap = len(claim_set & source_set)
    denominator = max(min(len(claim_set), 4), 1)
    concept_ratio = min(concept_overlap / denominator, 1.0)
    
    # Relation matching
    claim_rels = relation_hits(claim_simplified)
    source_rels = relation_hits(source_text)
    relation_overlap = len(claim_rels & source_rels)
    relation_ratio = relation_overlap / max(len(claim_rels), 1) if claim_rels else 0
    
    # Direction score
    direction_ratio = direction_overlap_score(claim_simplified, source_text)
    
    # ============================================================
    # PARTIAL CREDIT BONUS (NEW)
    # ============================================================
    partial_credit = 0
    
    # Bonus for ANY concept match
    if concept_overlap >= 1:
        partial_credit += 15
    
    # Bonus for concept match + relation match
    if concept_overlap >= 1 and relation_overlap >= 1:
        partial_credit += 10
    
    # Bonus for direction match
    if direction_ratio >= 0.8:
        partial_credit += 10
    
    # Bonus for at least 2 concept matches
    if concept_overlap >= 2:
        partial_credit += 10
    
    # ============================================================
    # BASE SCORE (LOWER WEIGHTS FOR CONCEPT, HIGHER FOR RELATION)
    # ============================================================
    base_score = (
        40 * concept_ratio +      # Concept overlap (reduced from 55)
        25 * relation_ratio +      # Relation matching (increased)
        15 * direction_ratio       # Direction consistency
    )
    
    # Final score with partial credit
    final_score = base_score + partial_credit
    final_score = round(min(final_score, 100), 1)
    
    # ============================================================
    # RELAXED THRESHOLDS (UPDATED)
    # ============================================================
    if final_score >= 50:      # Was 70
        status = "strong_support"
    elif final_score >= 35:    # Was 55
        status = "moderate_support"
    elif final_score >= 20:    # Was 38
        status = "related_evidence"
    elif final_score >= 10:    # Was 22
        status = "weak_or_unclear"
    else:
        status = "insufficient_evidence"
    
    return {
        "score": final_score,
        "status": status,
        "title_overlap": concept_overlap,
        "abstract_overlap": concept_overlap,
        "keyword_overlap": round(concept_ratio * 100, 1),
        "direction_overlap": round(direction_ratio * 100, 1),
        "relation_overlap": relation_overlap,
        "partial_support": partial_credit > 0,
        "claim_simplified": claim_simplified[:200],
        "concept_matches": list(claim_set & source_set)[:10]
    }


def fetch_openalex_metadata_by_doi(doi: str) -> Dict[str, Any]:
    """Fetch OpenAlex metadata including concepts."""
    if not doi:
        return {}
    
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
    
    for concept in data.get("concepts", [])[:8]:
        concept_name = concept.get("display_name", "")
        if concept_name:
            result["concepts"].append(concept_name)
    
    return result
