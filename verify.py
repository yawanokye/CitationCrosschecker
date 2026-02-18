# verify.py
import re
import time
from typing import Any, Dict, List, Optional

import requests
from rapidfuzz import fuzz
from tenacity import retry, stop_after_attempt, wait_exponential

CROSSREF_API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"

YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b", re.I)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "citation-crosschecker/fastapi (contact: admin)"})


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=6))
def _get_json(url: str, params: dict) -> dict:
    r = SESSION.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def extract_doi(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"https?://doi\.org/(10\.\d{4,9}/[^\s<>\"]+)", text, flags=re.I)
    if m:
        doi = m.group(1)
    else:
        m2 = re.search(r"(10\.\d{4,9}/[^\s<>\"]+)", text, flags=re.I)
        if not m2:
            return None
        doi = m2.group(1)
    doi = doi.strip().strip(").,;:]}>\"'")
    return doi if doi.lower().startswith("10.") else None


def _as_int_year(y: Optional[str]) -> Optional[int]:
    if not y:
        return None
    m = re.search(r"(16|17|18|19|20)\d{2}", str(y))
    return int(m.group(0)) if m else None


def guess_title_snippet(ref: str) -> str:
    r = " ".join((ref or "").split())
    r = re.sub(r"https?://doi\.org/\S+", " ", r, flags=re.I)
    r = re.sub(r"\b10\.\d{4,9}/\S+", " ", r, flags=re.I)
    r = re.sub(rf"\(.*?\b{YEAR}\b.*?\)", " ", r, flags=re.I)
    r = " ".join(r.split())
    # remove leading author blob roughly
    r = re.sub(r"^[^\.]{1,220}\.\s*", " ", r)
    r = " ".join(r.split())
    words = r.split()
    return " ".join(words[:22])[:260]


def crossref_item_year(it: dict) -> Optional[int]:
    for key in ["issued", "published-print", "published-online", "created"]:
        dp = (it.get(key, {}) or {}).get("date-parts", [])
        if dp and dp[0]:
            try:
                return int(dp[0][0])
            except Exception:
                pass
    return None


def crossref_first_author_family(it: dict) -> str:
    authors = it.get("author") or []
    fam = authors[0].get("family") if authors else ""
    return fam or ""


def crossref_title(it: dict) -> str:
    t = it.get("title") or []
    return (t[0] if t else "") or ""


def openalex_year(it: dict) -> Optional[int]:
    y = it.get("publication_year")
    try:
        return int(y) if y else None
    except Exception:
        return None


def openalex_first_author_family(it: dict) -> str:
    authorships = it.get("authorships") or []
    if not authorships:
        return ""
    dn = (authorships[0].get("author") or {}).get("display_name") or ""
    return (dn.split()[-1] if dn else "") or ""


def openalex_title(it: dict) -> str:
    return (it.get("title") or "") or ""


def openalex_doi(it: dict) -> str:
    ids = it.get("ids") or {}
    d = ids.get("doi") or ""
    if d:
        d = d.replace("https://doi.org/", "").strip()
        d = d.strip(").,;:]}>\"'")
    return d


def author_match_ok(ref_surname: str, cand_surname: str) -> bool:
    if not ref_surname or not cand_surname:
        return False
    return ref_surname.strip().lower() == cand_surname.strip().lower()


def year_match_ok(ref_year: Optional[int], cand_year: Optional[int]) -> bool:
    if ref_year is None or cand_year is None:
        return False
    if ref_year == cand_year:
        return True
    return abs(ref_year - cand_year) == 1


def _crossref_lookup_by_doi(doi: str) -> Optional[dict]:
    try:
        data = _get_json(f"{CROSSREF_API}/{doi}", params={})
        return data.get("message")
    except Exception:
        return None


def _crossref_search(query: str, rows: int = 10) -> List[dict]:
    try:
        data = _get_json(CROSSREF_API, params={"query.bibliographic": query, "rows": rows})
        return data.get("message", {}).get("items", []) or []
    except Exception:
        return []


def _openalex_search(query: str, per_page: int = 10) -> List[dict]:
    try:
        data = _get_json(OPENALEX_API, params={"search": query, "per-page": per_page})
        return data.get("results", []) or []
    except Exception:
        return []


def verify_one_reference(
    reference_text: str,
    ref_year: Optional[str] = None,
    ref_first_author_surname: str = "",
    throttle_s: float = 0.25,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> Dict[str, Any]:
    """
    Returns a dict with:
    status: verified | likely | needs_review | not_found | offline
    source: crossref_doi | crossref | openalex
    score: int
    doi, matched_year, matched_first_author, matched_title, query_used
    """
    try:
        time.sleep(max(0.0, float(throttle_s or 0.0)))

        doi = extract_doi(reference_text)
        y_ref = _as_int_year(ref_year)
        title_snip = guess_title_snippet(reference_text)

        # DOI path first
        if doi and use_crossref:
            cr = _crossref_lookup_by_doi(doi)
            if cr:
                return {
                    "status": "verified",
                    "source": "crossref_doi",
                    "score": 100,
                    "doi": doi,
                    "matched_year": str(crossref_item_year(cr) or ""),
                    "matched_first_author": crossref_first_author_family(cr),
                    "matched_title": (crossref_title(cr) or "")[:180],
                    "query_used": "doi_lookup",
                    "error": "",
                }
            return {
                "status": "not_found",
                "source": "crossref_doi",
                "score": 0,
                "doi": doi,
                "matched_year": "",
                "matched_first_author": "",
                "matched_title": "",
                "query_used": "doi_lookup",
                "error": "",
            }

        # Build query
        parts = []
        if ref_first_author_surname:
            parts.append(ref_first_author_surname)
        if y_ref:
            parts.append(str(y_ref))
        if title_snip:
            parts.append(title_snip)

        query = " ".join(parts).strip() or (reference_text[:220] if reference_text else "")

        best = {
            "status": "not_found",
            "source": "",
            "score": 0,
            "doi": doi or "",
            "matched_year": "",
            "matched_first_author": "",
            "matched_title": "",
            "query_used": query[:220],
            "error": "",
        }

        # Crossref search
        if use_crossref:
            items = _crossref_search(query, rows=10)
            for it in items:
                cand_year = crossref_item_year(it)
                cand_fam = crossref_first_author_family(it)
                cand_title = crossref_title(it)
                cand_doi = (it.get("DOI") or "").strip()

                if ref_first_author_surname and cand_fam and not author_match_ok(ref_first_author_surname, cand_fam):
                    continue
                if y_ref and cand_year and not year_match_ok(y_ref, cand_year):
                    continue

                score = fuzz.WRatio(title_snip, cand_title) if (title_snip and cand_title) else 70
                status = "verified" if score >= 90 else ("likely" if score >= 82 else "needs_review")
                if score > best["score"]:
                    best = {
                        "status": status,
                        "source": "crossref",
                        "score": int(score),
                        "doi": cand_doi,
                        "matched_year": str(cand_year or ""),
                        "matched_first_author": cand_fam,
                        "matched_title": (cand_title or "")[:180],
                        "query_used": query[:220],
                        "error": "",
                    }

        # OpenAlex search
        if use_openalex:
            items = _openalex_search(query, per_page=10)
            for it in items:
                cand_year = openalex_year(it)
                cand_fam = openalex_first_author_family(it)
                cand_title = openalex_title(it)
                cand_doi = openalex_doi(it)

                if ref_first_author_surname and cand_fam and not author_match_ok(ref_first_author_surname, cand_fam):
                    continue
                if y_ref and cand_year and not year_match_ok(y_ref, cand_year):
                    continue

                score = fuzz.WRatio(title_snip, cand_title) if (title_snip and cand_title) else 70
                status = "verified" if score >= 90 else ("likely" if score >= 82 else "needs_review")
                if score > best["score"]:
                    best = {
                        "status": status,
                        "source": "openalex",
                        "score": int(score),
                        "doi": cand_doi,
                        "matched_year": str(cand_year or ""),
                        "matched_first_author": cand_fam,
                        "matched_title": (cand_title or "")[:180],
                        "query_used": query[:220],
                        "error": "",
                    }

        return best

    except Exception as e:
        return {
            "status": "offline",
            "source": "",
            "score": 0,
            "doi": "",
            "matched_year": "",
            "matched_first_author": "",
            "matched_title": "",
            "query_used": "",
            "error": str(e)[:220],
        }


def _extract_year_from_reference(ref: str) -> Optional[str]:
    m = YEAR_RE.search(ref or "")
    return m.group(1) if m else None


def _extract_first_surname_from_reference(ref: str) -> str:
    # Rough: take first token before comma
    if not ref:
        return ""
    pre = (ref.split(".")[0] if "." in ref else ref)[:120]
    if "," in pre:
        first = pre.split(",")[0].strip()
    else:
        first = pre.split()[0].strip() if pre.split() else ""
    return first


def verify_references_batch(
    references: List[str],
    max_to_check: int = 200,
    throttle_s: float = 0.25,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    work = (references or [])[: int(max_to_check)]

    for ref in work:
        y = _extract_year_from_reference(ref)
        a = _extract_first_surname_from_reference(ref)
        res = verify_one_reference(
            reference_text=ref,
            ref_year=y,
            ref_first_author_surname=a,
            throttle_s=throttle_s,
            use_crossref=use_crossref,
            use_openalex=use_openalex,
        )
        rows.append(
            {
                "reference": ref,
                "status": res.get("status", ""),
                "source": res.get("source", ""),
                "score": res.get("score", ""),
                "doi": res.get("doi", ""),
                "matched_year": res.get("matched_year", ""),
                "matched_first_author": res.get("matched_first_author", ""),
                "matched_title": res.get("matched_title", ""),
                "query_used": res.get("query_used", ""),
                "error": res.get("error", ""),
            }
        )
    return rows
