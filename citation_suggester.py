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

def extract_context(text: str, citation: str, window: int = 200) -> str:
    if not text or not citation:
        return ""

    idx = text.find(citation)
    if idx == -1:
        return ""

    citation = citation.strip()
    cit_start = idx
    cit_end = idx + len(citation)

    # nearest sentence boundary on the left
    left_boundary = max(
        text.rfind(".", 0, cit_start),
        text.rfind("!", 0, cit_start),
        text.rfind("?", 0, cit_start),
        text.rfind(";", 0, cit_start),
    )
    left_boundary = 0 if left_boundary == -1 else left_boundary + 1

    # nearest sentence boundary on the right
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

    # parenthetical citation -> use left proposition
    if citation.startswith("(") and citation.endswith(")"):
        return left_chunk

    # narrative citation -> use right proposition
    if re.search(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)", citation):
        return right_chunk

    return (left_chunk + " " + right_chunk).strip()

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

    query = " ".join(keywords[:6])

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
