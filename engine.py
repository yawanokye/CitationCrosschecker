# engine.py (COMPLETE - with non-invasive Suggestion Engine)
__version__ = "1.5.0"

import re
import io
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter
from pdf_to_docx_pipeline import process_pdf

ENGINE_BUILD = "commercial-2026-03-01-final"

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
        return seen >= 2

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
    """Parse author and year from APA/Harvard citation.
    
    Now handles malformed years (2-3 digits like '204' -> '2024')
    """
    s = norm_space(cite)
    if not s:
        return None

    s = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", s, flags=re.I).strip()
    
    # First try to match standard 4-digit years
    ym = YEAR_RE.search(s)
    year = None
    year_start = None
    
    if ym:
        year = ym.group(1)
        year_start = ym.start()
    else:
        # Try to find malformed years (2-3 digit numbers that could be years)
        malformed_pat = re.compile(r"[,&]\s*([A-Za-z\s]+?)?\s*(\d{2,3})\s*[\),]")
        malformed_match = malformed_pat.search(s)
        
        if malformed_match:
            year_candidate = malformed_match.group(2)
            if year_candidate.isdigit() and 0 <= int(year_candidate) <= 999:
                year = year_candidate
                year_start = malformed_match.start(2)
        
        if not year:
            standalone_pat = re.compile(r"\b(\d{2,3})\b")
            standalone_match = standalone_pat.search(s)
            if standalone_match:
                year_candidate = standalone_match.group(1)
                if year_candidate.isdigit() and 0 <= int(year_candidate) <= 999:
                    year = year_candidate
                    year_start = standalone_match.start(1)
    
    if not year:
        return None
    
    if year_start:
        left = s[:year_start].strip(" ,;()")
    else:
        left = ""

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
        if YEAR_RE.fullmatch(inside) and not re.search(r"[A-Za-z]", inside):
            continue

        chunks = [c.strip() for c in inside.split(";") if c.strip()]
        for ch in chunks:
            ch2 = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", ch, flags=re.I).strip()
            if YEAR_RE.search(ch2):
                out.append(norm_space(ch2))

    for m in narr_pat.finditer(t):
        author = m.group(1).strip()
        years_block = m.group(2).strip()
    
        if not years_block:
            continue
    
        author = re.sub(r"(’s|'s)\b", "", author).strip()
        years = re.split(r"[;,]\s*", years_block)
    
        for y in years:
            y = y.strip()
            if YEAR_RE.fullmatch(y):
                out.append(norm_space(f"{author}, {y}"))

    return [c for c in out if c]


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


def reconcile_author_year(citations: List[str], references: List[RefAY]) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    ref_map: Dict[str, str] = {r.key: r.reference_full for r in references}
    alias_map: Dict[str, str] = dict(ref_map)

    refs_by_year: Dict[str, List[RefAY]] = defaultdict(list)
    for r in references:
        ym_r = YEAR_RE.search(r.reference_full)
        if ym_r:
            refs_by_year[_base_year(ym_r.group(1))].append(r)

    for r in references:
        try:
            auth, y = r.key.split("|", 1)
        except Exception:
            continue
        by = _base_year(y)
        if by and by != y:
            alias_map[f"{auth}|{by}".lower()] = r.reference_full

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

        for nm in names[:2]:
            alias_map[f"{nm}|{year_full}".lower()] = r.reference_full
            if year_base and year_base != year_full:
                alias_map[f"{nm}|{year_base}".lower()] = r.reference_full

        if len(names) >= 2:
            a, b = names[0], names[1]
            alias_map[f"{a}+{b}|{year_full}".lower()] = r.reference_full
            alias_map[f"{b}+{a}|{year_full}".lower()] = r.reference_full
            if year_base and year_base != year_full:
                alias_map[f"{a}+{b}|{year_base}".lower()] = r.reference_full
                alias_map[f"{b}+{a}|{year_base}".lower()] = r.reference_full

    cite_counts_by_ref = Counter()
    parsed_cites: List[Tuple[str, str, str]] = []

    for c in citations:
        parsed = _parse_author_year_from_cite(c)
        if not parsed:
            continue
        
        auth, year = parsed
        year_base = _base_year(year) if len(year) == 4 else year
        
        cand_keys = [f"{auth}|{year}".lower()]
        
        if re.search(r"\bet\s+al\.?", c, re.I):
            m = re.search(r'([A-Z][A-Za-z\'\-]+)\s+et\s+al', c, re.I)
            if m:
                first_author = m.group(1).lower()
                cand_keys.append(f"{first_author}|{year}".lower())
                if year_base and year_base != year:
                    cand_keys.append(f"{first_author}|{year_base}".lower())

        if year_base and year_base != year:
            cand_keys.append(f"{auth}|{year_base}".lower())

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

        matched_ref = None
        used_key = None
        for k in cand_keys:
            if k in alias_map:
                matched_ref = alias_map[k]
                used_key = k
                break

        if matched_ref:
            cite_counts_by_ref[matched_ref] += 1
            parsed_cites.append((matched_ref, c, f"alias:{used_key}" if used_key else ""))
        else:
            best_ref = ""
            best_score = 0
            ym_c = YEAR_RE.search(c)
            if ym_c:
                yb = _base_year(ym_c.group(1))
                left_c = (c[: ym_c.start()] or "").strip(" ,;()")
                cite_names = _surnames_from_author_blob(left_c)

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
                        score1 = fuzz.token_set_ratio(" ".join(cite_names), " ".join(ref_names))
                        score2 = fuzz.partial_ratio(" ".join(cite_names), " ".join(ref_names))
                        score_fuzz = int(round(0.6 * score1 + 0.4 * score2))
                        score = max(score, score_fuzz)

                    if score > best_score:
                        best_score = score
                        best_ref = rr.reference_full

            if best_ref and best_score >= 74:
                cite_counts_by_ref[best_ref] += 1
                parsed_cites.append((best_ref, c, f"fuzzy:{best_score}"))
            else:
                parsed_cites.append(("", c, ""))

    c2r: List[Dict[str, Any]] = []
    missing_counter = Counter()

    for matched_ref, c, flags in parsed_cites:
        if matched_ref:
            c2r.append({"status": "matched", "in_text": c, "matched_reference": matched_ref, "flags": flags})
        else:
            c2r.append({"status": "not_found", "in_text": c, "matched_reference": "", "flags": ""})
            missing_counter[c] += 1

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
# VANCOUVER STYLE - SIMPLE SEQUENTIAL
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
# SUGGESTION ENGINE (NON-INVASIVE - FINAL)
# ============================================================================

@dataclass
class FixSuggestion:
    original: str
    suggested: str
    fix_type: str
    confidence: float
    reason: str


# ============================================================
# CORE CITATION SUGGESTION ENGINE
# ============================================================

def generate_citation_suggestions(
    citations: List[str],
    references: List[RefAY],
    ref_map: Dict[str, str]
) -> List[Dict[str, Any]]:
    """
    Generate NON-INVASIVE citation suggestions.
    No modification of original text.
    """

    suggestions = []
    seen = set()

    for citation in citations:
        if citation in seen:
            continue
        seen.add(citation)

        suggestion = _generate_citation_fixes(
            citation,
            references,
            ref_map
        )

        if suggestion:
            suggestions.append({
                "citation": suggestion.original,
                "suggested": suggestion.suggested,
                "type": suggestion.fix_type,
                "confidence": suggestion.confidence,
                "reason": suggestion.reason,
                "action": "review_required"
            })

    return suggestions


# ============================================================
# REFERENCE SUGGESTION ENGINE
# ============================================================

def generate_reference_suggestions(
    references: List[RefAY]
) -> List[Dict[str, Any]]:
    suggestions = []

    for ref in references:
        ref_suggestions = _generate_reference_fixes(ref)

        for s in ref_suggestions:
            suggestions.append({
                "original": s.original,
                "suggested": s.suggested,
                "type": s.fix_type,
                "confidence": s.confidence,
                "reason": s.reason,
                "action": "optional_fix"
            })

    return suggestions


# ============================================================
# MASTER SUGGESTION ENGINE
# ============================================================

def generate_suggestions(
    citations: List[str],
    c2r: List[Dict[str, Any]],
    missing_rows: List[Dict[str, Any]],
    references: List[RefAY],
    ref_map: Dict[str, str]
) -> Dict[str, Any]:
    """
    Master Suggestion Engine:
    - Non-invasive
    - Structured
    - UI-ready
    """

    # 1. Direct citation suggestions
    citation_suggestions = generate_citation_suggestions(
        citations,
        references,
        ref_map
    )

    # 2. Missing citation suggestions
    missing_suggestions = []
    seen_missing = set()

    for missing in missing_rows:
        citation = missing.get("citation_in_text", "")
        if citation and citation not in seen_missing:
            seen_missing.add(citation)

            suggestion = _generate_citation_fixes(
                citation,
                references,
                ref_map
            )

            if suggestion:
                missing_suggestions.append({
                    "citation": suggestion.original,
                    "suggested": suggestion.suggested,
                    "type": suggestion.fix_type,
                    "confidence": suggestion.confidence,
                    "reason": suggestion.reason,
                    "action": "add_reference"
                })

    # 3. Unmatched citations in c2r
    unmatched_suggestions = []
    seen_unmatched = set()

    for item in c2r:
        if item.get("status") == "not_found":
            citation = item.get("in_text", "")
            if citation and citation not in seen_unmatched:
                seen_unmatched.add(citation)

                suggestion = _generate_citation_fixes(
                    citation,
                    references,
                    ref_map
                )

                if suggestion:
                    unmatched_suggestions.append({
                        "citation": suggestion.original,
                        "suggested": suggestion.suggested,
                        "type": suggestion.fix_type,
                        "confidence": suggestion.confidence,
                        "reason": suggestion.reason,
                        "action": "review_required"
                    })

    # 4. Reference suggestions
    reference_suggestions = generate_reference_suggestions(references)

    # ============================================================
    # STATISTICS
    # ============================================================

    all_suggestions = (
        citation_suggestions +
        missing_suggestions +
        unmatched_suggestions +
        reference_suggestions
    )

    high_conf = [s for s in all_suggestions if s["confidence"] >= 0.85]
    med_conf = [s for s in all_suggestions if 0.70 <= s["confidence"] < 0.85]
    low_conf = [s for s in all_suggestions if s["confidence"] < 0.70]

    by_type = {}
    for s in all_suggestions:
        by_type[s["type"]] = by_type.get(s["type"], 0) + 1

    # ============================================================
    # FINAL OUTPUT
    # ============================================================

    return {
        "citations": citation_suggestions,
        "missing": missing_suggestions,
        "unmatched": unmatched_suggestions,
        "references": reference_suggestions,

        "statistics": {
            "total": len(all_suggestions),
            "high_confidence": len(high_conf),
            "medium_confidence": len(med_conf),
            "low_confidence": len(low_conf),
            "by_type": by_type
        },

        "summary": {
            "auto_fixable": len(high_conf),
            "needs_review": len(med_conf) + len(low_conf)
        }
    }


# ============================================================
# HELPER FUNCTIONS FOR SUGGESTIONS (PRESERVED FROM ORIGINAL)
# ============================================================

def _generate_citation_fixes(
    citation: str, 
    references: List[RefAY],
    ref_map: Dict[str, str]
) -> Optional[FixSuggestion]:
    """Generate fix suggestions for problematic citations."""
    
    parsed = _parse_author_year_from_cite(citation)
    if not parsed:
        return None
    
    auth, year = parsed
    
    # Case 0: Fix malformed year (204 -> 2024)
    if len(year) < 4 and year.isdigit():
        year_int = int(year)
        possible_years = []
        
        if len(year) == 3:
            possible_years = [
                2000 + year_int,
                2000 + year_int + 10,
                2000 + year_int + 20,
                1900 + year_int,
            ]
        elif len(year) == 2:
            possible_years = [2000 + year_int, 1900 + year_int]
        elif len(year) == 1:
            possible_years = [2000 + year_int, 2000 + year_int + 10, 2000 + year_int + 20]
        
        author_variations = [auth]
        if ' & ' in auth:
            parts = auth.split(' & ')
            author_variations.extend(parts)
        if ' and ' in auth:
            parts = auth.split(' and ')
            author_variations.extend(parts)
        
        for alt_year in possible_years:
            alt_year_str = str(alt_year)
            for test_auth in author_variations:
                test_auth = test_auth.strip()
                if not test_auth:
                    continue
                alt_key = f"{test_auth}|{alt_year_str}".lower()
                if alt_key in ref_map:
                    alt_citation = re.sub(r'\b' + re.escape(year) + r'\b', alt_year_str, citation)
                    return FixSuggestion(
                        original=citation,
                        suggested=alt_citation,
                        fix_type="year_malformed",
                        confidence=0.90,
                        reason=f"Malformed year '{year}' corrected to '{alt_year_str}' based on reference for '{test_auth}'"
                    )
    
    # Case 1: Year typo (off by 1 or more) - only for 4-digit years
    if len(year) == 4 and year.isdigit():
        try:
            year_int = int(year[:4])
            for offset in [-5, -4, -3, -2, -1, 1, 2, 3, 4, 5]:
                alt_year = str(year_int + offset)
                if len(alt_year) != 4:
                    continue
                alt_key = f"{auth}|{alt_year}".lower()
                if alt_key in ref_map:
                    alt_citation = citation.replace(year, alt_year)
                    confidence = 0.95 if abs(offset) <= 2 else 0.80
                    return FixSuggestion(
                        original=citation,
                        suggested=alt_citation,
                        fix_type="year_typo",
                        confidence=confidence,
                        reason=f"Year {year} corrected to {alt_year} (off by {abs(offset)})"
                    )
        except (ValueError, TypeError):
            pass
    
    # Case 2: Author name variation using fuzzy matching
    if FUZZ_OK and fuzz:
        auth_norm = strip_punct(auth.lower())
        best_match = None
        best_score = 0
        
        target_years = []
        if len(year) == 4:
            target_years.append(year)
        elif year.isdigit() and len(year) < 4:
            y_int = int(year)
            target_years = [str(2000 + y_int), str(2000 + y_int + 10), str(2000 + y_int + 20), str(1900 + y_int)]
        
        for ref in references:
            ym = YEAR_RE.search(ref.reference_full)
            if ym:
                ref_year = _base_year(ym.group(1))
                for target_year in target_years:
                    if ref_year == target_year or (len(target_year) == 4 and abs(int(ref_year) - int(target_year)) <= 2):
                        left = ref.reference_full[:ym.start()].strip(" ,;()")
                        ref_auth = _first_author_or_org_key(left)
                        if ref_auth:
                            score = fuzz.ratio(auth_norm, ref_auth.lower())
                            if score > best_score and score >= 75:
                                best_score = score
                                best_match = ref_auth
        
        if best_match and best_match.lower() != auth.lower():
            alt_citation = re.sub(r'\b' + re.escape(auth) + r'\b', best_match, citation, count=1)
            return FixSuggestion(
                original=citation,
                suggested=alt_citation,
                fix_type="author_normalization",
                confidence=best_score / 100,
                reason=f"Author '{auth}' normalized to '{best_match}'"
            )
    
    # Case 3: Missing "et al." pattern
    if "et al" not in citation.lower() and len(citation.split(",")[0].split()) > 2:
        first_author = auth.split()[0] if auth else ""
        for ref in references:
            if first_author and first_author.lower() in ref.reference_full.lower():
                if "et al" in ref.reference_full.lower():
                    alt_citation = f"{first_author} et al., {year}"
                    return FixSuggestion(
                        original=citation,
                        suggested=alt_citation,
                        fix_type="add_et_al",
                        confidence=0.70,
                        reason=f"Added 'et al.' for {first_author}"
                    )
    
    return None


def _generate_reference_fixes(ref: RefAY) -> List[FixSuggestion]:
    """Generate fix suggestions for reference entries."""
    suggestions = []
    ref_text = ref.reference_full
    
    # Fix 1: Add DOI prefix if DOI exists but missing prefix
    if "doi:" not in ref_text.lower() and "https://doi.org" not in ref_text.lower():
        doi_match = _DOI_RE.search(ref_text)
        if doi_match:
            doi = doi_match.group(0)
            fixed = re.sub(rf"({re.escape(doi)})", r"DOI: \1", ref_text, flags=re.I)
            if fixed != ref_text:
                suggestions.append(FixSuggestion(
                    original=ref_text,
                    suggested=fixed,
                    fix_type="add_doi_prefix",
                    confidence=0.95,
                    reason="Added 'DOI:' prefix"
                ))
    
    # Fix 2: Add missing period at end
    if ref_text and not ref_text.rstrip().endswith('.'):
        suggestions.append(FixSuggestion(
            original=ref_text,
            suggested=ref_text.rstrip() + '.',
            fix_type="add_period",
            confidence=0.60,
            reason="Added trailing period"
        ))
    
    # Fix 3: Fix common URL scheme
    if "http://" in ref_text and "https://" not in ref_text:
        fixed = ref_text.replace("http://", "https://")
        suggestions.append(FixSuggestion(
            original=ref_text,
            suggested=fixed,
            fix_type="fix_url_scheme",
            confidence=0.90,
            reason="Updated HTTP to HTTPS"
        ))
    
    return suggestions


# -----------------------------
# Public API: run_crosscheck (UPDATED - includes main_text)
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
        cites = _extract_author_year_citations_chunked(main_text) if too_large else extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]

        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_author_year(cites, refs)
        ref_count = len(refs)

    elif style_s == "ieee":
        cites_nums = []
        if too_large:
            cites_nums = _extract_numeric_citations_chunked(main_text, style="ieee")
        else:
            cites_nums = extract_ieee_citations(main_text)
        
        refs = []
        for r in references_raw:
            parsed = parse_reference_numeric(r, style="ieee")
            if parsed:
                refs.append(parsed)
        
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(
            cites_nums, refs, style="ieee"
        )
        ref_count = len(refs)

    elif style_s == "vancouver":
        print("Using Vancouver style - sequential numbering")
        
        cites_nums = []
        if too_large:
            cites_nums = _extract_numeric_citations_chunked(main_text, style="vancouver")
        else:
            cites_nums = extract_vancouver_citations(main_text)
        
        refs = parse_vancouver_references(references_raw)
        
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_vancouver(
            cites_nums, refs
        )
        ref_count = len(refs)

    else:
        cites_nums = []
        if too_large:
            cites_nums = _extract_numeric_citations_chunked(main_text, style="ieee")
        else:
            cites_nums = extract_ieee_citations(main_text)
        
        refs = []
        for r in references_raw:
            parsed = parse_reference_numeric(r, style="ieee")
            if parsed:
                refs.append(parsed)
        
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(
            cites_nums, refs, style="ieee"
        )
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
# ENHANCED API WITH AUTO-FIX (OPTIONAL - DOES NOT REPLACE ORIGINAL)
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
    """
    Enhanced version with non-invasive suggestions.
    Calls original run_crosscheck and adds suggestion data.
    """
    
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
        
        if not is_numeric and style_s not in ["ieee", "vancouver"]:
            references_raw = result.get("references_raw", [])
            refs = [parse_reference_author_year(r) for r in references_raw]
            refs = [r for r in refs if r is not None]
            
            ref_map = {r.key: r.reference_full for r in refs}
            
            # Extract citations from main text
            main_text = result.get("main_text", "")
            citations = extract_author_year_citations(main_text)
            
            # Generate suggestions using the new non-invasive engine
            suggestions_data = generate_suggestions(
                citations=citations,
                c2r=result.get("reconciliation_intext_to_reference", []),
                missing_rows=result.get("missing_in_references", []),
                references=refs,
                ref_map=ref_map
            )
            
            result["autofix"] = {
                "enabled": True,
                "suggestions": suggestions_data,
                "summary": suggestions_data["summary"]
            }
        else:
            result["autofix"] = {
                "enabled": True,
                "message": f"Auto-fix primarily supports APA/Harvard style. Current style: {style_s}",
                "suggestions": {"citations": [], "references": [], "statistics": {"total_suggestions": 0}}
            }
    
    return result
