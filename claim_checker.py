# claim_checker.py

import re
from typing import Dict, Any, List, Set

from citation_suggester import extract_context, extract_keywords
from verify import _safe_get_json


# ---------------------------------------------------------
# Normalization dictionaries
# ---------------------------------------------------------

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

    "trust": "trust",
    "credibility": "trust",
    "confidence": "trust",

    "reliability": "reliability",
    "dependability": "reliability",
    "robustness": "reliability",
    "stability": "reliability",

    "application": "application",
    "app": "application",
    "software": "application",
    "system": "application",

    "performance": "performance",
    "productivity": "performance",
    "efficiency": "performance",

    "risk": "risk",
    "failure": "failure",
    "errors": "error",
    "error": "error",
}

DIRECTION_WORDS = {
    "improve", "reduce", "increase", "predict", "influence",
    "affect", "support", "enhance"
}

STOPWORDS_LIGHT = {
    "this", "that", "with", "from", "using", "study", "analysis",
    "method", "approach", "results", "paper", "research", "journal",
    "review", "these", "those", "their", "would", "could", "should",
    "might", "what", "when", "where", "which", "while", "there",
    "about", "into", "through", "during", "according", "significantly",
    "current", "finding", "findings"
}


# ---------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------

def extract_claim_from_citation(full_text: str, citation_text: str) -> str:
    """
    Extract a proposition-like claim using the same directional context logic.
    """
    context = extract_context(full_text, citation_text, window=220)
    if not context:
        return ""

    claim = context.strip()
    claim = re.sub(r"\s+", " ", claim).strip(" ,;:-")
    claim = re.sub(r"^(according to|as noted by|as argued by|based on)\s+", "", claim, flags=re.I)

    # trim very long claim text
    return claim[:400]


# ---------------------------------------------------------
# Source evidence retrieval
# ---------------------------------------------------------

def fetch_openalex_abstract_by_doi(doi: str) -> str:
    """
    Fetch abstract from OpenAlex using DOI.
    """
    if not doi:
        return ""

    doi = doi.replace("https://doi.org/", "").strip()
    if not doi:
        return ""

    url = f"https://api.openalex.org/works/https://doi.org/{doi}"
    data = _safe_get_json(url)

    if not data:
        return ""

    inv = data.get("abstract_inverted_index") or {}
    if not inv:
        return ""

    words = {}
    for token, positions in inv.items():
        for p in positions:
            words[p] = token

    return " ".join(words[i] for i in sorted(words))


# ---------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------

def normalize_terms(text: str) -> List[str]:
    words = re.findall(r"[A-Za-z]{3,}", (text or "").lower())
    out = []
    for w in words:
        if w in STOPWORDS_LIGHT:
            continue
        out.append(CONCEPT_MAP.get(w, w))
    return out


def concept_set(text: str) -> Set[str]:
    return set(normalize_terms(text))


def direction_set(text: str) -> Set[str]:
    return {w for w in normalize_terms(text) if w in DIRECTION_WORDS}


# ---------------------------------------------------------
# Support scoring
# ---------------------------------------------------------

def score_claim_support(claim: str, source_title: str, source_abstract: str) -> Dict[str, Any]:
    """
    Hybrid score for paraphrase-aware support assessment.
    """
    claim_concepts = concept_set(claim)
    title_concepts = concept_set(source_title)
    abstract_concepts = concept_set(source_abstract)

    claim_dirs = direction_set(claim)
    source_dirs = direction_set(source_title + " " + source_abstract)

    # Concept overlap
    title_overlap = len(claim_concepts & title_concepts)
    abstract_overlap = len(claim_concepts & abstract_concepts)
    total_claim_concepts = max(len(claim_concepts), 1)

    concept_ratio = min((abstract_overlap + title_overlap) / total_claim_concepts, 1.0)

    # Direction overlap
    direction_overlap = len(claim_dirs & source_dirs)
    direction_ratio = 1.0 if claim_dirs and direction_overlap > 0 else (0.5 if not claim_dirs else 0.0)

    # Keyword overlap
    claim_kw = set(extract_keywords(claim))
    source_kw = set(extract_keywords(source_title + " " + source_abstract))
    keyword_overlap = len(claim_kw & source_kw)
    keyword_ratio = min(keyword_overlap / max(len(claim_kw), 1), 1.0)

    # Weighted score
    score = (
        50 * concept_ratio +
        20 * direction_ratio +
        30 * keyword_ratio
    )

    score = round(min(score, 100), 1)

    if score >= 80:
        status = "strong_support"
    elif score >= 65:
        status = "moderate_support"
    elif score >= 45:
        status = "related_evidence"
    elif score >= 25:
        status = "weak_or_unclear"
    else:
        status = "insufficient_evidence"

    return {
        "score": score,
        "status": status,
        "title_overlap": title_overlap,
        "abstract_overlap": abstract_overlap,
        "keyword_overlap": keyword_overlap,
        "direction_overlap": direction_overlap,
        "concept_ratio": round(concept_ratio, 3),
        "direction_ratio": round(direction_ratio, 3),
        "keyword_ratio": round(keyword_ratio, 3),
    }


# ---------------------------------------------------------
# Build claim-support rows
# ---------------------------------------------------------

def build_claim_support_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build claim-to-source checks using verified/likely references only.
    """
    full_text = result.get("main_text", "") or result.get("full_text", "")
    verification_rows = (result.get("online_verification") or {}).get("rows", []) or []
    c2r_rows = result.get("reconciliation_intext_to_reference", []) or []

    verified_lookup = {}
    for vr in verification_rows:
        if vr.get("status") in {"verified", "likely"}:
            verified_lookup[vr.get("reference", "")] = vr

    out = []

    for row in c2r_rows:
        matched_ref = row.get("matched_reference", "") or ""
        citation_text = row.get("in_text", "") or row.get("citation", "") or row.get("citation_in_text", "") or ""

        if not matched_ref or not citation_text:
            continue

        vr = verified_lookup.get(matched_ref)
        if not vr:
            continue

        claim = extract_claim_from_citation(full_text, citation_text)
        if not claim:
            continue

        source_title = vr.get("matched_title", "") or ""
        doi = vr.get("doi", "") or ""
        source_abstract = fetch_openalex_abstract_by_doi(doi)

        if not source_title and not source_abstract:
            continue

        support = score_claim_support(claim, source_title, source_abstract)

        out.append({
            "citation": citation_text,
            "claim": claim,
            "reference": matched_ref,
            "source_title": source_title,
            "doi": doi,
            "support_score": support["score"],
            "support_status": support["status"],
            "evidence_used": "title+abstract" if source_abstract else "title_only",
            "title_overlap": support["title_overlap"],
            "abstract_overlap": support["abstract_overlap"],
            "keyword_overlap": support["keyword_overlap"],
            "direction_overlap": support["direction_overlap"],
        })

    return out
