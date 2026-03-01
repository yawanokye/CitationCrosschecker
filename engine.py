# engine.py
__version__ = "1.6.0"

import os
import re
import io
import json
import requests
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter

ENGINE_BUILD = "commercial-2026-03-01-vancouver-modular"

# ==================== DEEPSEEK CONFIGURATION ====================
# Only used for Vancouver style - load from environment variable for security
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not DEEPSEEK_API_KEY:
    print("WARNING: DEEPSEEK_API_KEY environment variable not set. Vancouver style will use fallback mode.")

DEEPSEEK_CONFIG = {
    "api_url": "https://api.deepseek.com/v1/chat/completions",
    "model": "deepseek-chat",
    "timeout": 30,
    "max_tokens": 1000,
    "temperature": 0,
    "fallback_to_rule_based": True
}

# Debug flag
DEBUG = True

def log_debug(msg: str):
    """Print debug messages if DEBUG is enabled."""
    if DEBUG:
        print(f"[DEBUG] {msg}")
# ================================================================

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

# Words/phrases that often precede citations in prose
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

# Commercial-grade narrative filtering
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
# DOCX extraction (optimized)
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
# PDF extraction with enhanced reference detection
# -----------------------------
def read_pdf_text(file_bytes: bytes) -> str:
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")

    out: List[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            try:
                text = page.extract_text() or ""
                # Fix common PDF extraction issues
                text = text.replace("\x00", " ")
                text = re.sub(r"-\n", "", text)  # Fix hyphenated line breaks
                text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)  # Join broken lines
            except Exception:
                text = ""
            out.append(text)
    return "\n".join(out)


def _looks_like_new_numeric_reference_start(s: str) -> bool:
    """Detect if a line starts a new numeric reference."""
    s0 = (s or "").strip()
    if not s0:
        return False
    
    # Pattern: [1] text (IEEE standard)
    if re.match(r"^\[\s*\d{1,4}\s*\]\s+\S", s0):
        return True
    
    # Pattern: (1) text
    if re.match(r"^\(\s*\d{1,4}\s*\)\s+\S", s0):
        return True
    
    # Pattern: 1. text (but not a year like 2008.)
    m = re.match(r"^(\d{1,4})[\.)]\s+(.+)$", s0)
    if m:
        num = m.group(1)
        num_int = int(num)
        # Skip if it's a year (1900-2099)
        if 1900 <= num_int <= 2099:
            # Check if the text after looks like a reference
            rest = m.group(2)
            if YEAR_RE.search(rest) or len(rest) > 30:
                return True
            return False
        return True
    
    # Pattern: 1 text (no punctuation, but ensure it's not a year)
    m = re.match(r"^(\d{1,4})\s+([A-Z].+)$", s0)
    if m:
        num = m.group(1)
        num_int = int(num)
        # Skip if it's a year (1900-2099)
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


# -----------------------------
# Generalized Reference Extraction (Works with multiple paper formats)
# -----------------------------

def detect_reference_format(lines: List[str], start_idx: int) -> str:
    """Detect the reference format used in the document."""
    sample_lines = []
    for i in range(start_idx + 1, min(start_idx + 20, len(lines))):
        line = lines[i].strip()
        if line:
            sample_lines.append(line)
    
    # Count pattern matches
    ieee_count = sum(1 for l in sample_lines if re.match(r'^\[\d+\]', l))
    numbered_count = sum(1 for l in sample_lines if re.match(r'^\d+\.', l) and not re.match(r'^\d{4}\.', l))
    apa_count = sum(1 for l in sample_lines if re.search(r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)', l))
    harvard_count = sum(1 for l in sample_lines if re.search(r'[A-Z][a-z]+\s+\(\d{4}[a-z]?\)', l))
    
    # Return the most common format
    formats = {
        'ieee': ieee_count,
        'numbered': numbered_count,
        'apa': apa_count,
        'harvard': harvard_count
    }
    
    best_format = max(formats, key=formats.get)
    return best_format if formats[best_format] > 0 else "unknown"


def join_reference_lines(current: str, next_line: str) -> str:
    """Intelligently join reference lines, handling hyphens and spaces."""
    if current.endswith('-'):
        # Hyphenated word break
        return current[:-1] + next_line
    elif re.search(r'[a-z]$', current) and re.search(r'^[a-z]', next_line):
        # Likely continuation of same word
        return current + next_line
    else:
        # Normal space separation
        return current + " " + next_line


def clean_reference(ref: str) -> str:
    """Clean up a reference string."""
    # Normalize spaces
    ref = re.sub(r'\s+', ' ', ref).strip()
    
    # Fix common PDF extraction artifacts
    ref = re.sub(r'-\s+', '', ref)  # Remove hyphens with following space
    ref = re.sub(r'\s+-\s+', '-', ref)  # Fix spaced hyphens
    
    # Remove leading/trailing punctuation
    ref = ref.strip('.,;:')
    
    return ref


def extract_references_generalized(text: str) -> List[str]:
    """Generalized reference extraction that works with multiple academic paper formats."""
    lines = text.splitlines()
    
    # Strategy 1: Find standard reference headings (multiple formats)
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
                # Verify this is near the end of document (usually references are at the end)
                if i > len(lines) * 0.6:  # After 60% of document
                    ref_start = i
                    break
        if ref_start != -1:
            break
    
    # Strategy 2: If no heading found, look for reference-like patterns
    if ref_start == -1:
        ref_candidates = []
        for i, line in enumerate(lines):
            # Only check latter part of document
            if i > len(lines) * 0.6:
                line = line.strip()
                # Pattern 1: [1] Author (IEEE)
                if re.match(r'^\[\d+\]\s+[A-Z]\.?\s+[A-Z][a-z]', line):
                    ref_candidates.append((i, line))
                # Pattern 2: 1. Author (Numbered)
                elif re.match(r'^\d+\.\s+[A-Z][a-z]', line) and not re.match(r'^\d{4}\.', line):
                    ref_candidates.append((i, line))
                # Pattern 3: Author (Year). Title (APA)
                elif re.search(r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)', line):
                    ref_candidates.append((i, line))
        
        # If we found reference-like lines, start from the first one
        if ref_candidates:
            ref_start = ref_candidates[0][0] - 1  # Include potential heading line
    
    if ref_start == -1:
        return []
    
    # Extract references using multiple format detectors
    references = []
    current_ref = ""
    
    # Detect reference format type
    ref_format = detect_reference_format(lines, ref_start)
    
    for i in range(ref_start + 1, min(ref_start + 500, len(lines))):  # Limit to 500 lines
        line = lines[i].strip()
        
        # Skip empty lines at beginning
        if not line and not current_ref:
            continue
        
        if not line:
            # Empty line might separate references
            if current_ref:
                references.append(clean_reference(current_ref))
                current_ref = ""
            continue
        
        # Check if this starts a new reference based on format
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
            # Auto-detect: look for patterns
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
            # Continuation of previous reference
            current_ref = join_reference_lines(current_ref, line)
    
    # Add last reference
    if current_ref:
        references.append(clean_reference(current_ref))
    
    # Post-process: filter out non-references and clean
    cleaned_refs = []
    for ref in references:
        # Basic validation: should contain author names and year/number
        if len(ref) > 30 and (
            re.search(r'\d{4}', ref) or  # Has year
            re.search(r'\[\d+\]', ref) or  # Has reference number
            re.search(r'[A-Z][a-z]+,\s+[A-Z]\.', ref)  # Has author format
        ):
            cleaned_refs.append(ref)
    
    return cleaned_refs


def extract_references_pattern_based(text: str) -> List[str]:
    """Extract references using pattern matching on the whole text."""
    # Look for common reference patterns in the text
    patterns = [
        # IEEE: [1] Author. Title...
        (r'\[\d+\]\s+[A-Z][A-Za-z\.\s]+,\s+[A-Z][A-Za-z\.\s]+,\s+["“].+?["”]', re.MULTILINE | re.DOTALL),
        # Numbered: 1. Author. Title...
        (r'^\d+\.\s+[A-Z][A-Za-z\.\s]+,\s+[A-Z][A-Za-z\.\s]+,\s+["“].+?["”]', re.MULTILINE | re.DOTALL),
        # APA: Author, A. (Year). Title...
        (r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)\.\s+[A-Z][a-zA-Z\s]+\.', re.MULTILINE | re.DOTALL),
    ]
    
    references = []
    for pattern, flags in patterns:
        matches = re.findall(pattern, text, flags)
        references.extend([clean_reference(m) for m in matches if len(m) > 30])
    
    return references


def extract_references_heuristic(text: str) -> List[str]:
    """Extract references using heuristics when patterns fail."""
    lines = text.splitlines()
    references = []
    current_ref = ""
    
    # Heuristic: references usually appear in the last 30% of the document
    # and contain years or author names
    start_idx = int(len(lines) * 0.7)  # Start at 70% through document
    
    for i in range(start_idx, len(lines)):
        line = lines[i].strip()
        if not line:
            if current_ref and len(current_ref) > 30:
                references.append(clean_reference(current_ref))
                current_ref = ""
            continue
        
        # Check if line looks like a reference
        has_year = bool(re.search(r'\b(19|20)\d{2}\b', line))
        has_bracket_num = bool(re.search(r'\[\d+\]', line))
        has_author = bool(re.search(r'[A-Z][a-z]+,\s+[A-Z]\.', line))
        has_caps_words = len(re.findall(r'\b[A-Z][a-z]{2,}\b', line)) >= 2
        
        if has_year or has_bracket_num or (has_author and has_caps_words):
            if not current_ref:
                current_ref = line
            else:
                # Check if this is a new reference or continuation
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
    """Enhanced reference extraction using multiple strategies."""
    
    # Strategy 1: Try generalized extraction
    refs = extract_references_generalized(text)
    
    # Strategy 2: If that fails, try pattern-based extraction
    if len(refs) < 5:
        refs = extract_references_pattern_based(text)
    
    # Strategy 3: If still failing, try heuristic extraction
    if len(refs) < 5:
        refs = extract_references_heuristic(text)
    
    return refs


# -----------------------------
# Reference merging/splitting
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


# ============================================================================
# VANCOUVER STYLE PIPELINES - MODULAR ARCHITECTURE
# ============================================================================

class VancouverVariant:
    """Enumeration of Vancouver variants"""
    PLOS_ONE = "plos_one"           # [1], [2,3], references: 1., 2.
    JAMA = "jama"                    # Superscript numbers, references: 1., 2.
    IEEE = "ieee"                    # [1], [2], references: [1], [2]
    VANCOUVER_SUPERSCRIPT = "superscript"  # Just superscript numbers
    VANCOUVER_PAREN = "parentheses"  # (1), (2), references: 1., 2.
    VANCOUVER_PLAIN = "plain"        # Just numbers, references: 1., 2.
    GENERIC = "generic"              # Fallback


class PlosOnePipeline:
    """
    Specialized for PLOS ONE style:
    - In-text: [1], [2,3], [4-7]
    - References: 1. Author... or 1 Author...
    """
    
    @staticmethod
    def extract_citations(text: str) -> List[str]:
        """Extract citations from PLOS ONE style."""
        citations = []
        
        # Pattern: [1], [2,3], [4-7]
        pattern = r'\[\s*(\d{1,4}(?:\s*[-–,]\s*\d{1,4})*)\s*\]'
        
        for match in re.finditer(pattern, text):
            content = match.group(1)
            for part in re.split(r'\s*,\s*', content):
                if '-' in part or '–' in part:
                    parts = re.split(r'[-–]', part)
                    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                        start, end = int(parts[0]), int(parts[1])
                        if start <= end and (end - start) <= 50:
                            citations.extend([str(i) for i in range(start, end + 1)])
                elif part.isdigit():
                    citations.append(part)
        
        return list(dict.fromkeys(citations))  # Deduplicate
    
    @staticmethod
    def parse_references(refs_raw: List[str]) -> List[RefNum]:
        """Parse PLOS ONE style references."""
        parsed = []
        
        for ref_block in refs_raw:
            # Split merged references
            split_refs = PlosOnePipeline._split_references(ref_block)
            
            for ref in split_refs:
                ref_num = PlosOnePipeline._extract_number(ref)
                if ref_num and PlosOnePipeline._validate_reference(ref_num, ref):
                    parsed.append(RefNum(
                        reference_full=PlosOnePipeline._clean_reference(ref),
                        num=ref_num
                    ))
        
        return parsed
    
    @staticmethod
    def _split_references(text: str) -> List[str]:
        """Split merged references."""
        if not text:
            return []
        
        # Look for pattern: number followed by dot and space, then capital letter
        pattern = r'(?=\n?\s*(\d+)\.\s+[A-Z])'
        matches = list(re.finditer(pattern, text))
        
        if len(matches) <= 1:
            return [text.strip()]
        
        splits = []
        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i < len(matches) - 1 else len(text)
            splits.append(text[start:end].strip())
        
        return splits
    
    @staticmethod
    def _extract_number(ref: str) -> Optional[str]:
        """Extract reference number."""
        ref = ref.strip()
        # Pattern 1: "1. Author..."
        m = re.match(r'^(\d+)\.\s+', ref)
        if m:
            return m.group(1)
        # Pattern 2: "1 Author..."
        m = re.match(r'^(\d+)\s+([A-Z])', ref)
        if m:
            return m.group(1)
        return None
    
    @staticmethod
    def _validate_reference(num: str, ref: str) -> bool:
        """Validate reference."""
        num_int = int(num)
        if 1900 <= num_int <= 2099:  # Filter out years
            return False
        if len(ref) < 30:
            return False
        if not re.search(r'[A-Z][a-z]+', ref):  # Need author name
            return False
        return True
    
    @staticmethod
    def _clean_reference(ref: str) -> str:
        """Clean reference."""
        ref = re.sub(r'\s+', ' ', ref).strip()
        if not ref.endswith('.'):
            ref += '.'
        return ref


class JamaPipeline:
    """
    Specialized for JAMA style:
    - In-text: Superscript numbers (e.g., ¹, ², ³)
    - References: 1. Author... or 1 Author...
    """
    
    @staticmethod
    def extract_citations(text: str) -> List[str]:
        """Extract citations from JAMA style (superscript numbers)."""
        citations = []
        
        # Convert common superscript Unicode to numbers
        superscript_map = {
            '¹': '1', '²': '2', '³': '3', '⁴': '4', '⁵': '5',
            '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9', '⁰': '0'
        }
        
        # Pattern for superscript Unicode
        for sup, num in superscript_map.items():
            if sup in text:
                citations.append(num)
        
        # Also look for <sup>1</sup> HTML-style superscript
        html_sup = re.findall(r'<sup>(\d+)</sup>', text)
        citations.extend(html_sup)
        
        # Look for numbers in brackets [1] (fallback)
        bracket_nums = re.findall(r'\[\s*(\d+)\s*\]', text)
        citations.extend(bracket_nums)
        
        return list(dict.fromkeys(citations))
    
    @staticmethod
    def parse_references(refs_raw: List[str]) -> List[RefNum]:
        """Parse JAMA style references."""
        # JAMA uses same reference format as PLOS ONE (1. Author...)
        return PlosOnePipeline.parse_references(refs_raw)


class GenericVancouverPipeline:
    """
    Generic Vancouver style:
    - In-text: [1], (1), 1, or superscript
    - References: 1., 1, [1], or (1)
    """
    
    @staticmethod
    def extract_citations(text: str) -> List[str]:
        """Extract citations from generic Vancouver."""
        citations = []
        
        # Try all possible patterns
        patterns = [
            (r'\[\s*(\d+)\s*\]', False),           # [1]
            (r'\(\s*(\d+)\s*\)', False),            # (1)
            (r'(?<!\d)(\d+)(?!\d)', True),           # standalone 1 (but not part of larger number)
            (r'<sup>(\d+)</sup>', False),           # <sup>1</sup>
        ]
        
        for pattern, check_context in patterns:
            for match in re.finditer(pattern, text):
                num = match.group(1)
                if check_context:
                    # Check context to avoid false positives
                    context = text[max(0, match.start()-30):min(len(text), match.end()+30)]
                    if GenericVancouverPipeline._is_valid_citation_number(num, context):
                        citations.append(num)
                else:
                    citations.append(num)
        
        # Also handle superscript Unicode
        superscript_map = {
            '¹': '1', '²': '2', '³': '3', '⁴': '4', '⁵': '5',
            '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9', '⁰': '0'
        }
        for sup, num in superscript_map.items():
            if sup in text:
                citations.append(num)
        
        return list(dict.fromkeys(citations))
    
    @staticmethod
    def _is_valid_citation_number(num: str, context: str) -> bool:
        """Check if a number is a valid citation."""
        num_int = int(num)
        
        # Filter out years
        if 1900 <= num_int <= 2099:
            if re.search(r'\b(?:in|during|since|year)\s+' + re.escape(num), context, re.I):
                return False
            if re.search(r'[A-Z][a-z]+(?:\s+et al\.?)?\s*[\(\[]\s*' + re.escape(num), context, re.I):
                return True
            return False
        
        # Filter out page numbers
        if re.search(r'[pP]\.?\s*' + re.escape(num) + r'\b', context):
            return False
        
        return True
    
    @staticmethod
    def parse_references(refs_raw: List[str]) -> List[RefNum]:
        """Parse generic Vancouver references."""
        parsed = []
        
        for ref in refs_raw:
            # Try all possible reference formats
            ref_num = None
            clean_ref = GenericVancouverPipeline._clean_reference(ref)
            
            # Pattern 1: 1. Author...
            m = re.match(r'^(\d+)\.\s+', clean_ref)
            if m:
                ref_num = m.group(1)
            
            # Pattern 2: [1] Author...
            if not ref_num:
                m = re.match(r'^\[\s*(\d+)\s*\]\s+', clean_ref)
                if m:
                    ref_num = m.group(1)
            
            # Pattern 3: (1) Author...
            if not ref_num:
                m = re.match(r'^\(\s*(\d+)\s*\)\s+', clean_ref)
                if m:
                    ref_num = m.group(1)
            
            # Pattern 4: 1 Author...
            if not ref_num:
                m = re.match(r'^(\d+)\s+([A-Z])', clean_ref)
                if m:
                    ref_num = m.group(1)
            
            if ref_num:
                num_int = int(ref_num)
                if not (1900 <= num_int <= 2099) and len(clean_ref) > 30:
                    parsed.append(RefNum(
                        reference_full=clean_ref,
                        num=ref_num
                    ))
        
        return parsed
    
    @staticmethod
    def _clean_reference(ref: str) -> str:
        """Clean reference."""
        ref = re.sub(r'\s+', ' ', ref).strip()
        return ref


class VancouverPipelineFactory:
    """Factory to get appropriate pipeline for Vancouver variant."""
    
    _pipelines = {
        VancouverVariant.PLOS_ONE: PlosOnePipeline,
        VancouverVariant.JAMA: JamaPipeline,
        VancouverVariant.GENERIC: GenericVancouverPipeline,
        VancouverVariant.VANCOUVER_SUPERSCRIPT: GenericVancouverPipeline,
        VancouverVariant.VANCOUVER_PAREN: GenericVancouverPipeline,
        VancouverVariant.VANCOUVER_PLAIN: GenericVancouverPipeline,
    }
    
    @staticmethod
    def detect_variant(text: str, references: List[str] = None) -> str:
        """
        Auto-detect which Vancouver variant is being used.
        """
        # Check for superscript Unicode
        superscript_chars = ['¹', '²', '³', '⁴', '⁵', '⁶', '⁷', '⁸', '⁹']
        if any(c in text for c in superscript_chars):
            return VancouverVariant.JAMA
        
        # Check for PLOS ONE style [1], [2,3]
        if re.search(r'\[\s*\d+\s*,\s*\d+\s*\]', text):
            return VancouverVariant.PLOS_ONE
        
        # Check for parentheses (1)
        if re.search(r'\(\s*\d+\s*\)', text):
            return VancouverVariant.VANCOUVER_PAREN
        
        # Check for standalone numbers (likely superscript or plain)
        if re.search(r'(?<!\d)\d+(?!\d)', text):
            # Check reference format if available
            if references and len(references) > 0:
                first_ref = references[0].strip()
                if re.match(r'^\d+\.', first_ref):
                    return VancouverVariant.PLOS_ONE
                elif re.match(r'^\[\d+\]', first_ref):
                    return VancouverVariant.IEEE
            
            return VancouverVariant.VANCOUVER_PLAIN
        
        # Default to PLOS ONE
        return VancouverVariant.PLOS_ONE
    
    @staticmethod
    def get_pipeline(variant: str = None, text: str = None, references: List[str] = None):
        """Get appropriate pipeline."""
        if variant is None and text is not None:
            variant = VancouverPipelineFactory.detect_variant(text, references or [])
            log_debug(f"Auto-detected Vancouver variant: {variant}")
        
        pipeline_class = VancouverPipelineFactory._pipelines.get(variant)
        if pipeline_class:
            return pipeline_class()
        else:
            log_debug(f"No specific pipeline for {variant}, using generic")
            return GenericVancouverPipeline()


# ============================================================================
# Citation extractors - APA/Harvard (UNCHANGED - WORKS PERFECTLY)
# ============================================================================
def extract_author_year_citations(text: str) -> List[str]:
    """Extract APA/Harvard citations - RULE BASED, NO AI."""
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
        year = m.group(2).strip()
        author = re.sub(r"(’s|'s)\b", "", author).strip()
        out.append(norm_space(f"{author}, {year}"))

    return [c for c in out if c]


# ============================================================================
# Citation extractors - IEEE (UNCHANGED - WORKS PERFECTLY)
# ============================================================================
def extract_ieee_citations(text: str) -> List[str]:
    """Extract IEEE citations - RULE BASED, NO AI.
    IEEE strictly uses square brackets: [1], [1,2,3], [1-5]
    """
    t = text or ""
    out: List[str] = []
    
    # Remove common false positives first
    t = re.sub(r"(?:table|figure|fig\.?|eq\.?|equation)\s+(\d{1,4})", "", t, flags=re.I)
    
    # Fix line breaks between citations (common in PDFs)
    t = re.sub(r'\]\s*\n\s*\[', '][', t)
    
    # IEEE: STRICTLY square brackets only [1], [1,2,3], [1-5]
    ieee_pat = re.compile(r"\[\s*(\d{1,4})(?:\s*[-–,]\s*(\d{1,4}))?(?:\s*,\s*(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?)?\s*\]")
    
    for m in ieee_pat.finditer(t):
        nums = _expand_citation_range(m)
        out.extend(nums)
    
    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for num in out:
        if num not in seen:
            seen.add(num)
            deduped.append(num)
    
    return deduped


def _expand_citation_range(match) -> List[str]:
    """Expand citation ranges like [2-5] or [2,3] into individual numbers."""
    nums = []
    groups = match.groups()
    
    if not groups or not groups[0]:
        return nums
    
    # Process first number and potential range
    start = int(groups[0])
    if groups[1]:  # Has range (e.g., 2-5)
        end = int(groups[1])
        if start <= end and (end - start) <= 50:  # Sanity check
            nums.extend([str(i) for i in range(start, end + 1)])
        else:
            nums.append(str(start))
            nums.append(str(end))
    else:
        nums.append(str(start))
    
    # Process additional numbers after comma
    if groups[2]:
        start2 = int(groups[2])
        if groups[3]:  # Has second range
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
# Main numeric citation dispatcher
# ============================================================================
def extract_numeric_citations(text: str, style: str = "ieee", vancouver_variant: str = None) -> List[str]:
    """Extract numeric citations based on style.
    
    Args:
        text: Document text
        style: "ieee", "vancouver", or "numeric"
        vancouver_variant: Specific Vancouver variant (optional)
    
    Returns:
        List of citation numbers as strings
    """
    style = style.lower()
    
    if style == "ieee":
        return extract_ieee_citations(text)
    elif style == "vancouver":
        pipeline = VancouverPipelineFactory.get_pipeline(variant=vancouver_variant, text=text)
        return pipeline.extract_citations(text)
    else:
        # Generic numeric (fallback to IEEE)
        return extract_ieee_citations(text)


# -----------------------------
# Reference parsers - Enhanced for academic papers
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


def parse_reference_numeric(ref: str, style: str = "ieee", vancouver_variant: str = None) -> Optional[RefNum]:
    """Parse numeric references from academic papers.
    
    Args:
        ref: Reference string
        style: "ieee", "vancouver", or "numeric"
        vancouver_variant: Specific Vancouver variant (optional)
    
    Returns:
        RefNum object if valid, None otherwise
    """
    s = norm_space(ref)
    if not s:
        return None
    
    style = style.lower()
    
    if style == "ieee":
        # IEEE: STRICTLY [1] format only
        m = re.match(r"^\[\s*(\d{1,4})\s*\]\s*(.+)$", s)
        if m:
            num = m.group(1)
            body = norm_space(m.group(2))
            body = _strip_leading_reference_number(body)
            # Check if it looks like a real reference (has author names, title, etc.)
            if len(body) > 20 and re.search(r'[A-Z][a-z]+', body):
                return RefNum(reference_full=s, num=num)
        return None
    
    elif style == "vancouver":
        # Use Vancouver pipeline
        pipeline = VancouverPipelineFactory.get_pipeline(variant=vancouver_variant)
        # For now, we need to call parse_references with a list
        refs = pipeline.parse_references([s])
        return refs[0] if refs else None
    
    else:
        # Generic numeric (fallback to IEEE)
        m = re.match(r"^\[\s*(\d{1,4})\s*\]\s*(.+)$", s)
        if m:
            num = m.group(1)
            return RefNum(reference_full=s, num=num)
        m = re.match(r"^(\d{1,4})\.\s*(.+)$", s)
        if m:
            num = m.group(1)
            return RefNum(reference_full=s, num=num)
        return None


# -----------------------------
# Reference clustering
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


# -----------------------------
# Reconciliation - Enhanced for academic papers
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
        year_base = _base_year(year)

        cand_keys = [f"{auth}|{year}".lower()]
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


def reconcile_numeric(citations: List[str], references: List[RefNum], style: str = "ieee") -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    """Reconcile numeric citations with references for academic papers.
    
    Args:
        citations: List of citation numbers from text
        references: List of parsed references
        style: "ieee" or "vancouver"
    
    Returns:
        Tuple of reconciliation results
    """
    style = style.lower()
    is_ieee = style == "ieee"
    
    # Build reference map with appropriate formats based on style
    ref_map: Dict[str, str] = {}
    ref_by_num: Dict[str, str] = {}  # For direct number matching
    
    for r in references:
        # Store with original number
        ref_map[r.num] = r.reference_full
        ref_by_num[r.num] = r.reference_full
        
        if is_ieee:
            # IEEE: Only square bracket format for matching
            ref_map[f"[{r.num}]"] = r.reference_full
        else:
            # Vancouver: Multiple formats for matching
            ref_map[f"[{r.num}]"] = r.reference_full
            ref_map[f"({r.num})"] = r.reference_full
            ref_map[f"{r.num}."] = r.reference_full
    
    # Count citations
    cite_counts = Counter()
    matched_refs = set()
    
    # Process each citation
    for cite in citations:
        cite_str = str(cite).strip()
        
        if is_ieee:
            # IEEE: Try direct match first
            if cite_str in ref_map:
                cite_counts[ref_map[cite_str]] += 1
                matched_refs.add(ref_map[cite_str])
                continue
            
            # Try as number (if it's just digits)
            if cite_str.isdigit() and cite_str in ref_by_num:
                cite_counts[ref_by_num[cite_str]] += 1
                matched_refs.add(ref_by_num[cite_str])
                continue
            
            # Try to extract number from square brackets only
            m = re.match(r'^\[\s*(\d{1,4})\s*\]$', cite_str)
            if m and m.group(1) in ref_by_num:
                cite_counts[ref_by_num[m.group(1)]] += 1
                matched_refs.add(ref_by_num[m.group(1)])
                continue
        
        else:
            # Vancouver: Flexible matching
            # Try direct match
            if cite_str in ref_map:
                cite_counts[ref_map[cite_str]] += 1
                matched_refs.add(ref_map[cite_str])
                continue
            
            # Try as number (if it's just digits)
            if cite_str.isdigit() and cite_str in ref_by_num:
                cite_counts[ref_by_num[cite_str]] += 1
                matched_refs.add(ref_by_num[cite_str])
                continue
            
            # Try to extract number from bracket/parentheses
            m = re.match(r'^[\(\[]?\s*(\d{1,4})\s*[\)\]]?$', cite_str)
            if m and m.group(1) in ref_by_num:
                cite_counts[ref_by_num[m.group(1)]] += 1
                matched_refs.add(ref_by_num[m.group(1)])
                continue
    
    # Build c2r (citations to references)
    c2r: List[Dict[str, Any]] = []
    missing_counter = Counter()
    
    for cite in citations:
        cite_str = str(cite).strip()
        matched = False
        
        if is_ieee:
            # IEEE matching logic
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
            else:
                m = re.match(r'^\[\s*(\d{1,4})\s*\]$', cite_str)
                if m and m.group(1) in ref_by_num:
                    c2r.append({
                        "status": "matched", 
                        "in_text": cite_str, 
                        "matched_reference": ref_by_num[m.group(1)], 
                        "flags": "format_normalized"
                    })
                    matched = True
        
        else:
            # Vancouver matching logic
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
                    "flags": "standalone_number"
                })
                matched = True
            else:
                m = re.match(r'^[\(\[]?\s*(\d{1,4})\s*[\)\]]?$', cite_str)
                if m and m.group(1) in ref_by_num:
                    c2r.append({
                        "status": "matched", 
                        "in_text": cite_str, 
                        "matched_reference": ref_by_num[m.group(1)], 
                        "flags": "format_variation"
                    })
                    matched = True
        
        if not matched:
            c2r.append({
                "status": "not_found", 
                "in_text": cite_str, 
                "matched_reference": "", 
                "flags": ""
            })
            missing_counter[cite_str] += 1
    
    # Build r2c (references to citations)
    r2c: List[Dict[str, Any]] = []
    uncited_refs: List[str] = []
    
    # Group citations by reference
    cite_samples_by_ref: Dict[str, List[str]] = defaultdict(list)
    
    for cite in citations:
        cite_str = str(cite).strip()
        
        if is_ieee:
            # IEEE grouping
            if cite_str in ref_map:
                if len(cite_samples_by_ref[ref_map[cite_str]]) < 6:
                    cite_samples_by_ref[ref_map[cite_str]].append(cite_str)
            elif cite_str.isdigit() and cite_str in ref_by_num:
                if len(cite_samples_by_ref[ref_by_num[cite_str]]) < 6:
                    cite_samples_by_ref[ref_by_num[cite_str]].append(cite_str)
            else:
                m = re.match(r'^\[\s*(\d{1,4})\s*\]$', cite_str)
                if m and m.group(1) in ref_by_num:
                    if len(cite_samples_by_ref[ref_by_num[m.group(1)]]) < 6:
                        cite_samples_by_ref[ref_by_num[m.group(1)]].append(cite_str)
        
        else:
            # Vancouver grouping
            if cite_str in ref_map:
                if len(cite_samples_by_ref[ref_map[cite_str]]) < 6:
                    cite_samples_by_ref[ref_map[cite_str]].append(cite_str)
            elif cite_str.isdigit() and cite_str in ref_by_num:
                if len(cite_samples_by_ref[ref_by_num[cite_str]]) < 6:
                    cite_samples_by_ref[ref_by_num[cite_str]].append(cite_str)
            else:
                m = re.match(r'^[\(\[]?\s*(\d{1,4})\s*[\)\]]?$', cite_str)
                if m and m.group(1) in ref_by_num:
                    if len(cite_samples_by_ref[ref_by_num[m.group(1)]]) < 6:
                        cite_samples_by_ref[ref_by_num[m.group(1)]].append(cite_str)
    
    for r in references:
        ref_full = r.reference_full
        times = int(cite_counts.get(ref_full, 0))
        
        if times == 0:
            uncited_refs.append(ref_full)
        
        r2c.append({
            "times_cited": times,
            "reference": ref_full,
            "cited_by": cite_samples_by_ref.get(ref_full, []),
        })
    
    missing_rows = [{"citation_in_text": k, "count_in_text": int(v)} for k, v in missing_counter.most_common()]
    unique_intext_count = int(len(set([str(c) for c in citations if c])))
    
    return c2r, r2c, missing_rows, uncited_refs, unique_intext_count


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


def _extract_numeric_citations_chunked(text: str, style: str = "ieee", vancouver_variant: str = None) -> List[str]:
    """Chunked version of numeric citation extraction with style parameter."""
    seen = set()
    total = []
    
    # For Vancouver, use smaller chunks
    if style == "vancouver":
        chunk_size = 300_000  # Keep standard chunk size, pipelines handle their own logic
    else:
        chunk_size = 300_000
    
    for chunk in _iter_text_chunks(text, chunk_size=chunk_size):
        for c in extract_numeric_citations(chunk, style=style, vancouver_variant=vancouver_variant):
            if c not in seen:
                seen.add(c)
                total.append(c)
    return total


# -----------------------------
# Public API: run_crosscheck
# -----------------------------
def run_crosscheck(
    file_bytes: bytes,
    filename: str,
    style: str = "apa",
    vancouver_variant: str = None,  # New parameter for Vancouver variant
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
        full_text = read_pdf_text(file_bytes)
        
        # Try multiple strategies to find references
        lines = full_text.splitlines()
        
        # First try standard heading detection
        idx, tail = _find_reference_heading(lines, style_hint=style_hint)
        
        if idx == -1:
            # Try enhanced reference extraction (works with multiple formats)
            references_raw = extract_references_enhanced(full_text)
            main_text = full_text
            ref_msg = f"Found {len(references_raw)} references using enhanced extraction."
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

    log_debug(f"Initial reference count: {len(references_raw)}")
    main_text_len = len(main_text or "")
    too_large = main_text_len > 2_000_000

    if style_hint == "apa":
        # APA/Harvard - rule based, no AI (WORKS PERFECTLY)
        cites = _extract_author_year_citations_chunked(main_text) if too_large else extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]

        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_author_year(cites, refs)
        ref_count = len(refs)

    else:
        # Style-specific numeric handling for academic papers
        if style_s == "ieee":
            # IEEE: Strict square brackets only (WORKS PERFECTLY)
            log_debug("Using IEEE rule-based extraction")
            cites_nums = []
            if too_large:
                cites_nums = _extract_numeric_citations_chunked(main_text, style="ieee")
            else:
                cites_nums = extract_numeric_citations(main_text, style="ieee")
            
            log_debug(f"IEEE extracted {len(cites_nums)} citations")
            
            # Parse references with IEEE strictness
            refs = []
            for r in references_raw:
                parsed = parse_reference_numeric(r, style="ieee")
                if parsed:
                    refs.append(parsed)
            
            log_debug(f"Parsed {len(refs)} IEEE references")
            
            # Reconcile with IEEE strictness
            c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(
                cites_nums, refs, style="ieee"
            )
            ref_count = len(refs)
            
        elif style_s == "vancouver":
            # Vancouver: Use modular pipeline system
            log_debug("Using Vancouver modular pipeline system")
            
            # Detect variant if not specified
            if vancouver_variant is None:
                vancouver_variant = VancouverPipelineFactory.detect_variant(main_text, references_raw)
                log_debug(f"Auto-detected Vancouver variant: {vancouver_variant}")
            
            # Get appropriate pipeline
            pipeline = VancouverPipelineFactory.get_pipeline(
                variant=vancouver_variant,
                text=main_text,
                references=references_raw
            )
            
            # Extract citations using pipeline
            cites_nums = []
            if too_large:
                # For large texts, process in chunks but let pipeline handle each chunk
                for chunk in _iter_text_chunks(main_text, chunk_size=300_000):
                    cites_nums.extend(pipeline.extract_citations(chunk))
                # Deduplicate
                seen = set()
                deduped = []
                for num in cites_nums:
                    if num not in seen:
                        seen.add(num)
                        deduped.append(num)
                cites_nums = deduped
            else:
                cites_nums = pipeline.extract_citations(main_text)
            
            log_debug(f"Vancouver extracted {len(cites_nums)} citations")
            
            # Parse references using pipeline
            refs = pipeline.parse_references(references_raw)
            log_debug(f"Parsed {len(refs)} Vancouver references")
            
            # Reconcile with Vancouver flexibility
            c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(
                cites_nums, refs, style="vancouver"
            )
            ref_count = len(refs)
            
        else:  # generic numeric
            # Default to IEEE
            log_debug(f"Using default numeric (IEEE) extraction for style: {style_s}")
            cites_nums = []
            if too_large:
                cites_nums = _extract_numeric_citations_chunked(main_text, style="ieee")
            else:
                cites_nums = extract_numeric_citations(main_text, style="ieee")
            
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

    log_debug(f"Final results: intext={intext_count}, refs={ref_count}, missing={missing_unique}, uncited={len(uncited_refs)}")

    return {
        "filename": filename,
        "style": style_s,
        "vancouver_variant": vancouver_variant if style_s == "vancouver" else None,
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
