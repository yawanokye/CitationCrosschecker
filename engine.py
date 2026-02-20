# engine.py
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
    "similarly", "however", "moreover", "likewise", "further", "also",
    "thus", "therefore", "in particular", "in response", "in addition",
    "for example", "for instance", "recently", "specifically"
}

LEAD_WORDS = {
    "see", "cf", "e.g", "i.e", "according to", "by", "from", "in", "as",
    "for example", "for instance"
}

GEO_PREFIXES = {
    "africa", "asia", "europe", "america", "latin america", "sub-saharan africa",
    "ghana", "nigeria", "kenya", "south africa", "usa", "uk", "china", "india"
}

COMMON_NONAUTHOR = {
    "war", "crisis", "revolution", "scandal", "attacks", "volatility", "model",
    "countries", "coefficients", "estimates", "computation", "instance"
}

ORG_ALIASES = {
    "who": ["who", "world health organization", "world health organisation"],
    "un": ["un", "united nations", "u.n.", "united nations organisation", "united nations organization"],
    "oecd": ["oecd", "organisation for economic co-operation and development", "organization for economic cooperation and development"],
    "imf": ["imf", "international monetary fund"],
    "world bank": ["world bank", "international bank for reconstruction and development", "ibrd"],
}
ORG_ACRONYMS = {k.upper() for k in ["WHO", "UN", "OECD", "IMF", "WORLD BANK", "IBRD"]}

_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


@dataclass
class InTextCitation:
    style: str
    raw: str
    key: str
    year: Optional[str] = None
    surnames: Optional[Tuple[str, ...]] = None
    number: Optional[int] = None
    flags: Optional[str] = None


@dataclass
class ReferenceEntry:
    raw: str
    key: str
    year: Optional[str] = None
    surnames: Optional[Tuple[str, ...]] = None
    number: Optional[int] = None


def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def norm_token(s: str) -> str:
    s = (s or "").replace("’", "'")
    s = _strip_accents(s)
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\-\s'&\.]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def canon_org(name: str) -> str:
    n = norm_token(name)
    for canon, variants in ORG_ALIASES.items():
        for v in variants:
            if n == norm_token(v):
                return canon
    return n


def is_known_org(text: str) -> bool:
    t = (text or "").strip()
    if t.upper() in ORG_ACRONYMS:
        return True
    c = canon_org(t)
    return c in ORG_ALIASES.keys()


def key_author_year(first_surname: str, year: str) -> str:
    return f"au_{norm_token(first_surname)}_{(year or '').lower()}"


def key_numeric(n: int) -> str:
    return f"n_{int(n)}"


def clean_surname(tok: str) -> str:
    t = (tok or "").strip()
    t = t.replace("&", " ")
    t = re.sub(r"\bet\s+al\.?\b", "", t, flags=re.I)
    t = re.sub(r"[^A-Za-z\-'\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    return t.split()[-1].strip()


def extract_surnames_from_blob(auth_blob: str) -> List[str]:
    b = (auth_blob or "").strip()
    b = b.replace("&", " and ")
    b = re.sub(r"\s+", " ", b).strip()

    parts = [p.strip() for p in re.split(r",|\band\b", b, flags=re.I) if p.strip()]

    surnames = []
    for p in parts:
        s = clean_surname(p)
        if s:
            surnames.append(s)

    out = []
    seen = set()
    for s in surnames:
        sn = norm_token(s)
        if sn and sn not in seen:
            out.append(s)
            seen.add(sn)
    return out


def _plausible_author_blob(blob: str) -> bool:
    b = norm_space(blob)
    if not b:
        return False

    bn = norm_token(b)

    if bn in {"al", "et", "et al"}:
        return False

    if len(b) > 65:
        return False

    toks = [t for t in re.split(r"\s+", bn) if t]
    if not toks:
        return False

    if len(toks) == 1 and toks[0] in COMMON_NONAUTHOR:
        return False

    if not any(re.fullmatch(r"[a-z][a-z\-']{1,}", t) for t in toks):
        return False

    return True


def _scrub_leading_prefixes(author_blob: str) -> Tuple[str, List[str]]:
    flags = []
    s = norm_space(author_blob).strip(" ,")
    if not s:
        return "", flags

    while True:
        m = re.match(r"^([A-Za-z][A-Za-z\s\-']+)\s*,\s*(.+)$", s)
        if not m:
            break
        left = norm_token(m.group(1))
        rest = m.group(2).strip()
        if left in DISCOURSE_PREFIXES or left in GEO_PREFIXES or left in COMMON_NONAUTHOR:
            flags.append(f"prefix_removed:{left}")
            s = rest
            continue
        break

    for lw in sorted(LEAD_WORDS, key=len, reverse=True):
        if norm_token(s).startswith(lw + " "):
            flags.append(f"lead_word_removed:{lw}")
            s = s[len(lw):].strip(" ,")
            break

    return s, flags


# -----------------------------
# File readers
# -----------------------------
def read_docx_paragraphs(file_bytes: bytes) -> List[str]:
    if not DOCX_OK:
        raise RuntimeError("python-docx not installed")
    doc = Document(io.BytesIO(file_bytes))
    return [norm_space(p.text) for p in doc.paragraphs if norm_space(p.text)]


def read_pdf_text(file_bytes: bytes) -> str:
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")
    out = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for p in pdf.pages:
            out.append(p.extract_text() or "")
    return "\n".join(out)


# -----------------------------
# Reference extraction
# -----------------------------
def _find_reference_heading(lines: List[str]) -> int:
    for i, line in enumerate(lines):
        s = (line or "").strip()
        if not s:
            continue
        for pat in REF_HEADINGS:
            if re.search(pat, s, flags=re.I):
                return i
    return -1


def _looks_like_new_apa_reference_start(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    if not re.match(r"^[A-Z][A-Za-z\-']+(?:\s+(?:Jr|Sr|II|III|IV))?(?:,|\s)", s):
        return False
    if re.search(rf"\(\s*{YEAR}\s*\)", s):
        return True
    if YEAR_RE.search(s[:90]):
        return True
    return False


def _looks_like_new_numeric_reference_start(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return bool(re.match(r"^\s*\[\d+\]\s+", s)) or bool(re.match(r"^\s*\d{1,4}[\.\)]\s+", s))


def _merge_reference_lines(raw_lines: List[str]) -> List[str]:
    raw_lines = [ln.strip() for ln in raw_lines if ln and ln.strip()]
    if not raw_lines:
        return []

    merged: List[str] = []
    buf = ""

    for ln in raw_lines:
        s = ln.strip()

        is_doi_line = bool(re.match(r"^\s*(doi\s*:\s*)?10\.\d{4,9}/", s, flags=re.I)) or \
                      bool(re.match(r"^\s*https?://doi\.org/10\.\d{4,9}/", s, flags=re.I)) or \
                      bool(re.match(r"^\s*10\.\d{4,9}/", s, flags=re.I))

        apa_start = _looks_like_new_apa_reference_start(s)
        numeric_start = _looks_like_new_numeric_reference_start(s)

        if is_doi_line:
            buf = (buf + " " + s).strip() if buf else s
            continue

        if apa_start or numeric_start:
            if buf:
                merged.append(buf.strip())
            buf = s
        else:
            buf = (buf + " " + s).strip() if buf else s

    if buf:
        merged.append(buf.strip())

    return [m for m in merged if len(m) >= 10]


# -----------------------------
# Reference parsers
# -----------------------------
def parse_reference_author_year(ref_raw: str) -> Optional[ReferenceEntry]:
    r = (ref_raw or "").strip()
    if not r:
        return None

    m = re.search(rf"\(\s*({YEAR})\s*\)", r, flags=re.I)
    if m:
        year = m.group(1)
        pre = r[: m.start()].strip()
    else:
        m2 = YEAR_RE.search(r)
        if not m2:
            return None
        year = m2.group(1)
        pre = r[: m2.start()].strip()

    pre = re.sub(r"^\s*(\[\s*\d+\s*\]|\d+\s*[\.\)])\s*", "", pre).strip()

    if is_known_org(pre) or pre.upper() in ORG_ACRONYMS:
        k = f"org_{canon_org(pre)}_{year.lower()}"
        return ReferenceEntry(raw=r, key=k, year=year, surnames=(pre,), number=None)

    first = pre.split(",")[0].strip() if "," in pre else (pre.split()[0].strip() if pre.split() else "")
    if not first:
        return None
    return ReferenceEntry(raw=r, key=key_author_year(first, year), year=year, surnames=(first,), number=None)


def parse_reference_numeric(ref_raw: str) -> Optional[ReferenceEntry]:
    r = (ref_raw or "").strip()
    if not r:
        return None

    m = re.match(r"^\s*\[\s*(\d+)\s*\]\s*(.+)$", r)
    if m:
        n = int(m.group(1))
        return ReferenceEntry(raw=r, key=key_numeric(n), number=n)

    m = re.match(r"^\s*(\d+)\s*[\.\)]\s*(.+)$", r)
    if m:
        n = int(m.group(1))
        return ReferenceEntry(raw=r, key=key_numeric(n), number=n)

    m = re.match(r"^\s*(\d+)\s+(.+)$", r)
    if m:
        n = int(m.group(1))
        return ReferenceEntry(raw=r, key=key_numeric(n), number=n)

    return None


# -----------------------------
# In-text extraction (APA/Harvard)
# -----------------------------
def _split_parenthetical_group(inside: str) -> List[str]:
    parts = [p.strip() for p in (inside or "").split(";")]
    return [p for p in parts if p and YEAR_RE.search(p)]


def _parse_one_author_year_piece(piece: str) -> Optional[Tuple[str, str, Tuple[str, ...], str]]:
    seg = norm_space(piece).strip()
    if not seg:
        return None

    y_m = YEAR_RE.search(seg)
    if not y_m:
        return None
    y = y_m.group(1)

    left = seg[:y_m.start()].strip().rstrip(",").strip()
    if not left:
        return None

    lead_flag = ""
    for w in sorted(LEAD_WORDS, key=len, reverse=True):
        if norm_token(left).startswith(w + " "):
            lead_flag = w
            left = left[len(w):].strip(" ,")
            break

    left2, scrub_flags = _scrub_leading_prefixes(left)
    left2 = left2.strip()

    if not _plausible_author_blob(left2):
        return None

    flags = []
    if lead_flag:
        flags.append(f"lead_word:{lead_flag}")
    flags.extend(scrub_flags)

    if is_known_org(left2):
        return (seg, y, (left2,), ";".join(flags))

    if re.search(r"\bet\s+al\.?\b", left2, flags=re.I):
        first = clean_surname(left2)
        if not first:
            return None
        return (seg, y, (first,), ";".join(flags) + (";etal" if flags else "etal"))

    surnames = extract_surnames_from_blob(left2)
    if not surnames:
        return None

    return (seg, y, tuple(surnames), ";".join(flags))


def extract_author_year_citations(text: str) -> List[InTextCitation]:
    txt = text or ""
    out: List[InTextCitation] = []
    seen = set()

    par_pat = re.compile(rf"\(([^()]*\b{YEAR}\b[^()]*)\)", flags=re.I)

    for m in par_pat.finditer(txt):
        inside = m.group(1).strip()
        pieces = _split_parenthetical_group(inside) or [inside]

        for piece in pieces:
            parsed = _parse_one_author_year_piece(piece)
            if not parsed:
                continue
            seg, y, surnames, flags = parsed

            if is_known_org(surnames[0]):
                k = f"org_{canon_org(surnames[0])}_{y.lower()}"
            else:
                k = key_author_year(surnames[0], y)

            raw = f"({seg})"
            if raw in seen:
                continue
            out.append(InTextCitation("author-year", raw, k, year=y, surnames=surnames, flags=flags))
            seen.add(raw)

    # UPDATED: narrative pattern now includes "Surname et al. (2020)" variants
    narr_pat = re.compile(
        rf"""
        (?P<lead>\b(?:according\s+to|see|by|from|in|as|for\s+example|for\s+instance|cf)\b\s+)?   # lead word
        (?P<authors>
            (?:[A-Z][A-Za-z\-']+(?:'s)?\s+et\.?\s+al\.?)                                         # Surname et al.
            |
            (?:[A-Z][A-Za-z\-']+(?:'s)?)                                                         # first token
            (?:\s*,\s*[A-Z][A-Za-z\-']+(?:'s)?)*                                                 # more tokens via comma
            (?:\s*,?\s*(?:and|&)\s*[A-Z][A-Za-z\-']+(?:'s)?)*                                    # final and/&
        )
        \s*\(\s*(?P<year>{YEAR})\s*\)
        """,
        flags=re.VERBOSE | re.I,
    )

    for m in narr_pat.finditer(txt):
        lead = (m.group("lead") or "").strip()
        authors_blob = (m.group("authors") or "").strip()
        y = m.group("year")

        authors_blob_clean = re.sub(r"\'s\b", "", authors_blob, flags=re.I).strip()
        cleaned, scrub_flags = _scrub_leading_prefixes(authors_blob_clean)
        if not cleaned:
            continue

        if not _plausible_author_blob(cleaned):
            continue

        flags_out = []
        if lead:
            flags_out.append(f"lead_word:{norm_token(lead).strip()}")
        flags_out.extend(scrub_flags)
        if re.search(r"\'s\s*\(", authors_blob, flags=re.I):
            flags_out.append("possessive")

        # If "et al", reduce to first surname only
        if re.search(r"\bet\s+al\.?\b", cleaned, flags=re.I):
            first = clean_surname(cleaned)
            if not first:
                continue
            k = key_author_year(first, y)
            raw = norm_space(m.group(0))
            if raw not in seen:
                out.append(InTextCitation("author-year", raw, k, year=y, surnames=(first,), flags=";".join(flags_out + ["etal"])))
                seen.add(raw)
            continue

        if is_known_org(cleaned):
            k = f"org_{canon_org(cleaned)}_{y.lower()}"
            raw = norm_space(m.group(0))
            if raw not in seen:
                out.append(InTextCitation("author-year", raw, k, year=y, surnames=(cleaned,), flags=";".join(flags_out)))
                seen.add(raw)
            continue

        surnames = extract_surnames_from_blob(cleaned)
        if not surnames:
            continue

        k = key_author_year(surnames[0], y)
        raw = norm_space(m.group(0))
        if raw not in seen:
            out.append(InTextCitation("author-year", raw, k, year=y, surnames=tuple(surnames), flags=";".join(flags_out)))
            seen.add(raw)

    return out


# -----------------------------
# Numeric extraction (IEEE/Vancouver)
# -----------------------------
_SUP_DIGITS = {"⁰":"0","¹":"1","²":"2","³":"3","⁴":"4","⁵":"5","⁶":"6","⁷":"7","⁸":"8","⁹":"9"}

def _sup_to_int(s: str) -> Optional[int]:
    try:
        digits = "".join(_SUP_DIGITS.get(ch, "") for ch in s)
        return int(digits) if digits else None
    except Exception:
        return None

def _expand_numeric_chunks(inside: str) -> List[int]:
    inside = inside.replace("–", "-")
    chunks = [c.strip() for c in inside.split(",") if c.strip()]
    nums: List[int] = []
    for c in chunks:
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", c)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b and (b - a) <= 2000:
                nums.extend(range(a, b + 1))
        else:
            if c.isdigit():
                nums.append(int(c))
    return nums

def extract_ieee_numeric_citations(text: str) -> List[InTextCitation]:
    out: List[InTextCitation] = []
    pat = re.compile(r"\[\s*(\d+(?:\s*[-–]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–]\s*\d+)?)*)\s*\]")
    for m in pat.finditer(text or ""):
        raw = m.group(0)
        inside = m.group(1)
        for n in _expand_numeric_chunks(inside):
            out.append(InTextCitation("numeric", raw, key_numeric(n), number=n))
    return out

def extract_vancouver_numeric_citations(text: str) -> List[InTextCitation]:
    out: List[InTextCitation] = []
    paren = re.compile(r"\(\s*(\d+(?:\s*[-–]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–]\s*\d+)?)*)\s*\)")
    for m in paren.finditer(text or ""):
        inside = m.group(1)
        if re.fullmatch(rf"{YEAR}", inside.strip(), flags=re.I):
            continue
        for n in _expand_numeric_chunks(inside):
            out.append(InTextCitation("numeric", m.group(0), key_numeric(n), number=n))

    out.extend(extract_ieee_numeric_citations(text))

    sup_run = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+")
    for m in sup_run.finditer(text or ""):
        n = _sup_to_int(m.group(0))
        if n is not None:
            out.append(InTextCitation("numeric", m.group(0), key_numeric(n), number=n))
    return out


# -----------------------------
# Reconciliation + reporting
# -----------------------------
def reconcile_author_year(cites: List[InTextCitation], refs: List[ReferenceEntry]):
    ref_by_key = defaultdict(list)
    for r in refs:
        ref_by_key[r.key].append(r)

    c2r = []
    for c in cites:
        hits = ref_by_key.get(c.key, [])
        if not hits:
            c2r.append({"in_text": c.raw, "status": "not_found", "matched_reference": "", "flags": c.flags or ""})
        elif len(hits) == 1:
            c2r.append({"in_text": c.raw, "status": "matched", "matched_reference": hits[0].raw, "flags": c.flags or ""})
        else:
            c2r.append({
                "in_text": c.raw,
                "status": f"ambiguous ({len(hits)})",
                "matched_reference": " || ".join(h.raw[:220] for h in hits),
                "flags": c.flags or "",
            })

    cite_group = defaultdict(list)
    for c in cites:
        cite_group[c.key].append(c.raw)

    r2c = []
    for r in refs:
        cited_by = cite_group.get(r.key, [])
        r2c.append({"reference": r.raw, "times_cited": int(len(cited_by)), "cited_by": cited_by})

    r2c.sort(key=lambda x: x.get("times_cited", 0), reverse=True)
    return c2r, r2c


def reconcile_numeric(cites: List[InTextCitation], refs: List[ReferenceEntry]):
    ref_by_key = {r.key: r for r in refs}
    c2r = []
    for c in cites:
        r = ref_by_key.get(c.key)
        if not r:
            c2r.append({"in_text": c.raw, "status": "not_found", "matched_reference": "", "flags": c.flags or ""})
        else:
            c2r.append({"in_text": c.raw, "status": "matched", "matched_reference": r.raw, "flags": c.flags or ""})

    cite_group = defaultdict(list)
    for c in cites:
        cite_group[c.key].append(c.raw)

    r2c = []
    for r in refs:
        cited_by = cite_group.get(r.key, [])
        r2c.append({"reference": r.raw, "times_cited": int(len(cited_by)), "cited_by": cited_by})

    r2c.sort(key=lambda x: x.get("times_cited", 0), reverse=True)
    return c2r, r2c


def build_missing_uncited(cites: List[InTextCitation], refs: List[ReferenceEntry]):
    cite_keys = [c.key for c in cites]
    ref_keys = [r.key for r in refs]

    cite_count_by_raw = Counter([c.raw for c in cites])
    cite_key_by_raw: Dict[str, str] = {}
    for c in cites:
        cite_key_by_raw.setdefault(c.raw, c.key)

    ref_key_set = set(ref_keys)
    missing = []
    for raw, cnt in cite_count_by_raw.items():
        k = cite_key_by_raw.get(raw, "")
        if k and (k not in ref_key_set):
            missing.append({"citation_in_text": raw, "count_in_text": int(cnt)})
    missing.sort(key=lambda x: (-x["count_in_text"], x["citation_in_text"]))

    cite_key_set = set(cite_keys)
    uncited = [r.raw for r in refs if r.key not in cite_key_set]

    summary = {
        "in_text_citations_found": int(len(cites)),
        "reference_entries_found": int(len(refs)),
        "missing_in_references": int(len(missing)),
        "uncited_references": int(len(uncited)),
    }
    return missing, uncited, summary


def _online_verify_select_refs(refs: List[ReferenceEntry], uncited_raw: List[str], verify_mode: str) -> List[str]:
    mode = (verify_mode or "all").strip().lower()
    all_ref_texts = [r.raw for r in refs]

    if mode == "uncited_only":
        unc_set = set(uncited_raw or [])
        work = [r for r in all_ref_texts if r in unc_set]
        return work if work else all_ref_texts

    return all_ref_texts


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

    if name.endswith(".docx"):
        paras = read_docx_paragraphs(file_bytes)
        full_text = "\n".join(paras)
    elif name.endswith(".pdf"):
        full_text = read_pdf_text(file_bytes)
    else:
        return {"error": "Upload a DOCX or PDF"}

    lines = full_text.splitlines()
    idx = _find_reference_heading(lines)

    if idx == -1:
        main_text = full_text
        references_raw: List[str] = []
        ref_msg = "No References heading found."
    else:
        main_text = "\n".join(lines[:idx]).strip()
        ref_msg = f"Found References heading: {lines[idx].strip()}"
        ref_block_lines = [ln for ln in lines[idx + 1:] if ln.strip()]
        references_raw = _merge_reference_lines(ref_block_lines)

    style_norm = (style or "apa").strip().lower()
    if style_norm in ("apa/harvard", "harvard", "author-year"):
        style_norm = "apa"

    if style_norm == "apa":
        cites = extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]
        c2r, r2c = reconcile_author_year(cites, refs)
    elif style_norm == "ieee":
        cites = extract_ieee_numeric_citations(main_text)
        refs = [parse_reference_numeric(r) for r in references_raw]
        refs = [r for r in refs if r is not None]
        c2r, r2c = reconcile_numeric(cites, refs)
    else:
        cites = extract_vancouver_numeric_citations(main_text)
        refs = [parse_reference_numeric(r) for r in references_raw]
        refs = [r for r in refs if r is not None]
        c2r, r2c = reconcile_numeric(cites, refs)

    missing, uncited, summary = build_missing_uncited(cites, refs)

    verify_rows: List[Dict[str, Any]] = []
    verify_counts = {k: 0 for k in ["verified", "likely", "needs_review", "not_found", "offline"]}

    if verify_online:
        if not VERIFY_OK:
            verify_rows = [{
                "reference": "",
                "status": "offline",
                "source": "",
                "score": 0,
                "doi": "",
                "matched_year": "",
                "matched_authors": "",
                "matched_title": "",
                "query_used": "",
            }]
            verify_counts["offline"] = 1
        else:
            selected = _online_verify_select_refs(refs=refs, uncited_raw=uncited, verify_mode=verify_mode)
            mv = int(max_verify or 0)
            if mv > 0:
                selected = selected[:mv]

            verify_rows = verify_references_batch(
                references=selected,
                max_to_check=(len(selected) if selected else 0),
                throttle_s=float(throttle_s or 0.0),
                use_crossref=bool(use_crossref),
                use_openalex=bool(use_openalex),
            )

            for row in verify_rows:
                row["status"] = _normalize_verify_status(row.get("status"))
                verify_counts[row["status"]] += 1

    online_verification = {
        "summary": {**verify_counts, "total": int(sum(verify_counts.values()))},
        "rows": verify_rows,
    }

    return {
        "filename": filename,
        "style": style_norm,
        "reference_detection_message": ref_msg,
        "text_length": len(full_text),
        "main_text_length": len(main_text),
        "references_detected": len(references_raw),

        "summary": summary,
        "missing_in_references": missing,
        "uncited_references": uncited,

        "reconciliation_intext_to_reference": c2r[:5000],
        "reconciliation_reference_to_intext": r2c[:5000],

        "online_verification": online_verification,
        "verify_mode_used": (verify_mode or "all"),
    }
