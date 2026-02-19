# engine.py
# Citation Crosschecker Engine (FastAPI)
# Supports: APA/Harvard (author-year), IEEE (numeric [1]), Vancouver (numeric (1)/superscript)
# Adds: Optional online verification (Crossref + OpenAlex) with STRICT matching:
#   - Exact year match (no +/-1)
#   - Very high title similarity (default >= 95)
#   - Multiple-author check when possible (>=2 surnames overlap if reference has >=2 surnames)
#
# Output is clean (no {"reference_full": ...} wrappers), table-friendly, and stable for exports.

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

# Online verify libs (already in your requirements)
import requests
from rapidfuzz import fuzz
from tenacity import retry, stop_after_attempt, wait_exponential


# ============================
# Constants
# ============================
YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b", re.I)

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

# Words that sometimes appear before citations and must NEVER be treated as surnames
BAD_NARRATIVE_PREFIX_WORDS = {
    # discourse markers
    "similarly", "however", "nonetheless", "nevertheless", "therefore", "thus", "moreover",
    "furthermore", "consequently", "hence", "also", "instead", "still", "meanwhile",
    # common academic words (avoid false author detection)
    "traditional", "classical", "analytical", "for", "from", "in", "on", "at", "by",
    "methods", "method", "approach", "approaches", "sample", "size", "power",
    "results", "discussion", "model", "framework", "study", "studies",
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

        is_doi_line = (
            bool(re.match(r"^\s*(doi\s*:\s*)?10\.\d{4,9}/", s, flags=re.I))
            or bool(re.match(r"^\s*https?://doi\.org/10\.\d{4,9}/", s, flags=re.I))
            or bool(re.match(r"^\s*10\.\d{4,9}/", s))
        )

        apa_start = bool(re.match(r"^[A-Z][A-Za-z\-\']+,\s+[A-Z]\.", s))

        numeric_start = (
            bool(re.match(r"^\s*\[\d+\]\s+", s))
            or bool(re.match(r"^\s*\d{1,4}[\.\)]\s+", s))
            or bool(re.match(r"^\s*\d{1,4}\s+", s))
        )

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
        m2 = YEAR_RE.search(r)
        if not m2:
            return None
        year = m2.group(1)
        pre = r[: m2.start()].strip()
    else:
        year = m.group(1)
        pre = r[: m.start()].strip()

    # org
    if is_known_org(pre) or pre.upper() in ORG_ACRONYMS:
        org = pre.strip()
        k = f"org_{canon_org(org)}_{year.lower()}"
        return ReferenceEntry(raw=r, key=k, year=year, surnames=(org,), number=None)

    # surname heuristic
    if "," in pre:
        first = pre.split(",")[0].strip()
    else:
        first = pre.split()[0].strip() if pre.split() else ""

    if not first:
        return None

    if norm_token(first) in BAD_NARRATIVE_PREFIX_WORDS:
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
            if left_norm in BAD_NARRATIVE_PREFIX_WORDS:
                continue

            # org parenthetical
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

    # Multi-author narrative (Authors (YEAR))
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

        first_token = authors_blob.split()[0] if authors_blob.split() else ""
        if norm_token(first_token) in BAD_NARRATIVE_PREFIX_WORDS:
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

    # Single-author narrative (Author (YEAR))
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
            c2r.append({"in_text": c.raw, "status": f"ambiguous ({len(hits)})", "matched_reference": " || ".join(h.raw[:220] for h in hits)})

    cite_group = defaultdict(list)
    for c in cites:
        cite_group[c.key].append(c.raw)

    r2c: List[Dict[str, Any]] = []
    for r in refs:
        cited_by = cite_group.get(r.key, [])
        r2c.append({"reference": r.raw, "times_cited": int(len(cited_by)), "cited_by": cited_by})

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
        r2c.append({"reference": r.raw, "times_cited": int(len(cited_by)), "cited_by": cited_by})

    r2c.sort(key=lambda x: x.get("times_cited", 0), reverse=True)
    return c2r, r2c

def build_missing_uncited(cites: List[InTextCitation], refs: List[ReferenceEntry]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    cite_keys = [c.key for c in cites]
    ref_keys = [r.key for r in refs]

    cite_count_by_raw = Counter([c.raw for c in cites])
    cite_key_by_raw: Dict[str, str] = {}
    for c in cites:
        cite_key_by_raw.setdefault(c.raw, c.key)

    ref_key_set = set(ref_keys)

    missing: List[Dict[str, Any]] = []
    for raw, cnt in cite_count_by_raw.items():
        k = cite_key_by_raw.get(raw, "")
        if k and (k not in ref_key_set):
            missing.append({"citation_in_text": raw, "count_in_text": int(cnt)})

    missing.sort(key=lambda x: (-x["count_in_text"], x["citation_in_text"]))

    cite_key_set = set(cite_keys)
    uncited: List[Dict[str, Any]] = []
    for r in refs:
        if r.key not in cite_key_set:
            uncited.append({"reference": r.raw, "note": ""})

    summary = {
        "in_text_citations_found": int(len(cites)),
        "reference_entries_found": int(len(refs)),
        "missing_in_references": int(len(missing)),
        "uncited_references": int(len(uncited)),
    }
    return missing, uncited, summary


# ============================
# Online verification (STRICT)
# ============================
CROSSREF_API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "citation-crosschecker/fastapi (contact: admin)"})

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=6))
def _get_json(url: str, params: dict) -> dict:
    r = SESSION.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def _as_int_year(y: Optional[str]) -> Optional[int]:
    if not y:
        return None
    m = re.search(r"(16|17|18|19|20)\d{2}", str(y))
    return int(m.group(0)) if m else None

def extract_doi(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"https?://doi\.org/(10\.\d{4,9}/[^\s<>\"]+)", text, flags=re.I)
    if m:
        doi = m.group(1)
    else:
        m2 = re.search(r"(10\.\d{4,9}/[^\s<>\"]+)", text, flags=re.I)
        if not m2:
            return None
        doi = m2.group(1)
    doi = doi.strip().strip(").,;:]}>\"'")
    return doi if doi.lower().startswith("10.") else None

def _extract_year_from_ref(ref: str) -> Optional[int]:
    m = YEAR_RE.search(ref or "")
    return _as_int_year(m.group(1)) if m else None

def _extract_surnames_from_ref(ref: str, max_names: int = 6) -> List[str]:
    """
    Heuristic: take the author part up to the year, then parse surnames.
    Works well for "Surname, I., Surname, I., & Surname, I. (2020)...."
    """
    r = ref or ""
    # author block likely before "(YEAR)" or before YEAR occurrence
    cut = None
    m = re.search(rf"\(\s*{YEAR}\s*\)", r)
    if m:
        cut = m.start()
    else:
        m2 = YEAR_RE.search(r)
        if m2:
            cut = m2.start()

    author_blob = r[:cut].strip() if cut is not None else r[:220].strip()

    # remove leading numbering
    author_blob = re.sub(r"^\s*\[\s*\d+\s*\]\s*", "", author_blob)
    author_blob = re.sub(r"^\s*\d{1,4}[\.\)]\s*", "", author_blob)

    # split by "&", "and", commas but keep "Surname," pattern
    # surnames often appear as tokens before commas
    candidates = []
    parts = [p.strip() for p in author_blob.split(",") if p.strip()]
    for p in parts:
        # surname is first token of p (often surname itself)
        tok = p.split()[0].strip()
        if looks_like_surname(tok) and norm_token(tok) not in BAD_NARRATIVE_PREFIX_WORDS:
            candidates.append(tok)

    # de-dup preserve order
    seen = set()
    out = []
    for c in candidates:
        k = norm_token(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
        if len(out) >= max_names:
            break
    return out

def _guess_title_from_ref(ref: str) -> str:
    """
    Heuristic: remove author block + year + DOI/URLs then take first sentence chunk.
    """
    r = norm_space(ref or "")
    r = re.sub(r"https?://doi\.org/\S+", " ", r, flags=re.I)
    r = re.sub(r"\b10\.\d{4,9}/\S+", " ", r, flags=re.I)
    r = re.sub(rf"\(\s*{YEAR}\s*\)", " ", r, flags=re.I)
    r = re.sub(rf"\b{YEAR}\b", " ", r, flags=re.I)
    r = norm_space(r)

    # drop leading author blob (up to first period)
    r = re.sub(r"^[^\.]{1,260}\.\s*", "", r)
    r = norm_space(r)

    # title often ends at next period
    title = r.split(".")[0].strip()
    # keep it reasonable
    words = title.split()
    return " ".join(words[:30]).strip()

def _crossref_items(query: str, rows: int = 8) -> List[dict]:
    try:
        data = _get_json(CROSSREF_API, params={"query.bibliographic": query, "rows": rows})
        return data.get("message", {}).get("items", []) or []
    except Exception:
        return []

def _openalex_items(query: str, per_page: int = 8) -> List[dict]:
    try:
        data = _get_json(OPENALEX_API, params={"search": query, "per-page": per_page})
        return data.get("results", []) or []
    except Exception:
        return []

def _crossref_year(it: dict) -> Optional[int]:
    for key in ["issued", "published-print", "published-online", "created"]:
        dp = (it.get(key, {}) or {}).get("date-parts", [])
        if dp and dp[0]:
            try:
                return int(dp[0][0])
            except Exception:
                pass
    return None

def _crossref_title(it: dict) -> str:
    t = it.get("title") or []
    return (t[0] if t else "") or ""

def _crossref_surnames(it: dict, max_n: int = 8) -> List[str]:
    authors = it.get("author") or []
    out = []
    for a in authors[:max_n]:
        fam = (a.get("family") or "").strip()
        if fam:
            out.append(fam)
    return out

def _openalex_year(it: dict) -> Optional[int]:
    y = it.get("publication_year")
    try:
        return int(y) if y else None
    except Exception:
        return None

def _openalex_title(it: dict) -> str:
    return (it.get("title") or "") or ""

def _openalex_surnames(it: dict, max_n: int = 8) -> List[str]:
    authorships = it.get("authorships") or []
    out = []
    for a in authorships[:max_n]:
        dn = ((a.get("author") or {}).get("display_name") or "").strip()
        if dn:
            out.append(dn.split()[-1])
    return out

def _openalex_doi(it: dict) -> str:
    ids = it.get("ids") or {}
    d = ids.get("doi") or ""
    if d:
        d = d.replace("https://doi.org/", "").strip()
        d = d.strip(").,;:]}>\"'")
    return d

def _author_overlap_ok(ref_surnames: List[str], cand_surnames: List[str]) -> bool:
    """
    STRICT: if ref has >=2 surnames, require at least 2 overlaps.
    If ref has 1 surname, require at least 1 overlap.
    """
    if not ref_surnames or not cand_surnames:
        return False
    ref_set = {norm_token(x) for x in ref_surnames}
    cand_set = {norm_token(x) for x in cand_surnames}
    overlap = len(ref_set.intersection(cand_set))
    need = 2 if len(ref_surnames) >= 2 else 1
    return overlap >= need

def verify_one_reference_strict(
    reference_text: str,
    use_crossref: bool = True,
    use_openalex: bool = True,
    throttle_s: float = 0.25,
    title_score_min: int = 95,
) -> Dict[str, Any]:
    """
    Returns:
      status: verified | not_found | needs_review | offline
      source: crossref_doi | crossref | openalex
      doi, matched_title, matched_year, matched_authors, score, query_used, error
    """
    try:
        time.sleep(max(0.0, float(throttle_s or 0.0)))

        ref = reference_text or ""
        doi = extract_doi(ref)
        y_ref = _extract_year_from_ref(ref)
        surnames_ref = _extract_surnames_from_ref(ref, max_names=6)
        title_ref = _guess_title_from_ref(ref)

        # If DOI is present, treat as strongest signal (Crossref DOI endpoint)
        if doi and use_crossref:
            try:
                data = _get_json(f"{CROSSREF_API}/{doi}", params={})
                msg = data.get("message") or {}
                cand_year = _crossref_year(msg)
                cand_title = _crossref_title(msg)
                cand_surnames = _crossref_surnames(msg)

                score = fuzz.WRatio(title_ref, cand_title) if (title_ref and cand_title) else 0
                year_ok = (y_ref is not None and cand_year == y_ref) if y_ref is not None else True
                auth_ok = _author_overlap_ok(surnames_ref, cand_surnames) if surnames_ref else True

                if year_ok and auth_ok and score >= title_score_min:
                    return {
                        "reference": ref,
                        "status": "verified",
                        "source": "crossref_doi",
                        "score": int(score),
                        "doi": doi,
                        "matched_year": str(cand_year or ""),
                        "matched_title": (cand_title or "")[:220],
                        "matched_authors": ", ".join(cand_surnames[:8]),
                        "query_used": "doi_lookup",
                        "error": "",
                    }

                # DOI exists but strict check fails
                return {
                    "reference": ref,
                    "status": "needs_review",
                    "source": "crossref_doi",
                    "score": int(score),
                    "doi": doi,
                    "matched_year": str(cand_year or ""),
                    "matched_title": (cand_title or "")[:220],
                    "matched_authors": ", ".join(cand_surnames[:8]),
                    "query_used": "doi_lookup",
                    "error": "",
                }
            except Exception as e:
                # fall through to search mode
                pass

        # Build strict query using MULTIPLE authors + year + exact-ish title
        author_query = " ".join(surnames_ref[:3])  # use up to 3 surnames
        year_query = str(y_ref) if y_ref is not None else ""
        query = norm_space(" ".join([author_query, year_query, title_ref]).strip())
        if not query:
            query = (ref[:220] if ref else "")

        best = {
            "reference": ref,
            "status": "not_found",
            "source": "",
            "score": 0,
            "doi": doi or "",
            "matched_year": "",
            "matched_title": "",
            "matched_authors": "",
            "query_used": query[:260],
            "error": "",
        }

        # Crossref search
        if use_crossref:
            items = _crossref_items(query, rows=8)
            for it in items:
                cand_year = _crossref_year(it)
                if y_ref is not None and cand_year != y_ref:
                    continue  # STRICT year

                cand_title = _crossref_title(it)
                cand_surnames = _crossref_surnames(it)
                if surnames_ref and not _author_overlap_ok(surnames_ref, cand_surnames):
                    continue  # STRICT multi-author

                score = fuzz.WRatio(title_ref, cand_title) if (title_ref and cand_title) else 0
                if score < title_score_min:
                    continue  # STRICT title

                cand_doi = (it.get("DOI") or "").strip()
                cand = {
                    "reference": ref,
                    "status": "verified",
                    "source": "crossref",
                    "score": int(score),
                    "doi": cand_doi,
                    "matched_year": str(cand_year or ""),
                    "matched_title": (cand_title or "")[:220],
                    "matched_authors": ", ".join(cand_surnames[:8]),
                    "query_used": query[:260],
                    "error": "",
                }
                if cand["score"] > best["score"]:
                    best = cand

        # OpenAlex search
        if use_openalex:
            items = _openalex_items(query, per_page=8)
            for it in items:
                cand_year = _openalex_year(it)
                if y_ref is not None and cand_year != y_ref:
                    continue  # STRICT year

                cand_title = _openalex_title(it)
                cand_surnames = _openalex_surnames(it)
                if surnames_ref and not _author_overlap_ok(surnames_ref, cand_surnames):
                    continue  # STRICT multi-author

                score = fuzz.WRatio(title_ref, cand_title) if (title_ref and cand_title) else 0
                if score < title_score_min:
                    continue  # STRICT title

                cand_doi = _openalex_doi(it)
                cand = {
                    "reference": ref,
                    "status": "verified",
                    "source": "openalex",
                    "score": int(score),
                    "doi": cand_doi,
                    "matched_year": str(cand_year or ""),
                    "matched_title": (cand_title or "")[:220],
                    "matched_authors": ", ".join(cand_surnames[:8]),
                    "query_used": query[:260],
                    "error": "",
                }
                if cand["score"] > best["score"]:
                    best = cand

        return best

    except Exception as e:
        return {
            "reference": reference_text or "",
            "status": "offline",
            "source": "",
            "score": 0,
            "doi": "",
            "matched_year": "",
            "matched_title": "",
            "matched_authors": "",
            "query_used": "",
            "error": str(e)[:220],
        }

def verify_references_batch_strict(
    references: List[str],
    max_to_check: int = 0,          # 0 means all
    throttle_s: float = 0.25,
    use_crossref: bool = True,
    use_openalex: bool = True,
    title_score_min: int = 95,
) -> List[Dict[str, Any]]:
    work = list(references or [])
    if max_to_check and int(max_to_check) > 0:
        work = work[: int(max_to_check)]

    rows: List[Dict[str, Any]] = []
    for ref in work:
        rows.append(
            verify_one_reference_strict(
                reference_text=ref,
                use_crossref=use_crossref,
                use_openalex=use_openalex,
                throttle_s=throttle_s,
                title_score_min=title_score_min,
            )
        )
    return rows


# ============================
# Public API: run_crosscheck
# ============================
def run_crosscheck(
    file_bytes: bytes,
    filename: str,
    style: str = "apa",  # "apa" | "ieee" | "vancouver"
    verify_online: bool = False,
    use_crossref: bool = True,
    use_openalex: bool = True,
    throttle: float = 0.25,     # NOTE: main.py sends "throttle"
    max_verify: int = 0,        # 0 means all
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
        references_raw: List[str] = []
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

    missing, uncited, summary = build_missing_uncited(cites, refs)

    # 4) Optional online verification (STRICT)
    verify_rows: List[Dict[str, Any]] = []
    verify_summary: Dict[str, Any] = {
        "enabled": bool(verify_online),
        "requested": int(max_verify) if max_verify else 0,
        "checked": 0,
        "verified": 0,
        "needs_review": 0,
        "not_found": 0,
        "offline": 0,
    }

    if verify_online and refs:
        verify_rows = verify_references_batch_strict(
            references=[r.raw for r in refs],
            max_to_check=int(max_verify) if (max_verify and int(max_verify) > 0) else 0,
            throttle_s=float(throttle or 0.0),
            use_crossref=bool(use_crossref),
            use_openalex=bool(use_openalex),
            title_score_min=95,
        )
        verify_summary["checked"] = int(len(verify_rows))
        verify_summary["verified"] = int(sum(1 for r in verify_rows if r.get("status") == "verified"))
        verify_summary["needs_review"] = int(sum(1 for r in verify_rows if r.get("status") == "needs_review"))
        verify_summary["not_found"] = int(sum(1 for r in verify_rows if r.get("status") == "not_found"))
        verify_summary["offline"] = int(sum(1 for r in verify_rows if r.get("status") == "offline"))

    # 5) Return JSON (stable keys for UI + exports)
    return {
        "filename": filename,
        "style": style,
        "reference_detection_message": ref_msg,
        "text_length": int(len(full_text)),
        "main_text_length": int(len(main_text)),
        "references_detected": int(len(references_raw)),

        "summary": summary,

        # Clean lists (no reference_full wrappers)
        "missing_in_references": missing,
        "uncited_references": uncited,
        "reconciliation_intext_to_reference": c2r[:5000],
        "reconciliation_reference_to_intext": r2c[:5000],

        # Online verification
        "online_verification": {
            "enabled": bool(verify_online),
            "use_crossref": bool(use_crossref),
            "use_openalex": bool(use_openalex),
            "throttle": float(throttle or 0.0),
            "max_verify": int(max_verify) if max_verify else 0,  # 0 means all
            "summary": verify_summary,
            "rows": verify_rows,
        },

        # Samples (debug-friendly, safe size)
        "sample_intext_citations": [c.__dict__ for c in cites[:120]],
        "sample_references_parsed": [r.__dict__ for r in refs[:120]],
    }
