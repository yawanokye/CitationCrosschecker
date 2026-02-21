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


def _safe_get_json(url: str, params: Optional[dict] = None, timeout: int = 22) -> Optional[dict]:
    try:
        headers = {
            "User-Agent": "CitationCrosschecker/1.0",
            "Accept": "application/json",
        }
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


def _extract_title_guess(ref: str) -> str:
    """
    Title guess from a reference string:
    - remove numbering
    - remove DOI
    - take first sentence after (YEAR) when possible
    """
    t = _strip_leading_numbering(ref)
    t = re.sub(r"\s+", " ", t).strip()

    # remove DOI forms
    t = re.sub(r"(doi\s*:\s*)?10\.\d{4,9}/\S+", "", t, flags=re.I)
    t = re.sub(r"https?://doi\.org/10\.\d{4,9}/\S+", "", t, flags=re.I)

    parts = re.split(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?", t, maxsplit=1, flags=re.I)
    if len(parts) >= 3:
        after = _safe_strip(parts[2])
    else:
        m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", t, flags=re.I)
        after = t[m.end():].strip() if m else t

    after = after.lstrip(". ").strip()
    title = after.split(".", 1)[0].strip() if "." in after else after.strip()

    if len(title) < 12:
        title = t.strip()

    return title


def _clean_query_string(s: str) -> str:
    s = _safe_strip(s)
    s = re.sub(r"\s+", " ", s)
    if len(s) > 280:
        s = s[:280].rstrip()
    return s


# -----------------------------
# Author extraction for multi-author precision
# -----------------------------
def _extract_author_surnames(ref: str, max_authors: int = 3) -> List[str]:
    """
    Best-effort extraction of up to max_authors surnames from a reference string.
    Handles typical styles like:
      - "Austin, P. C., & Steyerberg, E. W. (2017)..."
      - "Austin PC and Steyerberg EW (2017)..."
      - "Austin P. C.; Steyerberg E. W. (2017)..."
    """
    t = _strip_leading_numbering(ref)
    if not t:
        return []

    # Cut at year if present, so we don't treat title words as authors
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", t, flags=re.I)
    head = t[: m.start()].strip() if m else t

    head = head.replace("&", " and ")
    head = re.sub(r"\bet\s+al\.?\b", "", head, flags=re.I)
    head = re.sub(r"\s+", " ", head).strip()

    # Split author chunks
    chunks = re.split(r"\band\b|;", head, flags=re.I)

    parts: List[str] = []
    for ch in chunks:
        ch = ch.strip()
        if not ch:
            continue
        # keep commas to detect "Surname, Initials"
        parts.extend([p.strip() for p in ch.split(",") if p.strip()])

    surnames: List[str] = []
    comma_style = ("," in head)

    for p in parts:
        # remove non-letter/hyphen/apostrophe, keep whitespace
        p2 = re.sub(r"[^A-Za-z\-'\s]", " ", p).strip()
        if not p2:
            continue
        toks = [x for x in p2.split() if x]
        if not toks:
            continue

        surname = toks[0] if comma_style else toks[-1]
        surname = re.sub(r"[^A-Za-z\-']", "", surname).lower().strip()

        # Avoid adding single-letter junk
        if surname and len(surname) >= 2 and surname not in surnames:
            surnames.append(surname)
        if len(surnames) >= max_authors:
            break

    return surnames


def _authors_to_query(authors: List[str], max_join: int = 2) -> str:
    authors = [a for a in (authors or []) if _safe_strip(a)]
    if not authors:
        return ""
    return " ".join(authors[:max_join]).strip()


def _build_biblio_query(title: str, authors: List[str], year: str, raw_ref: str) -> str:
    """
    Strong query: title + up to 2 author surnames + year.
    This improves precision for multiple-author papers.
    """
    base = title if len(_safe_str(title)) >= 12 else _safe_str(raw_ref)
    base = _clean_query_string(base)

    y4 = _safe_str(year)[:4] if _safe_str(year)[:4].isdigit() else ""
    a_str = _authors_to_query(authors, max_join=2)

    bits = [base]
    if a_str:
        bits.append(a_str)
    if y4:
        bits.append(y4)

    return _clean_query_string(" ".join(bits))


# -----------------------------
# Crossref queries
# -----------------------------
def _query_crossref_by_doi(doi: str) -> Optional[Dict[str, Any]]:
    doi = _safe_strip(doi)
    if not doi:
        return None
    url = f"https://api.crossref.org/works/{doi}"
    params: Dict[str, Any] = {}
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=22)
    if not data:
        return None
    item = (data.get("message") or {})
    return {"source": "crossref", "item": item or {}, "query_used": f"doi:{doi}"}


def _query_crossref_gold(title: str, authors_list: List[str], year: str, raw_ref: str) -> List[Dict[str, Any]]:
    """
    Crossref search using:
      - query.bibliographic (title + authors + year)
      - query.author (up to 2 surnames as a string)
      - year filter
      - sort by score desc
      - default to journal-article for precision (can be relaxed if needed)
    """
    url = "https://api.crossref.org/works"
    q = _build_biblio_query(title, authors_list, year, raw_ref)

    y4 = _safe_str(year)[:4]
    a_str = _authors_to_query(authors_list, max_join=2)

    params: Dict[str, Any] = {
        "query.bibliographic": q,
        "rows": 3,  # a bit more room helps multi-author disambiguation
        "sort": "score",
        "order": "desc",
    }
    if a_str:
        params["query.author"] = a_str
    if MAILTO:
        params["mailto"] = MAILTO

    filters: List[str] = []
    if y4.isdigit():
        filters.append(f"from-pub-date:{y4}-01-01")
        filters.append(f"until-pub-date:{y4}-12-31")

    # Default precision bias (journal-heavy reference lists)
    filters.append("type:journal-article")

    if filters:
        params["filter"] = ",".join(filters)

    data = _safe_get_json(url, params=params, timeout=22)
    if not data:
        return []

    items = ((data.get("message") or {}).get("items") or [])
    return [{"source": "crossref", "item": it or {}, "query_used": q} for it in items]


# -----------------------------
# OpenAlex queries
# -----------------------------
def _query_openalex_by_doi(doi: str) -> Optional[Dict[str, Any]]:
    doi = _safe_strip(doi)
    if not doi:
        return None
    doi_url = "https://doi.org/" + doi.lower()
    url = "https://api.openalex.org/works/" + doi_url
    params: Dict[str, Any] = {}
    if MAILTO:
        params["mailto"] = MAILTO
    data = _safe_get_json(url, params=params, timeout=22)
    if not data:
        return None
    return {"source": "openalex", "item": data or {}, "query_used": f"doi:{doi}"}


def _query_openalex(title: str, authors_list: List[str], year: str, raw_ref: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"
    q = _build_biblio_query(title, authors_list, year, raw_ref)
    y4 = _safe_str(year)[:4]

    params: Dict[str, Any] = {"search": q, "per-page": 7}
    if MAILTO:
        params["mailto"] = MAILTO
    if y4.isdigit():
        params["filter"] = f"publication_year:{y4}"

    data = _safe_get_json(url, params=params, timeout=22)
    if not data:
        return []
    results = data.get("results") or []
    return [{"source": "openalex", "item": it or {}, "query_used": q} for it in results]


def _candidate_fields(cand: Dict[str, Any]) -> Tuple[str, str, str, List[str], int]:
    """
    Returns (doi, title, year, author_surnames[], api_score_if_any)
    """
    src = _safe_strip((cand or {}).get("source"))
    item = (cand or {}).get("item") or {}

    doi = ""
    title = ""
    year = ""
    author_surnames: List[str] = []
    api_score = 0

    if src == "crossref":
        doi = _safe_strip(item.get("DOI"))
        titles = item.get("title") or []
        title = _safe_str(titles[0]).strip() if titles else ""

        try:
            api_score = int(item.get("score") or 0)
        except Exception:
            api_score = 0

        pp = (((item.get("published-print") or {}).get("date-parts")) or [[None]])
        po = (((item.get("published-online") or {}).get("date-parts")) or [[None]])
        year_val = (pp[0][0] if pp and pp[0] else None) or (po[0][0] if po and po[0] else None) or ""
        year = _safe_str(year_val).strip()

        authors = item.get("author") or []
        if isinstance(authors, list) and authors:
            for au in authors[:3]:
                fam = _safe_strip((au or {}).get("family")).lower()
                fam = re.sub(r"[^a-z\-']", "", fam)
                if fam:
                    author_surnames.append(fam)

    elif src == "openalex":
        doi_raw = item.get("doi")
        doi = _safe_str(doi_raw).replace("https://doi.org/", "").strip()
        title = _safe_strip(item.get("title"))
        year = _safe_strip(item.get("publication_year"))

        auths = item.get("authorships") or []
        if isinstance(auths, list) and auths:
            for a in auths[:3]:
                au = (a or {}).get("author") or {}
                nm = _safe_strip(au.get("display_name"))
                if nm:
                    last = nm.split()[-1].lower()
                    last = re.sub(r"[^a-z\-']", "", last)
                    if last:
                        author_surnames.append(last)

    return doi, title, year, author_surnames, api_score


def _score(
    ref_title: str,
    ref_authors: List[str],
    ref_year: str,
    cand_title: str,
    cand_authors: List[str],
    cand_year: str,
    crossref_api_score: int = 0,
) -> Dict[str, Any]:
    ref_title = _safe_strip(ref_title)
    ref_year = _safe_strip(ref_year)
    cand_title = _safe_strip(cand_title)
    cand_year = _safe_strip(cand_year)

    title_score = fuzz.token_set_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0

    ref_set = set([a for a in (ref_authors or []) if a])
    cand_set = set([a for a in (cand_authors or []) if a])
    overlap = len(ref_set.intersection(cand_set))

    # 0..3, heavier weight for overlap, but still title-led
    author_score = min(100, overlap * 45)

    year_match = 1 if (ref_year and cand_year and ref_year[:4] == cand_year[:4]) else 0

    score = (title_score * 1.25) + (author_score * 0.55) + (14 * year_match)

    if crossref_api_score and crossref_api_score > 0:
        score += min(18, int(crossref_api_score / 10))

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_overlap": int(overlap),
        "year_match": int(year_match),
    }


def _classify(score: int, author_overlap: int, year_match: int, title_score: int, score_gap_ok: bool, has_doi: bool) -> str:
    """
    Rules:
    - If DOI exists, never return not_found.
    - For DOI cases: verified when title is strong or overall score is high with at least some anchors.
    """
    if has_doi:
        if title_score >= 80 and (author_overlap >= 1 or year_match):
            return "verified"
        if score >= 98 and (author_overlap >= 1 or year_match or title_score >= 72):
            return "likely"
        return "needs_review"

    if title_score >= 84 and (author_overlap >= 1 or year_match) and score >= 100 and score_gap_ok:
        return "verified"
    if title_score >= 80 and score >= 95 and (author_overlap >= 1 or year_match):
        return "likely"
    if title_score >= 72 and (author_overlap >= 1 or year_match) and score >= 90:
        return "needs_review"
    return "not_found"


def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
    use_semantic_scholar: bool = False,  # ignored, kept for compatibility
) -> List[Dict[str, Any]]:
    refs = [r for r in (references or []) if _safe_strip(r)]
    if not refs:
        return []

    if max_to_check and max_to_check > 0:
        refs = refs[: max_to_check]

    rows: List[Dict[str, Any]] = []

    for ref in refs:
        ref_raw = _safe_strip(ref)

        ref_year = _extract_year(ref_raw)
        ref_authors = _extract_author_surnames(ref_raw, max_authors=3)
        ref_doi = _extract_doi(ref_raw)
        ref_title = _extract_title_guess(ref_raw)

        row: Dict[str, Any] = {
            "reference": ref_raw,
            "status": "offline",
            "source": "",
            "score": 0,
            "doi": "",
            "matched_year": "",
            "matched_authors": "",
            "matched_title": "",
            "query_used": "",
            "error": "",
        }

        try:
            candidates: List[Dict[str, Any]] = []
            crossref_candidates: List[Dict[str, Any]] = []

            # 1) DOI-first exact retrieval
            doi_exact_verified = False
            if ref_doi:
                if use_crossref:
                    hit = _query_crossref_by_doi(ref_doi)
                    if hit:
                        candidates.append(hit)
                    time.sleep(max(0.0, float(throttle_s or 0.0)))

                if use_openalex:
                    hit = _query_openalex_by_doi(ref_doi)
                    if hit:
                        candidates.append(hit)
                    time.sleep(max(0.0, float(throttle_s or 0.0)))

            # 2) Fallback to search if DOI lookup did not return
            if not candidates:
                if use_crossref:
                    crossref_candidates = _query_crossref_gold(ref_title, ref_authors, ref_year, ref_raw)
                    candidates.extend(crossref_candidates)
                    time.sleep(max(0.0, float(throttle_s or 0.0)))

                if use_openalex:
                    candidates.extend(_query_openalex(ref_title, ref_authors, ref_year, ref_raw))
                    time.sleep(max(0.0, float(throttle_s or 0.0)))

            if not candidates:
                row["status"] = "not_found"
                rows.append(row)
                continue

            best = None
            best_meta = None
            best_score = -1

            # score-gap check from Crossref top-2 when available
            score_gap_ok = True
            if crossref_candidates and len(crossref_candidates) >= 2:
                d0 = _candidate_fields(crossref_candidates[0])[4]
                d1 = _candidate_fields(crossref_candidates[1])[4]
                score_gap_ok = (d0 - d1) >= 10

            for cand in candidates:
                cand_doi, cand_title, cand_year, cand_authors, api_score = _candidate_fields(cand)

                # DOI exact match -> Verified
                if ref_doi and cand_doi and ref_doi.lower() == cand_doi.lower():
                    row["source"] = _safe_strip((cand or {}).get("source"))
                    row["score"] = 999
                    row["doi"] = _safe_strip(cand_doi)
                    row["matched_year"] = _safe_strip(cand_year)
                    row["matched_authors"] = ", ".join([a for a in cand_authors if a])
                    row["matched_title"] = _safe_strip(cand_title)
                    row["query_used"] = _safe_strip((cand or {}).get("query_used")) or f"doi:{ref_doi}"
                    row["status"] = "verified"
                    doi_exact_verified = True
                    break

                meta = _score(ref_title, ref_authors, ref_year, cand_title, cand_authors, cand_year, api_score)

                if meta["score"] > best_score:
                    best_score = meta["score"]
                    best = cand
                    best_meta = meta

            if doi_exact_verified:
                rows.append(row)
                continue

            src = _safe_strip((best or {}).get("source"))
            cand_doi, cand_title, cand_year, cand_authors, _api_score = _candidate_fields(best or {})

            row["source"] = src
            row["score"] = int((best_meta or {}).get("score") or 0)
            row["doi"] = _safe_strip(cand_doi)
            row["matched_year"] = _safe_strip(cand_year)
            row["matched_authors"] = ", ".join([a for a in cand_authors if a])
            row["matched_title"] = _safe_strip(cand_title)
            row["query_used"] = _safe_strip((best or {}).get("query_used")) or ref_title or ref_raw

            status = _classify(
                score=int(row["score"]),
                author_overlap=int((best_meta or {}).get("author_overlap") or 0),
                year_match=int((best_meta or {}).get("year_match") or 0),
                title_score=int((best_meta or {}).get("title_score") or 0),
                score_gap_ok=bool(score_gap_ok),
                has_doi=bool((row.get("doi") or "").strip()),
            )
            row["status"] = _normalize_verify_status(status)
            rows.append(row)

        except Exception as e:
            row["status"] = "offline"
            row["error"] = _safe_str(e)
            rows.append(row)

    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
