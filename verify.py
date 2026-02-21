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

# High-confidence override
VERIFY_SCORE_OVERRIDE = 120
VERIFY_TITLE_MIN = 70


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


def _extract_title_guess(ref: str) -> str:
    t = _strip_leading_numbering(ref)
    t = re.sub(r"\s+", " ", t).strip()

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

    return _norm_text(title)


def _clean_query_string(s: str) -> str:
    s = _safe_strip(s)
    s = re.sub(r"\s+", " ", s)
    if len(s) > 280:
        s = s[:280].rstrip()
    return s


def _doi_equal(a: str, b: str) -> bool:
    a = _safe_strip(a).lower()
    b = _safe_strip(b).lower()
    if not a or not b:
        return False
    return a == b


# -----------------------------
# Better author extraction
# -----------------------------
def _extract_author_surnames(ref: str, max_authors: int = 6) -> List[str]:
    """
    Extract up to max_authors surnames from the reference string.
    More robust than the earlier version:
    - splits on commas, 'and', '&', ';'
    - removes initials
    - avoids pulling title words by cutting at year
    """
    t = _strip_leading_numbering(ref)
    if not t:
        return []

    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", t, flags=re.I)
    head = t[: m.start()].strip() if m else t

    head = head.replace("&", " and ")
    head = re.sub(r"\bet\s+al\.?\b", "", head, flags=re.I)
    head = re.sub(r"\s+", " ", head).strip()

    # split on separators
    pieces = re.split(r";|\band\b", head, flags=re.I)
    tokens: List[str] = []
    for p in pieces:
        p = p.strip()
        if not p:
            continue
        tokens.extend([x.strip() for x in p.split(",") if x.strip()])

    surnames: List[str] = []
    comma_style = ("," in head)

    for tok in tokens:
        # remove dots/initials and extra symbols
        tok2 = re.sub(r"[^A-Za-z\-'\s]", " ", tok)
        tok2 = re.sub(r"\s+", " ", tok2).strip()
        if not tok2:
            continue
        parts = tok2.split()

        # skip pure initials chunks
        if all(len(x) <= 2 for x in parts):
            continue

        surname = parts[0] if comma_style else parts[-1]
        surname = re.sub(r"[^A-Za-z\-']", "", surname).lower().strip()
        if surname and len(surname) >= 2 and surname not in surnames:
            surnames.append(surname)

        if len(surnames) >= max_authors:
            break

    return surnames


def _authors_to_query(authors: List[str], max_join: int = 3) -> str:
    authors = [a for a in (authors or []) if _safe_strip(a)]
    if not authors:
        return ""
    return " ".join(authors[:max_join]).strip()


def _build_biblio_query(title: str, authors: List[str], year: str, raw_ref: str) -> str:
    base = title if len(_safe_str(title)) >= 12 else _safe_str(raw_ref)
    base = _clean_query_string(base)

    y4 = _safe_str(year)[:4] if _safe_str(year)[:4].isdigit() else ""
    a_str = _authors_to_query(authors, max_join=3)

    bits = [base]
    if a_str:
        bits.append(a_str)
    if y4:
        bits.append(y4)

    return _clean_query_string(" ".join(bits))


# -----------------------------
# Crossref
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


def _query_crossref_biblio(title: str, authors_list: List[str], year: str, raw_ref: str) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"
    q = _build_biblio_query(title, authors_list, year, raw_ref)

    y4 = _safe_str(year)[:4]
    a_str = _authors_to_query(authors_list, max_join=3)

    params: Dict[str, Any] = {
        "query.bibliographic": q,
        "rows": 5,
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
    filters.append("type:journal-article")
    params["filter"] = ",".join(filters)

    data = _safe_get_json(url, params=params, timeout=22)
    if not data:
        return []

    items = ((data.get("message") or {}).get("items") or [])
    return [{"source": "crossref", "item": it or {}, "query_used": q} for it in items]


# -----------------------------
# OpenAlex
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


def _query_openalex_biblio(title: str, authors_list: List[str], year: str, raw_ref: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"
    q = _build_biblio_query(title, authors_list, year, raw_ref)
    y4 = _safe_str(year)[:4]

    params: Dict[str, Any] = {"search": q, "per-page": 10}
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
    Returns (doi, title_norm, year, author_surnames[], api_score_if_any)
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
        title_raw = _safe_str(titles[0]).strip() if titles else ""
        title = _norm_text(title_raw)

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
            for au in authors[:8]:
                fam = _safe_strip((au or {}).get("family")).lower()
                fam = re.sub(r"[^a-z\-']", "", fam)
                if fam:
                    author_surnames.append(fam)

    elif src == "openalex":
        doi_raw = item.get("doi")
        doi = _safe_str(doi_raw).replace("https://doi.org/", "").strip()

        title_raw = _safe_strip(item.get("title"))
        title = _norm_text(title_raw)

        year = _safe_strip(item.get("publication_year"))

        auths = item.get("authorships") or []
        if isinstance(auths, list) and auths:
            for a in auths[:8]:
                au = (a or {}).get("author") or {}
                nm = _safe_strip(au.get("display_name"))
                if nm:
                    last = nm.split()[-1].lower()
                    last = re.sub(r"[^a-z\-']", "", last)
                    if last:
                        author_surnames.append(last)

    return doi, title, year, author_surnames, api_score


def _score(
    ref_title_norm: str,
    ref_authors: List[str],
    ref_year: str,
    cand_title_norm: str,
    cand_authors: List[str],
    cand_year: str,
    crossref_api_score: int = 0,
) -> Dict[str, Any]:
    ref_title_norm = _norm_text(ref_title_norm)
    cand_title_norm = _norm_text(cand_title_norm)

    title_score = fuzz.token_set_ratio(ref_title_norm, cand_title_norm) if (ref_title_norm and cand_title_norm) else 0

    ref_set = set([a for a in (ref_authors or []) if a])
    cand_set = set([a for a in (cand_authors or []) if a])
    overlap = len(ref_set.intersection(cand_set))

    author_score = min(100, overlap * 40)
    year_match = 1 if (ref_year and cand_year and ref_year[:4] == cand_year[:4]) else 0

    score = (title_score * 1.25) + (author_score * 0.70) + (18 * year_match)

    if crossref_api_score and crossref_api_score > 0:
        score += min(20, int(crossref_api_score / 10))

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_overlap": int(overlap),
        "year_match": int(year_match),
    }


def _classify(
    doi_match: bool,
    title_score: int,
    author_overlap: int,
    year_match: int,
    score: int,
) -> str:
    # 1) If DOI matches, you’ve essentially got the record.
    # Make it VERIFIED with modest title support and correct year.
    if doi_match and year_match == 1 and title_score >= 60:
        return "verified"

    # If DOI matches and year is missing or off, keep as LIKELY.
    if doi_match and title_score >= 60:
        return "likely"

    # 2) Strong bibliographic match without DOI
    if title_score >= 85 and author_overlap >= 1 and year_match == 1:
        return "likely"

    # 3) What used to be NEEDS_REVIEW becomes LIKELY
    # (moderate title + either author overlap or year match)
    if title_score >= 70 and (author_overlap >= 1 or year_match == 1):
        return "likely"

    # 4) Keep a small review band for borderline cases
    if title_score >= 60 and (author_overlap >= 1 or year_match == 1):
        return "needs_review"

    return "not_found"


def _diff_authors(ref_authors: List[str], cand_authors: List[str]) -> Tuple[str, str]:
    rset = set(ref_authors or [])
    cset = set(cand_authors or [])
    missing_ref = sorted(list(rset - cset))
    missing_cand = sorted(list(cset - rset))
    return ", ".join(missing_ref), ", ".join(missing_cand)


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
        ref_authors = _extract_author_surnames(ref_raw, max_authors=6)
        ref_doi = _extract_doi(ref_raw)
        ref_title = _extract_title_guess(ref_raw)

        row: Dict[str, Any] = {
            "reference": ref_raw,
            "status": "offline",
            "source": "",
            "score": 0,
            "doi_in_reference": ref_doi,
            "doi": "",
            "doi_match": False,
            "matched_year": "",
            "matched_authors": "",
            "matched_title": "",
            "title_score": 0,
            "author_overlap": 0,
            "year_match": 0,
            "missing_ref_authors": "",
            "missing_matched_authors": "",
            "query_used": "",
            "error": "",
        }

        try:
            candidates: List[Dict[str, Any]] = []

            # DOI lookup first
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

            # Biblio search
            if use_crossref:
                candidates.extend(_query_crossref_biblio(ref_title, ref_authors, ref_year, ref_raw))
                time.sleep(max(0.0, float(throttle_s or 0.0)))
            if use_openalex:
                candidates.extend(_query_openalex_biblio(ref_title, ref_authors, ref_year, ref_raw))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            if not candidates:
                row["status"] = "not_found"
                rows.append(row)
                continue

            best = None
            best_meta = None
            best_score = -1
            best_doi_match = False
            best_query = ""

            for cand in candidates:
                cand_doi, cand_title, cand_year, cand_authors, api_score = _candidate_fields(cand)
                meta = _score(ref_title, ref_authors, ref_year, cand_title, cand_authors, cand_year, api_score)

                doi_match = _doi_equal(ref_doi, cand_doi) if ref_doi else False

                # Prefer DOI matches, but still title-led
                score = int(meta["score"] + (25 if doi_match else 0))

                if score > best_score:
                    best_score = score
                    best = cand
                    best_meta = meta
                    best_doi_match = doi_match
                    best_query = _safe_strip((cand or {}).get("query_used"))

            src = _safe_strip((best or {}).get("source"))
            cand_doi, cand_title, cand_year, cand_authors, _api_score = _candidate_fields(best or {})

            row["source"] = src
            row["score"] = int(best_score if best_score >= 0 else 0)
            row["doi"] = _safe_strip(cand_doi)
            row["doi_match"] = bool(best_doi_match)
            row["matched_year"] = _safe_strip(cand_year)
            row["matched_authors"] = ", ".join([a for a in cand_authors if a])
            row["matched_title"] = _safe_strip(cand_title)
            row["title_score"] = int((best_meta or {}).get("title_score") or 0)
            row["author_overlap"] = int((best_meta or {}).get("author_overlap") or 0)
            row["year_match"] = int((best_meta or {}).get("year_match") or 0)
            row["query_used"] = best_query or ref_raw

            miss_ref, miss_cand = _diff_authors(ref_authors, cand_authors)
            row["missing_ref_authors"] = miss_ref
            row["missing_matched_authors"] = miss_cand

            status = _classify(
                doi_match=bool(row["doi_match"]),
                title_score=int(row["title_score"]),
                author_overlap=int(row["author_overlap"]),
                year_match=int(row["year_match"]),
                score=int(row["score"]),
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

