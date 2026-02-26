# engine.py
"""
Citation Crosschecker - commercial-grade engine

Goals
- High matching power with guardrails against obvious false positives
- Stable output schema for main.py + static/app.js
- Import-safe: never throws at import time on Render
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict, Counter

# Optional deps
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
    from rapidfuzz import fuzz  # type: ignore
    RAPIDFUZZ_OK = True
except Exception:
    fuzz = None
    RAPIDFUZZ_OK = False


# ----------------------------
# Regex + constants
# ----------------------------

YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b", re.I)

# Headings that often start the reference list
REF_HEADINGS = [
    r"^\s*references?\s*(?:list)?\s*$",
    r"^\s*bibliograph(?:y|ies)\s*$",
    r"^\s*works\s+cited\s*$",
    r"^\s*literature\s+cited\s*$",
]

REF_HEADING_RE = re.compile("|".join(REF_HEADINGS), re.I | re.M)

# Many theses include appendix headings after references, we use them to stop reference parsing.
AFTER_REFS_HEADINGS = [
    r"^\s*appendix(?:es)?\s*$",
    r"^\s*annex(?:es)?\s*$",
    r"^\s*supporting\s+information\s*$",
    r"^\s*supplementary\s+material(?:s)?\s*$",
]
AFTER_REFS_RE = re.compile("|".join(AFTER_REFS_HEADINGS), re.I | re.M)

# Discourse prefixes that frequently pollute citation extraction.
# We handle case-insensitively and allow them to appear before citations.
DISCOURSE_PREFIXES = {
    "for instance", "for example", "e.g.", "eg", "i.e.", "ie", "see", "see also",
    "according to", "as noted by", "as shown by", "as argued by", "as reported by",
    "as stated by", "as observed by", "as discussed in", "as discussed by",
    "in line with", "in line", "in", "notably", "noted", "noting", "moreover",
    "however", "therefore", "thus", "hence", "overall", "first", "second", "third",
}

# Geography + common nouns that should not be treated as author surnames in narrative commas
GEO_STOP = {
    "africa", "europe", "asia", "america", "north", "south", "west", "east", "middle",
    "ghana", "nigeria", "kenya", "china", "india", "turkey", "malaysia", "romania",
    "wuhan", "pakistan", "usa", "uk", "u.k", "united", "states", "japan", "germany",
    "france", "spain", "italy", "britain", "england", "scotland", "ireland", "wales",
}

# Patterns for in-text citations (APA/Harvard-ish)
# Strict: citations enclosed in parentheses or narrative "Author (Year)"
STRICT_PAREN_CIT_RE = re.compile(
    rf"""\(([^()]*?\b{YEAR}\b[^()]*)\)""",
    re.I,
)

NARRATIVE_PAREN_YEAR_RE = re.compile(
    rf"""\b([A-Z][A-Za-z'’\-]+)(?:\s+(?:and|&)\s+([A-Z][A-Za-z'’\-]+)|\s+et\s+al\.)?\s*\(\s*({YEAR})\s*\)""",
    re.I,
)

# Loose: "Author, 1998" style
NARRATIVE_COMMA_YEAR_RE = re.compile(
    rf"""\b([A-Z][A-Za-z'’\-]{{2,}})\s*,\s*(?:[A-Z]\.\s*,\s*)?({YEAR})\b""",
    re.I,
)

# Identify TOC lines like: "Chapter 2 .... 15"
TOC_LINE_RE = re.compile(r"\.{3,}\s*\d+\s*$")
TOC_HEADER_RE = re.compile(r"^\s*(table\s+of\s+contents|contents)\s*$", re.I)


# ----------------------------
# Small helpers
# ----------------------------

def _norm(s: str) -> str:
    s = s or ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _lower_ascii(s: str) -> str:
    s = _norm(s).lower()
    s = s.replace("’", "'")
    return s

def _strip_leading_prefix(text: str) -> str:
    t = _lower_ascii(text)
    for p in sorted(DISCOURSE_PREFIXES, key=len, reverse=True):
        if t.startswith(p + " "):
            return _norm(text[len(p):])
    return text

def _is_plausible_author_token(tok: str) -> bool:
    t = _lower_ascii(tok)
    if not t or len(t) < 3:
        return False
    if t in GEO_STOP:
        return False
    # avoid "crisis, 2015" "war, 2003" style
    if t in {"crisis", "war", "revolution", "coup", "estimates", "prices"}:
        return False
    # avoid pure numbers
    if t.isdigit():
        return False
    # start with letter
    if not re.match(r"^[a-z]", t):
        return False
    return True

def _token_set_ratio(a: str, b: str) -> float:
    if not RAPIDFUZZ_OK:
        return 0.0
    return float(fuzz.token_set_ratio(a, b))

def _partial_ratio(a: str, b: str) -> float:
    if not RAPIDFUZZ_OK:
        return 0.0
    return float(fuzz.partial_ratio(a, b))

def _extract_year(s: str) -> Optional[str]:
    m = YEAR_RE.search(s or "")
    return m.group(1) if m else None

def _canon_year(y: Optional[str]) -> Optional[str]:
    if not y:
        return None
    y = y.strip()
    if len(y) >= 4:
        return y[:4] + (y[4:] if len(y) > 4 else "")
    return y

def _surname_only(author_chunk: str) -> str:
    # Take first token before comma/space, but keep hyphenated.
    a = _norm(author_chunk)
    a = re.split(r"[,\s]+", a, maxsplit=1)[0]
    return a

def _ref_key(first_author_surname: str, year: str) -> str:
    return f"{_lower_ascii(first_author_surname)}|{_lower_ascii(year)}"


# ----------------------------
# Reference parsing
# ----------------------------

@dataclass
class ReferenceEntry:
    raw: str
    year: Optional[str]
    first_author: str
    authors_str: str
    key: Optional[str]

def _split_reference_lines(block: str) -> List[str]:
    """
    Heuristic splitter for reference list block:
    - new entry starts at:
      * leading number + '.' or ')'
      * leading bracketed number [12]
      * or line starts with surname, initials and year later
    Continuation lines are appended.
    """
    lines = [_norm(x) for x in (block or "").splitlines()]
    lines = [x for x in lines if x]

    out: List[str] = []
    cur: List[str] = []

    start_re = re.compile(r"^(?:\[\d+\]|\(?\d+\)?[.)])\s+")
    for ln in lines:
        if start_re.match(ln):
            if cur:
                out.append(_norm(" ".join(cur)))
            cur = [start_re.sub("", ln)]
        else:
            # also treat as new if it looks like a fresh surname + initials and cur is "long enough"
            if cur and re.match(r"^[A-Z][A-Za-z'’\-]+,\s*[A-Z]", ln):
                out.append(_norm(" ".join(cur)))
                cur = [ln]
            else:
                cur.append(ln)

    if cur:
        out.append(_norm(" ".join(cur)))

    # de-dup exact duplicates
    dedup = []
    seen = set()
    for r in out:
        k = _lower_ascii(r)
        if k not in seen:
            seen.add(k)
            dedup.append(r)
    return dedup

def _parse_reference_entry(raw: str) -> ReferenceEntry:
    r = _norm(raw)
    year = _extract_year(r)
    year = _canon_year(year)
    # authors segment: up to first period
    authors_part = r.split(".", 1)[0]
    # if no period, use up to year
    if year and year in r:
        pre = r.split(year, 1)[0]
        if len(pre) > 6:
            authors_part = pre
    first_author = _surname_only(authors_part)
    authors_str = _norm(authors_part)
    key = _ref_key(first_author, year) if (first_author and year) else None
    return ReferenceEntry(raw=r, year=year, first_author=first_author, authors_str=authors_str, key=key)


# ----------------------------
# Text extraction
# ----------------------------

def _extract_text_docx(file_bytes: bytes) -> str:
    if not DOCX_OK or Document is None:
        raise RuntimeError("python-docx not available")
    doc = Document(io.BytesIO(file_bytes))
    parts = []
    for p in doc.paragraphs:
        txt = p.text or ""
        if txt:
            parts.append(txt)
    return "\n".join(parts)

def _extract_text_pdf(file_bytes: bytes) -> str:
    if not PDF_OK or pdfplumber is None:
        raise RuntimeError("pdfplumber not available")
    text_parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t:
                text_parts.append(t)
    return "\n".join(text_parts)

def extract_text(file_bytes: bytes, filename: str) -> str:
    fn = (filename or "").lower()
    if fn.endswith(".docx"):
        return _extract_text_docx(file_bytes)
    if fn.endswith(".pdf"):
        return _extract_text_pdf(file_bytes)
    # fallback: try docx then pdf
    if DOCX_OK:
        try:
            return _extract_text_docx(file_bytes)
        except Exception:
            pass
    if PDF_OK:
        try:
            return _extract_text_pdf(file_bytes)
        except Exception:
            pass
    return ""


def _strip_table_of_contents(text: str) -> str:
    """
    Removes TOC blocks to avoid false citations, common in theses.
    """
    lines = (text or "").splitlines()
    out = []
    in_toc = False
    toc_hits = 0
    for ln in lines:
        lns = _norm(ln)
        if not lns:
            if in_toc and toc_hits >= 3:
                # often TOC ends after a blank line
                in_toc = False
            out.append(ln)
            continue

        if TOC_HEADER_RE.match(lns):
            in_toc = True
            toc_hits = 0
            continue

        if in_toc:
            if TOC_LINE_RE.search(lns):
                toc_hits += 1
                continue
            # stop TOC if we no longer see dot leaders for a while
            if toc_hits >= 3 and not TOC_LINE_RE.search(lns):
                in_toc = False
                out.append(ln)
            # else keep skipping
            continue

        out.append(ln)

    return "\n".join(out)


# ----------------------------
# Reference section detection
# ----------------------------

def split_body_and_references(text: str) -> Tuple[str, str, str]:
    """
    Returns (body_text, references_block, ref_heading_found)
    """
    t = text or ""
    m = REF_HEADING_RE.search(t)
    if not m:
        return (t, "", "")
    ref_start = m.end()
    heading = _norm(m.group(0))
    rest = t[ref_start:]
    # stop at appendix/supplementary heading if present
    m2 = AFTER_REFS_RE.search(rest)
    if m2:
        refs = rest[: m2.start()]
    else:
        refs = rest
    body = t[: m.start()]
    return (body, refs, heading)


# ----------------------------
# Citation extraction
# ----------------------------

@dataclass
class InTextCitation:
    raw: str
    year: Optional[str]
    authors: str       # "Adam & Tweneboah" or "Adam et al."
    first_author: str
    key: Optional[str]
    mode: str          # strict|loose
    count: int = 1

def _parse_parenthetical_chunk(chunk: str) -> List[InTextCitation]:
    """
    Parses inside "( ... )" into 1+ citations split by ';'
    Example: "(Adam & Tweneboah, 2008; Kalam, 2020)"
    """
    chunk = _norm(chunk)
    chunk = re.sub(r"^\s*(?:cf\.|see|see also)\s+", "", chunk, flags=re.I)
    chunk = _strip_leading_prefix(chunk)

    pieces = [p.strip() for p in re.split(r"\s*;\s*", chunk) if p.strip()]
    out: List[InTextCitation] = []

    for p in pieces:
        # handle multiple years for same author: "Adam, 2008, 2010"
        yrs = YEAR_RE.findall(p)
        if not yrs:
            continue
        y = _canon_year(yrs[-1])
        # author part: remove years + punctuation
        author_part = YEAR_RE.sub("", p)
        author_part = re.sub(r"[(),;]", " ", author_part)
        author_part = _norm(author_part)
        if not author_part:
            continue

        # compress connectors
        author_part = re.sub(r"\band\b", "&", author_part, flags=re.I)
        # first author is first token
        first = _surname_only(author_part)
        if not _is_plausible_author_token(first):
            continue
        key = _ref_key(first, y) if y else None
        out.append(InTextCitation(raw=_norm(p), year=y, authors=author_part, first_author=first, key=key, mode="strict"))
    return out

def extract_intext_citations(body_text: str) -> Tuple[List[InTextCitation], List[InTextCitation]]:
    """
    Returns (strict, loose)
    """
    body = _strip_table_of_contents(body_text or "")
    strict: List[InTextCitation] = []
    loose: List[InTextCitation] = []

    # Strict parenthetical citations
    for m in STRICT_PAREN_CIT_RE.finditer(body):
        inside = m.group(1) or ""
        strict.extend(_parse_parenthetical_chunk(inside))

    # Strict narrative "Author (Year)" / "Author & Coauthor (Year)" / "Author et al. (Year)"
    for m in NARRATIVE_PAREN_YEAR_RE.finditer(body):
        a1 = _norm(m.group(1) or "")
        a2 = _norm(m.group(2) or "")
        y = _canon_year(m.group(3) or "")
        if not y:
            continue
        if not _is_plausible_author_token(a1):
            continue
        authors = a1
        if a2:
            authors = f"{a1} & {a2}"
        else:
            # detect et al.
            tail = body[m.start(): m.end()]
            if re.search(r"et\s+al\.", tail, re.I):
                authors = f"{a1} et al."
        key = _ref_key(a1, y)
        strict.append(InTextCitation(raw=_norm(m.group(0)), year=y, authors=authors, first_author=a1, key=key, mode="strict"))

    # Loose narrative "Author, 1998" (important for styles like: "Newman, I., 1998.")
    for m in NARRATIVE_COMMA_YEAR_RE.finditer(body):
        a1 = _norm(m.group(1) or "")
        y = _canon_year(m.group(2) or "")
        if not a1 or not y:
            continue
        if not _is_plausible_author_token(a1):
            continue
        # avoid cases where preceding word is a geography/cue, e.g., "Africa, 2019"
        prev = body[max(0, m.start()-20): m.start()]
        if re.search(r"\b(" + "|".join(re.escape(x) for x in GEO_STOP) + r")\s*,\s*$", _lower_ascii(prev)):
            continue
        key = _ref_key(a1, y)
        loose.append(InTextCitation(raw=_norm(m.group(0)), year=y, authors=a1, first_author=a1, key=key, mode="loose"))

    # Aggregate counts
    def _collapse(items: List[InTextCitation]) -> List[InTextCitation]:
        bucket: Dict[Tuple[str, str], InTextCitation] = {}
        for it in items:
            k = (it.key or "", it.authors)
            if k in bucket:
                bucket[k].count += 1
            else:
                bucket[k] = it
        return list(bucket.values())

    return (_collapse(strict), _collapse(loose))


# ----------------------------
# Matching logic (weighted fuzzy)
# ----------------------------

@dataclass
class Match:
    citation: InTextCitation
    ref: ReferenceEntry
    score: float
    method: str

def _weighted_score(cit: InTextCitation, ref: ReferenceEntry) -> float:
    """
    Weighted fuzzy scoring.
    Commercial goal: high recall, but still year-gated to reduce nonsense matches.
    """
    if not ref.year or not cit.year:
        return 0.0

    # Hard year gate: allow same year or same year with suffix a/b
    cy = cit.year[:4]
    ry = ref.year[:4]
    if cy != ry:
        return 0.0

    a = _lower_ascii(cit.authors)
    b = _lower_ascii(ref.authors_str)

    # Author similarity
    ts = _token_set_ratio(a, b)
    pr = _partial_ratio(a, b)
    auth_sim = max(ts, pr)

    # Bonus for first-author exact match
    fa = 0.0
    if _lower_ascii(cit.first_author) == _lower_ascii(ref.first_author):
        fa = 10.0

    score = 0.75 * auth_sim + 0.25 * min(100.0, auth_sim + fa)
    return float(score)

def match_citations_to_references(
    strict: List[InTextCitation],
    loose: List[InTextCitation],
    refs: List[ReferenceEntry],
    mode: str = "commercial",
) -> Tuple[List[Match], List[Dict[str, Any]], List[str]]:
    """
    Returns (matches, missing_in_references, uncited_references_text)
    """
    # Index references by key
    by_key: Dict[str, List[ReferenceEntry]] = defaultdict(list)
    for r in refs:
        if r.key:
            by_key[r.key].append(r)

    matches: List[Match] = []
    matched_ref_keys: set = set()

    def _try_match_one(cit: InTextCitation) -> Optional[Match]:
        if cit.key and cit.key in by_key:
            # choose best among same key (duplicate refs)
            candidates = by_key[cit.key]
            best = max(candidates, key=lambda rr: len(rr.raw))
            return Match(citation=cit, ref=best, score=100.0, method="exact_key")

        # fuzzy fallback within same year
        if not cit.year:
            return None
        year4 = cit.year[:4]
        year_candidates = [r for r in refs if (r.year or "").startswith(year4)]
        if not year_candidates:
            return None

        best_m: Optional[Match] = None
        for r in year_candidates:
            sc = _weighted_score(cit, r)
            if sc <= 0:
                continue
            if (best_m is None) or (sc > best_m.score):
                best_m = Match(citation=cit, ref=r, score=sc, method="fuzzy_year_gate")

        if best_m is None:
            return None

        # Tuned thresholds:
        # strict citations can accept lower threshold than loose
        thr = 78.0 if cit.mode == "strict" else 86.0
        if best_m.score >= thr:
            return best_m
        return None

    # Match strict first, then loose
    for cit in strict + loose:
        m = _try_match_one(cit)
        if m:
            matches.append(m)
            if m.ref.key:
                matched_ref_keys.add(m.ref.key)

    # Missing: only strict citations should contribute (academic accuracy)
    missing_counter: Counter = Counter()
    for cit in strict:
        ok = False
        if cit.key and cit.key in by_key:
            ok = True
        else:
            # check if matched by fuzzy
            for m in matches:
                if m.citation.raw == cit.raw and m.citation.mode == cit.mode:
                    ok = True
                    break
        if not ok:
            label = f"{cit.first_author}{' et al.' if 'et al' in cit.authors.lower() else ''}, {cit.year or ''}".strip().strip(",")
            missing_counter[label] += cit.count

    missing = [{"citation_in_text": k, "count_in_text": v} for k, v in missing_counter.most_common()]

    # Uncited references (strict): ref has no strict match
    strict_matched_keys = set()
    for m in matches:
        if m.citation.mode == "strict" and m.ref.key:
            strict_matched_keys.add(m.ref.key)

    uncited = []
    for r in refs:
        if r.key and r.key not in strict_matched_keys:
            uncited.append(r.raw)

    # De-dup uncited by normalized raw
    dedup_uncited = []
    seen = set()
    for u in uncited:
        k = _lower_ascii(u)
        if k not in seen:
            seen.add(k)
            dedup_uncited.append(u)

    return matches, missing, dedup_uncited


# ----------------------------
# Public API
# ----------------------------

def run_crosscheck(
    file_bytes: bytes,
    filename: str,
    style: str = "apa",
    verify_online: bool = False,
    verify_mode: str = "all",
    max_verify: int = 0,
    throttle_s: float = 0.25,
    use_crossref: bool = True,
    use_openalex: bool = True,
    use_semanticscholar: bool = False,
    ai_assist: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Main entrypoint called by main.py.

    Important: keep this signature stable. Accept **kwargs to avoid future breakage.
    """
    text = extract_text(file_bytes, filename)
    text = _strip_table_of_contents(text)

    body, ref_block, ref_heading = split_body_and_references(text)

    references: List[ReferenceEntry] = []
    ref_entries = _split_reference_lines(ref_block) if ref_block else []
    for r in ref_entries:
        references.append(_parse_reference_entry(r))

    strict, loose = extract_intext_citations(body)

    matches, missing, uncited = match_citations_to_references(strict, loose, references, mode="commercial")

    # Build intext -> reference mapping for UI
    intext_to_ref = []
    for m in matches:
        intext_to_ref.append(
            {
                "citation_in_text": m.citation.raw,
                "citation_norm": f"{m.citation.first_author}, {m.citation.year}",
                "reference": m.ref.raw,
                "score": round(m.score, 1),
                "method": m.method,
                "mode": m.citation.mode,
                "count_in_text": m.citation.count,
            }
        )

    # reference -> intext mapping (one-to-many)
    ref_to_intext_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for m in matches:
        ref_to_intext_map[m.ref.raw].append(
            {
                "citation_in_text": m.citation.raw,
                "score": round(m.score, 1),
                "method": m.method,
                "mode": m.citation.mode,
                "count_in_text": m.citation.count,
            }
        )

    ref_to_intext = []
    for r in references:
        ref_to_intext.append(
            {
                "reference": r.raw,
                "cited_in_text": r.raw in ref_to_intext_map,
                "citations": ref_to_intext_map.get(r.raw, []),
            }
        )

    # Summary counts (strict+loose shown for transparency)
    strict_count = sum(c.count for c in strict)
    loose_count = sum(c.count for c in loose)
    total_intext = strict_count + loose_count
    ref_count = len(references)

    # Match rate based on strict citations (academic)
    matched_strict = set()
    for m in matches:
        if m.citation.mode == "strict" and m.citation.key:
            matched_strict.add(m.citation.key)
    strict_unique = {c.key for c in strict if c.key}
    match_rate = (len(matched_strict) / max(1, len(strict_unique))) * 100.0

    result: Dict[str, Any] = {
        "style": style,
        "intext_citations_found": int(total_intext),
        "reference_entries_found": int(ref_count),
        "missing_in_references": missing,
        "uncited_references": uncited,  # MUST be list[str] for app.js compatibility
        "match_rate": round(match_rate, 1),
        "ref_heading_found": ref_heading,
        # transparency / commercial-grade debugging
        "strict_intext_count": int(strict_count),
        "loose_intext_count": int(loose_count),
        "matches_count": int(len(matches)),
        # tables
        "intext_to_reference": intext_to_ref,
        "reference_to_intext": ref_to_intext,
        # online verification passthrough flags (actual verification handled elsewhere)
        "verify": {
            "requested": bool(verify_online),
            "mode": verify_mode,
            "max_verify": int(max_verify) if max_verify is not None else 0,
            "throttle_s": float(throttle_s) if throttle_s is not None else 0.25,
            "use_crossref": bool(use_crossref),
            "use_openalex": bool(use_openalex),
            "use_semanticscholar": bool(use_semanticscholar),
            "ai_assist": bool(ai_assist),
            "ai_added": 0,
            "ai_status": "skipped" if ai_assist else "disabled",
        },
    }
    return result
