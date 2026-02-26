# engine.py
from __future__ import annotations

__version__ = "1.4.0"

import io
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict, Counter

ENGINE_BUILD = "commercial-final-2026-02-26"

# Optional deps
try:
    from docx import Document
    DOCX_OK = True
except Exception:
    Document = None
    DOCX_OK = False

try:
    import pdfplumber
    PDF_OK = True
except Exception:
    pdfplumber = None
    PDF_OK = False

try:
    from rapidfuzz import fuzz
    FUZZ_OK = True
except Exception:
    fuzz = None
    FUZZ_OK = False


# -----------------------------
# Utilities
# -----------------------------
YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b", re.I)
DECADE_RE = re.compile(r"\b(18|19|20)\d0s\b", re.I)

REF_HEADINGS = [
    r"^\s*references?\s*(?:list)?\s*$",
    r"^\s*bibliograph(?:y|ies)\s*$",
    r"^\s*works\s+cited\s*$",
    r"^\s*literature\s+cited\s*$",
]

# Discourse starters that should NEVER be treated as authors
DISCOURSE_PREFIXES = {
    "according", "based", "using", "see", "e.g", "eg", "for", "in", "on", "from",
    "like", "such", "as", "also", "moreover", "however", "therefore", "thus",
    "table", "figure", "fig", "appendix", "chapter", "section",
}

# Geo / common nouns often mis-detected as authors when you loosen patterns
GEO_STOP = {
    "africa","europe","asia","america","australia","antarctica",
    "ghana","nigeria","kenya","south","african","countries","country",
    "world","global","international","sub","saharan","sub-saharan",
}

ORG_ALIASES = {
    "world health organization": "who",
    "world health organisation": "who",
    "who": "who",
    "imf": "imf",
    "international monetary fund": "imf",
    "oecd": "oecd",
    "auc/oecd": "auc/oecd",
    "auc": "auc",
    "un": "un",
    "united nations": "un",
    "bis": "bis",
}

def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")

def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _strip_punct(s: str) -> str:
    return re.sub(r"[^\w\s\-'/]", "", s or "").strip()

def _base_year(y: str) -> str:
    m = re.match(r"^((?:19|20)\d{2})", (y or "").strip())
    return m.group(1) if m else (y or "").strip()

def _is_false_author_token(tok: str) -> bool:
    t = _strip_punct(_norm_space(tok)).lower()
    if not t:
        return True
    if t in DISCOURSE_PREFIXES or t in GEO_STOP:
        return True
    if len(t) <= 2 and t not in {"un", "uk", "us", "eu"}:
        return True
    if t.isdigit():
        return True
    return False

def _canon_author_token(tok: str) -> str:
    t = _norm_space(tok).replace("’", "'")
    t_low = t.lower()
    if t_low in ORG_ALIASES:
        return ORG_ALIASES[t_low]
    # normalize hyphens vs spaces for compound names
    t_low = t_low.replace("-", " ")
    t_low = _norm_space(t_low)
    return t_low

def _first_author_key(author_blob: str) -> str:
    s = _norm_space(author_blob)
    if not s:
        return ""
    s = s.replace("&", " and ")
    s = re.sub(r"\bet\s+al\.?\b", "", s, flags=re.I)
    # take first author segment before 'and' or comma
    seg = re.split(r"\band\b|;", s, flags=re.I)[0].strip()
    if "," in seg:
        seg = seg.split(",", 1)[0].strip()
    else:
        # last word often surname; but keep full if org (all caps / contains /)
        words = seg.split()
        if len(words) >= 1:
            seg = words[-1]
    seg = re.sub(r"[^A-Za-z\-/ '’]+", "", seg).strip()
    seg = seg.replace("’", "'")
    return _canon_author_token(seg)

def _safe_ratio(a: str, b: str) -> int:
    if not FUZZ_OK:
        return 0
    return int(fuzz.token_set_ratio(a or "", b or ""))


# -----------------------------
# Reference parsing
# -----------------------------
@dataclass(frozen=True)
class RefAY:
    raw: str
    author: str
    year: str
    key: str

def parse_reference_author_year(line: str) -> Optional[RefAY]:
    """
    Accept:
      - Adam, A. M., & Tweneboah, G. (2008). ...
      - Newman, I., 1998. Qualitative-quantitative ...
      - AUC/OECD (2018) ...
    """
    if not line or len(line.strip()) < 10:
        return None
    s = _norm_space(_nfkc(line))
    # strip leading numbering/bullets
    s = re.sub(r"^\s*(?:\[\d+\]|\(?\d+\)?[.)]|\d+\s+)\s*", "", s)

    # find first year anywhere early
    m = YEAR_RE.search(s)
    if not m:
        return None
    y = _base_year(m.group(1))

    left = s[:m.start()].strip()
    # if year is in parentheses, left may end with '('
    left = left.rstrip(" ([").strip()
    # allow comma-year format: Newman, I., 1998.
    left = left.rstrip(",;:").strip()

    if not left:
        return None

    akey = _first_author_key(left)
    if not akey or _is_false_author_token(akey):
        # org author with slash maybe
        akey = _canon_author_token(left)
        if _is_false_author_token(akey):
            return None

    key = f"{akey}|{y}"
    return RefAY(raw=s, author=left, year=y, key=key)


# -----------------------------
# In-text citation extraction
# -----------------------------
# STRICT: only real-looking author-year citations (avoid discourse and geo)
STRICT_PAREN = re.compile(
    rf"\(([^()]{0,140}?\b(?:{YEAR})\b[^()]{{0,40}}?)\)",
    re.I
)

# Detect patterns like: Adam & Tweneboah (2008)
NARRATIVE = re.compile(
    r"\b([A-Z][A-Za-z\-']{1,40}(?:\s*(?:&|and)\s*[A-Z][A-Za-z\-']{1,40}|(?:\s+et\s+al\.? )?)?)\s*\(\s*(" + YEAR + r")\s*\)",
    re.I
)

# Also accept comma-year without parentheses in text, but ONLY when it looks like a citation chunk
INLINE_COMMA_YEAR = re.compile(
    r"\b([A-Z][A-Za-z\-']{1,40}(?:\s+(?:et\s+al\.? )|(?:\s*(?:&|and)\s*[A-Z][A-Za-z\-']{1,40})?)\s*,\s*(" + YEAR + r"))\b",
    re.I
)

def _split_citation_group(group: str) -> List[str]:
    # split (A, 2001; B & C, 2009) into pieces
    parts = [p.strip() for p in re.split(r";", group or "") if p.strip()]
    out = []
    for p in parts:
        p = _norm_space(p)
        # drop leading discourse
        head = p.split(",")[0].strip()
        if _is_false_author_token(head):
            continue
        if YEAR_RE.search(p):
            out.append(p)
    return out

def extract_author_year_citations_strict(text: str) -> List[str]:
    t = _nfkc(text or "")
    cites: List[str] = []

    # parenthetical groups
    for m in STRICT_PAREN.finditer(t):
        group = m.group(1)
        cites.extend(_split_citation_group(group))

    # narrative forms
    for m in NARRATIVE.finditer(t):
        blob = _norm_space(m.group(1))
        yr = _base_year(m.group(2))
        head = blob.split()[0]
        if _is_false_author_token(head):
            continue
        cites.append(f"{blob}, {yr}")

    return cites

def extract_author_year_citations_loose(text: str) -> List[str]:
    """Looser net: helps reconciliation, but should not drive 'Missing'."""
    t = _nfkc(text or "")
    cites = extract_author_year_citations_strict(t)

    # inline comma-year (very carefully)
    for m in INLINE_COMMA_YEAR.finditer(t):
        blob = _norm_space(m.group(1))
        yr = _base_year(m.group(2))
        head = blob.split()[0]
        if _is_false_author_token(head):
            continue
        cites.append(f"{blob}, {yr}")

    # de-dup
    seen = set()
    out = []
    for c in cites:
        c2 = _norm_space(c)
        if c2 not in seen:
            seen.add(c2)
            out.append(c2)
    return out


def _parse_author_year_from_cite(cite: str) -> Optional[Tuple[str, str]]:
    s = _norm_space(cite)
    m = YEAR_RE.search(s)
    if not m:
        return None
    y = _base_year(m.group(1))
    left = s[:m.start()].strip().rstrip(",;:([")
    akey = _first_author_key(left)
    if not akey or _is_false_author_token(akey):
        akey = _canon_author_token(left)
        if _is_false_author_token(akey):
            return None
    return akey, y


# -----------------------------
# Reconciliation (weighted fuzzy)
# -----------------------------
def reconcile_author_year_dual(
    strict_cites: List[str],
    loose_cites: List[str],
    refs: List[RefAY],
    fuzzy_min: int = 88,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], int, int]:
    """
    Returns:
      c2r, r2c, missing_rows (STRICT only), uncited_refs (after using LOOSE for matching),
      strict_intext_count, loose_intext_count
    """
    # Build ref index by key and year buckets
    by_key: Dict[str, RefAY] = {r.key: r for r in refs}
    refs_by_year: Dict[str, List[RefAY]] = defaultdict(list)
    for r in refs:
        refs_by_year[r.year].append(r)

    def match_one(cite: str) -> Optional[Tuple[RefAY, str, int]]:
        parsed = _parse_author_year_from_cite(cite)
        if not parsed:
            return None
        akey, y = parsed
        k = f"{akey}|{y}"
        if k in by_key:
            return by_key[k], "exact", 100

        # weighted fuzzy within year
        c_auth = akey
        best: Optional[Tuple[RefAY, int]] = None
        for r in refs_by_year.get(y, []):
            s = _safe_ratio(c_auth, r.key.split("|", 1)[0])
            if best is None or s > best[1]:
                best = (r, s)

        if best and best[1] >= fuzzy_min:
            return best[0], "fuzzy", best[1]
        return None

    # Run matching using LOOSE cites for maximum coverage
    used_ref_keys: set = set()
    cite_to_ref: Dict[str, Dict[str, Any]] = {}
    cite_counts = Counter(_norm_space(c) for c in loose_cites if c)

    for c in cite_counts.keys():
        m = match_one(c)
        if m:
            r, how, score = m
            used_ref_keys.add(r.key)
            cite_to_ref[c] = {
                "citation_in_text": c,
                "reference_key": r.key,
                "match_type": how,
                "score": score,
                "reference": r.raw,
            }

    # c2r: show only strict cites (clean report) but mapped using best available
    c2r: List[Dict[str, Any]] = []
    strict_counts = Counter(_norm_space(c) for c in strict_cites if c)
    for c, ct in strict_counts.items():
        row = {"citation_in_text": c, "count": int(ct)}
        if c in cite_to_ref:
            row.update({
                "status": "matched",
                "match_type": cite_to_ref[c]["match_type"],
                "score": cite_to_ref[c]["score"],
                "reference": cite_to_ref[c]["reference"],
            })
        else:
            # Sometimes strict citation differs slightly from loose normalisation
            # Try a direct match on parse key
            m = match_one(c)
            if m:
                r, how, score = m
                used_ref_keys.add(r.key)
                row.update({"status":"matched", "match_type":how, "score":score, "reference":r.raw})
            else:
                row.update({"status":"missing"})
        c2r.append(row)

    # missing_rows: only STRICT items that are truly not matched
    missing_rows: List[Dict[str, Any]] = []
    for row in c2r:
        if row.get("status") == "missing":
            missing_rows.append({
                "citation_in_text": row["citation_in_text"],
                "count": row.get("count", 1)
            })

    # r2c + uncited after using loose matches
    r2c: List[Dict[str, Any]] = []
    for r in refs:
        if r.key in used_ref_keys:
            # gather all cites that matched this ref (from loose)
            matched_cites = [c for c, payload in cite_to_ref.items() if payload["reference_key"] == r.key]
            total = sum(cite_counts[c] for c in matched_cites)
            r2c.append({
                "reference": r.raw,
                "reference_key": r.key,
                "status": "cited",
                "count": int(total),
                "matched_citations": matched_cites[:20],
            })
        else:
            r2c.append({
                "reference": r.raw,
                "reference_key": r.key,
                "status": "uncited",
                "count": 0,
                "matched_citations": [],
            })

    uncited_refs = [x for x in r2c if x.get("status") == "uncited"]

    return c2r, r2c, missing_rows, uncited_refs, int(sum(strict_counts.values())), int(sum(cite_counts.values()))


# -----------------------------
# DOCX / PDF reading
# -----------------------------
def read_pdf_text(file_bytes: bytes) -> str:
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")
    text_parts: List[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    return "\n".join(text_parts)

def read_docx_text(file_bytes: bytes) -> List[str]:
    if not DOCX_OK:
        raise RuntimeError("python-docx not installed")
    doc = Document(io.BytesIO(file_bytes))
    out: List[str] = []
    # paragraphs
    for p in doc.paragraphs:
        t = p.text or ""
        if t.strip():
            out.append(t)
    # tables (important for theses)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                t = cell.text or ""
                t = _norm_space(t)
                if t.strip():
                    out.append(t)
    return out

def _find_reference_heading(lines: List[str]) -> int:
    for i, ln in enumerate(lines):
        s = (ln or "").strip()
        for pat in REF_HEADINGS:
            if re.match(pat, s, flags=re.I):
                return i
    return -1

def read_docx_split_main_and_refs(file_bytes: bytes) -> Tuple[str, List[str], str]:
    lines = read_docx_text(file_bytes)
    idx = _find_reference_heading(lines)
    if idx == -1:
        return "\n".join(lines), [], "No References heading found."
    main = "\n".join(lines[:idx]).strip()
    ref_lines = [ln for ln in lines[idx+1:] if ln.strip()]
    return main, ref_lines, f"Found References heading: {lines[idx].strip()}"

def _merge_reference_lines(ref_lines: List[str]) -> List[str]:
    """
    Merge broken lines into reference entries.
    Heuristic: a new entry often starts with numbering or author-like capital token.
    """
    merged: List[str] = []
    buf: List[str] = []

    def flush():
        nonlocal buf
        if buf:
            merged.append(_norm_space(" ".join(buf)))
            buf = []

    starter = re.compile(r"^\s*(?:\[\d+\]|\(?\d+\)?[.)]|\d+\s+)?\s*[A-Z]", re.I)

    for ln in ref_lines or []:
        s = _norm_space(ln)
        if not s:
            continue
        if starter.match(s) and buf and YEAR_RE.search(" ".join(buf)):
            flush()
        buf.append(s)
    flush()
    return [m for m in merged if len(m) > 10]


# -----------------------------
# Light dedupe (no O(n^2))
# -----------------------------
def _dedupe_refs(refs: List[RefAY]) -> List[RefAY]:
    seen = set()
    out: List[RefAY] = []
    for r in refs:
        # canonical on (first_author_key, year)
        k = r.key
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


# -----------------------------
# Main entry (used by FastAPI)
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
    """
    Commercial-grade behavior:
      - STRICT stream drives Missing (reduces false positives)
      - LOOSE stream + weighted fuzzy drives reconciliation + Uncited
      - Always returns JSON, never crashes caller
    """
    try:
        name = (filename or "").lower().strip()
        style_s = (style or "apa").strip().lower()

        if not (name.endswith(".docx") or name.endswith(".pdf")):
            return {"ok": False, "error": "Upload a DOCX or PDF"}

        if name.endswith(".docx"):
            main_text, ref_lines, ref_msg = read_docx_split_main_and_refs(file_bytes)
        else:
            full = read_pdf_text(file_bytes)
            lines = full.splitlines()
            idx = _find_reference_heading(lines)
            if idx == -1:
                main_text = full
                ref_lines = []
                ref_msg = "No References heading found."
            else:
                main_text = "\n".join(lines[:idx]).strip()
                ref_msg = f"Found References heading: {lines[idx].strip()}"
                ref_lines = [ln for ln in lines[idx+1:] if ln.strip()]

        references_raw = _merge_reference_lines(ref_lines)
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]
        refs = _dedupe_refs(refs)

        strict_cites = extract_author_year_citations_strict(main_text)
        loose_cites = extract_author_year_citations_loose(main_text)

        c2r, r2c, missing_rows, uncited_refs, strict_ct, loose_ct = reconcile_author_year_dual(
            strict_cites=strict_cites,
            loose_cites=loose_cites,
            refs=refs,
            fuzzy_min=88
        )

        ref_count = len(refs)
        missing_unique = len(missing_rows)
        # match rate computed on STRICT stream (reporting-grade)
        match_rate = 0.0
        if strict_ct > 0:
            match_rate = 100.0 * max(0.0, float(strict_ct - missing_unique)) / float(strict_ct)

        warnings: List[str] = []
        if loose_ct > strict_ct * 1.4 and strict_ct > 0:
            warnings.append("Loose detection found many more items than strict. Use 'Missing' list as the credible compliance list.")

        return {
            "ok": True,
            "filename": filename,
            "style": style_s,
            "engine_build": ENGINE_BUILD,
            "reference_detection_message": ref_msg,
            "summary": {
                "in_text_citations_found": int(strict_ct),  # report strict as the UI metric
                "reference_entries_found": int(ref_count),
                "missing_in_references": int(missing_unique),
                "uncited_references": int(len(uncited_refs)),
                "match_rate": float(round(match_rate, 1)),
                # Added debug fields requested
                "strict_intext_count": int(strict_ct),
                "loose_intext_count": int(loose_ct),
            },
            "missing_in_references": missing_rows,
            "uncited_references": uncited_refs,
            "reconciliation_intext_to_reference": c2r,
            "reconciliation_reference_to_intext": r2c,
            "references_raw": references_raw,
            "warnings": warnings,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"engine failure: {type(e).__name__}: {e}",
            "summary": {
                "in_text_citations_found": 0,
                "reference_entries_found": 0,
                "missing_in_references": 0,
                "uncited_references": 0,
                "match_rate": 0.0,
                "strict_intext_count": 0,
                "loose_intext_count": 0,
            },
            "missing_in_references": [],
            "uncited_references": [],
            "reconciliation_intext_to_reference": [],
            "reconciliation_reference_to_intext": [],
            "references_raw": [],
            "warnings": ["engine recovered from exception; check server logs for details"],
        }
