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


# ---------------------------------------------------------
# Helper functions
# ---------------------------------------------------------

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
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s\-:/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 20) -> Optional[dict]:
    try:
        headers = {
            "User-Agent": f"CitationCrosschecker (mailto:{MAILTO})",
            "Accept": "application/json",
        }
        r = requests.get(url, params=params, timeout=timeout, headers=headers)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


# ---------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------

def _candidate_fields(cand: Dict[str, Any]) -> Tuple[str, str, str, List[str], int, str]:

    src = _safe_strip((cand or {}).get("source"))
    item = (cand or {}).get("item") or {}

    doi = ""
    title = ""
    year = ""
    authors: List[str] = []
    api_score = 0
    journal = ""

    if src == "crossref":

        doi = _safe_strip(item.get("DOI"))

        titles = item.get("title") or []
        title_raw = _safe_str(titles[0]) if titles else ""
        title = _norm_text(title_raw)

        journal = _safe_str((item.get("container-title") or [""])[0])

        try:
            api_score = int(item.get("score") or 0)
        except Exception:
            api_score = 0

        pp = (((item.get("published-print") or {}).get("date-parts")) or [[None]])
        po = (((item.get("published-online") or {}).get("date-parts")) or [[None]])
        y = (pp[0][0] if pp and pp[0] else None) or (po[0][0] if po and po[0] else None)
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

        journal = _safe_str((item.get("host_venue") or {}).get("display_name"))

        for a in (item.get("authorships") or [])[:10]:
            au = (a or {}).get("author") or {}
            nm = _safe_strip(au.get("display_name"))
            if nm:
                last = nm.split()[-1].lower()
                last = re.sub(r"[^a-z\-']", "", last)
                if last:
                    authors.append(last)

    return doi, title, year, authors, api_score, journal


# ---------------------------------------------------------
# Peer review detection (ACII helper)
# ---------------------------------------------------------

def _detect_peer_review(source: str, journal: str) -> bool:
    if source in {"crossref", "openalex"} and journal:
        return True
    return False


# ---------------------------------------------------------
# Main verification batch
# ---------------------------------------------------------

def verify_references_batch(
    references: List[str],
    style: str = "apa",
    max_to_check: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> List[Dict[str, Any]]:

    refs = [r for r in (references or []) if _safe_strip(r)]

    if max_to_check and max_to_check > 0:
        refs = refs[:max_to_check]

    rows: List[Dict[str, Any]] = []

    for i, ref in enumerate(refs):

        ref_raw = _safe_strip(ref)

        row: Dict[str, Any] = {

            "reference_id": i + 1,
            "reference": ref_raw,

            "status": "offline",
            "source": "",
            "score": 0,

            "doi": "",
            "matched_title": "",
            "matched_year": "",
            "matched_authors": "",

            "title_score": 0,
            "author_overlap": 0,
            "year_match": 0,

            # ACII metadata
            "journal": "",
            "peer_reviewed": False,
            "citations_in_text": 1,
            "self_citation": False,

            "attempts": [],
        }

        try:

            # ---------------- Crossref ----------------

            if use_crossref:

                url = "https://api.crossref.org/works"
                params = {"query.bibliographic": ref_raw[:200], "rows": 5}

                data = _safe_get_json(url, params)

                items = (data or {}).get("message", {}).get("items", [])

                if items:

                    cand = {"source": "crossref", "item": items[0]}

                    doi, title, year, authors, api_score, journal = _candidate_fields(cand)

                    row.update({

                        "status": "verified",
                        "source": "crossref",

                        "doi": doi,
                        "matched_title": title,
                        "matched_year": year,
                        "matched_authors": ", ".join(authors),

                        "journal": journal,
                        "peer_reviewed": _detect_peer_review("crossref", journal),

                    })

            # ---------------- OpenAlex fallback ----------------

            if row["status"] != "verified" and use_openalex:

                url = "https://api.openalex.org/works"
                params = {"search": ref_raw[:200], "per-page": 3}

                data = _safe_get_json(url, params)

                results = (data or {}).get("results", [])

                if results:

                    cand = {"source": "openalex", "item": results[0]}

                    doi, title, year, authors, api_score, journal = _candidate_fields(cand)

                    row.update({

                        "status": "likely",
                        "source": "openalex",

                        "doi": doi,
                        "matched_title": title,
                        "matched_year": year,
                        "matched_authors": ", ".join(authors),

                        "journal": journal,
                        "peer_reviewed": _detect_peer_review("openalex", journal),

                    })

            rows.append(row)

        except Exception as e:

            row["status"] = "offline"
            row["error"] = _safe_str(e)

            rows.append(row)

        time.sleep(throttle_s)

    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
