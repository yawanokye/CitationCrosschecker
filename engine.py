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
    "similarly", "however", "moreover", "likewise",
    "therefore", "thus", "hence", "consequently",
    "additionally", "furthermore", "in addition",
    "in contrast", "on the other hand", "for example",
    "for instance", "in conclusion", "overall", "notably",
}

ETAL_RE = re.compile(r"\bet\s+al\.?\b", re.I)

# Organizations mapping (basic)
ORG_SYNONYMS = {
    "world health organization": "who",
    "who": "who",
    "united nations": "un",
    "un": "un",
    "african union": "au",
    "au": "au",
    "european union": "eu",
    "eu": "eu",
}


def _norm_text(s: str) -> str:
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    s = s.replace("–", "-").replace("—", "-")
    return s.strip()


def _canon_org(name: str) -> str:
    n = _norm_text(name)
    return ORG_SYNONYMS.get(n, n)


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    if st not in {"verified", "likely", "needs_review", "not_found", "offline"}:
        st = "needs_review"
    return st


def read_docx_paragraphs(file_bytes: bytes) -> List[str]:
    if not DOCX_OK:
        raise RuntimeError("python-docx not installed")
    doc = Document(io.BytesIO(file_bytes))
    out = []
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            out.append(t)
    return out


def read_pdf_text(file_bytes: bytes) -> str:
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")
    out = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            if txt:
                out.append(txt)
    return "\n".join(out)


def _find_reference_heading(lines: List[str]) -> int:
    for i, ln in enumerate(lines):
        s = (ln or "").strip()
        if not s:
            continue
        for pat in REF_HEADINGS:
            if re.match(pat, s, re.I):
                return i
    return -1


def _merge_reference_lines(ref_lines: List[str]) -> List[str]:
    """
    Merge broken PDF reference lines into reference entries.
    Handles:
      - Numeric styles: "1." or "1 " or "[1]"
      - Author-year: starts with Author, A. (YYYY)
    """
    items = []
    buf = ""
    for ln in ref_lines:
        s = (ln or "").strip()
        if not s:
            continue

        is_new_numeric = bool(re.match(r"^\s*(?:\[\d+\]|\d+\.|\d+\s)", s))
        is_new_apa = bool(re.match(r"^[A-Z][A-Za-z'`-]+.*\(\d{4}[a-z]?\)", s))

        if is_new_numeric or is_new_apa:
            if buf.strip():
                items.append(buf.strip())
            buf = s
        else:
            if buf:
                buf += " " + s
            else:
                buf = s

    if buf.strip():
        items.append(buf.strip())
    return items


@dataclass
class Citation:
    raw: str
    key: str
    count: int = 1


@dataclass
class ReferenceEntry:
    raw: str
    key: str
    year: str = ""
    authors: str = ""


def _clean_key_author_year(author: str, year: str) -> str:
    a = _canon_org(author)
    a = re.sub(r"[^a-z0-9 ]", " ", a)
    a = re.sub(r"\s+", " ", a).strip()
    return f"{a}_{year}".strip("_")


def extract_author_year_citations(text: str) -> List[Citation]:
    """
    Extract author-year citations: (Adam, 2020), Adam (2020), (WHO, 2020), etc.
    """
    t = text or ""
    out: List[Citation] = []

    # Parenthetical: (Author, 2020) or (Author & Author, 2020)
    for m in re.finditer(r"\(([^()]{1,120}?)\)", t):
        inside = m.group(1)
        yrs = YEAR_RE.findall(inside)
        if not yrs:
            continue
        year = yrs[-1]
        # take leading chunk before year
        left = inside.split(year)[0]
        left = left.replace("&", ",")
        left = re.sub(r"\band\b", ",", left, flags=re.I)
        left = left.split(",")[0].strip()
        if not left:
            continue
        key = _clean_key_author_year(left, year)
        out.append(Citation(raw=m.group(0), key=key))

    # Narrative: Adam (2020)
    for m in re.finditer(r"\b([A-Z][A-Za-z'`-]+(?:\s+[A-Z][A-Za-z'`-]+)*)\s*\(\s*(" + YEAR + r")\s*\)", t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        key = _clean_key_author_year(author, year)
        out.append(Citation(raw=m.group(0), key=key))

    # Count occurrences
    c = Counter([x.key for x in out])
    uniq = []
    for k, n in c.items():
        raw = next((x.raw for x in out if x.key == k), k)
        uniq.append(Citation(raw=raw, key=k, count=n))
    return uniq


def extract_ieee_numeric_citations(text: str) -> List[Citation]:
    out = []
    for m in re.finditer(r"\[(\d{1,4})\]", text or ""):
        num = m.group(1)
        out.append(Citation(raw=m.group(0), key=num))
    c = Counter([x.key for x in out])
    uniq = []
    for k, n in c.items():
        raw = f"[{k}]"
        uniq.append(Citation(raw=raw, key=k, count=n))
    return uniq


def extract_vancouver_numeric_citations(text: str) -> List[Citation]:
    """
    Extract Vancouver citations:
      - [1], [1-3], (1), (1-3), 1,2,3 (simple)
    """
    t = text or ""
    out = []

    # [1], [1-3], [1,2,3]
    for m in re.finditer(r"\[(\d{1,4}(?:\s*[-,]\s*\d{1,4})*)\]", t):
        inside = m.group(1)
        parts = re.split(r"\s*,\s*", inside)
        for p in parts:
            if "-" in p:
                a, b = [x.strip() for x in p.split("-", 1)]
                if a.isdigit() and b.isdigit():
                    for k in range(int(a), int(b) + 1):
                        out.append(Citation(raw=m.group(0), key=str(k)))
            else:
                if p.strip().isdigit():
                    out.append(Citation(raw=m.group(0), key=p.strip()))

    # (1), (1-3)
    for m in re.finditer(r"\((\d{1,4}(?:\s*[-,]\s*\d{1,4})*)\)", t):
        inside = m.group(1)
        parts = re.split(r"\s*,\s*", inside)
        for p in parts:
            if "-" in p:
                a, b = [x.strip() for x in p.split("-", 1)]
                if a.isdigit() and b.isdigit():
                    for k in range(int(a), int(b) + 1):
                        out.append(Citation(raw=m.group(0), key=str(k)))
            else:
                if p.strip().isdigit():
                    out.append(Citation(raw=m.group(0), key=p.strip()))

    c = Counter([x.key for x in out])
    uniq = []
    for k, n in c.items():
        uniq.append(Citation(raw=k, key=k, count=n))
    return uniq


def parse_reference_author_year(ref: str) -> Optional[ReferenceEntry]:
    r = (ref or "").strip()
    if not r:
        return None
    m = re.search(r"\((%s)\)" % YEAR, r)
    if not m:
        return None
    year = m.group(1)
    # take part before year as authors chunk
    left = r.split("(" + year)[0].strip()
    left = left.replace("&", ",")
    left = re.sub(r"\band\b", ",", left, flags=re.I)
    first = left.split(",")[0].strip()
    if not first:
        return None
    key = _clean_key_author_year(first, year)
    return ReferenceEntry(raw=r, key=key, year=year, authors=first)


def parse_reference_numeric(ref: str) -> Optional[ReferenceEntry]:
    r = (ref or "").strip()
    if not r:
        return None
    m = re.match(r"^\s*(?:\[(\d+)\]|(\d+)\.|(\d+)\s)", r)
    if not m:
        return None
    num = next(g for g in m.groups() if g is not None)
    key = str(num)
    return ReferenceEntry(raw=r, key=key)


def reconcile_author_year(cites: List[Citation], refs: List[ReferenceEntry]):
    ref_map = {r.key: r.raw for r in refs}
    c2r = []
    for c in cites:
        if c.key in ref_map:
            c2r.append({
                "status": "matched",
                "in_text": c.raw,
                "matched_reference": ref_map[c.key],
                "flags": "",
            })
        else:
            c2r.append({
                "status": "missing",
                "in_text": c.raw,
                "matched_reference": "",
                "flags": "Not in reference list",
            })

    r2c = []
    cite_keys = set([c.key for c in cites])
    cite_counts = Counter([c.key for c in cites])
    for r in refs:
        if r.key in cite_keys:
            r2c.append({
                "times_cited": int(cite_counts.get(r.key, 0)),
                "reference": r.raw,
                "cited_by": [r.key],
            })
        else:
            r2c.append({
                "times_cited": 0,
                "reference": r.raw,
                "cited_by": [],
            })
    return c2r, r2c


def reconcile_numeric(cites: List[Citation], refs: List[ReferenceEntry]):
    ref_map = {r.key: r.raw for r in refs}
    c2r = []
    for c in cites:
        if c.key in ref_map:
            c2r.append({
                "status": "matched",
                "in_text": c.raw,
                "matched_reference": ref_map[c.key],
                "flags": "",
            })
        else:
            c2r.append({
                "status": "missing",
                "in_text": c.raw,
                "matched_reference": "",
                "flags": "Not in reference list",
            })

    r2c = []
    cite_keys = [c.key for c in cites]
    cite_counts = Counter(cite_keys)
    for r in refs:
        k = r.key
        n = int(cite_counts.get(k, 0))
        r2c.append({
            "times_cited": n,
            "reference": r.raw,
            "cited_by": [k] if n > 0 else [],
        })
    return c2r, r2c


def build_missing_uncited(cites: List[Citation], refs: List[ReferenceEntry]):
    cite_keys = set([c.key for c in cites])
    ref_keys = set([r.key for r in refs])

    missing_keys = sorted(list(cite_keys - ref_keys))
    uncited_keys = sorted(list(ref_keys - cite_keys))

    cite_counts = Counter([c.key for c in cites])

    missing = []
    for k in missing_keys:
        missing.append({"citation_in_text": k, "count_in_text": int(cite_counts.get(k, 0))})

    uncited = []
    ref_map = {r.key: r.raw for r in refs}
    for k in uncited_keys:
        uncited.append(ref_map.get(k, k))

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
    offset: int = 0,
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

    total_selected = 0
    batch_offset = 0
    batch_size = 0
    next_offset: Optional[int] = None

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
            total_selected = 0
            batch_offset = 0
            batch_size = 0
            next_offset = None
        else:
            selected_all = _online_verify_select_refs(refs=refs, uncited_raw=uncited, verify_mode=verify_mode)
            total_selected = len(selected_all)

            off = int(offset or 0)
            if off < 0:
                off = 0
            batch_offset = off

            mv = int(max_verify or 0)
            if mv > 0:
                selected = selected_all[off: off + mv]
            else:
                selected = selected_all[off:]

            batch_size = len(selected)
            nxt = off + batch_size
            next_offset = None if nxt >= total_selected else nxt

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
        "total_selected": int(total_selected),
        "offset": int(batch_offset),
        "batch_size": int(batch_size),
        "next_offset": next_offset,
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
    }
