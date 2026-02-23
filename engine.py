# engine.py
__version__ = "1.2.4"

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

# Online verification is run by main.py in background batches.
# engine.py keeps import optional for local/offline runs.
try:
    from verify import verify_references_batch  # noqa
    VERIFY_OK = True
except Exception:
    VERIFY_OK = False


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
    "according", "adapted", "based", "cited", "citing", "reported",
}


# -----------------------------
# Helpers
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


# -----------------------------
# DOCX text extraction
# -----------------------------
def _iter_docx_text(doc: "Document"):
    """Yield text from paragraphs and table cells."""
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

    targets = [
        "word/document.xml",
        "word/footnotes.xml",
        "word/endnotes.xml",
    ]

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
    """Split DOCX into main_text and reference lines.
    Fixes:
    - Avoid TOC trap lines ('REFERENCES 60')
    - Includes tables
    - Includes headers/footers/footnotes/textboxes via XML
    """
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

            # relaxed heading
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

    msg = f"Found References heading: {heading_line}" if in_refs else "No References heading found."
    return "\n".join(main_lines).strip(), ref_lines, msg


# -----------------------------
# PDF reading
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
            text = text.replace("\x00", " ")
            out.append(text)
    return "\n".join(out)


# -----------------------------
# Reference start detectors
# -----------------------------
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
        # avoid treating "2019." as a reference number
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
        if len(a) >= 3:
            return True
    return False


def _count_reference_like(lines: List[str], style_hint: str = "auto") -> int:
    c = 0
    for ln in lines:
        s = (ln or "").strip()
        if not s:
            continue
        if style_hint == "numeric":
            if _looks_like_new_numeric_reference_start(s):
                c += 1
        elif style_hint == "apa":
            if _looks_like_new_apa_reference_start(s):
                c += 1
        else:
            if _looks_like_new_numeric_reference_start(s) or _looks_like_new_apa_reference_start(s):
                c += 1
    return c


def _find_reference_heading(lines: List[str], style_hint: str = "auto") -> Tuple[int, str]:
    """Return (index, tail_after_heading). Adds:
    - TOC/header guard
    - validation: next ~30 lines must look like references
    """
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
    merged = [m for m in merged if m and len(m) >= 8]
    return merged


def _split_embedded_numeric_refs(merged: List[str]) -> List[str]:
    """Split when multiple numeric references were glued into one string,
    e.g. '[36] ... [37] ... [38] ...' which makes [37] look 'missing'.
    """
    out: List[str] = []
    br_pat = re.compile(r"(?=(\[\s*\d{1,4}\s*\]\s+))")
    dot_pat = re.compile(r"(?=(\b\d{1,4}[\.\)]\s+))")

    for s in merged:
        if not s:
            continue
        s = s.strip()
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

    paren_pat = re.compile(
        r"\(([^()]{0,220}?\b(?:19|20)\d{2}[a-z]?\b[^()]{0,220}?)\)"
    )
    narr_pat = re.compile(
        r"\b([A-Z][A-Za-z'\-]+(?:\s+(?:&|and)\s+[A-Z][A-Za-z'\-]+)?|[A-Z][A-Za-z'\-]+\s+et\s+al\.)\s*\(\s*((?:19|20)\d{2}[a-z]?)\s*\)"
    )

    out: List[str] = []

    for m in paren_pat.finditer(t):
        inside = m.group(1)
        chunks = [c.strip() for c in inside.split(";") if c.strip()]
        for ch in chunks:
            ch2 = re.sub(r"\bp\.?\s*\d+\b", "", ch, flags=re.I).strip()
            ch2 = re.sub(r"\bpp\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", ch2, flags=re.I).strip()
            if YEAR_RE.search(ch2):
                out.append(norm_space(ch2))

    for m in narr_pat.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        out.append(norm_space(f"{author}, {year}"))

    return [c for c in out if c]


def extract_numeric_citations(text: str, bracketed: bool = True) -> List[str]:
    t = (text or "")
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


def _guess_title_from_reference(ref: str) -> str:
    s = norm_space(ref)
    s = re.sub(r"https?://\S+", "", s).strip()
    s = re.sub(r"\bdoi\s*:\s*\S+", "", s, flags=re.I).strip()
    parts = [p.strip() for p in re.split(r"\.\s+", s) if p.strip()]
    title = parts[1] if len(parts) >= 2 else (parts[0] if parts else s)
    return strip_punct(title)[:220]


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

    left2 = re.sub(r"^\[\s*\d{1,4}\s*\]\s*", "", left).strip()
    left2 = re.sub(r"^\d{1,4}[.)]\s*", "", left2).strip()

    authors = strip_punct(left2)[:160]
    auth_key = authors.replace(" et al", "").replace(" and ", "&")
    auth_key = re.sub(r"\s+", " ", auth_key).strip()
    key = f"{auth_key}|{year}".lower()
    return RefAY(reference_full=s, key=key)


def parse_reference_numeric(ref: str) -> Optional[RefNum]:
    s = norm_space(ref)
    if not s:
        return None

    m = re.match(r"^\[\s*(\d{1,4})\s*\]\s*(.+)$", s)
    if m:
        num = m.group(1)
        return RefNum(reference_full=s, num=num)

    m2 = re.match(r"^(\d{1,4})[.)]\s*(.+)$", s)
    if m2 and not YEAR_RE.fullmatch(m2.group(1)):
        num = m2.group(1)
        return RefNum(reference_full=s, num=num)

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
    # drop discourse prefixes
    left = re.sub(
        r"^(?:"
        + "|".join(sorted([re.escape(x) for x in DISCOURSE_PREFIXES], key=len, reverse=True))
        + r")\b",
        "",
        left,
        flags=re.I
    ).strip(" ,;()")

    left = left.replace(" et al.", " et al")
    auth = _norm_author_block(left)
    if not auth:
        return None
    return auth, year


def reconcile_author_year(citations: List[str], references: List[RefAY]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str], List[str]]:
    ref_map: Dict[str, str] = {}
    for r in references:
        ref_map[r.key] = r.reference_full

    cite_key_counts = Counter()
    parsed_cites: List[Tuple[str, str]] = []
    for c in citations:
        parsed = _parse_author_year_from_cite(c)
        if not parsed:
            continue
        auth, year = parsed
        key = f"{auth}|{year}".lower()
        cite_key_counts[key] += 1
        parsed_cites.append((key, c))

    c2r: List[Dict[str, Any]] = []
    missing_unique = set()
    for key, c in parsed_cites:
        if key in ref_map:
            c2r.append({"status": "matched", "in_text": c, "matched_reference": ref_map[key], "flags": ""})
        else:
            c2r.append({"status": "not_found", "in_text": c, "matched_reference": "", "flags": ""})
            missing_unique.add(c)

    r2c: List[Dict[str, Any]] = []
    uncited_refs: List[str] = []
    for r in references:
        times = cite_key_counts.get(r.key, 0)
        if times == 0:
            uncited_refs.append(r.reference_full)
        r2c.append({
            "times_cited": int(times),
            "reference": r.reference_full,
            "cited_by_sample": []
        })

    # missing citations list should be the "Citation in Text" strings
    missing_list = sorted(missing_unique)
    return c2r, r2c, missing_list, uncited_refs


def reconcile_numeric(citations: List[str], references: List[RefNum]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str], List[str]]:
    ref_by_num: Dict[str, str] = {}
    for r in references:
        ref_by_num[r.num] = r.reference_full

    cite_counts = Counter(citations)

    c2r: List[Dict[str, Any]] = []
    missing_nums = set()
    for num in citations:
        if num in ref_by_num:
            c2r.append({"status": "matched", "in_text": f"[{num}]", "matched_reference": ref_by_num[num], "flags": ""})
        else:
            c2r.append({"status": "not_found", "in_text": f"[{num}]", "matched_reference": "", "flags": ""})
            missing_nums.add(f"[{num}]")

    r2c: List[Dict[str, Any]] = []
    uncited_refs: List[str] = []
    for r in references:
        times = cite_counts.get(r.num, 0)
        if times == 0:
            uncited_refs.append(r.reference_full)
        r2c.append({
            "times_cited": int(times),
            "reference": r.reference_full,
            "cited_by_sample": [f"[{r.num}]"] if times else []
        })

    missing_list = sorted(missing_nums, key=lambda x: int(re.sub(r"\D", "", x) or "0"))
    return c2r, r2c, missing_list, uncited_refs


# -----------------------------
# Main runner
# -----------------------------
def run_crosscheck(
    file_bytes: bytes,
    filename: str,
    style: str = "apa",
    verify_online: bool = False,   # kept for compatibility, main.py uses offline here
    verify_mode: str = "all",
    max_verify: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
) -> Dict[str, Any]:

    name = (filename or "").lower().strip()

    # style hint for reference split and numeric glue splitting
    style_s = (style or "apa").strip().lower()
    if "ieee" in style_s or "vancouver" in style_s or "numeric" in style_s:
        style_hint = "numeric"
    else:
        style_hint = "apa"

    # --------------------------------
    # Read + split
    # --------------------------------
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
            references_raw = _merge_reference_lines(ref_block_lines)
            if style_hint == "numeric":
                references_raw = _split_embedded_numeric_refs(references_raw)

    else:
        return {"error": "Upload a DOCX or PDF"}

    # --------------------------------
    # Speed cap, keep head + tail (not only head)
    # --------------------------------
    if len(main_text) > 350_000:
        half = 175_000
        main_text = main_text[:half] + "\n... [TRUNCATED] ...\n" + main_text[-half:]

    # --------------------------------
    # Extract + reconcile
    # --------------------------------
    if style_hint == "apa":
        cites = extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]

        c2r, r2c, missing_list, uncited_refs = reconcile_author_year(cites, refs)

        intext_count = len([c for c in cites if _parse_author_year_from_cite(c)])
        ref_count = len(refs)

    else:
        # IEEE uses bracketed citations by default
        bracketed = True
        cites_nums = extract_numeric_citations(main_text, bracketed=bracketed)
        refs = [parse_reference_numeric(r) for r in references_raw]
        refs = [r for r in refs if r is not None]

        c2r, r2c, missing_list, uncited_refs = reconcile_numeric(cites_nums, refs)

        intext_count = len(cites_nums)
        ref_count = len(refs)

    # compute summary rates
    missing_rows = [x for x in c2r if x.get("status") != "matched"]
    uncited_rows = [x for x in r2c if int(x.get("times_cited") or 0) == 0]
    match_rate = 0.0
    if c2r:
        match_rate = 100.0 * (len(c2r) - len(missing_rows)) / len(c2r)

    # --------------------------------
    # Return keys matching main.py
    # --------------------------------
    return {
        "filename": filename,

        "summary": {
            "in_text": int(intext_count),
            "references": int(ref_count),
            "missing": int(len(missing_list)),
            "uncited": int(len(uncited_refs)),
            "match_rate": float(round(match_rate, 1)),
        },

        "reference_detection_message": ref_msg,

        # Main reconciliation outputs (what UI + CSV expects)
        "reconciliation_intext_to_reference": c2r,
        "reconciliation_reference_to_intext": r2c,

        # Lists used by main.py selection logic
        "missing_citations": missing_list,          # list[str], e.g. ["[37]", "[10]", ...] or "(Smith, 2020)"
        "uncited_references": uncited_refs,         # list[str] of full reference strings

        # Keep raw refs for optional debugging or future features
        "references_raw": references_raw,
    }
