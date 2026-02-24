# engine.py
__version__ = "1.2.8"
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
    re.I
)

DISCOURSE_PREFIXES = {
    "see", "e.g", "eg", "i.e", "ie",
    "as", "in", "for", "from", "to", "at", "on", "by", "with", "within",
    "according", "adapted", "based", "cited", "citing", "reported", "traditional", "classical", "analytical", "for", "from", "in", "on", "at", "by",
    "methods", "method", "approach", "approaches", "sample", "size", "power",
    "results", "discussion", "model", "framework", "similarly", "however", "nonetheless", "nevertheless", "therefore",
    "thus", "hence", "moreover", "furthermore", "additionally", "also",
    "conversely", "instead", "meanwhile", "specifically", "notably",
    "indeed", "importantly", "overall", "increasingly", "generally",
    "consequently", "accordingly", "alternatively", "likewise",
}


# Headings that often appear *after* the reference list in theses/articles.
# Used to avoid swallowing appendices/supplementary material as references.
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
    s = re.sub(r"[^a-z0-9\s\-&]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _first_author_or_org_key(author_left: str) -> str:
    """Return a stable key using the *first* author surname or an acronym in brackets.
    Examples:
      - "Bartlett, J. E., Kotrlik, J. W., & Higgins, C. C." -> "bartlett"
      - "United Nations Conference on Trade and Development (UNCTAD)" -> "unctad"
      - "Adam's" -> "adam"
    """
    s = norm_space(author_left)

    # Prefer acronym in brackets, e.g. "(UNCTAD)"
    m = re.search(r"\(([A-Z]{2,10})\)", s)
    if m:
        return strip_punct(m.group(1))

    # Remove numbering
    s = re.sub(r"^\[\s*\d{1,4}\s*\]\s*", "", s).strip()
    s = re.sub(r"^\d{1,4}[.)]\s*", "", s).strip()

    # Remove year if it leaked in
    s = re.sub(r"\(\s*(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?\s*\).*", "", s).strip()

    # Remove possessive on author token
    s = re.sub(r"(’s|'s)\b", "", s)

    # Split on separators, keep the first author part
    s0 = re.split(r"\s+(?:&|and)\s+|,", s, maxsplit=1)[0].strip()

    # Handle "et al."
    s0 = re.sub(r"\bet\s+al\.?\b", "", s0, flags=re.I).strip()

    toks = [t for t in re.split(r"\s+", s0) if t]
    if not toks:
        return ""
    return strip_punct(toks[-1])


def _looks_like_toc_references_line(s: str, tail: str) -> bool:
    """Detect TOC/header lines like:
      - 'REFERENCES 60'
      - 'References ....... 234'
    """
    if not s:
        return False
    tail = (tail or "").strip()
    if tail and re.fullmatch(r"\d{1,4}", tail):
        return True
    if re.search(r"\.{2,}\s*\d{1,4}\s*$", s):
        return True
    return False


def _looks_like_heading_line(s: str) -> bool:
    """Heuristic: short heading-like line (often appendix/supplementary headings)."""
    s0 = (s or "").strip()
    if not s0:
        return False
    if len(s0) > 120:
        return False
    # Avoid lines that look like normal sentences.
    if s0.endswith(".") and len(s0) > 25:
        return False
    # Many headings are ALL CAPS or Title Case
    letters = re.sub(r"[^A-Za-z]", "", s0)
    if letters and letters.isupper() and len(letters) >= 6:
        return True
    # Title case-ish (not perfect, but helpful)
    if re.match(r"^[A-Z][A-Za-z0-9\s\-,:]{3,}$", s0):
        return True
    return False


def _truncate_reference_block(lines: List[str], style_hint: str) -> List[str]:
    """Stop the reference block when it clearly transitions to appendices/supplementary sections."""
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

        # count reference-like starts early so we only allow end detection after we truly are in refs
        if _is_ref_like(s):
            ref_like_seen += 1

        # End heading detection (only after some refs already found)
        if ref_like_seen >= 3 and (REF_END_HEADING_RE.search(s) or (_looks_like_heading_line(s) and re.search(r"\b(appendix|appendices|annex|supplement|supporting|additional)\b", s, re.I))):
            # Lookahead: if upcoming lines don't look like references, stop here
            look = [x for x in lines[i:i+25] if (x or "").strip()]
            look_ref = sum(1 for x in look if _is_ref_like((x or "").strip()))
            if look_ref <= 1:
                break

        out.append(ln)

    return out

# -----------------------------
# DOCX extraction (robust)
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
    """Extract DOCX text from XML parts to catch:
    - textboxes/shapes
    - headers/footers
    - footnotes/endnotes
    """
    import zipfile
    import xml.etree.ElementTree as ET

    NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

    def _extract_from_xml(xml_bytes: bytes) -> List[str]:
        out = []
        try:
            root = ET.fromstring(xml_bytes)
        except Exception:
            return out
        for p in root.findall(".//w:p", NS):
            parts = []
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

    lines: List[str] = []
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
            # strict heading
            for pat in REF_HEADINGS:
                if re.search(pat, t, flags=re.I):
                    in_refs = True
                    heading_line = t
                    break

            # relaxed heading with TOC guard
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
        # also guard numeric-style theses
        ref_lines = _truncate_reference_block(ref_lines, style_hint="numeric")

    msg = f"Found References heading: {heading_line}" if in_refs else "No References heading found."
    return "\n".join(main_lines).strip(), ref_lines, msg


# -----------------------------
# PDF extraction + heading detection
# -----------------------------
def read_pdf_text(file_bytes: bytes) -> str:
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")

    out = []
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

    m = re.match(r"^(\d{1,4})([.)])\s+(.+)$", s0)
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
        a = re.sub(r"[^A-Za-z,\.\-\s&]", "", a).strip()
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
        lookahead = [ln for ln in lines[i + 1:i + 31] if (ln or "").strip()]
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
    """Split when multiple numeric references got glued together in PDFs,
    e.g. '[36] ... [37] ... [38] ...'
    """
    out: List[str] = []
    br_pat = re.compile(r"(?=(\[\s*\d{1,4}\s*\]\s+))")
    dot_pat = re.compile(r"(?=(\b\d{1,4}[\.\)]\s+))")

    for s in merged:
        s = (s or "").strip()
        if not s:
            continue

        cuts = []

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

    # Parenthetical: (Author, 2020; Author2, 2021)
    paren_pat = re.compile(
        r"\(([^()]{0,260}?\b(?:19|20)\d{2}[a-z]?\b[^()]{0,260}?)\)"
    )

    # Narrative: Bartlett, Kotrlik, and Higgins (2001); Adam's (2020); Bartlett & Higgins (2001); Bartlett et al. (2001)
    S = r"[A-Z][A-Za-z'\-]+(?:'s)?"
    narr_pat = re.compile(
        rf"\b("
        rf"(?:{S}(?:\s*,\s*{S}){{0,4}}(?:\s*,?\s*(?:&|and)\s*{S})?)"
        rf"|(?:{S}\s+(?:&|and)\s+{S})"
        rf"|(?:{S}\s+et\s+al\.)"
        rf")\s*\(\s*((?:19|20)\d{{2}}[a-z]?)\s*\)"
    )

    out: List[str] = []

    # parenthetical chunks
    for m in paren_pat.finditer(t):
        inside = m.group(1)
        chunks = [c.strip() for c in inside.split(";") if c.strip()]
        for ch in chunks:
            ch2 = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", ch, flags=re.I).strip()
            if YEAR_RE.search(ch2):
                out.append(norm_space(ch2))

    # narrative
    for m in narr_pat.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        # remove possessive: Adam's (2020) -> Adam (2020)
        author = re.sub(r"(’s|'s)\b", "", author).strip()
        out.append(norm_space(f"{author}, {year}"))

    return [c for c in out if c]

def extract_numeric_citations(text: str, bracketed: bool = True) -> List[str]:
    t = text or ""
    out: List[str] = []
    if bracketed:
        pat = re.compile(r"\[\s*(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?\s*\]")
    else:
        # captures standalone numbers and ranges (used as fallback for Vancouver)
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

    m = re.search(r"\(\s*(" + YEAR + r")\s*\)", s)
    if not m:
        m2 = re.search(r"\b(" + YEAR + r")\b", s)
        if not m2:
            return None
        year = m2.group(1)
        left = s[:m2.start()].strip()
    else:
        year = m.group(1)
        left = s[:m.start()].strip()

    author_key = _first_author_or_org_key(left)
    if not author_key:
        return None

    key = f"{author_key}|{year}".lower()
    return RefAY(reference_full=s, key=key)

def parse_reference_numeric(ref: str) -> Optional[RefNum]:
    s = norm_space(ref)
    if not s:
        return None

    m = re.match(r"^\[\s*(\d{1,4})\s*\]\s*(.+)$", s)
    if m:
        return RefNum(reference_full=s, num=m.group(1))

    m2 = re.match(r"^(\d{1,4})[.)]\s*(.+)$", s)
    if m2 and not YEAR_RE.fullmatch(m2.group(1)):
        return RefNum(reference_full=s, num=m2.group(1))

    return None


# -----------------------------
# Reconciliation
# -----------------------------
def _norm_author_block(s: str) -> str:
    s = strip_punct(s)
    s = s.replace("&", " and ")
    s = re.sub(r"\bet al\b", "", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_author_year_from_cite(cite: str) -> Optional[Tuple[str, str]]:
    s = norm_space(cite)
    if not s:
        return None

    s = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", s, flags=re.I).strip()
    ym = YEAR_RE.search(s)
    if not ym:
        return None
    year = ym.group(1)

    left = s[:ym.start()].strip(" ,;()")
    left = re.sub(
        r"^(?:"
        + "|".join(sorted([re.escape(x) for x in DISCOURSE_PREFIXES], key=len, reverse=True))
        + r")\b",
        "",
        left,
        flags=re.I
    ).strip(" ,;()")

    # remove possessive: Adam's, 2020 -> Adam, 2020
    left = re.sub(r"(’s|'s)\b", "", left).strip()

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

    cite_key_counts = Counter()
    cite_text_counts = Counter()
    parsed_cites: List[Tuple[str, str]] = []

    for c in citations:
        parsed = _parse_author_year_from_cite(c)
        if not parsed:
            continue
        auth, year = parsed
        key = f"{auth}|{year}".lower()
        cite_key_counts[key] += 1
        cite_text_counts[c] += 1
        parsed_cites.append((key, c))

    c2r: List[Dict[str, Any]] = []
    missing_counter = Counter()

    for key, c in parsed_cites:
        if key in ref_map:
            c2r.append({"status": "matched", "in_text": c, "matched_reference": ref_map[key], "flags": ""})
        else:
            c2r.append({"status": "not_found", "in_text": c, "matched_reference": "", "flags": ""})
            missing_counter[c] += 1

    # r2c rows + uncited list
    r2c: List[Dict[str, Any]] = []
    uncited_refs: List[str] = []

    # build sample cited_by (up to 6)
    cite_samples_by_key: Dict[str, List[str]] = defaultdict(list)
    for key, c in parsed_cites:
        lst = cite_samples_by_key[key]
        if len(lst) < 6:
            lst.append(c)

    for r in references:
        times = int(cite_key_counts.get(r.key, 0))
        if times == 0:
            uncited_refs.append(r.reference_full)
        r2c.append({
            "times_cited": times,
            "reference": r.reference_full,
            "cited_by": cite_samples_by_key.get(r.key, []),
        })

    missing_rows = [
        {"citation_in_text": k, "count_in_text": int(v)}
        for k, v in missing_counter.most_common()
    ]

    # In-text summary should be UNIQUE citations (unique keys), not total occurrences
    unique_intext_count = int(len(cite_key_counts))
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

    # r2c rows + uncited list
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

    missing_rows = [
        {"citation_in_text": k, "count_in_text": int(v)}
        for k, v in missing_counter.most_common()
    ]

    unique_intext_count = int(len(set(citations)))
    return c2r, r2c, missing_rows, uncited_refs, unique_intext_count


# -----------------------------
# Public API: run_crosscheck
# -----------------------------
def run_crosscheck(
    file_bytes: bytes,
    filename: str,
    style: str = "apa",
    verify_online: bool = False,   # main.py always calls offline here (kept for compatibility)
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

    # ---- read + split ----
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
            ref_block_lines = []
            if tail:
                ref_block_lines.append(tail)
            ref_block_lines.extend([ln for ln in lines[idx + 1:] if ln.strip()])
            ref_block_lines = _truncate_reference_block(ref_block_lines, style_hint=style_hint)
            references_raw = _merge_reference_lines(ref_block_lines)
            if style_hint == "numeric":
                references_raw = _split_embedded_numeric_refs(references_raw)

    else:
        return {"error": "Upload a DOCX or PDF"}

    # ---- speed cap: keep head + tail ----
    if len(main_text) > 350_000:
        half = 175_000
        main_text = main_text[:half] + "\n... [TRUNCATED] ...\n" + main_text[-half:]

    # ---- extract + reconcile ----
    if style_hint == "apa":
        cites = extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]

        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_author_year(cites, refs)
        ref_count = len(refs)

    else:
        # IEEE is bracketed. Vancouver can vary, so fallback if bracketed yields too few.
        if "ieee" in style_s:
            bracketed = True
        else:
            bracketed = True  # try bracketed first for Vancouver too

        cites_nums = extract_numeric_citations(main_text, bracketed=bracketed)

        if "vancouver" in style_s and len(cites_nums) < 3:
            cites_nums = extract_numeric_citations(main_text, bracketed=False)

        refs = [parse_reference_numeric(r) for r in references_raw]
        refs = [r for r in refs if r is not None]

        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(cites_nums, refs)
        ref_count = len(refs)

    # ---- summary match rate based on citation occurrences ----
    missing_unique = int(len(missing_rows or []))
    match_rate = 0.0
    if intext_count > 0:
        match_rate = 100.0 * max(0.0, float(intext_count - missing_unique)) / float(intext_count)

    # ---- return schema that app.js expects ----
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

        # Missing tab expects list of dicts: {citation_in_text, count_in_text}
        "missing_in_references": missing_rows,

        # Uncited tab expects list[str]
        "uncited_references": uncited_refs,

        # Mapping tabs
        "reconciliation_intext_to_reference": c2r,
        "reconciliation_reference_to_intext": r2c,

        # useful debugging / future features
        "references_raw": references_raw,
    }

