# verify.py
import re
import time
from typing import List, Dict, Any, Optional, Tuple
from functools import lru_cache

import requests
from rapidfuzz import fuzz

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}

UA = "CitationCrosschecker/1.0 (+https://citationcrosschecker.onrender.com)"

DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)", re.I)


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    return st if st in _ALLOWED_VERIFY_STATUSES else "needs_review"


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("’", "'")
    s = re.sub(r"\s+", " ", s)
    return s


def _safe_int(x, default=0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _extract_year(text: str) -> str:
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", text or "", flags=re.I)
    return (m.group(1) + (m.group(2) or "")).lower() if m else ""


def _extract_doi(text: str) -> str:
    if not text:
        return ""
    m = DOI_RE.search(text)
    if not m:
        return ""
    doi = m.group(1).strip().rstrip(").,;")
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    doi = doi.replace("https://dx.doi.org/", "").replace("http://dx.doi.org/", "")
    return doi.strip()


def _strip_leading_numbering(ref: str) -> str:
    t = (ref or "").strip()
    t = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", t)
    return t.strip()


def _extract_first_author_surname(ref: str) -> str:
    t = _strip_leading_numbering(ref)
    if not t:
        return ""
    # APA: Surname, I.
    if "," in t:
        first = t.split(",", 1)[0].strip()
        first = re.sub(r"[^A-Za-z\-']", "", first).lower()
        return first
    # else: take first word token
    first = re.split(r"\s+", t)[0].strip()
    return re.sub(r"[^A-Za-z\-']", "", first).lower()


def _extract_author_surnames(ref: str, max_n: int = 8) -> List[str]:
    """
    Pulls a small set of likely author surnames from the author segment.
    Works best for APA refs: "Apergis, N., Chang, T., ... & Gupta, R. (2017)."
    """
    t = _strip_leading_numbering(ref)
    # cut at year
    y = re.search(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)", t, flags=re.I)
    author_seg = t[:y.start()] if y else t[:220]
    # remove "et al"
    author_seg = re.sub(r"\bet\s+al\.?\b", "", author_seg, flags=re.I)

    # collect "Surname," patterns
    surnames = re.findall(r"\b([A-Z][A-Za-z\-']{2,50})\s*,\s*[A-Z]", author_seg)
    out = []
    for s in surnames:
        s2 = re.sub(r"[^A-Za-z\-']", "", s).lower()
        if s2 and s2 not in out:
            out.append(s2)
        if len(out) >= max_n:
            break
    # fallback: if none, use first author
    if not out:
        fa = _extract_first_author_surname(ref)
        if fa:
            out = [fa]
    return out


def _extract_title_guess(ref: str) -> str:
    """
    Better title extraction than the old approach:
    - Try APA: after (YEAR). Title.  -> capture between ")." and next "."
    - Else: remove DOI/URL and return a compact chunk
    """
    t = _strip_leading_numbering(ref)

    # remove DOI/URL noise first
    t = re.sub(r"https?://\S+", " ", t, flags=re.I)
    t = re.sub(r"\bdoi\s*:\s*\S+", " ", t, flags=re.I)
    t = re.sub(DOI_RE, " ", t)

    # APA year anchor
    m = re.search(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\s*\.\s*", t, flags=re.I)
    if m:
        rest = t[m.end():]
        # title until next period (but allow abbreviations by requiring some length)
        m2 = re.search(r"\.\s", rest)
        if m2 and m2.start() >= 8:
            title = rest[:m2.start()].strip()
            title = re.sub(r"\s+", " ", title)
            return title

        # fallback: first 140 chars
        return re.sub(r"\s+", " ", rest[:140]).strip()

    # fallback: remove authors chunk a bit and keep middle
    return re.sub(r"\s+", " ", t[:180]).strip()


def _title_score(a: str, b: str) -> int:
    if not a or not b:
        return 0
    # combine two robust scorers
    s1 = fuzz.token_set_ratio(a, b)
    s2 = fuzz.partial_ratio(a, b)
    return int(max(s1, 0.85 * s2))


def _author_overlap_score(ref_authors: List[str], cand_authors: List[str]) -> Tuple[int, float]:
    """
    Returns (points, overlap_ratio)
    """
    if not ref_authors or not cand_authors:
        return 0, 0.0
    rs = set([_norm(x) for x in ref_authors if x])
    cs = set([_norm(x) for x in cand_authors if x])
    if not rs or not cs:
        return 0, 0.0
    inter = rs.intersection(cs)
    overlap = len(inter) / max(1, min(len(rs), len(cs)))
    # scale to points
    pts = int(round(30 * overlap))
    return pts, overlap


def _year_match_points(ref_year: str, cand_year: str) -> int:
    if not ref_year or not cand_year:
        return 0
    return 10 if ref_year[:4] == str(cand_year)[:4] else 0


def _doi_match_points(ref_doi: str, cand_doi: str) -> int:
    if not ref_doi or not cand_doi:
        return 0
    return 25 if _norm(ref_doi) == _norm(cand_doi) else 0


class _Http:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.cache: Dict[Tuple[str, str], Optional[dict]] = {}

    def get_json(self, url: str, params: Optional[dict] = None, timeout: int = 15) -> Optional[dict]:
        key = (url, str(sorted((params or {}).items())))
        if key in self.cache:
            return self.cache[key]
        try:
            r = self.s.get(url, params=params, timeout=timeout)
            if r.status_code != 200:
                self.cache[key] = None
                return None
            data = r.json()
            self.cache[key] = data
            return data
        except Exception:
            self.cache[key] = None
            return None


def _crossref_item_fields(item: dict) -> Tuple[str, str, List[str]]:
    doi = (item.get("DOI") or "").strip()
    titles = item.get("title") or []
    title = (titles[0] if titles else "") or ""
    year = ""
    for k in ["published-print", "published-online", "issued", "created"]:
        dp = (item.get(k, {}) or {}).get("date-parts") or []
        if dp and dp[0] and dp[0][0]:
            year = str(dp[0][0])
            break
    authors = item.get("author") or []
    cand_auth = []
    for a in authors[:8]:
        fam = (a.get("family") or "").strip()
        if fam:
            cand_auth.append(fam.lower())
    return doi, year, cand_auth


def _openalex_item_fields(item: dict) -> Tuple[str, str, List[str], str]:
    doi = (item.get("doi") or "").replace("https://doi.org/", "").strip()
    title = (item.get("title") or "") or ""
    year = str(item.get("publication_year") or "")
    auths = item.get("authorships") or []
    cand_auth = []
    for a in auths[:8]:
        au = (a.get("author") or {}).get("display_name") or ""
        if au:
            cand_auth.append(au.split()[-1].lower())
    # OA id can be used for direct lookups later
    oa_id = (item.get("id") or "").strip()
    return doi, year, cand_auth, oa_id


def _query_crossref(http: _Http, title: str, first_author: str, year: str, raw_fallback: str) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"

    # structured query works much better than query.bibliographic alone
    params = {
        "rows": 8,
        "query.title": title[:200] if title else "",
        "query.author": first_author[:80] if first_author else "",
    }
    if year and year[:4].isdigit():
        params["filter"] = f"from-pub-date:{year[:4]}-01-01,until-pub-date:{year[:4]}-12-31"

    data = http.get_json(url, params=params, timeout=18)
    items = ((data or {}).get("message") or {}).get("items") or []
    if items:
        return [{"source": "crossref", "item": it} for it in items]

    # fallback: bibliographic search on a compact string
    params2 = {"query.bibliographic": raw_fallback[:240], "rows": 8}
    data2 = http.get_json(url, params=params2, timeout=18)
    items2 = ((data2 or {}).get("message") or {}).get("items") or []
    return [{"source": "crossref", "item": it} for it in items2]


def _query_openalex(http: _Http, title: str, first_author: str, year: str, raw_fallback: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"
    # OpenAlex search likes title-heavy queries
    q = " ".join([title, first_author, year[:4]]).strip()
    if not q:
        q = raw_fallback[:240]
    params = {"search": q[:260], "per-page": 8}
    data = http.get_json(url, params=params, timeout=18)
    results = (data or {}).get("results") or []
    return [{"source": "openalex", "item": it} for it in results]


def _query_by_doi(http: _Http, doi: str) -> List[Dict[str, Any]]:
    doi = (doi or "").strip()
    if not doi:
        return []

    out = []

    # Crossref DOI direct
    cr = http.get_json(f"https://api.crossref.org/works/{doi}", params=None, timeout=18)
    if cr and (cr.get("message") or {}):
        out.append({"source": "crossref", "item": cr["message"]})

    # OpenAlex DOI direct
    oa = http.get_json(f"https://api.openalex.org/works/https://doi.org/{doi}", params=None, timeout=18)
    if oa and oa.get("id"):
        out.append({"source": "openalex", "item": oa})

    return out


def _score_candidate(ref_raw: str, cand: dict, ref_meta: dict) -> Dict[str, Any]:
    ref_title = ref_meta["title"]
    ref_year = ref_meta["year"]
    ref_doi = ref_meta["doi"]
    ref_authors = ref_meta["authors"]

    cand_title = ""
    cand_year = ""
    cand_doi = ""
    cand_authors: List[str] = []
    src = cand.get("source")

    if src == "crossref":
        item = cand.get("item", {}) or {}
        cand_doi, cand_year, cand_authors = _crossref_item_fields(item)
        titles = item.get("title") or []
        cand_title = (titles[0] if titles else "") or ""

    if src == "openalex":
        item = cand.get("item", {}) or {}
        cand_doi, cand_year, cand_authors, _ = _openalex_item_fields(item)
        cand_title = (item.get("title") or "") or ""

    ts = _title_score(ref_title, cand_title)
    ap, overlap = _author_overlap_score(ref_authors, cand_authors)
    yp = _year_match_points(ref_year, cand_year)
    dp = _doi_match_points(ref_doi, cand_doi)

    # Total score: title dominates, then author overlap, then DOI, then year
    score = int(round((1.25 * ts) + ap + dp + yp))

    return {
        "score": score,
        "title_score": int(ts),
        "author_overlap": float(overlap),
        "author_points": int(ap),
        "year_points": int(yp),
        "doi_points": int(dp),
        "doi": cand_doi,
        "matched_title": cand_title,
        "matched_year": cand_year,
        "matched_authors": ", ".join(cand_authors[:3]),
    }


def _classify(meta: Dict[str, Any]) -> str:
    score = _safe_int(meta.get("score"), 0)
    doi_pts = _safe_int(meta.get("doi_points"), 0)
    yr_pts = _safe_int(meta.get("year_points"), 0)
    overlap = float(meta.get("author_overlap") or 0.0)
    title_score = _safe_int(meta.get("title_score"), 0)

    # strong signals
    if doi_pts >= 25 and title_score >= 70:
        return "verified"

    if score >= 135 and (overlap >= 0.5) and (yr_pts >= 10 or title_score >= 85):
        return "verified"

    if score >= 120 and (overlap >= 0.34 or yr_pts >= 10):
        return "likely"

    if score >= 100 and title_score >= 70:
        return "needs_review"

    if score >= 85:
        return "needs_review"

    return "not_found"


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

    Key upgrades:
    - DOI-first lookup (huge boost in accuracy)
    - structured Crossref queries (query.title + query.author + year filter)
    - better title extraction and scoring
    - author overlap scoring instead of only first-author equality
    - request/session caching for speed and stability
    """
    refs = [r for r in (references or []) if (r or "").strip()]
    if not refs:
        return []

    if max_to_check and max_to_check > 0:
        refs = refs[: max_to_check]

    http = _Http()
    rows: List[Dict[str, Any]] = []

    for ref in refs:
        ref_raw = (ref or "").strip()
        if not ref_raw:
            continue

        ref_doi = _extract_doi(ref_raw)
        ref_year = _extract_year(ref_raw)
        first_author = _extract_first_author_surname(ref_raw)
        ref_authors = _extract_author_surnames(ref_raw)
        title = _extract_title_guess(ref_raw)

        # Create a stable query_used that mirrors the “better” behaviour:
        # title + first author + year is consistently best for Crossref/OpenAlex
        query_used = " ".join([title, first_author, (ref_year[:4] if ref_year else "")]).strip()
        if not query_used:
            query_used = ref_raw[:260]

        row: Dict[str, Any] = {
            "reference": ref_raw,
            "status": "offline",
            "source": "",
            "score": 0,
            "doi": "",
            "matched_year": "",
            "matched_first_author": "",
            "matched_title": "",
            "matched_authors": "",
            "query_used": query_used,
            "error": "",
        }

        try:
            candidates: List[Dict[str, Any]] = []

            # 1) DOI direct lookups (fast + accurate)
            if ref_doi:
                candidates.extend(_query_by_doi(http, ref_doi))

            # 2) Search queries
            if not candidates:
                if use_crossref:
                    candidates.extend(_query_crossref(http, title, first_author, ref_year, ref_raw))
                    time.sleep(max(0.0, float(throttle_s or 0.0)))
                if use_openalex:
                    candidates.extend(_query_openalex(http, title, first_author, ref_year, ref_raw))
                    time.sleep(max(0.0, float(throttle_s or 0.0)))

            if not candidates:
                row["status"] = "not_found"
                rows.append(row)
                continue

            # Score all candidates, take best
            ref_meta = {"doi": ref_doi, "year": ref_year, "authors": ref_authors, "title": title}

            best = None
            best_meta = None
            best_score = -1

            for cand in candidates[:20]:
                meta = _score_candidate(ref_raw, cand, ref_meta)
                if meta["score"] > best_score:
                    best_score = meta["score"]
                    best = cand
                    best_meta = meta

            if not best or not best_meta:
                row["status"] = "not_found"
                rows.append(row)
                continue

            row["source"] = best.get("source") or ""
            row["score"] = int(best_meta.get("score") or 0)
            row["doi"] = best_meta.get("doi", "") or ""
            row["matched_year"] = str(best_meta.get("matched_year", "") or "")
            row["matched_title"] = best_meta.get("matched_title", "") or ""
            row["matched_authors"] = best_meta.get("matched_authors", "") or ""

            # matched_first_author: best effort from matched_authors
            ma = (row["matched_authors"] or "").split(",")[0].strip()
            row["matched_first_author"] = ma

            status = _classify(best_meta)
            row["status"] = _normalize_verify_status(status)
            rows.append(row)

        except Exception as e:
            row["status"] = "offline"
            row["error"] = str(e)
            rows.append(row)

    # Guarantee allowed statuses
    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
