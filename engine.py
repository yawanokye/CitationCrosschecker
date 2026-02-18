# engine.py
# Citation Crosschecker Engine (FastAPI version)
#
# Styles supported:
# - APA/Harvard (author-year)
# - IEEE (numeric [1])
# - Vancouver (numeric (1)/superscript)
#
# Outputs (export-safe, table-friendly, numbered):
# - missing_in_references_table
# - uncited_references_table
# - intext_to_reference_table
# - reference_to_intext_table
# - online_verification_table (+ meta)
#
# Online verification (NEW, expert-grade):
# - Uses title + first author + year (metadata search), returns DOI when found
# - Optional, and runs AFTER crosschecking (so you can keep it off by default or run it on demand)
# - Crossref and OpenAlex are both supported
# - If DOI is already present in the reference, it will be validated directly first (fast path)

import re
import io
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter

# Optional libs for reading files
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

# Optional online verification
try:
    import requests
    REQUESTS_OK = True
except Exception:
    requests = None
    REQUESTS_OK = False


# ============================
# Constants
# ============================
YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b")

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

BAD_NARRATIVE_PREFIX_WORDS = {
    "traditional", "classical", "analytical", "for", "from", "in", "on", "at", "by",
    "methods", "method", "approach", "approaches", "sample", "size", "power",
    "results", "discussion", "model", "framework", "similarly",

    # discourse markers that often precede citations and get misread as surnames
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

# DOI
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)\b", re.I)


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

def ensure_row_ids(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, r in enumerate(rows or []):
        rr = dict(r) if isinstance(r, dict) else {"value": str(r)}
        rr["row_id"] = i + 1
        out.append(rr)
    return out

def safe_str(x: Any) -> str:
    return "" if x is None else str(x)

def extract_doi(text: str) -> str:
    if not text:
        return ""
    t = text.replace("https://doi.org/", "").replace("http://doi.org/", "")
    m = DOI_RE.search(t)
    if not m:
        return ""
    doi = m.group(1).rstrip(").,;]}")
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

    merged: List[str] = []
    buf = ""

    for ln in raw_lines:
        s = ln.strip()

        # DOI lines attach to previous reference
        is_doi_line = bool(re.match(r"^\s*(doi\s*:\s*)?10\.\d{4,9}/", s, flags=re.I)) or \
                      bool(re.match(r"^\s*https?://doi\.org/10\.\d{4,9}/", s, flags=re.I)) or \
                      bool(re.match(r"^\s*10\.\d{4,9}/", s))

        # APA start
        apa_start = bool(re.match(r"^[A-Z][A-Za-z\-\']+,\s+[A-Z]\.", s))

        # IEEE/Vancouver numbering start
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

    return [m for m in merged if len(m) >= 10]


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

        first_word = norm_token(authors_blob.split()[0]) if authors_blob.split() else ""
        if first_word in BAD_NARRATIVE_PREFIX_WORDS:
            continue

        blob = authors_blob.replace("&", " and ")
        parts = [p.strip() for p in re.split(r"\s+and\s+|,", blob) if p.strip()]
        cand = [p for p in parts if looks_like_surname(p)]
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

    c2r: List[Dict[str, Any]] = []
    for c in cites:
        hits = ref_by_key.get(c.key, [])
        if not hits:
            c2r.append({"in_text": c.raw, "status": "not_found", "matched_reference": ""})
        elif len(hits) == 1:
            c2r.append({"in_text": c.raw, "status": "matched", "matched_reference": hits[0].raw})
        else:
            c2r.append({
                "in_text": c.raw,
                "status": f"ambiguous ({len(hits)})",
                "matched_reference": " || ".join(h.raw[:220] for h in hits),
            })

    cite_group = defaultdict(list)
    for c in cites:
        cite_group[c.key].append(c.raw)

    r2c: List[Dict[str, Any]] = []
    for r in refs:
        cited_by = cite_group.get(r.key, [])
        r2c.append({
            "reference": r.raw,
            "times_cited": int(len(cited_by)),
            "cited_by_joined": " | ".join(cited_by[:60]),
        })

    r2c.sort(key=lambda x: x.get("times_cited", 0), reverse=True)
    return c2r, r2c

def reconcile_numeric(cites: List[InTextCitation], refs: List[ReferenceEntry]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ref_by_key = {r.key: r for r in refs}

    c2r: List[Dict[str, Any]] = []
    for c in cites:
        r = ref_by_key.get(c.key)
        if not r:
            c2r.append({"in_text": c.raw, "status": "not_found", "matched_reference": ""})
        else:
            c2r.append({"in_text": c.raw, "status": "matched", "matched_reference": r.raw})

    cite_group = defaultdict(list)
    for c in cites:
        cite_group[c.key].append(c.raw)

    r2c: List[Dict[str, Any]] = []
    for r in refs:
        cited_by = cite_group.get(r.key, [])
        r2c.append({
            "reference": r.raw,
            "times_cited": int(len(cited_by)),
            "cited_by_joined": " | ".join(cited_by[:60]),
        })

    r2c.sort(key=lambda x: x.get("times_cited", 0), reverse=True)
    return c2r, r2c

def build_missing_uncited_tables(
    cites: List[InTextCitation],
    refs: List[ReferenceEntry],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:

    cite_keys = [c.key for c in cites]
    ref_keys = [r.key for r in refs]

    cite_count_by_raw = Counter([c.raw for c in cites])
    cite_key_by_raw: Dict[str, str] = {}
    for c in cites:
        cite_key_by_raw.setdefault(c.raw, c.key)

    ref_key_set = set(ref_keys)

    missing_rows: List[Dict[str, Any]] = []
    for raw, cnt in cite_count_by_raw.items():
        k = cite_key_by_raw.get(raw, "")
        if k and (k not in ref_key_set):
            missing_rows.append({"citation_in_text": raw, "count_in_text": int(cnt)})

    missing_rows.sort(key=lambda x: (-x["count_in_text"], x["citation_in_text"]))

    cite_key_set = set(cite_keys)

    uncited_rows: List[Dict[str, Any]] = []
    for r in refs:
        if r.key not in cite_key_set:
            uncited_rows.append({"reference": r.raw, "note": ""})

    summary = {
        "in_text_citations_found": int(len(cites)),
        "reference_entries_found": int(len(refs)),
        "missing_in_references": int(len(missing_rows)),
        "uncited_references": int(len(uncited_rows)),
    }

    return ensure_row_ids(missing_rows), ensure_row_ids(uncited_rows), summary


# ============================
# Online verification (metadata search)
# - Uses title + author + year and returns DOI
# - Optional, and runs after crosscheck
# ============================
def _http_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: float = 12.0) -> Tuple[int, Any, Optional[str]]:
    if not REQUESTS_OK:
        return 0, None, "requests_not_installed"
    try:
        r = requests.get(
            url,
            params=params,
            timeout=timeout,
            headers={"User-Agent": "CitationCrosschecker/1.0"},
        )
        status = int(r.status_code)
        try:
            return status, r.json(), None
        except Exception:
            return status, None, "non_json_response"
    except Exception as e:
        return 0, None, str(e)[:180]

def _title_similarity(a: str, b: str) -> float:
    # lightweight similarity without extra deps
    aa = norm_token(a)
    bb = norm_token(b)
    if not aa or not bb:
        return 0.0
    aset = set(aa.split())
    bset = set(bb.split())
    if not aset or not bset:
        return 0.0
    j = len(aset & bset) / max(1, len(aset | bset))
    return float(j)

def parse_reference_metadata(ref_raw: str) -> Dict[str, Any]:
    """
    Heuristic metadata extraction:
    - year: first year-like token
    - first_author: best guess from lead segment
    - title_guess: for APA, tries to capture title after year. For numeric, tries after year.
    - doi_existing: extracted DOI if present
    """
    r = norm_space(ref_raw or "")
    doi = extract_doi(r)

    # year
    ym = YEAR_RE.search(r)
    year = ym.group(1) if ym else ""

    # first author guess
    # try: "Surname, X." at the beginning
    first_author = ""
    m_apa = re.match(r"^\s*([A-Z][A-Za-z\-\']+)\s*,\s*[A-Z]", r)
    if m_apa:
        first_author = m_apa.group(1)
    else:
        # numeric refs: strip leading [12] or "12."
        rr = re.sub(r"^\s*(\[\s*\d+\s*\]|\d{1,4}[\.\)]|\d{1,4})\s+", "", r).strip()
        # take first token that looks like surname
        tok = rr.split(",", 1)[0].strip() if rr else ""
        tok2 = tok.split()[0].strip() if tok else ""
        if tok2 and re.fullmatch(r"[A-Z][A-Za-z\-\']{1,40}", tok2):
            first_author = tok2

    # title guess (best effort)
    title_guess = ""
    if year:
        # APA pattern: "(YEAR). Title." or "YEAR. Title."
        m_after = re.search(rf"\(\s*{re.escape(year)}\s*\)\.?\s*(.+)$", r)
        if not m_after:
            m_after = re.search(rf"\b{re.escape(year)}\b\.?\s*(.+)$", r)
        if m_after:
            tail = m_after.group(1).strip()

            # remove leading punctuation
            tail = tail.lstrip(" .:-–—")

            # take up to next period as title candidate
            # but avoid grabbing journal info if title itself contains periods, still better than nothing
            parts = [p.strip() for p in tail.split(".") if p.strip()]
            if parts:
                title_guess = parts[0]
                # remove bracketed source tags
                title_guess = re.sub(r"\s*\[.*?\]\s*$", "", title_guess).strip()

    # if still empty, try quoted title
    if not title_guess:
        mq = re.search(r"“([^”]{6,200})”|\"([^\"]{6,200})\"", r)
        if mq:
            title_guess = (mq.group(1) or mq.group(2) or "").strip()

    return {
        "first_author": first_author,
        "year": year,
        "title_guess": title_guess,
        "doi_existing": doi,
    }

def crossref_validate_doi(doi: str) -> Dict[str, Any]:
    status, j, err = _http_get(f"https://api.crossref.org/works/{doi}", timeout=12.0)
    if err:
        return {"found": False, "error": err}
    if status != 200 or not isinstance(j, dict):
        return {"found": False, "http_status": status}
    msg = (j.get("message") or {}) if isinstance(j.get("message"), dict) else {}
    title = ""
    if isinstance(msg.get("title"), list) and msg.get("title"):
        title = msg.get("title")[0] or ""
    return {"found": True, "doi": msg.get("DOI", doi), "title": title}

def crossref_search_metadata(title: str, author: str, year: str) -> Dict[str, Any]:
    """
    Uses Crossref /works with query fields and year filters.
    Returns best DOI if match quality is acceptable.
    """
    params: Dict[str, Any] = {"rows": 3}
    if title:
        params["query.title"] = title
    if author:
        params["query.author"] = author

    # strict year filter when possible
    y = year[:4] if year else ""
    if y.isdigit():
        params["filter"] = f"from-pub-date:{y}-01-01,until-pub-date:{y}-12-31"

    status, j, err = _http_get("https://api.crossref.org/works", params=params, timeout=12.0)
    if err:
        return {"found": False, "error": err}
    if status != 200 or not isinstance(j, dict):
        return {"found": False, "http_status": status}

    items = (((j.get("message") or {}).get("items")) or [])
    if not isinstance(items, list) or not items:
        return {"found": False}

    best = None
    best_score = 0.0

    for it in items[:3]:
        if not isinstance(it, dict):
            continue
        it_title = ""
        if isinstance(it.get("title"), list) and it.get("title"):
            it_title = it["title"][0] or ""
        it_doi = safe_str(it.get("DOI", "")).strip()

        score = _title_similarity(title, it_title) if title and it_title else 0.0

        # small boost if author name appears
        if author:
            auth_blob = norm_token(author)
            authors = it.get("author") or []
            if isinstance(authors, list) and authors:
                names = " ".join(
                    f"{a.get('family','')} {a.get('given','')}".strip()
                    for a in authors if isinstance(a, dict)
                )
                if auth_blob and auth_blob.split()[0] in norm_token(names):
                    score += 0.10

        if score > best_score:
            best_score = score
            best = {"doi": it_doi, "title": it_title, "score": round(best_score, 3)}

    # threshold tuned to avoid noisy false positives
    if best and best.get("doi") and best_score >= 0.25:
        return {"found": True, **best}
    return {"found": False, "best_score": round(best_score, 3)}

def openalex_search_metadata(title: str, author: str, year: str) -> Dict[str, Any]:
    """
    OpenAlex search, returns DOI if available.
    """
    params: Dict[str, Any] = {"per-page": 3}
    q = " ".join([x for x in [title, author] if x]).strip()
    if q:
        params["search"] = q

    y = year[:4] if year else ""
    if y.isdigit():
        params["filter"] = f"publication_year:{y}"

    status, j, err = _http_get("https://api.openalex.org/works", params=params, timeout=12.0)
    if err:
        return {"found": False, "error": err}
    if status != 200 or not isinstance(j, dict):
        return {"found": False, "http_status": status}

    results = j.get("results") or []
    if not isinstance(results, list) or not results:
        return {"found": False}

    best = None
    best_score = 0.0

    for w in results[:3]:
        if not isinstance(w, dict):
            continue
        it_title = safe_str(w.get("title", "")).strip()
        score = _title_similarity(title, it_title) if title and it_title else 0.0

        if author:
            auth_blob = norm_token(author).split()[0] if norm_token(author) else ""
            auths = w.get("authorships") or []
            if auth_blob and isinstance(auths, list):
                names = " ".join(
                    safe_str((a.get("author") or {}).get("display_name", ""))
                    for a in auths if isinstance(a, dict)
                )
                if auth_blob in norm_token(names):
                    score += 0.10

        if score > best_score:
            best_score = score
            # OpenAlex DOI can be in "doi" or in "ids"
            doi = safe_str(w.get("doi", "")).replace("https://doi.org/", "")
            ids = w.get("ids") or {}
            if not doi and isinstance(ids, dict):
                doi = safe_str(ids.get("doi", "")).replace("https://doi.org/", "")
            best = {
                "doi": doi,
                "title": it_title,
                "id": safe_str(w.get("id", "")),
                "score": round(best_score, 3),
            }

    if best and best.get("doi") and best_score >= 0.25:
        return {"found": True, **best}
    return {"found": False, "best_score": round(best_score, 3)}

def build_online_verification_table(
    refs: List[ReferenceEntry],
    max_checks: int = 20,
    verify_mode: str = "metadata",   # "metadata" or "doi_only"
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:

    if not REQUESTS_OK:
        return [], {"enabled": False, "reason": "requests not installed"}

    verify_mode = (verify_mode or "metadata").strip().lower()
    out: List[Dict[str, Any]] = []

    eligible = 0
    checked = 0
    doi_present = 0
    doi_returned = 0

    for r in refs or []:
        meta = parse_reference_metadata(r.raw)

        title = meta.get("title_guess", "") or ""
        author = meta.get("first_author", "") or ""
        year = meta.get("year", "") or ""
        doi_existing = meta.get("doi_existing", "") or ""

        # eligibility:
        # - doi_only: requires DOI in text
        # - metadata: needs at least title OR (author+year) to avoid garbage searches
        is_eligible = False
        if verify_mode == "doi_only":
            is_eligible = bool(doi_existing)
        else:
            is_eligible = bool(title) or (bool(author) and bool(year))

        if not is_eligible:
            continue

        eligible += 1
        checked += 1

        row: Dict[str, Any] = {
            "reference": r.raw,
            "title_guess": title,
            "author_guess": author,
            "year_guess": year,
            "doi_extracted": doi_existing,
            "doi_verified": "",
            "source": "",
            "crossref_found": False,
            "openalex_found": False,
            "crossref_score": "",
            "openalex_score": "",
            "note": "",
        }

        # Fast path: DOI present, validate it first
        if doi_existing:
            doi_present += 1
            crv = crossref_validate_doi(doi_existing)
            if crv.get("found"):
                row["doi_verified"] = safe_str(crv.get("doi", doi_existing))
                row["source"] = "crossref:doi"
                row["crossref_found"] = True
                row["note"] = "DOI validated"
                doi_returned += 1
                out.append(row)
                if checked >= max_checks:
                    break
                continue
            else:
                row["note"] = f"DOI present but not validated ({safe_str(crv.get('error') or crv.get('http_status') or '')})"

        # Metadata search
        if verify_mode != "doi_only":
            cr = crossref_search_metadata(title=title, author=author, year=year)
            oa = openalex_search_metadata(title=title, author=author, year=year)

            if cr.get("found"):
                row["crossref_found"] = True
                row["crossref_score"] = safe_str(cr.get("score", ""))
                if not row["doi_verified"] and cr.get("doi"):
                    row["doi_verified"] = safe_str(cr.get("doi"))
                    row["source"] = "crossref:metadata"
            else:
                row["crossref_score"] = safe_str(cr.get("best_score", ""))

            if oa.get("found"):
                row["openalex_found"] = True
                row["openalex_score"] = safe_str(oa.get("score", ""))
                if not row["doi_verified"] and oa.get("doi"):
                    row["doi_verified"] = safe_str(oa.get("doi"))
                    row["source"] = "openalex:metadata"
            else:
                row["openalex_score"] = safe_str(oa.get("best_score", ""))

            if row["doi_verified"]:
                doi_returned += 1
            else:
                if not row["note"]:
                    row["note"] = "No DOI found from metadata search"

        out.append(row)

        if checked >= max_checks:
            break

    out = ensure_row_ids(out)

    meta_out = {
        "enabled": True,
        "mode": verify_mode,
        "eligible_references": int(eligible),
        "checked": int(min(checked, max_checks)),
        "max_checks": int(max_checks),
        "doi_present_in_references": int(doi_present),
        "doi_returned": int(doi_returned),
    }
    return out, meta_out


# ============================
# Public API: run_crosscheck
# ============================
def run_crosscheck(
    file_bytes: bytes,
    filename: str,
    style: str = "apa",                 # "apa" | "ieee" | "vancouver"
    verify_online: bool = False,        # OPTIONAL: off by default (your requirement)
    verify_mode: str = "metadata",      # "metadata" (title+author+year) or "doi_only"
    max_verify: int = 20,               # cap API calls to avoid timeouts
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
        references_raw = []
        ref_msg = "No References heading found."
    else:
        main_text = "\n".join(lines[:idx]).strip()
        ref_msg = f"Found References heading: {lines[idx].strip()}"
        ref_block_lines = [ln for ln in lines[idx + 1:] if ln.strip()]
        references_raw = _merge_reference_lines(ref_block_lines)

    # 3) Parse citations + references
    style = (style or "apa").strip().lower()

    if style == "apa":
        cites = extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]
        c2r, r2c = reconcile_author_year(cites, refs)
    elif style == "ieee":
        cites = extract_ieee_numeric_citations(main_text)
        refs = [parse_reference_numeric(r) for r in references_raw]
        refs = [r for r in refs if r is not None]
        c2r, r2c = reconcile_numeric(cites, refs)
    else:
        cites = extract_vancouver_numeric_citations(main_text)
        refs = [parse_reference_numeric(r) for r in references_raw]
        refs = [r for r in refs if r is not None]
        c2r, r2c = reconcile_numeric(cites, refs)

    # 4) Missing + Uncited tables (clean, numbered)
    missing_table, uncited_table, summary = build_missing_uncited_tables(cites, refs)

    # 5) Reconciliation tables (numbered)
    intext_to_reference_table = ensure_row_ids(c2r)
    reference_to_intext_table = ensure_row_ids(r2c)

    # 6) Online verification (optional, AFTER crosschecking)
    online_verification_table: List[Dict[str, Any]] = []
    online_meta: Dict[str, Any] = {"enabled": False, "reason": "verify_online is False"}

    if verify_online:
        try:
            online_verification_table, online_meta = build_online_verification_table(
                refs=refs,
                max_checks=max_verify,
                verify_mode=verify_mode,
            )
        except Exception as e:
            online_verification_table = []
            online_meta = {"enabled": False, "reason": str(e)[:180]}

    summary["online_verified_rows"] = int(len(online_verification_table))
    summary["online_verification_enabled"] = bool(online_meta.get("enabled", False))
    summary["online_verification_mode"] = safe_str(online_meta.get("mode", ""))

    # 7) Legacy keys for backward compatibility (keep clean strings)
    legacy_uncited = [r.get("reference", "") for r in uncited_table]
    legacy_missing = [{"citation_in_text": r.get("citation_in_text", ""), "count_in_text": r.get("count_in_text", 0)} for r in missing_table]

    return {
        "filename": filename,
        "style": style,
        "reference_detection_message": ref_msg,
        "text_length": int(len(full_text)),
        "main_text_length": int(len(main_text)),
        "references_detected": int(len(references_raw)),

        "summary": summary,

        # Preferred export-safe tables (numbered)
        "missing_in_references_table": missing_table,
        "uncited_references_table": uncited_table,
        "intext_to_reference_table": intext_to_reference_table,
        "reference_to_intext_table": reference_to_intext_table,
        "online_verification_table": online_verification_table,
        "online_verification_meta": online_meta,

        # Legacy keys (older frontend paths)
        "missing_in_references": legacy_missing,
        "uncited_references": legacy_uncited,
        "reconciliation_intext_to_reference": c2r[:5000],
        "reconciliation_reference_to_intext": r2c[:5000],

        # Light samples for debugging
        "sample_intext_citations": [c.__dict__ for c in cites[:120]],
        "sample_references_parsed": [r.__dict__ for r in refs[:120]],
    }

