# engine.py
__version__ = "1.4.0"

import re
import io
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter

ENGINE_BUILD = "commercial-2026-02-26-optimized"

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


def extract_numeric_citations(text: str, style: str = "ieee") -> List[str]:
    """Extract numeric in-text citations.

    - IEEE: only square brackets, e.g. [1], [1,4,6], [1-3]
    - Vancouver: square brackets or parentheses, e.g. [1], (1), [1-3], (1, 2, 3)

    Returns a list of citation numbers as strings, expanded for ranges, with duplicates preserved.
    """
    t = text or ""
    style_s = (style or "ieee").strip().lower()
    is_ieee = ("ieee" in style_s)

    # Reduce common false positives early
    t = re.sub(r"\b(?:table|figure|fig\.?|equation|eq\.?|appendix|section|chap(?:ter)?|page|pp\.)\s+\d{1,4}\b", " ", t, flags=re.I)
    t = re.sub(r"\]\s*\n\s*\[", "][", t)

    out: List[str] = []

    def _expand_group(group: str) -> List[str]:
        g = (group or "").strip()
        if not g:
            return []
        nums: List[str] = []
        parts = [p for p in re.split(r"[\s,]+", g) if p]
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if "-" in part or "–" in part:
                a, b = re.split(r"[-–]", part, maxsplit=1)
                a = a.strip()
                b = b.strip()
                if a.isdigit() and b.isdigit():
                    lo = int(a)
                    hi = int(b)
                    if lo <= hi and (hi - lo) <= 100:
                        nums.extend([str(i) for i in range(lo, hi + 1)])
                    elif hi < lo and (lo - hi) <= 100:
                        nums.extend([str(i) for i in range(hi, lo + 1)])
                    else:
                        nums.extend([a, b])
                else:
                    digs = re.findall(r"\d{1,4}", part)
                    nums.extend(digs)
            else:
                if part.isdigit():
                    nums.append(part)
                else:
                    nums.extend(re.findall(r"\d{1,4}", part))
        return nums

    bracket_group_re = re.compile(r"\[\s*(\d{1,4}(?:\s*[-–]\s*\d{1,4})?(?:\s*(?:,|\s)\s*\d{1,4}(?:\s*[-–]\s*\d{1,4})?)*)\s*\]")
    for m in bracket_group_re.finditer(t):
        out.extend(_expand_group(m.group(1)))

    if not is_ieee:
        paren_group_re = re.compile(r"\(\s*(\d{1,4}(?:\s*[-–]\s*\d{1,4})?(?:\s*(?:,|\s)\s*\d{1,4}(?:\s*[-–]\s*\d{1,4})?)*)\s*\)")
        for m in paren_group_re.finditer(t):
            out.extend(_expand_group(m.group(1)))

    cleaned: List[str] = []
    for n in out:
        if not n:
            continue
        try:
            ni = int(n)
        except Exception:
            continue
        if 1900 <= ni <= 2099:
            continue
        cleaned.append(str(ni))

    return cleaned


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


def parse_reference_numeric(ref: str, style: str = "ieee") -> Optional[RefNum]:
    """Parse numeric references.

    IEEE expects: [1] ...
    Vancouver allows: [1] ..., (1) ..., 1. ..., 1) ..., 1 ...
    """
    s = norm_space(ref)
    if not s:
        return None

    style_s = (style or "ieee").strip().lower()
    is_ieee = ("ieee" in style_s)

    if is_ieee:
        m = re.match(r"^\[\s*(\d{1,4})\s*\]\s*(.+)$", s)
        if not m:
            return None
        num = m.group(1)
        body = norm_space(m.group(2))
        if len(body) < 10:
            return None
        return RefNum(reference_full=s, num=num)

    # Vancouver (flexible)
    m = re.match(r"^\[\s*(\d{1,4})\s*\]\s*(.+)$", s)
    if m:
        num = m.group(1)
        body = norm_space(m.group(2))
        if len(body) < 10:
            return None
        return RefNum(reference_full=s, num=num)

    m = re.match(r"^\(\s*(\d{1,4})\s*\)\s*(.+)$", s)
    if m:
        num = m.group(1)
        body = norm_space(m.group(2))
        if len(body) < 10:
            return None
        return RefNum(reference_full=s, num=num)

    m = re.match(r"^(\d{1,4})\s*[\.)\]]\s*(.+)$", s)
    if m:
        num = m.group(1)
        body = norm_space(m.group(2))
        try:
            ni = int(num)
            if 1900 <= ni <= 2099:
                return None
        except Exception:
            pass
        if len(body) < 10:
            return None
        return RefNum(reference_full=s, num=num)

    m = re.match(r"^(\d{1,4})\s+(.+)$", s)
    if m:
        num = m.group(1)
        body = norm_space(m.group(2))
        try:
            ni = int(num)
            if 1900 <= ni <= 2099:
                return None
        except Exception:
            pass
        if len(body) < 15:
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
        used = ""
        for k in cand_keys:
            if k in alias_map:
                matched_ref = alias_map[k]
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

    for r in references:
        ref_full = r.reference_full
        times = int(cite_counts_by_ref.get(ref_full, 0))
        if times == 0:
            uncited_refs.append(ref_full)

        r2c.append({
            "times_cited": times,
            "reference": ref_full,
            "cited_by": cite_samples_by_ref.get(ref_full, []),
        })

    missing_rows = [{"citation_in_text": k, "count_in_text": int(v)} for k, v in missing_counter.most_common()]
    unique_intext_count = int(len(set([c for c in citations if c])))
    return c2r, r2c, missing_rows, uncited_refs, unique_intext_count


def reconcile_numeric(citations: List[str], references: List[RefNum]) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    ref_map = {r.num: r.reference_full for r in references}

    cite_counts = Counter(citations)

    c2r = []
    missing_counter = Counter()
    for c in citations:
        if c in ref_map:
            c2r.append({"status": "matched", "in_text": f"[{c}]", "matched_reference": ref_map[c], "flags": ""})
        else:
            c2r.append({"status": "not_found", "in_text": f"[{c}]", "matched_reference": "", "flags": ""})
            missing_counter[c] += 1

    r2c = []
    uncited = []
    for r in references:
        times = int(cite_counts.get(r.num, 0))
        if times == 0:
            uncited.append(r.reference_full)
        r2c.append({"times_cited": times, "reference": r.reference_full, "cited_by": [f"[{r.num}]"] if times else []})

    missing_rows = [{"citation_in_text": f"[{k}]", "count_in_text": int(v)} for k, v in missing_counter.most_common()]
    unique_intext_count = int(len(set([c for c in citations if c])))
    return c2r, r2c, missing_rows, uncited, unique_intext_count


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

    if style_hint == "apa":
        cites = extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]

        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_author_year(cites, refs)
        ref_count = len(refs)

    else:
        # Numeric styles
        if "ieee" in style_s:
            cite_style = "ieee"
        else:
            # treat vancouver / numeric as flexible vancouver-style extraction
            cite_style = "vancouver"

        cites_nums = extract_numeric_citations(main_text, style=cite_style)

        refs = [parse_reference_numeric(r, style=cite_style) for r in references_raw]
        refs = [r for r in refs if r is not None]

        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(cites_nums, refs)
        ref_count = len(refs)

    missing_unique = int(len(missing_rows or []))
    match_rate = 0.0
    if intext_count > 0:
        match_rate = 100.0 * max(0.0, float(intext_count - missing_unique)) / float(intext_count)

    # strict vs loose counts (kept stable)
    strict_intext_count = int(intext_count)
    loose_intext_count = int(intext_count)

    return {
        "filename": filename,
        "style": style_s,
        "engine_build": ENGINE_BUILD,
        "verify_mode_used": (verify_mode or "all"),
        "reference_detection_message": ref_msg,
        "summary": {
            "in_text_citations_found": int(intext_count),
            "reference_entries_found": int(ref_count),
            "missing_in_references": int(missing_unique),
            "uncited_references": int(len(uncited_refs)),
            "match_rate": float(round(match_rate, 1)),
            "strict_intext_count": strict_intext_count,
            "loose_intext_count": loose_intext_count,
        },
        "strict_intext_count": strict_intext_count,
        "loose_intext_count": loose_intext_count,
        "missing_in_references": missing_rows,
        "uncited_references": uncited_refs,
        "reconciliation_intext_to_reference": c2r,
        "reconciliation_reference_to_intext": r2c,
        "references_raw": references_raw,
    }
