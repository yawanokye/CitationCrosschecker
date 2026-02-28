# engine.py
__version__ = "1.6.0"

import re
import io
import os
import json
import requests
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter

ENGINE_BUILD = "commercial-2026-02-28-llm-enhanced"

# ==================== CONFIGURATION ====================
# Your API key - replace with your actual key
YOUR_API_KEY = "sk-267f05e8f67b4fefa5189320eac5554f"

# LLM Configuration - supports multiple providers
LLM_CONFIG = {
    "provider": "openai",  # or "anthropic", "deepseek", "ollama", "custom"
    "api_key": YOUR_API_KEY,
    "api_url": "https://api.openai.com/v1/chat/completions",  # OpenAI endpoint
    "model": "gpt-3.5-turbo",  # or "gpt-4", "claude-3-haiku-20240307", etc.
    "timeout": 15,
    "max_tokens": 1000,
    "temperature": 0,
    "fallback_to_rule_based": True  # Use rule-based if LLM fails
}

# Alternative configurations (uncomment if using different provider)
# LLM_CONFIG = {
#     "provider": "anthropic",
#     "api_key": YOUR_API_KEY,
#     "api_url": "https://api.anthropic.com/v1/messages",
#     "model": "claude-3-haiku-20240307",
#     "timeout": 15,
#     "max_tokens": 1000,
#     "temperature": 0,
#     "fallback_to_rule_based": True
# }

# LLM_CONFIG = {
#     "provider": "deepseek",
#     "api_key": YOUR_API_KEY,
#     "api_url": "https://api.deepseek.com/v1/chat/completions",
#     "model": "deepseek-chat",
#     "timeout": 15,
#     "max_tokens": 1000,
#     "temperature": 0,
#     "fallback_to_rule_based": True
# }

# ======================================================

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
    r"^\s*REFERENCES\s*$",
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
}

REF_END_HEADINGS = [
    r"^\s*appendix(?:es)?\b",
    r"^\s*annex(?:es)?\b",
    r"^\s*supplement(?:ary)?\b",
]
REF_END_HEADING_RE = re.compile("|".join(REF_END_HEADINGS), re.I)

NON_NAME_AUTHOR_KEYS = {
    "survey", "field", "work", "data", "table", "figure", "fig",
    "chapter", "section", "appendix", "equation", "model", "analysis",
    "study", "paper", "thesis", "report", "source", "author",
}

# Commercial-grade narrative filtering
NARRATIVE_SINGLE_TOKENS = {
    "crisis", "war", "scandal", "revolution",
    "pandemic", "covid", "covid19", "covid-19",
}

NARRATIVE_PHRASE_PATTERNS = [
    r"\byear\s+on\s+year\b",
    r"\bgrowth\s+rate\b",
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
    s = re.sub(r"[^a-z0-9\s\-&/]", " ", s)
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
    parts = re.split(r"\band\b|;|/", s, flags=re.I)
    out = []
    for p in parts:
        p = p.strip(" ,.;:()[]{}")
        if not p:
            continue
        if "," in p:
            cand = p.split(",", 1)[0].strip()
        else:
            cand = p.split()[-1].strip()
        cand = re.sub(r"[^A-Za-z\-']+", "", cand).strip()
        cand = cand.replace("’", "'")
        if len(cand) < 2:
            continue
        if cand.lower() in {"available", "ssrn", "university", "press", "journal"}:
            continue
        out.append(cand.lower())
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))][:4]


def _looks_like_toc_references_line(s: str, tail: str) -> bool:
    tail = (tail or "").strip()
    return bool(tail and re.fullmatch(r"\d{1,4}", tail)) or bool(re.search(r"\.{2,}\s*\d{1,4}\s*$", s))


def _looks_like_heading_line(s: str) -> bool:
    s0 = (s or "").strip()
    if not s0 or len(s0) > 120 or (s0.endswith(".") and len(s0) > 25):
        return False
    letters = re.sub(r"[^A-Za-z]", "", s0)
    return bool((letters and letters.isupper() and len(letters) >= 6) or
                re.match(r"^[A-Z][A-Za-z0-9\s\-,:]{3,}$", s0))


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
    return _LEAD_NUM_RE.sub("", norm_space(s)).strip()


def _looks_like_person_author(s: str) -> bool:
    s0 = norm_space(s)
    return bool(re.search(r"\b[A-Z][A-Za-z'\-]+,\s*(?:[A-Z]\.\s*){1,4}", s0) or
                re.search(r"\b[A-Z][A-Za-z'\-]+\s+et\s+al\.", s0))


def _looks_like_org_author(s: str) -> bool:
    s0 = norm_space(s)
    if re.search(r"\(([A-Z]{2,10})\)", s0):
        return True
    toks = re.findall(r"[A-Z]{2,}", s0)
    return len(toks) >= 2


def _looks_like_title_piece(s: str) -> bool:
    s0 = norm_space(s)
    if len(s0) < 6 or len(re.findall(r"[A-Za-z]", s0)) < 5:
        return False
    if re.fullmatch(r"(?i)(?:vol|issue|no|pp|pages?|doi)\b.*", s0):
        return False
    return len(s0.split()) >= 3 or ":" in s0 or "–" in s0


def _is_plausible_reference_entry(s: str) -> bool:
    s0 = _strip_leading_reference_number(s)
    if not s0 or len(s0) < 18:
        return False

    ym = YEAR_RE.search(s0)
    if not ym:
        return False

    left = s0[: ym.start()].strip()
    author_ok = _looks_like_person_author(left) or _looks_like_org_author(left)

    after = s0[ym.end():].lstrip(" ).,;:-")
    after_title = after.split(".", 1)[0].strip()
    before = s0[: ym.start()].strip(" .;:-")
    before_parts = [p.strip() for p in before.split(".") if p.strip()]
    before_title = before_parts[-1] if before_parts else ""

    return author_ok and (_looks_like_title_piece(after_title) or _looks_like_title_piece(before_title))


def _first_author_or_org_key(author_left: str) -> str:
    s = norm_space(author_left)
    m = re.search(r"\(([A-Z][A-Z0-9/&\-]{1,15})\)", s)
    if m:
        return strip_punct(m.group(1))

    s = _strip_leading_reference_number(s)
    s = re.sub(r"\(\s*(?:1[6-9]\d{2}|20\d{2})\s*\).*", "", s).strip()
    s0 = re.split(r"\s+(?:&|and)\s+|,", s, maxsplit=1)[0].strip()
    toks = [t for t in s0.split() if re.search(r"[A-Za-z0-9]", t)]
    return strip_punct(toks[-1]) if toks else ""


def _truncate_reference_block(lines: List[str], style_hint: str) -> List[str]:
    out = []
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
        if ref_like_seen >= 3 and (REF_END_HEADING_RE.search(s) or
            (_looks_like_heading_line(s) and re.search(r"\b(appendix|supplement)\b", s, re.I))):
            look = [x for x in lines[i:i+25] if (x or "").strip()]
            if sum(1 for x in look if _is_ref_like((x or "").strip())) <= 1:
                break
        out.append(ln)
    return out


# -----------------------------
# DOCX extraction
# -----------------------------
def _iter_docx_text(doc):
    for p in doc.paragraphs:
        if t := norm_space(p.text):
            yield t
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if t := norm_space(p.text):
                        yield t


def _docx_xml_text(file_bytes: bytes) -> List[str]:
    import zipfile
    import xml.etree.ElementTree as ET

    NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    
    def _extract(xml_bytes):
        out = []
        try:
            root = ET.fromstring(xml_bytes)
            for p in root.findall(".//w:p", NS):
                parts = [tnode.text for tnode in p.findall(".//w:t", NS) if tnode.text]
                if s := norm_space("".join(parts)):
                    out.append(s)
        except Exception:
            pass
        return out

    targets = ["word/document.xml", "word/footnotes.xml", "word/endnotes.xml"]
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
        names = set(z.namelist())
        for name in names:
            if name.startswith("word/header") or name.startswith("word/footer"):
                if name.endswith(".xml"):
                    targets.append(name)
        lines = []
        for t in targets:
            if t in names:
                try:
                    lines.extend(_extract(z.read(t)))
                except Exception:
                    continue
    return lines


def read_docx_split_main_and_refs(file_bytes: bytes) -> Tuple[str, List[str], str]:
    """Read DOCX and split main text from references."""
    if not DOCX_OK:
        raise RuntimeError("python-docx not installed")

    lines = []
    
    # Try XML extraction first
    try:
        lines = _docx_xml_text(file_bytes)
    except Exception as e:
        print(f"XML extraction failed: {e}, falling back to python-docx")
    
    # Fall back to python-docx
    if not lines:
        try:
            doc = Document(io.BytesIO(file_bytes))
            lines = list(_iter_docx_text(doc))
        except Exception as e:
            return "", [], f"Error reading DOCX: {e}"

    if not lines:
        return "", [], "No text found in DOCX"

    def _ref_like(line: str) -> bool:
        s = line.strip()
        return bool(re.match(r"^\s*(\[\s*\d+\s*\]|\(\s*\d+\s*\)|\d+[\.)])\s+\S", s) or
                    (YEAR_RE.search(s) and re.match(r"^[A-Z]", s)) or
                    "doi:" in s.lower())

    main_lines, ref_lines, in_refs = [], [], False
    heading_line = ""

    for line in lines:
        if not in_refs:
            for pat in REF_HEADINGS:
                if re.search(pat, line, re.I):
                    in_refs = True
                    heading_line = line
                    break
            if not in_refs:
                m = REF_HEADING_RELAXED.search(line)
                if m and m.start() <= 4 and len(line) <= 160:
                    tail = line[m.end():].strip(" :-\t")
                    if not _looks_like_toc_references_line(line, tail):
                        in_refs = True
                        heading_line = line
                        if tail:
                            ref_lines.append(tail)
                        continue
        (ref_lines if in_refs else main_lines).append(line)

    msg = f"Found References heading: {heading_line}" if in_refs else "No References heading found."
    return "\n".join(main_lines).strip(), ref_lines, msg


# -----------------------------
# PDF extraction
# -----------------------------
def read_pdf_text(file_bytes: bytes) -> str:
    """Extract text from PDF with better formatting preservation."""
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")

    out = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            try:
                # Use better parameters for text extraction
                text = page.extract_text(
                    x_tolerance=3, 
                    y_tolerance=3,
                    layout=True,
                    keep_blank_chars=False
                ) or ""
                text = text.replace("\x00", " ")
                text = re.sub(r"-\n", "", text)  # Fix hyphenation
                out.append(text)
            except Exception:
                out.append("")
    return "\n".join(out)


def extract_references_from_pdf(text: str) -> Tuple[str, List[str], str]:
    """
    Extract references from PDF text with robust pattern matching.
    Returns (main_text, references_list, message)
    """
    lines = text.splitlines()
    
    # Find the references section
    ref_start = -1
    ref_heading = ""
    
    # Look for reference headings
    heading_patterns = [
        r'^\s*REFERENCES\s*$',
        r'^\s*BIBLIOGRAPHY\s*$',
        r'^\s*WORKS\s+CITED\s*$',
        r'^\s*LITERATURE\s+CITED\s*$',
    ]
    
    for i, line in enumerate(lines):
        line_clean = line.strip().upper()
        for pattern in heading_patterns:
            if re.search(pattern, line_clean, re.I):
                if i > len(lines) * 0.5:
                    ref_start = i
                    ref_heading = line
                    break
        if ref_start != -1:
            break
    
    # If no heading found, look for reference patterns
    if ref_start == -1:
        for i, line in enumerate(lines):
            if i > len(lines) * 0.5:
                line = line.strip()
                if re.match(r'^\[\d+\]\s+[A-Z]', line) or \
                   (re.match(r'^\d+\.\s+[A-Z]', line) and not re.match(r'^\d{4}\.', line)):
                    ref_start = i
                    ref_heading = "REFERENCES (detected)"
                    break
    
    if ref_start == -1:
        return text, [], "No references section found."
    
    # Extract main text
    main_text = "\n".join(lines[:ref_start]).strip()
    
    # Extract references
    references = []
    current_ref = ""
    ref_pattern = re.compile(r'^(\[\d+\]|\d+\.)\s+')
    
    for i in range(ref_start + 1, len(lines)):
        line = lines[i].rstrip()
        
        if not line and not current_ref:
            continue
        
        if ref_pattern.match(line.strip()):
            if current_ref:
                clean_ref = clean_reference_text(current_ref)
                if is_valid_reference(clean_ref):
                    references.append(clean_ref)
            current_ref = line
        elif current_ref:
            if current_ref.endswith('-'):
                current_ref = current_ref[:-1] + line
            else:
                current_ref += " " + line
        elif line.strip() and not current_ref and len(line) > 30:
            if is_valid_reference(line):
                references.append(clean_reference_text(line))
    
    if current_ref:
        clean_ref = clean_reference_text(current_ref)
        if is_valid_reference(clean_ref):
            references.append(clean_ref)
    
    merged_refs = merge_split_references(references)
    
    final_refs = []
    for ref in merged_refs:
        if len(ref) > 30 and is_valid_reference(ref):
            final_refs.append(ref)
    
    msg = f"Found {len(final_refs)} references."
    return main_text, final_refs, msg


def clean_reference_text(ref: str) -> str:
    """Clean up reference text."""
    ref = re.sub(r'\s+', ' ', ref).strip()
    ref = re.sub(r'-\s+', '', ref)
    ref = re.sub(r'\s+-\s+', '-', ref)
    ref = re.sub(r'\s+\.', '.', ref)
    ref = re.sub(r'[_-]{2,}', '', ref)
    return ref


def is_valid_reference(ref: str) -> bool:
    """Check if a string is a valid reference."""
    if len(ref) < 30:
        return False
    
    has_year = bool(re.search(r'\b(19|20)\d{2}\b', ref))
    has_author = bool(re.search(r'[A-Z][a-z]+,\s+[A-Z]\.', ref))
    has_doi = bool(re.search(r'10\.\d{4,9}/', ref))
    has_journal = bool(re.search(r'Journal|Review|Letters|Proceedings|Conference', ref, re.I))
    has_publisher = bool(re.search(r'Press|University|Institute|Publisher', ref, re.I))
    
    if re.search(r'Table\s+\d+|Figure\s+\d+', ref, re.I):
        return False
    
    if re.match(r'^\d+\s*$', ref):
        return False
    
    return has_year or has_author or has_doi or has_journal or has_publisher


def merge_split_references(refs: List[str]) -> List[str]:
    """Merge references that were incorrectly split."""
    if len(refs) <= 1:
        return refs
    
    merged = []
    i = 0
    while i < len(refs):
        current = refs[i]
        
        if i < len(refs) - 1 and not current.rstrip().endswith('.'):
            next_ref = refs[i + 1]
            if not re.match(r'^(\[\d+\]|\d+\.)', next_ref.strip()):
                current += " " + next_ref
                i += 1
        merged.append(current)
        i += 1
    
    return merged


def _looks_like_new_numeric_reference_start(s: str) -> bool:
    s0 = s.strip()
    if not s0:
        return False
    if re.match(r"^\[\s*\d+\s*\]\s+\S", s0):
        return True
    if re.match(r"^\(\s*\d+\s*\)\s+\S", s0):
        return True
    m = re.match(r"^(\d+)[\.)]\s+(.+)$", s0)
    if m:
        num = int(m.group(1))
        if 1900 <= num <= 2099:
            return False
        return True
    return False


def _looks_like_new_apa_reference_start(s: str) -> bool:
    s0 = s.strip()
    return bool(re.search(r"\.\s*\(\s*" + YEAR + r"\s*\)\.", s0)) or \
           bool(re.match(r"^(.+?)\s*\(\s*" + YEAR + r"\s*\)", s0))


def _count_reference_like(lines: List[str], style_hint: str) -> int:
    count = 0
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if style_hint == "numeric":
            if _looks_like_new_numeric_reference_start(s):
                count += 1
        else:
            if _looks_like_new_apa_reference_start(s):
                count += 1
    return count


def _find_reference_heading(lines: List[str], style_hint: str) -> Tuple[int, str]:
    candidates = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        for pat in REF_HEADINGS:
            if re.search(pat, s, re.I):
                candidates.append((i, ""))
        m = REF_HEADING_RELAXED.search(s)
        if m and m.start() <= 4 and len(s) <= 160:
            tail = s[m.end():].strip(" :-\t")
            if not _looks_like_toc_references_line(s, tail):
                candidates.append((i, tail))
    
    for i, tail in candidates:
        lookahead = [ln for ln in lines[i+1:i+31] if ln.strip()]
        if _count_reference_like(lookahead, style_hint) >= 3:
            return i, tail
    return -1, ""


def _merge_reference_lines(raw_lines: List[str]) -> List[str]:
    raw_lines = [ln.strip() for ln in raw_lines if ln and ln.strip()]
    if not raw_lines:
        return []

    merged, cur = [], ""
    for ln in raw_lines:
        s = ln.strip()
        if not s:
            continue
        if _looks_like_new_numeric_reference_start(s) or _looks_like_new_apa_reference_start(s):
            if cur:
                merged.append(norm_space(cur))
            cur = s
        else:
            cur = (cur + " " + s) if cur else s
    if cur:
        merged.append(norm_space(cur))
    return [m for m in merged if len(m) >= 8]


def _split_embedded_numeric_refs(merged: List[str]) -> List[str]:
    out = []
    br_pat = re.compile(r"(?=(\[\s*\d+\s*\]\s+))")
    dot_pat = re.compile(r"(?=(\b\d+[\.\)]\s+))")
    
    for s in merged:
        s = s.strip()
        if not s:
            continue
        cuts = []
        for m in br_pat.finditer(s):
            if m.start(1) > 0:
                cuts.append(m.start(1))
        for m in dot_pat.finditer(s):
            if m.start(1) > 0:
                token = m.group(1).strip()
                num = re.match(r"^(\d+)", token)
                if num and YEAR_RE.fullmatch(num.group(1)):
                    continue
                cuts.append(m.start(1))
        if not cuts:
            out.append(s)
            continue
        cuts = sorted(set(cuts))
        prev = 0
        for pos in cuts:
            if part := s[prev:pos].strip():
                out.append(part)
            prev = pos
        if tail := s[prev:].strip():
            out.append(tail)
    return [x for x in out if len(x) >= 10]


# -----------------------------
# LLM-based Vancouver Citation Extraction
# -----------------------------

def split_into_chunks(text: str, max_chars: int = 3000) -> List[str]:
    """Split text into chunks at sentence boundaries."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current_chunk = []
    current_length = 0
    
    for sentence in sentences:
        if current_length + len(sentence) > max_chars and current_chunk:
            chunks.append(' '.join(current_chunk))
            current_chunk = [sentence]
            current_length = len(sentence)
        else:
            current_chunk.append(sentence)
            current_length += len(sentence)
    
    if current_chunk:
        chunks.append(' '.join(current_chunk))
    
    return chunks


def call_llm_for_citations(text: str) -> List[str]:
    """Call LLM API to extract citations from text."""
    
    prompt = f"""You are an expert at identifying Vancouver-style citations in academic text.

Vancouver citation style uses numbers in various formats:
- (1), [1], ¹ (superscript), or just 1
- Multiple citations: (1,2,3), [1-5], (1,2,4-7,9)
- Author + citation: Smith et al. (1) found that...
- With page numbers: (1 p23), [2 pp45-67]

IMPORTANT: Do NOT extract:
- Years (like 2022, 1999) unless they're clearly citations
- Page numbers (like p. 23, pp. 45-67)
- Table/figure numbers (Table 1, Figure 2)
- Section numbers (Section 3, Chapter 4)
- Statistical numbers (50%, 100 participants)
- Currency amounts ($100, GHS 2,650)

Extract ONLY genuine citation numbers from the text.

If you see ranges like "1-5", expand them to individual numbers: 1,2,3,4,5.
If you see multiple citations like "1,2,3", list each number separately.

Return a JSON array of strings, each being a citation number.
Example: ["1","2","3","4","5"]

Text: {text}

Return ONLY the JSON array, no other text."""
    
    try:
        config = LLM_CONFIG
        
        if config["provider"] == "openai":
            response = requests.post(
                config["api_url"],
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": config["model"],
                    "messages": [
                        {"role": "system", "content": "You extract Vancouver citation numbers. Return JSON array only."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": config["temperature"],
                    "max_tokens": config["max_tokens"]
                },
                timeout=config["timeout"]
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                
                # Extract JSON array
                json_match = re.search(r'\[.*\]', content, re.DOTALL)
                if json_match:
                    citations = json.loads(json_match.group())
                    # Ensure all items are strings and look like citation numbers
                    return [str(c) for c in citations if str(c).isdigit()]
        
        elif config["provider"] == "anthropic":
            response = requests.post(
                config["api_url"],
                headers={
                    "x-api-key": config["api_key"],
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                },
                json={
                    "model": config["model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": config["max_tokens"],
                    "temperature": config["temperature"]
                },
                timeout=config["timeout"]
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result['content'][0]['text']
                json_match = re.search(r'\[.*\]', content, re.DOTALL)
                if json_match:
                    citations = json.loads(json_match.group())
                    return [str(c) for c in citations if str(c).isdigit()]
        
        elif config["provider"] == "deepseek":
            response = requests.post(
                config["api_url"],
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": config["model"],
                    "messages": [
                        {"role": "system", "content": "You extract Vancouver citation numbers. Return JSON array only."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": config["temperature"],
                    "max_tokens": config["max_tokens"]
                },
                timeout=config["timeout"]
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                json_match = re.search(r'\[.*\]', content, re.DOTALL)
                if json_match:
                    citations = json.loads(json_match.group())
                    return [str(c) for c in citations if str(c).isdigit()]
        
        return []
        
    except Exception as e:
        print(f"LLM call failed: {e}")
        if LLM_CONFIG["fallback_to_rule_based"]:
            return extract_vancouver_citations_fallback(text)
        return []


def extract_vancouver_citations_fallback(text: str) -> List[str]:
    """Fallback rule-based Vancouver citation extraction."""
    citations = []
    
    # Pattern for bracketed citations
    for match in re.finditer(r'[\(\[]\s*(\d{1,4}(?:\s*[-–,]\s*\d{1,4})*)\s*[\)\]]', text):
        content = match.group(1)
        for part in re.split(r'\s*,\s*', content):
            if '-' in part or '–' in part:
                parts = re.split(r'[-–]', part)
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    start, end = int(parts[0]), int(parts[1])
                    if 1 <= start <= end <= 9999 and (end - start) <= 50:
                        citations.extend([str(i) for i in range(start, end + 1)])
            elif part.isdigit():
                num = int(part)
                if not (1900 <= num <= 2099):  # Filter out years
                    citations.append(part)
    
    # Author + citation pattern
    for match in re.finditer(r'([A-Z][a-z]+(?:\s+et al\.?)?)\s*[\(\[]\s*(\d+)\s*[\)\]]', text, re.I):
        citations.append(match.group(2))
    
    # Deduplicate
    seen = set()
    return [x for x in citations if not (x in seen or seen.add(x))]


def extract_vancouver_citations_llm(text: str) -> List[str]:
    """
    Use LLM to extract Vancouver citations intelligently.
    Returns list of citation numbers.
    """
    # Split text into chunks
    chunks = split_into_chunks(text, max_chars=3000)
    all_citations = []
    
    for chunk in chunks:
        citations = call_llm_for_citations(chunk)
        all_citations.extend(citations)
    
    # Deduplicate while preserving order
    seen = set()
    return [x for x in all_citations if not (x in seen or seen.add(x))]


# -----------------------------
# Citation extractors
# -----------------------------
def extract_author_year_citations(text: str) -> List[str]:
    t = text.replace("\u2019", "'")
    out = []

    # Parenthetical citations
    for m in re.finditer(r"\(([^()]{0,260}?\b(?:19|20)\d{2}[a-z]?\b[^()]{0,260}?)\)", t):
        inside = m.group(1).strip()
        if YEAR_RE.fullmatch(inside) and not re.search(r"[A-Za-z]", inside):
            continue
        for ch in [c.strip() for c in inside.split(";") if c.strip()]:
            ch2 = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", ch, re.I).strip()
            if YEAR_RE.search(ch2):
                out.append(norm_space(ch2))

    # Narrative citations
    NAME = r"[A-Z][A-Za-z'\-]+(?:'s)?"
    AMP = r"(?:&|and)"
    AUTHOR_LIST = rf"{NAME}(?:\s*,\s*{NAME}){{0,10}}(?:\s*,?\s*{AMP}\s*{NAME})?"
    for m in re.finditer(rf"\b({AUTHOR_LIST}|{NAME}\s+et\s+al\.)\s*\(\s*((?:19|20)\d{{2}}[a-z]?)\s*\)?", t):
        author = re.sub(r"(’s|'s)\b", "", m.group(1)).strip()
        year = m.group(2).strip()
        out.append(norm_space(f"{author}, {year}"))

    return [c for c in out if c]


def extract_numeric_citations(text: str, style: str = "ieee") -> List[str]:
    """Extract numeric citations based on style."""
    
    if style == "vancouver":
        return extract_vancouver_citations_llm(text)
    
    # IEEE or generic numeric
    t = text or ""
    out = []
    
    t = re.sub(r"(?:table|figure|fig\.?|eq\.?|equation)\s+\d+", " ", t, flags=re.I)
    
    for m in re.finditer(r"\[\s*(\d{1,4}(?:\s*[-–,]\s*\d{1,4})*)\s*\]", t):
        content = m.group(1)
        for part in re.split(r'\s*,\s*', content):
            if '-' in part or '–' in part:
                parts = re.split(r'[-–]', part)
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    start, end = int(parts[0]), int(parts[1])
                    if 1 <= start <= end <= 9999 and (end - start) <= 50:
                        out.extend([str(i) for i in range(start, end + 1)])
            elif part.isdigit():
                out.append(part)
    
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _expand_citation_range(match) -> List[str]:
    groups = match.groups()
    if not groups or not groups[0]:
        return []
    nums = []
    start = int(groups[0])
    if groups[1]:
        end = int(groups[1])
        if start <= end and (end - start) <= 50:
            nums.extend([str(i) for i in range(start, end + 1)])
        else:
            nums.extend([str(start), str(end)])
    else:
        nums.append(str(start))
    if groups[2]:
        start2 = int(groups[2])
        if groups[3]:
            end2 = int(groups[3])
            if start2 <= end2 and (end2 - start2) <= 50:
                nums.extend([str(i) for i in range(start2, end2 + 1)])
            else:
                nums.extend([str(start2), str(end2)])
        else:
            nums.append(str(start2))
    return nums


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
    if m:
        year = m.group(1)
        left = s_clean[: m.start()].strip()
    else:
        m2 = re.search(r"\b(" + YEAR + r")\b", s_clean)
        if not m2:
            return None
        year = m2.group(1)
        left = s_clean[: m2.start()].strip()

    author_key = _first_author_or_org_key(left)
    return RefAY(reference_full=s_clean, key=f"{author_key}|{year}".lower()) if author_key else None


def parse_reference_numeric(ref: str, style: str = "ieee") -> Optional[RefNum]:
    s = norm_space(ref)
    if not s:
        return None
    
    patterns = [
        (r"^\[\s*(\d+)\s*\]\s*(.+)$", "bracket"),
        (r"^(\d+)\.\s*(.+)$", "dot"),
        (r"^\(\s*(\d+)\s*\)\s*(.+)$", "paren"),
        (r"^(\d+)\s+(.+)$", "space"),
    ]
    
    for pattern, _ in patterns:
        m = re.match(pattern, s)
        if m:
            num = m.group(1)
            num_int = int(num)
            if 1900 <= num_int <= 2099:
                continue
            body = norm_space(m.group(2))
            if len(body) > 20:
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


def reconcile_author_year(citations: List[str], references: List[RefAY]) -> Tuple:
    ref_map = {r.key: r.reference_full for r in references}
    alias_map = dict(ref_map)
    cite_counts = Counter()
    parsed_cites = []

    for c in citations:
        parsed = _parse_author_year_from_cite(c)
        if not parsed:
            continue
        auth, year = parsed
        key = f"{auth}|{year}".lower()
        
        if key in alias_map:
            cite_counts[alias_map[key]] += 1
            parsed_cites.append((alias_map[key], c, ""))
        else:
            parsed_cites.append(("", c, ""))

    c2r = []
    missing = Counter()
    for ref, c, flags in parsed_cites:
        if ref:
            c2r.append({"status": "matched", "in_text": c, "matched_reference": ref, "flags": flags})
        else:
            c2r.append({"status": "not_found", "in_text": c, "matched_reference": "", "flags": ""})
            missing[c] += 1

    r2c = []
    uncited = []
    for r in references:
        times = cite_counts.get(r.reference_full, 0)
        if times == 0:
            uncited.append(r.reference_full)
        r2c.append({"times_cited": times, "reference": r.reference_full, "cited_by": []})

    missing_rows = [{"citation_in_text": k, "count_in_text": v} for k, v in missing.most_common()]
    return c2r, r2c, missing_rows, uncited, len(set(citations))


def reconcile_numeric(citations: List[str], references: List[RefNum], style: str = "ieee") -> Tuple:
    ref_by_num = {r.num: r.reference_full for r in references}
    cite_counts = Counter(citations)
    
    c2r = []
    missing = Counter()
    for num in citations:
        if num in ref_by_num:
            c2r.append({"status": "matched", "in_text": f"[{num}]", "matched_reference": ref_by_num[num], "flags": ""})
        else:
            c2r.append({"status": "not_found", "in_text": f"[{num}]", "matched_reference": "", "flags": ""})
            missing[f"[{num}]"] += 1

    r2c = []
    uncited = []
    for r in references:
        times = cite_counts.get(r.num, 0)
        if times == 0:
            uncited.append(r.reference_full)
        r2c.append({"times_cited": times, "reference": r.reference_full, "cited_by": [f"[{r.num}]"] if times else []})

    missing_rows = [{"citation_in_text": k, "count_in_text": v} for k, v in missing.most_common()]
    return c2r, r2c, missing_rows, uncited, len(set(citations))


# -----------------------------
# Chunked processing
# -----------------------------
def _iter_text_chunks(text: str, chunk_size: int = 300000, overlap: int = 2000):
    n = len(text)
    if n <= chunk_size:
        yield text
        return
    step = max(1, chunk_size - overlap)
    for i in range(0, n, step):
        yield text[i:min(n, i + chunk_size)]


def _extract_author_year_citations_chunked(text: str) -> List[str]:
    seen = set()
    out = []
    for chunk in _iter_text_chunks(text):
        for c in extract_author_year_citations(chunk):
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out


def _extract_numeric_citations_chunked(text: str, style: str = "ieee") -> List[str]:
    if style == "vancouver":
        # For Vancouver, use smaller chunks for LLM
        seen = set()
        out = []
        for chunk in _iter_text_chunks(text, chunk_size=3000):
            for c in extract_numeric_citations(chunk, style):
                if c not in seen:
                    seen.add(c)
                    out.append(c)
        return out
    else:
        seen = set()
        out = []
        for chunk in _iter_text_chunks(text):
            for c in extract_numeric_citations(chunk, style):
                if c not in seen:
                    seen.add(c)
                    out.append(c)
        return out


# -----------------------------
# Public API
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

    name = filename.lower().strip() if filename else ""
    style_s = style.lower().strip()

    is_numeric = any(x in style_s for x in ["ieee", "vancouver", "numeric"])
    style_hint = "numeric" if is_numeric else "apa"

    try:
        if name.endswith(".docx"):
            main_text, ref_lines, ref_msg = read_docx_split_main_and_refs(file_bytes)
            refs_raw = _merge_reference_lines(ref_lines)
            if style_hint == "numeric":
                refs_raw = _split_embedded_numeric_refs(refs_raw)

        elif name.endswith(".pdf"):
            full_text = read_pdf_text(file_bytes)
            main_text, refs_raw, ref_msg = extract_references_from_pdf(full_text)
            if style_hint == "numeric":
                refs_raw = _split_embedded_numeric_refs(refs_raw)

        else:
            return {"error": "Upload a DOCX or PDF"}

    except Exception as e:
        return {"error": f"Error processing file: {str(e)}"}

    too_large = len(main_text) > 2_000_000

    if style_hint == "apa":
        cites = _extract_author_year_citations_chunked(main_text) if too_large else extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in refs_raw if parse_reference_author_year(r)]
        c2r, r2c, missing, uncited, intext_count = reconcile_author_year(cites, refs)
        ref_count = len(refs)

    else:
        # Extract citations with appropriate style
        if too_large:
            cites = _extract_numeric_citations_chunked(main_text, style_s)
        else:
            cites = extract_numeric_citations(main_text, style_s)
        
        # Parse references
        refs = []
        for r in refs_raw:
            parsed = parse_reference_numeric(r, style_s)
            if parsed:
                refs.append(parsed)
        
        c2r, r2c, missing, uncited, intext_count = reconcile_numeric(cites, refs, style_s)
        ref_count = len(refs)

    missing_unique = len(missing)
    match_rate = 100.0 * (intext_count - missing_unique) / intext_count if intext_count > 0 else 0.0

    return {
        "filename": filename,
        "style": style_s,
        "engine_build": ENGINE_BUILD,
        "reference_detection_message": ref_msg,
        "summary": {
            "in_text_citations_found": intext_count,
            "reference_entries_found": ref_count,
            "missing_in_references": missing_unique,
            "uncited_references": len(uncited),
            "match_rate": round(match_rate, 1),
        },
        "missing_in_references": missing,
        "uncited_references": uncited,
        "reconciliation_intext_to_reference": c2r,
        "reconciliation_reference_to_intext": r2c,
        "references_raw": refs_raw,
    }
