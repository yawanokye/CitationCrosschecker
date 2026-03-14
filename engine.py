# engine.py
__version__ = "1.6.0"

import re
import io
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter

ENGINE_BUILD = "commercial-2026-03-stable"

# Optional fuzzy matching
try:
    from rapidfuzz import fuzz
    FUZZ_OK = True
except Exception:
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


# ============================================================
# Data classes
# ============================================================

@dataclass
class RefAY:
    reference_full: str
    key: str


@dataclass
class RefNum:
    reference_full: str
    num: str


# ============================================================
# Helpers
# ============================================================

def _safe_str(x):
    try:
        return str(x)
    except Exception:
        return ""


def norm_space(s: str) -> str:
    s = s or ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def soft_lower(s: str) -> str:
    return norm_space(s).lower()


# ============================================================
# Reference Heading Detection
# ============================================================

REF_HEADINGS = [
    r"^\s*references?\s*$",
    r"^\s*bibliography\s*$",
    r"^\s*works\s+cited\s*$",
    r"^\s*literature\s+cited\s*$",
]


def _find_reference_heading(lines: List[str], style_hint="apa"):

    for i, line in enumerate(lines):

        s = (line or "").strip()

        if not s:
            continue

        for pat in REF_HEADINGS:
            if re.search(pat, s, re.I):
                return i, ""

    return -1, ""


# ============================================================
# Reference start detectors
# ============================================================

def _looks_like_new_numeric_reference_start(line: str):

    s = line.strip()

    if re.match(r'^\[\d+\]', s):
        return True

    if re.match(r'^\d+\.', s):
        return True

    return False


def _looks_like_new_apa_reference_start(line: str):

    s = line.strip()

    if re.match(r'^[A-Z][A-Za-z\-]+,\s+[A-Z]\.', s):
        return True

    if re.match(r'^[A-Z][A-Za-z\-]+\s+\([12][0-9]{3}', s):
        return True

    return False


# ============================================================
# PDF Processing
# ============================================================

def _pdf_page_to_lines(page):

    lines = []

    try:
        words = page.extract_words(
            use_text_flow=True,
            x_tolerance=2,
            y_tolerance=3
        ) or []
    except Exception:
        words = []

    if words:

        buckets = defaultdict(list)

        for w in words:
            top = int(round(float(w.get("top", 0)) / 3))
            buckets[top].append(w)

        for bucket in sorted(buckets):

            row = sorted(buckets[bucket], key=lambda x: float(x.get("x0", 0)))
            text = norm_space(" ".join(_safe_str(w.get("text")) for w in row))

            if text:
                lines.append(text)

        return lines

    try:
        text = page.extract_text() or ""
    except Exception:
        text = ""

    for ln in text.splitlines():
        s = norm_space(ln)
        if s:
            lines.append(s)

    return lines


def _remove_repeated_pdf_headers_footers(page_lines):

    top_counter = Counter()
    bottom_counter = Counter()

    for lines in page_lines:

        for ln in lines[:2]:
            top_counter[soft_lower(ln)] += 1

        for ln in lines[-2:]:
            bottom_counter[soft_lower(ln)] += 1

    repeated = {k for k,v in top_counter.items() if v>=2}
    repeated |= {k for k,v in bottom_counter.items() if v>=2}

    cleaned = []

    for lines in page_lines:

        new_lines = []

        for i,ln in enumerate(lines):

            key = soft_lower(ln)

            if key in repeated and (i<2 or i>=len(lines)-2):
                continue

            if re.fullmatch(r"(?:page\s+)?\d+", ln.lower()):
                continue

            new_lines.append(ln)

        cleaned.append(new_lines)

    return cleaned


def _flatten_pdf_lines(page_lines):

    flat = []

    for lines in page_lines:

        for ln in lines:

            s = norm_space(ln)

            if not s:
                continue

            if flat:

                prev = flat[-1]

                if prev.endswith("-"):
                    flat[-1] = prev[:-1] + s
                    continue

                if len(prev)>20 and re.match(r"^[a-z,(]", s):
                    flat[-1] = prev + " " + s
                    continue

            flat.append(s)

    return flat


def read_pdf_split_main_and_refs(file_bytes, style_hint="apa"):

    page_lines = []

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:

        for page in pdf.pages:
            page_lines.append(_pdf_page_to_lines(page))

    page_lines = _remove_repeated_pdf_headers_footers(page_lines)

    lines = _flatten_pdf_lines(page_lines)

    idx, tail = _find_reference_heading(lines)

    if idx == -1:

        full_text = "\n".join(lines)

        refs = extract_references_enhanced(full_text)

        return full_text, refs, "References detected heuristically"

    main = "\n".join(lines[:idx])

    ref_lines = lines[idx+1:]

    refs = _merge_reference_lines(ref_lines)

    return main, refs, "References heading detected"


# ============================================================
# Reference merging
# ============================================================

def _merge_reference_lines(raw_lines):

    raw_lines = [x.strip() for x in raw_lines if x.strip()]

    merged = []

    cur = ""

    for ln in raw_lines:

        new_ref = (
            _looks_like_new_numeric_reference_start(ln)
            or _looks_like_new_apa_reference_start(ln)
        )

        if new_ref:

            if cur:
                merged.append(norm_space(cur))

            cur = ln

        else:

            if cur.endswith("-"):
                cur = cur[:-1] + ln
            else:
                cur += " " + ln

    if cur:
        merged.append(norm_space(cur))

    return [x for x in merged if len(x)>15]


# ============================================================
# Reference Extraction
# ============================================================

def extract_references_enhanced(text):

    refs = []

    lines = text.splitlines()

    for ln in lines:

        if re.search(r'\b(19|20)\d{2}\b', ln) and len(ln)>30:
            refs.append(norm_space(ln))

    return refs


# ============================================================
# Citation extraction
# ============================================================

YEAR_RE = re.compile(r'\b(19|20)\d{2}\b')

def extract_author_year_citations(text):

    out = []

    for m in re.finditer(r'\(([^\)]+?\d{4}[^\)]*)\)', text):

        c = m.group(1)

        if YEAR_RE.search(c):
            out.append(norm_space(c))

    return out


# ============================================================
# Reconciliation
# ============================================================

def reconcile_author_year(citations, references):

    ref_map = {}

    for r in references:

        m = YEAR_RE.search(r)

        if not m:
            continue

        year = m.group()

        key = year

        ref_map[key] = r

    c2r = []
    missing = Counter()

    for c in citations:

        m = YEAR_RE.search(c)

        if not m:

            continue

        year = m.group()

        if year in ref_map:

            c2r.append({
                "status":"matched",
                "in_text":c,
                "matched_reference":ref_map[year],
                "flags":""
            })

        else:

            c2r.append({
                "status":"not_found",
                "in_text":c,
                "matched_reference":"",
                "flags":""
            })

            missing[c]+=1

    r2c = []
    cite_counts = Counter()

    for row in c2r:
        if row["matched_reference"]:
            cite_counts[row["matched_reference"]]+=1

    uncited=[]

    for r in references:

        times=cite_counts.get(r,0)

        if times==0:
            uncited.append(r)

        r2c.append({
            "times_cited":times,
            "reference":r,
            "cited_by":[]
        })

    missing_rows=[{"citation_in_text":k,"count_in_text":v} for k,v in missing.items()]

    return c2r,r2c,missing_rows,uncited,len(citations)


# ============================================================
# Main engine
# ============================================================

def run_crosscheck(file_bytes, filename, style="apa"):

    name = filename.lower()

    if name.endswith(".docx"):

        doc = Document(io.BytesIO(file_bytes))

        lines = [norm_space(p.text) for p in doc.paragraphs if p.text]

        text = "\n".join(lines)

        refs = extract_references_enhanced(text)

        main_text = text

    elif name.endswith(".pdf"):

        main_text, refs, msg = read_pdf_split_main_and_refs(file_bytes)

    else:

        return {"error":"Unsupported file"}

    cites = extract_author_year_citations(main_text)

    c2r,r2c,missing,uncited,count = reconcile_author_year(cites,refs)

    match_rate = 0

    if count>0:
        match_rate = 100*(count-len(missing))/count

    return {

        "filename":filename,

        "summary":{
            "in_text_citations_found":len(cites),
            "reference_entries_found":len(refs),
            "missing_in_references":len(missing),
            "uncited_references":len(uncited),
            "match_rate":round(match_rate,1)
        },

        "missing_in_references":missing,
        "uncited_references":uncited,

        "reconciliation_intext_to_reference":c2r,
        "reconciliation_reference_to_intext":r2c,

        "references_raw":refs
    }
