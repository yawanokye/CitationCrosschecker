# verify.py
import re
import time
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


def _safe_get_json(session: requests.Session, url: str, params: Optional[dict] = None, timeout: int = 12) -> Optional[dict]:
    try:
        r = session.get(url, params=params, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _extract_year(text: str) -> str:
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", text or "", flags=re.I)
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""


def _extract_doi(text: str) -> str:
    t = text or ""
    # doi:10.xxxx/yyy OR https://doi.org/10.xxxx/yyy
    m = re.search(r"(?:doi\s*:\s*)?(10\.\d{4,9}/[^\s\)]+)", t, flags=re.I)
    if m:
        return m.group(1).strip().rstrip(".").rstrip(",")
    m2 = re.search(r"https?://doi\.org/(10\.\d{4,9}/[^\s\)]+)", t, flags=re.I)
    if m2:
        return m2.group(1).strip().rstrip(".").rstrip(",")
    return ""


def _split_authors_guess(ref_raw: str) -> List[str]:
    """
    Best-effort extraction of author surnames from APA-like reference.
    Example: "Ochieng, E. G., Price, A. D. F., ... & Moore, D. (2013)."
    Return surnames lowercased.
    """
    t = (ref_raw or "").strip()
    if not t:
        return []
    # Take everything before (YEAR)
    m = re.search(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)", t, flags=re.I)
    head = t[:m.start()].strip() if m else t[:180].strip()

    # Replace & with and for splitting
    head = head.replace("&", " and ")
    # Split by commas and 'and'
    parts = [p.strip() for p in re.split(r"\s+and\s+|,", head) if p.strip()]
    surnames = []
    for p in parts:
        # surname may appear as "Afful Jr" before comma, keep last token unless it's an initial
        p2 = re.sub(r"[^A-Za-z\-\s']", " ", p)
        p2 = re.sub(r"\s+", " ", p2).strip()
        if not p2:
            continue
        toks = p2.split()
        # Remove single-letter initials
        toks = [x for x in toks if len(x) > 1]
        if not toks:
            continue
        sur = toks[0]  # in APA author segment, surname usually appears first in that chunk
        surnames.append(sur.lower())
    # De-duplicate keep order
    out = []
    seen = set()
    for s in surnames:
        if s not in seen:
            out.append(s)
            seen.add(s)
    return out[:8]


def _extract_title_guess(ref_raw: str) -> str:
    """
    Extract likely title from APA reference:
    After "(YEAR)." until next period.
    """
    t = re.sub(r"\s+", " ", (ref_raw or "").strip())
    if not t:
        return ""

    # remove leading numbering like [12] or 12. or 12)
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)

    # find year close
    m = re.search(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.\s*", t, flags=re.I)
    if not m:
        return ""

    after = t[m.end():].strip()
    # title until next period
    # If the title itself has abbreviations, this is imperfect but works better than raw ref text.
    parts = after.split(". ")
    title = parts[0].strip().strip(".")
    # avoid super short
    if len(title) < 6:
        return ""
    return title


def _author_overlap_score(ref_authors: List[str], cand_authors: List[str]) -> float:
    if not ref_authors or not cand_authors:
        return 0.0
    r = set(a.lower() for a in ref_authors if a)
    c = set(a.lower() for a in cand_authors if a)
    if not r or not c:
        return 0.0
    inter = len(r.intersection(c))
    return inter / max(1, min(len(r), len(c)))


def _score_match(ref_title: str, ref_authors: List[str], ref_year: str, cand_title: str, cand_authors: List[str], cand_year: str) -> Tuple[int, Dict[str, Any]]:
    # Title scores
    ts1 = fuzz.token_set_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0
    ts2 = fuzz.partial_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0
    title_score = max(ts1, ts2)

    # Author overlap
    a_overlap = _author_overlap_score(ref_authors, cand_authors)  # 0..1
    author_score = int(round(a_overlap * 100))

    # Year match
    year_match = 0
    if ref_year and cand_year and ref_year[:4] == str(cand_year)[:4]:
        year_match = 1

    # Weighted overall score 0..100
    overall = 0.65 * title_score + 0.25 * author_score + 0.10 * (100 if year_match else 0)
    overall_i = int(round(overall))

    meta = {
        "title_score": int(title_score),
        "author_overlap": float(round(a_overlap, 3)),
        "year_match": int(year_match),
        "overall": overall_i,
    }
    return overall_i, meta


def _classify(overall: int, author_overlap: float, year_match: int) -> str:
    # More realistic thresholds
    if overall >= 85 and author_overlap >= 0.50 and year_match == 1:
        return "verified"
    if overall >= 78 and (author_overlap >= 0.30 or year_match == 1):
        return "likely"
    if overall >= 68:
        return "needs_review"
    return "not_found"


def _query_crossref(session: requests.Session, title: str, author: str, year: str, raw: str) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"
    params = {"rows": 6}

    if title:
        params["query.title"] = title
    if author:
        params["query.author"] = author
    if not title and not author:
        params["query.bibliographic"] = raw

    # Narrow by year window when available
    if year and year[:4].isdigit():
        y = int(year[:4])
        params["filter"] = f"from-pub-date:{y-1}-01-01,until-pub-date:{y+1}-12-31"

    data = _safe_get_json(session, url, params=params, timeout=12)
    if not data:
        return []
    items = (data.get("message") or {}).get("items") or []
    out = []
    for it in items:
        out.append({"source": "crossref", "item": it})
    return out


def _query_openalex(session: requests.Session, title: str, author: str, year: str, raw: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"
    params = {"per-page": 6}

    # OpenAlex search works best with title text
    q = title or raw
    params["search"] = q

    filters = []
    if year and year[:4].isdigit():
        filters.append(f"publication_year:{int(year[:4])}")
    # Can't reliably filter by author name in the free search endpoint, but we include author in query string
    if author:
        params["search"] = f"{q} {author}"

    if filters:
        params["filter"] = ",".join(filters)

    data = _safe_get_json(session, url, params=params, timeout=12)
    if not data:
        return []
    results = data.get("results") or []
    out = []
    for it in results:
        out.append({"source": "openalex", "item": it})
    return out


def _query_semanticscholar(session: requests.Session, title: str, author: str, year: str, raw: str) -> List[Dict[str, Any]]:
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "limit": 6,
        "fields": "title,year,authors,externalIds,doi"
    }
    q = title or raw
    if author:
        q = f"{q} {author}"
    if year and year[:4].isdigit():
        q = f"{q} {year[:4]}"
    params["query"] = q

    data = _safe_get_json(session, url, params=params, timeout=12)
    if not data:
        return []
    results = data.get("data") or []
    out = []
    for it in results:
        out.append({"source": "semanticscholar", "item": it})
    return out


def _cand_fields(cand: Dict[str, Any]) -> Tuple[str, str, List[str], str]:
    """
    Return: (title, year, authors_surnames, doi)
    """
    src = cand.get("source")
    item = cand.get("item") or {}

    if src == "crossref":
        titles = item.get("title") or []
        title = (titles[0] if titles else "") or ""
        year = ""
        dp = (item.get("published-print", {}).get("date-parts") or [[None]])[0][0]
        do = (item.get("published-online", {}).get("date-parts") or [[None]])[0][0]
        year = str(dp or do or "")
        authors = item.get("author") or []
        auth = []
        for a in authors[:10]:
            fam = (a.get("family") or "").strip()
            if fam:
                auth.append(fam.lower())
        doi = (item.get("DOI") or "").strip()
        return title, year, auth, doi

    if src == "openalex":
        title = (item.get("title") or "") or ""
        year = str(item.get("publication_year") or "")
        auth = []
        for a in (item.get("authorships") or [])[:10]:
            au = (a.get("author") or {}).get("display_name") or ""
            if au:
                auth.append(au.split()[-1].lower())
        doi = (item.get("doi") or "").replace("https://doi.org/", "").strip()
        return title, year, auth, doi

    if src == "semanticscholar":
        title = (item.get("title") or "") or ""
        year = str(item.get("year") or "")
        auth = []
        for a in (item.get("authors") or [])[:10]:
            nm = (a.get("name") or "").strip()
            if nm:
                auth.append(nm.split()[-1].lower())
        doi = (item.get("doi") or "").strip()
        if not doi:
            ext = item.get("externalIds") or {}
            doi = (ext.get("DOI") or "").strip()
        return title, year, auth, doi

    return "", "", [], ""


def _verify_one(session: requests.Session, ref: str, throttle_s: float, use_crossref: bool, use_openalex: bool, use_semanticscholar: bool) -> Dict[str, Any]:
    ref = (ref or "").strip()
    row: Dict[str, Any] = {
        "reference": ref,
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

    if not ref:
        row["status"] = "not_found"
        return row

    try:
        ref_year = _extract_year(ref)
        ref_doi = _extract_doi(ref)
        ref_title = _extract_title_guess(ref)
        ref_authors = _split_authors_guess(ref)
        first_author = (ref_authors[0] if ref_authors else "")

        # Build query_used (your earlier CSV likely had title+author+year which works better than raw full reference)
        query_used = ""
        if ref_title and first_author and ref_year:
            query_used = f"{ref_title} {first_author} {ref_year[:4]}"
        elif ref_title and first_author:
            query_used = f"{ref_title} {first_author}"
        elif ref_title:
            query_used = ref_title
        else:
            query_used = ref[:240]
        row["query_used"] = query_used

        candidates: List[Dict[str, Any]] = []

        # If DOI exists, quick-verify by querying sources indirectly (still do search in case DOI is malformed)
        # We keep it simple and still use search endpoints (reliable on Render).
        if use_crossref:
            candidates.extend(_query_crossref(session, ref_title, first_author, ref_year, query_used))
            time.sleep(max(0.0, float(throttle_s or 0.0)))
        if use_openalex:
            candidates.extend(_query_openalex(session, ref_title, first_author, ref_year, query_used))
            time.sleep(max(0.0, float(throttle_s or 0.0)))
        if use_semanticscholar:
            candidates.extend(_query_semanticscholar(session, ref_title, first_author, ref_year, query_used))
            time.sleep(max(0.0, float(throttle_s or 0.0)))

        if not candidates:
            row["status"] = "not_found"
            return row

        best = None
        best_overall = -1
        best_meta = None
        best_fields = None

        for cand in candidates:
            c_title, c_year, c_auth, c_doi = _cand_fields(cand)
            overall, meta = _score_match(ref_title, ref_authors, ref_year, c_title, c_auth, c_year)
            if overall > best_overall:
                best_overall = overall
                best = cand
                best_meta = meta
                best_fields = (c_title, c_year, c_auth, c_doi)

        if not best or not best_fields:
            row["status"] = "not_found"
            return row

        c_title, c_year, c_auth, c_doi = best_fields
        row["source"] = best.get("source") or ""
        row["score"] = int(best_meta.get("overall", 0) if best_meta else 0)
        row["doi"] = (c_doi or ref_doi or "").strip()
        row["matched_year"] = str(c_year or "")
        row["matched_title"] = c_title or ""
        row["matched_authors"] = ", ".join(c_auth[:6]) if c_auth else ""

        status = _classify(
            overall=int(row["score"]),
            author_overlap=float(best_meta.get("author_overlap", 0.0) if best_meta else 0.0),
            year_match=int(best_meta.get("year_match", 0) if best_meta else 0),
        )
        row["status"] = _normalize_verify_status(status)
        return row

    except Exception as e:
        row["status"] = "offline"
        row["error"] = str(e)
        return row


def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
    use_semanticscholar: bool = True,
) -> List[Dict[str, Any]]:
    refs = [r for r in (references or []) if (r or "").strip()]
    if not refs:
        return []

    if max_to_check and max_to_check > 0:
        refs = refs[: max_to_check]

    headers = {"User-Agent": "CitationCrosschecker/1.0 (UCC)"}
    session = requests.Session()
    session.headers.update(headers)

    # Limited concurrency for Render stability
    max_workers = 4

    rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [
            ex.submit(_verify_one, session, ref, throttle_s, use_crossref, use_openalex, use_semanticscholar)
            for ref in refs
        ]
        for f in as_completed(futs):
            rows.append(f.result())

    # Preserve input order as much as possible (stable sort by original index)
    idx_map = {ref: i for i, ref in enumerate(refs)}
    rows.sort(key=lambda r: idx_map.get(r.get("reference", ""), 10**9))

    # Final enforcement
    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
