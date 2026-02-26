# engine.py
# Commercial-grade citation detection + reconciliation (Author-Date)
# Focus: robust extraction, TOC/headers suppression, plausibility scoring, weighted fuzzy matching.

from __future__ import annotations

__version__ = "1.4.3-commercial"

import io
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict, Counter

# Optional dependencies
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
    from rapidfuzz import fuzz
    FUZZY_OK = True
except Exception:
    FUZZY_OK = False
    fuzz = None


# -------------------------
# Normalisation
# -------------------------
YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b", re.I)

REF_HEADINGS_RE = re.compile(
    r"^\s*(references?|bibliograph(?:y|ies)|works\s+cited|literature\s+cited)\b",
    re.I,
)

TOC_HEADINGS_RE = re.compile(r"^\s*(table\s+of\s+contents|contents)\s*$", re.I)
TOC_LINE_RE = re.compile(r"\.{3,}\s*\d+\s*$")

REF_BULLET_RE = re.compile(r"^\s*(?:\[\d+\]|\(?\d+\)?[.)]|\d+\s+)\s*")


def _strip_accents(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _norm_text(s: str) -> str:
    s = _strip_accents((s or "")).lower()
    s = s.replace("’", "'")
    s = re.sub(r"[\u2010\u2011\u2012\u2013\u2014]", "-", s)
    s = re.sub(r"[^a-z0-9\-'/& ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _base_year(y: str) -> str:
    y = (y or "").strip()
    m = re.match(r"^((?:19|20)\d{2})", y)
    return m.group(1) if m else y


# -------------------------
# TOC suppression
# -------------------------

def _remove_toc(lines: List[str]) -> List[str]:
    """Remove TOC blocks that create large false-positive citation counts."""
    out: List[str] = []
    i = 0
    n = len(lines)

    while i < n:
        ln = (lines[i] or "").strip()
        if TOC_HEADINGS_RE.match(ln):
            i += 1
            skipped = 0
            while i < n and skipped < 1200:
                x = (lines[i] or "").strip()
                if not x:
                    i += 1
                    skipped += 1
                    continue
                if TOC_LINE_RE.search(x) or re.fullmatch(r"\d+", x):
                    i += 1
                    skipped += 1
                    continue
                # typical TOC entry: short + ends with page number
                if len(x) < 40 and re.search(r"\s\d+\s*$", x):
                    i += 1
                    skipped += 1
                    continue
                # stop skipping when real content begins
                if re.match(r"^\s*(chapter|abstract|introduction)\b", x, re.I):
                    break
                if len(x) > 80 and re.search(r"[.!?]", x):
                    break
                i += 1
                skipped += 1
            continue

        out.append(lines[i])
        i += 1

    return out


def _split_body_and_references(lines: List[str]) -> Tuple[List[str], List[str], str]:
    ref_idx = None
    ref_heading = ""
    for i, ln in enumerate(lines):
        if REF_HEADINGS_RE.match(ln):
            ref_idx = i
            ref_heading = ln.strip()
            break
    if ref_idx is None:
        return lines, [], ""
    return lines[:ref_idx], lines[ref_idx + 1 :], ref_heading


# -------------------------
# File extraction
# -------------------------

def _extract_lines_from_docx(file_bytes: bytes) -> List[str]:
    if not DOCX_OK or Document is None:
        return []

    doc = Document(io.BytesIO(file_bytes))
    lines: List[str] = []

    for p in doc.paragraphs:
        t = _norm_space(p.text)
        if t:
            lines.append(t)

    # Tables often contain references
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                t = _norm_space(cell.text)
                if t:
                    lines.append(t)

    return lines


def _extract_lines_from_pdf(file_bytes: bytes) -> List[str]:
    if not PDF_OK or pdfplumber is None:
        return []

    lines: List[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            for ln in txt.splitlines():
                ln = _norm_space(ln)
                if ln:
                    lines.append(ln)

    return lines


# -------------------------
# Reference parsing
# -------------------------

def _split_authors_blob(left: str) -> List[str]:
    """Extract probable surnames/org tokens from the author part."""
    s = _norm_text(left)
    if not s:
        return []

    # normalise conjunctions
    s = s.replace("&", " and ")
    s = re.sub(r"\bet\s+al\b\.?", "", s)

    # split authors
    parts = re.split(r"\band\b|;|/|\|", s)

    out: List[str] = []
    for p in parts:
        p = p.strip(" ,.;:()[]{}")
        if not p:
            continue
        # remove initials
        p = re.sub(r"\b[a-z]\b", " ", p)
        p = _norm_space(p)
        if not p:
            continue
        toks = p.split()
        cand = toks[-1] if toks else ""
        cand = cand.strip("-' ")
        if len(cand) < 2:
            continue
        out.append(cand)

    # de-dup, preserve order
    seen = set()
    final: List[str] = []
    for x in out:
        k = _norm_text(x).replace(" ", "")
        if k in seen:
            continue
        seen.add(k)
        final.append(x)

    return final


def _make_key(surnames: List[str], year: str) -> str:
    year = _base_year(year)
    surn = [_norm_text(x).replace(" ", "") for x in surnames if x]
    surn = [x for x in surn if x]
    surn.sort()  # order-invariant for matching
    return "|".join(surn) + "|" + year


def _candidate_reference_lines(ref_lines: List[str]) -> List[str]:
    """Merge wrapped reference lines."""
    merged: List[str] = []
    buf = ""

    def looks_like_new(line: str) -> bool:
        if REF_BULLET_RE.match(line):
            return True
        # common: Author, A. (2018). ...
        return bool(re.match(r"^[A-Z].{1,80}\b" + YEAR, line))

    for ln in ref_lines:
        ln = _norm_space(ln)
        if not ln:
            continue
        if looks_like_new(ln):
            if buf:
                merged.append(buf.strip())
            buf = ln
        else:
            buf = (buf + " " + ln).strip() if buf else ln

    if buf:
        merged.append(buf.strip())

    return [x for x in merged if not REF_HEADINGS_RE.match(x)]


def _parse_reference_entry(raw: str) -> Optional[Dict[str, Any]]:
    s = _norm_space(raw)
    if not s:
        return None
    s = REF_BULLET_RE.sub("", s).strip()

    m = YEAR_RE.search(s)
    if not m:
        return None

    year = _base_year(m.group(1))
    left = s[: m.start()].strip(" ,.;:()[]{}")
    if not left:
        return None

    surnames = _split_authors_blob(left)
    if not surnames:
        return None

    key = _make_key(surnames[:3], year)
    return {"raw": s, "year": year, "surnames": surnames, "key": key}


def extract_references(lines: List[str]) -> Tuple[List[Dict[str, Any]], str]:
    body, ref_lines, ref_heading = _split_body_and_references(lines)

    ref_lines = _remove_toc(ref_lines)
    cand = _candidate_reference_lines(ref_lines)

    refs: List[Dict[str, Any]] = []
    for r in cand:
        ent = _parse_reference_entry(r)
        if ent:
            refs.append(ent)

    # de-dup by key, keep longest raw
    best: Dict[str, Dict[str, Any]] = {}
    for ent in refs:
        k = ent["key"]
        if k not in best or len(ent["raw"]) > len(best[k]["raw"]):
            best[k] = ent

    return list(best.values()), ref_heading


# -------------------------
# In-text extraction + plausibility
# -------------------------

PAREN_GROUP_RE = re.compile(r"\((?P<inside>[^()]{0,260}?\b" + YEAR + r"\b[^()]*)\)")

NARRATIVE_RE = re.compile(
    r"(?P<auth>[A-Z][A-Za-z'’\-]+(?:\s*(?:&|and)\s*[A-Z][A-Za-z'’\-]+){0,3}|[A-Z][A-Za-z'’\-]+\s+et\s+al\.)\s*\(\s*(?P<year>"
    + YEAR
    + r")\s*\)",
    re.I,
)

SURNAME_COMMA_YEAR_RE = re.compile(
    r"\b(?P<surname>[A-Z][A-Za-z'’\-]{2,})\s*,\s*(?P<year>" + YEAR + r")\b"
)

DISCOURSE_PREFIXES = [
    "for instance",
    "for example",
    "e.g",
    "eg",
    "i.e",
    "ie",
    "see",
    "cf",
    "according to",
]

# Block common false positives (countries/continents + narrative words)
BLOCKED_TOKENS = set(
    _norm_text(x).replace(" ", "")
    for x in [
        "africa",
        "europe",
        "asia",
        "america",
        "australia",
        "ghana",
        "nigeria",
        "kenya",
        "war",
        "revolution",
        "crisis",
        "scandal",
        "coup",
        "estimates",
        "moreover",
        "chapter",
        "section",
        "appendix",
        "table",
        "figure",
        "source",
        "data",
    ]
)


def _build_ref_lexicons(refs: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]], set]:
    ref_by_key = {r["key"]: r for r in refs}
    ref_by_year: Dict[str, List[str]] = defaultdict(list)
    surn_lex = set()

    for r in refs:
        ref_by_year[r["year"]].append(r["key"])
        for s in r.get("surnames", [])[:3]:
            surn_lex.add(_norm_text(s).replace(" ", ""))

    return ref_by_key, ref_by_year, surn_lex


def _plausible_surname(surname: str, surn_lex: set) -> bool:
    t = _norm_text(surname).replace(" ", "")
    if not t or len(t) < 3:
        return False
    if t in BLOCKED_TOKENS:
        return False
    # If we have refs, require either known surname OR surname is hyphenated/apostrophe (often real)
    if surn_lex:
        if t in surn_lex:
            return True
        if "-" in t or "'" in t:
            return True
        return False
    return True


def _strip_discourse_prefix(s: str) -> str:
    s_norm = _norm_text(s)
    for dp in DISCOURSE_PREFIXES:
        d = _norm_text(dp)
        if s_norm.startswith(d + " "):
            return s[len(dp) :].lstrip(" ,")
    return s


def extract_intext_citations(body_lines: List[str], surn_lex: set) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (strict, loose) citations. Loose is only for debugging, never used to mark refs uncited."""

    text = "\n".join(body_lines)

    strict: List[Dict[str, Any]] = []
    loose: List[Dict[str, Any]] = []

    # Narrative citations: Surname (Year)
    for m in NARRATIVE_RE.finditer(text):
        auth = m.group("auth") or ""
        year = _base_year(m.group("year") or "")
        surnames = _split_authors_blob(auth)

        # Strict filter
        strict_surn = [s for s in surnames if _plausible_surname(s, surn_lex)]
        if strict_surn:
            strict.append(
                {
                    "raw": m.group(0),
                    "year": year,
                    "surnames": strict_surn,
                    "key": _make_key(strict_surn[:3], year),
                    "type": "narrative",
                }
            )

        # Loose record
        if surnames:
            loose.append(
                {
                    "raw": m.group(0),
                    "year": year,
                    "surnames": surnames,
                    "key": _make_key(surnames[:3], year),
                    "type": "narrative_loose",
                }
            )

    # Parenthetical groups: (Surname, Year; ...)
    for m in PAREN_GROUP_RE.finditer(text):
        inside = (m.group("inside") or "").strip()
        if len(inside) < 6:
            continue

        parts = re.split(r"\s*;\s*", inside)
        for part in parts:
            part = _strip_discourse_prefix(part.strip())
            if not part:
                continue

            # Prefer comma-year pattern
            m2 = SURNAME_COMMA_YEAR_RE.search(part)
            if m2:
                year = _base_year(m2.group("year"))
                left = part[: m2.start("year")].strip().rstrip(",")
                surnames = _split_authors_blob(left)
            else:
                ym = YEAR_RE.search(part)
                if not ym:
                    continue
                year = _base_year(ym.group(1))
                left = part[: ym.start()].strip(" ,.;:").rstrip(",")
                surnames = _split_authors_blob(left)

            strict_surn = [s for s in surnames if _plausible_surname(s, surn_lex)]
            if strict_surn:
                strict.append(
                    {
                        "raw": "(" + part + ")",
                        "year": year,
                        "surnames": strict_surn,
                        "key": _make_key(strict_surn[:3], year),
                        "type": "parenthetical",
                    }
                )

            if surnames:
                loose.append(
                    {
                        "raw": "(" + part + ")",
                        "year": year,
                        "surnames": surnames,
                        "key": _make_key(surnames[:3], year),
                        "type": "parenthetical_loose",
                    }
                )

    # De-dup
    def dedupe(lst: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        out: List[Dict[str, Any]] = []
        for c in lst:
            k = (c.get("key"), c.get("raw"))
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
        return out

    return dedupe(strict), dedupe(loose)


# -------------------------
# Matching
# -------------------------

def _weighted_fuzzy_match(cit: Dict[str, Any], ref_by_key: Dict[str, Dict[str, Any]], ref_by_year: Dict[str, List[str]]) -> Optional[Tuple[str, float]]:
    if not FUZZY_OK or fuzz is None:
        return None

    year = cit.get("year") or ""
    cand_keys = ref_by_year.get(year) or list(ref_by_key.keys())
    if not cand_keys:
        return None

    c_auth = " ".join(cit.get("surnames", [])[:3])
    best_key = None
    best_score = -1.0

    for rk in cand_keys:
        ref = ref_by_key[rk]
        r_auth = " ".join(ref.get("surnames", [])[:3])

        s1 = fuzz.token_set_ratio(_norm_text(c_auth), _norm_text(r_auth))
        s2 = fuzz.ratio(_norm_text(c_auth + " " + year), _norm_text(r_auth + " " + (ref.get("year") or "")))
        score = 0.8 * s1 + 0.2 * s2

        if score > best_score:
            best_score = score
            best_key = rk

    if best_key is None:
        return None

    return best_key, float(best_score)


def reconcile(strict_intexts: List[Dict[str, Any]], refs: List[Dict[str, Any]], mode: str = "commercial") -> Dict[str, Any]:
    ref_by_key, ref_by_year, surn_lex = _build_ref_lexicons(refs)

    matched_intext_to_ref: Dict[str, str] = {}
    matched_refs: set = set()

    # exact
    for c in strict_intexts:
        k = c.get("key")
        if k and k in ref_by_key:
            matched_intext_to_ref[k] = k
            matched_refs.add(k)

    # fuzzy fallback
    thresh = 92.0 if mode == "academic" else 88.0
    for c in strict_intexts:
        k = c.get("key")
        if not k or k in matched_intext_to_ref:
            continue
        hit = _weighted_fuzzy_match(c, ref_by_key, ref_by_year)
        if not hit:
            continue
        rk, sc = hit
        if sc >= thresh:
            matched_intext_to_ref[k] = rk
            matched_refs.add(rk)

    # missing in references (strict only)
    miss_counter = Counter()
    for c in strict_intexts:
        if c.get("key") not in matched_intext_to_ref:
            label = f"{', '.join(c.get('surnames', [])[:2])}, {c.get('year','')}".strip(" ,")
            miss_counter[label] += 1

    missing_rows = [{"citation": k, "count": v} for k, v in miss_counter.most_common()]

    # uncited references (strict only)
    uncited = [ref_by_key[k] for k in ref_by_key.keys() if k not in matched_refs]

    match_rate = 0.0
    if refs:
        match_rate = 100.0 * (len(refs) - len(uncited)) / len(refs)

    return {
        "missing_rows": missing_rows,
        "uncited": uncited,
        "matched_intext": matched_intext_to_ref,
        "match_rate": match_rate,
    }


# -------------------------
# Public API expected by main.py
# -------------------------

def run_crosscheck(file_bytes: bytes, filename: str, style: str = "apa", mode: str = "commercial") -> Dict[str, Any]:
    filename = filename or "document"
    style = (style or "apa").lower()
    mode = (mode or "commercial").lower()

    if filename.lower().endswith(".docx"):
        lines = _extract_lines_from_docx(file_bytes)
    else:
        lines = _extract_lines_from_pdf(file_bytes)

    lines = _remove_toc(lines)
    body_lines, _, ref_heading = _split_body_and_references(lines)

    refs, ref_heading2 = extract_references(lines)
    if not ref_heading:
        ref_heading = ref_heading2

    _, _, surn_lex = _build_ref_lexicons(refs)
    strict_intexts, loose_intexts = extract_intext_citations(body_lines, surn_lex)

    rec = reconcile(strict_intexts, refs, mode=mode)

    # UI expects list-of-rows and numeric fields
    out: Dict[str, Any] = {
        "intext_citations_found": len(strict_intexts),
        "reference_entries_found": len(refs),
        "missing_in_references": len(rec["missing_rows"]),
        "uncited_references": len(rec["uncited"]),
        "match_rate": round(rec["match_rate"], 1),
        "ref_heading": ref_heading or "",
        "missing_rows": rec["missing_rows"],
        "uncited_rows": [
            {
                "raw": r.get("raw", ""),
                "year": r.get("year", ""),
                "key": r.get("key", ""),
            }
            for r in rec["uncited"]
        ],
        # Optional, for debugging and commercial support
        "strict_intext_count": len(strict_intexts),
        "loose_intext_count": len(loose_intexts),
        "debug": {
            "style": style,
            "mode": mode,
            "version": __version__,
        },
    }

    return out


# Backwards-compatible helpers
def extract_text_from_docx(file_bytes: bytes) -> str:
    return "\n".join(_extract_lines_from_docx(file_bytes))


def extract_text_from_pdf(file_bytes: bytes) -> str:
    return "\n".join(_extract_lines_from_pdf(file_bytes))
