# verify.py
import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple
import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}
MAILTO = (os.getenv("CITATION_CROSSCHECKER_MAILTO") or os.getenv("CROSSREF_MAILTO") or "").strip()

# -------------------------
# SAFE HELPERS
# -------------------------
def _safe_str(x) -> str: return str(x) if x is not None else ""
def _safe_strip(x) -> str: return _safe_str(x).strip()

def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 15) -> Optional[dict]:
    try:
        headers = {"User-Agent": f"CitationCrosschecker/1.0 (mailto:{MAILTO})", "Accept": "application/json"}
        r = requests.get(url, params=params, timeout=timeout, headers=headers)
        return r.json() if r.status_code == 200 else None
    except: return None

# -------------------------
# ROBUST EXTRACTORS
# -------------------------
def _extract_year(text: str) -> str:
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", _safe_str(text), flags=re.I)
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""

def _extract_first_author_surname(text: str) -> str:
    # Strips leading [1] or 1. and finds the first word (likely the surname)
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", _safe_strip(text))
    if not t: return ""
    first = t.split(",", 1)[0] if "," in t else re.split(r"\s+", t)[0]
    return re.sub(r"[^A-Za-z\-']", "", _safe_str(first)).lower()

def _extract_title_guess(ref: str) -> str:
    """Robust extraction: works even if student formatting is messy."""
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", _safe_strip(ref)).strip()
    # Attempt split by year in parens
    parts = re.split(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?", t, maxsplit=1, flags=re.I)
    # Fallback to middle-chunk if split fails (skips author name noise)
    after = parts[2] if len(parts) >= 3 and len(_safe_strip(parts[2])) > 12 else (t[15:160] if len(t) > 40 else t)
    title = after.split(".", 1)[0] if "." in after else after
    return _safe_strip(title)

# -------------------------
# FINGERPRINT QUERIES (NO DOI NEEDED)
# -------------------------
def _query_api_fingerprint(title, author, year, raw_ref, margin=0, source="crossref"):
    """Queries specifically for Title + Author + Year Range."""
    # API handles short clean titles better than long messy strings
    clean_title = " ".join(_safe_strip(title).split()[:12])
    y4 = _safe_str(year)[:4]
    y_int = int(y4) if y4.isdigit() else None

    if source == "crossref":
        # 'query.title' is more precise for student refs than 'query.bibliographic'
        params = {"query.title": clean_title, "query.author": author, "rows": 5, "mailto": MAILTO}
        if y_int:
            params["filter"] = f"from-pub-date:{y_int-margin}-01-01,until-pub-date:{y_int+margin}-12-31"
        data = _safe_get_json("https://api.crossref.org", params)
        return [{"source": "crossref", "item": i, "query": clean_title} for i in (data.get("message", {}).get("items", []) if data else [])]
    
    else: # OpenAlex
        params = {"search": clean_title, "per-page": 5, "mailto": MAILTO}
        if y_int:
            params["filter"] = f"publication_year:{y_int-margin}-{y_int+margin}"
        data = _safe_get_json("https://api.openalex.org", params)
        return [{"source": "openalex", "item": i, "query": clean_title} for i in (data.get("results", []) if data else [])]

# -------------------------
# SCORING & MAPPING
# -------------------------
def _score_candidate(rt, ra, ry, ct, ca, cy):
    # token_set_ratio is 'optimal' for finding titles with missing words or typos
    ts = fuzz.token_set_ratio(rt.lower(), ct.lower()) if rt and ct else 0
    am = 1 if ra and ca and ra.lower() in ca.lower() else 0
    ym = 1 if ry and cy and ry[:4] == cy[:4] else 0
    
    # Truth Score: Title (Weight 1.3) + Metadata Bonuses
    score = int((ts * 1.3) + (20 * am) + (15 * ym))
    status = "verified" if ts >= 88 and (am or ym) and score >= 125 else \
             ("likely" if ts >= 82 and score >= 110 else \
             ("needs_review" if ts >= 72 else "not_found"))
    return score, status, ts, am, ym

def _map_fields(cand):
    src, item = cand.get("source"), cand.get("item") or {}
    doi, title, year, author = "", "", "", ""
    if src == "crossref":
        doi = item.get("DOI", "")
        title = (item.get("title") or [""])[0]
        y_parts = item.get("published-print", {}).get("date-parts", [[None]]) or \
                  item.get("published-online", {}).get("date-parts", [[None]])
        year = _safe_str(y_parts[0][0])
        authors = item.get("author") or []
        author = authors[0].get("family", "") if authors else ""
    else:
        doi = item.get("doi", "").replace("https://doi.org", "")
        title = item.get("title", "")
        year = _safe_str(item.get("publication_year", ""))
        auths = item.get("authorships") or []
        author = auths[0].get("author", {}).get("display_name", "").split()[-1] if auths else ""
    return doi, title, year, author

# -------------------------
# MAIN BATCH LOGIC
# -------------------------
def verify_references_batch(references: List[str], max_to_check: int = 0, **kwargs):
    # Fix: Ensure 'refs' is defined to avoid NameError
    refs = [_safe_strip(r) for r in references if _safe_strip(r)]
    if max_to_check: refs = refs[:max_to_check]
    
    rows = []
    for idx, ref in enumerate(refs):
        ry, ra, rt = _extract_year(ref), _extract_first_author_surname(ref), _extract_title_guess(ref)
        row = {"no": idx+1, "status": "not_found", "score": 0, "reference": ref, "query": rt}
        candidates = []
        
        # Two-Pass Waterfall Search
        for margin in [0, 1]: # Try exact year, then ±1 year
            if kwargs.get("use_crossref", True): candidates += _query_api_fingerprint(rt, ra, ry, ref, margin, "crossref")
            if kwargs.get("use_openalex", True): candidates += _query_api_fingerprint(rt, ra, ry, ref, margin, "openalex")
            if candidates: break # Stop if Pass 1 finds results

        best_cand, max_s = None, -1
        for cand in candidates:
            doi, title, year, author = _map_fields(cand)
            score, status, ts, am, ym = _score_candidate(rt, ra, ry, title, author, year)
            if score > max_s:
                max_s = score
                best_cand = {"status": status, "doi": doi, "score": score, "year": year, 
                             "author": author, "matched_title": title, "source": cand["source"]}
        
        if best_cand: row.update(best_cand)
        rows.append(row)
        if kwargs.get("throttle_s", 0.1) > 0: time.sleep(kwargs.get("throttle_s", 0.1))
        
    return rows
