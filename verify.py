# verify.py
import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple

import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {
    "verified",
    "likely",
    "needs_review",
    "not_found",
    "offline",
}

# Polite pool email for better API performance
MAILTO = (
    os.getenv("CITATION_CROSSCHECKER_MAILTO")
    or os.getenv("CROSSREF_MAILTO")
    or ""
).strip()


# -------------------------
# SAFE STRING HELPERS
# -------------------------
def _safe_str(x) -> str:
    return str(x) if x is not None else ""

def _safe_strip(x) -> str:
    return _safe_str(x).strip()

def _normalize_verify_status(s: str) -> str:
    st = _safe_strip(s).lower().replace(" ", "_")
    return st if st in _ALLOWED_VERIFY_STATUSES else "needs_review"


# -------------------------
# SAFE HTTP
# -------------------------
def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 15) -> Optional[dict]:
    """Handles API requests with explicit timeouts for Render stability."""
    try:
        headers = {
            "User-Agent": "CitationCrosschecker/1.0 (mailto:" + MAILTO + ")",
            "Accept": "application/json",
        }
        r = requests.get(url, params=params, timeout=timeout, headers=headers)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


# -------------------------
# IMPROVED EXTRACTORS
# -------------------------
def _extract_year(text: str) -> str:
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", _safe_str(text), flags=re.I)
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""

def _extract_first_author_surname(text: str) -> str:
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", _safe_strip(text))
    if not t: return ""
    first = t.split(",", 1)[0] if "," in t else re.split(r"\s+", t)[0]
    return re.sub(r"[^A-Za-z\-']", "", _safe_str(first)).lower()

def _extract_doi(text: str) -> str:
    m = re.search(r"(10\.\d{4,9}/[^\s]+)", _safe_str(text), flags=re.I)
    return m.group(1).rstrip(").,;") if m else ""

def _extract_title_guess(ref: str) -> str:
    """Robust title extraction that handles missing parentheses and various styles."""
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", _safe_strip(ref)).strip()
    # Try original year-split
    parts = re.split(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?", t, maxsplit=1, flags=re.I)
    
    if len(parts) >= 3 and len(_safe_strip(parts[2])) > 12:
        after = parts[2]
    else:
        # Fallback: if split fails, take a middle chunk to avoid leading author names
        after = t[15:160] if len(t) > 40 else t

    title = after.split(".", 1)[0] if "." in after else after
    return _safe_strip(title)


# -------------------------
# OPTIMAL QUERIES
# -------------------------
def _query_crossref_gold(title, author, year, raw_ref, margin=0):
    url = "https://api.crossref.org/works"
    q = title if len(title) >= 12 else raw_ref
    params = {"query.bibliographic": q, "rows": 5}
    if author: params["query.author"] = author
    if MAILTO: params["mailto"] = MAILTO

    y4 = _safe_str(year)[:4]
    if y4.isdigit():
        y_int = int(y4)
        params["filter"] = f"from-pub-date:{y_int - margin}-01-01,until-pub-date:{y_int + margin}-12-31"
    
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
        params["filter"] = f"publication_year:{y_int - margin}-{y_int + margin}"
    
    data = _safe_get_json(url, params)
    return [{"source": "openalex", "item": i, "query_used": q} for i in (data.get("results", []) if data else [])]


# -------------------------
# CANDIDATE PROCESSING
# -------------------------
def _candidate_fields(cand) -> Tuple[str, str, str, str]:
    src = _safe_strip(cand.get("source"))
    item = cand.get("item") or {}
    doi, title, year, first_author = "", "", "", ""

    if src == "crossref":
        doi = _safe_strip(item.get("DOI"))
        title = _safe_strip((item.get("title") or [""])[0])
        date = item.get("published-print", {}).get("date-parts", [[None]])[0][0] or \
               item.get("published-online", {}).get("date-parts", [[None]])[0][0]
        year = _safe_str(date)
        authors = item.get("author") or []
        if authors: first_author = _safe_strip(authors[0].get("family")).lower()

    elif src == "openalex":
        doi = _safe_strip(item.get("doi")).replace("https://doi.org", "")
        title = _safe_strip(item.get("title"))
        year = _safe_str(item.get("publication_year"))
        auths = item.get("authorships") or []
        if auths and auths[0].get("author"):
            nm = _safe_strip(auths[0]["author"].get("display_name"))
            if nm: first_author = nm.split()[-1].lower()

    return doi, title, year, first_author

def _score(rt, ra, ry, ct, ca, cy):
    # token_set_ratio is optimal for titles with slight ordering differences
    ts = fuzz.token_set_ratio(rt, ct) if rt and ct else 0
    am = 1 if ra and ca and ra == ca else 0
    ym = 1 if ry and cy and ry[:4] == cy[:4] else 0
    # Final truth weight
    score = (ts * 1.3) + (20 * am) + (15 * ym)
    return {"score": int(score), "ts": ts, "am": am, "ym": ym}

def _classify(score, am, ym, ts):
    # Optimized thresholds for academic matching
    if ts >= 88 and (am or ym) and score >= 128: return "verified"
    if ts >= 82 and score >= 110: return "likely"
    if ts >= 72: return "needs_review"
    return "not_found"


# -------------------------
# MAIN ENTRY POINT
# -------------------------
def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
):
    # CRITICAL: Fix 'refs' definition to prevent NameError
    refs = [_safe_strip(r) for r in references if _safe_strip(r)]
    if max_to_check:
        refs = refs[:max_to_check]

    rows = []
    for ref in refs:
        ref_year = _extract_year(ref)
        ref_author = _extract_first_author_surname(ref)
        ref_title = _extract_title_guess(ref)

        row = {"reference": ref, "status": "not_found", "doi": "", "score": 0}
        candidates = []

        try:
            # PHASE 1: Strict Search
            if use_crossref: candidates += _query_crossref_gold(ref_title, ref_author, ref_year, ref, 0)
            if use_openalex: candidates += _query_openalex(ref_title, ref_author, ref_year, ref, 0)

            # PHASE 2: ±1 Year Retry if no results found
            if not candidates and ref_year:
                if use_crossref: candidates += _query_crossref_gold(ref_title, ref_author, ref_year, ref, 1)
                if use_openalex: candidates += _query_openalex(ref_title, ref_author, ref_year, ref, 1)

            best_cand = None
            max_s = -1

            for cand in candidates:
                doi, title, year, author = _candidate_fields(cand)
                s_data = _score(ref_title, ref_author, ref_year, title, author, year)
                if s_data["score"] > max_s:
                    max_s = s_data["score"]
                    best_cand = {**cand, **s_data, "doi": doi, "title": title, "year": year}

            if best_cand:
                row["status"] = _classify(best_cand["score"], best_cand["am"], best_cand["ym"], best_cand["ts"])
                row["doi"] = best_cand["doi"]
                row["score"] = best_cand["score"]

        except Exception as e:
            row["error"] = str(e)

        rows.append(row)
        if throttle_s > 0: time.sleep(throttle_s)

    return rows
