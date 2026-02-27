# engine.py
__version__ = "1.5.0"

import re
import io
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter

ENGINE_BUILD = "commercial-2026-02-27-optimized"

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
]

# Words/phrases that often precede citations in prose
DISCOURSE_PREFIXES = {
    "see", "e.g", "eg", "i.e", "ie",
    "as", "in", "for", "from", "to", "at", "on", "by", "with", "within",
    "according", "adapted", "based", "cited", "citing", "reported",
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
        if len(cand) >= 2:
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
                lines.extend(_extract(z.read(t)))
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
# PDF extraction - COMPLETELY REWRITTEN
# -----------------------------
def read_pdf_text(file_bytes: bytes) -> str:
    """Extract text from PDF with better formatting preservation."""
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")

    out = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            try:
                text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                # Preserve line breaks better
                text = text.replace("\x00", " ")
                text = re.sub(r"-\n", "", text)  # Fix hyphenation
                out.append(text)
            except Exception:
                out.append("")
    return "\n".join(out)


def extract_references_from_pdf(text: str) -> Tuple[str, List[str], str]:
    """
    Extract references from PDF text with multiple strategies.
    Returns (main_text, references_list, message)
    """
    lines = text.splitlines()
    
    # First, try to find the references section
    ref_start = -1
    ref_heading = ""
    
    # Strategy 1: Look for common reference headings
    heading_patterns = [
        r'^\s*REFERENCES\s*$',
        r'^\s*BIBLIOGRAPHY\s*$',
        r'^\s*WORKS\s+CITED\s*$',
        r'^\s*LITERATURE\s+CITED\s*$',
        r'^\s*REFERENCES\s*$',
    ]
    
    for i, line in enumerate(lines):
        line_clean = line.strip()
        for pattern in heading_patterns:
            if re.search(pattern, line_clean, re.I):
                # Verify this is near the end of document
                if i > len(lines) * 0.6:  # After 60% of document
                    ref_start = i
                    ref_heading = line_clean
                    break
        if ref_start != -1:
            break
    
    # Strategy 2: If no heading found, look for reference-like patterns
    if ref_start == -1:
        ref_candidates = []
        for i, line in enumerate(lines):
            if i > len(lines) * 0.6:  # Only check latter part
                line = line.strip()
                # Look for patterns like [1] Author or 1. Author
                if re.match(r'^\[\d+\]\s+[A-Z]', line) or \
                   (re.match(r'^\d+\.\s+[A-Z]', line) and not re.match(r'^\d{4}\.', line)):
                    ref_candidates.append(i)
        
        if ref_candidates:
            ref_start = ref_candidates[0]
            ref_heading = "REFERENCES (detected)"
    
    if ref_start == -1:
        # No references found
        return text, [], "No references section found."
    
    # Extract main text (everything before references)
    main_text = "\n".join(lines[:ref_start]).strip()
    
    # Extract references using robust pattern matching
    references = []
    current_ref = ""
    in_refs = True
    
    # Detect reference format
    sample_line = ""
    for i in range(ref_start + 1, min(ref_start + 10, len(lines))):
        if lines[i].strip():
            sample_line = lines[i].strip()
            break
    
    # Determine format
    is_ieee = bool(re.match(r'^\[\d+\]', sample_line))
    is_numbered = bool(re.match(r'^\d+\.', sample_line)) and not re.match(r'^\d{4}\.', sample_line)
    
    for i in range(ref_start + 1, len(lines)):
        line = lines[i].rstrip()
        
        # Skip empty lines at the beginning
        if not line and not current_ref:
            continue
        
        if not line:
            # Empty line might separate references
            if current_ref and len(current_ref) > 20:
                references.append(clean_reference_text(current_ref))
                current_ref = ""
            continue
        
        # Check if this starts a new reference
        is_new_ref = False
        if is_ieee:
            is_new_ref = bool(re.match(r'^\[\d+\]', line))
        elif is_numbered:
            is_new_ref = bool(re.match(r'^\d+\.', line))
        else:
            # Auto-detect
            is_new_ref = bool(re.match(r'^\[\d+\]', line)) or \
                        (bool(re.match(r'^\d+\.', line)) and not re.match(r'^\d{4}\.', line))
        
        if is_new_ref:
            if current_ref:
                references.append(clean_reference_text(current_ref))
            current_ref = line
        elif current_ref:
            # Continuation of previous reference
            if current_ref.endswith('-'):
                current_ref = current_ref[:-1] + line
            else:
                current_ref += " " + line
    
    # Add last reference
    if current_ref and len(current_ref) > 20:
        references.append(clean_reference_text(current_ref))
    
    # Filter out false positives (table numbers, figure captions, etc.)
    filtered_refs = []
    for ref in references:
        # Must have at least one author-like name or year
        has_author = bool(re.search(r'[A-Z][a-z]+,\s+[A-Z]\.', ref))
        has_year = bool(re.search(r'\b(19|20)\d{2}\b', ref))
        has_doi = bool(re.search(r'10\.\d{4,9}/', ref))
        has_journal = bool(re.search(r'Journal|Review|Letters|Proceedings', ref, re.I))
        
        # Must be reasonably long
        if len(ref) < 30:
            continue
            
        # Must not be a table/figure reference
        if re.search(r'Table\s+\d+|Figure\s+\d+', ref, re.I):
            continue
            
        if has_author or has_year or has_doi or has_journal:
            filtered_refs.append(ref)
    
    msg = f"Found {len(filtered_refs)} references using enhanced extraction."
    return main_text, filtered_refs, msg


def clean_reference_text(ref: str) -> str:
    """Clean up reference text."""
    # Normalize spaces
    ref = re.sub(r'\s+', ' ', ref).strip()
    
    # Fix common PDF artifacts
    ref = re.sub(r'-\s+', '', ref)
    ref = re.sub(r'\s+-\s+', '-', ref)
    
    # Remove leading/trailing punctuation
    ref = ref.strip('.,;:')
    
    return ref


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
# Citation extractors - with year filtering
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


def is_valid_citation_number(num: str, context: str) -> bool:
    """Determine if a number is a genuine citation vs year/page number."""
    num_int = int(num) if num.isdigit() else 0
    
    # Years (1900-2099) are NOT citations
    if 1900 <= num_int <= 2099:
        # Check if it's in a year context
        if re.search(r'\b(?:in|during|since|year)\s+' + re.escape(num), context, re.I):
            return False
        # If it's alone in brackets, it might be a citation like (2015)
        if re.search(r'[\(\[]\s*' + re.escape(num) + r'\s*[\)\]]', context):
            # But only if preceded by author-like text
            if re.search(r'[A-Z][a-z]+(?:\s+et al\.?)?\s*[\(\[]\s*' + re.escape(num), context, re.I):
                return True
        return False
    
    # Page numbers
    if re.search(r'[pP]\.?\s*' + re.escape(num) + r'\b', context):
        return False
    
    # Table/figure references
    if re.search(r'(?:table|figure|fig|eq|equation)\s+' + re.escape(num), context, re.I):
        return False
    
    # Section numbers
    if re.search(r'(?:section|chapter|part)\s+' + re.escape(num), context, re.I):
        return False
    
    # Numbers in brackets/parentheses are likely citations
    if re.search(r'[\(\[]\s*' + re.escape(num) + r'\s*[\)\]]', context):
        return True
    
    return False


def extract_numeric_citations(text: str, style: str = "ieee") -> List[str]:
    """Extract numeric citations with filtering for years and false positives."""
    t = text or ""
    out = []
    
    # Remove false positive contexts first
    t = re.sub(r"(?:table|figure|fig\.?|eq\.?|equation)\s+\d+", " ", t, flags=re.I)
    
    if style == "ieee":
        # IEEE: square brackets only
        for m in re.finditer(r"\[\s*(\d{1,4}(?:\s*[-–,]\s*\d{1,4})*)\s*\]", t):
            content = m.group(1)
            context = t[max(0, m.start()-30):min(len(t), m.end()+30)]
            
            # Parse numbers
            for part in re.split(r'\s*,\s*', content):
                if '-' in part or '–' in part:
                    start, end = map(int, re.split(r'[-–]', part)[:2])
                    if 1 <= start <= end <= 9999 and (end - start) <= 50:
                        for i in range(start, end + 1):
                            if is_valid_citation_number(str(i), context):
                                out.append(str(i))
                elif part.isdigit():
                    if is_valid_citation_number(part, context):
                        out.append(part)
    
    else:  # Vancouver or generic numeric
        # Bracketed citations
        for m in re.finditer(r"[\(\[]\s*(\d{1,4}(?:\s*[-–,]\s*\d{1,4})*)\s*[\)\]]", t):
            content = m.group(1)
            context = t[max(0, m.start()-30):min(len(t), m.end()+30)]
            
            for part in re.split(r'\s*,\s*', content):
                if '-' in part or '–' in part:
                    start, end = map(int, re.split(r'[-–]', part)[:2])
                    if 1 <= start <= end <= 9999 and (end - start) <= 50:
                        for i in range(start, end + 1):
                            if is_valid_citation_number(str(i), context):
                                out.append(str(i))
                elif part.isdigit():
                    if is_valid_citation_number(part, context):
                        out.append(part)
        
        # Author + citation
        for m in re.finditer(r'([A-Z][a-z]+(?:\s+et al\.?)?)\s*[\(\[]\s*(\d+)\s*[\)\]]', t, re.I):
            num = m.group(2)
            if is_valid_citation_number(num, m.group(0)):
                out.append(num)
    
    # Deduplicate
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
    
    # Try different patterns
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
            # Skip if it looks like a year
            if 1900 <= num_int <= 2099:
                continue
            body = norm_space(m.group(2))
            if len(body) > 20:
                return RefNum(reference_full=s, num=num)
    
    return None


# -----------------------------
# Reconciliation
# -----------------------------
def reconcile_author_year(citations: List[str], references: List[RefAY]) -> Tuple:
    ref_map = {r.key: r.reference_full for r in references}
    alias_map = dict(ref_map)
    cite_counts = Counter()
    parsed_cites = []

    for c in citations:
        # Simplified parsing for demo - in real code would be more robust
        ym = YEAR_RE.search(c)
        if not ym:
            continue
        year = ym.group(1)
        left = c[:ym.start()].strip(" ,;()")
        author_key = _first_author_or_org_key(left)
        if not author_key:
            continue
        
        key = f"{author_key}|{year}".lower()
        if key in alias_map:
            cite_counts[alias_map[key]] += 1
            parsed_cites.append((alias_map[key], c, ""))
        else:
            parsed_cites.append(("", c, ""))

    # Build results
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
