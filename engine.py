"""engine.py - Commercial-grade citation crosschecker core.

Mode C target: commercial-ready behaviour.

Key properties
- Stable API for main.py (/verify): run_crosscheck(..., verify_online=..., verify_mode=..., throttle_s=..., max_verify=...)
- High matching power without inflating false "missing".
- TOC filtering (DOCX style-based + text-window based).
- Strict vs loose in-text counts in output.
- Robust reconciliation for author-year (e.g., Adam & Tweneboah (2008)) and hyphenated surnames.

This module is self-contained (no FastAPI imports).
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from collections import Counter, defaultdict

try:
    from docx import Document
    DOCX_OK = True
except Exception:
    DOCX_OK = False
    Document = None

try:
    import pdfplumber
    PDF_OK = True
except Exception:
    PDF_OK = False
    pdfplumber = None

try:
    from verify import verify_references_batch
    VERIFY_OK = True
except Exception:
    VERIFY_OK = False
    verify_references_batch = None


# -----------------------------
# Regex helpers
# -----------------------------

YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b", re.I)

REF_HEADINGS = [
    r"^\s*references?\s*(?:list)?\s*$",
    r"^\s*bibliograph(?:y|ies)\s*$",
    r"^\s*works\s+cited\s*$",
    r"^\s*literature\s+cited\s*$",
]
REF_HEADING_RE = re.compile("|".join(REF_HEADINGS), re.I | re.M)

# Parenthetical author-year patterns
PAREN_CIT_RE = re.compile(rf"\(([^\)]*?\b{YEAR}\b[^\)]*?)\)", re.I)

# Narrative: Author & Author (2008) / Author and Author (2008)
NARR_CIT_RE = re.compile(
    rf"\b([A-Z][\w'\-\u00C0-\u024F]+(?:\s+(?:&|and)\s+[A-Z][\w'\-\u00C0-\u024F]+|\s+et\s+al\.)?)\s*\(\s*({YEAR})\s*\)",
    re.I,
)

# Loose: Author, 2008 (used ONLY to reduce false "uncited")
LOOSE_AUTHOR_YEAR_RE = re.compile(rf"\b([A-Z][\w'\-\u00C0-\u024F]+)\s*,\s*({YEAR})\b", re.I)


# -----------------------------
# Noise filtering
# -----------------------------

DISCOURSE_PREFIXES = {
    "for instance",
    "for example",
    "e.g",
    "eg",
    "i.e",
    "ie",
    "see",
    "see also",
    "cf",
    "according to",
    "as cited in",
    "as shown in",
    "as reported in",
    "as noted by",
    "moreover",
    "however",
    "therefore",
    "thus",
    "in addition",
    "in fact",
    "for this reason",
    "in summary",
}

GEO_STOPWORDS = {
    # Continents / regions
    "africa",
    "europe",
    "asia",
    "america",
    "north",
    "south",
    "east",
    "west",
    "sub-saharan",
    "sub-sahara",
    # Common org / generic
    "world",
    "bank",
    "imf",
    "oecd",
    "un",
    "who",
    "bis",
    "ecb",
    # Generic narrative words that frequently appear before years
    "crisis",
    "war",
    "revolution",
    "coup",
    "scandal",
    "estimates",
}


def _u(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "")


def norm_text(s: str) -> str:
    s = _u(s).encode("ascii", "ignore").decode("ascii")
    s = s.replace("\u00A0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def norm_token(tok: str) -> str:
    tok = norm_text(tok).lower()
    tok = tok.replace("’", "'")
    tok = re.sub(r"[^a-z0-9\-']+", "", tok)
    # collapse hyphen variants
    tok = tok.replace("-", "")
    return tok


def looks_like_sentence_noise(s: str) -> bool:
    """Reject obvious non-citation phrases that get captured as 'Author, Year'."""
    st = norm_text(s).lower()
    if not st:
        return True
    if len(st) > 60:
        return True
    for p in DISCOURSE_PREFIXES:
        if st.startswith(p + " ") or st == p:
            return True
    first = st.split(" ", 1)[0]
    if first in GEO_STOPWORDS:
        return True
    return False


# -----------------------------
# DOCX / PDF text extraction
# -----------------------------


def _docx_iter_paragraphs(doc: Any) -> Iterable[Tuple[str, str]]:
    """Yield (text, style_name) for paragraphs."""
    for p in getattr(doc, "paragraphs", []) or []:
        t = (p.text or "").strip("\n")
        if not t.strip():
            continue
        style = ""
        try:
            style = (p.style.name or "") if getattr(p, "style", None) else ""
        except Exception:
            style = ""
        yield (t, style)


def extract_text_from_docx(file_bytes: bytes) -> Tuple[str, str]:
    if not DOCX_OK or Document is None:
        raise RuntimeError("python-docx not available")
    doc = Document(io.BytesIO(file_bytes))

    toc_mode = False
    toc_lines: List[str] = []
    body_lines: List[str] = []

    for txt, style in _docx_iter_paragraphs(doc):
        st = (style or "").lower()
        low = txt.strip().lower()

        if "toc" in st or low == "table of contents":
            toc_mode = True
            toc_lines.append(txt)
            continue

        if toc_mode:
            if re.match(r"^\s*(chapter|introduction|abstract|list of tables|list of figures)\b", low):
                toc_mode = False
            else:
                toc_lines.append(txt)
                continue

        body_lines.append(txt)

    return "\n".join(body_lines), "\n".join(toc_lines)


def extract_text_from_pdf(file_bytes: bytes, max_pages: int = 250) -> str:
    if not PDF_OK or pdfplumber is None:
        raise RuntimeError("pdfplumber not available")
    out: List[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        n = min(len(pdf.pages), max_pages)
        for i in range(n):
            try:
                out.append(pdf.pages[i].extract_text() or "")
            except Exception:
                continue
    return "\n".join(out)


# -----------------------------
# Reference section extraction
# -----------------------------


def _looks_like_toc_references_line(line: str) -> bool:
    """Heuristic: '... 123' is likely a TOC entry."""
    s = line.strip()
    if len(s) < 8:
        return False
    if re.search(r"\.{3,}\s*\d+\s*$", s):
        return True
    if re.search(r"\s\d+\s*$", s) and s.count(".") >= 3:
        return True
    return False


def find_references_start_line(lines: Sequence[str]) -> Optional[int]:
    for i, ln in enumerate(lines):
        if REF_HEADING_RE.match((ln or "").strip()):
            return i
    return None


def split_reference_entries(ref_lines: Sequence[str]) -> List[str]:
    """Split lines into reference entries."""
    entries: List[str] = []
    buf: List[str] = []

    def flush() -> None:
        nonlocal buf
        if buf:
            s = norm_text(" ".join(buf))
            if s:
                entries.append(s)
        buf = []

    start_re = re.compile(r"^\s*(?:\[\d+\]|\d+\s*[\.)]|\d+\s+)")

    for ln in ref_lines:
        if not ln or not ln.strip():
            flush()
            continue

        if _looks_like_toc_references_line(ln):
            continue

        if start_re.match(ln) and buf:
            flush()

        if buf and re.match(r"^\s*[A-Z][A-Za-z\-\u00C0-\u024F' ]{2,}\,", ln) and YEAR_RE.search(ln):
            flush()

        buf.append(ln.strip())

    flush()
    return entries


def extract_reference_section(text: str) -> Tuple[List[str], str]:
    lines = (text or "").splitlines()
    start = find_references_start_line(lines)
    if start is None:
        return [], "References heading not found"

    ref_lines: List[str] = []
    for ln in lines[start + 1 :]:
        low = (ln or "").strip().lower()
        if re.match(r"^\s*(appendix|appendices|supplementary|supporting)\b", low):
            break
        ref_lines.append(ln)

    entries = split_reference_entries(ref_lines)
    return entries, f"Found References heading: {lines[start].strip()}"


# -----------------------------
# Keying and matching
# -----------------------------


@dataclass(frozen=True)
class CiteKey:
    a1: str
    a2: str
    year: str
    etal: bool = False

    def as_tuple(self) -> Tuple[str, str, str, bool]:
        return (self.a1, self.a2, self.year, self.etal)


def _parse_year(s: str) -> Optional[str]:
    m = YEAR_RE.search(s or "")
    return m.group(1).lower() if m else None


def _extract_surnames_from_author_chunk(chunk: str) -> Tuple[str, str, bool]:
    c = norm_text(chunk)
    if not c:
        return ("", "", False)

    c = re.sub(r"\s+", " ", c)
    c = c.replace("&", " and ")
    etal = bool(re.search(r"\bet\s+al\.?\b", c, re.I))

    parts = [p.strip() for p in re.split(r"\band\b", c, flags=re.I) if p.strip()]
    if not parts:
        return ("", "", etal)

    a1 = norm_token(parts[0].split()[-1])
    a2 = norm_token(parts[1].split()[-1]) if len(parts) >= 2 else ""

    if not a1 or looks_like_sentence_noise(a1) or a1 in GEO_STOPWORDS:
        return ("", "", etal)
    if a2 in GEO_STOPWORDS:
        a2 = ""

    return (a1, a2, etal)


def citekey_from_intext(author_chunk: str, year: str) -> Optional[CiteKey]:
    y = _parse_year(year)
    if not y:
        return None
    a1, a2, etal = _extract_surnames_from_author_chunk(author_chunk)
    if not a1:
        return None
    return CiteKey(a1=a1, a2=a2, year=y, etal=etal)


def citekey_from_reference(ref: str) -> Optional[CiteKey]:
    s = norm_text(ref)
    y = _parse_year(s)
    if not y:
        return None

    head = s
    y_pos = s.lower().find(y)
    if y_pos > 0:
        head = s[:y_pos]
    head = head.split(".")[0]
    head = head.replace("&", " and ")

    tokens = re.split(r",|\band\b", head, flags=re.I)
    tokens = [t.strip() for t in tokens if t.strip()]

    surnames: List[str] = []
    for t in tokens:
        w = norm_token(t.split()[0])
        if not w or w in GEO_STOPWORDS:
            continue
        surnames.append(w)
        if len(surnames) >= 2:
            break

    if not surnames:
        return None
    a1 = surnames[0]
    a2 = surnames[1] if len(surnames) > 1 else ""
    return CiteKey(a1=a1, a2=a2, year=y, etal=False)


def key_match_score(k: CiteKey, refk: CiteKey) -> float:
    if k.year != refk.year:
        return 0.0
    if k.a1 != refk.a1:
        return 0.0
    if k.etal:
        return 0.95
    if not k.a2 or not refk.a2:
        return 0.85
    return 1.0 if k.a2 == refk.a2 else 0.0


def build_reference_index(ref_entries: Sequence[str]) -> Tuple[Dict[Tuple[str, str, str, bool], str], Dict[str, List[Tuple[CiteKey, str]]]]:
    exact: Dict[Tuple[str, str, str, bool], str] = {}
    by_year: Dict[str, List[Tuple[CiteKey, str]]] = defaultdict(list)

    for r in ref_entries:
        rk = citekey_from_reference(r)
        if not rk:
            continue
        exact[rk.as_tuple()] = r
        by_year[rk.year].append((rk, r))

    return exact, by_year


# -----------------------------
# In-text extraction
# -----------------------------


def _split_parenthetical_group(group: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    parts = re.split(r";|\)|\(|\[|\]|\n", group)
    for p in parts:
        p = p.strip()
        if not p:
            continue
        p = re.sub(r"\bpp?\.?\s*\d+\b", "", p, flags=re.I).strip()
        y = _parse_year(p)
        if not y:
            continue
        m = YEAR_RE.search(p)
        author = p[: m.start()].strip(" ,") if m else p
        author = re.sub(
            r"^(?:see\s+also|see|cf\.|cf|e\.g\.|eg|i\.e\.|ie|for\s+example|for\s+instance)\s+",
            "",
            author,
            flags=re.I,
        ).strip(" ,")
        if not author or looks_like_sentence_noise(author):
            continue
        out.append((author, y))
    return out


def extract_intext_strict(text: str) -> List[CiteKey]:
    keys: List[CiteKey] = []
    t = text or ""

    for m in PAREN_CIT_RE.finditer(t):
        group = m.group(1)
        for author, y in _split_parenthetical_group(group):
            ck = citekey_from_intext(author, y)
            if ck:
                keys.append(ck)

    for m in NARR_CIT_RE.finditer(t):
        ck = citekey_from_intext(m.group(1), m.group(2))
        if ck:
            keys.append(ck)

    return keys


def extract_intext_loose(text: str) -> List[CiteKey]:
    keys = extract_intext_strict(text)

    for m in LOOSE_AUTHOR_YEAR_RE.finditer(text or ""):
        author = m.group(1)
        y = m.group(2)
        if looks_like_sentence_noise(author):
            continue
        a = norm_token(author)
        if not a or a in GEO_STOPWORDS:
            continue
        yy = _parse_year(y)
        if not yy:
            continue
        keys.append(CiteKey(a1=a, a2="", year=yy, etal=False))

    return keys


# -----------------------------
# Reconciliation
# -----------------------------


def reconcile_intext_to_reference(
    intext_keys: Sequence[CiteKey],
    ref_exact: Dict[Tuple[str, str, str, bool], str],
    ref_by_year: Dict[str, List[Tuple[CiteKey, str]]],
    fuzzy_year_scan_cap: int = 2500,
) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str, str, bool], str]]:
    rows: List[Dict[str, Any]] = []
    matched: Dict[Tuple[str, str, str, bool], str] = {}

    for ck in intext_keys:
        best_ref = ""
        status = "missing"

        ex = ref_exact.get(ck.as_tuple())
        if ex:
            best_ref = ex
            status = "matched"
        else:
            cands = ref_by_year.get(ck.year, [])
            if len(cands) > fuzzy_year_scan_cap:
                cands = cands[:fuzzy_year_scan_cap]
            best_s = 0.0
            for rk, ref in cands:
                s = key_match_score(ck, rk)
                if s > best_s:
                    best_s = s
                    best_ref = ref
            if best_s >= 0.85:
                status = "matched"

        rows.append(
            {
                "status": status,
                "in_text": f"{ck.a1}{(' & ' + ck.a2) if ck.a2 else ''}, {ck.year}",
                "matched_reference": best_ref,
            }
        )
        if status == "matched" and best_ref:
            matched[ck.as_tuple()] = best_ref

    return rows, matched


def compute_missing_strict(intext_keys: Sequence[CiteKey], matched: Dict[Tuple[str, str, str, bool], str]) -> List[Dict[str, Any]]:
    cnt = Counter(k.as_tuple() for k in intext_keys)
    out: List[Dict[str, Any]] = []
    for kt, n in cnt.most_common():
        if kt in matched:
            continue
        a1, a2, y, _etal = kt
        label = f"{a1}{(' & ' + a2) if a2 else ''}, {y}"
        out.append({"citation_in_text": label, "count_in_text": n})
    return out


def reconcile_reference_to_intext(
    ref_entries: Sequence[str],
    strict_keys: Sequence[CiteKey],
    loose_keys: Sequence[CiteKey],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    strict_cnt = Counter(k.as_tuple() for k in strict_keys)
    loose_cnt = Counter(k.as_tuple() for k in loose_keys)

    rows: List[Dict[str, Any]] = []
    uncited: List[str] = []

    for ref in ref_entries:
        rk = citekey_from_reference(ref)
        if not rk:
            rows.append({"reference": ref, "times_cited": 0, "cited_by": []})
            continue

        t = rk.as_tuple()
        times = strict_cnt.get(t, 0) or loose_cnt.get(t, 0)
        cited_by = [f"{rk.a1}{(' & ' + rk.a2) if rk.a2 else ''}, {rk.year}"] if times else []

        rows.append({"reference": ref, "times_cited": times, "cited_by": cited_by})
        if times == 0:
            uncited.append(ref)

    return rows, uncited


# -----------------------------
# Public entrypoint
# -----------------------------


def run_crosscheck(
    file_bytes: bytes,
    filename: str = "document",
    style: str = "apa",
    verify_online: bool = False,
    verify_mode: str = "all",
    throttle_s: float = 0.12,
    max_verify: int = 0,
    use_crossref: bool = True,
    use_openalex: bool = True,
    ai_assist: bool = False,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Main worker called by FastAPI.

    Signature is intentionally stable. main.py may pass extra args over time.
    """

    ext = (filename or "").lower()
    toc_text = ""
    if ext.endswith(".docx"):
        body, toc = extract_text_from_docx(file_bytes)
        text = body
        toc_text = toc
    elif ext.endswith(".pdf"):
        text = extract_text_from_pdf(file_bytes)
    else:
        if DOCX_OK:
            try:
                body, toc = extract_text_from_docx(file_bytes)
                text = body
                toc_text = toc
            except Exception:
                text = ""
        else:
            text = ""

    if toc_text and len(toc_text) > 50:
        text = text.replace(toc_text, " ")

    ref_entries, ref_msg = extract_reference_section(text)

    strict_keys = extract_intext_strict(text)
    loose_keys = extract_intext_loose(text)

    strict_intext_count = len(strict_keys)
    loose_intext_count = len(loose_keys)

    ref_exact, ref_by_year = build_reference_index(ref_entries)
    c2r_rows, matched_map = reconcile_intext_to_reference(strict_keys, ref_exact, ref_by_year)
    missing_rows = compute_missing_strict(strict_keys, matched_map)

    r2c_rows, uncited_list = reconcile_reference_to_intext(ref_entries, strict_keys, loose_keys)

    matched_n = sum(1 for r in c2r_rows if r.get("status") == "matched")
    match_rate = (matched_n / strict_intext_count * 100.0) if strict_intext_count else 0.0

    out: Dict[str, Any] = {
        "summary": {
            "in_text_citations_found": strict_intext_count,
            "reference_entries_found": len(ref_entries),
            "missing_in_references": len(missing_rows),
            "uncited_references": len(uncited_list),
            "match_rate": round(match_rate, 1),
            "strict_intext_count": strict_intext_count,
            "loose_intext_count": loose_intext_count,
        },
        "reference_detection_message": ref_msg,
        "missing_in_references": missing_rows,
        "uncited_references": uncited_list,
        "reconciliation_intext_to_reference": c2r_rows,
        "reconciliation_reference_to_intext": r2c_rows,
        "ai_assist": {"enabled": bool(ai_assist), "added_citations": 0},
    }

    if verify_online and VERIFY_OK and verify_references_batch is not None:
        try:
            ov = verify_references_batch(
                ref_entries,
                throttle_s=float(throttle_s or 0.12),
                max_verify=int(max_verify or 0),
                use_crossref=bool(use_crossref),
                use_openalex=bool(use_openalex),
            )
            out["online_verification"] = ov
        except Exception:
            out["online_verification"] = {"summary": {}, "rows": [], "error": "verification_failed"}
    else:
        out["online_verification"] = {"summary": {}, "rows": []}

    return out
