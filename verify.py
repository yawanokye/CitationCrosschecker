# verify.py
import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple
import requests
from rapidfuzz import fuzz

# Keep your existing constants and helpers
_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}
MAILTO = os.getenv("CITATION_CROSSCHECKER_MAILTO") or os.getenv("CROSSREF_MAILTO") or ""
MAILTO = str(MAILTO or "").strip()

def _safe_str(x) -> str: return str(x) if x is not None else ""
def _safe_strip(x) -> str: return _safe_str(x).strip()

# -------------------------
# IMPROVED EXTRACTORS
# -------------------------
def _extract_title_guess(ref: str) -> str:
    """Enhanced to handle cases where year parentheses are missing."""
    ref = _safe_str(ref)
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", ref).strip() # strip leading nums
    t = re.sub(r"\s+", " ", t)
    
    # Try the original year-split method
    parts = re.split(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?", t, maxsplit=1, flags=re.I)
    
    if len(parts) >= 3 and len(_safe_strip(parts[2])) > 10:
        title = _safe_strip(parts[2]).split(".", 1)[0]
    else:
        # FALLBACK: If year-split fails, find the first large text block 
        # (usually after authors/initials). 
        # We skip the first ~15 chars to avoid matching 'Smith, J.' as the title.
        fallback = t[15:160] if len(t) > 40 else t
        title = fallback.split(".", 1)[0]
        
    return _safe_strip(title)

# -------------------------
# IMPROVED QUERIES WITH RETRY
# -------------------------
def _query_crossref_gold(title, author, year, raw_ref, year_margin=0):
    url = "https://api.crossref.org/works"
    q = _safe_strip(title) if len(title) >= 12 else _safe_strip(raw_ref)
    params = {"query.bibliographic": q, "rows": 5}
    if author: params["query.author"] = author
    if MAILTO: params["mailto"] = MAILTO

    y4 = _safe_str(year)[:4]
    if y4.isdigit():
        y_int = int(y4)
        # Apply the margin for the retry phase
        params["filter"] = f"from-pub-date:{y_int - year_margin},until-pub-date:{y_int + year_margin}"

    data = _safe_get_json(url, params)
    return [{"source": "crossref", "item": item, "query_used": q} for item in (data.get("message", {}).get("items", []) if data else [])]

def _query_openalex(title, author, year, raw_ref, year_margin=0):
    url = "https://api.openalex.org"
    q = _safe_strip(title) if len(title) >= 12 else _safe_strip(raw_ref)
    params = {"search": q, "per-page": 7}
    if MAILTO: params["mailto"] = MAILTO

    y4 = _safe_str(year)[:4]
    if y4.isdigit():
        y_int = int(y4)
        # OpenAlex uses range syntax: 2020-2022
        params["filter"] = f"publication_year:{y_int - year_margin}-{y_int + year_margin}"

    data = _safe_get_json(url, params)
    return [{"source": "openalex", "item": item, "query_used": q} for item in (data.get("results", []) if data else [])]

# -------------------------
# UPDATED SCORING & CLASSIFY
# -------------------------
def _score(rt, ra, ry, ct, ca, cy):
    # token_set_ratio is best for finding papers with slight title variations
    ts = fuzz.token_set_ratio(rt, ct) if rt and ct else 0
    # token_sort_ratio helps confirm strictness
    sort_ts = fuzz.token_sort_ratio(rt, ct) if rt and ct else 0
    
    avg_ts = (ts + sort_ts) / 2
    am = 1 if ra and ca and ra == ca else 0
    ym = 1 if ry and cy and ry[:4] == cy[:4] else 0

    # Optimal weighting: Title (weighted heavily) + Bonuses for Author/Year
    final_score = (avg_ts * 1.3) + (20 * am) + (15 * ym)
    return {"score": int(final_score), "ts": int(avg_ts), "am": am, "ym": ym}

def _classify(score, am, ym, ts):
    # Lowered 'verified' from 92 to 88 to catch valid academic matches
    if ts >= 88 and (am or ym) and score >= 125: return "verified"
    if ts >= 82 and score >= 110: return "likely"
    if ts >= 70: return "needs_review"
    return "not_found"

# -------------------------
# MAIN BATCH LOGIC
# -------------------------
def verify_references_batch(references, **kwargs):
    # ... (Keep your setup logic) ...
    for ref in refs:
        # ... (Keep your extraction logic) ...
        candidates = []
        # PHASE 1: Strict Search
        if kwargs.get("use_crossref"): candidates += _query_crossref_gold(ref_title, ref_author, ref_year, ref, 0)
        if kwargs.get("use_openalex"): candidates += _query_openalex(ref_title, ref_author, ref_year, ref, 0)
        
        # PHASE 2: Re-try with ±1 Year if nothing found
        if not candidates and ref_year:
            if kwargs.get("use_crossref"): candidates += _query_crossref_gold(ref_title, ref_author, ref_year, ref, 1)
            if kwargs.get("use_openalex"): candidates += _query_openalex(ref_title, ref_author, ref_year, ref, 1)

        # ... (Keep your best-candidate selection and reporting logic) ...
