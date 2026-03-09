# verify.py
import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple

import requests
from rapidfuzz import fuzz


_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}

MAILTO = (
    os.getenv("CITATION_CROSSCHECKER_MAILTO")
    or os.getenv("CROSSREF_MAILTO")
    or os.getenv("OPENALEX_MAILTO")
    or ""
)
MAILTO = (MAILTO or "").strip()

UNPAYWALL_EMAIL = (os.getenv("UNPAYWALL_EMAIL") or MAILTO or "").strip()


# ---------- helpers ----------
def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


def _safe_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    try:
        return str(x)
    except Exception:
        return ""


def _safe_strip(x: Any) -> str:
    return _safe_str(x).strip()


def _norm_text(s: str) -> str:
    s = _safe_strip(s).lower()
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[^\w\s\-:/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 22) -> Optional[dict]:
    try:
        headers = {"User-Agent": "CitationCrosschecker/1.0", "Accept": "application/json"}
        r = requests.get(url, params=params, timeout=timeout, headers=headers)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _extract_year(text: str) -> str:
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", text or "", flags=re.I)
    return (_safe_str(m.group(1)) + (_safe_str(m.group(2)) if m and m.group(2) else "")).lower() if m else ""


def _strip_leading_numbering(text: str) -> str:
    t = _safe_strip(text)
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)
    return t.strip()


def _extract_doi(text: str) -> str:
    t = _safe_str(text)
    m = re.search(r"(10\.\d{4,9}/[^\s]+)", t, flags=re.I)
    if not m:
        return ""
    return _safe_strip(m.group(1)).rstrip(").,;")


def _clean_query_string(s: str) -> str:
    s = _safe_strip(s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > 280:
        s = s[:280].rstrip()
    return s


def _doi_equal(a: str, b: str) -> bool:
    a = _safe_strip(a).lower()
    b = _safe_strip(b).lower()
    if not a or not b:
        return False
    return a == b


# ---------- queries ----------

def _query_crossref_by_doi(doi: str) -> Optional[Dict[str, Any]]:
    doi = _safe_strip(doi)
    if not doi:
        return None

    url = f"https://api.crossref.org/works/{doi}"

    params: Dict[str, Any] = {}
    if MAILTO:
        params["mailto"] = MAILTO

    data = _safe_get_json(url, params=params)

    if not data:
        return None

    item = data.get("message") or {}

    return {"source": "crossref", "item": item or {}, "query_used": f"doi:{doi}"}


def _query_crossref(q: str, a_query: str) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"

    params: Dict[str, Any] = {
        "query.bibliographic": q,
        "rows": 8,        # EXTENDED candidate pool
        "sort": "score",
        "order": "desc",
    }

    if a_query:
        params["query.author"] = a_query

    if MAILTO:
        params["mailto"] = MAILTO

    data = _safe_get_json(url, params=params)

    if not data:
        return []

    items = ((data.get("message") or {}).get("items") or [])

    return [{"source": "crossref", "item": it or {}, "query_used": q} for it in items]


def _query_openalex_by_doi(doi: str) -> Optional[Dict[str, Any]]:
    doi = _safe_strip(doi)
    if not doi:
        return None

    doi_url = "https://doi.org/" + doi.lower()

    url = "https://api.openalex.org/works/" + doi_url

    params: Dict[str, Any] = {}
    if MAILTO:
        params["mailto"] = MAILTO

    data = _safe_get_json(url, params=params)

    if not data:
        return None

    return {"source": "openalex", "item": data or {}, "query_used": f"doi:{doi}"}


def _query_openalex(q: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"

    params: Dict[str, Any] = {
        "search": q,
        "per-page": 8,      # EXTENDED candidate pool
    }

    if MAILTO:
        params["mailto"] = MAILTO

    data = _safe_get_json(url, params=params)

    if not data:
        return []

    results = data.get("results") or []

    return [{"source": "openalex", "item": it or {}, "query_used": q} for it in results]


# ---------- NEW: Semantic Scholar ----------

def _query_semantic_scholar(q: str) -> List[Dict[str, Any]]:
    url = "https://api.semanticscholar.org/graph/v1/paper/search"

    params = {
        "query": q,
        "limit": 5,
        "fields": "title,year,authors,externalIds"
    }

    data = _safe_get_json(url, params=params)

    if not data:
        return []

    rows = []

    for r in data.get("data", []):
        rows.append({
            "source": "semantic_scholar",
            "item": r,
            "query_used": q
        })

    return rows


def _query_unpaywall_by_doi(doi: str) -> Optional[Dict[str, Any]]:
    doi = _safe_strip(doi)

    if not doi or not UNPAYWALL_EMAIL:
        return None

    url = f"https://api.unpaywall.org/v2/{doi}"

    params = {"email": UNPAYWALL_EMAIL}

    data = _safe_get_json(url, params=params)

    if not data:
        return None

    return {"source": "unpaywall", "item": data or {}, "query_used": f"doi:{doi}"}


# ---------- candidate extraction ----------

def _candidate_fields(cand: Dict[str, Any]) -> Tuple[str, str, str, List[str], int]:

    src = _safe_strip((cand or {}).get("source"))
    item = (cand or {}).get("item") or {}

    doi = ""
    title = ""
    year = ""
    authors: List[str] = []
    api_score = 0

    if src == "crossref":

        doi = _safe_strip(item.get("DOI"))

        titles = item.get("title") or []

        title_raw = _safe_str(titles[0]).strip() if titles else ""

        title = _norm_text(title_raw)

        try:
            api_score = int(item.get("score") or 0)
        except Exception:
            api_score = 0

        pp = (((item.get("published-print") or {}).get("date-parts")) or [[None]])

        y = pp[0][0] if pp and pp[0] else ""

        year = _safe_str(y)

        for au in (item.get("author") or [])[:10]:
            fam = _safe_strip((au or {}).get("family")).lower()
            fam = re.sub(r"[^a-z\-']", "", fam)
            if fam:
                authors.append(fam)

    elif src == "openalex":

        doi_raw = item.get("doi")

        doi = _safe_str(doi_raw).replace("https://doi.org/", "")

        title = _norm_text(_safe_strip(item.get("title")))

        year = _safe_strip(item.get("publication_year"))

        for a in (item.get("authorships") or [])[:10]:

            au = (a or {}).get("author") or {}

            nm = _safe_strip(au.get("display_name"))

            if nm:

                last = nm.split()[-1].lower()

                last = re.sub(r"[^a-z\-']", "", last)

                authors.append(last)

    elif src == "semantic_scholar":

        ext = item.get("externalIds") or {}

        doi = _safe_strip(ext.get("DOI"))

        title = _norm_text(_safe_strip(item.get("title")))

        year = _safe_strip(item.get("year"))

        for a in (item.get("authors") or []):

            nm = _safe_strip(a.get("name"))

            if nm:

                last = nm.split()[-1].lower()

                last = re.sub(r"[^a-z\-']", "", last)

                authors.append(last)

    elif src == "unpaywall":

        doi = _safe_strip(item.get("doi"))

        title = _norm_text(_safe_strip(item.get("title")))

        year = _safe_strip(item.get("year")) or ""

    return doi, title, year, authors, api_score
