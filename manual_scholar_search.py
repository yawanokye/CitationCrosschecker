"""
manual_scholar_search.py
Manual Verification Search for CiteIntegrity.

Searches open scholarly APIs without scraping Google Scholar:
- OpenAlex
- Crossref
- Semantic Scholar
- DataCite
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional


CROSSREF_MAILTO = os.environ.get("CROSSREF_MAILTO", "").strip()
OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO", "").strip()
SEMANTIC_SCHOLAR_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()

MANUAL_SEARCH_TIMEOUT = int(os.environ.get("MANUAL_SEARCH_TIMEOUT", "10"))
MANUAL_SEARCH_ROWS = int(os.environ.get("MANUAL_SEARCH_ROWS", "6"))

SUPPORTED_MANUAL_SOURCES = {"openalex", "crossref", "semantic_scholar", "datacite"}


def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def _clean_text(text: str, max_len: int = 260) -> str:
    text = _safe_str(text)
    text = re.sub(r"https?://\S+", " ", text, flags=re.I)
    text = re.sub(r"\bdoi\s*:?\s*10\.\S+", " ", text, flags=re.I)
    text = re.sub(r"\b10\.\d{4,9}/[^\s\]\)>,;]+", " ", text, flags=re.I)
    text = re.sub(r"[^A-Za-z0-9\s:&,\-'/]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def _normalise_for_score(text: str) -> str:
    text = _safe_str(text).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_year(text: str) -> str:
    m = re.search(r"\b((?:19|20)\d{2})[a-z]?\b", _safe_str(text), flags=re.I)
    return m.group(1) if m else ""


def _title_similarity(query: str, title: str) -> int:
    q = _normalise_for_score(query)
    t = _normalise_for_score(title)
    if not q or not t:
        return 0

    seq = SequenceMatcher(None, q, t).ratio()
    q_words = set(q.split())
    t_words = set(t.split())
    overlap = len(q_words & t_words) / max(len(q_words), 1)
    return int((seq * 45) + (overlap * 55))


def _safe_get_json(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = MANUAL_SEARCH_TIMEOUT,
) -> Optional[dict]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)

    base_headers = {
        "User-Agent": "CiteIntegrity/1.0 (manual-verification-search; https://citeintegrity.org)",
        "Accept": "application/json",
    }
    if headers:
        base_headers.update(headers)

    req = urllib.request.Request(url, headers=base_headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return {"_error": f"HTTP {e.code}: {raw[:300]}", "_url": url}
    except Exception as e:
        return {"_error": str(e), "_url": url}


def _candidate(
    *,
    source: str,
    title: str,
    year: Any = "",
    authors: Any = None,
    doi: str = "",
    url: str = "",
    venue: str = "",
    publisher: str = "",
    source_type: str = "",
    raw: Optional[dict] = None,
    query: str = "",
) -> Optional[Dict[str, Any]]:
    title = _safe_str(title).strip()
    if not title:
        return None

    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(",") if a.strip()]
    elif not isinstance(authors, list):
        authors = []

    doi = _safe_str(doi).strip()
    if doi.lower().startswith("https://doi.org/"):
        doi = doi.split("https://doi.org/", 1)[1]

    match_score = _title_similarity(query, title)
    query_year = _extract_year(query)
    cand_year = _safe_str(year)
    if query_year and cand_year and query_year[:4] == cand_year[:4]:
        match_score = min(100, match_score + 10)

    return {
        "title": title,
        "year": cand_year,
        "authors": authors[:8],
        "doi": doi,
        "url": url or (f"https://doi.org/{doi}" if doi else ""),
        "venue": venue,
        "publisher": publisher,
        "source_type": source_type,
        "source": source,
        "match_score": match_score,
        "query_used": query,
        "manual_note": "Review this candidate before accepting it as a match.",
        "raw": raw or {},
    }


def search_openalex(query: str, rows: int = MANUAL_SEARCH_ROWS) -> List[Dict[str, Any]]:
    query = _clean_text(query)
    if not query:
        return []
    params = {"search": query, "per-page": str(rows)}
    if OPENALEX_MAILTO:
        params["mailto"] = OPENALEX_MAILTO
    data = _safe_get_json("https://api.openalex.org/works", params=params)
    if not data or data.get("_error"):
        return []
    out = []
    for item in data.get("results", []) or []:
        authors = []
        for a in item.get("authorships") or []:
            name = ((a.get("author") or {}).get("display_name") or "").strip()
            if name:
                authors.append(name)
        doi = item.get("doi") or ""
        if doi.lower().startswith("https://doi.org/"):
            doi = doi.split("https://doi.org/", 1)[1]
        primary = item.get("primary_location") or {}
        source_info = primary.get("source") or {}
        cand = _candidate(
            source="openalex",
            title=item.get("title") or item.get("display_name") or "",
            year=item.get("publication_year") or "",
            authors=authors,
            doi=doi,
            url=primary.get("landing_page_url") or item.get("id") or "",
            venue=source_info.get("display_name") or "",
            publisher=source_info.get("host_organization_name") or "",
            source_type=item.get("type") or "",
            raw=item,
            query=query,
        )
        if cand:
            out.append(cand)
    return out


def search_crossref(query: str, rows: int = MANUAL_SEARCH_ROWS) -> List[Dict[str, Any]]:
    query = _clean_text(query)
    if not query:
        return []
    params = {
        "query.bibliographic": query,
        "rows": str(rows),
        "select": "DOI,title,author,issued,published-print,published-online,created,URL,container-title,publisher,type,score",
    }
    if CROSSREF_MAILTO:
        params["mailto"] = CROSSREF_MAILTO
    data = _safe_get_json("https://api.crossref.org/works", params=params)
    if not data or data.get("_error"):
        return []
    out = []
    for item in ((data.get("message") or {}).get("items") or []):
        titles = item.get("title") or []
        title = titles[0] if isinstance(titles, list) and titles else _safe_str(titles)
        year = ""
        for k in ("published-print", "published-online", "issued", "created"):
            parts = ((item.get(k) or {}).get("date-parts") or [])
            if parts and parts[0]:
                year = str(parts[0][0])
                break
        authors = []
        for au in item.get("author") or []:
            name = " ".join(x for x in [au.get("given"), au.get("family")] if x).strip()
            if name:
                authors.append(name)
        container = item.get("container-title") or []
        venue = container[0] if isinstance(container, list) and container else ""
        cand = _candidate(
            source="crossref",
            title=title,
            year=year,
            authors=authors,
            doi=item.get("DOI") or "",
            url=item.get("URL") or "",
            venue=venue,
            publisher=item.get("publisher") or "",
            source_type=item.get("type") or "",
            raw=item,
            query=query,
        )
        if cand:
            out.append(cand)
    return out


def search_semantic_scholar(query: str, rows: int = MANUAL_SEARCH_ROWS) -> List[Dict[str, Any]]:
    query = _clean_text(query)
    if not query:
        return []
    headers = {}
    if SEMANTIC_SCHOLAR_API_KEY:
        headers["x-api-key"] = SEMANTIC_SCHOLAR_API_KEY
    params = {
        "query": query,
        "limit": str(rows),
        "fields": "title,year,authors,venue,journal,externalIds,url,publicationTypes,isOpenAccess",
    }
    data = _safe_get_json("https://api.semanticscholar.org/graph/v1/paper/search", params=params, headers=headers)
    if not data or data.get("_error"):
        return []
    out = []
    for item in data.get("data", []) or []:
        external = item.get("externalIds") or {}
        authors = [au.get("name") for au in item.get("authors") or [] if au.get("name")]
        journal = item.get("journal") or {}
        cand = _candidate(
            source="semantic_scholar",
            title=item.get("title") or "",
            year=item.get("year") or "",
            authors=authors,
            doi=external.get("DOI") or "",
            url=item.get("url") or "",
            venue=item.get("venue") or journal.get("name") or "",
            source_type=", ".join(item.get("publicationTypes") or []),
            raw=item,
            query=query,
        )
        if cand:
            out.append(cand)
    return out


def search_datacite(query: str, rows: int = MANUAL_SEARCH_ROWS) -> List[Dict[str, Any]]:
    query = _clean_text(query)
    if not query:
        return []
    params = {"query": query, "page[size]": str(rows)}
    data = _safe_get_json("https://api.datacite.org/dois", params=params)
    if not data or data.get("_error"):
        return []
    out = []
    for item in data.get("data", []) or []:
        attrs = item.get("attributes") or {}
        titles = attrs.get("titles") or []
        title = titles[0].get("title") if titles and isinstance(titles[0], dict) else ""
        authors = []
        for c in attrs.get("creators") or []:
            name = c.get("name") or " ".join(x for x in [c.get("givenName"), c.get("familyName")] if x).strip()
            if name:
                authors.append(name)
        doi = attrs.get("doi") or item.get("id") or ""
        types = attrs.get("types") or {}
        cand = _candidate(
            source="datacite",
            title=title,
            year=attrs.get("publicationYear") or "",
            authors=authors,
            doi=doi,
            url=attrs.get("url") or (f"https://doi.org/{doi}" if doi else ""),
            publisher=attrs.get("publisher") or "",
            source_type=types.get("resourceTypeGeneral", "") if isinstance(types, dict) else "",
            raw=item,
            query=query,
        )
        if cand:
            out.append(cand)
    return out


def dedupe_candidates(candidates: List[Dict[str, Any]], limit: int = 20) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for c in sorted(candidates or [], key=lambda x: int(x.get("match_score") or 0), reverse=True):
        doi = _safe_str(c.get("doi")).lower().strip()
        title = _normalise_for_score(c.get("title", ""))
        year = _safe_str(c.get("year")).strip()
        key = doi or f"{title}|{year}"
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def manual_scholar_search(
    query: str,
    sources: Optional[List[str]] = None,
    rows_per_source: int = MANUAL_SEARCH_ROWS,
) -> Dict[str, Any]:
    query = _clean_text(query)
    selected = [s.strip().lower() for s in (sources or ["openalex", "crossref", "semantic_scholar", "datacite"])]
    selected = [s for s in selected if s in SUPPORTED_MANUAL_SOURCES]
    candidates = []
    source_counts = {}
    for source in selected:
        try:
            if source == "openalex":
                rows = search_openalex(query, rows=rows_per_source)
            elif source == "crossref":
                rows = search_crossref(query, rows=rows_per_source)
            elif source == "semantic_scholar":
                rows = search_semantic_scholar(query, rows=rows_per_source)
            elif source == "datacite":
                rows = search_datacite(query, rows=rows_per_source)
            else:
                rows = []
            source_counts[source] = len(rows)
            candidates.extend(rows)
        except Exception as e:
            source_counts[source] = 0
            candidates.append({"source": source, "error": str(e), "title": "", "match_score": 0})
    clean = dedupe_candidates(candidates, limit=max(rows_per_source * max(len(selected), 1), 10))
    return {
        "query": query,
        "sources": selected,
        "source_counts": source_counts,
        "total_candidates": len(clean),
        "candidates": clean,
    }
