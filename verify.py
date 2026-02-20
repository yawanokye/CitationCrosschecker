# verify.py
import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple
import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}
MAILTO = (os.getenv("CITATION_CROSSCHECKER_MAILTO") or os.getenv("CROSSREF_MAILTO") or "").strip()

def _safe_str(x) -> str: return str(x) if x is not None else ""
def _safe_strip(x) -> str: return _safe_str(x).strip()

# -------------------------
# IMPROVED EXTRACTORS
# -------------------------
def _extract_title_guess(ref: str) -> str:
    """Robust extraction: attempts year-split, fallbacks to middle-chunk to avoid author noise."""
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", _safe_strip(ref)).strip()
    # 1. Try splitting by common year patterns (APA, MLA)
    parts = re.split(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?", t, maxsplit=1, flags=re.I)
    
    if len(parts) >= 3 and len(_safe_strip(parts[2])) > 12:
        after = parts[2]
    else:
        # 2. Fallback: Skip first ~15 chars to bypass author surnames
        after = t[15:160] if len(t) > 40 else t

    title = after.split(".", 1)[0] if "." in after else after
    return _safe_strip(title)

# -------------------------
# QUERIES WITH ±1 YEAR RETRY
# -------------------------
def _query_crossref_gold(title, author, year, raw_ref, margin=0):
    url = "https://api.crossref.org"
    q = title if len(title) >= 12 else raw_ref
    params = {"query.bibliographic": q, "rows": 5}
    if author: params["query.author"] = author
    if MAILTO: params["mailto"] = MAILTO

    y4 = _safe_str(year)[:4]
    if y4.isdigit():
        y_int = int(y4)
        # RECOMMENDED: Use year range to catch print vs online discrepancies
        params["filter"] = f"from-pub-date:{y_int-margin}-01-01,until-pub-date:{y_int+margin}-12-31"
    
    data = _safe_get_json(url, params)
    return [{"source": "crossref", "item": i, "query_used": q} for i in (data.get("message", {}).get("items", []) if data else [])]

def _query_openalex(title, author, year, raw_ref, margin=0):
    url = "https://api.openalex.org/works"
    q = title if len(title) >= 12 else raw_ref
    params = {"search": q, "per-page": 5}
    if MAILTO: params["mailto"] = MAILTO

    y4 = _safe_str(year)[:4]
    if y4.isdigit():
        y_int = int(y4)
        # OpenAlex optimized year range filtering
        params["filter"] = f"publication_year:{y_int-margin}-{y_int+margin}"
    
    data = _safe_get_json(url, params)
    return [{"source": "openalex", "item": i, "query_used": q} for i in (data.get("results", []) if data else [])]

# -------------------------
# SCORING & CLASSIFY
# -------------------------
def _score(rt, ra, ry, ct, ca, cy):
    # token_set_ratio handles titles with slight ordering differences or missing words
    ts = fuzz.token_set_ratio(rt, ct) if rt and ct else 0
    am = 1 if ra and ca and ra == ca else 0
    ym = 1 if ry and cy and ry[:4] == cy[:4] else 0
    # Weighted truth score: Title (most weight) + Metadata bonuses
    score = (ts * 1.3) + (20 * am) + (15 * ym)
    return {"score": int(score), "ts": ts, "am": am, "ym": ym}

def _classify(score, am, ym, ts):
    # RECOMMENDATION: Use lower thresholds for academic titles (88% vs 92%)
    if ts >= 88 and (am or ym) and score >= 128: return "verified"
    if ts >= 82 and score >= 110: return "likely"
    if ts >= 72: return "needs_review"
    return "not_found"

# -------------------------
# MAIN BATCH LOGIC
# -------------------------
def verify_references_batch(references: List[str], max_to_check: int = 0, **kwargs):
    # Fix: Ensure 'refs' is initialized to avoid NameError
    refs = [_safe_strip(r) for r in references if _safe_strip(r)]
    if max_to_check: refs = refs[:max_to_check]

    rows = []
    for ref in refs:
        ref_year, ref_author, ref_title = _extract_year(ref), _extract_first_author_surname(ref), _extract_title_guess(ref)
        row = {"reference": ref, "status": "not_found", "doi": "", "score": 0, "year": "", "author": "", "matched_title": ""}
        candidates = []

        try:
            # PHASE 1: Strict Year Search
            candidates += _query_crossref_gold(ref_title, ref_author, ref_year, ref, 0)
            candidates += _query_openalex(ref_title, ref_author, ref_year, ref, 0)

            # PHASE 2: ±1 Year Retry if no results found (Crucial for online/print mismatch)
            if not candidates and ref_year:
                candidates += _query_crossref_gold(ref_title, ref_author, ref_year, ref, 1)
                candidates += _query_openalex(ref_title, ref_author, ref_year, ref, 1)

            best_cand, max_s = None, -1
            for cand in candidates:
                doi, title, year, author = _candidate_fields(cand)
                s_data = _score(ref_title, ref_author, ref_year, title, author, year)
                if s_data["score"] > max_s:
                    max_s = s_data["score"]
                    best_cand = {**cand, **s_data, "doi": doi, "title": title, "year": year, "author": author}

            if best_cand:
                row.update({
                    "status": _classify(best_cand["score"], best_cand["am"], best_cand["ym"], best_cand["ts"]),
                    "doi": best_cand["doi"], "score": best_cand["score"],
                    "year": best_cand["year"], "author": best_cand["author"], "matched_title": best_cand["title"]
                })
        except Exception as e: row["error"] = str(e)
        rows.append(row)
    return rows
