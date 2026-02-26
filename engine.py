# engine.py
# Commercial-grade citation detection + reconciliation (APA/Harvard primary)
# Design goals:
# - Missing_in_references computed from STRICT citations only (prevents false missing inflation)
# - Uncited_references reduced using STRICT + LOOSE citations (max matching power)
# - Plausibility / narrative classifier to suppress "Africa, ..." "War, 2003" etc.
# - Weighted fuzzy fallback (author+year+title) for tough cases
#
# Exposes: run_crosscheck(...) used by main.py

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter, defaultdict

# Optional dependencies
try:
    from docx import Document  # python-docx
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

# Fuzzy matching (prefer rapidfuzz)
try:
    from rapidfuzz import fuzz
    FUZZ_OK = True
except Exception:
    fuzz = None
    FUZZ_OK = False


# -----------------------------
# Constants / Heuristics
# -----------------------------

YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b", re.I)

REF_HEADINGS = [
    r"^\s*references?\s*(?:list)?\s*$",
    r"^\s*bibliograph(?:y|ies)\s*$",
    r"^\s*works\s+cited\s*$",
    r"^\s*literature\s+cited\s*$",
]

# If these appear at the beginning of a captured "citation",
# it is almost always narrative text, not an author.
NARRATIVE_PREFIXES = {
    "according", "see", "e.g", "eg", "for", "for instance", "for example", "example",
    "instance", "as", "as reported", "as noted", "as shown", "as suggested",
    "in", "on", "at", "by", "from", "to", "the", "a", "an", "this", "that",
    "like", "such", "including", "includes", "notably", "moreover",
}

# Common “false author” nouns we saw in your logs/screens
FALSE_AUTHOR_NOUNS = {
    "war", "crisis", "scandal", "revolution", "coup", "estimates", "statistics",
    "figure", "table", "appendix", "chapter", "section", "model", "equation",
}

# Continents + common regions (keep small; you can expand later)
REGIONS = {
    "africa", "europe", "asia", "america", "oceania",
    "sub-saharan", "sub saharan", "middle east", "gulf", "gcc",
}

# “Country-like” tokens frequently causing false hits when preceding commas
# (keep small, high-impact; don’t try to list the world)
COUNTRY_TOKENS = {
    "ghana", "nigeria", "kenya", "south africa", "malaysia", "china", "wuhan",
    "turkey", "pakistan", "romania", "ireland", "brics", "emu", "us", "usa", "uk",
}

# Organization alias normalization (minimal, high-value)
ORG_ALIASES = {
    "world health organisation": "world health organization",
    "who": "world health organization",
    "imf": "international monetary fund",
    "ecb": "european central bank",
    "bis": "bank for international settlements",
    "oecd": "oecd",
    "auc/oecd": "auc oecd",
    "auc oecd": "auc oecd",
}


# -----------------------------
# Helpers: text normalization
# -----------------------------

def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _norm_basic(s: str) -> str:
    s = _strip_accents(s or "")
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    s = s.replace("\u00a0", " ")
    return _norm_space(s)

def _norm_key_token(s: str) -> str:
    # Lowercase and keep letters only for matching surnames robustly
    s = _strip_accents(s or "").lower()
    s = re.sub(r"[^a-z]+", "", s)
    return s

def _norm_author_blob(s: str) -> str:
    s = _norm_basic(s).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\bet al\.?\b", " etal ", s)
    s = re.sub(r"[^a-z0-9\s\-\/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # normalize org aliases
    s2 = s
    for k, v in ORG_ALIASES.items():
        if s2 == k:
            s2 = v
    return s2

def _title_hint(s: str, max_len: int = 60) -> str:
    s = _norm_basic(s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= max_len:
        return s
    return s[:max_len].rstrip()

def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


# -----------------------------
# File extraction
# -----------------------------

def _extract_text_from_docx(file_bytes: bytes) -> str:
    if not DOCX_OK:
        return ""
    doc = Document(io.BytesIO(file_bytes))
    parts: List[str] = []

    # paragraphs
    for p in doc.paragraphs:
        t = _norm_basic(p.text)
        if t:
            parts.append(t)

    # tables (important for “hanging indent” / broken reference lines)
    for tbl in doc.tables:
        for row in tbl.rows:
            row_txt = []
            for cell in row.cells:
                ct = _norm_basic(cell.text)
                if ct:
                    row_txt.append(ct)
            if row_txt:
                parts.append(" | ".join(row_txt))

    return "\n".join(parts)

def _extract_text_from_pdf(file_bytes: bytes) -> str:
    if not PDF_OK:
        return ""
    text_parts: List[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            t = _norm_basic(t)
            if t:
                text_parts.append(t)
    return "\n".join(text_parts)

def _extract_full_text(file_bytes: bytes, filename: str) -> str:
    fn = (filename or "").lower()
    if fn.endswith(".docx"):
        return _extract_text_from_docx(file_bytes)
    if fn.endswith(".pdf"):
        return _extract_text_from_pdf(file_bytes)
    # fallback: try docx then pdf
    t = _extract_text_from_docx(file_bytes)
    if t:
        return t
    return _extract_text_from_pdf(file_bytes)


# -----------------------------
# Reference section detection + parsing
# -----------------------------

def _find_references_start(lines: List[str]) -> int:
    heading_res = [re.compile(pat, re.I) for pat in REF_HEADINGS]
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        for hre in heading_res:
            if hre.match(s):
                return i
    return -1

def _slice_reference_lines(full_text: str) -> Tuple[str, List[str]]:
    lines = full_text.splitlines()
    idx = _find_references_start(lines)
    if idx < 0:
        return "", []
    # everything after heading
    ref_lines = lines[idx + 1 :]
    return lines[idx].strip(), ref_lines

def _is_probable_ref_line(line: str) -> bool:
    # Must contain a year or a DOI/URL-ish marker
    if YEAR_RE.search(line):
        return True
    if "doi" in line.lower() or "http://" in line.lower() or "https://" in line.lower():
        return True
    return False

def _split_reference_entries(ref_lines: List[str]) -> List[str]:
    """
    Robustly merge broken lines into reference entries.
    Handles:
    - numbering like "1." "2)" "[3]"
    - hanging indents where continuation line doesn’t start with a number
    """
    entries: List[str] = []
    buf: List[str] = []

    start_re = re.compile(r"^\s*(\[\d+\]|\d+\s*[\.\)]|\(\d+\))\s+")
    for raw in ref_lines:
        ln = _norm_basic(raw)
        if not ln:
            continue

        starts_new = bool(start_re.match(ln))
        # also start new if line looks like a new author line and buffer already has a plausible ref
        looks_new_author = bool(re.match(r"^[A-Z][A-Za-z\-\']+(?:\s+[A-Z][A-Za-z\-\']+)?\s*,", ln))

        if buf and (starts_new or (looks_new_author and _is_probable_ref_line(ln) and _is_probable_ref_line(" ".join(buf)))):
            entries.append(_norm_space(" ".join(buf)))
            buf = []

        # strip numbering token
        ln = start_re.sub("", ln).strip()
        buf.append(ln)

    if buf:
        entries.append(_norm_space(" ".join(buf)))

    # keep only plausible refs
    out = []
    for e in entries:
        if _is_probable_ref_line(e) and len(e) >= 12:
            out.append(e)
    return out

def _extract_year(s: str) -> Optional[str]:
    m = YEAR_RE.search(s or "")
    if not m:
        return None
    return m.group(1)

def _extract_author_blob(ref: str) -> str:
    """
    Try to capture author portion: from start up to year token.
    Works even if year is like "1998." without parentheses.
    """
    ref = _norm_basic(ref)
    y = _extract_year(ref)
    if not y:
        # fallback: up to first period
        parts = ref.split(".", 1)
        return parts[0].strip() if parts else ref.strip()

    # take everything before year occurrence
    pos = ref.lower().find(y.lower())
    if pos <= 0:
        parts = ref.split(".", 1)
        return parts[0].strip() if parts else ref.strip()

    blob = ref[:pos].strip(" ,.;:-")
    # remove leading editors markers if any
    blob = re.sub(r"^\s*(eds?|editor(?:s)?)\b[:\s\-]*", "", blob, flags=re.I).strip()
    return blob

def _extract_title_blob(ref: str) -> str:
    """
    Rough title hint: after year token to next period.
    """
    ref = _norm_basic(ref)
    y = _extract_year(ref)
    if not y:
        return ""
    # split around year
    parts = re.split(rf"\b{re.escape(y)}\b", ref, maxsplit=1, flags=re.I)
    if len(parts) < 2:
        return ""
    tail = parts[1]
    tail = tail.lstrip(" )].,;:-")
    # title usually up to next period
    t = tail.split(".", 1)[0].strip()
    return t

def _surnames_from_author_blob(blob: str) -> List[str]:
    """
    Extract surname tokens robustly:
    - handles "Kyereboah‐Coleman" vs "Kyereboah Coleman"
    - handles "&", "and", commas
    - handles org authors (AUC/OECD etc.)
    """
    b = _norm_author_blob(blob)

    # Org style: if it is mostly uppercase or contains '/', keep as a single token
    if "/" in b or (b.isupper() and len(b) <= 12):
        tok = _norm_key_token(b.replace("/", " "))
        return [tok] if tok else []

    # Split by separators
    b = b.replace("/", " ")
    b = re.sub(r"\betal\b", " etal ", b)
    b = re.sub(r"\band\b", " ", b)
    b = b.replace(",", " ")
    b = b.replace("&", " ")
    tokens = [t for t in re.split(r"\s+", b) if t]

    # Heuristic: surnames are usually alphabetic tokens, keep the “strong” ones
    surnames: List[str] = []
    for t in tokens:
        if t in {"etal"}:
            continue
        if len(t) < 2:
            continue
        # remove hyphens, apostrophes already mostly removed by normalization
        key = _norm_key_token(t)
        if not key:
            continue
        surnames.append(key)

    # Prefer first 3 strong tokens (avoid huge org blobs)
    if len(surnames) > 5:
        surnames = surnames[:5]
    return surnames

@dataclass
class ReferenceEntry:
    raw: str
    year: str
    author_blob: str
    surnames: Tuple[str, ...]
    title_hint: str

    @property
    def key(self) -> str:
        return f"{'|'.join(self.surnames)}::{self.year}"

@dataclass
class InTextCitation:
    raw: str
    year: str
    surnames: Tuple[str, ...]
    strict: bool  # strict vs loose capture
    context_hint: str = ""

    @property
    def key(self) -> str:
        return f"{'|'.join(self.surnames)}::{self.year}"


def _parse_reference_entries(entries: List[str]) -> List[ReferenceEntry]:
    out: List[ReferenceEntry] = []
    for e in entries:
        y = _extract_year(e)
        if not y:
            continue
        author_blob = _extract_author_blob(e)
        surn = tuple(_surnames_from_author_blob(author_blob))
        if not surn:
            continue
        title = _title_hint(_extract_title_blob(e))
        out.append(ReferenceEntry(raw=e, year=y, author_blob=author_blob, surnames=surn, title_hint=title))
    return out


# -----------------------------
# Citation extraction (STRICT + LOOSE)
# -----------------------------

def _looks_like_false_author(name_blob: str) -> bool:
    nb = _norm_basic(name_blob).strip()
    if not nb:
        return True

    low = nb.lower().strip(" ,.;:-")
    # narrative prefixes
    for p in sorted(NARRATIVE_PREFIXES, key=len, reverse=True):
        if low.startswith(p + " "):
            return True

    # region/country tokens at start
    low2 = low.strip(",")
    if low2 in REGIONS or low2 in COUNTRY_TOKENS:
        return True

    # single common noun (war, crisis, etc.)
    if low2 in FALSE_AUTHOR_NOUNS:
        return True

    # too long narrative string
    if len(low2.split()) >= 8 and "," in low2 and not any(ch.isdigit() for ch in low2):
        return True

    return False

def _plausible_surname_tokens(tokens: List[str], ref_surname_vocab: Optional[set] = None) -> List[str]:
    """
    Keep surname tokens that look real:
    - appear in reference vocab OR look like name-like tokens
    """
    out = []
    for t in tokens:
        if not t:
            continue
        if t in {"etal"}:
            continue
        if ref_surname_vocab and t in ref_surname_vocab:
            out.append(t)
            continue
        # heuristic: 3+ letters tends to be more surname-like
        if len(t) >= 3 and t.isalpha():
            out.append(t)
    return out

def _extract_intext_strict(full_text: str, ref_surname_vocab: Optional[set]) -> List[InTextCitation]:
    """
    STRICT patterns only:
    - (Surname, 2010)
    - (Surname & Surname, 2010)
    - Surname (2010)
    - (Surname et al., 2010)
    - Multiple citations inside parentheses separated by ; are split.
    """
    text = _norm_basic(full_text)

    # parenthetical groups: ( ... 2010 ... )
    paren_group_re = re.compile(r"\(([^()]{0,220}?\b" + YEAR + r"\b[^()]{0,60}?)\)")
    # narrative: Surname (2010)
    narrative_re = re.compile(r"\b([A-Z][A-Za-z\-\']+(?:\s+(?:&|and)\s+[A-Z][A-Za-z\-\']+|\s+et\s+al\.?)?)\s*\(\s*(" + YEAR + r")\s*\)")

    out: List[InTextCitation] = []

    for m in paren_group_re.finditer(text):
        group = m.group(1)
        # split on ; for multiple citations
        parts = [p.strip() for p in group.split(";") if p.strip()]
        for p in parts:
            y = _extract_year(p)
            if not y:
                continue
            # try to take author portion before year
            pre = p.lower().split(str(y).lower(), 1)[0]
            pre = _norm_basic(pre).strip(" ,.;:-")
            if not pre:
                continue
            if _looks_like_false_author(pre):
                continue
            # remove leading connectors
            pre = re.sub(r"^\s*(?:see|e\.g\.|eg|cf\.?)\s+", "", pre, flags=re.I).strip()
            tokens = _surnames_from_author_blob(pre)
            tokens = _plausible_surname_tokens(tokens, ref_surname_vocab)
            if not tokens:
                continue
            out.append(InTextCitation(raw=_norm_space(p), year=y, surnames=tuple(tokens), strict=True))

    for m in narrative_re.finditer(text):
        author_blob = m.group(1)
        y = m.group(2)
        if _looks_like_false_author(author_blob):
            continue
        tokens = _surnames_from_author_blob(author_blob)
        tokens = _plausible_surname_tokens(tokens, ref_surname_vocab)
        if not tokens:
            continue
        out.append(InTextCitation(raw=_norm_space(m.group(0)), year=y, surnames=tuple(tokens), strict=True))

    return out

def _extract_intext_loose(full_text: str, ref_surname_vocab: set) -> List[InTextCitation]:
    """
    LOOSE patterns to help reconcile references without inflating Missing:
    - "Surname, 2010" (without parentheses)
    - "Surname & Surname, 2010"
    - "Surname et al., 2010"
    But only if surname token exists in reference surname vocab (key commercial trick).
    """
    text = _norm_basic(full_text)

    # This is intentionally conservative: require at least one known reference surname
    # This prevents “Africa, 2019” nonsense if Africa is not a ref surname.
    loose_re = re.compile(
        r"\b([A-Z][A-Za-z\-\']+"
        r"(?:\s+(?:&|and)\s+[A-Z][A-Za-z\-\']+|\s+et\s+al\.?)?)\s*,\s*(" + YEAR + r")\b"
    )

    out: List[InTextCitation] = []
    for m in loose_re.finditer(text):
        author_blob = m.group(1)
        y = m.group(2)

        if _looks_like_false_author(author_blob):
            continue

        tokens = _surnames_from_author_blob(author_blob)
        if not tokens:
            continue

        # Key filter: at least one token must appear in ref vocab
        if not any(t in ref_surname_vocab for t in tokens):
            continue

        out.append(InTextCitation(raw=_norm_space(m.group(0)), year=y, surnames=tuple(tokens), strict=False))
    return out


# -----------------------------
# Matching (exact + weighted fuzzy)
# -----------------------------

def _token_overlap(a: Tuple[str, ...], b: Tuple[str, ...]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))

def _weighted_fuzzy_score(cit: InTextCitation, ref: ReferenceEntry) -> float:
    """
    Weighted score in [0, 100]
    - year match is mandatory for high score
    - author overlap dominates
    - slight bonus if title hint shares words with citation raw (rare, but helps)
    """
    if cit.year.lower() != ref.year.lower():
        # allow letter suffix differences e.g., 2020a vs 2020
        cy = re.sub(r"[a-z]$", "", cit.year.lower())
        ry = re.sub(r"[a-z]$", "", ref.year.lower())
        if cy != ry:
            return 0.0

    overlap = _token_overlap(cit.surnames, ref.surnames)  # 0..1

    # author string fuzz
    a1 = " ".join(cit.surnames)
    a2 = " ".join(ref.surnames)
    if FUZZ_OK:
        f1 = fuzz.token_set_ratio(a1, a2)
    else:
        # small fallback
        f1 = 100.0 * overlap

    # title bonus if citation raw includes a rare title word (light weight)
    bonus = 0.0
    if ref.title_hint:
        words = [w.lower() for w in re.findall(r"[A-Za-z]{5,}", ref.title_hint)]
        raw_low = cit.raw.lower()
        hits = sum(1 for w in words[:6] if w in raw_low)
        bonus = min(6.0, hits * 2.0)

    score = (overlap * 55.0) + (0.40 * float(f1)) + bonus
    if score > 100:
        score = 100.0
    return score

def _build_ref_index(refs: List[ReferenceEntry]) -> Dict[str, List[int]]:
    idx = defaultdict(list)
    for i, r in enumerate(refs):
        idx[r.year.lower()].append(i)
    return idx

def _match_citations_to_refs(
    citations: List[InTextCitation],
    refs: List[ReferenceEntry],
    fuzzy_threshold: float = 82.0,
) -> Tuple[Dict[int, int], Dict[int, List[int]]]:
    """
    Returns:
      - best_match: citation_index -> ref_index
      - ref_to_citations: ref_index -> [citation_indices]
    """
    ref_by_year = _build_ref_index(refs)

    best_match: Dict[int, int] = {}
    ref_to_citations: Dict[int, List[int]] = defaultdict(list)

    # 1) Exact-ish match: token overlap + same year
    for ci, c in enumerate(citations):
        candidates = ref_by_year.get(c.year.lower(), [])
        # also try year without suffix
        if not candidates:
            cys = re.sub(r"[a-z]$", "", c.year.lower())
            candidates = []
            for y, idxs in ref_by_year.items():
                if re.sub(r"[a-z]$", "", y) == cys:
                    candidates.extend(idxs)

        best_r = None
        best_score = -1.0
        for ri in candidates:
            r = refs[ri]
            ov = _token_overlap(c.surnames, r.surnames)
            if ov >= 0.60:  # strong overlap, accept immediately
                best_r = ri
                best_score = 100.0
                break
            # else keep for fuzzy
            sc = _weighted_fuzzy_score(c, r)
            if sc > best_score:
                best_score = sc
                best_r = ri

        if best_r is not None and best_score >= fuzzy_threshold:
            best_match[ci] = best_r
            ref_to_citations[best_r].append(ci)

    return best_match, ref_to_citations


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
    """
    Offline extraction + matching used by /verify (online verification handled elsewhere).
    Returns structure expected by static/app.js:
      summary
      missing_in_references (list dict)
      uncited_references (list str)
      reconciliation_intext_to_reference (list dict)
      reconciliation_reference_to_intext (list dict)
      (ai_assist handled in main.py, not here)
    """

    full_text = _extract_full_text(file_bytes, filename)
    heading, ref_lines = _slice_reference_lines(full_text)
    raw_refs = _split_reference_entries(ref_lines)
    refs = _parse_reference_entries(raw_refs)

    # build reference surname vocab (powerful filter for loose citations)
    ref_surname_vocab = set()
    for r in refs:
        for s in r.surnames:
            if s:
                ref_surname_vocab.add(s)

    strict_cits = _extract_intext_strict(full_text, ref_surname_vocab)
    loose_cits = _extract_intext_loose(full_text, ref_surname_vocab)

    # Deduplicate citations (same key + same raw)
    def _dedupe(cits: List[InTextCitation]) -> List[InTextCitation]:
        seen = set()
        out = []
        for c in cits:
            k = (c.key, c.raw.lower())
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
        return out

    strict_cits = _dedupe(strict_cits)
    loose_cits = _dedupe(loose_cits)

    # IMPORTANT COMMERCIAL RULE:
    # - Missing is computed from STRICT ONLY (prevents missing inflation)
    # - Uncited is reduced using STRICT + LOOSE (max matching power)
    strict_match, strict_ref_to_cits = _match_citations_to_refs(strict_cits, refs, fuzzy_threshold=82.0)
    all_cits = strict_cits + [c for c in loose_cits if c.raw.lower() not in {x.raw.lower() for x in strict_cits}]
    all_match, all_ref_to_cits = _match_citations_to_refs(all_cits, refs, fuzzy_threshold=80.0)

    # Build “Missing in references” list from STRICT citations not matched to any reference
    missing_counter = Counter()
    for i, c in enumerate(strict_cits):
        if i not in strict_match:
            # display in concise form "Surname et al., YEAR" like your UI
            disp = _norm_space(re.sub(r"\s*\(\s*", ", ", c.raw).replace(")", ""))
            missing_counter[disp] += 1

    missing_in_refs_rows = [
        {"citation_in_text": k, "count_in_text": v}
        for k, v in missing_counter.most_common()
    ]

    # Build “Uncited references” list: refs that never matched any STRICT+LOOSE citation
    uncited_refs: List[str] = []
    for ri, r in enumerate(refs):
        if ri not in all_ref_to_cits:
            # must be a string for app.js (prevents [object Object])
            uncited_refs.append(r.raw)

    # Build reconciliation tables
    recon_c2r = []
    for i, c in enumerate(strict_cits):
        if i in strict_match:
            r = refs[strict_match[i]]
            recon_c2r.append({
                "citation_in_text": c.raw,
                "matched_reference": r.raw,
                "status": "matched",
            })
        else:
            recon_c2r.append({
                "citation_in_text": c.raw,
                "matched_reference": "",
                "status": "missing_in_references",
            })

    recon_r2c = []
    for ri, r in enumerate(refs):
        if ri in all_ref_to_cits:
            # show first matched citation (any)
            ci = all_ref_to_cits[ri][0]
            recon_r2c.append({
                "reference": r.raw,
                "matched_citation_in_text": all_cits[ci].raw,
                "status": "matched",
            })
        else:
            recon_r2c.append({
                "reference": r.raw,
                "matched_citation_in_text": "",
                "status": "uncited_reference",
            })

    # summary
    in_text_found = len(strict_cits) + len(loose_cits)  # informational
    ref_found = len(refs)
    missing_count = len(missing_in_refs_rows)
    uncited_count = len(uncited_refs)
    matched_refs = ref_found - uncited_count
    match_rate = (matched_refs / ref_found) if ref_found else 0.0

    summary = {
        "in_text_citations_found": in_text_found,
        "reference_entries_found": ref_found,
        "missing_in_references": missing_count,
        "uncited_references": uncited_count,
        "match_rate": round(match_rate * 100.0, 1),

        # diagnostics that help you tune without guessing
        "strict_intext_count": len(strict_cits),
        "loose_intext_count": len(loose_cits),
        "references_heading": heading or "",
    }

    return {
        "summary": summary,
        "missing_in_references": missing_in_refs_rows,
        "uncited_references": uncited_refs,
        "reconciliation_intext_to_reference": recon_c2r,
        "reconciliation_reference_to_intext": recon_r2c,
        "online_verification": [],  # filled by verify.py pipeline in your stack
    }
