"""Shared classification and context-fit helpers for evidence resolution.

The module keeps claim-support and online-reference findings on one vocabulary.
It deliberately separates discovery relevance from proof that a source supports
the author's exact claim.
"""

from __future__ import annotations

from collections import Counter
import re
from typing import Any, Dict, Iterable, List, Set


VERIFIED_STATUSES = {
    "verified", "matched", "valid", "confirmed", "exact_match", "verified_exact",
}
METADATA_DIFFERENCE_STATUSES = {
    "likely", "verified_with_metadata_differences", "verified_with_differences",
    "metadata_difference", "metadata_differences", "metadata_mismatch",
}
VALID_NOT_INDEXED_STATUSES = {
    "valid_not_indexed", "valid_but_not_indexed", "not_digitally_indexed",
    "valid_offline_source", "manual_valid",
}
POSSIBLE_MATCH_STATUSES = {
    "possible", "possible_match", "needs_review", "manual_review",
    "review_required", "source_needs_review", "ambiguous", "unverified",
}
NOT_FOUND_STATUSES = {
    "not_found", "metadata_not_found", "no_match", "no_record", "unmatched",
}
LOOKUP_FAILED_STATUSES = {
    "failed", "lookup_failed", "verification_failed", "offline", "error",
    "timeout", "timed_out", "unavailable", "service_unavailable", "unknown",
}
IDENTITY_CONFLICT_STATUSES = {
    "identity_conflict", "reference_identity_conflict", "serious_identity_conflict",
    "doi_title_conflict", "title_author_conflict",
}


CLAIM_DIRECT_STATUSES = {
    "strong", "strong_support", "direct_support", "supported", "verified_support",
}
CLAIM_PARTIAL_STATUSES = {
    "moderate", "moderate_support", "related_evidence", "partial_support",
}
CLAIM_WEAK_STATUSES = {
    "weak", "weak_support", "weak_or_unclear", "unclear", "source_needs_review",
    "unverified", "failed",
}
CLAIM_INCOMPLETE_STATUSES = {
    "mapping_incomplete", "incomplete_mapping", "no_mapping", "no_source",
    "not_mapped", "unmapped",
}
CLAIM_UNSUPPORTED_STATUSES = {
    "insufficient_evidence", "insufficient", "no_support", "unsupported",
    "contradicted", "contradictory",
}


_STOPWORDS = {
    "about", "after", "again", "against", "also", "among", "because", "been",
    "before", "being", "between", "both", "could", "does", "during", "each",
    "from", "have", "into", "more", "most", "other", "over", "same", "should",
    "such", "than", "that", "their", "there", "these", "they", "this", "those",
    "through", "under", "using", "very", "were", "what", "when", "where", "which",
    "while", "with", "would", "study", "studies", "research", "paper", "article",
    "results", "result", "finding", "findings", "analysis", "method", "methods",
    "claim", "claims", "source", "sources", "citation", "evidence", "support",
    "abstract", "keyword", "keywords", "objective", "objectives", "question",
    "questions", "introduction", "background", "discussion", "conclusion",
}

_GENERIC_OVERLAP_TERMS = {
    "academic", "access", "approach", "change", "development", "effect",
    "general", "impact", "improvement", "information", "issue", "management",
    "performance", "policy", "practice", "process", "quality", "reform",
    "relationship", "service", "system", "technology", "use", "work",
}

_GEOGRAPHIC_TERMS = {
    "africa", "sub-saharan africa", "west africa", "east africa", "southern africa",
    "ghana", "nigeria", "kenya", "south africa", "ethiopia", "uganda", "tanzania",
    "rwanda", "zambia", "zimbabwe", "botswana", "namibia", "malawi", "senegal",
    "gambia", "sierra leone", "liberia", "cameroon", "cote d'ivoire", "ivory coast",
    "burkina faso", "togo", "benin", "niger", "mali", "morocco", "egypt",
    "united kingdom", "uk", "united states", "usa", "canada", "australia",
    "new zealand", "india", "china", "japan", "pakistan", "bangladesh",
    "indonesia", "malaysia", "singapore", "europe", "european union", "asia",
    "latin america", "caribbean", "middle east", "global", "international",
    "accra", "kumasi", "takoradi", "sekondi-takoradi", "tamale", "cape coast",
    "greater accra", "ashanti region", "western region", "central region",
}

_POPULATION_NOUNS = {
    "adolescents", "adults", "children", "citizens", "clients", "communities",
    "employees", "farmers", "firms", "households", "judges", "lawyers",
    "managers", "nurses", "patients", "practitioners", "pupils", "respondents",
    "students", "teachers", "women", "workers", "youth", "courts", "hospitals",
    "schools", "universities", "institutions", "organisations", "organizations",
}

_DISCIPLINE_LEXICONS = {
    "law and justice": {"court", "courts", "judicial", "justice", "legal", "law", "judge", "litigation"},
    "health and medicine": {"health", "hospital", "clinical", "patient", "disease", "treatment", "nurse", "medical"},
    "education": {"education", "student", "teacher", "school", "university", "learning", "curriculum", "teaching"},
    "business and management": {"business", "employee", "firm", "management", "organisation", "organization", "performance", "market"},
    "public administration and policy": {"government", "public", "policy", "administration", "governance", "institution", "service", "reform"},
    "social sciences": {"social", "community", "household", "population", "survey", "behaviour", "behavior", "society"},
    "technology and information systems": {"digital", "technology", "information", "system", "software", "data", "platform", "automation"},
}

_RELATIONSHIP_PATTERNS = {
    "causal_or_effect": r"\b(?:affect(?:s|ed)?|cause(?:s|d)?|drive(?:s|n)?|influence(?:s|d)?|lead(?:s)?\s+to|result(?:s|ed)?\s+in|increase(?:s|d)?|reduce(?:s|d)?|improve(?:s|d)?|worsen(?:s|ed)?|determin(?:e|es|ed))\b",
    "association": r"\b(?:associat(?:e|ed|ion)|correlat(?:e|ed|ion)|relat(?:e|ed|ionship)|link(?:s|ed)?|predict(?:s|ed)?)\b",
    "comparison": r"\b(?:higher|lower|greater|less|more|difference|differ(?:s|ed)?|compar(?:e|ed|ison))\b",
    "prevalence_or_frequency": r"\b(?:prevalence|incidence|rate|frequency|proportion|percentage|majority|minority)\b",
    "descriptive_or_process": r"\b(?:describe(?:s|d)?|implement(?:s|ed|ation)|adopt(?:s|ed|ion)|experience(?:s|d)?|perceiv(?:e|ed)|report(?:s|ed)?)\b",
}

_RELATIONSHIP_WORDS = {
    "affect", "affects", "affected", "association", "associated", "cause",
    "causes", "caused", "correlation", "correlated", "determine", "determines",
    "determined", "difference", "effect", "effects", "greater", "higher",
    "impact", "impacts", "improve", "improves", "improved", "increase",
    "increases", "increased", "influence", "influences", "influenced", "link",
    "linked", "lower", "predict", "predicts", "predicted", "reduce", "reduces",
    "reduced", "relationship", "result", "results", "worsen", "worsens",
}


def _slug(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def normalise_verification_status(row_or_status: Any) -> str:
    """Return one fair, user-facing reference verification status."""
    if isinstance(row_or_status, dict):
        raw = (
            row_or_status.get("canonical_status") or row_or_status.get("status") or
            row_or_status.get("verification_status") or row_or_status.get("state") or
            row_or_status.get("result") or "unknown"
        )
    else:
        raw = row_or_status
    status = _slug(raw) or "unknown"
    if status in VERIFIED_STATUSES:
        return "verified"
    if status in METADATA_DIFFERENCE_STATUSES:
        return "verified_with_metadata_differences"
    if status in VALID_NOT_INDEXED_STATUSES:
        return "valid_but_not_digitally_indexed"
    if status in POSSIBLE_MATCH_STATUSES:
        return "possible_match_human_review"
    if status in NOT_FOUND_STATUSES:
        return "not_found"
    if status in LOOKUP_FAILED_STATUSES:
        return "lookup_failed"
    if status in IDENTITY_CONFLICT_STATUSES:
        return "serious_identity_conflict"
    # Unknown external states must be reviewed rather than silently treated as
    # verified or fabricated.
    return "possible_match_human_review"


def verification_requires_resolution(row_or_status: Any) -> bool:
    return normalise_verification_status(row_or_status) not in {
        "verified", "valid_but_not_digitally_indexed",
    }


def verification_status_explanation(row_or_status: Any) -> str:
    status = normalise_verification_status(row_or_status)
    return {
        "verified": "The external record matched the reference with sufficient confidence.",
        "verified_with_metadata_differences": "A likely record was found, but one or more bibliographic fields differ and need review.",
        "valid_but_not_digitally_indexed": "The student confirmed that the source is valid even though no suitable digital index record was found.",
        "possible_match_human_review": "A possible record or unresolved result was found. Human verification is required.",
        "not_found": "No suitable indexed record was found. This does not mean the source is fabricated.",
        "lookup_failed": "The online lookup did not complete successfully. Retry or verify the source manually.",
        "serious_identity_conflict": "The returned metadata may describe a different publication.",
    }[status]


def normalise_claim_support_status(row_or_status: Any) -> str:
    if isinstance(row_or_status, dict):
        raw = row_or_status.get("canonical_support_status") or row_or_status.get("support_status") or row_or_status.get("status") or ""
    else:
        raw = row_or_status
    status = _slug(raw)
    if status in CLAIM_DIRECT_STATUSES:
        return "direct_support"
    if status in CLAIM_PARTIAL_STATUSES:
        return "partial_support"
    if status in CLAIM_WEAK_STATUSES:
        return "weak_or_unclear"
    if status in CLAIM_INCOMPLETE_STATUSES:
        return "mapping_incomplete"
    if status in CLAIM_UNSUPPORTED_STATUSES:
        return "insufficient_evidence"
    return status or "mapping_incomplete"


def claim_requires_resolution(row_or_status: Any) -> bool:
    return normalise_claim_support_status(row_or_status) in {
        "weak_or_unclear", "mapping_incomplete", "insufficient_evidence",
    }


def _tokens(text: Any, limit: int = 80) -> List[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'’-]{2,}", str(text or "").casefold())
    out: List[str] = []
    seen: Set[str] = set()
    for word in words:
        word = word.strip("-'’")
        if len(word) < 4 or word in _STOPWORDS or word in seen:
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= limit:
            break
    return out


def _looks_like_heading(line: str) -> bool:
    clean = re.sub(r"\s+", " ", str(line or "")).strip()
    return bool(
        1 <= len(clean.split()) <= 14
        and (
            clean.isupper()
            or re.match(
                r"^(?:chapter\s+\w+|\d+(?:\.\d+)*\.?\s+)?(?:abstract|keywords?|introduction|background|literature|research objectives?|research questions?|methodology|methods?|results?|findings?|discussion|conclusion|recommendations?|references)\b",
                clean,
                re.I,
            )
        )
    )


def _labelled_block(raw: str, label_pattern: str, max_chars: int = 5000) -> str:
    lines = str(raw or "").splitlines()
    for index, line in enumerate(lines[:500]):
        match = re.match(rf"^\s*(?:\d+(?:\.\d+)*\s*)?(?:{label_pattern})\s*[:.-]?\s*(.*)$", line, re.I)
        if not match:
            continue
        collected = [match.group(1).strip()] if match.group(1).strip() else []
        for next_line in lines[index + 1:index + 80]:
            if _looks_like_heading(next_line):
                break
            clean = re.sub(r"\s+", " ", next_line).strip()
            if clean:
                collected.append(clean)
            if sum(len(part) for part in collected) >= max_chars:
                break
        return re.sub(r"\s+", " ", " ".join(collected)).strip()[:max_chars]
    return ""


def _extract_geography(text: str) -> List[str]:
    lowered = re.sub(r"\s+", " ", str(text or "").casefold())
    found = {
        term for term in _GEOGRAPHIC_TERMS
        if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lowered)
    }
    explicit_patterns = (
        r"\b(?:conducted|undertaken|carried out|located|based)\s+in\s+([A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){0,4})",
        r"\bstudy\s+(?:area|setting|site)\s+(?:was|is)?\s*(?:in|within|at)\s+([A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){0,4})",
    )
    for pattern in explicit_patterns:
        for match in re.finditer(pattern, str(text or "")):
            value = re.sub(r"\s+", " ", match.group(1)).strip(" ,.;")
            if 2 <= len(value) <= 80:
                found.add(value.casefold())
    return sorted(found)


def _extract_population_and_setting(text: str) -> Dict[str, List[str]]:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    lowered = raw.casefold()
    populations = sorted({noun for noun in _POPULATION_NOUNS if re.search(rf"\b{re.escape(noun)}\b", lowered)})
    phrases: List[str] = []
    for pattern in (
        r"\b(?:among|involving|sample of|population of|participants were|respondents were)\s+([^.;:?]{3,90})",
        r"\b(?:study|research)\s+(?:population|participants|respondents)\s+(?:comprised|included|were|consisted of)\s+([^.;:?]{3,90})",
    ):
        for match in re.finditer(pattern, raw, re.I):
            phrase = re.split(r"\b(?:using|who|that|where|which)\b", re.sub(r"\s+", " ", match.group(1)), maxsplit=1, flags=re.I)[0].strip(" ,")
            if phrase and len(phrase.split()) <= 14 and phrase.casefold() not in {p.casefold() for p in phrases}:
                phrases.append(phrase[:100])
            if len(phrases) >= 8:
                break
    settings: List[str] = []
    for pattern in (
        r"\b(?:at|within|across|in)\s+(?:the\s+)?([^.;:]{2,80}\b(?:courts?|hospitals?|schools?|universit(?:y|ies)|districts?|regions?|communities|industry|sector|organisations?|organizations?|institutions?))",
        r"\b(?:study|research)\s+(?:setting|site|area)\s+(?:was|is|included)?\s*([^.;:]{3,90})",
    ):
        for match in re.finditer(pattern, raw, re.I):
            value = re.sub(r"\s+", " ", match.group(1)).strip(" ,")
            if (
                value
                and len(value.split()) <= 12
                and not re.search(r"\b(?:abstract|introduction|objective|question|keyword|method|result)\b", value, re.I)
                and value.casefold() not in {s.casefold() for s in settings}
            ):
                settings.append(value[:100])
            if len(settings) >= 8:
                break
    return {"population_terms": populations[:20], "population_phrases": phrases[:8], "settings": settings[:8]}


def _infer_discipline(text: str, explicit: str = "") -> List[str]:
    if str(explicit or "").strip():
        return [re.sub(r"\s+", " ", str(explicit)).strip()[:100]]
    terms = Counter(_tokens(text, limit=500))
    scored = []
    for discipline, lexicon in _DISCIPLINE_LEXICONS.items():
        score = sum(terms.get(term, 0) for term in lexicon)
        if score:
            scored.append((score, discipline))
    return [discipline for _score, discipline in sorted(scored, reverse=True)[:2]]


def build_document_topic_profile(
    text: str,
    metadata: Dict[str, Any] | None = None,
    limit: int = 24,
) -> Dict[str, Any]:
    """Build an explainable manuscript profile without requiring an AI API."""
    raw = str(text or "")
    metadata = metadata or {}
    summary = metadata.get("summary") if isinstance(metadata.get("summary"), dict) else {}
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines() if line.strip()]
    title = str(
        metadata.get("document_title") or metadata.get("title") or summary.get("document_title") or ""
    ).strip()
    if not title:
        title = next((line for line in lines[:20] if 3 <= len(line.split()) <= 30 and not _looks_like_heading(line)), "")
    abstract = str(metadata.get("abstract") or _labelled_block(raw, "abstract", 6000)).strip()
    objectives = str(
        metadata.get("research_objectives") or metadata.get("objectives")
        or _labelled_block(raw, r"(?:research\s+)?objectives?", 3500)
    ).strip()
    questions = str(
        metadata.get("research_questions") or metadata.get("questions")
        or _labelled_block(raw, r"research\s+questions?", 3500)
    ).strip()
    keyword_value = metadata.get("keywords") or summary.get("keywords") or _labelled_block(raw, r"key\s*words?", 800)
    if isinstance(keyword_value, list):
        keywords = [str(value).strip() for value in keyword_value if str(value).strip()]
    else:
        keywords = [part.strip() for part in re.split(r"[,;|]", str(keyword_value or "")) if part.strip()]

    headings = []
    for line in lines[:500]:
        if _looks_like_heading(line):
            headings.append(line[:140])
        if len(headings) >= 24:
            break

    profile_source = " ".join([
        (title + " ") * 4,
        (abstract + " ") * 3,
        (" ".join(keywords) + " ") * 4,
        (objectives + " ") * 2,
        (questions + " ") * 2,
        raw[:18000],
        raw[-5000:] if len(raw) > 23000 else "",
    ])
    tokens = [
        word.strip("-'’")
        for word in re.findall(r"[A-Za-z][A-Za-z'’-]{2,}", profile_source.casefold())
        if len(word.strip("-'’")) >= 4 and word.strip("-'’") not in _STOPWORDS
    ][:8000]
    frequencies = Counter(tokens)
    ordered = [term for term, _count in frequencies.most_common(limit)]
    explicit_population_setting = " ".join(str(metadata.get(key) or "") for key in (
        "population", "participants", "respondents", "setting", "study_setting",
    ))
    population = _extract_population_and_setting(" ".join([
        abstract, objectives, questions, explicit_population_setting, raw[:16000],
    ]))
    explicit_geography = " ".join(str(metadata.get(key) or "") for key in ("country", "geography", "location", "study_area"))
    geography = _extract_geography(" ".join([title, abstract, objectives, questions, explicit_geography, raw[:16000]]))
    explicit_discipline = metadata.get("discipline") or summary.get("discipline") or ""
    return {
        "title": title[:500],
        "abstract": abstract[:6000],
        "research_objectives": objectives[:3500],
        "research_questions": questions[:3500],
        "keywords": keywords[:30],
        "disciplines": _infer_discipline(profile_source, str(explicit_discipline or "")),
        "geography": geography,
        **population,
        "terms": ordered,
        "headings": headings,
        "method": "structured_title_abstract_objective_question_keyword_context_profile",
        "ai_api_required": False,
    }


def _relationship_family(text: str) -> str:
    for label, pattern in _RELATIONSHIP_PATTERNS.items():
        if re.search(pattern, str(text or ""), re.I):
            return label
    return "descriptive_or_unspecified"


def _required_evidence_type(claim: str, relationship: str) -> str:
    lowered = str(claim or "").casefold()
    if re.search(r"\b(?:law|act|regulation|constitution|judgment|case law|policy)\b", lowered):
        return "primary legal, policy or official institutional source"
    if relationship == "causal_or_effect":
        return "causal, longitudinal, experimental, quasi-experimental or strong synthesis evidence"
    if relationship == "prevalence_or_frequency" or re.search(r"\d+(?:\.\d+)?\s*%", lowered):
        return "population-based quantitative evidence or official statistics"
    if re.search(r"\b(?:experience|perception|view|barrier|theme)\b", lowered):
        return "qualitative or mixed-method evidence from the stated population and setting"
    if re.search(r"\b(?:define|definition|theory|framework|model)\b", lowered):
        return "primary conceptual, theoretical or authoritative source"
    return "empirical or authoritative evidence matching the claim scope"


def _nearby_citations(text: str) -> List[str]:
    raw = str(text or "")
    matches = re.findall(r"\([^()]{0,140}\b(?:19|20)\d{2}[a-z]?\b[^()]{0,80}\)", raw, re.I)
    matches += re.findall(r"\b[A-Z][A-Za-z'’.-]+(?:\s+(?:and|&|et\s+al\.?))?\s*\((?:19|20)\d{2}[a-z]?\)", raw)
    matches += re.findall(r"\[(?:\d+[–—,-]?\s*)+\]", raw)
    deduped = []
    for match in matches:
        clean = re.sub(r"\s+", " ", match).strip()
        if clean and clean.casefold() not in {row.casefold() for row in deduped}:
            deduped.append(clean)
    return deduped[:12]


def build_claim_fingerprint(
    claim: str,
    surrounding_context: str = "",
    document_profile: Dict[str, Any] | None = None,
    current_section: str = "",
) -> Dict[str, Any]:
    """Represent the exact evidentiary needs of one claim."""
    document_profile = document_profile or {}
    claim_text = re.sub(r"\s+", " ", str(claim or "")).strip()
    context_text = re.sub(r"\s+", " ", str(surrounding_context or "")).strip()
    combined = f"{claim_text} {context_text}".strip()
    claim_without_citations = claim_text
    for citation in _nearby_citations(claim_text):
        claim_without_citations = claim_without_citations.replace(citation, " ")
    relationship = _relationship_family(claim_without_citations)
    location = _extract_geography(combined)
    population = _extract_population_and_setting(combined)
    population_terms = population["population_terms"]
    excluded = set(location) | set(population_terms) | _RELATIONSHIP_WORDS
    concepts = [term for term in _tokens(claim_without_citations, limit=35) if term not in excluded and not re.fullmatch(r"(?:19|20)\d{2}", term)]
    time_source = claim_without_citations
    time_period = re.findall(r"\b(?:18|19|20)\d{2}[a-z]?(?:\s*[–—-]\s*(?:18|19|20)?\d{2}[a-z]?)?\b", time_source, re.I)
    time_period += re.findall(r"\b(?:currently|current|recent(?:ly)?|historical(?:ly)?|over the past \d+ years?|between \d{4} and \d{4}|since \d{4})\b", time_source, re.I)
    profile_terms = set(str(term).casefold() for term in document_profile.get("terms") or [])
    section = str(current_section or "").strip()
    if not section:
        section = next((heading for heading in document_profile.get("headings") or [] if str(heading).casefold() in context_text.casefold()), "")
    anchors = [term for term in concepts if term not in _GENERIC_OVERLAP_TERMS]
    section_terms = _tokens(section, limit=8)
    context_anchor_terms = [
        term for term in _tokens(context_text, limit=80)
        if term in profile_terms and term not in concepts and term not in _GENERIC_OVERLAP_TERMS
    ][:12]
    search_terms = []
    for value in population_terms[:3] + anchors[:7] + location[:2]:
        for term in _tokens(value, limit=5):
            if term not in search_terms:
                search_terms.append(term)
    if relationship != "descriptive_or_unspecified":
        search_terms.append(relationship.replace("_", " "))
    return {
        "claim": claim_text,
        "subject_or_population": population_terms[:12] or population["population_phrases"][:5],
        "concepts_or_variables": concepts[:18],
        "specific_anchor_terms": anchors[:14],
        "claimed_relationship": relationship,
        "location": location,
        "time_period": list(dict.fromkeys(str(value) for value in time_period))[:10],
        "required_evidence_type": _required_evidence_type(claim_without_citations, relationship),
        "nearby_citations": _nearby_citations(context_text),
        "current_section": section,
        "current_section_terms": section_terms,
        "nearby_context_anchor_terms": context_anchor_terms,
        "manuscript_disciplines": document_profile.get("disciplines") or [],
        "manuscript_settings": document_profile.get("settings") or [],
        "manuscript_geography": document_profile.get("geography") or [],
        "manuscript_population": document_profile.get("population_terms") or [],
        "manuscript_keywords": document_profile.get("keywords") or [],
        "research_objective_terms": _tokens(document_profile.get("research_objectives") or "", limit=16),
        "research_question_terms": _tokens(document_profile.get("research_questions") or "", limit=16),
        "manuscript_topic_terms": sorted(set(concepts) & profile_terms)[:14],
        "search_query": " ".join(search_terms[:14]),
        "method": "structured_claim_fingerprint",
        "ai_api_required": False,
    }


def _candidate_evidence_type_matches(required: str, candidate_text: str, publication_type: str) -> bool:
    required = str(required or "").casefold()
    candidate = f"{candidate_text} {publication_type}".casefold()
    if "legal" in required or "policy" in required:
        return bool(re.search(r"\b(?:law|legal|policy|government|official|regulation|court|judicial)\b", candidate))
    if "causal" in required:
        return bool(re.search(r"\b(?:experiment|quasi-experiment|longitudinal|panel data|difference-in-differences|randomi[sz]ed|systematic review|meta-analysis|causal)\b", candidate))
    if "quantitative" in required or "statistics" in required:
        return bool(re.search(r"\b(?:survey|census|sample|quantitative|prevalence|incidence|regression|statistical|administrative data)\b", candidate))
    if "qualitative" in required:
        return bool(re.search(r"\b(?:qualitative|interview|focus group|thematic|mixed method|ethnograph)\b", candidate))
    if "conceptual" in required or "theoretical" in required:
        return bool(re.search(r"\b(?:theory|theoretical|conceptual|framework|review)\b", candidate))
    return bool(candidate.strip())


def assess_candidate_context_fit(
    claim: str,
    surrounding_context: str,
    candidate: Dict[str, Any],
    document_profile: Dict[str, Any] | None = None,
    claim_fingerprint: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Compare candidate metadata/abstract with a structured claim fingerprint."""
    document_profile = document_profile or {}
    fingerprint = claim_fingerprint or build_claim_fingerprint(claim, surrounding_context, document_profile)
    abstract = str(candidate.get("abstract_excerpt") or "").strip()
    abstract_usable = len(_tokens(abstract, limit=20)) >= 12
    candidate_text = " ".join(str(candidate.get(key) or "") for key in (
        "title", "abstract_excerpt", "journal", "publisher", "concepts",
    ))
    candidate_terms = set(_tokens(candidate_text, limit=180))
    concept_terms = set(str(term).casefold() for term in fingerprint.get("concepts_or_variables") or [])
    subject_terms = set(_tokens(" ".join(str(term) for term in fingerprint.get("subject_or_population") or []), limit=30))
    topic_terms = set(str(term).casefold() for term in document_profile.get("terms") or [])
    location_terms = set(str(term).casefold() for term in fingerprint.get("location") or [])
    time_terms = set(str(term).casefold() for term in fingerprint.get("time_period") or [])
    section_terms = set(str(term).casefold() for term in fingerprint.get("current_section_terms") or [])
    discipline_terms = set(_tokens(" ".join(str(value) for value in fingerprint.get("manuscript_disciplines") or []), limit=20))
    manuscript_population_terms = set(str(term).casefold() for term in fingerprint.get("manuscript_population") or [])
    manuscript_geography_terms = set(str(term).casefold() for term in fingerprint.get("manuscript_geography") or [])
    concept_overlap = sorted(concept_terms & candidate_terms)
    subject_overlap = sorted(subject_terms & candidate_terms)
    topic_overlap = sorted(topic_terms & candidate_terms)
    location_overlap = sorted(term for term in location_terms if term in candidate_text.casefold())
    time_overlap = sorted(term for term in time_terms if term in candidate_text.casefold())
    section_overlap = sorted(section_terms & candidate_terms)
    discipline_overlap = sorted(discipline_terms & candidate_terms)
    manuscript_population_overlap = sorted(manuscript_population_terms & candidate_terms)
    manuscript_geography_overlap = sorted(term for term in manuscript_geography_terms if term in candidate_text.casefold())
    specific_overlap = [term for term in concept_overlap if term not in _GENERIC_OVERLAP_TERMS]
    claim_relationship = str(fingerprint.get("claimed_relationship") or "descriptive_or_unspecified")
    candidate_relationship = _relationship_family(candidate_text)
    relationship_match = (
        claim_relationship == "descriptive_or_unspecified"
        or claim_relationship == candidate_relationship
    )
    evidence_type_match = _candidate_evidence_type_matches(
        fingerprint.get("required_evidence_type") or "",
        candidate_text,
        str(candidate.get("publication_type") or candidate.get("type") or ""),
    )
    metadata_relevance = int(candidate.get("relevance") or candidate.get("match_score") or 0)
    concept_ratio = len(concept_overlap) / max(1, min(len(concept_terms), 10))
    subject_ratio = len(subject_overlap) / max(1, min(len(subject_terms), 5)) if subject_terms else 1.0
    topic_ratio = len(topic_overlap) / max(1, min(len(topic_terms), 12))
    score = round(min(100,
        concept_ratio * 40
        + subject_ratio * 12
        + (16 if relationship_match else 0)
        + (12 if not location_terms or location_overlap else 0)
        + (8 if evidence_type_match else 0)
        + topic_ratio * 7
        + min(4, len(section_overlap) * 2)
        + min(4, len(discipline_overlap))
        + min(4, len(manuscript_population_overlap) * 2)
        + min(4, len(manuscript_geography_overlap) * 2)
        + min(metadata_relevance, 100) * .05
    ))
    general_keyword_only = bool(concept_overlap) and not specific_overlap
    rejection_reasons = []
    if len(specific_overlap) < 2:
        rejection_reasons.append("Fewer than two specific claim concepts appear in the candidate metadata or abstract.")
    if general_keyword_only:
        rejection_reasons.append("The overlap is limited to broad or generic keywords.")
    if location_terms and not location_overlap:
        rejection_reasons.append("The claim's geographical scope was not found in the candidate metadata or abstract.")
    if not relationship_match:
        rejection_reasons.append("The candidate does not describe the same claimed relationship.")
    if not abstract_usable:
        rejection_reasons.append("No usable abstract was available for structured claim comparison.")
    if score < 70:
        rejection_reasons.append("The structured context-fit score is below 70/100.")
    passes_gate = not rejection_reasons
    if score >= 85 and passes_gate:
        label = "strong_structured_context_fit"
    elif passes_gate:
        label = "possible_structured_context_fit"
    elif score >= 55:
        label = "weak_or_incomplete_context_fit"
    else:
        label = "out_of_scope_or_general_keyword_match"
    return {
        "score": score,
        "label": label,
        "passes_context_gate": passes_gate,
        "general_keyword_only": general_keyword_only,
        "claim_overlap_terms": concept_overlap[:14],
        "specific_claim_overlap_terms": specific_overlap[:12],
        "document_topic_overlap_terms": topic_overlap[:10],
        "field_matches": {
            "subject_or_population": subject_overlap,
            "concepts_or_variables": concept_overlap,
            "claimed_relationship": relationship_match,
            "candidate_relationship": candidate_relationship,
            "location": location_overlap,
            "time_period": time_overlap,
            "required_evidence_type": evidence_type_match,
            "current_section": section_overlap,
            "discipline": discipline_overlap,
            "manuscript_population": manuscript_population_overlap,
            "manuscript_geography": manuscript_geography_overlap,
        },
        "claim_fingerprint": fingerprint,
        "candidate_abstract_compared": abstract_usable,
        "candidate_text_available": bool(candidate_text.strip()),
        "rejection_reasons": rejection_reasons,
        "support_decision": "not_assessed_from_metadata",
        "explanation": (
            "The candidate passed the structured topic, population, relationship and scope screen. Open the full source and confirm the exact evidence."
            if passes_gate else
            "The candidate was withheld because it did not satisfy the structured claim-context gate."
        ),
        "warning": "Context fit is a discovery screen, not proof that the source supports the claim.",
    }


EVIDENCE_GROUP_LABELS = {
    "unsupported_claims": "Unsupported claims",
    "incomplete_mappings": "Incomplete mappings",
    "weak_support": "Weak or unclear support",
    "missing_references": "Missing references",
    "unresolved_verification": "Unresolved online verification",
    "incomplete_metadata": "Incomplete reference metadata",
    "metadata_conflicts": "Metadata conflicts",
    "uncited_references": "Uncited references",
}


def evidence_group_for_item(item: Dict[str, Any]) -> str:
    category = str(item.get("category") or "")
    if category == "citation_needed":
        return "unsupported_claims"
    if category == "claim_support":
        status = normalise_claim_support_status(item.get("supporting_metadata") or {})
        if status == "mapping_incomplete":
            return "incomplete_mappings"
        if status == "weak_or_unclear":
            return "weak_support"
        return "unsupported_claims"
    return {
        "missing_reference": "missing_references",
        "source_verification": "unresolved_verification",
        "reference_incomplete": "incomplete_metadata",
        "reference_identity_conflict": "metadata_conflicts",
        "uncited_reference": "uncited_references",
    }.get(category, "")


def build_evidence_resolution_workspace(items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the single paid correction workspace from correction-plan items."""
    entries: List[Dict[str, Any]] = []
    grouped: Dict[str, List[Dict[str, Any]]] = {key: [] for key in EVIDENCE_GROUP_LABELS}
    for item in items:
        group = evidence_group_for_item(item)
        if not group:
            continue
        item["evidence_group"] = group
        item["evidence_group_label"] = EVIDENCE_GROUP_LABELS[group]
        item["source_search_available"] = "find_source" in (item.get("available_actions") or [])
        decision = str(item.get("decision") or "pending")
        item["resolution_state"] = {
            "pending": "needs_action",
            "accepted": "approved_for_track_changes",
            "resolved": "resolved_without_manuscript_change",
            "rejected": "rejected_by_user",
            "ignored": "ignored_by_user",
        }.get(decision, decision)
        grouped[group].append(item)
        entries.append(item)
    groups = []
    for key, label in EVIDENCE_GROUP_LABELS.items():
        rows = grouped[key]
        if not rows:
            continue
        pending = sum(str(row.get("decision") or "pending") == "pending" for row in rows)
        groups.append({
            "key": key,
            "label": label,
            "total": len(rows),
            "pending": pending,
            "resolved": len(rows) - pending,
            "item_ids": [row.get("id") for row in rows],
        })
    return {
        "feature": "Evidence Resolution Workspace",
        "central_paid_product": True,
        "headline": "Resolve every evidence problem in one place, then download approved changes for Word review.",
        "disclaimer": "Not found does not mean fabricated. Candidate discovery does not prove that a source supports a claim.",
        "counts": {
            "total": len(entries),
            "pending": sum(str(row.get("decision") or "pending") == "pending" for row in entries),
            "resolved": sum(str(row.get("decision") or "pending") != "pending" for row in entries),
        },
        "groups": groups,
        "item_ids": [row.get("id") for row in entries],
        "workflow": [
            {"step": 1, "name": "Review problem"},
            {"step": 2, "name": "Find or verify evidence"},
            {"step": 3, "name": "Approve, revise, reject or ignore"},
            {"step": 4, "name": "Download Track Changes"},
        ],
    }
