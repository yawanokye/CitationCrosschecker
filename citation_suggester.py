# citation_suggester.py

import re
from typing import List, Dict, Any, Tuple

from verify import (
    _query_crossref,
    _query_openalex,
    _candidate_fields,
    _score,
    _extract_fields_by_style,
)

# ============================================================
# HELPER FUNCTIONS FOR ROBUST CONTEXT EXTRACTION
# ============================================================

_YEAR_RE = r"(?:19|20)\d{2}[a-z]?"
_SENT_BOUNDARY_RE = r"[.!?;]"

def _norm_ws(s: str) -> str:
    """Normalize whitespace in a string."""
    return re.sub(r"\s+", " ", (s or "")).strip()

def _extract_author_year_bits(citation: str):
    """Extract author surname and year from citation for fallback matching."""
    citation = citation or ""
    years = re.findall(_YEAR_RE, citation)
    year = years[0] if years else ""

    # crude author token extraction, keeps likely surnames
    tokens = re.findall(r"[A-Z][a-zA-Z'`-]{2,}", citation)
    stop = {"And", "Et", "Al"}
    authors = [t for t in tokens if t not in stop]

    surname = authors[0] if authors else ""
    return surname, year

def _find_sentence_span(text: str, pos: int):
    """Find the sentence boundaries around a position in text."""
    if pos < 0:
        return (0, len(text))
    left = text.rfind(".", 0, pos)
    left_q = text.rfind("?", 0, pos)
    left_e = text.rfind("!", 0, pos)
    left_s = text.rfind(";", 0, pos)
    left = max(left, left_q, left_e, left_s)
    left = 0 if left == -1 else left + 1

    candidates = [p for p in [
        text.find(".", pos),
        text.find("?", pos),
        text.find("!", pos),
        text.find(";", pos),
    ] if p != -1]
    right = min(candidates) + 1 if candidates else len(text)
    return left, right

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

    # fallback, keep as single citation
    return [c]

# ============================================================
# CORE FUNCTIONS
# ============================================================

def extract_citation_author_year(citation: str) -> Tuple[List[str], str]:
    """
    Extract author surname(s) and year from a citation text.
    
    Args:
        citation: The citation text (e.g., "(Beck et al., 2021)" or "Beck et al. (2021)")
    
    Returns:
        Tuple of (list of author surnames, year string)
    """
    citation = (citation or "").strip()
    
    # Extract year
    year_match = re.search(r"\b((?:19|20)\d{2}[a-z]?)\b", citation)
    year = year_match.group(1) if year_match else ""
    
    # Remove year and brackets for author extraction
    left = re.sub(r"\b(?:19|20)\d{2}[a-z]?\b", "", citation)
    left = re.sub(r"[\(\)]", " ", left)
    left = re.sub(r"\bet\s+al\.?\b", "", left, flags=re.I)
    left = left.replace("&", " and ")
    
    authors = []
    # Split by common separators
    parts = re.split(r"\band\b|,|;", left, flags=re.I)
    for p in parts:
        p = p.strip()
        if not p:
            continue
        toks = p.split()
        if toks:
            # Take the last token as surname
            surname = re.sub(r"[^A-Za-z'\-]", "", toks[-1]).lower()
            if len(surname) >= 2:
                authors.append(surname)
    
    # Deduplicate while preserving order
    seen = set()
    out = []
    for a in authors:
        if a not in seen:
            seen.add(a)
            out.append(a)
    
    return out[:2], year  # Return at most 2 authors


def extract_context(text: str, citation: str, window: int = 400) -> str:
    """
    AGGRESSIVE claim extraction for narrative citations.
    For narrative citations (Author Year verb...), returns EVERYTHING after the citation.
    """
    if not text or not citation:
        return ""

    raw_text = text
    raw_cit = citation.strip()

    # ============================================================
    # FIND CITATION POSITION
    # ============================================================
    idx = -1
    
    # Exact match
    idx = raw_text.find(raw_cit)
    
    # Normalized match
    if idx == -1:
        text_norm = re.sub(r"\s+", " ", raw_text)
        cit_norm = re.sub(r"\s+", " ", raw_cit)
        idx_norm = text_norm.find(cit_norm)
        if idx_norm != -1:
            probe = cit_norm[:50]
            for match in re.finditer(re.escape(probe[:20]), raw_text):
                idx = match.start()
                break
    
    # Author-year pattern match
    if idx == -1:
        author_match = re.search(r'([A-Z][a-z]+(?:\s+et\s+al\.?)?)', raw_cit)
        year_match = re.search(r'\b(19|20)\d{2}\b', raw_cit)
        
        if author_match and year_match:
            author = author_match.group(1)
            year = year_match.group(1)
            pattern = rf'{re.escape(author)}.*?\b{year}\b'
            m = re.search(pattern, raw_text)
            if m:
                idx = m.start()
                raw_cit = raw_text[m.start():m.end()]
    
    if idx == -1:
        return ""

    cit_start = idx
    cit_end = min(len(raw_text), idx + len(raw_cit))

    # ============================================================
    # DETECT NARRATIVE VS PARENTHETICAL
    # ============================================================
    
    is_parenthetical = raw_cit.startswith("(") and raw_cit.endswith(")")
    
    # Check for narrative patterns
    pattern1 = re.search(r'[A-Z][a-z]+(?:\s+et\s+al\.?)?\s*\(\s*(?:19|20)\d{2}\s*\)', raw_cit)
    pattern2 = re.search(r'[A-Z][a-z]+(?:\s+et\s+al\.?)?\s+(?:19|20)\d{2}', raw_cit)
    
    is_narrative = (pattern1 or pattern2) and not is_parenthetical
    
    # Check for narrative verbs after citation
    after_text = raw_text[cit_end:min(cit_end + 150, len(raw_text))]
    narrative_verbs = {
        'argues', 'argue', 'stated', 'states', 'state', 'claimed', 'claims', 'claim',
        'suggested', 'suggests', 'suggest', 'found', 'finds', 'find', 'showed', 'shows', 'show',
        'demonstrated', 'demonstrates', 'demonstrate', 'reported', 'reports', 'report',
        'proposed', 'proposes', 'propose', 'described', 'describes', 'describe',
        'examined', 'examines', 'examine', 'investigated', 'investigates', 'investigate',
        'analyzed', 'analyzes', 'analyze', 'concluded', 'concludes', 'conclude',
        'noted', 'notes', 'note', 'observed', 'observes', 'observe',
        'emphasized', 'emphasizes', 'emphasize', 'highlighted', 'highlights', 'highlight',
        'indicated', 'indicates', 'indicate', 'explained', 'explains', 'explain',
        'wrote', 'writes', 'write', 'published', 'publishes', 'publish'
    }
    
    after_clean = after_text.lstrip()
    first_word = after_clean.split()[0].lower() if after_clean.split() else ""
    
    if first_word in narrative_verbs:
        is_narrative = True

    # ============================================================
    # EXTRACT CLAIM - FORCE RIGHT SIDE FOR NARRATIVE
    # ============================================================
    
    # PARENTHETICAL: claim is BEFORE
    if is_parenthetical:
        sent_start = max(
            raw_text.rfind('.', 0, cit_start),
            raw_text.rfind('!', 0, cit_start),
            raw_text.rfind('?', 0, cit_start),
            raw_text.rfind('\n', 0, cit_start)
        ) + 1
        if sent_start == 0:
            sent_start = max(0, cit_start - window)
        
        claim = raw_text[sent_start:cit_start].strip()
        if claim:
            return re.sub(r"\s+", " ", claim).strip(" ,;:-")
    
    # NARRATIVE: claim is AFTER (take up to window characters or until sentence ends)
    if is_narrative:
        # Find sentence end (period, question mark, exclamation)
        sent_end = raw_text.find('.', cit_end)
        if sent_end == -1 or sent_end > cit_end + window:
            sent_end = min(len(raw_text), cit_end + window)
        else:
            sent_end = sent_end + 1  # Include the period
        
        claim = raw_text[cit_end:sent_end].strip()
        
        # If claim is too short, take more
        if len(claim) < 30 and cit_end + window < len(raw_text):
            claim = raw_text[cit_end:cit_end + window].strip()
        
        # Clean up
        claim = re.sub(r'^[\s,;:]+', '', claim)
        claim = re.sub(r'\s+', ' ', claim)
        
        if claim and len(claim) > 10:
            return claim
    
    # ============================================================
    # FALLBACK: Try right side first (academic writing prefers claim after citation)
    # ============================================================
    
    right_end = min(len(raw_text), cit_end + window)
    right_claim = raw_text[cit_end:right_end].strip()
    right_claim = re.sub(r'^[\s,;:]+', '', right_claim)
    
    if right_claim and len(right_claim) > 15:
        return re.sub(r"\s+", " ", right_claim)
    
    # Last resort: left side
    left_start = max(0, cit_start - window)
    left_claim = raw_text[left_start:cit_start].strip()
    
    if left_claim:
        return re.sub(r"\s+", " ", left_claim)
    
    return ""


def extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from text for search queries."""
    words = re.findall(r"[A-Za-z]{4,}", (text or "").lower())
    stop = {
        "this", "that", "with", "from", "using", "study", "analysis",
        "method", "approach", "results", "table", "figure", "paper",
        "research", "journal", "review", "these", "those", "their",
        "would", "could", "should", "might", "what", "when", "where",
        "which", "while", "there", "about", "into", "through", "during"
    }
    seen = set()
    out = []
    for w in words:
        if w in stop or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:10]


def suggest_from_context(context: str, citation: str = "", top_k: int = 3) -> List[Dict[str, Any]]:
    """
    Suggest references based on surrounding context AND citation text.
    
    Uses hybrid query: author(s) + year + context keywords for optimal precision.
    
    Args:
        context: The surrounding text where the citation appears
        citation: The original citation text (e.g., "(Beck et al., 2021)")
        top_k: Number of suggestions to return
    
    Returns:
        List of suggested reference dictionaries
    """
    keywords = extract_keywords(context)
    authors, year = extract_citation_author_year(citation)
    
    # Build hybrid query
    query_parts = []
    if authors:
        query_parts.extend(authors[:2])  # Use up to 2 authors
    if year:
        query_parts.append(year)
    if keywords:
        query_parts.extend(keywords[:8])  # Use up to 8 keywords
    
    query = " ".join(query_parts).strip()
    
    # Fallback to context-only if hybrid query is empty
    if not query and keywords:
        query = " ".join(keywords[:6])
    
    if not query:
        return []
    
    # Search both CrossRef and OpenAlex
    candidates = []
    candidates.extend(_query_crossref(query, rows=8))
    candidates.extend(_query_openalex(query, rows=8))
    
    # Deduplicate by title
    seen = set()
    suggestions = []
    
    for cand in candidates:
        doi, title, cand_year, authors_list = _candidate_fields(cand)
        if not title:
            continue
        
        # Create unique key
        key = f"{title.lower()}|{cand_year}|{doi.lower()}"
        if key in seen:
            continue
        seen.add(key)
        
        # Calculate relevance score (prioritize year and author matches)
        relevance = 75  # Base score
        
        # Boost if year matches
        if year and cand_year and year[:4] == cand_year[:4]:
            relevance += 15
        
        # Boost if any author matches
        if authors and authors_list:
            if any(a in [au.lower() for au in authors_list] for a in authors):
                relevance += 20
        
        suggestions.append({
            "title": title,
            "year": cand_year,
            "authors": authors_list,
            "doi": doi,
            "type": "context",
            "relevance": relevance
        })
    
    # Sort by relevance
    suggestions.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return suggestions[:top_k]


def suggest_for_unverified(ref: str, top_k: int = 3) -> List[Dict[str, Any]]:
    """
    Suggest corrected references for unverified/needs_review references.
    
    Uses extracted fields from the reference string.
    
    Args:
        ref: The reference string to correct
        top_k: Number of suggestions to return
    
    Returns:
        List of suggested reference dictionaries
    """
    fields = _extract_fields_by_style(ref, "apa")
    title = fields.get("title", "") or ""
    authors = fields.get("authors", []) or []
    year = fields.get("year", "") or ""

    query_parts = []
    if title:
        query_parts.append(title)
    if authors:
        query_parts.extend(authors[:2])
    if year:
        query_parts.append(year)

    query = " ".join(query_parts).strip()
    if not query:
        return []

    # Search both CrossRef and OpenAlex
    candidates = []
    candidates.extend(_query_crossref(query, rows=8))
    candidates.extend(_query_openalex(query, rows=8))

    seen = set()
    suggestions = []

    for cand in candidates:
        doi, cand_title, cand_year, cand_authors = _candidate_fields(cand)
        if not cand_title:
            continue

        # Calculate score using existing _score function
        meta = _score(title, authors, year, cand_title, cand_authors, cand_year)

        # Require at least 70% title similarity for corrections
        if meta["title_score"] < 70:
            continue

        key = f"{cand_title.lower()}|{cand_year}|{doi.lower()}"
        if key in seen:
            continue
        seen.add(key)

        suggestions.append({
            "title": cand_title,
            "year": cand_year,
            "authors": cand_authors,
            "doi": doi,
            "score": meta["score"],
            "title_score": meta["title_score"],
            "author_similarity": meta["author_similarity"],
            "year_match": meta["year_match"],
            "type": "correction"
        })

    suggestions.sort(key=lambda x: (x.get("score", 0), x.get("title_score", 0)), reverse=True)
    return suggestions[:top_k]
