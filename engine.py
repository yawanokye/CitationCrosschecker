# engine.py
# Citation Crosschecker Engine (FastAPI version)
# Supports: APA/Harvard (author-year), IEEE (numeric [1]), Vancouver (numeric (1)/superscript)
# Outputs: missing in references, uncited references, reconciliation tables, optional online DOI verification
#
# Online verification (optional):
# - Uses title guess + first author + year (when available) to search Crossref and OpenAlex
# - Returns verified DOI if found
# - Runs AFTER local crosscheck to avoid slowing down core parsing

import re
import io
import time
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter

# Optional libs
try:
    from docx import Document
    DOCX_OK = True
except Exception:
    DOCX_OK = False

try:
    import pdfplumber
    PDF_OK = True
except Exception:
    PDF_OK = False

# Optional online verification deps
try:
    import requests
except Exception:
    requests = None

try:
    from rapidfuzz import fuzz
except Exception:
    fuzz = None

try:
    from tenacity import retry, stop_after_attempt, wait_exponential
except Exception:
    retry = None


# ============================
# Constants
# ============================
YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b")
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>\]]+)\b", re.IGNORECASE)

REF_HEADINGS = [
    r"^\s*references?\s*(?:list)?\s*$",
    r"^\s*bibliograph(?:y|ies)\s*$",
    r"^\s*works\s+cited\s*$",
    r"^\s*literature\s+cited\s*$",
]

NONCITE_LEADS = {
    "e.g", "i.e", "see", "cf", "for example", "for instance",
    "chapter", "section", "table", "figure", "eq", "equation", "appendix",
}

# Words that often precede narrative citations and get misread as surnames
BAD_NARRATIVE_PREFIX_WORDS = {
    "traditional", "classical", "analytical", "for", "from", "in", "on", "at", "by",
    "methods", "method", "approach", "approaches", "sample", "size", "power",
    "results", "discussion", "model", "framework",

    "similarly", "however", "nonetheless", "nevertheless", "therefore",
    "thus", "hence", "moreover", "furthermore", "additionally", "also",
    "conversely", "instead", "meanwhile", "specifically", "notably",
    "indeed", "importantly", "overall", "increasingly", "generally",
    "consequently", "accordingly", "alternatively", "likewise",
}

ORG_ALIASES = {
    "who": ["who", "world health organization", "world health organisation"],
    "un": ["un", "united nations", "u.n.", "united nations organisation", "united nations organization"],
    "oecd": ["oecd", "organisation for economic co-operation and development", "organization for economic cooperation and development"],
    "imf": ["imf", "international monetary fund"],
    "world bank": ["world bank", "international bank for reconstruction and development", "ibrd"],
    "unesco": ["unesco", "united nations educational, scientific and cultural organization", "united nations educational scientific and cultural organization"],
    "unicef": ["unicef", "united nations children's fund", "united nations childrens fund"],
}
ORG_ACRONYMS = {k.upper() for k in ["WHO", "UN", "OECD", "IMF", "UNESCO", "UNICEF", "WORLD BANK", "IBRD"]}

DEFAULT_HTTP_TIMEOUT = 8  # seconds


# ============================
# Data classes
# ============================
@dataclass
class InTextCitation:
    style: str                  # "author-year" or "numeric"
    raw: str                    # full raw in-text cite
    key: str                    # matching key
    year: Optional[str] = None
    surnames: Optional[Tuple[str, ...]] = None
    number: Optional[int] = None


@dataclass
class ReferenceEntry:
    raw: str
    key: str
    year: Optional[str] = None
    surnames: Optional[Tuple[str, ...]] = None
    number: Optional[int] = None


# ============================
# Normalisation helpers
# ============================
def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def norm_token(s: str) -> str:
    s = (s or "").replace("’", "'")
    s = _strip_accents(s)
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\-\s'&\.]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def canon_org(name: str) -> str:
    n = norm_token(name)
    for canon, variants in ORG_ALIASES.items():
        for v in variants:
            if n == norm_token(v):
                return canon
    return n

def is_known_org(text: str) -> bool:
    t = (text or "").strip()
    if t.upper() in ORG_ACRONYMS:
        return True
    c = canon_org(t)
    return c in ORG_ALIASES.keys()

def looks_like_surname(tok: str) -> bool:
    if not tok:
        return False
    t = tok.strip()
    if len(t) < 2:
        return False
    if not re.fullmatch(r"[A-Z][A-Za-z\-']{1,40}", t):
        return False
    # block discourse markers even if capitalised (Similarly, However, ...)
    if norm_token(t) in BAD_NARRATIVE_PREFIX_WORDS:
        return False
    return True

def clean_surname(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("&", " and ")
    s = re.sub(r"\bet\s+al\.?\b", "", s, flags=re.I)
    s = re.sub(r"[^A-Za-z\-'\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    parts = s.split()
    return parts[-1] if parts else ""

def key_author_year(first_surname: str, year: str) -> str:
    return f"au_{norm_token(first_surname)}_{(year or '').lower()}"

def key_numeric(n: int) -> str:
    return f"n_{int(n)}"

def is_bare_year_parenthetical(raw_inside: str) -> bool:
    s = norm_space(raw_inside)
    return bool(re.fullmatch(rf"{YEAR}", s))

def split_semicolons(block: str) -> List[str]:
    parts = [p.strip() for p in (block or "").split(";") if p.strip()]
    return parts if parts else [block.strip()]

def extract_doi(text: str) -> str:
    if not text:
        return ""
    m = DOI_RE.search(text)
    if not m:
        return ""
    doi = m.group(1).rstrip(").,;]")
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    doi = doi.replace("doi:", "").strip()
    return doi


# ============================
# File readers
# ============================
def read_docx_paragraphs(file_bytes: bytes) -> List[str]:
    if not DOCX_OK:
        raise RuntimeError("python-docx not installed")
    doc = Document(io.BytesIO(file_bytes))
    return [norm_space(p.text) for p in doc.paragraphs if norm_space(p.text)]

def read_pdf_text(file_bytes: bytes) -> str:
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")
    out = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for p in pdf.pages:
            out.append(p.extract_text() or "")
    return "\n".join(out)


# ============================
# References section detection + splitting
# ============================
def _find_reference_heading(lines: List[str]) -> int:
    for i, line in enumerate(lines):
        s = (line or "").strip()
        if not s:
            continue
        for pat in REF_HEADINGS:
            if re.search(pat, s, flags=re.I):
                return i
    return -1

def _merge_reference_lines(raw_lines: List[str]) -> List[str]:
    raw_lines = [ln.strip() for ln in raw_lines if ln and ln.strip()]
    if not raw_lines:
        return []

    merged = []
    buf = ""

    for ln in raw_lines:
        s = ln.strip()

        is_doi_line = bool(re.match(r"^\s*(doi\s*:\s*)?10\.\d{4,9}/", s, flags=re.I)) or \
                      bool(re.match(r"^\s*https?://doi\.org/10\.\d{4,9}/", s, flags=re.I)) or \
                      bool(re.match(r"^\s*10\.\d{4,9}/", s))

        apa_start = bool(re.match(r"^[A-Z][A-Za-z\-\']+,\s+[A-Z]\.", s))

        numeric_start = bool(re.match(r"^\s*\[\d+\]\s+", s)) or \
                        bool(re.match(r"^\s*\d{1,4}[\.\)]\s+", s)) or \
                        bool(re.match(r"^\s*\d{1,4}\s+", s))

        if is_doi_line:
            buf = (buf + " " + s).strip() if buf else s
            continue

        if apa_start or numeric_start:
            if buf:
                merged.append(buf.strip())
            buf = s
        else:
            buf = (buf + " " + s).strip() if buf else s

    if buf:
        merged.append(buf.strip())

    merged = [m for m in merged if len(m) >= 10]
    return merged


# ============================
# Reference parsing
# ============================
def parse_reference_author_year(ref_raw: str) -> Optional[ReferenceEntry]:
    r = (ref_raw or "").strip()
    if not r:
        return None

    m = re.search(rf"\(\s*({YEAR})\s*\)", r)
    if not m:
        m2 = re.search(rf"\b({YEAR})\b", r)
        if not m2:
            return None
        year = m2.group(1)
        pre = r[: m2.start()].strip()
    else:
        year = m.group(1)
        pre = r[: m.start()].strip()

    if is_known_org(pre) or pre.upper() in ORG_ACRONYMS:
        k = f"org_{canon_org(pre)}_{year.lower()}"
        return ReferenceEntry(raw=r, key=k, year=year, surnames=(pre,), number=None)

    if "," in pre:
        first = pre.split(",")[0].strip()
    else:
        first = pre.split()[0].strip() if pre.split() else ""

    if not first:
        return None

    return ReferenceEntry(raw=r, key=key_author_year(first, year), year=year, surnames=(first,), number=None)

def parse_reference_numeric(ref_raw: str) -> Optional[ReferenceEntry]:
    r = (ref_raw or "").strip()
    if not r:
        return None

    m = re.match(r"^\s*\[\s*(\d+)\s*\]\s*(.+)$", r)
    if m:
        n = int(m.group(1))
        return ReferenceEntry(raw=r, key=key_numeric(n), number=n)

    m = re.match(r"^\s*(\d+)\s*[\.\)]\s*(.+)$", r)
    if m:
        n = int(m.group(1))
        return ReferenceEntry(raw=r, key=key_numeric(n), number=n)

    m = re.match(r"^\s*(\d+)\s+(.+)$", r)
    if m:
        n = int(m.group(1))
        return ReferenceEntry(raw=r, key=key_numeric(n), number=n)

    return None


# ============================
# In-text extraction (APA/Harvard)
# ============================
def extract_author_year_citations(text: str) -> List[InTextCitation]:
    out: List[InTextCitation] = []
    txt = text or ""

    taken_spans: List[Tuple[int, int]] = []

    def _overlaps(span: Tuple[int, int], spans: List[Tuple[int, int]]) -> bool:
        a, b = span
        for s, e in spans:
            if a < e and b > s:
                return True
        return False

    # Parenthetical blocks containing a year
    for m in re.finditer(rf"\(([^()]*\b{YEAR}\b[^()]*)\)", txt):
        inside = m.group(1).strip()
        if is_bare_year_parenthetical(inside):
            continue

        for chunk in split_semicolons(inside):
            c = chunk.strip()
            y_m = YEAR_RE.search(c)
            if not y_m:
                continue
            y = y_m.group(1)

            left = c[: y_m.start()].strip().rstrip(",").strip()
            left_norm = norm_token(left)
            if left_norm in NONCITE_LEADS:
                continue

            if is_known_org(left):
                k = f"org_{canon_org(left)}_{y.lower()}"
                out.append(InTextCitation("author-year", f"({norm_space(c)})", k, year=y, surnames=(left,)))
                continue

            if re.search(r"\bet\s+al\.?\b", left, flags=re.I):
                first = clean_surname(left)
                if looks_like_surname(first):
                    out.append(InTextCitation("author-year", f"({norm_space(c)})", key_author_year(first, y), year=y, surnames=(first,)))
                continue

            left2 = left.replace("&", " and ")
            toks = [t.strip() for t in re.split(r"\s+and\s+|,", left2) if t.strip()]
            cand = [t for t in toks if looks_like_surname(t)]
            cand = [t for t in cand if norm_token(t) not in BAD_NARRATIVE_PREFIX_WORDS]
            if not cand:
                continue

            first = cand[0]
            out.append(InTextCitation("author-year", f"({norm_space(c)})", key_author_year(first, y), year=y, surnames=tuple(cand)))

    # Multi-author narrative
    narr_multi = re.finditer(
        rf"""
        \b
        (?P<authors>
            [A-Z][A-Za-z\-']{{1,40}}
            (?:\s*,\s*[A-Z][A-Za-z\-']{{1,40}})*
            \s*(?:,\s*)?(?:and|&)\s*[A-Z][A-Za-z\-']{{1,40}}
        )
        \s*
        \(\s*(?P<year>{YEAR})\s*\)
        """,
        txt,
        flags=re.VERBOSE,
    )

    for m in narr_multi:
        span = (m.start(), m.end())
        if _overlaps(span, taken_spans):
            continue

        authors_blob = m.group("authors").strip()
        y = m.group("year")

        # Reject if the first token is a discourse marker
        first_token = authors_blob.split()[0] if authors_blob.split() else ""
        if norm_token(first_token.strip(",.")) in BAD_NARRATIVE_PREFIX_WORDS:
            # Try to salvage by dropping the first token if it is a marker like "Similarly,"
            # Example: "Similarly, Adam and Kofi (2020)" => treat "Adam and Kofi"
            if "," in authors_blob:
                after = authors_blob.split(",", 1)[1].strip()
                if after:
                    authors_blob = after
            else:
                continue

        blob = authors_blob.replace("&", " and ")
        parts = [p.strip() for p in re.split(r"\s+and\s+|,", blob) if p.strip()]
        cand = [p for p in parts if looks_like_surname(p)]
        cand = [p for p in cand if norm_token(p) not in BAD_NARRATIVE_PREFIX_WORDS]
        if not cand:
            continue

        first = cand[0]
        out.append(InTextCitation("author-year", m.group(0), key_author_year(first, y), year=y, surnames=tuple(cand)))
        taken_spans.append(span)

    # "et al." narrative
    for m in re.finditer(rf"\b(?P<a>[A-Z][A-Za-z\-']{{1,40}})\s+et\s+al\.\s*\(\s*(?P<y>{YEAR})\s*\)", txt, flags=re.IGNORECASE):
        span = (m.start(), m.end())
        if _overlaps(span, taken_spans):
            continue
        first = m.group("a").strip()
        y = m.group("y")
        if looks_like_surname(first):
            out.append(InTextCitation("author-year", m.group(0), key_author_year(first, y), year=y, surnames=(first,)))
            taken_spans.append(span)

    # Single-author narrative
    for m in re.finditer(rf"\b(?P<author>[A-Z][A-Za-z\-']{{1,40}})\s*\(\s*(?P<year>{YEAR})\s*\)", txt):
        span = (m.start(), m.end())
        if _overlaps(span, taken_spans):
            continue

        au = m.group("author").strip()
        y = m.group("year")

        if norm_token(au) in BAD_NARRATIVE_PREFIX_WORDS:
            continue

        if is_known_org(au):
            k = f"org_{canon_org(au)}_{y.lower()}"
            out.append(InTextCitation("author-year", m.group(0), k, year=y, surnames=(au,)))
        else:
            if looks_like_surname(au):
                out.append(InTextCitation("author-year", m.group(0), key_author_year(au, y), year=y, surnames=(au,)))

    # de-dup by raw
    uniq: List[InTextCitation] = []
    seen = set()
    for c in out:
        if c.raw not in seen:
            uniq.append(c)
            seen.add(c.raw)
    return uniq


# ============================
# Numeric extraction (IEEE + Vancouver)
# ============================
_SUP_DIGITS = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
}

def _sup_to_int(s: str) -> Optional[int]:
    try:
        digits = "".join(_SUP_DIGITS.get(ch, "") for ch in s)
        return int(digits) if digits else None
    except Exception:
        return None

def _expand_numeric_chunks(inside: str) -> List[int]:
    inside = inside.replace("–", "-")
    chunks = [c.strip() for c in inside.split(",") if c.strip()]
    nums: List[int] = []
    for c in chunks:
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", c)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b and (b - a) <= 2000:
                nums.extend(range(a, b + 1))
        else:
            if c.isdigit():
                nums.append(int(c))
    return nums

def extract_ieee_numeric_citations(text: str) -> List[InTextCitation]:
    out: List[InTextCitation] = []
    pat = re.compile(r"\[\s*(\d+(?:\s*[-–]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–]\s*\d+)?)*)\s*\]")
    for m in pat.finditer(text or ""):
        raw = m.group(0)
        inside = m.group(1)
        for n in _expand_numeric_chunks(inside):
            out.append(InTextCitation("numeric", raw, key_numeric(n), number=n))
    return out

def extract_vancouver_numeric_citations(text: str) -> List[InTextCitation]:
    out: List[InTextCitation] = []

    paren = re.compile(r"\(\s*(\d+(?:\s*[-–]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–]\s*\d+)?)*)\s*\)")
    for m in paren.finditer(text or ""):
        inside = m.group(1)
        if re.fullmatch(rf"{YEAR}", inside.strip()):
            continue
        for n in _expand_numeric_chunks(inside):
            out.append(InTextCitation("numeric", m.group(0), key_numeric(n), number=n))

    out.extend(extract_ieee_numeric_citations(text))

    sup_run = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+")
    for m in sup_run.finditer(text or ""):
        n = _sup_to_int(m.group(0))
        if n is not None:
            out.append(InTextCitation("numeric", m.group(0), key_numeric(n), number=n))

    return out


# ============================
# Reconciliation + Missing/Uncited
# ============================
def reconcile_author_year(cites: List[InTextCitation], refs: List[ReferenceEntry]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ref_by_key = defaultdict(list)
    for r in refs:
        ref_by_key[r.key].append(r)

    c2r = []
    for c in cites:
        hits = ref_by_key.get(c.key, [])
        if not hits:
            c2r.append({"in_text": c.raw, "status": "not_found", "matched_reference": ""})
        elif len(hits) == 1:
            c2r.append({"in_text": c.raw, "status": "matched", "matched_reference": hits[0].raw})
        else:
            c2r.append({"in_text": c.raw, "status": f"ambiguous ({len(hits)})", "matched_reference": " || ".join(h.raw[:220] for h in hits)})

    cite_group = defaultdict(list)
    for c in cites:
        cite_group[c.key].append(c.raw)

    r2c = []
    for r in refs:
        cited_by = cite_group.get(r.key, [])
        r2c.append({"reference": r.raw, "times_cited": int(len(cited_by)), "cited_by": cited_by})

    r2c.sort(key=lambda x: x.get("times_cited", 0), reverse=True)
    return c2r, r2c

def reconcile_numeric(cites: List[InTextCitation], refs: List[ReferenceEntry]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ref_by_key = {r.key: r for r in refs}

    c2r = []
    for c in cites:
        r = ref_by_key.get(c.key)
        if not r:
            c2r.append({"in_text": c.raw, "status": "not_found", "matched_reference": ""})
        else:
            c2r.append({"in_text": c.raw, "status": "matched", "matched_reference": r.raw})

    cite_group = defaultdict(list)
    for c in cites:
        cite_group[c.key].append(c.raw)

    r2c = []
    for r in refs:
        cited_by = cite_group.get(r.key, [])
        r2c.append({"reference": r.raw, "times_cited": int(len(cited_by)), "cited_by": cited_by})

    r2c.sort(key=lambda x: x.get("times_cited", 0), reverse=True)
    return c2r, r2c

def build_missing_uncited(
    cites: List[InTextCitation],
    refs: List[ReferenceEntry],
    c2r_table: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """
    missing: citations that appear in text but no matching reference entry (based on c2r not_found)
    uncited: reference entries that never appear as in-text citations (based on keys)
    """
    cite_keys = [c.key for c in cites]
    cite_key_set = set(cite_keys)

    # Missing from references (based on reconciliation status not_found, aggregated)
    missing_counter = Counter()
    for row in c2r_table:
        if (row.get("status") or "").lower() == "not_found":
            missing_counter[row.get("in_text", "")] += 1

    missing = [{"citation_in_text": k, "count_in_text": int(v)} for k, v in missing_counter.items() if k]
    missing.sort(key=lambda x: (-x["count_in_text"], x["citation_in_text"]))

    # Uncited references (key not in cite set)
    uncited = []
    for r in refs:
        if r.key not in cite_key_set:
            # return clean text only
            uncited.append({"reference": r.raw, "note": ""})

    summary = {
        "in_text_citations_found": int(len(cites)),
        "reference_entries_found": int(len(refs)),
        "missing_in_references": int(len(missing)),
        "uncited_references": int(len(uncited)),
    }
    return missing, uncited, summary


# ============================
# Online verification
# ============================
def _http_ok() -> bool:
    return requests is not None

def _fuzzy_ok() -> bool:
    return fuzz is not None

def _safe_fuzz_ratio(a: str, b: str) -> int:
    if not a or not b:
        return 0
    if _fuzzy_ok():
        return int(fuzz.token_set_ratio(a, b))
    # fallback: crude
    a2 = set(norm_token(a).split())
    b2 = set(norm_token(b).split())
    if not a2 or not b2:
        return 0
    return int(100 * len(a2 & b2) / max(1, len(a2 | b2)))

def _guess_title_author_year(reference: str) -> Tuple[str, str, str]:
    """
    Heuristic extraction.
    - year: first year-like token
    - author: first chunk before year
    - title: chunk after year that looks like a title (before next period)
    """
    ref = norm_space(reference)
    if not ref:
        return "", "", ""

    y = ""
    ym = YEAR_RE.search(ref)
    if ym:
        y = ym.group(1)

    author = ""
    title = ""

    if ym:
        before = ref[:ym.start()].strip().rstrip(".,;")
        after = ref[ym.end():].strip()
        # remove surrounding parentheses if present
        before = before.rstrip("() ").strip()
        author = before

        # title guess: up to next period
        # remove leading punctuation
        after = after.lstrip(").,;: ").strip()
        if after:
            title = after.split(".")[0].strip()
    else:
        # no year found: use first period split
        parts = ref.split(".")
        if len(parts) >= 2:
            author = parts[0].strip()
            title = parts[1].strip()

    # normalize author: pick first surname token
    # try "Surname," pattern
    if "," in author:
        first = author.split(",")[0].strip()
    else:
        first = author.split()[0].strip() if author.split() else ""
    author_first = first

    return title, author_first, y

def _crossref_headers() -> Dict[str, str]:
    return {"User-Agent": "CitationCrosschecker/1.0 (mailto:admin@example.com)"}

def _openalex_headers() -> Dict[str, str]:
    return {"User-Agent": "CitationCrosschecker/1.0"}

def _tenacity_wrap(fn):
    if retry is None:
        return fn
    return retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=0.7, min=0.7, max=2.5))(fn)

@_tenacity_wrap
def _crossref_lookup_by_doi(doi: str) -> Dict[str, Any]:
    if not _http_ok():
        return {}
    url = f"https://api.crossref.org/works/{doi}"
    r = requests.get(url, headers=_crossref_headers(), timeout=DEFAULT_HTTP_TIMEOUT)
    if r.status_code != 200:
        return {}
    return r.json() or {}

@_tenacity_wrap
def _openalex_lookup_by_doi(doi: str) -> Dict[str, Any]:
    if not _http_ok():
        return {}
    # OpenAlex uses doi: prefix
    url = f"https://api.openalex.org/works/doi:{doi}"
    r = requests.get(url, headers=_openalex_headers(), timeout=DEFAULT_HTTP_TIMEOUT)
    if r.status_code != 200:
        return {}
    return r.json() or {}

@_tenacity_wrap
def _crossref_search(title: str, author: str, year: str) -> Dict[str, Any]:
    if not _http_ok():
        return {}
    q = title or ""
    url = "https://api.crossref.org/works"
    params = {"query.bibliographic": q, "rows": 3}
    r = requests.get(url, params=params, headers=_crossref_headers(), timeout=DEFAULT_HTTP_TIMEOUT)
    if r.status_code != 200:
        return {}
    return r.json() or {}

@_tenacity_wrap
def _openalex_search(title: str) -> Dict[str, Any]:
    if not _http_ok():
        return {}
    url = "https://api.openalex.org/works"
    params = {"search": title or "", "per-page": 3}
    r = requests.get(url, params=params, headers=_openalex_headers(), timeout=DEFAULT_HTTP_TIMEOUT)
    if r.status_code != 200:
        return {}
    return r.json() or {}

def _pick_best_crossref_item(payload: Dict[str, Any], title: str, author: str, year: str) -> Tuple[str, str]:
    """
    Returns (doi, note)
    """
    try:
        items = (payload.get("message") or {}).get("items") or []
    except Exception:
        items = []
    if not items:
        return "", "Crossref: no results"

    best_doi = ""
    best_score = -1
    best_note = ""

    for it in items:
        doi = (it.get("DOI") or "").strip()
        it_title = ""
        tlist = it.get("title") or []
        if isinstance(tlist, list) and tlist:
            it_title = tlist[0] or ""
        it_year = ""
        issued = it.get("issued") or {}
        parts = (issued.get("date-parts") or [[]])
        if parts and parts[0]:
            it_year = str(parts[0][0])

        it_author = ""
        auth = it.get("author") or []
        if isinstance(auth, list) and auth:
            it_author = (auth[0].get("family") or "").strip()

        score = _safe_fuzz_ratio(it_title, title) + _safe_fuzz_ratio(it_author, author)
        if year and it_year:
            if str(it_year) == str(year):
                score += 15
            else:
                score -= 10

        if score > best_score and doi:
            best_score = score
            best_doi = doi
            best_note = f"Crossref match score={score}, year={it_year or '-'}"

    if not best_doi:
        return "", "Crossref: no DOI in top results"
    return best_doi, best_note

def _pick_best_openalex_item(payload: Dict[str, Any], title: str, author: str, year: str) -> Tuple[str, str]:
    """
    Returns (doi, note)
    """
    results = payload.get("results") or []
    if not results:
        return "", "OpenAlex: no results"

    best_doi = ""
    best_score = -1
    best_note = ""

    for it in results:
        it_title = (it.get("title") or "").strip()
        it_year = str(it.get("publication_year") or "")
        it_author = ""
        auths = it.get("authorships") or []
        if isinstance(auths, list) and auths:
            a0 = auths[0].get("author") or {}
            it_author = (a0.get("display_name") or "").split()[-1].strip()

        doi = (it.get("doi") or "").strip()
        doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "").strip()

        score = _safe_fuzz_ratio(it_title, title) + _safe_fuzz_ratio(it_author, author)
        if year and it_year:
            if str(it_year) == str(year):
                score += 15
            else:
                score -= 10

        if score > best_score and doi:
            best_score = score
            best_doi = doi
            best_note = f"OpenAlex match score={score}, year={it_year or '-'}"

    if not best_doi:
        return "", "OpenAlex: no DOI in top results"
    return best_doi, best_note

def run_online_verification(
    references: List[str],
    mode: str = "metadata",          # "metadata" | "doi_only"
    max_verify: int = 20,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Produces a table for the dashboard and exports.
    Each row includes doi_extracted and doi_verified (if found/validated).
    """
    table: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        "max_verify": int(max_verify),
        "http_available": _http_ok(),
        "fuzzy_available": _fuzzy_ok(),
    }

    if not references:
        meta["note"] = "No references to verify"
        return table, meta

    if not _http_ok():
        meta["note"] = "requests not installed, online verification disabled"
        return table, meta

    max_verify = max(0, int(max_verify or 0))
    refs_to_check = references[:max_verify] if max_verify else []

    for ref in refs_to_check:
        ref_txt = norm_space(ref)
        doi_extracted = extract_doi(ref_txt)

        title_guess, author_guess, year_guess = _guess_title_author_year(ref_txt)

        doi_verified = ""
        source = ""
        note = ""

        try:
            if mode == "doi_only":
                if not doi_extracted:
                    note = "No DOI in reference"
                else:
                    # Validate DOI via Crossref first, fallback OpenAlex
                    cr = _crossref_lookup_by_doi(doi_extracted)
                    if cr:
                        doi_verified = doi_extracted
                        source = "Crossref"
                        note = "DOI validated"
                    else:
                        oa = _openalex_lookup_by_doi(doi_extracted)
                        if oa:
                            doi_verified = doi_extracted
                            source = "OpenAlex"
                            note = "DOI validated"
                        else:
                            note = "DOI not found in Crossref/OpenAlex"
            else:
                # metadata mode
                if doi_extracted:
                    # quick validate DOI first
                    cr = _crossref_lookup_by_doi(doi_extracted)
                    if cr:
                        doi_verified = doi_extracted
                        source = "Crossref"
                        note = "DOI validated (from reference)"
                    else:
                        oa = _openalex_lookup_by_doi(doi_extracted)
                        if oa:
                            doi_verified = doi_extracted
                            source = "OpenAlex"
                            note = "DOI validated (from reference)"

                if not doi_verified:
                    # Crossref search using title guess
                    if title_guess:
                        crs = _crossref_search(title_guess, author_guess, year_guess)
                        doi, n = _pick_best_crossref_item(crs, title_guess, author_guess, year_guess)
                        if doi:
                            doi_verified = doi
                            source = "Crossref"
                            note = n
                    # OpenAlex search fallback
                    if not doi_verified and title_guess:
                        oas = _openalex_search(title_guess)
                        doi, n = _pick_best_openalex_item(oas, title_guess, author_guess, year_guess)
                        if doi:
                            doi_verified = doi
                            source = "OpenAlex"
                            note = n

                if not doi_verified and not note:
                    note = "No match found"

        except Exception as e:
            note = f"Verification error: {str(e)[:120]}"

        table.append({
            "reference": ref_txt,
            "title_guess": title_guess,
            "author_guess": author_guess,
            "year_guess": year_guess,
            "doi_extracted": doi_extracted,
            "doi_verified": doi_verified,
            "source": source,
            "note": note,
        })

    meta["rows_returned"] = len(table)
    meta["verified_count"] = sum(1 for r in table if r.get("doi_verified"))
    return table, meta


# ============================
# Public API: run_crosscheck
# ============================
def run_crosscheck(
    file_bytes: bytes,
    filename: str,
    style: str = "apa",                 # "apa" | "ieee" | "vancouver"
    verify_online: bool = False,
    verify_mode: str = "metadata",      # "metadata" | "doi_only"
    max_verify: int = 20,
) -> dict:

    name = (filename or "").lower()

    # 1) Read file
    if name.endswith(".docx"):
        paras = read_docx_paragraphs(file_bytes)
        full_text = "\n".join(paras)
    elif name.endswith(".pdf"):
        full_text = read_pdf_text(file_bytes)
    else:
        return {"error": "Upload a DOCX or PDF"}

    # 2) Split refs
    lines = full_text.splitlines()
    idx = _find_reference_heading(lines)

    if idx == -1:
        main_text = full_text
        references = []
        ref_msg = "No References heading found."
    else:
        main_text = "\n".join(lines[:idx]).strip()
        ref_msg = f"Found References heading: {lines[idx].strip()}"
        ref_block_lines = [ln for ln in lines[idx + 1:] if ln.strip()]
        references = _merge_reference_lines(ref_block_lines)

    # 3) Parse citations + references
    style = (style or "apa").strip().lower()

    if style == "apa":
        cites = extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references]
        refs = [r for r in refs if r is not None]
        c2r, r2c = reconcile_author_year(cites, refs)
    elif style == "ieee":
        cites = extract_ieee_numeric_citations(main_text)
        refs = [parse_reference_numeric(r) for r in references]
        refs = [r for r in refs if r is not None]
        c2r, r2c = reconcile_numeric(cites, refs)
    else:
        cites = extract_vancouver_numeric_citations(main_text)
        refs = [parse_reference_numeric(r) for r in references]
        refs = [r for r in refs if r is not None]
        c2r, r2c = reconcile_numeric(cites, refs)

    missing, uncited, summary = build_missing_uncited(cites, refs, c2r)

    # 4) Build clean, numbered tables for UI + export
    intext_to_reference_table = []
    for i, row in enumerate(c2r, start=1):
        intext_to_reference_table.append({
            "no": i,
            "in_text": row.get("in_text", ""),
            "status": row.get("status", ""),
            "matched_reference": row.get("matched_reference", ""),
        })

    missing_table = []
    for i, row in enumerate(missing, start=1):
        missing_table.append({
            "no": i,
            "citation_in_text": row.get("citation_in_text", ""),
            "count_in_text": row.get("count_in_text", ""),
        })

    uncited_table = []
    for i, row in enumerate(uncited, start=1):
        # ensure no "reference_full" key leaks
        txt = row.get("reference", "") if isinstance(row, dict) else str(row)
        uncited_table.append({
            "no": i,
            "reference": txt,
            "note": row.get("note", "") if isinstance(row, dict) else "",
        })

    # 5) Optional online verification (after local crosscheck)
    online_table = []
    online_meta = {"enabled": False}
    if verify_online:
        t0 = time.time()
        online_table, online_meta = run_online_verification(
            references=[r.raw for r in refs],
            mode=(verify_mode or "metadata").strip().lower(),
            max_verify=max_verify,
        )
        online_meta["elapsed_seconds"] = round(time.time() - t0, 3)
        summary["online_verification_enabled"] = True
        summary["online_verification_mode"] = (verify_mode or "metadata").strip().lower()
        summary["online_verified_rows"] = int(online_meta.get("verified_count", 0) or 0)
    else:
        summary["online_verification_enabled"] = False
        summary["online_verification_mode"] = ""
        summary["online_verified_rows"] = 0

    # 6) Return JSON
    return {
        "filename": filename,
        "style": style,
        "reference_detection_message": ref_msg,
        "text_length": len(full_text),
        "main_text_length": len(main_text),
        "references_detected": len(references),

        "summary": summary,

        # legacy keys (kept for compatibility)
        "missing_in_references": missing,
        "uncited_references": [u.get("reference", "") for u in uncited_table],

        "reconciliation_intext_to_reference": c2r[:5000],
        "reconciliation_reference_to_intext": r2c[:5000],

        # NEW: clean tables for UI + exports
        "missing_in_references_table": missing_table,
        "uncited_references_table": uncited_table,
        "intext_to_reference_table": intext_to_reference_table,

        # Online verification
        "online_verification_table": online_table,
        "online_verification_meta": online_meta,

        # Samples (debug)
        "sample_intext_citations": [c.__dict__ for c in cites[:120]],
        "sample_references_parsed": [r.__dict__ for r in refs[:120]],
    }
