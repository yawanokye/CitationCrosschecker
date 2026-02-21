import os
import re
import time
import requests
from rapidfuzz import fuzz
from typing import List, Dict, Any, Optional, Tuple

# --- CONFIGURATION ---
MAILTO = os.getenv("CROSSREF_MAILTO") or "your-email@example.com"

# --- IMPROVED HTTP WITH RETRIES ---
def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 20) -> Optional[dict]:
    for attempt in range(3):  # Retry up to 3 times for 429 errors
        try:
            headers = {"User-Agent": f"CitationVerifier/1.1 (mailto:{MAILTO})"}
            r = requests.get(url, params=params, timeout=timeout, headers=headers)
            
            if r.status_code == 429:  # Rate limited
                wait = int(r.headers.get("Retry-After", 2))
                time.sleep(wait)
                continue
                
            if r.status_code != 200:
                return None
                
            return r.json()
        except Exception:
            time.sleep(1)
    return None

# --- IMPROVED SCORING LOGIC ---
def _score_match(ref_title, ref_author, ref_year, cand_title, cand_author, cand_year):
    """
    Calculates a weighted similarity score between reference and candidate.
    """
    # 1. Fuzzy Title Match (Highest Weight)
    title_sim = fuzz.token_set_ratio(ref_title, cand_title) if ref_title and cand_title else 0
    
    # 2. Fuzzy Author Match (Handles surnames vs full names)
    # Using partial_ratio allows "Doe" to match "Doe, John"
    auth_sim = fuzz.partial_ratio(ref_author.lower(), cand_author.lower()) if ref_author and cand_author else 0
    author_match = 1.0 if auth_sim >= 90 else (0.5 if auth_sim >= 70 else 0)

    # 3. Buffered Year Match (Handles online-first vs print discrepancies)
    year_match = 0
    try:
        ry, cy = int(str(ref_year)[:4]), int(str(cand_year)[:4])
        if ry == cy:
            year_match = 1.0
        elif abs(ry - cy) == 1:
            year_match = 0.5  # Partial credit for +/- 1 year
    except (ValueError, TypeError):
        year_match = 0

    # Total weighted score
    total = (title_sim * 1.2) + (author_match * 25) + (year_match * 15)
    
    # Classification
    status = "not_found"
    if title_sim >= 85 and (author_match >= 0.5 or year_match >= 0.5) and total >= 100:
        status = "verified"
    elif title_sim >= 75 and total >= 85:
        status = "likely"
    elif title_sim >= 70:
        status = "needs_review"

    return {"status": status, "total_score": int(total), "title_score": title_sim}

# --- CROSSREF QUERY WITH LOOSE FILTERING ---
def query_crossref(title, author, year, raw_ref):
    url = "https://api.crossref.org"
    
    # Prefer title if it looks substantial, otherwise fallback to raw string
    q = title if len(title) > 15 else raw_ref
    
    params = {
        "query.bibliographic": q,
        "rows": 5,
        "mailto": MAILTO
    }
    if author:
        params["query.author"] = author

    # Note: We do NOT use hard 'filter=from-pub-date' here to allow for the +/- 1 year buffer
    data = _safe_get_json(url, params)
    if not data: return []

    results = []
    for item in data.get("message", {}).get("items", []):
        c_doi = item.get("DOI")
        c_title = (item.get("title") or [""])[0]
        c_year = item.get("published-print", item.get("published-online", {})).get("date-parts", [[None]])[0][0]
        c_author = (item.get("author", [{}])[0].get("family", ""))
        
        score_data = _score_match(title, author, year, c_title, c_author, c_year)
        results.append({
            "doi": c_doi,
            "title": c_title,
            "year": c_year,
            "author": c_author,
            **score_data
        })
    
    # Sort by highest score
    return sorted(results, key=lambda x: x["total_score"], reverse=True)

# --- EXAMPLE EXECUTION ---
# result = query_crossref("Attention is all you need", "Vaswani", "2017", "Vaswani et al. 2017 Attention...")
