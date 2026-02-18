# engine.py
# Citation Crosschecker Engine (FastAPI version)
# Supports: APA/Harvard (author-year), IEEE (numeric [1]), Vancouver (numeric (1)/superscript)
# Outputs: missing in references, uncited references, reconciliation tables (intext->ref, ref->cited_by)
# Optional: Online verification (Crossref + OpenAlex), strict title+authors+year match, returns DOI.

import re
import io
import time
import json
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter

# Optional libs for parsing and verification
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

try:
    import requests
    REQ_OK = True
except Exception:
    REQ_OK = False

try:
    from rapidfuzz import fuzz
    RF_OK = True
except Exception:
    RF_OK = False

try:
    from tenacity import retry, stop_after_attempt, wait_exponential
    TEN_OK = True
except Exception:
    TEN_OK = False


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

# Words that often precede citations but are NOT authors
DISCOURSE_PREFIX_WORDS = {
    "similarly", "however", "nonetheless", "therefore", "thus", "moreover",
    "furthermore", "consequently", "instead", "alternatively", "notably",
    "specifically", "generally", "overall",
}

BAD_NARRATIVE_PREFIX_WORDS = {
    # narrative noise
    "traditional", "classical", "analytical", "for", "from", "in", "on", "at", "by",
    "methods", "method", "approach", "approaches", "sample", "size", "power",
    "results", "discussion", "model", "framework",
    # discourse words
    *DISCOURSE_PREFIX_WORDS,
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

    # For online verification
    title_guess: Optional[str] = None
    author_surnames_guess: Optional[List[str]] = None


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

        # DOI lines often start with "10." or "doi: 10." or "https://doi.org/..."
        is_doi_line = bool(re.match(r"^\s*(doi\s*:\s*)?10\.\d{4,9}/", s, flags=re.I)) or \
                      bool(re.match(r"^\s*https?://doi\.org/10\.\d{4,9}/", s, flags=re.I)) or \
                      bool(re.match(r"^\s*10\.\d{4,9}/", s))

        # APA start: Surname, A.
        apa_start = bool(re.match(r"^[A-Z][A-Za-z\-\']+,\s+[A-Z]\.", s))

        # IEEE/Vancouver numbering start: [26] OR 26. OR 26) OR 26 <space>
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
def _guess_title_from_reference(raw: str) -> str:
    """
    Best-effort title guess from an APA-like reference:
    Often appears after year and before journal/book.
    We'll keep it conservative to avoid garbage.
    """
    r = norm_space(raw)
    # remove leading numbering
    r = re.sub(r"^\s*(\[\d+\]|\d{1,4}[\.\)]|\d{1,4}\s+)\s*", "", r).strip()

    # Try: ... (YEAR). Title. Journal...
    m = re.search(rf"\(\s*{YEAR}\s*\)\.\s*(.+?)\.\s", r)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s+", " ", title)
        return title

    # Try: YEAR. Title. Journal...
    m2 = re.search(rf"\b{YEAR}\b\.\s*(.+?)\.\s", r)
    if m2:
        title = m2.group(1).strip()
        title = re.sub(r"\s+", " ", title)
        return title

    return ""

def _guess_author_surnames_from_reference(raw: str) -> List[str]:
    """
    Rough extraction of surnames from reference head:
    "Surname, A., Surname2, B., & Surname3, C."
    """
    head = raw.strip()
    head = re.sub(r"^\s*(\[\d+\]|\d{1,4}[\.\)]|\d{1,4}\s+)\s*", "", head).strip()
    # take part before year
    m = re.search(rf"\(\s*{YEAR}\s*\)", head)
    if m:
        head = head[:m.start()].strip()
    else:
        m2 = re.search(rf"\b{YEAR}\b", head)
        if m2:
            head = head[:m2.start()].strip()

    # capture tokens before commas as surname candidates
    cand = []
    for part in head.split(","):
        p = part.strip()
        if not p:
            continue
        # surname is usually first token in the segment
        tok = p.split()[0].strip()
        if re.fullmatch(r"[A-Z][A-Za-z\-']{1,40}", tok):
            if norm_token(tok) not in BAD_NARRATIVE_PREFIX_WORDS:
                cand.append(tok)
    # de-dup preserve order
    out = []
    seen = set()
    for s in cand:
        k = norm_token(s)
        if k and k not in seen:
            out.append(s)
            seen.add(k)
    return out

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
        entry = ReferenceEntry(raw=r, key=k, year=year, surnames=(pre,), number=None)
        entry.title_guess = _guess_title_from_reference(r)
        entry.author_surnames_guess = _guess_author_surnames_from_reference(r)
        return entry

    if "," in pre:
        first = pre.split(",")[0].strip()
    else:
        first = pre.split()[0].strip() if pre.split() else ""

    if not first:
        return None

    entry = ReferenceEntry(raw=r, key=key_author_year(first, year), year=year, surnames=(first,), number=None)
    entry.title_guess = _guess_title_from_reference(r)
    entry.author_surnames_guess = _guess_author_surnames_from_reference(r)
    return entry

def parse_reference_numeric(ref_raw: str) -> Optional[ReferenceEntry]:
    r = (ref_raw or "").strip()
    if not r:
        return None

    m = re.match(r"^\s*\[\s*(\d+)\s*\]\s*(.+)$", r)
    if m:
        n = int(m.group(1))
        entry = ReferenceEntry(raw=r, key=key_numeric(n), number=n)
        entry.title_guess = _guess_title_from_reference(r)
        entry.author_surnames_guess = _guess_author_surnames_from_reference(r)
        return entry

    m = re.match(r"^\s*(\d+)\s*[\.\)]\s*(.+)$", r)
    if m:
        n = int(m.group(1))
        entry = ReferenceEntry(raw=r, key=key_numeric(n), number=n)
        entry.title_guess = _guess_title_from_reference(r)
        entry.author_surnames_guess = _guess_author_surnames_from_reference(r)
        return entry

    m = re.match(r"^\s*(\d+)\s+(.+)$", r)
    if m:
        n = int(m.group(1))
        entry = ReferenceEntry(raw=r, key=key_numeric(n), number=n)
        entry.title_guess = _guess_title_from_reference(r)
        entry.author_surnames_guess = _guess_author_surnames_from_reference(r)
        return entry

    return None


# ============================
# In-text extraction (APA/Harvard)
# ============================
def _drop_discourse_prefix(authors_blob: str) -> str:
    """
    Handles cases like: "Similarly, Adam and Kofi (2020)"
    where the regex may capture "Similarly, Adam and Kofi" as authors.
    If the first comma-separated token is a discourse word, drop it.
    """
    blob = (authors_blob or "").strip()
    if "," not in blob:
        return blob
    first, rest = blob.split(",", 1)
    if norm_token(first.strip()) in DISCOURSE_PREFIX_WORDS:
        return rest.strip()
    return blob

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

        authors_blob = _drop_discourse_prefix(m.group("authors").strip())
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

    # Single-author narrative (skip inside multi-author spans)
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
    r2c: Optional[List[Dict[str, Any]]] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    cite_keys = [c.key for c in cites]
    ref_keys = [r.key for r in refs]

    cite_count_by_raw = Counter([c.raw for c in cites])
    cite_key_by_raw: Dict[str, str] = {}
    for c in cites:
        cite_key_by_raw.setdefault(c.raw, c.key)

    ref_key_set = set(ref_keys)
    missing = []
    for raw, cnt in cite_count_by_raw.items():
        k = cite_key_by_raw.get(raw, "")
        if k and (k not in ref_key_set):
            missing.append({"citation_in_text": raw, "count_in_text": int(cnt)})
    missing.sort(key=lambda x: (-x["count_in_text"], x["citation_in_text"]))

    # Uncited references: best from r2c if provided, else compute using keys
    uncited: List[Dict[str, Any]] = []
    if isinstance(r2c, list) and r2c:
        for row in r2c:
            if int(row.get("times_cited", 0) or 0) == 0:
                uncited.append({"reference": row.get("reference", "")})
    else:
        cite_key_set = set(cite_keys)
        for r in refs:
            if r.key not in cite_key_set:
                uncited.append({"reference": r.raw})

    summary = {
        "in_text_citations_found": int(len(cites)),
        "reference_entries_found": int(len(refs)),
        "missing_in_references": int(len(missing)),
        "uncited_references": int(len(uncited)),
    }
    return missing, uncited, summary


# =============================
# Online verification (Crossref + OpenAlex)
# =============================
UA = "CitationCrosschecker/1.0 (mailto:admin@example.com)"

def _title_norm(s: str) -> str:
    s = norm_token(s)
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if RF_OK:
        return float(fuzz.token_set_ratio(a, b)) / 100.0
    # fallback
    a_set = set(a.split())
    b_set = set(b.split())
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / max(len(a_set), len(b_set))

def _year_from_crossref(item: Dict[str, Any]) -> Optional[int]:
    for k in ("published-print", "published-online", "issued", "created"):
        v = item.get(k)
        try:
            parts = v.get("date-parts", [])
            if parts and parts[0] and isinstance(parts[0][0], int):
                return int(parts[0][0])
        except Exception:
            pass
    return None

def _authors_from_crossref(item: Dict[str, Any]) -> List[str]:
    out = []
    for a in (item.get("author") or []):
        fam = (a.get("family") or "").strip()
        if fam:
            out.append(fam)
    return out

def _title_from_crossref(item: Dict[str, Any]) -> str:
    t = item.get("title") or []
    if isinstance(t, list) and t:
        return str(t[0] or "").strip()
    if isinstance(t, str):
        return t.strip()
    return ""

def _year_from_openalex(item: Dict[str, Any]) -> Optional[int]:
    y = item.get("publication_year")
    try:
        return int(y) if y else None
    except Exception:
        return None

def _authors_from_openalex(item: Dict[str, Any]) -> List[str]:
    out = []
    for a in (item.get("authorships") or []):
        name = (((a.get("author") or {}).get("display_name")) or "").strip()
        if name:
            # last token as surname
            out.append(name.split()[-1])
    return out

def _title_from_openalex(item: Dict[str, Any]) -> str:
    return (item.get("title") or "").strip()

def _doi_from_openalex(item: Dict[str, Any]) -> str:
    doi = (item.get("doi") or "").strip()
    if doi and doi.lower().startswith("https://doi.org/"):
        return doi[len("https://doi.org/"):]
    return doi

def _author_overlap_ratio(expected: List[str], got: List[str]) -> float:
    """
    Strict-ish: require overlap across multiple author surnames.
    ratio = |intersection| / max(1, min(len(expected), len(got)))
    """
    e = [norm_token(x) for x in (expected or []) if x]
    g = [norm_token(x) for x in (got or []) if x]
    e_set = set(e)
    g_set = set(g)
    if not e_set or not g_set:
        return 0.0
    inter = len(e_set & g_set)
    denom = max(1, min(len(e_set), len(g_set)))
    return inter / denom

def _strict_decision(title_sim: float, author_overlap: float, year_match: bool) -> Tuple[bool, str]:
    """
    Very strict rule:
      - exact year match
      - title similarity very high
      - meaningful author overlap (multiple authors)
    """
    if not year_match:
        return False, "Year mismatch"
    if title_sim >= 0.95 and author_overlap >= 0.60:
        return True, "Strict match"
    if title_sim >= 0.90 and author_overlap >= 0.75:
        return True, "Strict match (authors very strong)"
    return False, "Not strict enough"

if TEN_OK:
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.6, min=0.6, max=4))
    def _get_json(url: str, params: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
        if not REQ_OK:
            raise RuntimeError("requests not installed")
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        return r.json()
else:
    def _get_json(url: str, params: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
        if not REQ_OK:
            raise RuntimeError("requests not installed")
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        return r.json()

def verify_reference_online(
    ref: ReferenceEntry,
    throttle_s: float = 0.25,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> Dict[str, Any]:
    """
    Returns:
      status: verified | not_verified | needs_review | error
      doi: DOI if verified
      source: crossref|openalex
      score: dict with title_sim, author_overlap, year_match
      matched_title, matched_year, matched_authors
      reason
    """
    title = (ref.title_guess or "").strip()
    authors = ref.author_surnames_guess or []
    year = ref.year

    # if we cannot form a strict query, return quickly
    if not title or not year:
        return {
            "status": "needs_review",
            "source": "",
            "doi": "",
            "reason": "Missing title/year in parsed reference",
            "score": {"title_sim": 0, "author_overlap": 0, "year_match": False},
            "matched_title": "",
            "matched_year": "",
            "matched_authors": "",
        }

    exp_title = _title_norm(title)
    exp_auth = [a for a in authors if a and norm_token(a) not in BAD_NARRATIVE_PREFIX_WORDS]
    exp_year = None
    try:
        exp_year = int(re.sub(r"[^\d]", "", str(year))[:4])
    except Exception:
        exp_year = None

    best = None  # (verified_bool, info)
    best_score = -1.0

    # --- Crossref ---
    err_crossref = ""
    if use_crossref:
        try:
            params = {
                "rows": 5,
                "query.title": title,
                "query.author": " ".join(exp_auth[:4]) if exp_auth else "",
                "select": "DOI,title,author,issued,published-print,published-online,created",
            }
            data = _get_json("https://api.crossref.org/works", params=params)
            items = (((data or {}).get("message") or {}).get("items") or [])
            for it in items:
                it_title = _title_from_crossref(it)
                it_year = _year_from_crossref(it)
                it_auth = _authors_from_crossref(it)
                title_sim = _sim(exp_title, _title_norm(it_title))
                author_overlap = _author_overlap_ratio(exp_auth, it_auth)
                year_match = (exp_year is not None) and (it_year == exp_year)

                verified, reason = _strict_decision(title_sim, author_overlap, year_match)

                # ranking score emphasises title then authors
                score = (title_sim * 0.7) + (author_overlap * 0.3)
                if score > best_score:
                    best_score = score
                    best = {
                        "status": "verified" if verified else "not_verified",
                        "source": "crossref",
                        "doi": (it.get("DOI") or "") if verified else "",
                        "reason": reason,
                        "score": {
                            "title_sim": round(title_sim, 3),
                            "author_overlap": round(author_overlap, 3),
                            "year_match": bool(year_match),
                        },
                        "matched_title": it_title,
                        "matched_year": str(it_year or ""),
                        "matched_authors": ", ".join(it_auth[:8]),
                        "query_used": json.dumps(params),
                        "error_crossref": "",
                        "error_openalex": "",
                    }

                    # stop early if strictly verified
                    if verified:
                        break

            time.sleep(max(0.0, float(throttle_s or 0.0)))

        except Exception as e:
            err_crossref = str(e)[:240]

    # --- OpenAlex ---
    err_openalex = ""
    if use_openalex:
        try:
            # OpenAlex: search works with a free-text query, then filter by year
            q = f'"{title}" {(" ".join(exp_auth[:4]))} {exp_year or ""}'.strip()
            params = {"search": q, "per_page": 5}
            data = _get_json("https://api.openalex.org/works", params=params)
            items = (data or {}).get("results") or []

            for it in items:
                it_title = _title_from_openalex(it)
                it_year = _year_from_openalex(it)
                it_auth = _authors_from_openalex(it)

                title_sim = _sim(exp_title, _title_norm(it_title))
                author_overlap = _author_overlap_ratio(exp_auth, it_auth)
                year_match = (exp_year is not None) and (it_year == exp_year)

                verified, reason = _strict_decision(title_sim, author_overlap, year_match)
                score = (title_sim * 0.7) + (author_overlap * 0.3)

                # choose if better than existing best
                if score > best_score:
                    best_score = score
                    best = {
                        "status": "verified" if verified else "not_verified",
                        "source": "openalex",
                        "doi": _doi_from_openalex(it) if verified else "",
                        "reason": reason,
                        "score": {
                            "title_sim": round(title_sim, 3),
                            "author_overlap": round(author_overlap, 3),
                            "year_match": bool(year_match),
                        },
                        "matched_title": it_title,
                        "matched_year": str(it_year or ""),
                        "matched_authors": ", ".join(it_auth[:8]),
                        "query_used": q,
                        "error_crossref": "",
                        "error_openalex": "",
                    }

                    if verified:
                        break

            time.sleep(max(0.0, float(throttle_s or 0.0)))

        except Exception as e:
            err_openalex = str(e)[:240]

    if best is None:
        return {
            "status": "error",
            "source": "",
            "doi": "",
            "reason": "No result candidates (or requests blocked)",
            "score": {"title_sim": 0, "author_overlap": 0, "year_match": False},
            "matched_title": "",
            "matched_year": "",
            "matched_authors": "",
            "query_used": "",
            "error_crossref": err_crossref,
            "error_openalex": err_openalex,
        }

    # attach any errors
    best["error_crossref"] = best.get("error_crossref", "") or err_crossref
    best["error_openalex"] = best.get("error_openalex", "") or err_openalex
    return best

def run_online_verification(
    refs: List[ReferenceEntry],
    max_verify: int = 0,           # 0 = verify ALL
    throttle_s: float = 0.25,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> Dict[str, Any]:
    """
    Returns a compact payload for dashboard + exports.
    """
    if not refs:
        return {"attempted": 0, "verified": 0, "results": []}

    if max_verify and max_verify > 0:
        target = refs[:max_verify]
    else:
        target = refs[:]  # ALL

    results = []
    verified = 0
    attempted = 0

    for r in target:
        attempted += 1
        res = verify_reference_online(
            r,
            throttle_s=throttle_s,
            use_crossref=use_crossref,
            use_openalex=use_openalex,
        )

        is_verified = (res.get("status") == "verified") and bool(res.get("doi"))
        if is_verified:
            verified += 1

        results.append(
            {
                "reference": r.raw,
                "verified": bool(is_verified),
                "status": res.get("status", ""),
                "source": res.get("source", ""),
                "doi": res.get("doi", ""),
                "reason": res.get("reason", ""),
                "score": res.get("score", {}),
                "matched_year": res.get("matched_year", ""),
                "matched_title": res.get("matched_title", ""),
                "matched_authors": res.get("matched_authors", ""),
                "query_used": res.get("query_used", ""),
                "error_crossref": res.get("error_crossref", ""),
                "error_openalex": res.get("error_openalex", ""),
            }
        )

    return {"attempted": attempted, "verified": verified, "results": results}


# ============================
# Public API: run_crosscheck
# ============================
def run_crosscheck(
    file_bytes: bytes,
    filename: str,
    style: str = "apa",             # "apa" | "ieee" | "vancouver"
    verify_online: bool = False,    # optional online verification
    max_verify: int = 0,            # 0 = verify ALL
    throttle_s: float = 0.25,
    use_crossref: bool = True,
    use_openalex: bool = True,
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

    missing, uncited, summary = build_missing_uncited(cites, refs, r2c=r2c)

    # 4) Optional online verification (runs AFTER crosscheck)
    online_payload = None
    if verify_online:
        online_payload = run_online_verification(
            refs=refs,
            max_verify=max_verify,     # 0 = ALL
            throttle_s=throttle_s,
            use_crossref=use_crossref,
            use_openalex=use_openalex,
        )

    # 5) Return JSON
    out = {
        "filename": filename,
        "style": style,
        "reference_detection_message": ref_msg,
        "text_length": len(full_text),
        "main_text_length": len(main_text),
        "references_detected": len(references),
        "summary": summary,
        "missing_in_references": missing,
        "uncited_references": uncited,  # clean dicts with "reference"
        "reconciliation_intext_to_reference": c2r[:5000],
        "reconciliation_reference_to_intext": r2c[:5000],
        "sample_intext_citations": [c.__dict__ for c in cites[:120]],
        "sample_references_parsed": [r.__dict__ for r in refs[:120]],
    }

    if online_payload is not None:
        out["online_verification"] = online_payload

    return out
