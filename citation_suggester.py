# claim_checker.py

import re
from typing import Dict, Any, List, Set

from citation_suggester import extract_context, extract_keywords
from verify import _safe_get_json


# ---------------------------------------------------------
# Normalization dictionaries
# ---------------------------------------------------------

CONCEPT_MAP = {
    # improvement / strengthening
    "improves": "improve",
    "improve": "improve",
    "improved": "improve",
    "improving": "improve",
    "enhances": "improve",
    "enhance": "improve",
    "enhanced": "improve",
    "enhancing": "improve",
    "boosts": "improve",
    "boost": "improve",
    "boosted": "improve",
    "strengthens": "improve",
    "strengthen": "improve",
    "strengthened": "improve",
    "optimises": "improve",
    "optimizes": "improve",
    "optimise": "improve",
    "optimize": "improve",
    "facilitates": "improve",
    "facilitate": "improve",

    # reduction / mitigation
    "reduces": "reduce",
    "reduce": "reduce",
    "reduced": "reduce",
    "reducing": "reduce",
    "mitigates": "reduce",
    "mitigate": "reduce",
    "mitigated": "reduce",
    "lowers": "reduce",
    "lower": "reduce",
    "decreases": "reduce",
    "decrease": "reduce",
    "decreased": "reduce",
    "minimizes": "reduce",
    "minimize": "reduce",
    "lessens": "reduce",
    "lessen": "reduce",

    # increase / growth
    "increases": "increase",
    "increase": "increase",
    "increased": "increase",
    "increasing": "increase",
    "raises": "increase",
    "raise": "increase",
    "raised": "increase",
    "elevates": "increase",
    "elevate": "increase",
    "growth": "increase",
    "expands": "increase",
    "expand": "increase",

    # adoption / implementation / use
    "adoption": "adopt",
    "adopted": "adopt",
    "adopt": "adopt",
    "adopting": "adopt",
    "implementation": "adopt",
    "implement": "adopt",
    "implemented": "adopt",
    "implementing": "adopt",
    "use": "adopt",
    "uses": "adopt",
    "usage": "adopt",
    "utilisation": "adopt",
    "utilization": "adopt",
    "uptake": "adopt",
    "acceptance": "adopt",
    "readiness": "adopt",

    # performance / effectiveness
    "performance": "performance",
    "productivity": "performance",
    "efficiency": "performance",
    "effective": "performance",
    "effectiveness": "performance",
    "outcome": "performance",
    "outcomes": "performance",
    "success": "performance",

    # reliability / robustness
    "reliability": "reliability",
    "reliable": "reliability",
    "robustness": "reliability",
    "robust": "reliability",
    "stability": "reliability",
    "stable": "reliability",
    "consistency": "reliability",
    "consistent": "reliability",
    "accuracy": "reliability",
    "accurate": "reliability",

    # trust / credibility
    "trust": "trust",
    "credibility": "trust",
    "credible": "trust",
    "confidence": "trust",
    "confidence-building": "trust",

    # transparency / accountability
    "transparency": "transparency",
    "transparent": "transparency",
    "accountability": "transparency",
    "accountable": "transparency",
    "openness": "transparency",
    "disclosure": "transparency",

    # risk / uncertainty
    "risk": "risk",
    "risks": "risk",
    "uncertainty": "risk",
    "exposure": "risk",
    "volatility": "risk",
    "vulnerability": "risk",

    # errors / failure
    "error": "error",
    "errors": "error",
    "mistake": "error",
    "mistakes": "error",
    "failure": "failure",
    "failures": "failure",

    # ethics / corruption
    "corruption": "corruption",
    "fraud": "corruption",
    "misconduct": "corruption",
    "unethical": "corruption",
    "ethics": "ethics",
    "ethical": "ethics",
    "integrity": "ethics",
    "compliance": "ethics",

    # behaviour / intention
    "behaviour": "behaviour",
    "behavior": "behaviour",
    "intention": "intention",
    "intentions": "intention",
    "willingness": "intention",
    "attitude": "attitude",
    "attitudes": "attitude",

    # systems / applications
    "application": "application",
    "applications": "application",
    "app": "application",
    "apps": "application",
    "software": "application",
    "system": "application",
    "systems": "application",
    "platform": "application",
    "platforms": "application",

    # finance / cost
    "cost": "cost",
    "costs": "cost",
    "expense": "cost",
    "expenses": "cost",
    "price": "cost",
    "prices": "cost",
    "profitability": "profit",
    "profit": "profit",
}

DIRECTION_WORDS = {
    "improve", "reduce", "increase", "predict", "influence",
    "affect", "support", "enhance", "adopt", "associate",
    "explain", "determine"
}

STOPWORDS_LIGHT = {
    "this", "that", "with", "from", "using", "study", "analysis",
    "method", "approach", "results", "paper", "research", "journal",
    "review", "these", "those", "their", "would", "could", "should",
    "might", "what", "when", "where", "which", "while", "there",
    "about", "into", "through", "during", "according", "significantly",
    "current", "finding", "findings", "article", "articles", "author",
    "authors", "evidence", "model", "models", "framework", "frameworks",
    "effect", "effects", "role", "roles", "impact", "impacts",
    "relationship", "relationships", "factor", "factors", "based",
    "examines", "examined", "investigates", "investigated", "data",
    "report", "reports", "reported", "among", "across", "within",
    "between", "toward", "towards", "because", "therefore", "thus",
    "overall", "general", "generally", "specific", "various",
    "several", "many", "more", "less", "most", "such"
}

RELATION_PATTERNS = {
    "improve": [
        r"\bimprov\w*\b", r"\benhanc\w*\b", r"\bboost\w*\b",
        r"\bstrength\w*\b", r"\boptimi[sz]\w*\b", r"\bfacilitat\w*\b"
    ],
    "reduce": [
        r"\breduc\w*\b", r"\bmitigat\w*\b", r"\blower\w*\b",
        r"\bdecreas\w*\b", r"\bminimi[sz]\w*\b", r"\blessen\w*\b"
    ],
    "increase": [
        r"\bincreas\w*\b", r"\brais\w*\b", r"\belevat\w*\b",
        r"\bgrow\w*\b", r"\bexpand\w*\b"
    ],
    "influence": [
        r"\binfluenc\w*\b", r"\baffect\w*\b", r"\bimpact\w*\b"
    ],
    "predict": [
        r"\bpredict\w*\b", r"\bdetermin\w*\b", r"\bexplain\w*\b"
    ],
    "associate": [
        r"\bassociat\w*\b", r"\brelat\w*\b", r"\blink\w*\b",
        r"\bconnect\w*\b"
    ],
    "adopt": [
        r"\badopt\w*\b", r"\bimplement\w*\b", r"\buse\w*\b",
        r"\butili[sz]\w*\b", r"\buptake\b", r"\baccept\w*\b"
    ],
}


# ---------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------

def simplify_claim(claim: str) -> str:
    """
    Remove noisy framing language so scoring focuses on the core proposition.
    """
    claim = (claim or "").strip()

    claim = re.sub(
        r"\b(this study|this paper|the study|the findings show that|"
        r"results show that|it was found that|the authors found that|"
        r"the evidence suggests that|the results indicate that)\b",
        "",
        claim,
        flags=re.I
    )
    claim = re.sub(
        r"\b(significantly|generally|often|may|might|can|could|"
        r"appears to|tends to|likely|possibly|probably)\b",
        "",
        claim,
        flags=re.I
    )
    claim = re.sub(r"\s+", " ", claim).strip(" ,;:-")
    return claim


def extract_claim_from_citation(full_text: str, citation_text: str) -> str:
    """
    Extract a proposition-like claim using contextual citation logic.
    Requires the improved extract_context in citation_suggester.py.
    """
    context = extract_context(full_text, citation_text, window=220)
    if not context:
        return ""

    claim = context.strip()
    claim = re.sub(r"\s+", " ", claim).strip(" ,;:-")
    claim = re.sub(
        r"^(according to|as noted by|as argued by|based on|as reported by)\s+",
        "",
        claim,
        flags=re.I
    )

    claim = simplify_claim(claim)

    # trim very long claim text
    return claim[:400]


def split_citation_cluster(citation_text: str) -> List[str]:
    """
    Split clustered citations like:
    (Beck et al., 2021; Zhang et al., 2022; Patel, 2023)
    into individual citation strings.
    """
    if not citation_text:
        return []

    c = citation_text.strip()

    if c.startswith("(") and c.endswith(")"):
        inner = c[1:-1]
        parts = [p.strip() for p in re.split(r"\s*;\s*", inner) if p.strip()]
        return [f"({p})" for p in parts]

    return [c]


# ---------------------------------------------------------
# Source evidence retrieval
# ---------------------------------------------------------

def fetch_openalex_metadata_by_doi(doi: str) -> Dict[str, Any]:
    """
    Fetch metadata from OpenAlex using DOI.
    """
    if not doi:
        return {}

    doi = doi.replace("https://doi.org/", "").strip()
    if not doi:
        return {}

    url = f"https://api.openalex.org/works/https://doi.org/{doi}"
    data = _safe_get_json(url)

    return data or {}


def reconstruct_openalex_abstract(data: Dict[str, Any]) -> str:
    """
    Reconstruct abstract from OpenAlex inverted index.
    """
    inv = (data or {}).get("abstract_inverted_index") or {}
    if not inv:
        return ""

    words = {}
    for token, positions in inv.items():
        for p in positions:
            words[p] = token

    try:
        return " ".join(words[i] for i in sorted(words))
    except Exception:
        return ""


def extract_openalex_topics(data: Dict[str, Any]) -> str:
    """
    Pull concept/topic display names as fallback semantic evidence.
    """
    items = []

    for c in (data or {}).get("concepts", [])[:8]:
        name = (c or {}).get("display_name", "")
        if name:
            items.append(name)

    primary_topic = ((data or {}).get("primary_topic") or {})
    primary_name = primary_topic.get("display_name", "")
    if primary_name:
        items.append(primary_name)

    return " ".join(items).strip()


# ---------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------

def normalize_terms(text: str) -> List[str]:
    words = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", (text or "").lower())
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


def relation_hits(text: str) -> Set[str]:
    text = (text or "").lower()
    hits = set()
    for rel, patterns in RELATION_PATTERNS.items():
        for p in patterns:
            if re.search(p, text):
                hits.add(rel)
                break
    return hits


# ---------------------------------------------------------
# Support scoring
# ---------------------------------------------------------

def score_claim_support(claim: str, source_title: str, source_abstract: str, source_topics: str = "") -> Dict[str, Any]:
    """
    Hybrid score for paraphrase-aware support assessment.
    Uses concepts, direction words, relation patterns, and partial support.
    """
    claim = simplify_claim(claim)

    source_text = " ".join(
        part for part in [source_title, source_abstract, source_topics] if part
    ).strip()

    claim_concepts = concept_set(claim)
    title_concepts = concept_set(source_title)
    abstract_concepts = concept_set(source_abstract)
    topic_concepts = concept_set(source_topics)

    claim_dirs = direction_set(claim)
    source_dirs = direction_set(source_text)

    # Concept overlap
    title_overlap = len(claim_concepts & title_concepts)
    abstract_overlap = len(claim_concepts & abstract_concepts)
    topic_overlap = len(claim_concepts & topic_concepts)

    total_claim_concepts = max(len(claim_concepts), 1)

    weighted_overlap = (
        (title_overlap * 1.5) +
        (abstract_overlap * 1.0) +
        (topic_overlap * 0.8)
    )

    # softer denominator so long claims are not punished too harshly
    concept_ratio = weighted_overlap / max(min(total_claim_concepts, 4), 1)
    concept_ratio = min(concept_ratio, 1.0)

    # Direction overlap
    direction_overlap = len(claim_dirs & source_dirs)
    direction_ratio = 1.0 if claim_dirs and direction_overlap > 0 else (0.6 if not claim_dirs else 0.0)

    # Keyword overlap
    claim_kw = set(extract_keywords(claim))
    source_kw = set(extract_keywords(source_text))
    keyword_overlap = len(claim_kw & source_kw)
    keyword_ratio = min(keyword_overlap / max(min(len(claim_kw), 4), 1), 1.0)

    # Relation overlap
    claim_rels = relation_hits(claim)
    source_rels = relation_hits(source_text)
    relation_overlap = len(claim_rels & source_rels)
    relation_bonus = 10 if relation_overlap > 0 else 0

    # Partial support bonus
    partial_support_bonus = 0
    if (title_overlap + abstract_overlap + topic_overlap) >= 2:
        partial_support_bonus += 10
    if relation_overlap > 0 and (title_overlap + abstract_overlap + topic_overlap) >= 1:
        partial_support_bonus += 10

    # Weighted total
    score = (
        55 * concept_ratio +
        15 * direction_ratio +
        10 * keyword_ratio +
        relation_bonus +
        partial_support_bonus
    )

    score = round(min(score, 100), 1)

    if score >= 70:
        status = "strong_support"
    elif score >= 55:
        status = "moderate_support"
    elif score >= 38:
        status = "related_evidence"
    elif score >= 22:
        status = "weak_or_unclear"
    else:
        status = "insufficient_evidence"

    return {
        "score": score,
        "status": status,
        "title_overlap": title_overlap,
        "abstract_overlap": abstract_overlap,
        "topic_overlap": topic_overlap,
        "keyword_overlap": keyword_overlap,
        "direction_overlap": direction_overlap,
        "relation_overlap": relation_overlap,
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
    Handles clustered citations as one claim checked against multiple sources.
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

        citation_items = split_citation_cluster(citation_text)

        for cit in citation_items:
            claim = extract_claim_from_citation(full_text, cit)
            if not claim:
                continue

            source_title = vr.get("matched_title", "") or ""
            doi = vr.get("doi", "") or ""

            ox = fetch_openalex_metadata_by_doi(doi) if doi else {}
            source_abstract = reconstruct_openalex_abstract(ox)
            source_topics = extract_openalex_topics(ox)

            if not source_title and not source_abstract and not source_topics:
                continue

            support = score_claim_support(
                claim=claim,
                source_title=source_title,
                source_abstract=source_abstract,
                source_topics=source_topics,
            )

            out.append({
                "citation": cit,
                "claim": claim,
                "reference": matched_ref,
                "source_title": source_title,
                "doi": doi,
                "support_score": support["score"],
                "support_status": support["status"],
                "evidence_used": "title+abstract+topics" if (source_abstract or source_topics) else "title_only",
                "title_overlap": support["title_overlap"],
                "abstract_overlap": support["abstract_overlap"],
                "topic_overlap": support["topic_overlap"],
                "keyword_overlap": support["keyword_overlap"],
                "direction_overlap": support["direction_overlap"],
                "relation_overlap": support["relation_overlap"],
                "concept_ratio": support["concept_ratio"],
                "direction_ratio": support["direction_ratio"],
                "keyword_ratio": support["keyword_ratio"],
            })

    return out
