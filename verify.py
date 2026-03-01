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

UNPAYWALL_EMAIL = (os.getenv("UNPAYWALL_EMAIL") or MAILTO or "").strip()


# ---------- helpers ----------
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


def _clean_query_string(s: str) -> str:
    s = _safe_strip(s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > 280:
        s = s[:280].rstrip()
    return s


def _doi_equal(a: str, b: str) -> bool:
    a = _safe_strip(a).lower()
    b = _safe_strip(b).lower()
    if not a or not b:
        return False
    return a == b


# ============================================================================
# APA FORMAT CONVERSION - Convert any reference style to APA format
# ============================================================================

_STOP_AUTHOR_KEYS = {
    # common false positives from narrative text
    "survey","surveys","field","fields","fieldwork","work","works","study","studies","table","figure",
    "chapter","section","appendix","appendices","supplementary","supporting","information","data",
    "analysis","method","methods","results","discussion","conclusion","reference","references",
    "available", "retrieved", "accessed", "archive", "repository"
}

_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})(?:[a-z])?\b")


def _convert_to_apa(ref: str) -> Dict[str, Any]:
    """
    Convert any reference style to APA format and extract key components.
    Returns dict with: authors, year, title, journal, volume, issue, pages, doi
    """
    ref = _safe_strip(ref)
    if not ref:
        return {}
    
    result = {
        "authors": [],
        "year": "",
        "title": "",
        "journal": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": "",
        "apa_string": ""
    }
    
    # Extract DOI first (most reliable)
    result["doi"] = _extract_doi(ref)
    
    # Remove leading numbering
    clean_ref = _strip_leading_numbering(ref)
    
    # Extract year
    year_match = _YEAR_RE.search(clean_ref)
    if year_match:
        result["year"] = year_match.group(1)
    
    # Split into parts based on common patterns
    parts = []
    
    # Try to split by year with parentheses (APA style)
    if result["year"]:
        year_pattern = r'[\(\[]?\s*' + re.escape(result["year"]) + r'\s*[\)\]]?'
        split_parts = re.split(year_pattern, clean_ref, maxsplit=1)
        if len(split_parts) >= 2:
            # Everything before year is author block
            author_block = split_parts[0].strip(" .,;:")
            # Everything after year is title + journal + etc
            after_year = split_parts[1].strip(" .,;:")
            parts = [author_block, after_year]
    
    # If no year-based split, try to find author block by looking for comma + year
    if not parts:
        # Look for pattern: Author, A. (Year).
        m = re.search(r'^(.+?)[,\.]\s+[\(\[]?(\d{4})[\)\]]?\.?\s+(.+)$', clean_ref, re.I)
        if m:
            parts = [m.group(1).strip(), m.group(3).strip()]
            if not result["year"]:
                result["year"] = m.group(2)
    
    # If still no split, try IEEE/Vancouver pattern
    if not parts:
        # Look for: Author, A., "Title", Journal, vol, no, pp, year
        m = re.search(r'^(.+?)[,\.]\s+["“](.+?)["”][,\.]\s+(.+?)[,\.]\s+vol\.?\s*(\d+)[,\.]\s*(?:no\.?\s*(\d+))?[,\.]\s*(?:pp?\.?\s*(\d+(?:-\d+)?))?[,\.]\s*(?:[\(\[]?(\d{4})[\)\]]?)?', clean_ref, re.I)
        if m:
            parts = [m.group(1).strip(), m.group(2).strip(), m.group(3).strip()]
            result["volume"] = m.group(4) if m.group(4) else ""
            result["issue"] = m.group(5) if m.group(5) else ""
            result["pages"] = m.group(6) if m.group(6) else ""
            if not result["year"] and m.group(7):
                result["year"] = m.group(7)
    
    # Extract authors from author block
    if parts and parts[0]:
        author_block = parts[0]
        
        # Split authors by '&', 'and', ','
        author_parts = re.split(r'\s+(?:&|and|＆)\s+|\s*,\s*', author_block, flags=re.I)
        
        for auth in author_parts:
            auth = auth.strip(" .,;:")
            if not auth or auth.lower() in _STOP_AUTHOR_KEYS:
                continue
            
            # Handle "Surname, Initials" format
            if ',' in auth:
                surname_part = auth.split(',')[0].strip()
                # Take last word of surname part (handle compound surnames)
                surname_words = surname_part.split()
                if surname_words:
                    surname = surname_words[-1].strip(" .,")
                else:
                    surname = surname_part
            else:
                # Handle "Initials Surname" format - take last word
                name_parts = auth.split()
                if name_parts:
                    surname = name_parts[-1].strip(" .,")
                else:
                    surname = auth
            
            # Clean surname
            surname = re.sub(r"[^A-Za-z\-']", "", surname).lower().strip()
            if surname and len(surname) >= 2 and surname not in _STOP_AUTHOR_KEYS:
                result["authors"].append(surname)
        
        # Limit to first 3 authors for query
        result["authors"] = result["authors"][:3]
    
    # Extract title
    if len(parts) >= 2:
        title_candidate = parts[1]
        # Remove trailing journal info if present
        title_parts = re.split(r'[\.!?]\s+(?:In|Journal|Proceedings|Conference|\(|$)', title_candidate, maxsplit=1, flags=re.I)
        result["title"] = title_parts[0].strip(" .,;:\"'")
    
    # Extract journal from remaining parts
    if len(parts) >= 3:
        result["journal"] = parts[2].strip(" .,;:")
    elif len(parts) >= 2 and not result["journal"]:
        # Check if second part contains journal indicators
        if re.search(r'\b(?:Journal|Review|Letters|Proceedings|Conference|Trans\.|Ann\.|Int\.|J\.|Rev\.|Res\.|BMC|PLOS|Nature|Science|Cell|Elsevier|Springer|Wiley|Taylor|Sage)\b', parts[1], re.I):
            result["journal"] = parts[1].strip(" .,;:")
    
    # Clean up extracted fields
    result["title"] = re.sub(r'\s+', ' ', result["title"]).strip()
    result["journal"] = re.sub(r'\s+', ' ', result["journal"]).strip()
    
    # Build APA string for query
    apa_parts = []
    
    # Add authors (APA format: Last, A., & Last, B.)
    if result["authors"]:
        apa_authors = []
        for i, auth in enumerate(result["authors"]):
            if i == 0:
                apa_authors.append(auth.capitalize())
            elif i == len(result["authors"]) - 1 and len(result["authors"]) > 1:
                apa_authors.append(f"& {auth.capitalize()}")
            else:
                apa_authors.append(auth.capitalize())
        
        if len(apa_authors) == 1:
            apa_parts.append(f"{apa_authors[0]}.")
        elif len(apa_authors) == 2:
            apa_parts.append(f"{apa_authors[0]} & {apa_authors[1]}.")
        elif len(apa_authors) >= 3:
            apa_parts.append(f"{apa_authors[0]}, {apa_authors[1]}, & {apa_authors[2]}.")
    
    # Add year
    if result["year"]:
        apa_parts.append(f"({result['year']}).")
    
    # Add title
    if result["title"]:
        # Capitalize first letter of title
        title = result["title"].capitalize()
        apa_parts.append(f"{title}.")
    
    # Add journal
    if result["journal"]:
        journal = result["journal"]
        apa_parts.append(f"*{journal}*")
    
    # Add volume/issue/pages
    vol_issue = []
    if result["volume"]:
        vol_issue.append(result["volume"])
    if result["issue"]:
        vol_issue.append(f"({result['issue']})")
    if vol_issue:
        apa_parts.append("".join(vol_issue))
    
    if result["pages"]:
        apa_parts.append(result["pages"])
    
    # Add DOI
    if result["doi"]:
        apa_parts.append(f"https://doi.org/{result['doi']}")
    
    result["apa_string"] = " ".join(apa_parts)
    
    return result


def _build_apa_query(apa_data: Dict[str, Any]) -> str:
    """Build optimal query from APA-converted data."""
    query_parts = []
    
    # Add title (most important)
    if apa_data.get("title"):
        query_parts.append(apa_data["title"])
    
    # Add author surnames
    if apa_data.get("authors"):
        query_parts.append(" ".join(apa_data["authors"]))
    
    # Add year
    if apa_data.get("year"):
        query_parts.append(apa_data["year"])
    
    # Add journal if available
    if apa_data.get("journal") and len(apa_data["journal"]) > 5:
        query_parts.append(apa_data["journal"])
    
    query = " ".join(query_parts)
    return _clean_query_string(query)


# ---------- canonical parsing (kept for backward compatibility) ----------
def _pick_year(s: str) -> str:
    m = _YEAR_RE.search(s or "")
    return m.group(1) if m else ""

def _strip_urls_and_doi_tail(s: str) -> str:
    s = re.sub(r"https?://\S+", " ", s, flags=re.I)
    s = re.sub(r"\bdoi\s*[:]?\s*\S+", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()

def _extract_title_numbered_or_ieee(ref: str) -> str:
    s = _safe_strip(ref)
    if not s:
        return ""
    # remove leading numbering
    s = re.sub(r"^\s*\[\s*\d{1,4}\s*\]\s*", "", s)
    s = re.sub(r"^\s*\d{1,4}[.)]\s*", "", s)

    # prefer quoted title (IEEE often)
    m = re.search(r"[\"“](.+?)[\"”]", s)
    if m:
        t = m.group(1).strip()
        return t

    # Vancouver-like: try to find the best 'sentence chunk' that looks like a title
    s2 = _strip_urls_and_doi_tail(s)
    # split on period+space. Keep a few early chunks.
    parts = [p.strip() for p in re.split(r"\.\s+", s2) if p.strip()]
    if not parts:
        return ""
    # heuristic: title is usually the longest early chunk with >=4 words and few digits
    def score(p: str) -> int:
        w = p.split()
        if len(w) < 4:
            return -10
        if sum(ch.isdigit() for ch in p) > 2:
            return -5
        # penalize very short chunks and journal-like abbreviations
        if len(p) < 25:
            return -2
        if re.search(r"\b(j|vol|no|pp|pages|ed|edition)\b", p, flags=re.I):
            return -1
        return len(p)
    # skip first chunk if it looks like an author list (many commas/initials)
    cand_parts = parts[:6]
    if cand_parts and (cand_parts[0].count(",") >= 2 or re.search(r"\b[A-Z]{1,3}\b", cand_parts[0])):
        cand_parts = cand_parts[1:6]
    best = max(cand_parts, key=score)
    return best if score(best) > 0 else ""

def _extract_authors_numbered_or_ieee(ref: str, max_authors: int = 8) -> List[str]:
    s = _safe_strip(ref)
    if not s:
        return []
    s = re.sub(r"^\s*\[\s*\d{1,4}\s*\]\s*", "", s)
    s = re.sub(r"^\s*\d{1,4}[.)]\s*", "", s)

    # if quoted title, authors are before first quote
    q = re.search(r"[\"“]", s)
    prefix = s[: q.start()].strip() if q else ""
    if not prefix:
        # else try before first period-space as authors prefix
        m = re.search(r"\.\s+", s)
        prefix = s[: m.start()].strip() if m else s[:120]

    # split authors on commas and conjunctions
    bits = re.split(r"\s*(?:,|\band\b|\&|;)\s*", prefix, flags=re.I)
    surnames: List[str] = []
    for b in bits:
        b = b.strip()
        if not b:
            continue
        # vancouver: "Button KS" -> surname is first token; IEEE: "J. Tan" -> surname last token
        toks = [t for t in re.split(r"\s+", b) if t]
        if not toks:
            continue
        # choose token that contains letters and is not just initials
        cand1 = re.sub(r"[^A-Za-z\-']", "", toks[0]).lower()
        candN = re.sub(r"[^A-Za-z\-']", "", toks[-1]).lower()
        cand = cand1 if (len(cand1) >= 2 and not re.fullmatch(r"[a-z]{1,2}", cand1)) else candN
        cand = cand.strip("-'").strip()
        if not cand or cand in _STOP_AUTHOR_KEYS:
            continue
        if cand not in surnames:
            surnames.append(cand)
        if len(surnames) >= max_authors:
            break
    return surnames


def _canonical_fields(ref_raw: str) -> Tuple[str, List[str], str, str]:
    """Return (title, authors_surnames, year, doi) using style-agnostic heuristics."""
    ref_raw = _safe_strip(ref_raw)
    doi = _extract_doi(ref_raw)
    year = _extract_year(ref_raw) or _pick_year(ref_raw)

    # title
    title = _extract_title_guess(ref_raw)
    if not title or len(title) < 8:
        title = _extract_title_numbered_or_ieee(ref_raw)

    # authors
    authors = _extract_author_surnames(ref_raw, max_authors=8)
    if not authors:
        authors = _extract_authors_numbered_or_ieee(ref_raw, max_authors=8)

    # final cleanup stopwords
    authors = [a for a in authors if a and a not in _STOP_AUTHOR_KEYS]
    return title, authors, year, doi


def _extract_title_guess(ref: str) -> str:
    """
    Best-effort title guess:
    - remove leading numbering
    - remove DOI + doi.org link
    - remove URLs/noisy "Retrieved from / Link via"
    - prefer sentence after (YEAR) when present
    """
    t = _strip_leading_numbering(ref)
    t = re.sub(r"\s+", " ", t).strip()

    # remove DOI forms and doi.org links
    t = re.sub(r"(doi\s*:\s*)?10\.\d{4,9}/\S+", "", t, flags=re.I)
    t = re.sub(r"https?://doi\.org/10\.\d{4,9}/\S+", "", t, flags=re.I)

    # remove urls and noisy tails
    t = re.sub(r"https?://\S+", "", t, flags=re.I)
    t = re.sub(r"\b(retrieved\s+from|link\s+via)\b.*$", "", t, flags=re.I)

    parts = re.split(r"\(\s*(1[6-9]\d{2}|20\d{2})([a-z])?\s*\)\.?\s*", t, maxsplit=1, flags=re.I)
    if len(parts) >= 4:
        after = _safe_strip(parts[3])
    else:
        m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", t, flags=re.I)
        after = t[m.end():].strip() if m else t

    after = after.lstrip(". ").strip()
    # take first sentence chunk as title
    title = after.split(".", 1)[0].strip() if "." in after else after.strip()

    if len(title) < 8:
        title = t.strip()

    return _norm_text(title)


def _extract_author_surnames(ref: str, max_authors: int = 8) -> List[str]:
    """
    Robust-ish for APA/Vancouver:
    - take block before year as "author block"
    - split by '&', 'and', ';'
    - handle "Surname, Initials" and "Surname Initials" patterns
    """
    t = _strip_leading_numbering(ref)
    if not t:
        return []

    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", t, flags=re.I)
    head = t[: m.start()].strip() if m else t

    head = head.replace("&", " and ")
    head = re.sub(r"\bet\s+al\.?\b", "", head, flags=re.I)
    head = re.sub(r"\s+", " ", head).strip()
    if not head:
        return []

    parts = re.split(r";|\band\b", head, flags=re.I)
    surnames: List[str] = []

    for p in parts:
        p = p.strip(" ,.")
        if not p:
            continue

        # APA often: "Austin, P. C.," -> surname before comma
        if "," in p:
            cand = p.split(",", 1)[0].strip()
        else:
            # otherwise last token
            toks = p.split()
            cand = toks[-1] if toks else ""

        cand = re.sub(r"[^A-Za-z\-']", "", cand).lower().strip()
        if cand and len(cand) >= 2 and cand not in surnames:
            surnames.append(cand)

        if len(surnames) >= max_authors:
            break

    return surnames


# ---------- queries ----------
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


def _query_crossref(q: str, a_query: str) -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"
    params: Dict[str, Any] = {
        "query.bibliographic": q,
        "rows": 5,
        "sort": "score",
        "order": "desc",
    }
    if a_query:
        params["query.author"] = a_query
    if MAILTO:
        params["mailto"] = MAILTO

    data = _safe_get_json(url, params=params, timeout=22)
    if not data:
        return []
    items = ((data.get("message") or {}).get("items") or [])
    return [{"source": "crossref", "item": it or {}, "query_used": q} for it in items]


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


def _query_openalex(q: str) -> List[Dict[str, Any]]:
    url = "https://api.openalex.org/works"
    params: Dict[str, Any] = {"search": q, "per-page": 5}
    if MAILTO:
        params["mailto"] = MAILTO

    data = _safe_get_json(url, params=params, timeout=22)
    if not data:
        return []
    results = data.get("results") or []
    return [{"source": "openalex", "item": it or {}, "query_used": q} for it in results]


def _query_unpaywall_by_doi(doi: str) -> Optional[Dict[str, Any]]:
    doi = _safe_strip(doi)
    if not doi or not UNPAYWALL_EMAIL:
        return None
    url = f"https://api.unpaywall.org/v2/{doi}"
    params = {"email": UNPAYWALL_EMAIL}
    data = _safe_get_json(url, params=params, timeout=22)
    if not data:
        return None
    return {"source": "unpaywall", "item": data or {}, "query_used": f"doi:{doi}"}


# ---------- candidate extraction ----------
def _candidate_fields(cand: Dict[str, Any]) -> Tuple[str, str, str, List[str], int]:
    """
    Returns (doi, title_norm, year, author_surnames[], api_score_if_any)
    """
    src = _safe_strip((cand or {}).get("source"))
    item = (cand or {}).get("item") or {}

    doi = ""
    title = ""
    year = ""
    authors: List[str] = []
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
        y = (pp[0][0] if pp and pp[0] else None) or (po[0][0] if po and po[0] else None) or ""
        year = _safe_str(y).strip()

        for au in (item.get("author") or [])[:10]:
            fam = _safe_strip((au or {}).get("family")).lower()
            fam = re.sub(r"[^a-z\-']", "", fam)
            if fam:
                authors.append(fam)

    elif src == "openalex":
        doi_raw = item.get("doi")
        doi = _safe_str(doi_raw).replace("https://doi.org/", "").strip()
        title = _norm_text(_safe_strip(item.get("title")))
        year = _safe_strip(item.get("publication_year"))

        for a in (item.get("authorships") or [])[:10]:
            au = (a or {}).get("author") or {}
            nm = _safe_strip(au.get("display_name"))
            if nm:
                last = nm.split()[-1].lower()
                last = re.sub(r"[^a-z\-']", "", last)
                if last:
                    authors.append(last)

    elif src == "unpaywall":
        doi = _safe_strip(item.get("doi"))
        title = _norm_text(_safe_strip(item.get("title")))
        year = _safe_strip(item.get("year")) or ""
        authors = []

    return doi, title, year, authors, api_score


# ---------- scoring + classification ----------
def _score(
    ref_title: str,
    ref_authors: List[str],
    ref_year: str,
    cand_title: str,
    cand_authors: List[str],
    cand_year: str,
    api_score: int = 0,
) -> Dict[str, Any]:
    ref_title = _norm_text(ref_title)
    cand_title = _norm_text(cand_title)

    title_score = fuzz.token_set_ratio(ref_title, cand_title) if (ref_title and cand_title) else 0

    ref_set = set([a for a in (ref_authors or []) if a])
    cand_set = set([a for a in (cand_authors or []) if a])
    author_overlap = len(ref_set.intersection(cand_set))

    # year mismatch is common (online-first vs issue year)
    year_match = 1 if (ref_year and cand_year and ref_year[:4] == cand_year[:4]) else 0

    score = (title_score * 1.35) + (author_overlap * 26) + (year_match * 8)

    if api_score and api_score > 0:
        score += min(20, int(api_score / 10))

    return {
        "score": int(score),
        "title_score": int(title_score),
        "author_overlap": int(author_overlap),
        "year_match": int(year_match),
    }


def _classify(
    ref_has_authors: bool,
    doi_match: bool,
    title_score: int,
    author_overlap: int,
    year_match: int,
    score: int,
    cand_has_doi: bool,
) -> str:
    """
    Fixes:
    - if reference lacks authors, don't penalize for author_overlap=0
    - don't hard-fail on year mismatch
    - high score + good title should not be not_found
    """
    if doi_match and title_score >= 55:
        return "verified"

    if title_score >= 88 and (author_overlap >= 1 or not ref_has_authors) and (cand_has_doi or score >= 120):
        return "verified"

    if title_score >= 80 and (author_overlap >= 1 or not ref_has_authors) and (cand_has_doi or score >= 105):
        return "likely"

    if title_score >= 70 and (author_overlap >= 1 or year_match == 1 or not ref_has_authors):
        return "needs_review"

    return "not_found"


def _attempt_summary(source: str, doi: str, title: str, year: str, meta: Dict[str, Any], doi_match: bool) -> Dict[str, Any]:
    return {
        "source": source,
        "doi": doi,
        "title": title,
        "year": year,
        "doi_match": bool(doi_match),
        "score": int(meta.get("score") or 0),
        "title_score": int(meta.get("title_score") or 0),
        "author_overlap": int(meta.get("author_overlap") or 0),
        "year_match": int(meta.get("year_match") or 0),
    }


def _pick_best_candidate(
    ref_title: str,
    ref_authors: List[str],
    ref_year: str,
    ref_doi: str,
    candidates: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], bool]:
    best = None
    best_meta: Dict[str, Any] = {"score": -1, "title_score": 0, "author_overlap": 0, "year_match": 0}
    best_doi_match = False

    for cand in candidates:
        cand_doi, cand_title, cand_year, cand_authors, api_score = _candidate_fields(cand)
        meta = _score(ref_title, ref_authors, ref_year, cand_title, cand_authors, cand_year, api_score)

        doi_match = _doi_equal(ref_doi, cand_doi) if ref_doi else False
        meta_score = int(meta["score"] + (35 if doi_match else 0))

        if meta_score > int(best_meta.get("score") or -1):
            best = cand
            best_meta = dict(meta)
            best_meta["score"] = meta_score
            best_doi_match = doi_match

    return best, best_meta, best_doi_match


# ---------- public function ----------
def verify_references_batch(
    references: List[str],
    max_to_check: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
    use_unpaywall: bool = True,
    use_semantic_scholar: bool = False,  # kept for compatibility
) -> List[Dict[str, Any]]:
    refs = [r for r in (references or []) if _safe_strip(r)]
    if not refs:
        return []

    if max_to_check and max_to_check > 0:
        refs = refs[: max_to_check]

    rows: List[Dict[str, Any]] = []

    for ref in refs:
        ref_raw = _safe_strip(ref)
        
        # ====================================================================
        # STEP 1: Convert reference to APA format
        # ====================================================================
        apa_data = _convert_to_apa(ref_raw)
        
        # Extract components from APA conversion
        ref_title = apa_data.get("title", "")
        ref_authors = apa_data.get("authors", [])
        ref_year = apa_data.get("year", "")
        ref_doi = apa_data.get("doi", "")
        ref_has_authors = bool(ref_authors)
        
        # Build query using APA data
        query = _build_apa_query(apa_data)
        author_for_ui = ", ".join(ref_authors) if ref_authors else ""

        row: Dict[str, Any] = {
            "reference": ref_raw,
            "reference_apa": apa_data.get("apa_string", ""),
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
            "query_used": query,
            "attempts": [],
            "error": "",
            "author": author_for_ui,
            "query": query,
        }

        try:
            # ---------- 1) CROSSREF ----------
            crossref_candidates: List[Dict[str, Any]] = []
            if use_crossref:
                if ref_doi:
                    hit = _query_crossref_by_doi(ref_doi)
                    if hit:
                        crossref_candidates.append(hit)
                    time.sleep(max(0.0, float(throttle_s or 0.0)))

                a_query = " ".join(ref_authors[:3]) if ref_authors else ""
                crossref_candidates.extend(_query_crossref(query, a_query))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            best, meta, doi_match = _pick_best_candidate(ref_title, ref_authors, ref_year, ref_doi, crossref_candidates)
            if best is not None:
                cand_doi, cand_title, cand_year, cand_auths, _ = _candidate_fields(best)
                row["attempts"].append(_attempt_summary("crossref", cand_doi, cand_title, cand_year, meta, doi_match))

                status = _classify(
                    ref_has_authors=ref_has_authors,
                    doi_match=doi_match,
                    title_score=int(meta["title_score"]),
                    author_overlap=int(meta["author_overlap"]),
                    year_match=int(meta["year_match"]),
                    score=int(meta["score"]),
                    cand_has_doi=bool(_safe_strip(cand_doi)),
                )
                status = _normalize_verify_status(status)

                if status in {"verified", "likely"}:
                    row["status"] = status
                    row["source"] = "crossref"
                    row["score"] = int(meta["score"])
                    row["doi"] = _safe_strip(cand_doi)
                    row["doi_match"] = bool(doi_match)
                    row["matched_year"] = _safe_strip(cand_year)
                    row["matched_authors"] = ", ".join([a for a in cand_auths if a])
                    row["matched_title"] = _safe_strip(cand_title)
                    row["title_score"] = int(meta["title_score"])
                    row["author_overlap"] = int(meta["author_overlap"])
                    row["year_match"] = int(meta["year_match"])

                    if not row["author"]:
                        row["author"] = row["matched_authors"]
                    rows.append(row)
                    continue

            # ---------- 2) OPENALEX ----------
            openalex_candidates: List[Dict[str, Any]] = []
            if use_openalex:
                crossref_best_doi = _safe_strip(_candidate_fields(best)[0]) if best is not None else ""
                if crossref_best_doi:
                    hit = _query_openalex_by_doi(crossref_best_doi)
                    if hit:
                        openalex_candidates.append(hit)
                    time.sleep(max(0.0, float(throttle_s or 0.0)))

                openalex_candidates.extend(_query_openalex(query))
                time.sleep(max(0.0, float(throttle_s or 0.0)))

            best2, meta2, doi_match2 = _pick_best_candidate(ref_title, ref_authors, ref_year, ref_doi, openalex_candidates)
            if best2 is not None:
                cand_doi2, cand_title2, cand_year2, cand_auths2, _ = _candidate_fields(best2)
                row["attempts"].append(_attempt_summary("openalex", cand_doi2, cand_title2, cand_year2, meta2, doi_match2))

                status2 = _classify(
                    ref_has_authors=ref_has_authors,
                    doi_match=doi_match2,
                    title_score=int(meta2["title_score"]),
                    author_overlap=int(meta2["author_overlap"]),
                    year_match=int(meta2["year_match"]),
                    score=int(meta2["score"]),
                    cand_has_doi=bool(_safe_strip(cand_doi2)),
                )
                status2 = _normalize_verify_status(status2)

                if status2 in {"verified", "likely"}:
                    row["status"] = status2
                    row["source"] = "openalex"
                    row["score"] = int(meta2["score"])
                    row["doi"] = _safe_strip(cand_doi2)
                    row["doi_match"] = bool(doi_match2)
                    row["matched_year"] = _safe_strip(cand_year2)
                    row["matched_authors"] = ", ".join([a for a in cand_auths2 if a])
                    row["matched_title"] = _safe_strip(cand_title2)
                    row["title_score"] = int(meta2["title_score"])
                    row["author_overlap"] = int(meta2["author_overlap"])
                    row["year_match"] = int(meta2["year_match"])

                    if not row["author"]:
                        row["author"] = row["matched_authors"]
                    rows.append(row)
                    continue

            # ---------- 3) UNPAYWALL ----------
            best_doi_any = ""
            if best2 is not None:
                best_doi_any = _safe_strip(_candidate_fields(best2)[0])
            if not best_doi_any and best is not None:
                best_doi_any = _safe_strip(_candidate_fields(best)[0])
            if not best_doi_any:
                best_doi_any = _safe_strip(ref_doi)

            if use_unpaywall and best_doi_any:
                up = _query_unpaywall_by_doi(best_doi_any)
                if up is not None:
                    cand_du, cand_tu, cand_yu, cand_au, _ = _candidate_fields(up)
                    meta_u = _score(ref_title, ref_authors, ref_year, cand_tu, cand_au, cand_yu, 0)
                    doi_match_u = _doi_equal(ref_doi, cand_du) if ref_doi else False
                    meta_u["score"] = int(meta_u["score"] + (35 if doi_match_u else 0))

                    row["attempts"].append(_attempt_summary("unpaywall", cand_du, cand_tu, cand_yu, meta_u, doi_match_u))

                    status_u = _classify(
                        ref_has_authors=ref_has_authors,
                        doi_match=doi_match_u,
                        title_score=int(meta_u["title_score"]),
                        author_overlap=int(meta_u["author_overlap"]),
                        year_match=int(meta_u["year_match"]),
                        score=int(meta_u["score"]),
                        cand_has_doi=bool(_safe_strip(cand_du)),
                    )
                    status_u = _normalize_verify_status(status_u)

                    if status_u in {"verified", "likely"}:
                        row["status"] = status_u
                        row["source"] = "unpaywall"
                        row["score"] = int(meta_u["score"])
                        row["doi"] = _safe_strip(cand_du)
                        row["doi_match"] = bool(doi_match_u)
                        row["matched_year"] = _safe_strip(cand_yu)
                        row["matched_authors"] = ""
                        row["matched_title"] = _safe_strip(cand_tu)
                        row["title_score"] = int(meta_u["title_score"])
                        row["author_overlap"] = int(meta_u["author_overlap"])
                        row["year_match"] = int(meta_u["year_match"])

                        if not row["author"]:
                            row["author"] = row["matched_authors"]
                        rows.append(row)
                        continue

            # ---------- 4) fallback ----------
            if row["attempts"]:
                row["attempts"].sort(key=lambda x: int(x.get("score") or 0), reverse=True)
                top = row["attempts"][0]

                fallback = _classify(
                    ref_has_authors=ref_has_authors,
                    doi_match=bool(top.get("doi_match")),
                    title_score=int(top.get("title_score") or 0),
                    author_overlap=int(top.get("author_overlap") or 0),
                    year_match=int(top.get("year_match") or 0),
                    score=int(top.get("score") or 0),
                    cand_has_doi=bool(_safe_strip(top.get("doi"))),
                )
                row["status"] = _normalize_verify_status(fallback)
                row["source"] = _safe_strip(top.get("source"))
                row["score"] = int(top.get("score") or 0)
                row["doi"] = _safe_strip(top.get("doi"))
                row["matched_year"] = _safe_strip(top.get("year"))
                row["matched_title"] = _safe_strip(top.get("title"))
                row["title_score"] = int(top.get("title_score") or 0)
                row["author_overlap"] = int(top.get("author_overlap") or 0)
                row["year_match"] = int(top.get("year_match") or 0)

                if not row["author"]:
                    row["author"] = row.get("matched_authors") or ""
            else:
                row["status"] = "not_found"

            rows.append(row)

        except Exception as e:
            row["status"] = "offline"
            row["error"] = _safe_str(e)
            rows.append(row)

    for r in rows:
        r["status"] = _normalize_verify_status(r.get("status"))

    return rows
