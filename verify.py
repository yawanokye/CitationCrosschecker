# verify.py
import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple

import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}

# Accept either env var name
MAILTO = (
    os.getenv("CITATION_CROSSCHECKER_MAILTO")
    or os.getenv("CROSSREF_MAILTO")
    or ""
)
MAILTO = (MAILTO or "").strip()


# -----------------------------
# Small safety helpers
# -----------------------------
def _s(x: Any) -> str:
    """Safe string"""
    return "" if x is None else str(x)


def _normalize_verify_status(s: str) -> str:
    st = _s(s).strip().lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


def _safe_get_json(session: requests.Session, url: str, params: Optional[dict] = None, timeout: int = 22) -> Optional[dict]:
    try:
        headers = {
            "User-Agent": "CitationCrosschecker/1.0",
            "Accept": "application/json",
        }
        r = session.get(url, params=params, timeout=timeout, headers=headers)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _extract_year(text: str) -> str:
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", _s(text), flags=re.I)
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""


def _strip_leading_numbering(text: str) -> str:
    t = _s(text).strip()
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)
    return t.strip()


def _extract_first_author_surname(text: str) -> str:
    t = _strip_leading_numbering(text)
    if not t:
        return ""
    if "," in t:
        first = t.split(",", 1)[0].strip()
        return re.sub(r"[^A-Za-z\-']", "", first).lower()
    first = re.split(r"\s+", t)[0].strip()
    return re.sub(r"[^A-Za-z\-']", "", first).lower()


def _extract_doi(text: str) -> str:
    t = _s(text)
    m = re.search(r"(10\.\d{4,9}/[^\s]+)", t, flags=re.I)
    if not m:
        return ""
    return m.group(1).strip().rstrip(").,;]")


def _clean_query_string(s: str) -> str:
    s = _s(s).strip()
    s = re.sub(r"\s+", " ", s)
    # trim long strings so APIs don't choke
    if len(s) > 260:
        s = s[:260].rstrip()
    return s


def _extract_title_guess(ref: str) -> str:
    """
    Title guess from a reference string:
    - remove numbering
    - remove DOI
    - take the first sentence after (YEAR)
    """
    t = _strip_leading_numbering(ref)
    t = re.sub(r"\s+", " ", t).strip()

    # remove DOI forms
    t = re.sub(r"(doi\s*:\s*)?10\.\d{4,9}/\S+", "", t, flags=re.I)
    t = re.sub(r"https?://doi\.org/10\.\d{4,9}/\S+", "", t, flags=re.I)

    parts = re.split(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?", t, maxsplit=1, flags=re.I)
    if len(parts) >= 3:
        after = parts[2].strip()
    else:
        m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", t, flags=re.I)
        after = t[m.end():].strip() if m else t

    after = after.lstrip(". ").strip()
    title = after.split(".", 1)[0].strip() if "." in after else after.strip()
    if len(title) < 12:
        title = t.strip()
    return title


def _extract_container_guess(ref: str, title_guess: str) -> str:
    """
    Tries to guess journal/book container title from reference:
    After the title sentence, the next chunk often contains the journal or book.
    """
    ref = re.sub(r"\s+", " ", _strip_leading_numbering(ref)).strip()
    if not ref:
        return ""

    # remove DOI
    ref = re.sub(r"(doi\s*:\s*)?10\.\d{4,9}/\S+", "", ref, flags=re.I)
    ref = re.sub(r"https?://doi\.org/10\.\d{4,9}/\S+", "", ref, flags=re.I)

    # try to locate title and take the next chunk
    tg = _s(title_guess).strip()
    if tg and tg in ref:
        after = ref.split(tg, 1)[1].strip()
    else:
        # fallback: after year block
        parts = re.split(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?", ref, maxsplit=1, flags=re.I)
        after = parts[2].strip() if len(parts) >= 3 else ref

    after = after.lstrip(". ").strip()
    if not after:
        return ""

    # container typically ends at first comma before volume/issue/pages
    # Example: "Journal Name, 26(5), 90–97."
    cand = after.split(",", 1)[0].strip() if "," in after else after.strip()
    # remove leading junk like "In" or "Retrieved from"
    cand = re.sub(r"^(in|retrieved\s+from|available\s+at)\s+", "", cand, flags=re.I).strip()

    # short containers aren't useful
    if len(cand) < 6:
        return ""
    # avoid returning pure years/volumes
    if re.fullmatch(r"(1[6-9]\d{2}|20\d{2}).*", cand):
        return ""
    return cand


# -----------------------------
# Query builders
# -----------------------------
def _crossref_filters(year: str) -> Optional[str]:
    y4 = _s(year)[:4]
    if y4.isdigit():
        return f"from-pub-date:{y4}-01-01,until-pub-date:{y4}-12-31"
    return None


def _query_crossref_attempts(
    session: requests.Session,
    title: str,
    author: str,
    year: str,
    container: str,
    raw_ref: str,
) -> List[Dict[str, Any]]:
    """
    Multiple Crossref attempts, from most precise to most forgiving.
    Uses query.bibliographic (citation-like), plus field queries when available.
    """
    url = "https://api.crossref.org/works"

    title_q = _clean_query_string(title)
    container_q = _clean_query_string(container)
    raw_q = _clean_query_string(raw_ref)
    author_q = _clean_query_string(author)

    flt = _crossref_filters(year)
    out: List[Dict[str, Any]] = []

    def run(params: Dict[str, Any], query_used: str):
        if MAILTO:
            params["mailto"] = MAILTO
        if flt:
            params["filter"] = flt
        data = _safe_get_json(session, url, params=params, timeout=22)
        if not data:
            return
        items = (data.get("message") or {}).get("items") or []
        for it in items:
            out.append({"source": "crossref", "item": it, "query_used": query_used})

    # A) Fielded query (best when title is clean)
    # Crossref supports query.title/query.author/query.container-title
    if len(title_q) >= 12:
        params = {"rows": 5, "query.title": title_q}
        if author_q:
            params["query.author"] = author_q
        if container_q:
            params["query.container-title"] = container_q
        run(params, query_used=f"title:{title_q} | author:{author_q} | container:{container_q}")

    # B) Citation-like “golden” query.bibliographic + author
    # Keep the bibliographic string tight: title + container (better than full raw reference)
    bib = title_q if len(title_q) >= 12 else raw_q
    if container_q and len(bib) < 220:
        bib = _clean_query_string(f"{bib} {container_q}")
    params = {"rows": 5, "query.bibliographic": bib}
    if author_q:
        params["query.author"] = author_q
    run(params, query_used=bib)

    # C) Fallback: raw reference string (noisy, but sometimes helps recall)
    params = {"rows": 5, "query.bibliographic": raw_q}
    if author_q:
        params["query.author"] = author_q
    run(params, query_used=raw_q)

    return out


def _query_openalex(
    session: requests.Session,
    title: str,
    author: str,
    year: str,
    container: str,
    raw_ref: str,
) -> List[Dict[str, Any]]:
    """
    OpenAlex search. We keep it simple and stable:
      - search = title (+container if helpful)
      - filter publication_year when we have it
      - include mailto if provided
    """
    url = "https://api.openalex.org/works"

    title_q = _clean_query_string(title)
    container_q = _clean_query_string(container)
    raw_q = _clean_query_string(raw_ref)

    q = title_q if len(title_q) >= 12 else raw_q
    if container_q and len(q) < 220:
        q = _clean_query_string(f"{q} {container_q}")

    params: Dict[str, Any] = {"search": q, "per-page": 10}

    if MAILTO:
        params["mailto"] = MAILTO

    y4 = _s(year)[:4]
    if y4.isdigit():
        params["filter"] = f"publication_year:{y4}"

    data = _safe_get_json(session, url, params=params, timeout=22)
    if not data:
        return []
    results = data.get("results") or []
    return [{"source": "openalex", "item": it, "query_used": q} for it in results]


# -----------------------------
# Candidate parsing + scoring
# -----------------------------
def _candidate_fields(cand: Dict[str, Any]) -> Tuple[str, str, str, str]:
    src = _s(cand.get("source")).strip().lower()
    item = cand.get("item", {}) or {}

    doi = ""
    title = ""
    year = ""
    first_author = ""

    if src == "crossref":
        doi = _s(item.get("DOI")).strip()
        titles = item.get("title") or []
        title = _s(titles[0] if titles else "").strip()
        year = _s(
            (item.get("published-print", {}).get("date-parts") or [[None]])[0][0]
            or (item.get("published-online", {}).get("date-parts") or [[None]])[0][0]
            or ""
        ).strip()
        authors = item.get("author") or []
        if authors:
            first_author = _s(authors[0].get("family")).lower().strip()

    elif src == "openalex":
        doi = _s(item.get("doi")).replace("https://doi.org/", "").strip()
        title = _s(item.get("title")).strip()
        year = _s(item.get("publication_year")).strip()
        auths = item.get("authorships") or []
        if auths and auths[0].get("author"):
            nm = _s(auths[0]["author"].get("display_name")).strip()
            first_author = nm.split()[-1].lower() if nm else ""

    return doi, title, year, first_author


def _score(ref_title: str, ref_author: str, ref_year: str, cand_title: str, cand_author: str, cand_year: str) -> Dict[str, Any]:
    ref_title = _s(ref_title).strip()
    cand_title = _s(cand_title).strip()

    title_score = fuzz.token_set_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0
    author_match = 1 if (_s(ref_author) and _s(cand_author) and _s(ref_author) == _s(cand_author)) else 0
    year_match = 1 if (_s(ref_year) and _s(cand_year) and _s(ref_year)[:4] == _s(cand_year)[:4]) else 0

    # emphasize title most, then author/year
    score = (title_score * 1.30) + (26 * author_match) + (14 * year_match)

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_match": int(author_match),
        "year_match": int(year_match),
    }


def _classify(score: int, author_match: int, year_match: int, title_score: int, doi_bonus_applied: bool) -> str:
    # If DOI match bonus was applied, allow slightly lower title score
    if doi_bonus_applied and title_score >= 82 and score >= 125:
        return "verified"
    if title_score >= 92 and (author_match or year_match) and score >= 134:
        return "verified"
    if title_score >= 86 and score >= 122:
        return "likely"
    if title_score >= 75 and score >= 98:
        return "needs_review"
    return "not_found"


# -----------------------------
# Main public function (engine.py expects these fields)
# -----------------------------
def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
    use_semantic_scholar: bool = False,  # ignored, kept for compatibility
) -> List[Dict[str, Any]]:
    refs = [r for r in (references or []) if _s(r).strip()]
    if not refs:
        return []

    if max_to_check and max_to_check > 0:
        refs = refs[: max_to_check]

    rows: List[Dict[str, Any]] = []

    with requests.Session() as session:
        for ref in refs:
            ref_raw = _s(ref).strip()

            ref_year = _extract_year(ref_raw)
            ref_author = _extract_first_author_surname(ref_raw)
            ref_doi = _extract_doi(ref_raw)
            ref_title = _extract_title_guess(ref_raw)
            ref_container = _extract_container_guess(ref_raw, ref_title)

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

                if use_crossref:
                    candidates.extend(
                        _query_crossref_attempts(
                            session=session,
                            title=ref_title,
                            author=ref_author,
                            year=ref_year,
                            container=ref_container,
                            raw_ref=ref_raw,
                        )
                    )
                    time.sleep(max(0.0, float(throttle_s or 0.0)))

                if use_openalex:
                    candidates.extend(
                        _query_openalex(
                            session=session,
                            title=ref_title,
                            author=ref_author,
                            year=ref_year,
                            container=ref_container,
                            raw_ref=ref_raw,
                        )
                    )
                    time.sleep(max(0.0, float(throttle_s or 0.0)))

                if not candidates:
                    row["status"] = "not_found"
                    rows.append(row)
                    continue

                best = None
                best_meta = None
                best_score = -1
                best_doi_bonus = False

                for cand in candidates:
                    cand_doi, cand_title, cand_year, cand_author = _candidate_fields(cand)

                    doi_bonus = 0
                    doi_bonus_applied = False
                    if ref_doi and cand_doi and ref_doi.lower() == cand_doi.lower():
                        doi_bonus = 48
                        doi_bonus_applied = True

                    meta = _score(ref_title, ref_author, ref_year, cand_title, cand_author, cand_year)
                    meta["score"] = int(meta["score"] + doi_bonus)

                    if meta["score"] > best_score:
                        best_score = meta["score"]
                        best = cand
                        best_meta = meta
                        best_doi_bonus = doi_bonus_applied

                src = _s(best.get("source") if best else "").strip()
                cand_doi, cand_title, cand_year, cand_author = _candidate_fields(best) if best else ("", "", "", "")

                row["source"] = src
                row["score"] = int(best_meta["score"] if best_meta else 0)
                row["doi"] = _s(cand_doi).strip()
                row["matched_year"] = _s(cand_year).strip()
                row["matched_authors"] = _s(cand_author).strip()
                row["matched_title"] = _s(cand_title).strip()
                row["query_used"] = _s(best.get("query_used") if best else "") or ref_title or ref_raw

                status = _classify(
                    score=int(row["score"]),
                    author_match=int(best_meta.get("author_match", 0) if best_meta else 0),
                    year_match=int(best_meta.get("year_match", 0) if best_meta else 0),
                    title_score=int(best_meta.get("title_score", 0) if best_meta else 0),
                    doi_bonus_applied=bool(best_doi_bonus),
                )
                row["status"] = _normalize_verify_status(status)
                rows.append(row)

            except Exception as e:
                row["status"] = "offline"
                row["error"] = _s(e)
                rows.append(row)

    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
