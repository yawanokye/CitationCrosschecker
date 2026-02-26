from __future__ import annotations
"""
Citation Crosschecker - Commercial Grade Engine (Mode C)
- Dual-stream in-text detection (strict + loose) with narrative/geo filtering
- High-recall reconciliation with aliasing + weighted fuzzy backstop
- Commercial-safe behaviour: keeps false 'Missing' low, reduces false 'Uncited'
- Import-safe: no server-only imports

Drop-in: replace engine.py with this file.
"""

__version__ = "1.4.0-c"

import io
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict, Counter

# Optional deps (safe on server if absent)
try:
    from docx import Document  # type: ignore
    DOCX_OK = True
except Exception:
    Document = None
    DOCX_OK = False

try:
    import pdfplumber  # type: ignore
    PDF_OK = True
except Exception:
    pdfplumber = None
    PDF_OK = False

try:
    from rapidfuzz import fuzz as RFUZZ  # type: ignore
    HAVE_RAPIDFUZZ = True
except Exception:
    RFUZZ = None
    HAVE_RAPIDFUZZ = False


# -----------------------------
# Regex + constants
# -----------------------------
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

# Discourse prefixes (case-insensitive). Keep short, stable.
DISCOURSE_PREFIXES = {
    "see", "e.g", "eg", "i.e", "ie",
    "as", "in", "for", "from", "to", "at", "on", "by", "with", "within",
    "according", "adapted", "based", "cited", "citing", "reported",
    "like",
    "however", "similarly", "therefore", "thus", "hence", "consequently",
    "moreover", "furthermore", "additionally", "meanwhile", "nonetheless",
    "nevertheless", "overall", "generally", "specifically", "particularly",
    "importantly", "indeed",
    "for instance", "instance", "for example", "example",
}

# Geo tokens that often precede citations and should NOT be treated as authors.
GEO_PREFIXES = {
    "africa", "europe", "asia", "america", "north america", "south america",
    "sub-saharan africa", "ssa",
    "ghana", "nigeria", "kenya", "south africa", "tanzania", "uganda",
    "china", "japan", "india", "uk", "u.k", "usa", "u.s.a", "united states",
    "germany", "france", "italy", "spain",
}

# Narrative false positives (not citations)
NARRATIVE_SINGLE_TOKENS = {
    "crisis", "war", "scandal", "revolution", "coup", "katrina",
    "pandemic", "covid", "covid19", "covid-19",
    "estimates",
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

NON_NAME_AUTHOR_KEYS = {
    "survey","field","work","fieldwork","data","dataset","table","tables","figure","fig","figures",
    "chapter","section","appendix","appendices","annex","equation","eq","model","models",
    "analysis","results","method","methods","discussion","introduction","conclusion",
    "study","paper","thesis","report","source","sources","author","authors",
    "however","similarly","therefore","thus","hence","consequently","moreover","furthermore",
    "additionally","meanwhile","nonetheless","nevertheless","overall","generally","specifically",
    "particularly","importantly","indeed","instance","example",
}

_LEAD_NUM_RE = re.compile(r"^\s*(?:\[\s*\d{1,4}\s*\]|\(?\s*\d{1,4}\s*\)?|\d{1,4})\s*[\.)\]]\s*")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s)]+", re.I)

_REF_STOPWORDS = {
    "the","a","an","and","or","of","in","on","for","to","with","from","at","by","as",
    "ed","eds","edition","vol","volume","no","number","pp","pages","page",
}


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

def _strip_accents(s: str) -> str:
    s = s or ""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))

def _norm_ref_text(s: str) -> str:
    s = _strip_accents((s or "").lower())
    s = s.replace("&", " and ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def _strip_leading_reference_number(s: str) -> str:
    s0 = norm_space(s)
    s0 = _LEAD_NUM_RE.sub("", s0)
    return s0.strip()

def _looks_like_toc_references_line(s: str, tail: str) -> bool:
    tail = (tail or "").strip()
    if tail and re.fullmatch(r"\d{1,4}", tail):
        return True
    return bool(re.search(r"\.{2,}\s*\d{1,4}\s*$", s or ""))

def _looks_like_heading_line(s: str) -> bool:
    s0 = (s or "").strip()
    if not s0:
        return False
    if len(s0) > 120:
        return False
    letters = re.sub(r"[^A-Za-z]", "", s0)
    if letters and letters.isupper() and len(letters) >= 6:
        return True
    return bool(re.match(r"^[A-Z][A-Za-z0-9\s\-,:]{3,}$", s0))

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


def _looks_like_person_author(s: str) -> bool:
    s0 = norm_space(s)
    if re.search(r"\b[A-Z][A-Za-z'\-]+,\s*(?:[A-Z]\.\s*){1,4}", s0):
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
        t0 = re.sub(r"[^A-Za-z]", "", toks[0])
        if t0 and t0.isupper() and len(t0) >= 2:
            return True
    run = best = 0
    for w in toks[:16]:
        wc = re.sub(r"[^A-Za-z]", "", w)
        ok = bool(wc and ((wc.isupper() and 2 <= len(wc) <= 12) or re.match(r"^[A-Z][a-z]{2,}$", wc)))
        if ok:
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
    return True

def _is_plausible_reference_entry(s: str) -> bool:
    s0 = _strip_leading_reference_number(s)
    if not s0 or len(s0) < 18:
        return False
    ym = YEAR_RE.search(s0)
    if not ym:
        return False
    left = s0[: ym.start()].strip()
    author_ok = (_looks_like_person_author(left) or _looks_like_org_author(left) or
                 _looks_like_person_author(s0[:120]) or _looks_like_org_author(s0[:120]))
    if not author_ok:
        return False
    after = s0[ym.end():].lstrip(" ).,;:-")
    after_title = after.split(".", 1)[0].strip()
    if len(after_title) < 6 and "," in after:
        after_title = after.split(",", 1)[0].strip()
    return _looks_like_title_piece(after_title)


def _looks_like_new_numeric_reference_start(s: str) -> bool:
    s0 = (s or "").strip()
    if not s0:
        return False
    if re.match(r"^\[\s*\d{1,4}\s*\]\s+\S", s0):
        return True
    if re.match(r"^\(\s*\d{1,4}\s*\)\s+\S", s0):
        return True
    m = re.match(r"^(\d{1,4})([\.)])\s+(.+)$", s0)
    if m and not YEAR_RE.fullmatch(m.group(1)):
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
        a = re.sub(r"[^A-Za-z,\.\-\s&/\u2013\u2014-]", "", m.group(1)).strip()
        return len(a) >= 3
    return False

def _count_reference_like(lines: List[str], style_hint: str) -> int:
    c = 0
    for ln in lines:
        s = (ln or "").strip()
        if not s:
            continue
        if style_hint == "numeric":
            c += int(_looks_like_new_numeric_reference_start(s))
        else:
            c += int(_looks_like_new_apa_reference_start(s))
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
        lookahead = [ln for ln in lines[i+1:i+31] if (ln or "").strip()]
        if _count_reference_like(lookahead, style_hint=style_hint) >= 3:
            return i, tail
    return -1, ""

def _truncate_reference_block(lines: List[str], style_hint: str) -> List[str]:
    out: List[str] = []
    ref_like_seen = 0
    def _is_ref_like(ln: str) -> bool:
        return _looks_like_new_numeric_reference_start(ln) if style_hint == "numeric" else _looks_like_new_apa_reference_start(ln)
    for i, ln in enumerate(lines):
        s = (ln or "").strip()
        if not s:
            continue
        if _is_ref_like(s):
            ref_like_seen += 1
        if ref_like_seen >= 3 and (REF_END_HEADING_RE.search(s) or (_looks_like_heading_line(s) and re.search(r"\b(appendix|appendices|annex|supplement|supporting|additional)\b", s, re.I))):
            look = [x for x in lines[i:i+25] if (x or "").strip()]
            look_ref = sum(1 for x in look if _is_ref_like((x or "").strip()))
            if look_ref <= 1:
                break
        out.append(ln)
    return out


def _merge_reference_lines(raw_lines: List[str]) -> List[str]:
    raw_lines = [ln.strip() for ln in raw_lines if ln and ln.strip()]
    if not raw_lines:
        return []
    merged: List[str] = []
    cur = ""
    for ln in raw_lines:
        s = ln.strip()
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
    if not lines and Document is not None:
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
                    tail = t[m.end():].strip(" :-\t")
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

def read_pdf_text(file_bytes: bytes) -> str:
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")
    out: List[str] = []
    assert pdfplumber is not None
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            out.append((text or "").replace("\x00", " "))
    return "\n".join(out)


NAME = r"[A-Z][A-Za-z'\-]+(?:'s)?"
AMP = r"(?:&|and|＆)"
AUTHOR_LIST = rf"{NAME}(?:\s*,\s*{NAME}){{0,10}}(?:\s*,?\s*{AMP}\s*{NAME})?"
_PAREN_BLOCK_RE = re.compile(r"\(([^()]{0,260}?\b(?:19|20)\d{2}[a-z]?\b[^()]{0,260}?)\)")
_NARR_RE = re.compile(
    rf"\b("
    rf"(?:{AUTHOR_LIST})"
    rf"|(?:{NAME}\s+{AMP}\s+{NAME})"
    rf"|(?:{NAME}\s+et\s+al\.)"
    rf")\s*\(\s*((?:19|20)\d{{2}}[a-z]?)\s*\)?"
)
_BARE_RE = re.compile(
    rf"(?<!\w)({NAME}(?:\s+et\s+al\.)?|[A-Z][A-Za-z&/\-]{{2,}})\s*,\s*((?:19|20)\d{{2}}[a-z]?)\b"
)

def extract_author_year_citations_strict(text: str) -> List[str]:
    t = (text or "").replace("\u2019", "'")
    out: List[str] = []
    for m in _PAREN_BLOCK_RE.finditer(t):
        inside = (m.group(1) or "").strip()
        if YEAR_RE.fullmatch(inside) and not re.search(r"[A-Za-z]", inside):
            continue
        chunks = [c.strip() for c in inside.split(";") if c.strip()]
        for ch in chunks:
            ch2 = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", ch, flags=re.I).strip()
            if YEAR_RE.search(ch2):
                out.append(norm_space(ch2))
    for m in _NARR_RE.finditer(t):
        author = re.sub(r"(’s|'s)\b", "", m.group(1).strip()).strip()
        year = m.group(2).strip()
        out.append(norm_space(f"{author}, {year}"))
    return [c for c in out if c]

def extract_author_year_citations_loose(text: str) -> List[str]:
    t = (text or "").replace("\u2019", "'")
    out: List[str] = []
    for m in _BARE_RE.finditer(t):
        left = m.group(1).strip()
        year = m.group(2).strip()
        cand = f"{left}, {year}"
        if _is_likely_narrative_citation(left, year, cand):
            continue
        if strip_punct(left) in NON_NAME_AUTHOR_KEYS:
            continue
        out.append(norm_space(cand))
    return out

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
                out.extend([str(k) for k in range(lo, hi+1)])
            else:
                out.append(str(a)); out.append(str(b2))
        else:
            out.append(str(a))
    return out


@dataclass
class RefAY:
    reference_full: str
    key: str
    year_base: str
    author_blob_norm: str

@dataclass
class RefNum:
    reference_full: str
    num: str

def _first_author_or_org_key(author_left: str) -> str:
    s = norm_space(author_left)
    m = re.search(r"\(([A-Z][A-Z0-9/&\-]{1,15})\)", s)
    if m:
        return strip_punct(m.group(1))
    s = _strip_leading_reference_number(s)
    s = re.sub(r"\(\s*(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?\s*\).*", "", s).strip()
    s = re.sub(r"(’s|'s)\b", "", s)
    if "," in s:
        cand = s.split(",", 1)[0].strip()
        if cand:
            return strip_punct(cand)
    m2 = re.match(r"^\s*([A-Z][A-Za-z'\-]+)\s+[A-Z]{1,3}\b", s)
    if m2:
        return strip_punct(m2.group(1))
    toks = [t for t in re.split(r"\s+", s) if t]
    if toks:
        return strip_punct(toks[0])
    return ""

def _surnames_from_author_blob(left: str) -> List[str]:
    s = (left or "").strip()
    if not s:
        return []
    s = s.replace("&", " and ")
    s = re.sub(r"\bet\s+al\.?\b", "", s, flags=re.I)
    s = re.sub(r"(’s|'s)\b", "", s)
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
        cand = re.sub(r"[^A-Za-z\-’' ]+", "", cand).strip().replace("’", "'")
        if len(cand) >= 2:
            out.append(cand.lower())
    seen=set(); final=[]
    for x in out:
        if x not in seen:
            seen.add(x); final.append(x)
    return final

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
        left = s_clean[:m.start()].strip()
    else:
        m2 = YEAR_RE.search(s_clean)
        if not m2:
            return None
        year = m2.group(1)
        left = s_clean[:m2.start()].strip()
    author_key = _first_author_or_org_key(left)
    if not author_key:
        return None
    year_base = _base_year(year)
    author_blob_norm = strip_punct(left)
    key = f"{author_key}|{year}".lower()
    return RefAY(reference_full=s_clean, key=key, year_base=year_base, author_blob_norm=author_blob_norm)

def parse_reference_numeric(ref: str) -> Optional[RefNum]:
    s = norm_space(ref)
    if not s:
        return None
    m = re.match(r"^\[\s*(\d{1,4})\s*\]\s*(.+)$", s)
    if m:
        num = m.group(1)
        body = _strip_leading_reference_number(norm_space(m.group(2)))
        if not _is_plausible_reference_entry(body):
            return None
        return RefNum(reference_full=s, num=num)
    m2 = re.match(r"^(\d{1,4})[\.)]\s*(.+)$", s)
    if m2 and not YEAR_RE.fullmatch(m2.group(1)):
        num = m2.group(1)
        body = _strip_leading_reference_number(norm_space(m2.group(2)))
        if not _is_plausible_reference_entry(body):
            return None
        return RefNum(reference_full=s, num=num)
    return None


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
    names = _surnames_from_author_blob(left)
    first_author = names[0] if names else _norm_ref_text(left)[:24]
    first_author = re.sub(r"[^a-z0-9\- ]+", "", _norm_ref_text(first_author))
    right = right.lstrip(" .,:;)-–—\"'[]")
    right2 = re.split(r"\.\s+|\.?$|\s+https?://|\s+doi:\s*", right, maxsplit=1, flags=re.I)[0]
    tokens = [re.sub(r"[^a-z0-9\-]+", "", t) for t in _norm_ref_text(right2).split()]
    tokens = [t for t in tokens if t and t not in _REF_STOPWORDS]
    title_stub = " ".join(tokens[:12])
    return (year, first_author, title_stub, doi)

def _cluster_references(refs: List[RefAY]) -> Dict[str, Dict[str, Any]]:
    by_doi: Dict[str, List[str]] = defaultdict(list)
    by_bucket: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    sigs: Dict[str, Tuple[str, str, str, str]] = {}
    for r in refs:
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
    for (_y, _a1), items in by_bucket.items():
        if len(items) <= 1:
            clusters.append(items); continue
        used = set()
        for i, rf_i in enumerate(items):
            if rf_i in used: continue
            used.add(rf_i)
            ti = sigs[rf_i][2]
            cluster = [rf_i]
            for rf_j in items[i+1:]:
                if rf_j in used: continue
                tj = sigs[rf_j][2]
                if not ti or not tj: continue
                if HAVE_RAPIDFUZZ:
                    score = max(RFUZZ.token_set_ratio(ti, tj), RFUZZ.partial_ratio(ti, tj))  # type: ignore
                else:
                    si = set(ti.split()); sj = set(tj.split())
                    score = int(round(100 * (len(si & sj) / max(1, len(si), len(sj)))))
                if score >= 88:
                    used.add(rf_j)
                    cluster.append(rf_j)
            clusters.append(cluster)
    mapping: Dict[str, Dict[str, Any]] = {}
    for cid, members in enumerate(clusters, start=1):
        canonical = max(members, key=lambda x: len(x or ""))
        for rf in members:
            mapping[rf] = {"cluster_id": cid, "canonical_ref": canonical, "is_duplicate": (rf != canonical)}
    return mapping


def _strip_leading_context(left: str) -> str:
    left = (left or "").strip(" ,;()")
    if not left:
        return ""
    left0 = norm_space(left)
    prefixes = sorted({*DISCOURSE_PREFIXES, *GEO_PREFIXES}, key=len, reverse=True)
    if prefixes:
        pref_re = re.compile(r"^(?:" + "|".join(re.escape(x) for x in prefixes) + r")\b", re.I)
        for _ in range(4):
            new_left = pref_re.sub("", left0).strip(" ,;()")
            if new_left == left0:
                break
            left0 = new_left
    for _ in range(3):
        if "," not in left0:
            break
        first, rest = left0.split(",", 1)
        if re.search(r"\b[A-Z][A-Za-z'\-]+\b", first):
            break
        left0 = rest.strip(" ,;()")
    return left0

def _author_tokens_from_left(left: str) -> List[str]:
    s = _strip_leading_context(left)
    if not s:
        return []
    s = s.replace("&", " and ")
    s = re.sub(r"\bet\s+al\.?\b", "", s, flags=re.I)
    s = re.sub(r"(’s|'s)\b", "", s)
    parts = re.split(r"\band\b|;|/|\||&", s, flags=re.I)
    out: List[str] = []
    for p in parts:
        p = p.strip(" ,.;:()[]{}")
        if not p:
            continue
        if "," in p:
            cand = p.split(",", 1)[0].strip()
        else:
            cand = p.split()[-1].strip()
        cand = re.sub(r"[^A-Za-z\-’' ]+", "", cand).strip().replace("’", "'")
        if len(cand) >= 2:
            out.append(cand.lower())
    seen=set(); final=[]
    for x in out:
        if x not in seen:
            seen.add(x); final.append(x)
    return final

def _parse_author_year_from_cite(cite: str) -> Optional[Tuple[List[str], str, str]]:
    s = norm_space(cite)
    if not s:
        return None
    s = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", s, flags=re.I).strip()
    ym = YEAR_RE.search(s)
    if not ym:
        return None
    year = ym.group(1)
    left = s[:ym.start()].strip(" ,;()")
    left = _strip_leading_context(left)
    if _is_likely_narrative_citation(left, year, s):
        return None
    tokens = _author_tokens_from_left(left)
    if not tokens:
        org = strip_punct(left)
        if org and org not in NON_NAME_AUTHOR_KEYS:
            tokens = [org]
        else:
            return None
    if len(tokens) == 1 and tokens[0] in NON_NAME_AUTHOR_KEYS:
        return None
    return tokens, year, left


def _build_alias_map(refs: List[RefAY]) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    alias: Dict[str, str] = {}
    by_year: Dict[str, List[str]] = defaultdict(list)
    for r in refs:
        alias[r.key.lower()] = r.reference_full
        by_year[r.year_base].append(r.reference_full)
        try:
            auth, y = r.key.split("|", 1)
        except Exception:
            continue
        by = _base_year(y)
        if by and by != y:
            alias[f"{auth}|{by}".lower()] = r.reference_full
        ym = YEAR_RE.search(r.reference_full)
        if not ym:
            continue
        year_full = ym.group(1)
        year_base = _base_year(year_full)
        left = (r.reference_full[:ym.start()] or "").strip(" ,;()")
        names = _surnames_from_author_blob(left)
        if names:
            for nm in names[:2]:
                alias[f"{nm}|{year_full}".lower()] = r.reference_full
                if year_base and year_base != year_full:
                    alias[f"{nm}|{year_base}".lower()] = r.reference_full
            if len(names) >= 2:
                a, b = names[0], names[1]
                alias[f"{a}+{b}|{year_full}".lower()] = r.reference_full
                alias[f"{b}+{a}|{year_full}".lower()] = r.reference_full
                if year_base and year_base != year_full:
                    alias[f"{a}+{b}|{year_base}".lower()] = r.reference_full
                    alias[f"{b}+{a}|{year_base}".lower()] = r.reference_full
        org = _first_author_or_org_key(left)
        if org:
            org_norm = strip_punct(org)
            if org_norm and org_norm not in NON_NAME_AUTHOR_KEYS:
                alias[f"{org_norm}|{year_full}".lower()] = r.reference_full
                if year_base and year_base != year_full:
                    alias[f"{org_norm}|{year_base}".lower()] = r.reference_full
            for piece in re.split(r"[\/&]", org):
                p = strip_punct(piece.strip())
                if p and p not in NON_NAME_AUTHOR_KEYS and len(p) >= 2:
                    alias[f"{p}|{year_full}".lower()] = r.reference_full
                    if year_base and year_base != year_full:
                        alias[f"{p}|{year_base}".lower()] = r.reference_full
    return alias, by_year

def _weighted_fuzzy_match(cite_tokens: List[str], left_raw: str, year: str, candidates: List[RefAY]) -> Optional[str]:
    if not candidates:
        return None
    cite_year_base = _base_year(year)
    cite_left_norm = _norm_ref_text(left_raw)
    cite_set = set([t for t in cite_tokens if t])
    best_ref = None
    best_score = 0.0
    for r in candidates:
        if r.year_base != cite_year_base:
            continue
        author_norm = _norm_ref_text(r.author_blob_norm)
        author_tokens = set(author_norm.split())
        cover = len(cite_set & author_tokens)
        cover_bonus = 12.0 * min(2, cover)
        if HAVE_RAPIDFUZZ:
            a_score = float(RFUZZ.token_set_ratio(cite_left_norm, author_norm))  # type: ignore
        else:
            cs = set(cite_left_norm.split()); rs = set(author_norm.split())
            a_score = 100.0 * (len(cs & rs) / max(1, len(cs), len(rs)))
        score = 0.85 * a_score + cover_bonus
        if score > best_score:
            best_score = score
            best_ref = r.reference_full
    if best_ref and best_score >= 88.0:
        return best_ref
    return None

def reconcile_author_year(strict_citations: List[str], loose_citations: List[str], references: List[RefAY]):
    alias_map, _by_year = _build_alias_map(references)
    clusters = _cluster_references(references)
    by_year_refs: Dict[str, List[RefAY]] = defaultdict(list)
    for r in references:
        by_year_refs[r.year_base].append(r)

    def _match_one(cite: str) -> Tuple[str, str]:
        parsed = _parse_author_year_from_cite(cite)
        if not parsed:
            return "", ""
        tokens, year, left_raw = parsed
        year_base = _base_year(year)
        cand_keys: List[str] = []
        for t in tokens[:3]:
            cand_keys.append(f"{t}|{year}".lower())
            if year_base and year_base != year:
                cand_keys.append(f"{t}|{year_base}".lower())
        if len(tokens) >= 2:
            a, b = tokens[0], tokens[1]
            cand_keys.append(f"{a}+{b}|{year}".lower())
            cand_keys.append(f"{b}+{a}|{year}".lower())
            if year_base and year_base != year:
                cand_keys.append(f"{a}+{b}|{year_base}".lower())
                cand_keys.append(f"{b}+{a}|{year_base}".lower())
        for k in cand_keys:
            if k in alias_map:
                return alias_map[k], f"alias:{k}"
        ref = _weighted_fuzzy_match(tokens, left_raw, year, by_year_refs.get(year_base, []))
        if ref:
            return ref, "fuzzy:author"
        return "", ""

    strict_match_counts = Counter()
    strict_missing_counter = Counter()
    strict_c2r: List[Dict[str, Any]] = []
    for c in strict_citations:
        ref, flags = _match_one(c)
        if ref:
            strict_match_counts[ref] += 1
            strict_c2r.append({"status": "matched", "in_text": c, "matched_reference": ref, "flags": f"strict,{flags}" if flags else "strict"})
        else:
            strict_c2r.append({"status": "not_found", "in_text": c, "matched_reference": "", "flags": "strict"})
            strict_missing_counter[c] += 1

    loose_match_counts = Counter(strict_match_counts)
    loose_c2r_extra: List[Dict[str, Any]] = []
    for c in loose_citations:
        ref, flags = _match_one(c)
        if ref:
            loose_match_counts[ref] += 1
            loose_c2r_extra.append({"status": "matched", "in_text": c, "matched_reference": ref, "flags": f"loose,{flags}" if flags else "loose"})
        else:
            loose_c2r_extra.append({"status": "ignored", "in_text": c, "matched_reference": "", "flags": "loose_ignored"})

    c2r = strict_c2r + [x for x in loose_c2r_extra if x["status"] != "ignored"]

    cite_samples_by_ref: Dict[str, List[str]] = defaultdict(list)
    for row in c2r:
        if row["status"] == "matched":
            ref = row["matched_reference"]
            if len(cite_samples_by_ref[ref]) < 6:
                cite_samples_by_ref[ref].append(row["in_text"])

    r2c: List[Dict[str, Any]] = []
    uncited_refs: List[str] = []
    for r in references:
        ref_full = r.reference_full
        times = int(loose_match_counts.get(ref_full, 0))
        meta = clusters.get(ref_full) or {}
        canonical = meta.get("canonical_ref", ref_full)
        is_dup = bool(meta.get("is_duplicate", False))
        cid = int(meta.get("cluster_id", 0))
        canonical_times = int(loose_match_counts.get(canonical, 0))
        uncited = (times == 0 and not (is_dup and canonical_times > 0))
        if uncited:
            uncited_refs.append(ref_full)
        r2c.append({
            "times_cited": times,
            "reference": ref_full,
            "cited_by": cite_samples_by_ref.get(ref_full, []),
            "cluster_id": cid,
            "canonical_reference": canonical,
            "duplicate_of_cited": bool(is_dup and canonical_times > 0),
        })

    missing_rows = [{"citation_in_text": k, "count_in_text": int(v)} for k, v in strict_missing_counter.most_common()]
    strict_unique = int(len(set(strict_citations)))
    loose_unique = int(len(set(strict_citations + loose_citations)))
    return c2r, r2c, missing_rows, uncited_refs, strict_unique, loose_unique


def reconcile_numeric(citations: List[str], references: List[RefNum]):
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
        r2c.append({"times_cited": times, "reference": r.reference_full, "cited_by": [f"[{r.num}]"] if times else []})
    missing_rows = [{"citation_in_text": k, "count_in_text": int(v)} for k, v in missing_counter.most_common()]
    unique_intext_count = int(len(set(citations)))
    return c2r, r2c, missing_rows, uncited_refs, unique_intext_count


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
            ref_block_lines.extend([ln for ln in lines[idx+1:] if ln.strip()])
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
        strict_cites = extract_author_year_citations_strict(main_text)
        loose_cites = extract_author_year_citations_loose(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]
        c2r, r2c, missing_rows, uncited_refs, strict_unique, loose_unique = reconcile_author_year(strict_cites, loose_cites, refs)
        ref_count = len(refs)
        missing_unique = int(len(missing_rows or []))
        match_rate = 0.0
        if strict_unique > 0:
            match_rate = 100.0 * max(0.0, float(strict_unique - missing_unique)) / float(strict_unique)
        return {
            "filename": filename,
            "style": style_s,
            "verify_mode_used": (verify_mode or "all"),
            "reference_detection_message": ref_msg,
            "summary": {
                "strict_intext_count": int(strict_unique),
                "loose_intext_count": int(loose_unique),
                "in_text_citations_found": int(loose_unique),
                "reference_entries_found": int(ref_count),
                "missing_in_references": int(missing_unique),
                "uncited_references": int(len(uncited_refs)),
                "match_rate": float(round(match_rate, 1)),
            },
            "missing_in_references": missing_rows,
            "uncited_references": [str(x) for x in uncited_refs],
            "reconciliation_intext_to_reference": c2r,
            "reconciliation_reference_to_intext": r2c,
            "references_raw": references_raw,
        }

    cites_nums = extract_numeric_citations(main_text, bracketed=True)
    if "vancouver" in style_s and len(cites_nums) < 3:
        cites_nums = extract_numeric_citations(main_text, bracketed=False)
    refs_n = [parse_reference_numeric(r) for r in references_raw]
    refs_n = [r for r in refs_n if r is not None]
    c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(cites_nums, refs_n)
    ref_count = len(refs_n)
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
        "uncited_references": [str(x) for x in uncited_refs],
        "reconciliation_intext_to_reference": c2r,
        "reconciliation_reference_to_intext": r2c,
        "references_raw": references_raw,
    }
