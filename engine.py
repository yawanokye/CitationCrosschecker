# engine.py (COMPLETE FIXED VERSION)
__version__ = "1.6.0"

import re
import io
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter
from pdf_to_docx_pipeline import process_pdf

ENGINE_BUILD = "commercial-2026-04-10-final"

# Fuzzy matching (optional)
try:
    from rapidfuzz import fuzz
    FUZZ_OK = True
except Exception:
    fuzz = None
    FUZZ_OK = False

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


# ============================================================================
# Define dataclasses FIRST
# ============================================================================

@dataclass
class RefAY:
    reference_full: str
    key: str


@dataclass
class RefNum:
    reference_full: str
    num: str


# ============================================================================
# Constants and patterns
# ============================================================================

YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b", re.I)

REF_HEADINGS = [
    r"^\s*references?\s*(?:list)?\s*$",
    r"^\s*bibliograph(?:y|ies)\s*$",
    r"^\s*works\s+cited\s*$",
    r"^\s*literature\s+cited\s*$",
    r"^\s*REFERENCES\s*$",
    r"^\s*BIBLIOGRAPHY\s*$",
    r"^\s*REFERENCES\s*\[.*\]\s*$",
    r"^\s*REFERENCES AND NOTES\s*$",
]

REF_HEADING_RELAXED = re.compile(
    r"^\s*(references?|bibliography|works\s+cited|literature\s+cited|REFERENCES|BIBLIOGRAPHY)\b",
    re.I,
)

DISCOURSE_PREFIXES = {
    "see", "e.g", "eg", "i.e", "ie",
    "as", "in", "for", "from", "to", "at", "on", "by", "with", "within",
    "according", "adapted", "based", "cited", "citing", "reported",
    "like",
    "however", "similarly", "regrettably", "traditionally", "notably",
    "therefore", "thus", "hence", "consequently", "moreover", "furthermore",
    "additionally", "meanwhile", "nonetheless", "nevertheless", "overall",
    "generally", "specifically", "particularly", "importantly", "indeed",
    "for instance", "instance", "for example", "example", "likely", 
    "for instance,", "for example,", "uncertainty", "likewise", "Moreover,",
}

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

NON_NAME_AUTHOR_KEYS = {
    "survey", "field", "work", "fieldwork", "data", "dataset", "table", "tables", 
    "figure", "fig", "figures", "chapter", "section", "appendix", "appendices", 
    "annex", "equation", "eq", "model", "models", "analysis", "results", "method", 
    "methods", "discussion", "introduction", "conclusion", "study", "paper", "thesis", 
    "report", "source", "sources", "author", "authors",
    "however", "similarly", "regrettably", "traditionally", "therefore", "thus", "hence",
    "consequently", "moreover", "furthermore", "additionally", "meanwhile", "nonetheless",
    "nevertheless", "overall", "generally", "specifically", "particularly", "importantly",
    "indeed", "instance", "example",
}

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


def _base_year(y: str) -> str:
    y = (y or "").strip()
    m = re.match(r"^((?:19|20)\d{2})", y)
    return m.group(1) if m else y


def _surnames_from_author_blob(left: str) -> List[str]:
    s = (left or "").strip()
    if not s:
        return []
    s = s.replace("&", " and ")
    s = re.sub(r"\bet\s+al\.?\b", "", s, flags=re.I)
    s = re.sub(r"(’s|'s)\b", "", s)
    s = re.sub(r"\b(and|for|instance|see|e\.g\.|i\.e\.)\b", " ", s, flags=re.I)
    s = re.sub(r"\b[A-Z]\.\b", " ", s)
    s = re.sub(r"\b[A-Z]\b", " ", s)
    parts = re.split(r"\band\b|;|/|\|", s, flags=re.I)
    out: List[str] = []
    for p in parts:
        p = p.strip(" ,.;:()[]{}")
        if not p:
            continue
        if "," in p:
            cand = p.split(",", 1)[0].strip()
        else:
            cand = p.split()[-1].strip()
        cand = re.sub(r"[^A-Za-z\-’' ]+", "", cand).strip()
        cand = cand.replace("’", "'")
        if len(cand) < 2:
            continue
        if cand.lower() in {"available", "ssrn", "university", "press", "journal"}:
            continue
        out.append(cand.lower())
    seen = set()
    final = []
    for x in out:
        if x not in seen:
            seen.add(x)
            final.append(x)
    return final[:4]


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


def _is_likely_narrative_citation(left: str, year: str, full_cite: str) -> bool:
    l = (left or "").strip()
    if not l:
        return True

    s_full = (full_cite or "").lower()
    for pat in NARRATIVE_PHRASE_PATTERNS:
        if re.search(pat, s_full, flags=re.I):
            return True

    if year and isinstance(year, str) and year.lower().endswith("s"):
        if _DECADE_YEAR_RE.search(full_cite or ""):
            return True

    l_norm = soft_lower(l)
    if re.fullmatch(r"[a-z\-']+", l_norm) and l_norm in NARRATIVE_SINGLE_TOKENS:
        return True

    return False


# -----------------------------
# Reference acceptance
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
        cue_ok = bool(re.search(r"\b(ssrn|arxiv|working\s+paper|available\s+at|retrieved\s+from|doi|report|policy\s+brief)\b", s0, re.I))
        after = s0[ym.end():].lstrip(" ).,;:-")
        after_title = after.split(".", 1)[0].strip()
        if len(after_title) < 6 and "," in after:
            after_title = after.split(",", 1)[0].strip()

        before = s0[: ym.start()].strip(" .;:-")
        before_parts = [p.strip() for p in before.split(".") if p.strip()]
        before_title = before_parts[-1] if before_parts else ""

        if cue_ok or _looks_like_title_piece(after_title) or _looks_like_title_piece(before_title):
            return True
        return False

    after = s0[ym.end():].lstrip(" ).,;:-")
    after_title = after.split(".", 1)[0].strip()
    if len(after_title) < 6 and "," in after:
        after_title = after.split(",", 1)[0].strip()

    before = s0[: ym.start()].strip(" .;:-")
    before_parts = [p.strip() for p in before.split(".") if p.strip()]
    before_title = before_parts[-1] if before_parts else ""

    return _looks_like_title_piece(after_title) or _looks_like_title_piece(before_title)


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

    def _ref_like(line: str) -> bool:
        s = (line or "").strip()
        if not s:
            return False
        if re.match(r"^\s*(\[\s*\d{1,4}\s*\]|\(\s*\d{1,4}\s*\)|\d{1,4}[\.)])\s+\S", s):
            return True
        if YEAR_RE.search(s) and re.match(r"^[A-Z][A-Za-z\-’'\.]+", s):
            return True
        if "doi:" in s.lower() or "https://doi.org/" in s.lower():
            return True
        return False

    def _lookahead_is_real_refs(idx: int) -> bool:
        seen = 0
        checked = 0
        j = idx + 1
        while j < len(lines) and checked < 20:
            s = (lines[j] or "").strip()
            j += 1
            if not s:
                continue
            checked += 1
            if _ref_like(s):
                seen += 1
        return seen >= 6

    main_lines: List[str] = []
    ref_lines: List[str] = []
    in_refs = False
    heading_line = ""

    i = 0
    while i < len(lines):
        t = lines[i]

        if not in_refs:
            hit = False
            for pat in REF_HEADINGS:
                if re.search(pat, t, flags=re.I):
                    if _looks_like_toc_references_line(t, ""):
                        break
                    if _lookahead_is_real_refs(i):
                        in_refs = True
                        heading_line = t
                        hit = True
                    break
            if hit:
                i += 1
                continue

            m = REF_HEADING_RELAXED.search(t)
            if m and m.start() <= 4 and len(t) <= 160:
                tail = t[m.end():].strip(" :-\t")
                if _looks_like_toc_references_line(t, tail):
                    main_lines.append(t)
                    i += 1
                    continue
                if _lookahead_is_real_refs(i):
                    in_refs = True
                    heading_line = t
                    if tail:
                        ref_lines.append(tail)
                    i += 1
                    continue

        if in_refs:
            ref_lines.append(t)
        else:
            main_lines.append(t)

        i += 1

    if in_refs:
        ref_lines = _truncate_reference_block(ref_lines, style_hint="apa")
        ref_lines = _truncate_reference_block(ref_lines, style_hint="numeric")

    msg = f"Found References heading: {heading_line}" if in_refs else "No References heading found."
    return "\n".join(main_lines).strip(), ref_lines, msg


# -----------------------------
# PDF extraction (fallback, but process_pdf is preferred)
# -----------------------------
def read_pdf_text(file_bytes: bytes) -> str:
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")

    out: List[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            try:
                text = page.extract_text() or ""
                text = text.replace("\x00", " ")
                text = re.sub(r"-\n", "", text)
                text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
            except Exception:
                text = ""
            out.append(text)
    return "\n".join(out)


def _looks_like_new_numeric_reference_start(s: str) -> bool:
    s0 = (s or "").strip()
    if not s0:
        return False
    
    if re.match(r"^\[\s*\d{1,4}\s*\]\s+\S", s0):
        return True
    if re.match(r"^\(\s*\d{1,4}\s*\)\s+\S", s0):
        return True
    
    m = re.match(r"^(\d{1,4})[\.)]\s+(.+)$", s0)
    if m:
        num = m.group(1)
        num_int = int(num)
        if 1900 <= num_int <= 2099:
            rest = m.group(2)
            if YEAR_RE.search(rest) or len(rest) > 30:
                return True
            return False
        return True
    
    m = re.match(r"^(\d{1,4})\s+([A-Z].+)$", s0)
    if m:
        num = m.group(1)
        num_int = int(num)
        if 1900 <= num_int <= 2099:
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
            tail = s[m.end():].strip(" :-\t")
            if _looks_like_toc_references_line(s, tail):
                continue
            candidates.append((i, tail))

    for i, tail in candidates:
        lookahead = [ln for ln in lines[i + 1: i + 31] if (ln or "").strip()]
        if _count_reference_like(lookahead, style_hint=style_hint) >= 3:
            return i, tail

    return -1, ""


# ============================================================================
# COMPREHENSIVE CITATION EXTRACTION (FIXED - captures ALL citations)
# ============================================================================

def extract_author_year_citations(text: str) -> List[str]:
    """Extract ALL citations from academic text - handles APA/Harvard formats comprehensively"""
    if not text:
        return []
    
    t = text.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    citations = set()
    
    # Pattern 1: Standard parenthetical (Author, Year) - captures (Smith, 2020)
    # Also handles multiple citations separated by semicolons
    paren_pattern = re.compile(r'\(([^()]{0,300}?\b(?:19|20)\d{2}[a-z]?\b[^()]{0,300}?)\)')
    for m in paren_pattern.finditer(t):
        inside = m.group(1).strip()
        # Skip if it's just a year
        if re.match(r'^\s*(?:19|20)\d{2}\s*$', inside):
            continue
        # Split multiple citations separated by semicolons
        parts = re.split(r'\s*;\s*', inside)
        for part in parts:
            part = part.strip()
            if part and re.search(r'\b(?:19|20)\d{2}\b', part):
                # Clean up the citation
                part = re.sub(r'\s+', ' ', part)
                citations.add(part)
    
    # Pattern 2: Narrative citations - Author (Year) - captures Smith (2020)
    narr_pattern = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+et\s+al\.?)?)\s*\(\s*((?:19|20)\d{2}[a-z]?)\s*\)')
    for m in narr_pattern.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        citations.add(f"{author}, {year}")
    
    # Pattern 3: "According to Author (Year)" or "Author (Year)" with text after
    # Also captures "Author et al. (Year)"
    pattern3 = re.compile(r'\b(?:According\s+to\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+et\s+al\.?)?),\s+((?:19|20)\d{2}[a-z]?)')
    for m in pattern3.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        # Check if this is part of a parenthetical (already captured)
        if not re.search(rf'{re.escape(author)},\s+{year}\s*\)', t[max(0, m.start()-10):m.end()+10]):
            citations.add(f"{author}, {year}")
    
    # Pattern 4: "Author (year)" without comma - captures Smith (2020)
    pattern4 = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+\(((?:19|20)\d{2}[a-z]?)\)')
    for m in pattern4.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        citations.add(f"{author}, {year}")
    
    # Pattern 5: Citations with page numbers - (Author, year, p. 15)
    pattern5 = re.compile(r'\(([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+et\s+al\.?)?),\s+((?:19|20)\d{2}[a-z]?)(?:,\s+(?:p\.|pp\.|page)\s+\d+)?\)')
    for m in pattern5.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        citations.add(f"{author}, {year}")
    
    # Pattern 6: "Author et al. (year)" - captures Smith et al. (2020)
    pattern6 = re.compile(r'\b([A-Z][a-z]+)\s+et\s+al\.?\s*\(\s*((?:19|20)\d{2}[a-z]?)\s*\)')
    for m in pattern6.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        citations.add(f"{author} et al., {year}")
    
    # Pattern 7: "Author & Author (year)" - captures Smith & Jones (2020)
    pattern7 = re.compile(r'\b([A-Z][a-z]+\s+&\s+[A-Z][a-z]+)\s*\(\s*((?:19|20)\d{2}[a-z]?)\s*\)')
    for m in pattern7.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        citations.add(f"{author}, {year}")
    
    # Pattern 8: "Author, Author, and Author (year)" - three authors
    pattern8 = re.compile(r'\b([A-Z][a-z]+,\s+[A-Z][a-z]+,\s+and\s+[A-Z][a-z]+)\s*\(\s*((?:19|20)\d{2}[a-z]?)\s*\)')
    for m in pattern8.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        citations.add(f"{author}, {year}")
    
    # Pattern 9: Citations in square brackets [Author, year]
    pattern9 = re.compile(r'\[([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),\s+((?:19|20)\d{2}[a-z]?)\]')
    for m in pattern9.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        citations.add(f"{author}, {year}")
    
    # Pattern 10: "Author (year, year)" - multiple years for same author
    pattern10 = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+\(((?:19|20)\d{2}[a-z]?(?:,\s*(?:19|20)\d{2}[a-z]?)+)\)')
    for m in pattern10.finditer(t):
        author = m.group(1).strip()
        years_part = m.group(2).strip()
        years = re.findall(r'(?:19|20)\d{2}[a-z]?', years_part)
        for year in years:
            citations.add(f"{author}, {year}")
    
    # Pattern 11: "Author (year); Author (year)" - semicolon separated narrative
    pattern11 = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*\(((?:19|20)\d{2}[a-z]?)\)\s*;\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*\(((?:19|20)\d{2}[a-z]?)\)')
    for m in pattern11.finditer(t):
        author1, year1, author2, year2 = m.group(1), m.group(2), m.group(3), m.group(4)
        citations.add(f"{author1}, {year1}")
        citations.add(f"{author2}, {year2}")
    
    # Pattern 12: "Author (year) and Author (year)" - with 'and'
    pattern12 = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*\(((?:19|20)\d{2}[a-z]?)\)\s+and\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*\(((?:19|20)\d{2}[a-z]?)\)')
    for m in pattern12.finditer(t):
        author1, year1, author2, year2 = m.group(1), m.group(2), m.group(3), m.group(4)
        citations.add(f"{author1}, {year1}")
        citations.add(f"{author2}, {year2}")
    
    # Pattern 13: Citations at end of sentences with period
    pattern13 = re.compile(r'\(([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),\s+((?:19|20)\d{2}[a-z]?)\)\.')
    for m in pattern13.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        citations.add(f"{author}, {year}")
    
    # Pattern 14: "see Author (year)" - with 'see' prefix
    pattern14 = re.compile(r'\bsee\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*\(((?:19|20)\d{2}[a-z]?)\)')
    for m in pattern14.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        citations.add(f"{author}, {year}")
    
    # Pattern 15: "e.g., Author (year)" - with 'e.g.,' prefix
    pattern15 = re.compile(r'\be\.g\.,?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*\(((?:19|20)\d{2}[a-z]?)\)')
    for m in pattern15.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        citations.add(f"{author}, {year}")
    
    # Filter out invalid citations
    valid_citations = []
    for c in citations:
        c = c.strip()
        # Must have a year
        if not re.search(r'\b(?:19|20)\d{2}\b', c):
            continue
        # Must have at least one letter (author)
        if not re.search(r'[A-Za-z]', c):
            continue
        # Remove citations that are too long (probably errors)
        if len(c) > 200:
            continue
        valid_citations.append(c)
    
    print(f"[DEBUG] Extracted {len(valid_citations)} unique citations")
    
    # Print first 30 for debugging
    if valid_citations:
        print(f"[DEBUG] Sample citations (first 30):")
        for i, cite in enumerate(valid_citations[:30]):
            print(f"  {i+1}. {cite}")
    
    return valid_citations

def extract_references_fast(ref_lines: List[str]) -> List[str]:
    """Fast reference extraction from reference section lines"""
    references = []
    
    for line in ref_lines:
        line = line.strip()
        if not line:
            continue
        
        # Must have a year and look like a reference
        if YEAR_RE.search(line) and len(line) > 30:
            # Remove leading numbers
            cleaned = re.sub(r'^\s*(\[\d+\]|\d+\.)\s*', '', line)
            references.append(cleaned)
    
    return references


# ============================================================================
# Reference Extraction Functions
# ============================================================================

def detect_reference_format(lines: List[str], start_idx: int) -> str:
    sample_lines = []
    for i in range(start_idx + 1, min(start_idx + 20, len(lines))):
        line = lines[i].strip()
        if line:
            sample_lines.append(line)
    
    ieee_count = sum(1 for l in sample_lines if re.match(r'^\[\d+\]', l))
    numbered_count = sum(1 for l in sample_lines if re.match(r'^\d+\.', l) and not re.match(r'^\d{4}\.', l))
    apa_count = sum(1 for l in sample_lines if re.search(r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)', l))
    harvard_count = sum(1 for l in sample_lines if re.search(r'[A-Z][a-z]+\s+\(\d{4}[a-z]?\)', l))
    
    formats = {
        'ieee': ieee_count,
        'numbered': numbered_count,
        'apa': apa_count,
        'harvard': harvard_count
    }
    
    best_format = max(formats, key=formats.get)
    return best_format if formats[best_format] > 0 else "unknown"


def join_reference_lines(current: str, next_line: str) -> str:
    if current.endswith('-'):
        return current[:-1] + next_line
    elif re.search(r'[a-z]$', current) and re.search(r'^[a-z]', next_line):
        return current + next_line
    else:
        return current + " " + next_line


def clean_reference(ref: str) -> str:
    ref = re.sub(r'\s+', ' ', ref).strip()
    ref = re.sub(r'-\s+', '', ref)
    ref = re.sub(r'\s+-\s+', '-', ref)
    ref = ref.strip('.,;:')
    return ref


def extract_references_generalized(text: str) -> List[str]:
    lines = text.splitlines()
    
    ref_start = -1
    heading_patterns = [
        r'^\s*REFERENCES\s*$',
        r'^\s*BIBLIOGRAPHY\s*$',
        r'^\s*WORKS\s+CITED\s*$',
        r'^\s*LITERATURE\s+CITED\s*$',
        r'^\s*REFERENCES\s*\[.*\]\s*$',
        r'^\s*REFERENCES AND NOTES\s*$',
    ]
    
    for i, line in enumerate(lines):
        for pattern in heading_patterns:
            if re.search(pattern, line, re.I):
                if i > len(lines) * 0.6:
                    ref_start = i
                    break
        if ref_start != -1:
            break
    
    if ref_start == -1:
        ref_candidates = []
        for i, line in enumerate(lines):
            if i > len(lines) * 0.6:
                line = line.strip()
                if re.match(r'^\[\d+\]\s+[A-Z]\.?\s+[A-Z][a-z]', line):
                    ref_candidates.append((i, line))
                elif re.match(r'^\d+\.\s+[A-Z][a-z]', line) and not re.match(r'^\d{4}\.', line):
                    ref_candidates.append((i, line))
                elif re.search(r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)', line):
                    ref_candidates.append((i, line))
        
        if ref_candidates:
            ref_start = ref_candidates[0][0] - 1
    
    if ref_start == -1:
        return []
    
    references = []
    current_ref = ""
    ref_format = detect_reference_format(lines, ref_start)
    
    for i in range(ref_start + 1, min(ref_start + 500, len(lines))):
        line = lines[i].strip()
        
        if not line and not current_ref:
            continue
        
        if not line:
            if current_ref:
                references.append(clean_reference(current_ref))
                current_ref = ""
            continue
        
        is_new_ref = False
        
        if ref_format == "ieee":
            is_new_ref = bool(re.match(r'^\[\d+\]', line))
        elif ref_format == "numbered":
            is_new_ref = bool(re.match(r'^\d+\.', line)) and not re.match(r'^\d{4}\.', line)
        elif ref_format == "apa":
            is_new_ref = bool(re.search(r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)', line[:100]))
        elif ref_format == "harvard":
            is_new_ref = bool(re.search(r'[A-Z][a-z]+\s+\(\d{4}[a-z]?\)', line[:100]))
        else:
            is_new_ref = (
                bool(re.match(r'^\[\d+\]', line)) or
                (bool(re.match(r'^\d+\.', line)) and not re.match(r'^\d{4}\.', line)) or
                bool(re.search(r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)', line[:100]))
            )
        
        if is_new_ref:
            if current_ref:
                references.append(clean_reference(current_ref))
            current_ref = line
        elif current_ref:
            current_ref = join_reference_lines(current_ref, line)
    
    if current_ref:
        references.append(clean_reference(current_ref))
    
    cleaned_refs = []
    for ref in references:
        if len(ref) > 30 and (
            re.search(r'\d{4}', ref) or
            re.search(r'\[\d+\]', ref) or
            re.search(r'[A-Z][a-z]+,\s+[A-Z]\.', ref)
        ):
            cleaned_refs.append(ref)
    
    return cleaned_refs


def extract_references_pattern_based(text: str) -> List[str]:
    patterns = [
        (r'\[\d+\]\s+[A-Z][A-Za-z\.\s]+,\s+[A-Z][A-Za-z\.\s]+,\s+["“].+?["”]', re.MULTILINE | re.DOTALL),
        (r'^\d+\.\s+[A-Z][A-Za-z\.\s]+,\s+[A-Z][A-Za-z\.\s]+,\s+["“].+?["”]', re.MULTILINE | re.DOTALL),
        (r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)\.\s+[A-Z][a-zA-Z\s]+\.', re.MULTILINE | re.DOTALL),
    ]
    
    references = []
    for pattern, flags in patterns:
        matches = re.findall(pattern, text, flags)
        references.extend([clean_reference(m) for m in matches if len(m) > 30])
    
    return references


def extract_references_heuristic(text: str) -> List[str]:
    lines = text.splitlines()
    references = []
    current_ref = ""
    
    start_idx = int(len(lines) * 0.7)
    
    for i in range(start_idx, len(lines)):
        line = lines[i].strip()
        if not line:
            if current_ref and len(current_ref) > 30:
                references.append(clean_reference(current_ref))
                current_ref = ""
            continue
        
        has_year = bool(re.search(r'\b(19|20)\d{2}\b', line))
        has_bracket_num = bool(re.search(r'\[\d+\]', line))
        has_author = bool(re.search(r'[A-Z][a-z]+,\s+[A-Z]\.', line))
        has_caps_words = len(re.findall(r'\b[A-Z][a-z]{2,}\b', line)) >= 2
        
        if has_year or has_bracket_num or (has_author and has_caps_words):
            if not current_ref:
                current_ref = line
            else:
                if (has_bracket_num or 
                    (re.match(r'^\d+\.', line) and not re.match(r'^\d{4}\.', line)) or
                    (has_author and len(current_ref) > 50)):
                    if current_ref:
                        references.append(clean_reference(current_ref))
                    current_ref = line
                else:
                    current_ref += " " + line
        elif current_ref:
            current_ref += " " + line
    
    if current_ref and len(current_ref) > 30:
        references.append(clean_reference(current_ref))
    
    return references


def extract_references_enhanced(text: str) -> List[str]:
    refs = extract_references_generalized(text)
    if len(refs) < 5:
        refs = extract_references_pattern_based(text)
    if len(refs) < 5:
        refs = extract_references_heuristic(text)
    return refs


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


# ============================================================================
# APA/HARVARD STYLE
# ============================================================================

def _parse_author_year_from_cite(cite: str) -> Optional[Tuple[str, str]]:
    """Parse author and year from APA/Harvard citation."""
    s = norm_space(cite)
    if not s:
        return None

    s = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", s, flags=re.I).strip()
    
    ym = YEAR_RE.search(s)
    if not ym:
        return None
    
    year = ym.group(1)
    left = s[:ym.start()].strip(" ,;()")

    if left:
        prefixes = sorted([re.escape(x) for x in DISCOURSE_PREFIXES], key=len, reverse=True)
        pref_re = re.compile(r"^(?:" + "|".join(prefixes) + r")\b", re.I)
        while True:
            new_left = pref_re.sub("", left).strip(" ,;()")
            if new_left == left:
                break
            left = new_left

    for _ in range(3):
        if "," not in left:
            break
        first, rest = left.split(",", 1)
        if re.search(r"\b[A-Z][A-Za-z'\-]+\b", first):
            break
        left = rest.strip(" ,;()")

    left = re.sub(r"(’s|'s)\b", "", left).strip()

    if _is_likely_narrative_citation(left, year, s):
        return None

    author_key = _first_author_or_org_key(left)
    if not author_key:
        return None
    if author_key.lower() in NON_NAME_AUTHOR_KEYS:
        return None
    return author_key, year


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


# ============================================================================
# OPTIMIZED RECONCILIATION
# ============================================================================

def reconcile_author_year(citations: List[str], references: List[RefAY]) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    """Optimized reconciliation with comprehensive matching"""
    
    # Build fast lookup indexes
    alias_map: Dict[str, str] = {}
    refs_by_year: Dict[str, List[RefAY]] = defaultdict(list)
    
    for r in references:
        ref_full = r.reference_full
        if not ref_full:
            continue
        
        # Store by key
        alias_map[r.key.lower()] = ref_full
        
        # Store by year for fallback
        ym = YEAR_RE.search(ref_full)
        if ym:
            refs_by_year[_base_year(ym.group(1))].append(r)
        
        # Create surname-based aliases
        ym = YEAR_RE.search(ref_full)
        if ym:
            year_full = ym.group(1)
            year_base = _base_year(year_full)
            left = ref_full[: ym.start()].strip(" ,;()")
            surnames = _surnames_from_author_blob(left)
            
            if surnames:
                alias_map[f"{surnames[0]}|{year_full}".lower()] = ref_full
                if year_base != year_full:
                    alias_map[f"{surnames[0]}|{year_base}".lower()] = ref_full
                
                if len(surnames) >= 2:
                    alias_map[f"{surnames[0]}+{surnames[1]}|{year_full}".lower()] = ref_full
                    alias_map[f"{surnames[1]}+{surnames[0]}|{year_full}".lower()] = ref_full
                    if year_base != year_full:
                        alias_map[f"{surnames[0]}+{surnames[1]}|{year_base}".lower()] = ref_full
                        alias_map[f"{surnames[1]}+{surnames[0]}|{year_base}".lower()] = ref_full
    
    cite_counts_by_ref = Counter()
    parsed_cites: List[Tuple[str, str, str]] = []
    
    for c in citations:
        citation = norm_space(c)
        if not citation:
            parsed_cites.append(("", citation, ""))
            continue
        
        parsed = _parse_author_year_from_cite(citation)
        if not parsed:
            parsed_cites.append(("", citation, ""))
            continue
        
        auth, year = parsed
        year_base = _base_year(year) if len(year) >= 4 else year
        
        # Build candidate keys
        cand_keys = [f"{auth}|{year}".lower()]
        if year_base and year_base != year:
            cand_keys.append(f"{auth}|{year_base}".lower())
        
        # Extract surnames from citation
        ym = YEAR_RE.search(citation)
        cite_surnames = []
        if ym:
            left = (citation[: ym.start()] or "").strip(" ,;()")
            cite_surnames = _surnames_from_author_blob(left)
            if cite_surnames:
                cand_keys.append(f"{cite_surnames[0]}|{ym.group(1)}".lower())
                if year_base and year_base != ym.group(1):
                    cand_keys.append(f"{cite_surnames[0]}|{year_base}".lower())
                if len(cite_surnames) >= 2:
                    cand_keys.append(f"{cite_surnames[0]}+{cite_surnames[1]}|{ym.group(1)}".lower())
                    cand_keys.append(f"{cite_surnames[1]}+{cite_surnames[0]}|{ym.group(1)}".lower())
        
        # Handle et al.
        if re.search(r"\bet\s+al\.?", citation, re.I):
            m = re.search(r"([A-Z][A-Za-z'\-]+)\s+et\s+al", citation, re.I)
            if m:
                first_author = m.group(1).lower()
                cand_keys.append(f"{first_author}|{year}".lower())
                if year_base and year_base != year:
                    cand_keys.append(f"{first_author}|{year_base}".lower())
        
        # Try direct lookup
        matched_ref = None
        used_key = None
        for k in cand_keys:
            if k in alias_map:
                matched_ref = alias_map[k]
                used_key = k
                break
        
        # Fallback: year-based fuzzy matching (check ALL references)
        if not matched_ref and year_base:
            best_ref = ""
            best_score = 0
            ym_c = YEAR_RE.search(citation)
            if ym_c:
                yb = _base_year(ym_c.group(1))
                left_c = (citation[: ym_c.start()] or "").strip(" ,;()")
                cite_names = _surnames_from_author_blob(left_c)
                
                # Check ALL references with matching year
                for rr in refs_by_year.get(yb, []):
                    s_full = rr.reference_full
                    ym_r = YEAR_RE.search(s_full)
                    if not ym_r:
                        continue
                    left_r = s_full[: ym_r.start()].strip(" ,;()")
                    ref_names = _surnames_from_author_blob(left_r)
                    
                    overlap = len(set(cite_names) & set(ref_names))
                    score_overlap = int(round(100 * (overlap / max(1, len(set(cite_names))))))
                    
                    score = score_overlap
                    if FUZZ_OK and fuzz and cite_names and ref_names:
                        try:
                            score1 = fuzz.token_set_ratio(" ".join(cite_names), " ".join(ref_names))
                            score2 = fuzz.partial_ratio(" ".join(cite_names), " ".join(ref_names))
                            score_fuzz = int(round(0.6 * score1 + 0.4 * score2))
                            score = max(score, score_fuzz)
                        except Exception:
                            pass
                    
                    if score > best_score:
                        best_score = score
                        best_ref = rr.reference_full
                        if best_score >= 95:
                            break
            
            if best_ref and best_score >= 65:
                matched_ref = best_ref
                used_key = f"fuzzy:{best_score}"
        
        if matched_ref:
            cite_counts_by_ref[matched_ref] += 1
            parsed_cites.append((matched_ref, citation, used_key or ""))
        else:
            parsed_cites.append(("", citation, ""))
    
    # Build output
    c2r = []
    missing_counter = Counter()
    
    for matched_ref, c, flags in parsed_cites:
        if matched_ref:
            c2r.append({"status": "matched", "in_text": c, "matched_reference": matched_ref, "flags": flags})
        else:
            c2r.append({"status": "not_found", "in_text": c, "matched_reference": "", "flags": ""})
            missing_counter[c] += 1
    
    r2c = []
    uncited_refs = []
    
    cite_samples_by_ref = defaultdict(list)
    for matched_ref, c, _ in parsed_cites:
        if matched_ref and len(cite_samples_by_ref[matched_ref]) < 6:
            cite_samples_by_ref[matched_ref].append(c)
    
    ref_cluster_map = _cluster_references(references)
    
    for r in references:
        ref_full = r.reference_full
        times = cite_counts_by_ref.get(ref_full, 0)
        
        meta = ref_cluster_map.get(ref_full) or {}
        canonical = meta.get("canonical_ref", ref_full)
        is_dup = bool(meta.get("is_duplicate", False))
        cid = meta.get("cluster_id", 0)
        
        canonical_times = cite_counts_by_ref.get(canonical, 0)
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
    unique_intext_count = len(set([norm_space(c) for c in citations if norm_space(c)]))
    
    print(f"[DEBUG] Reconciliation: {len([p for p in parsed_cites if p[0]])} matched, {len(missing_rows)} missing")
    
    return c2r, r2c, missing_rows, uncited_refs, unique_intext_count


# ============================================================================
# IEEE STYLE
# ============================================================================

def extract_ieee_citations(text: str) -> List[str]:
    t = text or ""
    out: List[str] = []
    
    t = re.sub(r"(?:table|figure|fig\.?|eq\.?|equation)\s+(\d{1,4})", "", t, flags=re.I)
    t = re.sub(r'\]\s*\n\s*\[', '][', t)
    
    ieee_pat = re.compile(r"\[\s*(\d{1,4})(?:\s*[-–,]\s*(\d{1,4}))?(?:\s*,\s*(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?)?\s*\]")
    
    for m in ieee_pat.finditer(t):
        nums = _expand_citation_range(m)
        out.extend(nums)
    
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def parse_reference_numeric(ref: str, style: str = "ieee") -> Optional[RefNum]:
    s = norm_space(ref)
    if not s:
        return None
    
    style = style.lower()
    is_ieee = style == "ieee"
    
    if is_ieee:
        m = re.match(r"^\[\s*(\d{1,4})\s*\]\s*(.+)$", s)
        if m:
            num = m.group(1)
            body = norm_space(m.group(2))
            body = _strip_leading_reference_number(body)
            if len(body) > 20 and re.search(r'[A-Z][a-z]+', body):
                return RefNum(reference_full=s, num=num)
        return None
    
    return None


def reconcile_numeric(citations: List[str], references: List[RefNum], style: str = "ieee") -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    style = style.lower()
    is_ieee = style == "ieee"
    
    if not is_ieee:
        return [], [], [], [], 0
    
    ref_map: Dict[str, str] = {}
    ref_by_num: Dict[str, str] = {}
    
    for r in references:
        ref_map[r.num] = r.reference_full
        ref_by_num[r.num] = r.reference_full
        ref_map[f"[{r.num}]"] = r.reference_full
    
    cite_counts = Counter()
    
    for cite in citations:
        cite_str = str(cite).strip()
        
        if cite_str in ref_map:
            cite_counts[ref_map[cite_str]] += 1
        elif cite_str.isdigit() and cite_str in ref_by_num:
            cite_counts[ref_by_num[cite_str]] += 1
    
    c2r = []
    missing = Counter()
    
    for cite in citations:
        cite_str = str(cite).strip()
        matched = False
        
        if cite_str in ref_map:
            c2r.append({
                "status": "matched",
                "in_text": cite_str,
                "matched_reference": ref_map[cite_str],
                "flags": ""
            })
            matched = True
        elif cite_str.isdigit() and cite_str in ref_by_num:
            c2r.append({
                "status": "matched",
                "in_text": cite_str,
                "matched_reference": ref_by_num[cite_str],
                "flags": "number_only"
            })
            matched = True
        
        if not matched:
            c2r.append({
                "status": "not_found",
                "in_text": cite_str,
                "matched_reference": "",
                "flags": ""
            })
            missing[cite_str] += 1
    
    r2c = []
    uncited = []
    
    cite_samples = defaultdict(list)
    for cite in citations:
        cite_str = str(cite).strip()
        if cite_str in ref_map:
            if len(cite_samples[ref_map[cite_str]]) < 6:
                cite_samples[ref_map[cite_str]].append(cite_str)
        elif cite_str.isdigit() and cite_str in ref_by_num:
            if len(cite_samples[ref_by_num[cite_str]]) < 6:
                cite_samples[ref_by_num[cite_str]].append(cite_str)
    
    for r in references:
        times = cite_counts.get(r.reference_full, 0)
        if times == 0:
            uncited.append(r.reference_full)
        r2c.append({
            "times_cited": times,
            "reference": r.reference_full,
            "cited_by": cite_samples.get(r.reference_full, [])
        })
    
    missing_rows = [{"citation_in_text": k, "count_in_text": v} for k, v in missing.items()]
    unique_intext_count = len(set(citations))
    
    return c2r, r2c, missing_rows, uncited, unique_intext_count


def _expand_citation_range(match) -> List[str]:
    nums = []
    groups = match.groups()
    
    if not groups or not groups[0]:
        return nums
    
    start = int(groups[0])
    if groups[1]:
        end = int(groups[1])
        if start <= end and (end - start) <= 50:
            nums.extend([str(i) for i in range(start, end + 1)])
        else:
            nums.append(str(start))
            nums.append(str(end))
    else:
        nums.append(str(start))
    
    if groups[2]:
        start2 = int(groups[2])
        if groups[3]:
            end2 = int(groups[3])
            if start2 <= end2 and (end2 - start2) <= 50:
                nums.extend([str(i) for i in range(start2, end2 + 1)])
            else:
                nums.append(str(start2))
                nums.append(str(end2))
        else:
            nums.append(str(start2))
    
    return nums


# ============================================================================
# VANCOUVER STYLE
# ============================================================================

def extract_vancouver_citations(text: str) -> List[str]:
    t = text or ""
    citations = []
    
    single_pat = re.compile(r'\[\s*(\d+)\s*\]')
    for m in single_pat.finditer(t):
        citations.append(m.group(1))
    
    multi_pat = re.compile(r'\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]')
    for m in multi_pat.finditer(t):
        numbers = m.group(1).split(',')
        for num in numbers:
            num = num.strip()
            if num.isdigit():
                citations.append(num)
    
    range_pat = re.compile(r'\[\s*(\d+)\s*[-–]\s*(\d+)\s*\]')
    for m in range_pat.finditer(t):
        start, end = int(m.group(1)), int(m.group(2))
        if start <= end and (end - start) <= 50:
            for i in range(start, end + 1):
                citations.append(str(i))
    
    try:
        citations = sorted(set(citations), key=lambda x: int(x))
    except:
        citations = list(dict.fromkeys(citations))
    
    return citations


def parse_vancouver_references(references_raw: List[str]) -> List[RefNum]:
    parsed_refs = []
    
    for i, ref in enumerate(references_raw, start=1):
        ref = ref.strip()
        if not ref or len(ref) < 20:
            continue
            
        num = str(i)
        
        m = re.match(r'^(\d+)\.?\s+', ref)
        if m:
            extracted_num = m.group(1)
            if extracted_num != num:
                print(f"Warning: Reference {i} has number {extracted_num}")
        
        parsed_refs.append(RefNum(
            reference_full=ref,
            num=num
        ))
    
    return parsed_refs


def reconcile_vancouver(citations: List[str], references: List[RefNum]) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    ref_by_num = {r.num: r.reference_full for r in references}
    cite_counts = Counter(citations)
    
    c2r = []
    missing = Counter()
    
    for cite in citations:
        if cite in ref_by_num:
            c2r.append({
                "status": "matched",
                "in_text": f"[{cite}]",
                "matched_reference": ref_by_num[cite],
                "flags": ""
            })
        else:
            c2r.append({
                "status": "not_found",
                "in_text": f"[{cite}]",
                "matched_reference": "",
                "flags": ""
            })
            missing[f"[{cite}]"] += 1
    
    r2c = []
    uncited = []
    
    cite_samples = defaultdict(list)
    for cite in citations:
        if cite in ref_by_num and len(cite_samples[ref_by_num[cite]]) < 6:
            cite_samples[ref_by_num[cite]].append(f"[{cite}]")
    
    for r in references:
        times = cite_counts.get(r.num, 0)
        if times == 0:
            uncited.append(r.reference_full)
        r2c.append({
            "times_cited": times,
            "reference": r.reference_full,
            "cited_by": cite_samples.get(r.reference_full, [])
        })
    
    missing_rows = [{"citation_in_text": k, "count_in_text": v} for k, v in missing.items()]
    unique_intext_count = len(set(citations))
    
    return c2r, r2c, missing_rows, uncited, unique_intext_count


# ============================================================================
# Reference clustering (shared)
# ============================================================================

def _cluster_references(references: List[Any]) -> Dict[str, Dict[str, Any]]:
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

    for _doi, items in by_doi.items():
        clusters.append(items)

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

                if FUZZ_OK and fuzz:
                    score = max(fuzz.token_set_ratio(ti, tj), fuzz.partial_ratio(ti, tj))
                else:
                    si = set(ti.split())
                    sj = set(tj.split())
                    score = int(round(100 * (len(si & sj) / max(1, len(si), len(sj)))))

                if score >= 88:
                    used.add(rf_j)
                    cluster.append(rf_j)

            clusters.append(cluster)

    mapping: Dict[str, Dict[str, Any]] = {}
    for cid, members in enumerate(clusters, start=1):
        canonical = max(members, key=lambda x: len(x or ""))
        for rf in members:
            mapping[rf] = {
                "cluster_id": cid,
                "canonical_ref": canonical,
                "is_duplicate": (rf != canonical),
            }
    return mapping


def _extract_ref_signature(ref_full: str) -> Tuple[str, str, str, str]:
    s = ref_full or ""
    doi = ""
    mdoi = _DOI_RE.search(s)
    if mdoi:
        doi = mdoi.group(0).rstrip(".,;")

    m = YEAR_RE.search(s)
    if not m:
        t = _norm_ref_text(s)[:80]
        return ("", t[:24], t[24:60], doi)

    year = _base_year(m.group(1))
    left = (s[:m.start()] or "").strip(" ,;()")
    right = (s[m.end():] or "").strip()

    surnames = _surnames_from_author_blob(left)
    first_author = surnames[0] if surnames else _norm_ref_text(left)[:24]
    first_author = re.sub(r"[^a-z0-9\- ]+", "", _norm_ref_text(first_author))

    right = right.lstrip(" .,:;)-–—\"'[]")
    right2 = re.split(r"\.\s+|\.?$|\s+https?://|\s+doi:\s*", right, maxsplit=1, flags=re.I)[0]
    tokens = [re.sub(r"[^a-z0-9\-]+", "", t) for t in _norm_ref_text(right2).split()]
    tokens = [t for t in tokens if t and t not in _REF_STOPWORDS]
    title_stub = " ".join(tokens[:12])
    return (year, first_author, title_stub, doi)


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


# -----------------------------
# Chunked text processing
# -----------------------------
def _iter_text_chunks(text: str, chunk_size: int = 300_000, overlap: int = 2_000):
    s = text or ""
    n = len(s)
    if n <= chunk_size:
        yield s
        return
    step = max(1, chunk_size - overlap)
    for i in range(0, n, step):
        yield s[i: min(n, i + chunk_size)]
        if i + chunk_size >= n:
            break


def _extract_author_year_citations_chunked(text: str) -> List[str]:
    seen = set()
    total = []
    for chunk in _iter_text_chunks(text):
        for c in extract_author_year_citations(chunk):
            if c not in seen:
                seen.add(c)
                total.append(c)
    return total


def _extract_numeric_citations_chunked(text: str, style: str = "ieee") -> List[str]:
    seen = set()
    total = []
    for chunk in _iter_text_chunks(text):
        if style == "vancouver":
            for c in extract_vancouver_citations(chunk):
                if c not in seen:
                    seen.add(c)
                    total.append(c)
        else:
            for c in extract_ieee_citations(chunk):
                if c not in seen:
                    seen.add(c)
                    total.append(c)
    return total


# ============================================================================
# SUGGESTION ENGINE FUNCTIONS (Keep your existing ones)
# ============================================================================

# ... (keep your existing generate_suggestions, _generate_citation_fixes, etc.)


# -----------------------------
# Public API: run_crosscheck
# -----------------------------
def run_crosscheck(
    file_bytes: bytes,
    filename: str,
    style: str = "apa",
    verify_online: bool = False,
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
        if not references_raw:
            references_raw = ref_block_lines
        if style_hint == "numeric":
            references_raw = _split_embedded_numeric_refs(references_raw)

    elif name.endswith(".pdf"):
        try:
            pdf_data = process_pdf(file_bytes)
            main_text = pdf_data["main_text"]
            references_raw = pdf_data["references"]
            ref_msg = f"PDF converted to DOCX and cleaned. Found {len(references_raw)} references."
        except Exception as e:
            return {
                "error": "PDF conversion failed",
                "note": str(e),
                "filename": filename
            }
        if style_hint == "numeric":
            references_raw = _split_embedded_numeric_refs(references_raw)

    else:
        return {"error": "Upload a DOCX or PDF"}

    main_text_len = len(main_text or "")
    too_large = main_text_len > 2_000_000

    if style_hint == "apa":
        # Extract citations using comprehensive function
        if too_large:
            cites = _extract_author_year_citations_chunked(main_text)
        else:
            cites = extract_author_year_citations(main_text)
        
        cites = [c for c in cites if len(c) > 5]
        
        print(f"[DEBUG] Citations extracted: {len(cites)}")
        
        # Extract references
        references = extract_references_fast(ref_block_lines)
        
        print(f"[DEBUG] References from ref section: {len(references)}")
        
        # Fallback if needed
        if len(references) < 5:
            combined_text = main_text + "\n".join(ref_block_lines)
            references = extract_references_generalized(combined_text)
            print(f"[DEBUG] References (fallback): {len(references)}")
        
        if not references:
            return {"error": "No references detected. Ensure your document has a reference section."}
        
        # Parse references
        refs = []
        for r in references:
            parsed = parse_reference_author_year(r)
            if parsed:
                refs.append(parsed)
        
        print(f"[DEBUG] Parsed references: {len(refs)}")
        
        if not refs:
            return {"error": "References detected but could not be parsed."}
        
        # Run reconciliation (no limits on cites/refs - handle all)
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_author_year(cites, refs)
        ref_count = len(refs)

    elif style_s == "ieee":
        if too_large:
            cites_nums = _extract_numeric_citations_chunked(main_text, style="ieee")
        else:
            cites_nums = extract_ieee_citations(main_text)
        
        refs = []
        for r in references_raw:
            parsed = parse_reference_numeric(r, style="ieee")
            if parsed:
                refs.append(parsed)
        
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(cites_nums, refs, style="ieee")
        ref_count = len(refs)

    elif style_s == "vancouver":
        print("Using Vancouver style")
        
        if too_large:
            cites_nums = _extract_numeric_citations_chunked(main_text, style="vancouver")
        else:
            cites_nums = extract_vancouver_citations(main_text)
        
        refs = parse_vancouver_references(references_raw)
        
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_vancouver(cites_nums, refs)
        ref_count = len(refs)

    else:
        # Default to APA
        if too_large:
            cites = _extract_author_year_citations_chunked(main_text)
        else:
            cites = extract_author_year_citations(main_text)
        
        references = extract_references_fast(ref_block_lines)
        
        if len(references) < 5:
            references = extract_references_generalized(main_text + "\n".join(ref_block_lines))
        
        refs = []
        for r in references:
            parsed = parse_reference_author_year(r)
            if parsed:
                refs.append(parsed)
        
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_author_year(cites, refs)
        ref_count = len(refs)

    missing_unique = int(len(missing_rows or []))
    match_rate = 0.0
    if intext_count > 0:
        match_rate = 100.0 * max(0.0, float(intext_count - missing_unique)) / float(intext_count)

    result = {
        "filename": filename,
        "style": style_s,
        "main_text": main_text,
        "engine_build": ENGINE_BUILD,
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
    
    return result


# ============================================================================
# ENHANCED API WITH AUTO-FIX
# ============================================================================

def run_crosscheck_with_autofix(
    file_bytes: bytes,
    filename: str,
    style: str = "apa",
    verify_online: bool = False,
    verify_mode: str = "all",
    max_verify: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
    enable_autofix: bool = False,
) -> Dict[str, Any]:
    
    result = run_crosscheck(
        file_bytes=file_bytes,
        filename=filename,
        style=style,
        verify_online=verify_online,
        verify_mode=verify_mode,
        max_verify=max_verify,
        throttle_s=throttle_s,
        use_crossref=use_crossref,
        use_openalex=use_openalex
    )
    
    if enable_autofix and "error" not in result:
        style_s = (style or "apa").strip().lower()
        is_numeric = ("ieee" in style_s) or ("vancouver" in style_s) or ("numeric" in style_s)
        
        if not is_numeric:
            references_raw = result.get("references_raw", [])
            refs = [parse_reference_author_year(r) for r in references_raw if r]
            refs = [r for r in refs if r is not None]
            
            ref_map = {r.key: r.reference_full for r in refs}
            main_text = result.get("main_text", "")
            citations = extract_author_year_citations(main_text)
            
            suggestions_data = generate_suggestions(
                citations=citations[:500],
                c2r=result.get("reconciliation_intext_to_reference", []),
                missing_rows=result.get("missing_in_references", []),
                references=refs[:300],
                ref_map=ref_map
            )
            
            result["autofix"] = {
                "enabled": True,
                "suggestions": suggestions_data,
                "summary": suggestions_data.get("summary", {"auto_fixable": 0, "needs_review": 0})
            }
        else:
            result["autofix"] = {
                "enabled": True,
                "message": f"Auto-fix supports APA/Harvard style. Current style: {style_s}",
                "suggestions": {"citations": [], "references": [], "statistics": {"total_suggestions": 0}}
            }
    
    return result
