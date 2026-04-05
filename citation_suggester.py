# citation_suggester.py

import re
from typing import List, Dict, Any

from verify import (
    _query_crossref,
    _query_openalex,
    _candidate_fields,
    _score,
    _extract_fields_by_style,
)

def extract_context(text: str, citation: str, window: int = 120) -> str:
    if not text or not citation:
        return ""
    idx = text.find(citation)
    if idx == -1:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + len(citation) + window)
    return text[start:end]

def extract_keywords(text: str) -> List[str]:
    words = re.findall(r"[A-Za-z]{4,}", (text or "").lower())
    stop = {
        "this", "that", "with", "from", "using", "study", "analysis",
        "method", "approach", "results", "table", "figure", "paper",
        "research", "journal", "review", "these", "those", "their"
    }
    seen = set()
    out = []
    for w in words:
        if w in stop or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:10]

def suggest_from_context(context: str, top_k: int = 3) -> List[Dict[str, Any]]:
    keywords = extract_keywords(context)
    if not keywords:
        return []

    query = " ".join(keywords)

    candidates = []
    candidates.extend(_query_crossref(query, rows=5))
    candidates.extend(_query_openalex(query, rows=5))

    seen = set()
    suggestions = []

    for cand in candidates:
        doi, title, year, authors = _candidate_fields(cand)
        if not title:
            continue
        key = f"{title.lower()}|{year}|{doi.lower()}"
        if key in seen:
            continue
        seen.add(key)
        suggestions.append({
            "title": title,
            "year": year,
            "authors": authors,
            "doi": doi,
            "type": "context"
        })

    return suggestions[:top_k]

def suggest_for_unverified(ref: str, top_k: int = 3) -> List[Dict[str, Any]]:
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

    candidates = []
    candidates.extend(_query_crossref(query, rows=5))
    candidates.extend(_query_openalex(query, rows=5))

    seen = set()
    suggestions = []

    for cand in candidates:
        doi, cand_title, cand_year, cand_authors = _candidate_fields(cand)
        if not cand_title:
            continue

        meta = _score(title, authors, year, cand_title, cand_authors, cand_year)

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
            "type": "correction"
        })

    suggestions.sort(key=lambda x: (x.get("score", 0), x.get("title_score", 0)), reverse=True)
    return suggestions[:top_k]
