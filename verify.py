# verify.py
# Strict online verification (Crossref + OpenAlex)
# Criteria (stricter):
# - Exact year match (if year is available)
# - Title must match very strongly (near-exact similarity)
# - Author match uses MULTIPLE authors where available (not just first author)
# - Returns DOI when verified

import re
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import requests
from rapidfuzz import fuzz
from tenacity import retry, stop_after_attempt, wait_exponential

CROSSREF_API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"

YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b", re.I)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "citation-crosschecker/fastapi (contact: admin)"})


# -----------------------------
# HTTP
# -----------------------------
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=6))
def _get_json(url: str, params: dict) -> dict:
    r = SESSION.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


# -----------------------------
# Normalisation
# -----------------------------
def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def norm_text(s: str) -> str:
    s = _strip_accents(s or "")
    s = s.lower()
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"\s+", " ", s)
    s = s.strip()
    return s


def norm_title(s: str) -> str:
    s = norm_text(s)
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"\b10\.\d{4,9}/\S+", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


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


# -----------------------------
# Reference parsing (title + authors)
# -----------------------------
def _clean_author_token(a: str) -> str:
    a = (a or "").strip()
    a = a.replace("&", " and ")
    a = re.sub(r"\bet\s+al\.?\b", " ", a, flags=re.I)
    a = re.sub(r"[^A-Za-z\-\s']", " ", a)
    a = re.sub(r"\s+", " ", a).strip()
    return a


def extract_authors_from_reference(ref: str, max_authors: int = 8) -> List[str]:
    """
    Returns a list of likely author surnames from the start of the reference.
    Works for typical APA-like: Surname, I., Surname, I., & Surname, I. (Year) ...
    Also tolerates some variations.
    """
    if not ref:
        return []

    s = " ".join(ref.split())
    # Cut at year if present
    m = YEAR_RE.search(s)
    head = s[: m.start()].strip() if m else s[:250].strip()

    head = _clean_author_token(head)

    # Split by 'and' or commas
    parts = [p.strip() for p in re.split(r"\s+and\s+|,", head) if p.strip()]

    surnames: List[str] = []
    for p in parts:
        toks = p.split()
        if not toks:
            continue
        # heuristic: surname is last token in chunk
        sn = toks[-1].strip()
        if len(sn) < 2:
            continue
        if not re.fullmatch(r"[A-Za-z\-']{2,40}", sn):
            continue
        surnames.append(sn)

    # De-dup preserve order
    out = []
    seen = set()
    for sn in surnames:
        k = sn.lower()
        if k not in seen:
            out.append(sn)
            seen.add(k)
        if len(out) >= max_authors:
            break
    return out


def extract_year_from_reference(ref: str) -> Optional[str]:
    m = YEAR_RE.search(ref or "")
    return m.group(1) if m else None


def extract_title_from_reference(ref: str) -> str:
    """
    Tries to pull a cleaner title candidate:
    - Remove DOI/URLs
    - Prefer quoted title if present
    - Else take text after year and before next period
    - Fallback to robust snippet
    """
    if not ref:
        return ""

    s = " ".join(ref.split())
    s = re.sub(r"https?://doi\.org/\S+", " ", s, flags=re.I)
    s = re.sub(r"\b10\.\d{4,9}/\S+", " ", s, flags=re.I)
    s = " ".join(s.split())

    # Quoted title
    qm = re.search(r"\"([^\"]{8,300})\"", s)
    if qm:
        return qm.group(1).strip()

    # After year, up to next period
    ym = YEAR_RE.search(s)
    if ym:
        tail = s[ym.end() :].strip()
        # remove leading punctuation
        tail = re.sub(r"^[\)\]\}\s\.\-:;]+", "", tail)
        # stop at first period that likely ends title
        # (titles sometimes have colon, keep it)
        pm = re.search(r"\.\s", tail)
        if pm:
            cand = tail[: pm.start()].strip()
            if len(cand.split()) >= 3:
                return cand

    # Fallback: robust snippet
    return guess_title_snippet(s)


def guess_title_snippet(ref: str) -> str:
    r = " ".join((ref or "").split())
    r = re.sub(r"https?://doi\.org/\S+", " ", r, flags=re.I)
    r = re.sub(r"\b10\.\d{4,9}/\S+", " ", r, flags=re.I)
    r = re.sub(rf"\(.*?\b{YEAR}\b.*?\)", " ", r, flags=re.I)
    r = " ".join(r.split())
    # remove leading author blob roughly
    r = re.sub(r"^[^\.]{1,260}\.\s*", " ", r)
    r = " ".join(r.split())
    words = r.split()
    return " ".join(words[:24])[:320]


# -----------------------------
# Crossref helpers
# -----------------------------
def crossref_item_year(it: dict) -> Optional[int]:
    for key in ["issued", "published-print", "published-online", "created"]:
        dp = (it.get(key, {}) or {}).get("date-parts", [])
        if dp and dp[0]:
            try:
                return int(dp[0][0])
            except Exception:
                pass
    return None


def crossref_authors_families(it: dict, max_n: int = 10) -> List[str]:
    authors = it.get("author") or []
    fams = []
    for a in authors[:max_n]:
        fam = (a.get("family") or "").strip()
        if fam:
            fams.append(fam)
    return fams


def crossref_title(it: dict) -> str:
    t = it.get("title") or []
    return (t[0] if t else "") or ""


def _crossref_lookup_by_doi(doi: str) -> Optional[dict]:
    try:
        data = _get_json(f"{CROSSREF_API}/{doi}", params={})
        return data.get("message")
    except Exception:
        return None


def _crossref_search_strict(title: str, authors: List[str], year: Optional[int], rows: int = 20) -> List[dict]:
    """
    Uses Crossref query.title + query.author and year filter if provided.
    This is stricter than query.bibliographic.
    """
    params: Dict[str, Any] = {"rows": rows}
    title = (title or "").strip()
    if title:
        params["query.title"] = title[:300]
    # Include up to 2 authors in query.author to reduce false hits
    if authors:
        params["query.author"] = " ".join(authors[:2])[:120]

    # Year filter: exact window
    if year:
        params["filter"] = f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31"

    try:
        data = _get_json(CROSSREF_API, params=params)
        return data.get("message", {}).get("items", []) or []
    except Exception:
        return []


# -----------------------------
# OpenAlex helpers
# -----------------------------
def openalex_year(it: dict) -> Optional[int]:
    y = it.get("publication_year")
    try:
        return int(y) if y else None
    except Exception:
        return None


def openalex_authors_families(it: dict, max_n: int = 10) -> List[str]:
    authorships = it.get("authorships") or []
    fams = []
    for au in authorships[:max_n]:
        dn = ((au.get("author") or {}).get("display_name") or "").strip()
        if dn:
            fams.append(dn.split()[-1])
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


def _openalex_search_strict(title: str, year: Optional[int], per_page: int = 20) -> List[dict]:
    params: Dict[str, Any] = {"search": (title or "")[:300], "per-page": per_page}
    if year:
        params["filter"] = f"publication_year:{year}"
    try:
        data = _get_json(OPENALEX_API, params=params)
        return data.get("results", []) or []
    except Exception:
        return []


# -----------------------------
# Matching logic (STRICT)
# -----------------------------
def _author_overlap(ref_authors: List[str], cand_authors: List[str]) -> Tuple[int, int]:
    """
    returns (matches, required)
    required is dynamic:
      - if ref has >=3 authors -> require 2 matches
      - if ref has 2 authors -> require 2 matches
      - if ref has 1 author -> require 1 match
    """
    ref_set = {a.lower() for a in (ref_authors or []) if a}
    cand_set = {a.lower() for a in (cand_authors or []) if a}
    matches = len(ref_set.intersection(cand_set))

    if len(ref_set) >= 2:
        required = 2
    else:
        required = 1

    # If we could not extract authors reliably, force required=0 (don’t block)
    if len(ref_set) == 0:
        required = 0

    return matches, required


def _year_ok_strict(ref_year: Optional[int], cand_year: Optional[int]) -> bool:
    if ref_year is None:
        return True  # allow if reference has no year
    if cand_year is None:
        return False
    return ref_year == cand_year


def _title_score_strict(ref_title: str, cand_title: str) -> int:
    a = norm_title(ref_title)
    b = norm_title(cand_title)
    if not a or not b:
        return 0
    # Use two measures and take the minimum for strictness
    s1 = fuzz.WRatio(a, b)
    s2 = fuzz.token_set_ratio(a, b)
    return int(min(s1, s2))


def _is_verified_strict(title_score: int, author_matches: int, author_required: int, year_ok: bool) -> bool:
    if not year_ok:
        return False
    if title_score < 96:  # near-exact
        return False
    if author_required > 0 and author_matches < author_required:
        return False
    return True


# -----------------------------
# Public API
# -----------------------------
def verify_one_reference(
    reference_text: str,
    throttle_s: float = 0.25,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> Dict[str, Any]:
    """
    Returns:
      status: verified | needs_review | not_found | offline
      source: crossref_doi | crossref | openalex
      score: int (strict title score)
      verified: bool
      doi, matched_year, matched_authors, matched_title
      query_used, error
      reason: why verified/not
    """
    try:
        time.sleep(max(0.0, float(throttle_s or 0.0)))

        ref_doi = extract_doi(reference_text)
        ref_year = _as_int_year(extract_year_from_reference(reference_text))
        ref_authors = extract_authors_from_reference(reference_text)
        ref_title = extract_title_from_reference(reference_text)

        # DOI path first (Crossref DOI is authoritative)
        if ref_doi and use_crossref:
            cr = _crossref_lookup_by_doi(ref_doi)
            if cr:
                cand_year = crossref_item_year(cr)
                cand_title = crossref_title(cr)
                cand_authors = crossref_authors_families(cr)

                title_score = _title_score_strict(ref_title, cand_title) if ref_title else 100
                year_ok = _year_ok_strict(ref_year, cand_year)
                am, req = _author_overlap(ref_authors, cand_authors)

                verified = year_ok and (req == 0 or am >= req) and (title_score >= 90)
                reason = "DOI lookup matched" if verified else "DOI found but metadata mismatch"

                return {
                    "status": "verified" if verified else "needs_review",
                    "verified": bool(verified),
                    "source": "crossref_doi",
                    "score": int(title_score),
                    "doi": ref_doi,
                    "matched_year": str(cand_year or ""),
                    "matched_authors": ", ".join(cand_authors[:6]),
                    "matched_title": (cand_title or "")[:220],
                    "query_used": "doi_lookup",
                    "error": "",
                    "reason": reason,
                }

            return {
                "status": "not_found",
                "verified": False,
                "source": "crossref_doi",
                "score": 0,
                "doi": ref_doi,
                "matched_year": "",
                "matched_authors": "",
                "matched_title": "",
                "query_used": "doi_lookup",
                "error": "",
                "reason": "DOI not found in Crossref",
            }

        # Build strict query parts
        title_q = (ref_title or "").strip()
        if not title_q:
            # Title missing, fall back (still strict-ish)
            title_q = guess_title_snippet(reference_text)

        query_used = f"title={title_q[:180]}"
        if ref_authors:
            query_used += f" | authors={','.join(ref_authors[:3])}"
        if ref_year:
            query_used += f" | year={ref_year}"

        best = {
            "status": "not_found",
            "verified": False,
            "source": "",
            "score": 0,
            "doi": "",
            "matched_year": "",
            "matched_authors": "",
            "matched_title": "",
            "query_used": query_used[:240],
            "error": "",
            "reason": "No acceptable match found",
        }

        # --- Crossref strict search
        if use_crossref:
            items = _crossref_search_strict(title=title_q, authors=ref_authors, year=ref_year, rows=20)
            for it in items:
                cand_year = crossref_item_year(it)
                cand_title = crossref_title(it)
                cand_authors = crossref_authors_families(it)
                cand_doi = (it.get("DOI") or "").strip()

                year_ok = _year_ok_strict(ref_year, cand_year)
                title_score = _title_score_strict(title_q, cand_title)
                am, req = _author_overlap(ref_authors, cand_authors)
                verified = _is_verified_strict(title_score, am, req, year_ok)

                # Candidate ranking: prefer verified; otherwise highest strict title score with year_ok
                rank = title_score + (50 if verified else 0) + (10 if year_ok else 0) + (5 if am >= req and req > 0 else 0)

                if rank > best["score"]:
                    best = {
                        "status": "verified" if verified else ("needs_review" if year_ok and title_score >= 90 else "not_found"),
                        "verified": bool(verified),
                        "source": "crossref",
                        "score": int(rank),
                        "doi": cand_doi,
                        "matched_year": str(cand_year or ""),
                        "matched_authors": ", ".join(cand_authors[:6]),
                        "matched_title": (cand_title or "")[:220],
                        "query_used": query_used[:240],
                        "error": "",
                        "reason": (
                            "Strict match: title + authors + year" if verified
                            else f"Closest Crossref hit, title_score={title_score}, author_matches={am}/{req}, year_ok={year_ok}"
                        ),
                    }

        # --- OpenAlex strict search
        if use_openalex:
            items = _openalex_search_strict(title=title_q, year=ref_year, per_page=20)
            for it in items:
                cand_year = openalex_year(it)
                cand_title = openalex_title(it)
                cand_authors = openalex_authors_families(it)
                cand_doi = openalex_doi(it)

                year_ok = _year_ok_strict(ref_year, cand_year)
                title_score = _title_score_strict(title_q, cand_title)
                am, req = _author_overlap(ref_authors, cand_authors)
                verified = _is_verified_strict(title_score, am, req, year_ok)

                rank = title_score + (50 if verified else 0) + (10 if year_ok else 0) + (5 if am >= req and req > 0 else 0)

                if rank > best["score"]:
                    best = {
                        "status": "verified" if verified else ("needs_review" if year_ok and title_score >= 90 else "not_found"),
                        "verified": bool(verified),
                        "source": "openalex",
                        "score": int(rank),
                        "doi": cand_doi,
                        "matched_year": str(cand_year or ""),
                        "matched_authors": ", ".join(cand_authors[:6]),
                        "matched_title": (cand_title or "")[:220],
                        "query_used": query_used[:240],
                        "error": "",
                        "reason": (
                            "Strict match: title + authors + year" if verified
                            else f"Closest OpenAlex hit, title_score={title_score}, author_matches={am}/{req}, year_ok={year_ok}"
                        ),
                    }

        # Convert rank score back to a meaningful strict title score for display (optional)
        # We keep rank in "score" because it helps selection, but you can display reason anyway.
        return best

    except Exception as e:
        return {
            "status": "offline",
            "verified": False,
            "source": "",
            "score": 0,
            "doi": "",
            "matched_year": "",
            "matched_authors": "",
            "matched_title": "",
            "query_used": "",
            "error": str(e)[:220],
            "reason": "Verification crashed",
        }


def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,          # 0 = ALL
    throttle_s: float = 0.25,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> List[Dict[str, Any]]:
    work = list(references or [])
    if max_to_check and max_to_check > 0:
        work = work[: int(max_to_check)]

    rows: List[Dict[str, Any]] = []
    for ref in work:
        res = verify_one_reference(
            reference_text=ref,
            throttle_s=throttle_s,
            use_crossref=use_crossref,
            use_openalex=use_openalex,
        )
        rows.append(
            {
                "reference": ref,
                "status": res.get("status", ""),
                "verified": res.get("verified", False),
                "source": res.get("source", ""),
                "score": res.get("score", ""),
                "doi": res.get("doi", ""),
                "matched_year": res.get("matched_year", ""),
                "matched_authors": res.get("matched_authors", ""),
                "matched_title": res.get("matched_title", ""),
                "query_used": res.get("query_used", ""),
                "reason": res.get("reason", ""),
                "error": res.get("error", ""),
            }
        )
    return rows
