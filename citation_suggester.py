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


def extract_context(text: str, citation: str, window: int = 220) -> str:
    """
    Robust claim/context extraction for both parenthetical and narrative citations.
    Returns the most proposition-like text around the citation.
    """
    if not text or not citation:
        return ""

    raw_text = text
    raw_cit = citation.strip()

    # 1. exact match first
    idx = raw_text.find(raw_cit)

    # 2. whitespace-normalized fallback
    if idx == -1:
        text_norm = _norm_ws(raw_text)
        cit_norm = _norm_ws(raw_cit)
        idx_norm = text_norm.find(cit_norm)
        if idx_norm != -1:
            probe = cit_norm[:40]
            idx = raw_text.find(probe.split()[0]) if probe else -1

    # 3. author-year fallback
    if idx == -1:
        surname, year = _extract_author_year_bits(raw_cit)
        if surname and year:
            m = re.search(rf"\b{re.escape(surname)}\b.*?\b{re.escape(year)}\b", raw_text)
            if not m:
                m = re.search(rf"\b{re.escape(year)}\b.*?\b{re.escape(surname)}\b", raw_text)
            if m:
                idx = m.start()
                raw_cit = raw_text[m.start():m.end()]
        elif year:
            m = re.search(rf"\b{re.escape(year)}\b", raw_text)
            if m:
                idx = m.start()

    if idx == -1:
        return ""

    cit_start = idx
    cit_end = min(len(raw_text), idx + len(raw_cit))

    # Find sentence boundaries
    sent_left, sent_right = _find_sentence_span(raw_text, cit_start)
    sentence = raw_text[sent_left:sent_right].strip()

    # Extract chunks around citation
    left_chunk = raw_text[max(sent_left, cit_start - window):cit_start].strip(" ,;:-")
    right_chunk = raw_text[cit_end:min(sent_right, cit_end + window)].strip(" ,;:-")

    # ============================================================
    # IMPROVED NARRATIVE CITATION DETECTION
    # ============================================================
    
    # Pattern 1: Author (Year) verb... (classic narrative)
    # e.g., "Beck et al. (2021) argue that X improves Y"
    narrative_pattern1 = re.search(
        rf'[A-Z][a-z]+(?:\s+et\s+al\.?)?\s*\(\s*{_YEAR_RE}\s*\)\s+(\w+)', 
        raw_cit
    )
    
    # Pattern 2: Citation at beginning of sentence with verb after
    # e.g., "(Beck et al., 2021) found that X..."
    narrative_pattern2 = re.search(
        rf'\(\s*[^)]+{_YEAR_RE}[^)]*\)\s+(\w+)', 
        raw_cit
    )
    
    # Pattern 3: Author (Year) without parentheses around year
    # e.g., "Beck et al. 2021 examined X"
    narrative_pattern3 = re.search(
        rf'[A-Z][a-z]+(?:\s+et\s+al\.?)?\s+{_YEAR_RE}\s+(\w+)', 
        raw_cit
    )
    
    is_narrative = bool(narrative_pattern1 or narrative_pattern2 or narrative_pattern3)
    
    # Also detect if there's a verb immediately after the citation in the original text
    verb_after = False
    if cit_end < len(raw_text):
        next_word_match = re.match(r'\s+([a-z]+)', raw_text[cit_end:cit_end + 30])
        if next_word_match:
            verb = next_word_match.group(1).lower()
            # Common academic verbs indicating narrative citation
            narrative_verbs = {'argue', 'state', 'claim', 'suggest', 'find', 'show', 
                               'demonstrate', 'report', 'propose', 'describe', 'present',
                               'discuss', 'examine', 'investigate', 'analyze', 'assess',
                               'evaluate', 'compare', 'contrast', 'review', 'conclude',
                               'note', 'observe', 'emphasize', 'highlight', 'indicate'}
            if verb in narrative_verbs:
                verb_after = True
                is_narrative = True

    # ============================================================
    # EXTRACT CLAIM BASED ON CITATION TYPE
    # ============================================================
    
    # Parenthetical citation: claim is on the LEFT
    if raw_cit.startswith("(") and raw_cit.endswith(")"):
        claim = left_chunk or sentence.replace(raw_cit, "").strip()
        if not claim:
            claim = right_chunk
        return re.sub(r"\s+", " ", claim).strip(" ,;:-")

    # Narrative citation: claim is on the RIGHT (after the citation)
    if is_narrative or verb_after:
        # Priority 1: Right chunk (most common for narrative)
        claim = right_chunk
        
        # Priority 2: If right chunk is too short, look ahead further
        if len(claim) < 20 and cit_end + 100 < len(raw_text):
            extended_right = raw_text[cit_end:min(sent_right, cit_end + 200)].strip(" ,;:-")
            if len(extended_right) > len(claim):
                claim = extended_right
        
        # Priority 3: Extract from sentence after removing citation
        if not claim or len(claim) < 10:
            # Remove the citation from sentence and take the rest
            claim = sentence.replace(raw_cit, "").strip()
            # If the citation was at the beginning, take the whole sentence after it
            if claim and len(claim) > 0:
                pass
        
        # Priority 4: Fallback to left chunk
        if not claim:
            claim = left_chunk
        
        return re.sub(r"\s+", " ", claim).strip(" ,;:-")

    # Default: use both chunks
    claim = sentence.replace(raw_cit, "").strip()
    if not claim:
        claim = (left_chunk + " " + right_chunk).strip()
    
    return re.sub(r"\s+", " ", claim).strip(" ,;:-")


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
