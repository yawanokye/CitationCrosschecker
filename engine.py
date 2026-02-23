# engine.py
__version__ = "1.2.3"

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

try:
    from verify import verify_references_batch
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

DISCOURSE_PREFIXES = {
    "see", "e.g", "eg", "i.e", "ie",
    "as", "in", "for", "from", "to", "at", "on", "by", "with", "within",
    "according", "adapted", "based", "cited", "citing", "reported",
}

REF_HEADING_RELAXED = re.compile(
    r"^\s*(references?|bibliography|works\s+cited|literature\s+cited)\b",
    re.I
)


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
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def looks_like_year_token(tok: str) -> bool:
    tok = (tok or "").strip()
    return bool(YEAR_RE.fullmatch(tok))


def _looks_like_toc_references_line(s: str, tail: str) -> bool:
    """Detect TOC-style lines like 'REFERENCES 60' or 'References ....... 234'."""
    if not s:
        return False
    if tail and re.fullmatch(r"\d{1,4}", tail.strip()):
        return True
    if re.search(r"\.{2,}\s*\d{1,4}\s*$", s):
        return True
    return False


# -----------------------------
# DOCX reading and splitting
# -----------------------------
def _iter_docx_text(doc: "Document"):
    """Yield text from paragraphs and table cells (python-docx)."""
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
    """Extract DOCX text from XML parts (covers textboxes, headers, footnotes, endnotes).
    Returns a list of lines.
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
    - Avoid TOC traps like 'REFERENCES 60'
    - Read table cell text
    - Read footnotes/endnotes/headers/textboxes via XML extraction (best-effort)
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
# Reference heading finder (PDF)
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
    """Return (index, tail_after_heading).

    Fixes:
    - Avoid false hits on page headers/TOC lines like 'REFERENCES 234' or 'References .... 234'
    - Validate candidates by checking that the following lines look like references
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
# Reference line merging (PDF and DOCX)
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
    """Split merged numeric reference strings when multiple [n] entries got glued together (common in 2-column PDFs)."""
    out: List[str] = []
    br_pat = re.compile(r"(?=(\[\s*\d{1,4}\s*\]\s+))")
    dot_pat = re.compile(r"(?=(\b\d{1,4}[\.\)]\s+))")

    for s in merged:
        if not s:
            continue
        s = s.strip()
        splits = []

        for m in br_pat.finditer(s):
            pos = m.start(1)
            if pos > 0:
                splits.append(pos)

        for m in dot_pat.finditer(s):
            pos = m.start(1)
            if pos > 0:
                token = m.group(1).strip()
                num = re.match(r"^(\d{1,4})", token)
                if num and YEAR_RE.fullmatch(num.group(1)):
                    continue
                splits.append(pos)

        if not splits:
            out.append(s)
            continue

        splits = sorted(set(splits))
        parts = []
        prev = 0
        for pos in splits:
            part = s[prev:pos].strip()
            if part:
                parts.append(part)
            prev = pos
        tail = s[prev:].strip()
        if tail:
            parts.append(tail)

        for p in parts:
            if len(p) >= 10:
                out.append(p)

    return out


# -----------------------------
# Citation extractors
# -----------------------------
def extract_author_year_citations(text: str) -> List[str]:
    t = (text or "")
    t = t.replace("\u2019", "'")

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
                out.append(ch2)

    for m in narr_pat.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        out.append(f"{author}, {year}")

    cleaned: List[str] = []
    for c in out:
        c0 = norm_space(c)
        if not c0:
            continue
        cleaned.append(c0)
    return cleaned


def extract_numeric_citations(text: str, bracketed: bool = False) -> List[str]:
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
    year: str
    authors: str
    title_guess: str


@dataclass
class RefNum:
    reference_full: str
    num: str
    title_guess: str
    authors: str
    year: str


def _guess_title_from_reference(ref: str) -> str:
    s = norm_space(ref)
    s = re.sub(r"https?://\S+", "", s).strip()
    s = re.sub(r"\bdoi\s*:\s*\S+", "", s, flags=re.I).strip()
    parts = [p.strip() for p in re.split(r"\.\s+", s) if p.strip()]
    if len(parts) >= 2:
        title = parts[1]
    elif parts:
        title = parts[0]
    else:
        title = s
    title = re.sub(r"^\(\d{4}[a-z]?\)\s*", "", title)
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

    auth_key = authors
    auth_key = auth_key.replace(" et al", "")
    auth_key = auth_key.replace(" and ", "&")
    auth_key = re.sub(r"\s+", " ", auth_key).strip()

    key = f"{auth_key}|{year}".lower()

    title_guess = _guess_title_from_reference(s)
    return RefAY(reference_full=s, key=key, year=year, authors=authors, title_guess=title_guess)


def parse_reference_numeric(ref: str) -> Optional[RefNum]:
    s = norm_space(ref)
    if not s:
        return None

    num = ""
    m = re.match(r"^\[\s*(\d{1,4})\s*\]\s*(.+)$", s)
    if m:
        num = m.group(1)
        rest = m.group(2).strip()
    else:
        m2 = re.match(r"^(\d{1,4})[.)]\s*(.+)$", s)
        if m2 and not YEAR_RE.fullmatch(m2.group(1)):
            num = m2.group(1)
            rest = m2.group(2).strip()
        else:
            return None

    year = ""
    my = re.search(r"\b(" + YEAR + r")\b", rest)
    if my:
        year = my.group(1)

    authors_part = rest.split(".", 1)[0].strip()
    authors = strip_punct(authors_part)[:160]
    title_guess = _guess_title_from_reference(rest)

    return RefNum(reference_full=s, num=num, title_guess=title_guess, authors=authors, year=year)


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

    left = s[:ym.start()].strip(" ,;")
    left = re.sub(r"^(?:"
                  + "|".join(sorted([re.escape(x) for x in DISCOURSE_PREFIXES], key=len, reverse=True))
                  + r")\b", "", left, flags=re.I).strip(" ,;")

    left = left.strip("()")
    left = left.replace(" et al.", " et al")
    auth = _norm_author_block(left)
    if not auth:
        return None
    return auth, year


def reconcile_author_year(citations: List[str], references: List[RefAY]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ref_map: Dict[str, List[int]] = defaultdict(list)
    for i, r in enumerate(references):
        if not r:
            continue
        ref_map[r.key].append(i)

    cite_counts = Counter()
    parsed_cites: List[Tuple[str, str, str]] = []
    for c in citations:
        parsed = _parse_author_year_from_cite(c)
        if not parsed:
            continue
        auth, year = parsed
        key = f"{auth}|{year}".lower()
        cite_counts[key] += 1
        parsed_cites.append((key, c, year))

    c2r: List[Dict[str, Any]] = []
    for key, c, year in parsed_cites:
        if key in ref_map:
            idx = ref_map[key][0]
            c2r.append({
                "status": "matched",
                "in_text": c,
                "matched_reference": references[idx].reference_full,
                "flags": ""
            })
        else:
            c2r.append({
                "status": "not_found",
                "in_text": c,
                "matched_reference": "",
                "flags": ""
            })

    r2c: List[Dict[str, Any]] = []
    for r in references:
        k = r.key
        times = cite_counts.get(k, 0)
        cited_by = []
        for ck, c, _ in parsed_cites:
            if ck == k:
                cited_by.append(c)
                if len(cited_by) >= 5:
                    break
        r2c.append({
            "times_cited": times,
            "reference": r.reference_full,
            "cited_by_sample": cited_by
        })

    return c2r, r2c


def reconcile_numeric(citations: List[str], references: List[RefNum]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ref_by_num: Dict[str, RefNum] = {r.num: r for r in references if r and r.num}
    cite_counts = Counter(citations)

    c2r: List[Dict[str, Any]] = []
    for num in citations:
        if num in ref_by_num:
            c2r.append({
                "status": "matched",
                "in_text": f"[{num}]",
                "matched_reference": ref_by_num[num].reference_full,
                "flags": ""
            })
        else:
            c2r.append({
                "status": "not_found",
                "in_text": f"[{num}]",
                "matched_reference": "",
                "flags": ""
            })

    r2c: List[Dict[str, Any]] = []
    for r in references:
        times = cite_counts.get(r.num, 0)
        r2c.append({
            "times_cited": times,
            "reference": r.reference_full,
            "cited_by_sample": [f"[{r.num}]"] if times else []
        })

    return c2r, r2c


# -----------------------------
# Crosscheck runner
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

    # style hint helps pick the right References split and post-processing
    style_hint = "auto"
    s0 = (style or "").lower()
    if "ieee" in s0 or "vancouver" in s0 or "numeric" in s0:
        style_hint = "numeric"
    elif "apa" in s0 or "harvard" in s0 or "author" in s0:
        style_hint = "apa"

    if name.endswith(".docx"):
        main_text, ref_block_lines, ref_msg = read_docx_split_main_and_refs(file_bytes)
        references_raw = _merge_reference_lines(ref_block_lines)
        if style_hint == "numeric":
            references_raw = _split_embedded_numeric_refs(references_raw)
        full_text = main_text + "\n" + "\n".join(ref_block_lines)

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

        full_text = main_text + "\n" + "\n".join(references_raw)

    else:
        return {"error": "Upload a DOCX or PDF"}

    style_norm = (style or "apa").strip().lower()
    if style_norm in ("apa/harvard", "harvard", "author-year"):
        style_norm = "apa"

    # Keep regex fast on huge docs
    if len(main_text) > 350_000:
        half = 175_000
        main_text = (main_text[:half] + "\n... [TRUNCATED] ...\n" + main_text[-half:])

    if style_norm == "apa":
        cites = extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]
        c2r, r2c = reconcile_author_year(cites, refs)
        intext_count = len([_parse_author_year_from_cite(c) for c in cites if _parse_author_year_from_cite(c)])
        ref_count = len(refs)

        missing = [x for x in c2r if x["status"] != "matched"]
        uncited = [x for x in r2c if (x.get("times_cited") or 0) == 0]

        match_rate = 0.0
        if len(c2r) > 0:
            match_rate = 100.0 * (len(c2r) - len(missing)) / len(c2r)

    else:
        bracketed = ("ieee" in style_norm) or ("bracket" in style_norm)
        cites_nums = extract_numeric_citations(main_text, bracketed=bracketed)
        refs = [parse_reference_numeric(r) for r in references_raw]
        refs = [r for r in refs if r is not None]
        c2r, r2c = reconcile_numeric(cites_nums, refs)
        intext_count = len(cites_nums)
        ref_count = len(refs)

        missing = [x for x in c2r if x["status"] != "matched"]
        uncited = [x for x in r2c if (x.get("times_cited") or 0) == 0]

        match_rate = 0.0
        if len(c2r) > 0:
            match_rate = 100.0 * (len(c2r) - len(missing)) / len(c2r)

    verify_results = []
    if verify_online and VERIFY_OK and references_raw:
        verify_results = verify_references_batch(
            references_raw,
            mode=verify_mode,
            max_per_batch=max_verify,
            throttle_s=throttle_s,
            use_crossref=use_crossref,
            use_openalex=use_openalex,
        )

    return {
        "meta": {
            "ref_message": ref_msg,
            "style": style_norm,
            "verify_online": bool(verify_online),
        },
        "summary": {
            "in_text": int(intext_count),
            "references": int(ref_count),
            "missing": int(len(missing)),
            "uncited": int(len(uncited)),
            "match_rate": float(round(match_rate, 1)),
        },
        "missing": missing,
        "uncited": uncited,
        "intext_to_reference": c2r,
        "reference_to_intext": r2c,
        "online_verification": verify_results,
    }
