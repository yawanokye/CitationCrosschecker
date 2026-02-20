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
# EXTRACTORS
# -------------------------
def _extract_year(text: str) -> str:
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", _safe_str(text), flags=re.I)
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""

def _extract_first_author_surname(text: str) -> str:
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", _safe_strip(text))
    if not t: return ""
    first = t.split(",", 1)[0] if "," in t else re.split(r"\s+", t)[0]
    return re.sub(r"[^A-Za-z\-']", "", _safe_str(first)).lower()

def _extract_title_guess(ref: str) -> str:
    """Improved: handles varied citation styles and falls back gracefully."""
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", _safe_strip(ref)).strip()
    parts = re.split(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?", t, maxsplit=1, flags=re.I)
    after = parts[2] if len(parts) >= 3 and len(_safe_strip(parts[2])) > 12 else (t[15:160] if len(t) > 40 else t)
    title = after.split(".", 1)[0] if "." in after else after
    return _safe_strip(title)

# -------------------------
# QUERIES, SCORING, CLASSIFY
# -------------------------
def _query_api(title, author, year, raw_ref, margin=0, source="crossref"):
    q = title if len(title) >= 12 else raw_ref
    y_int = int(year[:4]) if year[:4].isdigit() else None
    if source == "crossref":
        params = {"query.bibliographic": q, "query.author": author, "rows": 5, "mailto": MAILTO}
        if y_int: params["filter"] = f"from-pub-date:{y_int-margin}-01-01,until-pub-date:{y_int+margin}-12-31"
        data = _safe_get_json("https://api.crossref.org", params)
        return [{"source": "crossref", "item": i, "query": q} for i in (data.get("message", {}).get("items", []) if data else [])]
    else:
        params = {"search": q, "per-page": 5, "mailto": MAILTO}
        if y_int: params["filter"] = f"publication_year:{y_int-margin}-{y_int+margin}"
        data = _safe_get_json("https://api.openalex.org", params)
        return [{"source": "openalex", "item": i, "query": q} for i in (data.get("results", []) if data else [])]

def _score_and_classify(rt, ra, ry, ct, ca, cy):
    ts = fuzz.token_set_ratio(rt, ct) if rt and ct else 0
    am, ym = (1 if ra and ca and ra == ca else 0), (1 if ry and cy and ry[:4] == cy[:4] else 0)
    score = int((ts * 1.3) + (20 * am) + (15 * ym))
    status = "verified" if ts >= 88 and (am or ym) and score >= 128 else ("likely" if ts >= 82 and score >= 110 else ("needs_review" if ts >= 72 else "not_found"))
    return score, status

# -------------------------
# CANDIDATE MAPPING (HEADINGS)
# -------------------------
def _map_candidate(cand):
    src, item = cand.get("source"), cand.get("item") or {}
    doi, title, year, author = "", "", "", ""
    if src == "crossref":
        doi = item.get("DOI", "")
        title = (item.get("title") or [""])[0]
        year = _safe_str(item.get("published-print", {}).get("date-parts", [[None]])[0][0])
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
    # Initialize 'refs' properly to prevent NameError
    refs = [_safe_strip(r) for r in references if _safe_strip(r)]
    if max_to_check: refs = refs[:max_to_check]
    rows = []
    
    for idx, ref in enumerate(refs):
        ry, ra, rt = _extract_year(ref), _extract_first_author_surname(ref), _extract_title_guess(ref)
        row = {"no": idx+1, "status": "not_found", "score": 0, "reference": ref, "query": rt}
        candidates = []
        
        # Phase 1: Search (Strict, then ±1 Year)
        for margin in [0, 1]:
            if kwargs.get("use_crossref", True): candidates += _query_api(rt, ra, ry, ref, margin, "crossref")
            if kwargs.get("use_openalex", True): candidates += _query_api(rt, ra, ry, ref, margin, "openalex")
            if candidates: break 

        best_cand, max_s = None, -1
        for cand in candidates:
            doi, title, year, author = _map_candidate(cand)
            score, status = _score_and_classify(rt, ra, ry, title, author, year)
            if score > max_s:
                max_s, best_cand = score, {"status": status, "doi": doi, "score": score, "year": year, "author": author, "matched_title": title, "source": cand["source"]}
        
        if best_cand: row.update(best_cand)
        rows.append(row)
    return rows
