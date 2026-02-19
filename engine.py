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

BAD_NARRATIVE_PREFIX_WORDS = {
    "similarly", "however", "nonetheless", "therefore", "thus", "hence",
    "moreover", "furthermore", "consequently", "instead", "meanwhile",
    "overall", "generally", "typically", "notably", "specifically",
    "for", "from", "in", "on", "at", "by", "of", "to", "with", "without",
    "methods", "method", "approach", "approaches",
    "sample", "size", "power", "results", "discussion", "model", "framework",
    "journal", "research", "study", "analysis", "evidence", "theory", "review",
    "table", "figure", "equation", "appendix", "chapter", "section",
}

ORG_ALIASES = {
    "who": ["who", "world health organization", "world health organisation"],
    "un": ["un", "united nations", "u.n.", "united nations organisation", "united nations organization"],
    "oecd": ["oecd", "organisation for economic co-operation and development", "organization for economic cooperation and development"],
    "imf": ["imf", "international monetary fund"],
    "world_bank": ["world bank", "international bank for reconstruction and development", "ibrd"],
    "unesco": ["unesco", "united nations educational, scientific and cultural organization", "united nations educational scientific and cultural organization"],
    "unicef": ["unicef", "united nations children's fund", "united nations childrens fund"],
}
ORG_ACRONYMS = {k.upper() for k in ["WHO", "UN", "OECD", "IMF", "UNESCO", "UNICEF", "WORLD BANK", "IBRD"]}

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


def looks_like_surname(tok: str) -> bool:
    if not tok:
        return False
    t = tok.strip()
    if len(t) < 2:
        return False
    if not re.fullmatch(r"[A-Z][A-Za-z\-']{1,40}", t):
        return False
    if norm_token(t) in BAD_NARRATIVE_PREFIX_WORDS:
        return False
    return True


def clean_surname(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("&", " and ")
    s = re.sub(r"\bet\s+al\.?\b", "", s, flags=re.I)
    s = re.sub(r"[^A-Za-z\-'\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    parts = s.split()
    return parts[-1] if parts else ""


def key_author_year(first_surname: str, year: str) -> str:
    return f"au_{norm_token(first_surname)}_{(year or '').lower()}"


def key_numeric(n: int) -> str:
    return f"n_{int(n)}"


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


def _find_reference_heading(lines: List[str]) -> int:
    for i, line in enumerate(lines):
        s = (line or "").strip()
        if not s:
            continue
        for pat in REF_HEADINGS:
            if re.search(pat, s, flags=re.I):
                return i
    return -1


def _merge_reference_lines(raw_lines: List[str]) -> List[str]:
    raw_lines = [ln.strip() for ln in raw_lines if ln and ln.strip()]
    if not raw_lines:
        return []

    merged = []
    buf = ""

    for ln in raw_lines:
        s = ln.strip()

        is_doi_line = bool(re.match(r"^\s*(doi\s*:\s*)?10\.\d{4,9}/", s, flags=re.I)) or \
                      bool(re.match(r"^\s*https?://doi\.org/10\.\d{4,9}/", s, flags=re.I)) or \
                      bool(re.match(r"^\s*10\.\d{4,9}/", s, flags=re.I))

        apa_start = bool(re.match(r"^[A-Z][A-Za-z\-\']+,\s+[A-Z]\.", s))
        numeric_start = bool(re.match(r"^\s*\[\d+\]\s+", s)) or \
                        bool(re.match(r"^\s*\d{1,4}[\.\)]\s+", s)) or \
                        bool(re.match(r"^\s*\d{1,4}\s+", s))

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

    if is_known_org(pre) or pre.upper() in ORG_ACRONYMS:
        k = f"org_{canon_org(pre)}_{year.lower()}"
        return ReferenceEntry(raw=r, key=k, year=year, surnames=(pre,), number=None)

    if "," in pre:
        first = pre.split(",")[0].strip()
    else:
        first = pre.split()[0].strip() if pre.split() else ""

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
# Author-year in-text extraction
#   Adds:
#   - by Adam, Kofi, & Yaw (2020)
#   - Adam, Kofi and Yaw (2020)
#   - Adam's (2020)
# -----------------------------
def _is_probably_title_context(txt: str, start: int) -> bool:
    left = txt[max(0, start - 80):start].strip()
    if not left:
        return False
    tokens = re.findall(r"[A-Za-z][A-Za-z\-']+", left)
    if len(tokens) < 2:
        return False
    prev = tokens[-1]
    prev2 = tokens[-2]
    # consecutive Title Case words often indicate a title segment
    if prev and prev2 and prev[0].isupper() and prev2[0].isupper():
        return True
    # end-of-phrase tokens that indicate non-citation
    if norm_token(prev) in ("journal", "research", "study", "analysis", "methods", "method", "results", "discussion"):
        return True
    return False


def _emit_author_year(out: List[InTextCitation], raw: str, first_author: str, year: str, all_surnames: List[str]):
    if not first_author or not year:
        return
    k = key_author_year(first_author, year)
    out.append(InTextCitation("author-year", raw, k, year=year, surnames=tuple(all_surnames or [first_author])))


def extract_author_year_citations(text: str) -> List[InTextCitation]:
    out: List[InTextCitation] = []
    txt = text or ""

    # 1) Parenthetical: (Adam, 2020; Kofi, 2021)
    for m in re.finditer(rf"\(([^()]*\b{YEAR}\b[^()]*)\)", txt, flags=re.I):
        inside = m.group(1).strip()
        # ignore bare year: (2020)
        if re.fullmatch(rf"{YEAR}", norm_space(inside), flags=re.I):
            continue

        chunks = [p.strip() for p in inside.split(";") if p.strip()] or [inside]
        for chunk in chunks:
            y_m = YEAR_RE.search(chunk)
            if not y_m:
                continue
            year = y_m.group(1)
            left = chunk[: y_m.start()].strip().rstrip(",").strip()

            # org support
            if is_known_org(left):
                k = f"org_{canon_org(left)}_{year.lower()}"
                out.append(InTextCitation("author-year", f"({norm_space(chunk)})", k, year=year, surnames=(left,)))
                continue

            # parse author list before year
            left2 = left.replace("&", " and ")
            toks = [t.strip() for t in re.split(r"\s+and\s+|,", left2) if t.strip()]
            surns = []
            for t in toks:
                if looks_like_surname(t):
                    surns.append(t)
                else:
                    # allow "Adam et al."
                    if re.search(r"\bet\s+al\.?\b", t, flags=re.I):
                        s0 = clean_surname(t)
                        if looks_like_surname(s0):
                            surns.append(s0)

            if not surns:
                continue
            _emit_author_year(out, f"({norm_space(chunk)})", surns[0], year, surns)

    # 2) Narrative "Adam et al. (2020)"
    for m in re.finditer(rf"\b(?P<a>[A-Z][A-Za-z\-']{{1,40}})\s+et\s+al\.\s*\(\s*(?P<y>{YEAR})\s*\)", txt, flags=re.I):
        if _is_probably_title_context(txt, m.start()):
            continue
        a = m.group("a").strip()
        y = m.group("y")
        if looks_like_surname(a):
            _emit_author_year(out, m.group(0), a, y, [a])

    # 3) Narrative single: "Adam (2020)"
    for m in re.finditer(rf"\b(?P<a>[A-Z][A-Za-z\-']{{1,40}})\s*\(\s*(?P<y>{YEAR})\s*\)", txt, flags=re.I):
        if _is_probably_title_context(txt, m.start()):
            continue
        a = m.group("a").strip()
        y = m.group("y")
        if norm_token(a) in BAD_NARRATIVE_PREFIX_WORDS:
            continue
        if is_known_org(a):
            k = f"org_{canon_org(a)}_{y.lower()}"
            out.append(InTextCitation("author-year", m.group(0), k, year=y, surnames=(a,)))
            continue
        if looks_like_surname(a):
            _emit_author_year(out, m.group(0), a, y, [a])

    # 4) NEW: "by Adam, Kofi, & Yaw (2020)" or "Adam, Kofi and Yaw (2020)"
    #    We capture up to 5 surnames, require at least 2 surnames before the year
    multi_pat = re.compile(
        rf"\b(?:by\s+)?(?P<names>(?:[A-Z][A-Za-z\-']{{1,40}}(?:\s*,\s*|\s+and\s+|\s*&\s*)){{1,4}}[A-Z][A-Za-z\-']{{1,40}})\s*\(\s*(?P<y>{YEAR})\s*\)",
        flags=re.I
    )
    for m in multi_pat.finditer(txt):
        if _is_probably_title_context(txt, m.start()):
            continue
        names = m.group("names") or ""
        y = m.group("y")
        # split into surname tokens
        nm = names.replace("&", " and ")
        parts = [p.strip() for p in re.split(r"\s+and\s+|,", nm) if p.strip()]
        surns = [p for p in parts if looks_like_surname(p)]
        if len(surns) >= 2:
            _emit_author_year(out, m.group(0), surns[0], y, surns)

    # 5) NEW: Possessive "Adam's (2020)"
    poss_pat = re.compile(rf"\b(?P<a>[A-Z][A-Za-z\-']{{1,40}})'s\s*\(\s*(?P<y>{YEAR})\s*\)", flags=re.I)
    for m in poss_pat.finditer(txt):
        if _is_probably_title_context(txt, m.start()):
            continue
        a = m.group("a").strip()
        y = m.group("y")
        if looks_like_surname(a):
            _emit_author_year(out, m.group(0), a, y, [a])

    # de-dup by raw
    uniq: List[InTextCitation] = []
    seen = set()
    for c in out:
        if c.raw not in seen:
            uniq.append(c)
            seen.add(c.raw)
    return uniq


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


def reconcile_author_year(cites: List[InTextCitation], refs: List[ReferenceEntry]) -> Tuple[List[Dict], List[Dict]]:
    ref_by_key = defaultdict(list)
    for r in refs:
        ref_by_key[r.key].append(r)

    c2r = []
    for c in cites:
        hits = ref_by_key.get(c.key, [])
        if not hits:
            c2r.append({"in_text": c.raw, "status": "not_found", "matched_reference": ""})
        elif len(hits) == 1:
            c2r.append({"in_text": c.raw, "status": "matched", "matched_reference": hits[0].raw})
        else:
            c2r.append({
                "in_text": c.raw,
                "status": f"ambiguous ({len(hits)})",
                "matched_reference": " || ".join(h.raw[:220] for h in hits),
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


def reconcile_numeric(cites: List[InTextCitation], refs: List[ReferenceEntry]) -> Tuple[List[Dict], List[Dict]]:
    ref_by_key = {r.key: r for r in refs}
    c2r = []
    for c in cites:
        r = ref_by_key.get(c.key)
        if not r:
            c2r.append({"in_text": c.raw, "status": "not_found", "matched_reference": ""})
        else:
            c2r.append({"in_text": c.raw, "status": "matched", "matched_reference": r.raw})

    cite_group = defaultdict(list)
    for c in cites:
        cite_group[c.key].append(c.raw)

    r2c = []
    for r in refs:
        cited_by = cite_group.get(r.key, [])
        r2c.append({"reference": r.raw, "times_cited": int(len(cited_by)), "cited_by": cited_by})

    r2c.sort(key=lambda x: x.get("times_cited", 0), reverse=True)
    return c2r, r2c


def build_missing_uncited(cites: List[InTextCitation], refs: List[ReferenceEntry]) -> Tuple[List[Dict], List[str], Dict]:
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


def _online_verify_select_refs(
    refs: List[ReferenceEntry],
    uncited_raw: List[str],
    verify_mode: str,
) -> List[str]:
    mode = (verify_mode or "all").strip().lower()
    all_ref_texts = [r.raw for r in refs]
    if not all_ref_texts:
        return []
    if mode == "uncited_only":
        u = set(uncited_raw or [])
        sel = [r for r in all_ref_texts if r in u]
        return sel if sel else all_ref_texts
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
                "matched_first_author": "",
                "matched_authors": "",
                "matched_title": "",
                "query_used": "",
                "error": "verify.py not available or import failed",
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
        "verify_enabled": bool(verify_online),
    }
