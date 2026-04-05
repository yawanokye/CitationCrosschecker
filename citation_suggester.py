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


def extract_context(text: str, citation: str, window: int = 200) -> str:
    """Extract surrounding context around a citation in the text."""
    if not text or not citation:
        return ""

    idx = text.find(citation)
    if idx == -1:
        return ""

    citation = citation.strip()
    cit_start = idx
    cit_end = idx + len(citation)

    # Nearest sentence boundary on the left
    left_boundary = max(
        text.rfind(".", 0, cit_start),
        text.rfind("!", 0, cit_start),
        text.rfind("?", 0, cit_start),
        text.rfind(";", 0, cit_start),
    )
    left_boundary = 0 if left_boundary == -1 else left_boundary + 1

    # Nearest sentence boundary on the right
    right_candidates = [
        p for p in [
            text.find(".", cit_end),
            text.find("!", cit_end),
            text.find("?", cit_end),
            text.find(";", cit_end),
        ] if p != -1
    ]
    right_boundary = min(right_candidates) + 1 if right_candidates else len(text)

    left_chunk = text[max(left_boundary, cit_start - window):cit_start].strip(" ,;:-")
    right_chunk = text[cit_end:min(right_boundary, cit_end + window)].strip(" ,;:-")

    # Parenthetical citation (e.g., (Beck et al., 2021)) -> use left context
    if citation.startswith("(") and citation.endswith(")"):
        return left_chunk

    # Narrative citation with year in parentheses (e.g., Beck et al. (2021)) -> use right context
    if re.search(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)", citation):
        return right_chunk

    return (left_chunk + " " + right_chunk).strip()


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
