# verify.py
import re
import time
from typing import List, Dict, Any, Optional, Tuple

import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}


# -----------------------------
# Basic helpers
# -----------------------------
def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


def _normalize_doi(doi: str) -> str:
    d = (doi or "").strip()
    if not d:
        return ""
    d = d.replace("https://doi.org/", "").replace("http://doi.org/", "")
    d = d.replace("https://dx.doi.org/", "").replace("http://dx.doi.org/", "")
    return d.strip().lower()


def _clean_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _safe_get_json(
    session: requests.Session,
    url: str,
    params: Optional[dict] = None,
    timeout: int = 12,
    retries: int = 2,
    backoff_s: float = 0.6,
) -> Optional[dict]:
    headers = {"User-Agent": "CitationCrosschecker/1.0"}
    for i in range(max(0, int(retries)) + 1):
        try:
            r = session.get(url, params=params, timeout=timeout, headers=headers)
            if r.status_code == 200:
                return r.json()

            if r.status_code in (429, 500, 502, 503, 504):
                sleep_for = backoff_s * (2 ** i)
                ra = r.headers.get("Retry-After")
                if ra and ra.isdigit():
                    sleep_for = max(sleep_for, float(ra))
                time.sleep(min(8.0, sleep_for))
                continue

            return None
        except Exception:
            time.sleep(min(8.0, backoff_s * (2 ** i)))
    return None


# -----------------------------
# Reference parsing (lightweight but effective)
# -----------------------------
_YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", re.I)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s<>\"']+\b", re.I)


def _extract_year(text: str) -> str:
    m = _YEAR_RE.search(text or "")
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""


def _extract_doi(text: str) -> str:
    m = _DOI_RE.search(text or "")
    return _normalize_doi(m.group(0)) if m else ""


def _strip_leading_numbering(ref: str) -> str:
    r = ref or ""
    r = re.sub(r"^\s*\[\s*\d+\s*\]\s*", "", r)  # [12]
    r = re.sub(r"^\s*\d+\s*[\.\)]\s*", "", r)  # 12. or 12)
    return r.strip()


def _split_authors_segment(seg: str) -> List[str]:
    seg = (seg or "").replace("&", " and ")
    seg = re.sub(r"\bet\s+al\.?\b", "", seg, flags=re.I)
    parts = re.split(r"\s+and\s+|,", seg)
    names = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # take surname-ish token
        tok = re.sub(r"[^A-Za-z\-']", " ", p).strip()
        if not tok:
            continue
        # surname usually last word in token
        surn = tok.split()[-1].lower()
        if len(surn) >= 2:
            names.append(surn)
    # de-dup preserving order
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            out.append(n)
            seen.add(n)
    return out[:8]


def _extract_authors_guess(ref: str) -> List[str]:
    """
    Best effort:
    - APA-like: "Surname, I., Surname, I., & Surname, I. (Year)..."
    - Numeric: "Surname AB, Surname CD. Title. Year..."
    We take up to the first 3 surnames as strong signal.
    """
    r = _strip_leading_numbering(ref)
    if not r:
        return []
    # Stop at year parentheses if present
    cut = r
    m = re.search(r"\(\s*(1[6-9]\d{2}|20\d{2})", r)
    if m:
        cut = r[:m.start()].strip()

    # Stop at first period if it looks like end of author list
    # Many styles separate authors and title with a period
    if "." in cut:
        left = cut.split(".", 1)[0]
        if len(left) > 5:
            cut = left

    return _split_authors_segment(cut)[:3]


def _extract_title_guess(ref: str) -> str:
    """
    For verification, title similarity is dominant.
    We remove leading numbering, authors, year parentheses, and then take a title-ish chunk.
    """
    r = _strip_leading_numbering(ref)
    r = re.sub(r"\s+", " ", r).strip()

    # remove DOI/URL tail
    r = re.sub(r"https?://\S+", "", r, flags=re.I).strip()
    r = re.sub(_DOI_RE, "", r).strip()

    # remove year in parentheses
    r = re.sub(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)", "", r, flags=re.I).strip()

    # common pattern: authors. title. journal...
    # take between first period and second period, if plausible
    parts = [p.strip() for p in r.split(".") if p.strip()]
    if len(parts) >= 2:
        # parts[0] often authors, parts[1] often title
        title = parts[1]
        if len(title) >= 10:
            return title

    # fallback: remove first author segment heuristically
    # if comma exists, remove up to last comma + maybe initials
    if "," in r and len(r.split(",", 1)[0]) < 40:
        # remove first chunk before first period if any
        if "." in r:
            return r.split(".", 1)[1].strip()
    return r[:220].strip()


def _reference_features(ref: str) -> Dict[str, Any]:
    ref_clean = _clean_text(ref)
    return {
        "ref": ref_clean,
        "doi": _extract_doi(ref_clean),
        "year": _extract_year(ref_clean),
        "authors": _extract_authors_guess(ref_clean),
        "title": _extract_title_guess(ref_clean),
    }


# -----------------------------
# Query building (multi-strategy)
# -----------------------------
def _build_queries(feat: Dict[str, Any]) -> List[str]:
    """
    Multiple strategies, fastest and most precise first.
    """
    ref = feat.get("ref", "")
    doi = feat.get("doi", "")
    year = feat.get("year", "")
    authors = feat.get("authors", []) or []
    title = feat.get("title", "")

    qs = []

    # If DOI exists, DOI wins (some refs include DOI)
    if doi:
        qs.append(f"DOI:{doi}")

    # Strong structured queries
    if title and authors:
        qs.append(f"{' '.join(authors[:2])} {year[:4]} {title}")
        qs.append(f"{' '.join(authors[:2])} {title}")

    if title:
        # title-first query reduces noise a lot
        if year:
            qs.append(f"{title} {year[:4]}")
        qs.append(title)

    # fallback: raw ref
    qs.append(ref)

    # de-dup, preserve order
    seen = set()
    out = []
    for q in qs:
        q = _clean_text(q)
        if q and q.lower() not in seen:
            out.append(q)
            seen.add(q.lower())

    return out[:6]


# -----------------------------
# API queries
# -----------------------------
def _query_crossref(session: requests.Session, query: str) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"
    params = {"query.bibliographic": query, "rows": 8}
    data = _safe_get_json(session, url, params=params, timeout=12, retries=2)
    if not data:
        return []
    items = (data.get("message") or {}).get("items") or []
    return [{"source": "crossref", "item": it} for it in items]


def _query_openalex(session: requests.Session, query: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"
    params = {"search": query, "per_page": 8}
    data = _safe_get_json(session, url, params=params, timeout=12, retries=2)
    if not data:
        return []
    results = data.get("results") or []
    return [{"source": "openalex", "item": it} for it in results]


# -----------------------------
# Candidate extraction and scoring
# -----------------------------
def _candidate_fields(cand: Dict[str, Any]) -> Dict[str, str]:
    src = cand.get("source")
    item = cand.get("item", {}) or {}

    if src == "crossref":
        doi = _normalize_doi(item.get("DOI") or "")
        titles = item.get("title") or []
        title = (titles[0] if titles else "") or ""
        year = ""
        year = str(
            (item.get("published-print", {}).get("date-parts") or [[None]])[0][0]
            or (item.get("published-online", {}).get("date-parts") or [[None]])[0][0]
            or ""
        )
        authors = item.get("author") or []
        author_surnames = [((a.get("family") or "")).strip().lower() for a in authors if (a.get("family") or "").strip()]
        return {
            "doi": doi,
            "title": title,
            "year": year,
            "authors": ", ".join(author_surnames[:8]),
            "first_author": (author_surnames[0] if author_surnames else ""),
        }

    if src == "openalex":
        doi = _normalize_doi((item.get("doi") or "").replace("https://doi.org/", ""))
        title = (item.get("title") or "") or ""
        year = str(item.get("publication_year") or "")
        auths = item.get("authorships") or []
        author_surnames = []
        for a in auths:
            nm = ((a.get("author") or {}).get("display_name") or "").strip()
            if nm:
                author_surnames.append(nm.split()[-1].lower())
        return {
            "doi": doi,
            "title": title,
            "year": year,
            "authors": ", ".join(author_surnames[:8]),
            "first_author": (author_surnames[0] if author_surnames else ""),
        }

    return {"doi": "", "title": "", "year": "", "authors": "", "first_author": ""}


def _author_overlap_score(ref_authors: List[str], cand_authors_csv: str) -> int:
    if not ref_authors:
        return 0
    cand_authors = [a.strip().lower() for a in (cand_authors_csv or "").split(",") if a.strip()]
    if not cand_authors:
        return 0
    hit = 0
    for ra in ref_authors[:3]:
        if ra and ra in cand_authors:
            hit += 1
    # scale to 0..30
    return min(30, hit * 15)


def _score(ref_feat: Dict[str, Any], cand: Dict[str, Any]) -> Dict[str, Any]:
    cf = _candidate_fields(cand)

    ref_title = ref_feat.get("title", "")
    ref_year = ref_feat.get("year", "")
    ref_authors = ref_feat.get("authors", []) or []
    ref_doi = ref_feat.get("doi", "")

    title_score = int(fuzz.token_set_ratio(ref_title, cf["title"])) if (ref_title and cf["title"]) else 0

    year_match = 0
    if ref_year and cf["year"] and ref_year[:4] == str(cf["year"])[:4]:
        year_match = 10

    author_score = _author_overlap_score(ref_authors, cf["authors"])

    doi_bonus = 0
    if ref_doi and cf["doi"] and ref_doi == cf["doi"]:
        doi_bonus = 60

    # Total score
    total = title_score + author_score + year_match + doi_bonus

    return {
        "score": int(total),
        "title_score": int(title_score),
        "author_score": int(author_score),
        "year_bonus": int(year_match),
        "doi_bonus": int(doi_bonus),
        "doi": cf["doi"],
        "matched_title": cf["title"],
        "matched_year": cf["year"],
        "matched_first_author": cf["first_author"],
        "matched_authors": cf["authors"],
    }


def _classify(score: int, doi_bonus: int, title_score: int, author_score: int, year_bonus: int) -> str:
    # DOI exact match is verified
    if doi_bonus >= 60:
        return "verified"

    # Strong combined evidence
    if score >= 135 and title_score >= 80 and (author_score >= 15 or year_bonus >= 10):
        return "verified"

    if score >= 115 and title_score >= 70 and (author_score >= 15 or year_bonus >= 10):
        return "likely"

    if score >= 95 and title_score >= 60:
        return "needs_review"

    return "not_found"


# -----------------------------
# Main function
# -----------------------------
def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> List[Dict[str, Any]]:
    """
    Returns rows with status strictly in:
      verified, likely, needs_review, not_found, offline
    """
    refs = [r for r in (references or []) if (r or "").strip()]
    if not refs:
        return []

    if max_to_check and max_to_check > 0:
        refs = refs[: max_to_check]

    rows: List[Dict[str, Any]] = []
    session = requests.Session()

    for ref in refs:
        feat = _reference_features(ref)
        queries = _build_queries(feat)

        row: Dict[str, Any] = {
            "reference": feat["ref"],
            "status": "offline",
            "source": "",
            "score": 0,
            "doi": "",
            "matched_year": "",
            "matched_first_author": "",
            "matched_authors": "",
            "matched_title": "",
            "query_used": "",
            "error": "",
        }

        try:
            all_candidates: List[Dict[str, Any]] = []
            used_query = ""

            # Try queries from strongest to weakest, break early if very good match appears
            best_meta = None
            best_cand = None
            best_src = ""

            for q in queries:
                used_query = q

                candidates: List[Dict[str, Any]] = []
                if use_crossref:
                    candidates.extend(_query_crossref(session, q))
                    if throttle_s:
                        time.sleep(max(0.0, float(throttle_s)))

                if use_openalex:
                    candidates.extend(_query_openalex(session, q))
                    if throttle_s:
                        time.sleep(max(0.0, float(throttle_s)))

                if not candidates:
                    continue

                for cand in candidates:
                    meta = _score(feat, cand)
                    if (best_meta is None) or (meta["score"] > best_meta["score"]):
                        best_meta = meta
                        best_cand = cand
                        best_src = cand.get("source", "")

                # early exit if verified already
                if best_meta and best_meta.get("doi_bonus", 0) >= 60:
                    break
                if best_meta and best_meta["score"] >= 150 and best_meta["title_score"] >= 85:
                    break

            if not best_meta:
                row["status"] = "not_found"
                row["query_used"] = used_query or (queries[0] if queries else "")
                rows.append(row)
                continue

            row["source"] = best_src
            row["score"] = int(best_meta["score"])
            row["doi"] = best_meta.get("doi", "")
            row["matched_year"] = str(best_meta.get("matched_year", "") or "")
            row["matched_first_author"] = best_meta.get("matched_first_author", "")
            row["matched_authors"] = best_meta.get("matched_authors", "")
            row["matched_title"] = best_meta.get("matched_title", "")
            row["query_used"] = used_query

            status = _classify(
                score=int(best_meta["score"]),
                doi_bonus=int(best_meta.get("doi_bonus", 0)),
                title_score=int(best_meta.get("title_score", 0)),
                author_score=int(best_meta.get("author_score", 0)),
                year_bonus=int(best_meta.get("year_bonus", 0)),
            )
            row["status"] = _normalize_verify_status(status)
            rows.append(row)

        except Exception as e:
            row["status"] = "offline"
            row["error"] = str(e)
            rows.append(row)

    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
