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
SESSION.headers.update({"User-Agent": "citation-crosschecker/fastapi (contact: admin@example.com)"})

COMMON_NON_AUTHOR = {
    "journal","research","study","analysis","results","discussion","evidence","theory","review",
    "report","proceedings","conference","international","national","university","press","publisher",
    "volume","vol","issue","no","pp","pages","doi","http","https","org",
}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=6))
def _get_json(url: str, params: dict) -> dict:
    r = SESSION.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _as_int_year(y: Optional[str]) -> Optional[int]:
    if not y:
        return None
    m = re.search(r"(16|17|18|19|20)\d{2}", str(y))
    return int(m.group(0)) if m else None


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


def extract_year_from_reference(ref: str) -> Optional[str]:
    m = YEAR_RE.search(ref or "")
    return m.group(1) if m else None


def extract_authors_surnames(ref: str, max_authors: int = 5) -> List[str]:
    if not ref:
        return []
    y = YEAR_RE.search(ref)
    head = ref[: y.start()] if y else ref[:240]

    # Prefer APA-like "Surname," patterns
    surnames = []
    for m in re.finditer(r"\b([A-Z][A-Za-z\-']{1,40})\s*,", head):
        s = m.group(1).strip()
        if _norm(s) not in COMMON_NON_AUTHOR:
            surnames.append(s)
        if len(surnames) >= max_authors:
            break

    # Fallback: first token before comma
    if not surnames and "," in head:
        first = head.split(",")[0].strip()
        if first and _norm(first) not in COMMON_NON_AUTHOR:
            surnames = [first]

    # Dedup
    seen = set()
    out = []
    for s in surnames:
        k = _norm(s)
        if k and k not in seen:
            out.append(s)
            seen.add(k)
    return out


def extract_title_from_reference(ref: str) -> str:
    if not ref:
        return ""
    r = " ".join(ref.split())
    r = re.sub(r"https?://doi\.org/\S+", " ", r, flags=re.I)
    r = re.sub(r"\b10\.\d{4,9}/\S+", " ", r, flags=re.I)

    # After "(YEAR). "
    m = re.search(rf"\(\s*{YEAR}\s*\)\.\s*", r)
    if m:
        tail = r[m.end():]
        parts = tail.split(".")
        title = parts[0].strip() if parts else ""
        return title[:300]

    # If no "(YEAR)." pattern, use first sentence chunk
    parts = r.split(".")
    return (parts[0].strip() if parts else r.strip())[:300]


def crossref_item_year(it: dict) -> Optional[int]:
    for key in ["issued", "published-print", "published-online", "created"]:
        dp = (it.get(key, {}) or {}).get("date-parts", [])
        if dp and dp[0]:
            try:
                return int(dp[0][0])
            except Exception:
                pass
    return None


def crossref_authors_families(it: dict, max_authors: int = 8) -> List[str]:
    authors = it.get("author") or []
    fams = []
    for a in authors[:max_authors]:
        fam = (a.get("family") or "").strip()
        if fam and _norm(fam) not in COMMON_NON_AUTHOR:
            fams.append(fam)
    return fams


def crossref_title(it: dict) -> str:
    t = it.get("title") or []
    return (t[0] if t else "") or ""


def openalex_year(it: dict) -> Optional[int]:
    y = it.get("publication_year")
    try:
        return int(y) if y else None
    except Exception:
        return None


def openalex_authors_families(it: dict, max_authors: int = 8) -> List[str]:
    authorships = it.get("authorships") or []
    fams = []
    for au in authorships[:max_authors]:
        dn = ((au.get("author") or {}).get("display_name") or "").strip()
        if dn:
            fam = dn.split()[-1]
            if _norm(fam) not in COMMON_NON_AUTHOR:
                fams.append(fam)
    return fams


def openalex_title(it: dict) -> str:
    return (it.get("title") or "") or ""


def openalex_doi(it: dict) -> str:
    ids = it.get("ids") or {}
    d = ids.get("doi") or ""
    if d:
        d = d.replace("https://doi.org/", "").strip()
        d = d.strip(").,;:]}>\"'")
    return d


def author_overlap(ref_surnames: List[str], cand_surnames: List[str]) -> int:
    rs = {_norm(x) for x in (ref_surnames or []) if x}
    cs = {_norm(x) for x in (cand_surnames or []) if x}
    return len(rs.intersection(cs))


def year_score(ref_year: Optional[int], cand_year: Optional[int]) -> float:
    if ref_year is None or cand_year is None:
        return 0.0
    if ref_year == cand_year:
        return 1.0
    d = abs(ref_year - cand_year)
    if d == 1:
        return 0.8
    if d == 2:
        return 0.4
    return 0.0


def _crossref_lookup_by_doi(doi: str) -> Optional[dict]:
    try:
        data = _get_json(f"{CROSSREF_API}/{doi}", params={})
        return data.get("message")
    except Exception:
        return None


def _crossref_search(query: str, rows: int = 12) -> List[dict]:
    try:
        data = _get_json(CROSSREF_API, params={"query.bibliographic": query, "rows": rows})
        return data.get("message", {}).get("items", []) or []
    except Exception:
        return []


def _openalex_search(query: str, per_page: int = 12) -> List[dict]:
    try:
        data = _get_json(OPENALEX_API, params={"search": query, "per-page": per_page})
        return data.get("results", []) or []
    except Exception:
        return []


def verify_one_reference_optimised(
    reference_text: str,
    throttle_s: float = 0.25,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> Dict[str, Any]:
    """
    Optimised verification:
      - Weighted score: Title(0.60) + Authors(0.30) + Year(0.10)
      - Controlled relaxation: year ±2 gives some credit, not an outright rejection
      - Requires at least 1 author overlap if reference has authors
      - Guards against title/journal tokens being treated as authors
    """
    try:
        time.sleep(max(0.0, float(throttle_s or 0.0)))

        doi = extract_doi(reference_text)
        ref_year = _as_int_year(extract_year_from_reference(reference_text))
        ref_authors = extract_authors_surnames(reference_text, max_authors=5)
        ref_title = extract_title_from_reference(reference_text)

        # DOI shortcut
        if doi and use_crossref:
            cr = _crossref_lookup_by_doi(doi)
            if cr:
                return {
                    "status": "verified",
                    "source": "crossref_doi",
                    "score": 100,
                    "doi": doi,
                    "matched_year": str(crossref_item_year(cr) or ""),
                    "matched_authors": ", ".join(crossref_authors_families(cr)[:5]),
                    "matched_title": (crossref_title(cr) or "")[:180],
                    "query_used": "doi_lookup",
                    "error": "",
                }

        # Query strategy: title + first author + year (when available)
        q_parts = []
        if ref_title:
            q_parts.append(ref_title)
        if ref_authors:
            q_parts.append(ref_authors[0])
        if ref_year:
            q_parts.append(str(ref_year))
        query = " ".join(q_parts).strip() or reference_text[:220]

        best = {
            "status": "not_found",
            "source": "",
            "score": 0,
            "doi": doi or "",
            "matched_year": "",
            "matched_authors": "",
            "matched_title": "",
            "query_used": query[:220],
            "error": "",
        }

        # If ref has authors, require at least one overlap to accept candidate
        require_author_overlap = len(ref_authors) > 0

        def evaluate_candidate(cand_title: str, cand_year: Optional[int], cand_authors: List[str], cand_doi: str, source: str):
            nonlocal best

            # Title similarity (relaxed but guarded)
            t_sim = fuzz.WRatio(_norm(ref_title), _norm(cand_title)) if (ref_title and cand_title) else 0
            if t_sim < 82:
                return

            # Author overlap score
            overlap = author_overlap(ref_authors, cand_authors)
            if require_author_overlap and overlap < 1:
                return

            # author ratio: overlap / min(len(ref), len(cand))
            denom = max(1, min(len(ref_authors), len(cand_authors)))
            a_ratio = overlap / denom if denom else 0.0

            # Year score
            y_sc = year_score(ref_year, cand_year)

            # Weighted score (0..100)
            score = (0.60 * (t_sim / 100.0) + 0.30 * a_ratio + 0.10 * y_sc) * 100.0
            score_i = int(round(score))

            if score_i > best["score"]:
                if score_i >= 90:
                    status = "verified"
                elif score_i >= 80:
                    status = "likely"
                elif score_i >= 70:
                    status = "needs_review"
                else:
                    status = "not_found"

                best = {
                    "status": status,
                    "source": source,
                    "score": score_i,
                    "doi": cand_doi or "",
                    "matched_year": str(cand_year or ""),
                    "matched_authors": ", ".join(cand_authors[:5]),
                    "matched_title": (cand_title or "")[:180],
                    "query_used": query[:220],
                    "error": "",
                }

        if use_crossref:
            items = _crossref_search(query, rows=12)
            for it in items:
                evaluate_candidate(
                    cand_title=crossref_title(it),
                    cand_year=crossref_item_year(it),
                    cand_authors=crossref_authors_families(it, max_authors=8),
                    cand_doi=(it.get("DOI") or "").strip(),
                    source="crossref",
                )

        if use_openalex:
            items = _openalex_search(query, per_page=12)
            for it in items:
                evaluate_candidate(
                    cand_title=openalex_title(it),
                    cand_year=openalex_year(it),
                    cand_authors=openalex_authors_families(it, max_authors=8),
                    cand_doi=openalex_doi(it),
                    source="openalex",
                )

        return best

    except Exception as e:
        return {
            "status": "offline",
            "source": "",
            "score": 0,
            "doi": "",
            "matched_year": "",
            "matched_authors": "",
            "matched_title": "",
            "query_used": "",
            "error": str(e)[:220],
        }


def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.25,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> List[Dict[str, Any]]:
    work = references or []
    if max_to_check and int(max_to_check) > 0:
        work = work[: int(max_to_check)]

    rows: List[Dict[str, Any]] = []
    for ref in work:
        res = verify_one_reference_optimised(
            reference_text=ref,
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
                "matched_authors": res.get("matched_authors", ""),
                "matched_title": res.get("matched_title", ""),
                "query_used": res.get("query_used", ""),
                "error": res.get("error", ""),
            }
        )
    return rows
