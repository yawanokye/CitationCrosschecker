# verify.py
import re
import asyncio
from typing import List, Dict, Any, Optional, Tuple

import httpx
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", re.I)


def _extract_year(text: str) -> str:
    m = YEAR_RE.search(text or "")
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""


def _strip_leading_numbering(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)
    return t.strip()


def _extract_first_author_surname(text: str) -> str:
    t = _strip_leading_numbering(text)
    if not t:
        return ""
    # Prefer "Surname," style
    if "," in t:
        first = t.split(",", 1)[0].strip()
        return re.sub(r"[^A-Za-z\-']", "", first).lower()
    # Else first token
    first = re.split(r"\s+", t)[0].strip()
    return re.sub(r"[^A-Za-z\-']", "", first).lower()


def _extract_title_guess(text: str) -> str:
    t = _strip_leading_numbering(text)
    t = re.sub(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)", " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    # Remove very common trailing DOI-like fragments
    t = re.sub(r"\bdoi\s*:\s*10\.\d{4,9}/\S+\b", "", t, flags=re.I)
    t = re.sub(r"https?://doi\.org/10\.\d{4,9}/\S+", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_doi(text: str) -> str:
    s = text or ""
    m = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", s, flags=re.I)
    return (m.group(0) or "").lower() if m else ""


def _classify(score: int, author_match: int, year_match: int, doi_match: int) -> str:
    # Strongest: DOI match
    if doi_match:
        return "verified"
    if score >= 125 and author_match and year_match:
        return "verified"
    if score >= 110 and (author_match or year_match):
        return "likely"
    if score >= 90:
        return "needs_review"
    return "not_found"


async def _safe_get_json(client: httpx.AsyncClient, url: str, params: Optional[dict] = None, timeout: float = 15.0) -> Optional[dict]:
    try:
        r = await client.get(url, params=params, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _score_candidate(ref_raw: str, cand_source: str, item: dict) -> Dict[str, Any]:
    ref_year = _extract_year(ref_raw)
    ref_author = _extract_first_author_surname(ref_raw)
    ref_title = _extract_title_guess(ref_raw)
    ref_doi = _extract_doi(ref_raw)

    cand_title = ""
    cand_year = ""
    cand_author = ""
    cand_doi = ""

    if cand_source == "crossref":
        cand_doi = (item.get("DOI") or "").strip().lower()
        titles = item.get("title") or []
        cand_title = (titles[0] if titles else "") or ""
        cand_year = str(
            (item.get("published-print", {}).get("date-parts") or [[None]])[0][0]
            or (item.get("published-online", {}).get("date-parts") or [[None]])[0][0]
            or ""
        )
        authors = item.get("author") or []
        if authors:
            cand_author = (authors[0].get("family") or "").lower()

    if cand_source == "openalex":
        cand_doi = (item.get("doi") or "").replace("https://doi.org/", "").strip().lower()
        cand_title = (item.get("title") or "") or ""
        cand_year = str(item.get("publication_year") or "")
        auths = item.get("authorships") or []
        if auths and auths[0].get("author"):
            cand_author = (auths[0]["author"].get("display_name") or "").split()[-1].lower()

    title_score = fuzz.token_set_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0
    author_match = 1 if (ref_author and cand_author and ref_author == cand_author) else 0
    year_match = 1 if (ref_year and cand_year and ref_year[:4] == str(cand_year)[:4]) else 0
    doi_match = 1 if (ref_doi and cand_doi and ref_doi.lower() == cand_doi.lower()) else 0

    # Score weights tuned for bibliographic fuzz
    score = (
        int(title_score)
        + (25 * author_match)
        + (12 * year_match)
        + (60 * doi_match)
    )

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_match": int(author_match),
        "year_match": int(year_match),
        "doi_match": int(doi_match),
        "doi": cand_doi,
        "matched_title": cand_title,
        "matched_year": cand_year,
        "matched_first_author": cand_author,
    }


def _build_queries(ref: str) -> Dict[str, Any]:
    """
    Build better search payload from parsed bits.
    """
    title = _extract_title_guess(ref)
    author = _extract_first_author_surname(ref)
    year = _extract_year(ref)

    # Shorten very long inputs (Crossref/OpenAlex do worse with huge strings)
    if len(title) > 220:
        title = title[:220]

    return {"title": title, "author": author, "year": year}


async def _query_crossref(client: httpx.AsyncClient, ref_raw: str) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"
    q = _build_queries(ref_raw)

    params = {
        # Use title-focused query first
        "query.title": q["title"] or ref_raw,
        "rows": 5,
        "select": "DOI,title,author,published-print,published-online",
    }

    # Add author hint if available
    if q["author"]:
        params["query.author"] = q["author"]

    # Add year filter window if available
    if q["year"] and q["year"][:4].isdigit():
        y = q["year"][:4]
        params["filter"] = f"from-pub-date:{y}-01-01,until-pub-date:{y}-12-31"

    data = await _safe_get_json(client, url, params=params, timeout=15.0)
    if not data:
        return []

    items = (data.get("message") or {}).get("items") or []
    return [{"source": "crossref", "item": it} for it in items]


async def _query_openalex(client: httpx.AsyncClient, ref_raw: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"
    q = _build_queries(ref_raw)

    # OpenAlex works best with a compact search string
    search = q["title"] or ref_raw
    if q["author"]:
        search = f"{q['author']} {search}"
    if q["year"]:
        search = f"{search} {q['year'][:4]}"

    params = {
        "search": search,
        "per-page": 5,
        "select": "id,doi,title,publication_year,authorships",
    }

    data = await _safe_get_json(client, url, params=params, timeout=15.0)
    if not data:
        return []

    results = data.get("results") or []
    return [{"source": "openalex", "item": it} for it in results]


async def _verify_one(
    client: httpx.AsyncClient,
    ref: str,
    throttle_s: float,
    use_crossref: bool,
    use_openalex: bool,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "reference": ref,
        "status": "offline",
        "source": "",
        "score": 0,
        "doi": "",
        "matched_year": "",
        "matched_first_author": "",
        "matched_title": "",
        "query_used": "",
        "error": "",
    }

    try:
        candidates: List[Dict[str, Any]] = []

        # Run both sources in parallel for this reference
        tasks = []
        if use_crossref:
            tasks.append(_query_crossref(client, ref))
        if use_openalex:
            tasks.append(_query_openalex(client, ref))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                continue
            candidates.extend(r or [])

        if throttle_s and throttle_s > 0:
            await asyncio.sleep(float(throttle_s))

        if not candidates:
            row["status"] = "not_found"
            return row

        best = None
        best_score = -1
        best_meta = None

        for cand in candidates:
            src = cand.get("source", "")
            item = cand.get("item", {}) or {}
            meta = _score_candidate(ref, src, item)
            if meta["score"] > best_score:
                best_score = meta["score"]
                best = cand
                best_meta = meta

        src = best.get("source") if best else ""
        row["source"] = src
        row["score"] = int(best_meta["score"] if best_meta else 0)
        row["doi"] = best_meta.get("doi", "") if best_meta else ""
        row["matched_year"] = str(best_meta.get("matched_year", "") if best_meta else "")
        row["matched_first_author"] = best_meta.get("matched_first_author", "") if best_meta else ""
        row["matched_title"] = best_meta.get("matched_title", "") if best_meta else ""

        status = _classify(
            score=int(row["score"]),
            author_match=int(best_meta.get("author_match", 0) if best_meta else 0),
            year_match=int(best_meta.get("year_match", 0) if best_meta else 0),
            doi_match=int(best_meta.get("doi_match", 0) if best_meta else 0),
        )
        row["status"] = _normalize_verify_status(status)
        return row

    except Exception as e:
        row["status"] = "offline"
        row["error"] = str(e)
        return row


async def verify_references_batch_async(
    references: List[str],
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
    concurrency: int = 6,
) -> List[Dict[str, Any]]:
    refs = [r for r in (references or []) if (r or "").strip()]
    if not refs:
        return []

    # Concurrency guard
    sem = asyncio.Semaphore(max(1, int(concurrency or 6)))

    async with httpx.AsyncClient(headers={"User-Agent": "CitationCrosschecker/2.0 (UCC)"} ) as client:
        async def run_one(ref: str):
            async with sem:
                return await _verify_one(
                    client=client,
                    ref=ref,
                    throttle_s=float(throttle_s or 0.0),
                    use_crossref=bool(use_crossref),
                    use_openalex=bool(use_openalex),
                )

        rows = await asyncio.gather(*[run_one(r) for r in refs], return_exceptions=True)

    out: List[Dict[str, Any]] = []
    for r in rows:
        if isinstance(r, Exception):
            out.append({"reference": "", "status": "offline", "source": "", "score": 0, "doi": "", "matched_year": "", "matched_first_author": "", "matched_title": "", "query_used": "", "error": str(r)})
        else:
            r["status"] = _normalize_verify_status(r.get("status"))
            out.append(r)

    return out
