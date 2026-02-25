# engine.py
__version__ = "1.3.0"

import re
import io
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter

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


YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b", re.I)

REF_HEADINGS = [
    r"^\s*references?\s*(?:list)?\s*$",
    r"^\s*bibliograph(?:y|ies)\s*$",
    r"^\s*works\s+cited\s*$",
    r"^\s*literature\s+cited\s*$",
]

REF_HEADING_RELAXED = re.compile(
    r"^\s*(references?|bibliography|works\s+cited|literature\s+cited)\b",
    re.I,
)

# Words/phrases that often precede citations in prose and should NOT be treated as author tokens.
# Used only when they appear at the *start* of a candidate citation string.
DISCOURSE_PREFIXES = {
    "see", "e.g", "eg", "i.e", "ie",
    "as", "in", "for", "from", "to", "at", "on", "by", "with", "within",
    "according", "adapted", "based", "cited", "citing", "reported",
    "like",

    # common prose lead-ins
    "however", "similarly", "regrettably", "traditionally", "notably",
    "therefore", "thus", "hence", "consequently", "moreover", "furthermore",
    "additionally", "meanwhile", "nonetheless", "nevertheless", "overall",
    "generally", "specifically", "particularly", "importantly", "indeed",

    # examples
    "for instance", "instance", "for example", "example",
    "for instance,", "for example,",
}

# Headings that often appear *after* the reference list in theses/articles.
REF_END_HEADINGS = [
    r"^\s*appendix(?:es)?\b",
    r"^\s*annex(?:es)?\b",
    r"^\s*supplement(?:ary)?\b",
    r"^\s*supporting\s+information\b",
    r"^\s*supporting\s+documents?\b",
    r"^\s*additional\s+materials?\b",
    r"^\s*online\s+appendix\b",
]
REF_END_HEADING_RE = re.compile("|".join(REF_END_HEADINGS), re.I)

# Common false-positive "author" tokens we should never treat as citations.
NON_NAME_AUTHOR_KEYS = {
    "survey", "field", "work", "fieldwork", "data", "dataset", "table", "tables", "figure", "fig", "figures",
    "chapter", "section", "appendix", "appendices", "annex", "equation", "eq", "model", "models",
    "analysis", "results", "method", "methods", "discussion", "introduction", "conclusion",
    "study", "paper", "thesis", "report", "source", "sources", "author", "authors",

    # discourse words that sometimes get misread as authors
    "however", "similarly", "regrettably", "traditionally", "therefore", "thus", "hence",
    "consequently", "moreover", "furthermore", "additionally", "meanwhile", "nonetheless",
    "nevertheless", "overall", "generally", "specifically", "particularly", "importantly",
    "indeed", "instance", "example",
}


# -----------------------------
# Commercial-grade narrative filtering (avoid false "Missing")
# -----------------------------
# These are common *narrative* words/phrases that the regex can mistakenly treat as "Author, YEAR".
# We filter them out at parse-time so they don't inflate "Missing" counts.
NARRATIVE_SINGLE_TOKENS = {
    "crisis", "war", "scandal", "revolution", "katrina",
    "pandemic", "covid", "covid19", "covid-19",
}

NARRATIVE_PHRASE_PATTERNS = [
    r"\byear\s+on\s+year\b",
    r"\bgrowth\s+rate\b",
    r"\ball\s+share\s+index\b",
    r"\bselected\s+african\s+countries\b",
    r"\btop\s+four\s+african\s+countries\b",
    r"\baccording\s+to\b",
]

_DECADE_YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20\d{2})s\b", re.I)

def _is_likely_narrative_citation(left: str, year: str, full_cite: str) -> bool:
    """Return True if the captured 'Author' part looks like narrative text, not a real author/org."""
    l = (left or "").strip()
    if not l:
        return True

    s_full = (full_cite or "").lower()
    for pat in NARRATIVE_PHRASE_PATTERNS:
        if re.search(pat, s_full, flags=re.I):
            return True

    # decades like "Fisher, 1930s" are not standard author-year citations
    if year and isinstance(year, str) and year.lower().endswith("s"):
        if _DECADE_YEAR_RE.search(full_cite or ""):
            return True

    # single-word narrative tokens
    l_norm = soft_lower(l)
    if re.fullmatch(r"[a-z\-']+", l_norm) and l_norm in NARRATIVE_SINGLE_TOKENS:
        return True

    return False


# -----------------------------
# Small helpers
# -----------------------------
def norm_space(s: str) -> str:
    s = s or ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def soft_lower(s: str) -> str:
    return norm_space(s).lower()


def strip_punct(s: str) -> str:
    s = soft_lower(s)
    s = re.sub(r"[“”\"'’`]", "", s)
    s = re.sub(r"[^a-z0-9\s\-&/\u2013\u2014-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _looks_like_toc_references_line(s: str, tail: str) -> bool:
    if not s:
        return False
    tail = (tail or "").strip()
    if tail and re.fullmatch(r"\d{1,4}", tail):
        return True
    if re.search(r"\.{2,}\s*\d{1,4}\s*$", s):
        return True
    return False


def _looks_like_heading_line(s: str) -> bool:
    s0 = (s or "").strip()
    if not s0:
        return False
    if len(s0) > 120:
        return False
    if s0.endswith(".") and len(s0) > 25:
        return False
    letters = re.sub(r"[^A-Za-z]", "", s0)
    if letters and letters.isupper() and len(letters) >= 6:
        return True
    if re.match(r"^[A-Z][A-Za-z0-9\s\-,:]{3,}$", s0):
        return True
    return False


# -----------------------------
# Stricter reference acceptance (but not brittle)
# Accept only entries that contain Author + Year + Title (either order)
# and support broad organisation authors.
# -----------------------------

_LEAD_NUM_RE = re.compile(r"^\s*(?:\[\s*\d{1,4}\s*\]|\(?\s*\d{1,4}\s*\)?|\d{1,4})\s*[\.)\]]\s*")


def _strip_leading_reference_number(s: str) -> str:
    s0 = norm_space(s)
    s0 = _LEAD_NUM_RE.sub("", s0)
    return s0.strip()


def _looks_like_person_author(s: str) -> bool:
    s0 = norm_space(s)
    if re.search(r"\b[A-Z][A-Za-z'\-]+,\s*(?:[A-Z]\.\s*){1,4}(?:[A-Z]\.\s*)?", s0):
        return True
    if re.search(r"\b[A-Z][A-Za-z'\-]+\s+(?:[A-Z]\.?)\s*(?:[A-Z]\.?)\b", s0):
        return True
    if re.search(r"\b[A-Z][A-Za-z'\-]+\s+et\s+al\.", s0):
        return True
    return False


def _looks_like_org_author(s: str) -> bool:
    s0 = norm_space(s)

    if re.search(r"\(([A-Z]{2,10})\)", s0):
        return True

    head = re.sub(r"[^A-Za-z0-9\s/&\-]", " ", s0)
    toks = [t for t in head.split() if t]
    if toks:
        t0 = toks[0]
        t0_clean = re.sub(r"[^A-Za-z]", "", t0)
        if t0_clean and t0_clean.isupper() and len(t0_clean) >= 2:
            return True

    def titleish(w: str) -> bool:
        wc = re.sub(r"[^A-Za-z]", "", w)
        if not wc:
            return False
        if wc.isupper() and 2 <= len(wc) <= 12:
            return True
        return bool(re.match(r"^[A-Z][a-z]{2,}$", wc))

    run = 0
    best = 0
    for w in toks[:16]:
        if titleish(w):
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best >= 2


def _looks_like_title_piece(s: str) -> bool:
    s0 = norm_space(s)
    if len(s0) < 6:
        return False
    letters = re.findall(r"[A-Za-z]", s0)
    if len(letters) < 5:
        return False
    if re.fullmatch(r"(?i)(?:vol(?:ume)?|issue|no\.?|pp\.?|pages?|doi)\b.*", s0):
        return False
    if re.fullmatch(r"\d{1,4}(?:\s*[-–]\s*\d{1,4})?", s0):
        return False

    word_count = len([w for w in re.split(r"\s+", s0) if w])
    if word_count >= 3:
        return True
    if ":" in s0 or "–" in s0 or "-" in s0:
        return True
    return True


def _is_plausible_reference_entry(s: str) -> bool:
    s0 = _strip_leading_reference_number(s)
    if not s0 or len(s0) < 18:
        return False

    ym = YEAR_RE.search(s0)
    if not ym:
        return False

    left = s0[: ym.start()].strip()
    author_ok = (
        _looks_like_person_author(left)
        or _looks_like_org_author(left)
        or _looks_like_person_author(s0[:120])
        or _looks_like_org_author(s0[:120])
    )
    if not author_ok:
        return False

    after = s0[ym.end():].lstrip(" ).,;:-")
    after_title = after.split(".", 1)[0].strip()
    if len(after_title) < 6 and "," in after:
        after_title = after.split(",", 1)[0].strip()

    before = s0[: ym.start()].strip(" .;:-")
    before_parts = [p.strip() for p in before.split(".") if p.strip()]
    before_title = before_parts[-1] if before_parts else ""

    return _looks_like_title_piece(after_title) or _looks_like_title_piece(before_title)


# -----------------------------
# Author key extraction (improved)
# -----------------------------
def _first_author_or_org_key(author_left: str) -> str:
    s = norm_space(author_left)

    m = re.search(r"\(([A-Z][A-Z0-9/&\-]{1,15})\)", s)
    if m:
        return strip_punct(m.group(1))

    s = _strip_leading_reference_number(s)
    s = re.sub(r"\(\s*(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?\s*\).*", "", s).strip()
    s = re.sub(r"(’s|'s)\b", "", s)

    m_si = re.match(r"^\s*([A-Z][A-Za-z'\-]+)\s+[A-Z]{1,3}\b", s)
    if m_si:
        return strip_punct(m_si.group(1))

    s0 = re.split(r"\s+(?:&|and|＆)\s+|,", s, maxsplit=1)[0].strip()
    s0 = re.sub(r"\bet\s+al\.?\b", "", s0, flags=re.I).strip()

    toks = [t for t in re.split(r"\s+", s0) if t and re.search(r"[A-Za-z0-9]", t)]
    if not toks:
        return ""
    return strip_punct(toks[-1])


def _truncate_reference_block(lines: List[str], style_hint: str) -> List[str]:
    out: List[str] = []
    ref_like_seen = 0

    def _is_ref_like(ln: str) -> bool:
        if style_hint == "numeric":
            return _looks_like_new_numeric_reference_start(ln)
        return _looks_like_new_apa_reference_start(ln)

    for i, ln in enumerate(lines):
        s = (ln or "").strip()
        if not s:
            continue

        if _is_ref_like(s):
            ref_like_seen += 1

        if ref_like_seen >= 3 and (
            REF_END_HEADING_RE.search(s)
            or (
                _looks_like_heading_line(s)
                and re.search(r"\b(appendix|appendices|annex|supplement|supporting|additional)\b", s, re.I)
            )
        ):
            look = [x for x in lines[i : i + 25] if (x or "").strip()]
            look_ref = sum(1 for x in look if _is_ref_like((x or "").strip()))
            if look_ref <= 1:
                break

        out.append(ln)

    return out


# -----------------------------
# DOCX extraction
# -----------------------------
def _iter_docx_text(doc: "Document"):
    for p in doc.paragraphs:
        t = norm_space(p.text)
        if t:
            yield t
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    t = norm_space(p.text)
                    if t:
                        yield t


def _docx_xml_text(file_bytes: bytes) -> List[str]:
    import zipfile
    import xml.etree.ElementTree as ET

    NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

    def _extract_from_xml(xml_bytes: bytes) -> List[str]:
        out: List[str] = []
        try:
            root = ET.fromstring(xml_bytes)
        except Exception:
            return out
        for p in root.findall(".//w:p", NS):
            parts: List[str] = []
            for tnode in p.findall(".//w:t", NS):
                if tnode.text:
                    parts.append(tnode.text)
            s = norm_space("".join(parts))
            if s:
                out.append(s)
        return out

    targets = ["word/document.xml", "word/footnotes.xml", "word/endnotes.xml"]

    with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
        names = set(z.namelist())
        for name in sorted(names):
            if name.startswith("word/header") and name.endswith(".xml"):
                targets.append(name)
            if name.startswith("word/footer") and name.endswith(".xml"):
                targets.append(name)

        lines: List[str] = []
        for t in targets:
            if t in names:
                try:
                    lines.extend(_extract_from_xml(z.read(t)))
                except Exception:
                    continue
    return lines


def read_docx_split_main_and_refs(file_bytes: bytes) -> Tuple[str, List[str], str]:
    if not DOCX_OK:
        raise RuntimeError("python-docx not installed")

    try:
        lines = _docx_xml_text(file_bytes)
    except Exception:
        lines = []

    if not lines:
        doc = Document(io.BytesIO(file_bytes))
        lines = list(_iter_docx_text(doc))

    main_lines: List[str] = []
    ref_lines: List[str] = []
    in_refs = False
    heading_line = ""

    for t in lines:
        if not in_refs:
            for pat in REF_HEADINGS:
                if re.search(pat, t, flags=re.I):
                    in_refs = True
                    heading_line = t
                    break

            if not in_refs:
                m = REF_HEADING_RELAXED.search(t)
                if m and m.start() <= 4 and len(t) <= 160:
                    tail = t[m.end() :].strip(" :-\t")
                    if _looks_like_toc_references_line(t, tail):
                        main_lines.append(t)
                        continue
                    in_refs = True
                    heading_line = t
                    if tail:
                        ref_lines.append(tail)
                    continue

        if in_refs:
            ref_lines.append(t)
        else:
            main_lines.append(t)

    if in_refs:
        ref_lines = _truncate_reference_block(ref_lines, style_hint="apa")
        ref_lines = _truncate_reference_block(ref_lines, style_hint="numeric")

    msg = f"Found References heading: {heading_line}" if in_refs else "No References heading found."
    return "\n".join(main_lines).strip(), ref_lines, msg


# -----------------------------
# PDF extraction + heading detection
# -----------------------------
def read_pdf_text(file_bytes: bytes) -> str:
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")

    out: List[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            out.append((text or "").replace("\x00", " "))
    return "\n".join(out)


def _looks_like_new_numeric_reference_start(s: str) -> bool:
    s0 = (s or "").strip()
    if not s0:
        return False

    if re.match(r"^\[\s*\d{1,4}\s*\]\s+\S", s0):
        return True
    if re.match(r"^\(\s*\d{1,4}\s*\)\s+\S", s0):
        return True

    m = re.match(r"^(\d{1,4})([\.)])\s+(.+)$", s0)
    if m:
        num = m.group(1)
        if YEAR_RE.fullmatch(num):
            return False
        return True

    return False


def _looks_like_new_apa_reference_start(s: str) -> bool:
    s0 = (s or "").strip()
    if not s0:
        return False

    if re.search(r"\.\s*\(\s*" + YEAR + r"\s*\)\.", s0):
        return True

    m = re.match(r"^(.+?)\s*\(\s*" + YEAR + r"\s*\)", s0)
    if m:
        a = m.group(1)
        a = re.sub(r"[^A-Za-z,\.\-\s&/\u2013\u2014-]", "", a).strip()
        return len(a) >= 3
    return False


def _count_reference_like(lines: List[str], style_hint: str) -> int:
    c = 0
    for ln in lines:
        s = (ln or "").strip()
        if not s:
            continue
        if style_hint == "numeric":
            if _looks_like_new_numeric_reference_start(s):
                c += 1
        else:
            if _looks_like_new_apa_reference_start(s):
                c += 1
    return c


def _find_reference_heading(lines: List[str], style_hint: str) -> Tuple[int, str]:
    candidates: List[Tuple[int, str]] = []

    for i, line in enumerate(lines):
        s = (line or "").strip()
        if not s:
            continue

        for pat in REF_HEADINGS:
            if re.search(pat, s, flags=re.I):
                candidates.append((i, ""))

        m = REF_HEADING_RELAXED.search(s)
        if m and m.start() <= 4 and len(s) <= 160:
            tail = s[m.end() :].strip(" :-\t")
            if _looks_like_toc_references_line(s, tail):
                continue
            candidates.append((i, tail))

    for i, tail in candidates:
        lookahead = [ln for ln in lines[i + 1 : i + 31] if (ln or "").strip()]
        if _count_reference_like(lookahead, style_hint=style_hint) >= 3:
            return i, tail

    return -1, ""


# -----------------------------
# Merge and split reference lines
# -----------------------------
def _merge_reference_lines(raw_lines: List[str]) -> List[str]:
    raw_lines = [ln.strip() for ln in raw_lines if ln and ln.strip()]
    if not raw_lines:
        return []

    merged: List[str] = []
    cur = ""
    for ln in raw_lines:
        s = ln.strip()
        if not s:
            continue

        is_new = _looks_like_new_numeric_reference_start(s) or _looks_like_new_apa_reference_start(s)
        if is_new:
            if cur:
                merged.append(norm_space(cur))
            cur = s
        else:
            if not cur:
                cur = s
            else:
                joiner = " "
                if cur.endswith("-"):
                    cur = cur[:-1]
                    joiner = ""
                cur = cur + joiner + s

    if cur:
        merged.append(norm_space(cur))

    return [m for m in merged if m and len(m) >= 8]


def _split_embedded_numeric_refs(merged: List[str]) -> List[str]:
    out: List[str] = []
    br_pat = re.compile(r"(?=(\[\s*\d{1,4}\s*\]\s+))")
    dot_pat = re.compile(r"(?=(\b\d{1,4}[\.\)]\s+))")

    for s in merged:
        s = (s or "").strip()
        if not s:
            continue

        cuts: List[int] = []

        for m in br_pat.finditer(s):
            pos = m.start(1)
            if pos > 0:
                cuts.append(pos)

        for m in dot_pat.finditer(s):
            pos = m.start(1)
            if pos > 0:
                token = m.group(1).strip()
                num = re.match(r"^(\d{1,4})", token)
                if num and YEAR_RE.fullmatch(num.group(1)):
                    continue
                cuts.append(pos)

        if not cuts:
            out.append(s)
            continue

        cuts = sorted(set(cuts))
        prev = 0
        for pos in cuts:
            part = s[prev:pos].strip()
            if part:
                out.append(part)
            prev = pos
        tail = s[prev:].strip()
        if tail:
            out.append(tail)

    return [x for x in out if x and len(x) >= 10]


# -----------------------------
# Citation extractors
# -----------------------------
def extract_author_year_citations(text: str) -> List[str]:
    t = (text or "").replace("\u2019", "'")

    paren_pat = re.compile(r"\(([^()]{0,260}?\b(?:19|20)\d{2}[a-z]?\b[^()]{0,260}?)\)")

    NAME = r"[A-Z][A-Za-z'\-]+(?:'s)?"
    AMP = r"(?:&|and|＆)"
    AUTHOR_LIST = rf"{NAME}(?:\s*,\s*{NAME}){{0,10}}(?:\s*,?\s*{AMP}\s*{NAME})?"

    narr_pat = re.compile(
        rf"\b("
        rf"(?:{AUTHOR_LIST})"
        rf"|(?:{NAME}\s+{AMP}\s+{NAME})"
        rf"|(?:{NAME}\s+et\s+al\.)"
        rf")\s*\(\s*((?:19|20)\d{{2}}[a-z]?)\s*\)?"
    )

    out: List[str] = []

    for m in paren_pat.finditer(t):
        inside = (m.group(1) or "").strip()
        # IMPORTANT FIX: ignore bare "(2008)" captured inside a bigger parenthesis
        if YEAR_RE.fullmatch(inside) and not re.search(r"[A-Za-z]", inside):
            continue

        chunks = [c.strip() for c in inside.split(";") if c.strip()]
        for ch in chunks:
            ch2 = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", ch, flags=re.I).strip()
            if YEAR_RE.search(ch2):
                out.append(norm_space(ch2))

    for m in narr_pat.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        author = re.sub(r"(’s|'s)\b", "", author).strip()
        out.append(norm_space(f"{author}, {year}"))

    return [c for c in out if c]


def extract_numeric_citations(text: str, bracketed: bool = True) -> List[str]:
    t = text or ""
    out: List[str] = []
    if bracketed:
        pat = re.compile(r"\[\s*(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?\s*\]")
    else:
        pat = re.compile(r"\b(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?\b")

    for m in pat.finditer(t):
        a = int(m.group(1))
        b = m.group(2)
        if b:
            b2 = int(b)
            lo, hi = (a, b2) if a <= b2 else (b2, a)
            if hi - lo <= 50:
                for k in range(lo, hi + 1):
                    out.append(str(k))
            else:
                out.append(str(a))
                out.append(str(b2))
        else:
            out.append(str(a))
    return out


# -----------------------------
# Reference parsers
# -----------------------------
@dataclass
class RefAY:
    reference_full: str
    key: str


@dataclass
class RefNum:
    reference_full: str
    num: str


def parse_reference_author_year(ref: str) -> Optional[RefAY]:
    s = norm_space(ref)
    if not s:
        return None

    s_clean = _strip_leading_reference_number(s)

    if not _is_plausible_reference_entry(s_clean):
        return None

    m = re.search(r"\(\s*(" + YEAR + r")\s*\)", s_clean)
    if not m:
        m2 = re.search(r"\b(" + YEAR + r")\b", s_clean)
        if not m2:
            return None
        year = m2.group(1)
        left = s_clean[: m2.start()].strip()
    else:
        year = m.group(1)
        left = s_clean[: m.start()].strip()

    author_key = _first_author_or_org_key(left)
    if not author_key:
        return None

    key = f"{author_key}|{year}".lower()
    return RefAY(reference_full=s_clean, key=key)


def parse_reference_numeric(ref: str) -> Optional[RefNum]:
    s = norm_space(ref)
    if not s:
        return None

    m = re.match(r"^\[\s*(\d{1,4})\s*\]\s*(.+)$", s)
    if m:
        num = m.group(1)
        body = norm_space(m.group(2))
        body = _strip_leading_reference_number(body)
        if not _is_plausible_reference_entry(body):
            return None
        return RefNum(reference_full=s, num=num)

    m2 = re.match(r"^(\d{1,4})[\.)]\s*(.+)$", s)
    if m2 and not YEAR_RE.fullmatch(m2.group(1)):
        num = m2.group(1)
        body = norm_space(m2.group(2))
        body = _strip_leading_reference_number(body)
        if not _is_plausible_reference_entry(body):
            return None
        return RefNum(reference_full=s, num=num)

    return None


# -----------------------------
# Reconciliation
# -----------------------------
def _parse_author_year_from_cite(cite: str) -> Optional[Tuple[str, str]]:
    s = norm_space(cite)
    if not s:
        return None

    s = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", s, flags=re.I).strip()
    ym = YEAR_RE.search(s)
    if not ym:
        return None
    year = ym.group(1)

    left = s[: ym.start()].strip(" ,;()")

    if left:
        prefixes = sorted([re.escape(x) for x in DISCOURSE_PREFIXES], key=len, reverse=True)
        pref_re = re.compile(r"^(?:" + "|".join(prefixes) + r")\b", re.I)
        while True:
            new_left = pref_re.sub("", left).strip(" ,;()")
            if new_left == left:
                break
            left = new_left

    # Fix: "for instance, Adam & X" -> drop first clause if it has no names
    for _ in range(3):
        if "," not in left:
            break
        first, rest = left.split(",", 1)
        if re.search(r"\b[A-Z][A-Za-z'\-]+\b", first):
            break
        left = rest.strip(" ,;()")

    left = re.sub(r"(’s|'s)", "", left).strip()

    # commercial-grade: drop likely narrative/non-citation captures
    if _is_likely_narrative_citation(left, year, s):
        return None

    author_key = _first_author_or_org_key(left)
    if not author_key:
        return None
    if author_key.lower() in NON_NAME_AUTHOR_KEYS:
        return None
    return author_key, year



# -----------------------------
# Commercial-grade de-duplication for "Uncited" accuracy
# -----------------------------
_REF_STOPWORDS = {
    "the","a","an","and","or","of","in","on","for","to","with","from","at","by","as",
    "ed","eds","edition","vol","volume","no","number","pp","pages","page",
}

def _strip_accents(s: str) -> str:
    s = s or ""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))

def _norm_ref_text(s: str) -> str:
    s = _strip_accents(s.lower())
    s = s.replace("&", " and ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s)]+", re.I)

def _extract_ref_signature(ref_full: str) -> Tuple[str, str, str, str]:
    """
    Returns (year_base, first_author_key, title_stub, doi).
    Used for clustering near-duplicate reference entries.
    """
    s = ref_full or ""
    doi = ""
    mdoi = _DOI_RE.search(s)
    if mdoi:
        doi = mdoi.group(0).rstrip(".,;")

    m = YEAR_RE.search(s)
    if not m:
        # fall back: hash on first 80 chars
        t = _norm_ref_text(s)[:80]
        return ("", t[:24], t[24:60], doi)

    year = _base_year(m.group(1))
    left = (s[:m.start()] or "").strip(" ,;()")
    right = (s[m.end():] or "").strip()

    # first author key (surname or org token)
    surnames = _surnames_from_author_blob(left)
    first_author = surnames[0] if surnames else _norm_ref_text(left)[:24]
    first_author = re.sub(r"[^a-z0-9\- ]+", "", _norm_ref_text(first_author))

    # title stub: take a short token window after the year
    # remove leading punctuation and quotes
    right = right.lstrip(" .,:;)-–—\"'[]")
    # stop at the first strong separator that often ends titles
    right2 = re.split(r"\.\s+|\.?$|\s+https?://|\s+doi:\s*", right, maxsplit=1, flags=re.I)[0]
    tokens = [re.sub(r"[^a-z0-9\-]+", "", t) for t in _norm_ref_text(right2).split()]
    tokens = [t for t in tokens if t and t not in _REF_STOPWORDS]
    title_stub = " ".join(tokens[:12])  # short but stable
    return (year, first_author, title_stub, doi)

def _cluster_references(references: List[RefEntry]) -> Dict[str, Dict[str, Any]]:
    """
    Build clusters of near-duplicate references.
    Returns mapping: ref_full -> {cluster_id, canonical_ref, is_duplicate}
    """
    # Prefer RapidFuzz if present, otherwise fallback to overlap ratio
    try:
        from rapidfuzz import fuzz as _rfuzz
        _HAS_RF = True
    except Exception:
        _HAS_RF = False
        _rfuzz = None

    # group by (doi) first (strongest), then (year, first_author)
    by_doi: Dict[str, List[str]] = defaultdict(list)
    by_bucket: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    sigs: Dict[str, Tuple[str, str, str, str]] = {}

    for r in references:
        rf = r.reference_full
        y, a1, t, doi = _extract_ref_signature(rf)
        sigs[rf] = (y, a1, t, doi)
        if doi:
            by_doi[doi.lower()].append(rf)
        else:
            by_bucket[(y, a1)].append(rf)

    clusters: List[List[str]] = []

    # DOI clusters
    for _doi, items in by_doi.items():
        clusters.append(items)

    # Title-based clusters within bucket
    for (y, a1), items in by_bucket.items():
        if len(items) <= 1:
            clusters.append(items)
            continue

        used = set()
        for i, rf_i in enumerate(items):
            if rf_i in used:
                continue
            used.add(rf_i)
            _, _, ti, _ = sigs[rf_i]
            cluster = [rf_i]

            for rf_j in items[i+1:]:
                if rf_j in used:
                    continue
                _, _, tj, _ = sigs[rf_j]

                if not ti or not tj:
                    continue

                if _HAS_RF:
                    score = max(_rfuzz.token_set_ratio(ti, tj), _rfuzz.partial_ratio(ti, tj))
                else:
                    si = set(ti.split())
                    sj = set(tj.split())
                    score = int(round(100 * (len(si & sj) / max(1, len(si), len(sj)))))

                if score >= 88:  # high confidence near-duplicate titles
                    used.add(rf_j)
                    cluster.append(rf_j)

            clusters.append(cluster)

    # Build mapping
    mapping: Dict[str, Dict[str, Any]] = {}
    for cid, members in enumerate(clusters, start=1):
        # canonical = longest string (often most complete)
        canonical = max(members, key=lambda x: len(x or ""))
        for rf in members:
            mapping[rf] = {
                "cluster_id": cid,
                "canonical_ref": canonical,
                "is_duplicate": (rf != canonical),
            }
    return mapping


def reconcile_author_year(citations: List[str], references: List[RefAY]) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    # Robust reconciliation:
    # - supports year suffix mismatch (2008 vs 2008a)
    # - supports swapped first/second author (Adam & Tweneboah vs Tweneboah & Adam)
    # - supports organisation acronyms as authors (WHO/IMF etc.) via existing keying

    def _base_year(y: str) -> str:
        y = (y or "").strip()
        m = re.match(r"^((?:19|20)\d{2})", y)
        return m.group(1) if m else y

    def _surnames_from_author_blob(left: str) -> List[str]:
        # Extract likely surnames from the author part (before year).
        s = (left or "")
        s = re.sub(r"(’s|'s)\b", "", s)
        s = re.sub(r"\bet\s+al\.?\b", "", s, flags=re.I)
        s = s.replace("&", " and ")
        s = re.sub(r"\b(and|for|instance|see|e\.g\.|i\.e\.)\b", " ", s, flags=re.I)
        # remove initials like "A." "M."
        s = re.sub(r"\b[A-Z]\.\b", " ", s)
        s = re.sub(r"\b[A-Z]\b", " ", s)
        # keep word tokens
        tokens = re.findall(r"[A-Za-z][A-Za-z'\-]{1,}", s)
        out = []
        for tok in tokens:
            # skip common non-name words
            if tok.lower() in {"available", "ssrn", "university", "press", "journal"}:
                continue
            out.append(tok.lower())
        # de-duplicate but keep order
        seen = set()
        res = []
        for w in out:
            if w not in seen:
                seen.add(w)
                res.append(w)
        return res[:4]  # we only need a few

    # Build an alias -> reference map
    ref_map: Dict[str, str] = {r.key: r.reference_full for r in references}
    alias_map: Dict[str, str] = dict(ref_map)

    for r in references:
        # Add year-without-suffix alias for the canonical key
        try:
            auth, y = r.key.split("|", 1)
        except Exception:
            continue
        by = _base_year(y)
        if by and by != y:
            alias_map[f"{auth}|{by}".lower()] = r.reference_full

        # Add aliases from full reference text (handles swapped author order)
        s_full = r.reference_full
        ym = YEAR_RE.search(s_full)
        if not ym:
            continue
        year_full = ym.group(1)
        year_base = _base_year(year_full)

        left = s_full[: ym.start()].strip(" ,;()")
        names = _surnames_from_author_blob(left)
        if not names:
            continue

        # Single-name aliases
        for nm in names[:2]:
            alias_map[f"{nm}|{year_full}".lower()] = r.reference_full
            if year_base and year_base != year_full:
                alias_map[f"{nm}|{year_base}".lower()] = r.reference_full

        # Two-name combined aliases (order-insensitive)
        if len(names) >= 2:
            a, b = names[0], names[1]
            alias_map[f"{a}+{b}|{year_full}".lower()] = r.reference_full
            alias_map[f"{b}+{a}|{year_full}".lower()] = r.reference_full
            if year_base and year_base != year_full:
                alias_map[f"{a}+{b}|{year_base}".lower()] = r.reference_full
                alias_map[f"{b}+{a}|{year_base}".lower()] = r.reference_full

    # Parse citations and try multiple candidate keys
    cite_counts_by_ref = Counter()
    parsed_cites: List[Tuple[str, str, str]] = []  # (matched_ref_key, cite_str, flags)

    for c in citations:
        parsed = _parse_author_year_from_cite(c)
        if not parsed:
            continue
        auth, year = parsed
        year_base = _base_year(year)

        # candidate keys
        cand_keys = [f"{auth}|{year}".lower()]
        if year_base and year_base != year:
            cand_keys.append(f"{auth}|{year_base}".lower())

        # also attempt surname extraction from the raw citation string (captures "Adam & Tweneboah (2008)")
        ym = YEAR_RE.search(c)
        if ym:
            left = (c[: ym.start()] or "").strip(" ,;()")
            names = _surnames_from_author_blob(left)
            if names:
                cand_keys.append(f"{names[0]}|{ym.group(1)}".lower())
                if year_base and year_base != ym.group(1):
                    cand_keys.append(f"{names[0]}|{year_base}".lower())
                if len(names) >= 2:
                    cand_keys.append(f"{names[0]}+{names[1]}|{ym.group(1)}".lower())
                    cand_keys.append(f"{names[1]}+{names[0]}|{ym.group(1)}".lower())
                    if year_base and year_base != ym.group(1):
                        cand_keys.append(f"{names[0]}+{names[1]}|{year_base}".lower())
                        cand_keys.append(f"{names[1]}+{names[0]}|{year_base}".lower())

        matched_ref = ""
        matched_key = ""
        used = ""
        for k in cand_keys:
            if k in alias_map:
                matched_ref = alias_map[k]
                matched_key = k
                used = k
                break

        if matched_ref:
            cite_counts_by_ref[matched_ref] += 1
            parsed_cites.append((matched_ref, c, f"alias:{used}" if used else ""))
        else:
            parsed_cites.append(("", c, ""))

    # Build c2r + missing
    c2r: List[Dict[str, Any]] = []
    missing_counter = Counter()

    for matched_ref, c, flags in parsed_cites:
        if matched_ref:
            c2r.append({"status": "matched", "in_text": c, "matched_reference": matched_ref, "flags": flags})
        else:
            c2r.append({"status": "not_found", "in_text": c, "matched_reference": "", "flags": ""})
            missing_counter[c] += 1

    # Build r2c + uncited
    r2c: List[Dict[str, Any]] = []
    uncited_refs: List[str] = []

    cite_samples_by_ref: Dict[str, List[str]] = defaultdict(list)
    for matched_ref, c, _flags in parsed_cites:
        if matched_ref and len(cite_samples_by_ref[matched_ref]) < 6:
            cite_samples_by_ref[matched_ref].append(c)

    ref_cluster_map = _cluster_references(references)

    for r in references:
        ref_full = r.reference_full
        times = int(cite_counts_by_ref.get(ref_full, 0))

        meta = ref_cluster_map.get(ref_full) or {}
        canonical = meta.get("canonical_ref", ref_full)
        is_dup = bool(meta.get("is_duplicate", False))
        cid = meta.get("cluster_id", 0)

        canonical_times = int(cite_counts_by_ref.get(canonical, 0))
        if times == 0 and not (is_dup and canonical_times > 0):
            uncited_refs.append(ref_full)

        r2c.append({
            "times_cited": times,
            "reference": ref_full,
            "cited_by": cite_samples_by_ref.get(ref_full, []),
            "cluster_id": cid,
            "canonical_reference": canonical,
            "duplicate_of_cited": bool(is_dup and canonical_times > 0),
        })

    missing_rows = [{"citation_in_text": k, "count_in_text": int(v)} for k, v in missing_counter.most_common()]
    unique_intext_count = int(len(set([c for c in citations if c])))
    return c2r, r2c, missing_rows, uncited_refs, unique_intext_count
def reconcile_numeric(citations: List[str], references: List[RefNum]) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    ref_by_num: Dict[str, str] = {r.num: r.reference_full for r in references}
    cite_counts = Counter(citations)

    c2r: List[Dict[str, Any]] = []
    missing_counter = Counter()

    for num in citations:
        if num in ref_by_num:
            c2r.append({"status": "matched", "in_text": f"[{num}]", "matched_reference": ref_by_num[num], "flags": ""})
        else:
            c2r.append({"status": "not_found", "in_text": f"[{num}]", "matched_reference": "", "flags": ""})
            missing_counter[f"[{num}]"] += 1

    r2c: List[Dict[str, Any]] = []
    uncited_refs: List[str] = []
    for r in references:
        times = int(cite_counts.get(r.num, 0))
        if times == 0:
            uncited_refs.append(r.reference_full)
        r2c.append({
            "times_cited": times,
            "reference": r.reference_full,
            "cited_by": [f"[{r.num}]"] if times else [],
        })

    missing_rows = [{"citation_in_text": k, "count_in_text": int(v)} for k, v in missing_counter.most_common()]
    unique_intext_count = int(len(set(citations)))
    return c2r, r2c, missing_rows, uncited_refs, unique_intext_count


# -----------------------------
# Public API: run_crosscheck
# -----------------------------
def run_crosscheck(
    file_bytes: bytes,
    filename: str,
    style: str = "apa",
    verify_online: bool = False,  # kept for compatibility
    verify_mode: str = "all",
    max_verify: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> Dict[str, Any]:

    name = (filename or "").lower().strip()
    style_s = (style or "apa").strip().lower()

    is_numeric = ("ieee" in style_s) or ("vancouver" in style_s) or ("numeric" in style_s)
    style_hint = "numeric" if is_numeric else "apa"

    if name.endswith(".docx"):
        main_text, ref_block_lines, ref_msg = read_docx_split_main_and_refs(file_bytes)
        references_raw = _merge_reference_lines(ref_block_lines)
        if style_hint == "numeric":
            references_raw = _split_embedded_numeric_refs(references_raw)

    elif name.endswith(".pdf"):
        full_text = read_pdf_text(file_bytes)
        lines = full_text.splitlines()

        idx, tail = _find_reference_heading(lines, style_hint=style_hint)
        if idx == -1:
            main_text = full_text
            references_raw = []
            ref_msg = "No References heading found."
        else:
            main_text = "\n".join(lines[:idx]).strip()
            ref_msg = f"Found References heading: {lines[idx].strip()}"
            ref_block_lines: List[str] = []
            if tail:
                ref_block_lines.append(tail)
            ref_block_lines.extend([ln for ln in lines[idx + 1:] if ln.strip()])
            ref_block_lines = _truncate_reference_block(ref_block_lines, style_hint=style_hint)
            references_raw = _merge_reference_lines(ref_block_lines)
            if style_hint == "numeric":
                references_raw = _split_embedded_numeric_refs(references_raw)

    else:
        return {"error": "Upload a DOCX or PDF"}

    if len(main_text) > 350_000:
        half = 175_000
        main_text = main_text[:half] + "\n... [TRUNCATED] ...\n" + main_text[-half:]

    if style_hint == "apa":
        cites = extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]

        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_author_year(cites, refs)
        ref_count = len(refs)

    else:
        cites_nums = extract_numeric_citations(main_text, bracketed=True)
        if "vancouver" in style_s and len(cites_nums) < 3:
            cites_nums = extract_numeric_citations(main_text, bracketed=False)

        refs = [parse_reference_numeric(r) for r in references_raw]
        refs = [r for r in refs if r is not None]

        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(cites_nums, refs)
        ref_count = len(refs)

    missing_unique = int(len(missing_rows or []))
    match_rate = 0.0
    if intext_count > 0:
        match_rate = 100.0 * max(0.0, float(intext_count - missing_unique)) / float(intext_count)

    return {
        "filename": filename,
        "style": style_s,
        "verify_mode_used": (verify_mode or "all"),
        "reference_detection_message": ref_msg,
        "summary": {
            "in_text_citations_found": int(intext_count),
            "reference_entries_found": int(ref_count),
            "missing_in_references": int(missing_unique),
            "uncited_references": int(len(uncited_refs)),
            "match_rate": float(round(match_rate, 1)),
        },
        "missing_in_references": missing_rows,
        "uncited_references": uncited_refs,
        "reconciliation_intext_to_reference": c2r,
        "reconciliation_reference_to_intext": r2c,
        "references_raw": references_raw,
    }
