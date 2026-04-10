# engine.py (COMPLETE OPTIMIZED VERSION)
__version__ = "1.6.0"

import re
import io
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter
from functools import lru_cache
from pdf_to_docx_pipeline import process_pdf

ENGINE_BUILD = "commercial-2026-04-10-final"

# Optional imports with fallbacks
try:
    from rapidfuzz import fuzz
    FUZZ_OK = True
except ImportError:
    fuzz = None
    FUZZ_OK = False

try:
    from docx import Document
    DOCX_OK = True
except ImportError:
    DOCX_OK = False

try:
    import pdfplumber
    PDF_OK = True
except ImportError:
    PDF_OK = False


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class RefAY:
    reference_full: str
    key: str

@dataclass
class RefNum:
    reference_full: str
    num: str


# ============================================================================
# Constants
# ============================================================================

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
    re.I,
)

DISCOURSE_PREFIXES = {
    "see", "e.g", "eg", "i.e", "ie", "as", "in", "for", "from", "to", "at", 
    "on", "by", "with", "within", "according", "adapted", "based", "cited", 
    "citing", "reported", "like", "however", "similarly", "regrettably", 
    "traditionally", "notably", "therefore", "thus", "hence", "consequently", 
    "moreover", "furthermore", "additionally", "meanwhile", "nonetheless", 
    "nevertheless", "overall", "generally", "specifically", "particularly", 
    "importantly", "indeed", "for instance", "instance", "for example", 
    "example", "likely", "uncertainty", "likewise",
}

REF_END_HEADINGS = [
    r"^\s*appendix(?:es)?\b",
    r"^\s*annex(?:es)?\b",
    r"^\s*supplement(?:ary)?\b",
    r"^\s*supporting\s+information\b",
]
REF_END_HEADING_RE = re.compile("|".join(REF_END_HEADINGS), re.I)

NON_NAME_AUTHOR_KEYS = {
    "survey", "field", "work", "fieldwork", "data", "dataset", "table", 
    "figure", "fig", "chapter", "section", "appendix", "model", "analysis", 
    "results", "method", "study", "paper", "thesis", "report", "source",
}

NARRATIVE_PHRASE_PATTERNS = [
    r"\byear\s+on\s+year\b", r"\bgrowth\s+rate\b", r"\ball\s+share\s+index\b",
    r"\bselected\s+african\s+countries\b", r"\btop\s+four\s+african\s+countries\b",
    r"\baccording\s+to\b",
]

_LEAD_NUM_RE = re.compile(r"^\s*(?:\[\s*\d{1,4}\s*\]|\(?\s*\d{1,4}\s*\)?|\d{1,4})\s*[\.)\]]\s*")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s)]+", re.I)
_DECADE_YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20\d{2})s\b", re.I)

_REF_STOPWORDS = {
    "the","a","an","and","or","of","in","on","for","to","with","from","at","by","as",
    "ed","eds","edition","vol","volume","no","number","pp","pages","page",
}


# ============================================================================
# Helper Functions
# ============================================================================

@lru_cache(maxsize=10000)
def norm_space(s: str) -> str:
    """Normalize whitespace with caching for performance."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def soft_lower(s: str) -> str:
    return norm_space(s).lower()


def strip_punct(s: str) -> str:
    s = soft_lower(s)
    s = re.sub(r"[“”\"'’`]", "", s)
    s = re.sub(r"[^a-z0-9\s\-&/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _base_year(y: str) -> str:
    y = (y or "").strip()
    m = re.match(r"^((?:19|20)\d{2})", y)
    return m.group(1) if m else y


def _surnames_from_author_blob(left: str) -> List[str]:
    """Extract author surnames from text blob."""
    s = (left or "").strip()
    if not s:
        return []
    
    s = s.replace("&", " and ")
    s = re.sub(r"\bet\s+al\.?\b", "", s, flags=re.I)
    s = re.sub(r"(’s|'s)\b", "", s)
    s = re.sub(r"\b(and|for|instance|see|e\.g\.|i\.e\.)\b", " ", s, flags=re.I)
    s = re.sub(r"\b[A-Z]\.\b", " ", s)
    s = re.sub(r"\b[A-Z]\b", " ", s)
    
    parts = re.split(r"\band\b|;|/|\|", s, flags=re.I)
    out = []
    for p in parts:
        p = p.strip(" ,.;:()[]{}")
        if not p:
            continue
        if "," in p:
            cand = p.split(",", 1)[0].strip()
        else:
            cand = p.split()[-1].strip()
        cand = re.sub(r"[^A-Za-z\-']+", "", cand).strip()
        if len(cand) < 2:
            continue
        if cand.lower() in {"available", "ssrn", "university", "press", "journal"}:
            continue
        out.append(cand.lower())
    
    seen = set()
    final = []
    for x in out:
        if x not in seen:
            seen.add(x)
            final.append(x)
    return final[:4]


def _strip_leading_reference_number(s: str) -> str:
    s0 = norm_space(s)
    s0 = _LEAD_NUM_RE.sub("", s0)
    return s0.strip()


def _first_author_or_org_key(author_left: str) -> str:
    s = norm_space(author_left)
    
    m = re.search(r"\(([A-Z][A-Z0-9/&\-]{1,15})\)", s)
    if m:
        return strip_punct(m.group(1))
    
    s = _strip_leading_reference_number(s)
    s = re.sub(r"\(\s*(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?\s*\).*", "", s).strip()
    s = re.sub(r"(’s|'s)\b", "", s)
    
    m_si = re.match(r"^\s*([A-Z][A-Za-z'\-]+)\s+[A-Z]{1,3}\b", s)
    if m_si:
        return strip_punct(m_si.group(1))
    
    s0 = re.split(r"\s+(?:&|and|＆)\s+|,", s, maxsplit=1)[0].strip()
    s0 = re.sub(r"\bet\s+al\.?\b", "", s0, flags=re.I).strip()
    
    toks = [t for t in re.split(r"\s+", s0) if t and re.search(r"[A-Za-z0-9]", t)]
    if not toks:
        return ""
    return strip_punct(toks[-1])


def _is_likely_narrative_citation(left: str, year: str, full_cite: str) -> bool:
    if not (left or "").strip():
        return True
    
    s_full = (full_cite or "").lower()
    for pat in NARRATIVE_PHRASE_PATTERNS:
        if re.search(pat, s_full, flags=re.I):
            return True
    
    if year and year.lower().endswith("s") and _DECADE_YEAR_RE.search(full_cite or ""):
        return True
    
    l_norm = soft_lower(left)
    if re.fullmatch(r"[a-z\-']+", l_norm) and l_norm in {"crisis", "war", "pandemic", "covid"}:
        return True
    
    return False


# ============================================================================
# Citation Extraction
# ============================================================================

def extract_author_year_citations(text: str) -> List[str]:
    """Extract all APA/Harvard citations from text."""
    if not text:
        return []
    
    t = text.replace("\u2019", "'")
    citations = set()
    
    # Parenthetical citations (Author, Year)
    paren_pat = re.compile(r"\(([^()]{0,260}?\b(?:19|20)\d{2}[a-z]?\b[^()]{0,260}?)\)")
    for m in paren_pat.finditer(t):
        inside = m.group(1).strip()
        if YEAR_RE.fullmatch(inside) and not re.search(r"[A-Za-z]", inside):
            continue
        for ch in re.split(r"\s*;\s*", inside):
            ch = ch.strip()
            if ch and YEAR_RE.search(ch):
                citations.add(norm_space(ch))
    
    # Narrative citations - Author (Year)
    narr_pat = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+et\s+al\.?)?)\s*\(\s*((?:19|20)\d{2}[a-z]?)\s*\)')
    for m in narr_pat.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        citations.add(f"{author}, {year}")
    
    return [c for c in citations if c]


def extract_ieee_citations(text: str) -> List[str]:
    t = text or ""
    out = []
    t = re.sub(r"(?:table|figure|fig\.?|eq\.?|equation)\s+(\d{1,4})", "", t, flags=re.I)
    ieee_pat = re.compile(r"\[\s*(\d{1,4})(?:\s*[-–,]\s*(\d{1,4}))?(?:\s*,\s*(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?)?\s*\]")
    for m in ieee_pat.finditer(t):
        nums = _expand_citation_range(m)
        out.extend(nums)
    return list(dict.fromkeys(out))


def extract_vancouver_citations(text: str) -> List[str]:
    citations = []
    for m in re.compile(r'\[\s*(\d+)\s*\]').finditer(text or ""):
        citations.append(m.group(1))
    try:
        return sorted(set(citations), key=int)
    except:
        return list(dict.fromkeys(citations))


def _expand_citation_range(match) -> List[str]:
    nums = []
    groups = match.groups()
    if not groups or not groups[0]:
        return nums
    start = int(groups[0])
    if groups[1]:
        end = int(groups[1])
        if start <= end and (end - start) <= 50:
            nums.extend([str(i) for i in range(start, end + 1)])
        else:
            nums.extend([str(start), str(end)])
    else:
        nums.append(str(start))
    return nums


# ============================================================================
# Reference Parsing
# ============================================================================

def _is_plausible_reference_entry(s: str) -> bool:
    s0 = _strip_leading_reference_number(s)
    if not s0 or len(s0) < 18 or not YEAR_RE.search(s0):
        return False
    return True


def parse_reference_author_year(ref: str) -> Optional[RefAY]:
    s = norm_space(ref)
    if not s:
        return None
    
    s_clean = _strip_leading_reference_number(s)
    if not _is_plausible_reference_entry(s_clean):
        return None
    
    m = re.search(r"\(\s*(" + YEAR + r")\s*\)", s_clean)
    if not m:
        m2 = re.search(r"\b(" + YEAR + r")\b", s_clean)
        if not m2:
            return None
        year = m2.group(1)
        left = s_clean[: m2.start()].strip()
    else:
        year = m.group(1)
        left = s_clean[: m.start()].strip()
    
    author_key = _first_author_or_org_key(left)
    if not author_key:
        return None
    
    return RefAY(reference_full=s_clean, key=f"{author_key}|{year}".lower())


def parse_reference_numeric(ref: str, style: str = "ieee") -> Optional[RefNum]:
    s = norm_space(ref)
    if not s:
        return None
    
    if style.lower() == "ieee":
        m = re.match(r"^\[\s*(\d{1,4})\s*\]\s*(.+)$", s)
        if m:
            num = m.group(1)
            body = norm_space(m.group(2))
            body = _strip_leading_reference_number(body)
            if len(body) > 20 and re.search(r'[A-Z][a-z]+', body):
                return RefNum(reference_full=s, num=num)
    return None


def parse_vancouver_references(references_raw: List[str]) -> List[RefNum]:
    parsed_refs = []
    for i, ref in enumerate(references_raw, start=1):
        ref = ref.strip()
        if not ref or len(ref) < 20:
            continue
        parsed_refs.append(RefNum(reference_full=ref, num=str(i)))
    return parsed_refs


# ============================================================================
# Reconciliation Functions
# ============================================================================

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
    
    if left:
        prefixes = sorted([re.escape(x) for x in DISCOURSE_PREFIXES], key=len, reverse=True)
        pref_re = re.compile(r"^(?:" + "|".join(prefixes) + r")\b", re.I)
        while True:
            new_left = pref_re.sub("", left).strip(" ,;()")
            if new_left == left:
                break
            left = new_left
    
    for _ in range(3):
        if "," not in left:
            break
        first, rest = left.split(",", 1)
        if re.search(r"\b[A-Z][A-Za-z'\-]+\b", first):
            break
        left = rest.strip(" ,;()")
    
    left = re.sub(r"(’s|'s)\b", "", left).strip()
    
    if _is_likely_narrative_citation(left, year, s):
        return None
    
    author_key = _first_author_or_org_key(left)
    if not author_key or author_key.lower() in NON_NAME_AUTHOR_KEYS:
        return None
    return author_key, year


def reconcile_author_year(citations: List[str], references: List[RefAY]) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    """Match citations to references using multi-strategy approach."""
    
    # Build lookup structures
    alias_map: Dict[str, str] = {}
    refs_by_year: Dict[str, List[RefAY]] = defaultdict(list)
    
    for r in references:
        alias_map[r.key.lower()] = r.reference_full
        
        ym = YEAR_RE.search(r.reference_full)
        if ym:
            refs_by_year[_base_year(ym.group(1))].append(r)
        
        # Build surname aliases
        ym = YEAR_RE.search(r.reference_full)
        if ym:
            year_full = ym.group(1)
            year_base = _base_year(year_full)
            left = r.reference_full[: ym.start()].strip(" ,;()")
            names = _surnames_from_author_blob(left)
            
            for nm in names[:2]:
                alias_map[f"{nm}|{year_full}".lower()] = r.reference_full
                if year_base != year_full:
                    alias_map[f"{nm}|{year_base}".lower()] = r.reference_full
            
            if len(names) >= 2:
                a, b = names[0], names[1]
                alias_map[f"{a}+{b}|{year_full}".lower()] = r.reference_full
                alias_map[f"{b}+{a}|{year_full}".lower()] = r.reference_full
    
    cite_counts = Counter()
    parsed_cites = []
    
    for c in citations:
        parsed = _parse_author_year_from_cite(c)
        if not parsed:
            continue
        
        auth, year = parsed
        year_base = _base_year(year) if len(year) == 4 else year
        
        # Build candidate keys
        cand_keys = [f"{auth}|{year}".lower()]
        if year_base and year_base != year:
            cand_keys.append(f"{auth}|{year_base}".lower())
        
        # Extract surnames for additional keys
        ym = YEAR_RE.search(c)
        if ym:
            left = (c[: ym.start()] or "").strip(" ,;()")
            cite_names = _surnames_from_author_blob(left)
            if cite_names:
                cand_keys.append(f"{cite_names[0]}|{ym.group(1)}".lower())
                if len(cite_names) >= 2:
                    cand_keys.append(f"{cite_names[0]}+{cite_names[1]}|{ym.group(1)}".lower())
        
        # Try direct lookup
        matched_ref = None
        used_key = None
        for k in cand_keys:
            if k in alias_map:
                matched_ref = alias_map[k]
                used_key = k
                break
        
        # Fallback: fuzzy matching
        if not matched_ref and year_base:
            best_ref = ""
            best_score = 0
            ym_c = YEAR_RE.search(c)
            if ym_c:
                yb = _base_year(ym_c.group(1))
                left_c = (c[: ym_c.start()] or "").strip(" ,;()")
                cite_names = _surnames_from_author_blob(left_c)
                
                for rr in refs_by_year.get(yb, []):
                    s_full = rr.reference_full
                    ym_r = YEAR_RE.search(s_full)
                    if not ym_r:
                        continue
                    left_r = s_full[: ym_r.start()].strip(" ,;()")
                    ref_names = _surnames_from_author_blob(left_r)
                    
                    overlap = len(set(cite_names) & set(ref_names))
                    score = int(round(100 * (overlap / max(1, len(set(cite_names))))))
                    
                    if FUZZ_OK and fuzz and cite_names and ref_names:
                        score = max(score, fuzz.token_set_ratio(" ".join(cite_names), " ".join(ref_names)))
                    
                    if score > best_score:
                        best_score = score
                        best_ref = rr.reference_full
                        if best_score >= 95:
                            break
            
            if best_ref and best_score >= 74:
                matched_ref = best_ref
                used_key = f"fuzzy:{best_score}"
        
        if matched_ref:
            cite_counts[matched_ref] += 1
            parsed_cites.append((matched_ref, c, used_key or ""))
        else:
            parsed_cites.append(("", c, ""))
    
    # Build output
    c2r = []
    missing_counter = Counter()
    for matched_ref, c, flags in parsed_cites:
        if matched_ref:
            c2r.append({"status": "matched", "in_text": c, "matched_reference": matched_ref, "flags": flags})
        else:
            c2r.append({"status": "not_found", "in_text": c, "matched_reference": "", "flags": ""})
            missing_counter[c] += 1
    
    # Build reference-to-citation mapping
    cite_samples = defaultdict(list)
    for matched_ref, c, _ in parsed_cites:
        if matched_ref and len(cite_samples[matched_ref]) < 6:
            cite_samples[matched_ref].append(c)
    
    r2c = []
    uncited_refs = []
    for r in references:
        times = cite_counts.get(r.reference_full, 0)
        if times == 0:
            uncited_refs.append(r.reference_full)
        r2c.append({
            "times_cited": times,
            "reference": r.reference_full,
            "cited_by": cite_samples.get(r.reference_full, [])
        })
    
    missing_rows = [{"citation_in_text": k, "count_in_text": int(v)} for k, v in missing_counter.most_common()]
    unique_intext_count = len(set(citations))
    
    return c2r, r2c, missing_rows, uncited_refs, unique_intext_count


def reconcile_numeric(citations: List[str], references: List[RefNum], style: str = "ieee") -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    if style.lower() != "ieee":
        return [], [], [], [], 0
    
    ref_by_num = {r.num: r.reference_full for r in references}
    cite_counts = Counter(citations)
    
    c2r = []
    missing = Counter()
    for cite in citations:
        if cite in ref_by_num:
            c2r.append({"status": "matched", "in_text": cite, "matched_reference": ref_by_num[cite], "flags": ""})
        else:
            c2r.append({"status": "not_found", "in_text": cite, "matched_reference": "", "flags": ""})
            missing[cite] += 1
    
    r2c = []
    uncited = []
    cite_samples = defaultdict(list)
    for cite in citations:
        if cite in ref_by_num and len(cite_samples[ref_by_num[cite]]) < 6:
            cite_samples[ref_by_num[cite]].append(cite)
    
    for r in references:
        times = cite_counts.get(r.num, 0)
        if times == 0:
            uncited.append(r.reference_full)
        r2c.append({"times_cited": times, "reference": r.reference_full, "cited_by": cite_samples.get(r.reference_full, [])})
    
    missing_rows = [{"citation_in_text": k, "count_in_text": v} for k, v in missing.items()]
    return c2r, r2c, missing_rows, uncited, len(set(citations))


def reconcile_vancouver(citations: List[str], references: List[RefNum]) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    return reconcile_numeric(citations, references, "ieee")


# ============================================================================
# DOCX Extraction
# ============================================================================

def read_docx_split_main_and_refs(file_bytes: bytes) -> Tuple[str, List[str], str]:
    """Extract main text and reference lines from DOCX."""
    if not DOCX_OK:
        raise RuntimeError("python-docx not installed")
    
    lines = _extract_docx_lines(file_bytes)
    if not lines:
        doc = Document(io.BytesIO(file_bytes))
        lines = list(_iter_docx_text(doc))
    
    return _split_main_and_refs(lines)


def _extract_docx_lines(file_bytes: bytes) -> List[str]:
    """Extract text lines from DOCX XML."""
    import zipfile
    import xml.etree.ElementTree as ET
    
    NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    
    def extract_from_xml(xml_bytes: bytes) -> List[str]:
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
    
    targets = ["word/document.xml", "word/footnotes.xml", "word/endnotes.xml"]
    
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
        names = set(z.namelist())
        for name in names:
            if name.startswith("word/header") and name.endswith(".xml"):
                targets.append(name)
            if name.startswith("word/footer") and name.endswith(".xml"):
                targets.append(name)
        
        lines = []
        for t in set(targets):
            if t in names:
                try:
                    lines.extend(extract_from_xml(z.read(t)))
                except Exception:
                    continue
    return lines


def _iter_docx_text(doc: "Document"):
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


def _split_main_and_refs(lines: List[str]) -> Tuple[str, List[str], str]:
    """Split lines into main text and references."""
    in_refs = False
    heading_line = ""
    main_lines = []
    ref_lines = []
    
    def is_ref_like(line: str) -> bool:
        s = line.strip()
        if not s:
            return False
        if re.match(r"^\s*(\[\s*\d{1,4}\s*\]|\(\s*\d{1,4}\s*\)|\d{1,4}[\.)])\s+\S", s):
            return True
        if YEAR_RE.search(s) and re.match(r"^[A-Z][A-Za-z\-’'\.]+", s):
            return True
        return False
    
    def lookahead_has_refs(idx: int) -> bool:
        seen = 0
        for j in range(idx + 1, min(idx + 30, len(lines))):
            if is_ref_like(lines[j]):
                seen += 1
                if seen >= 3:
                    return True
        return False
    
    for i, line in enumerate(lines):
        if not in_refs:
            for pat in REF_HEADINGS:
                if re.search(pat, line, re.I):
                    if lookahead_has_refs(i):
                        in_refs = True
                        heading_line = line
                        break
            if in_refs:
                continue
            
            m = REF_HEADING_RELAXED.search(line)
            if m and m.start() <= 4 and len(line) <= 160:
                tail = line[m.end():].strip(" :-\t")
                if not (tail and re.fullmatch(r"\d{1,4}", tail)) and lookahead_has_refs(i):
                    in_refs = True
                    heading_line = line
                    if tail:
                        ref_lines.append(tail)
                    continue
        
        if in_refs:
            ref_lines.append(line)
        else:
            main_lines.append(line)
    
    msg = f"Found References heading: {heading_line}" if in_refs else "No References heading found."
    return "\n".join(main_lines).strip(), ref_lines, msg


def _merge_reference_lines(raw_lines: List[str]) -> List[str]:
    """Merge multi-line references into single entries."""
    raw_lines = [ln.strip() for ln in raw_lines if ln and ln.strip()]
    if not raw_lines:
        return []
    
    merged = []
    cur = ""
    for ln in raw_lines:
        s = ln.strip()
        if not s:
            continue
        
        is_new = bool(re.match(r"^\[\s*\d{1,4}\s*\]", s)) or bool(re.match(r"^(\d{1,4})[\.)]", s))
        if is_new:
            if cur:
                merged.append(norm_space(cur))
            cur = s
        else:
            cur = cur + " " + s if cur else s
    
    if cur:
        merged.append(norm_space(cur))
    
    return [m for m in merged if m and len(m) >= 8]


def _split_embedded_numeric_refs(merged: List[str]) -> List[str]:
    """Split embedded numeric references."""
    out = []
    for s in merged:
        s = s.strip()
        if not s:
            continue
        
        parts = re.split(r'(?=\[\s*\d{1,4}\s*\]|\b\d{1,4}[\.\)]\s+)', s)
        out.extend([p.strip() for p in parts if p.strip() and len(p.strip()) >= 10])
    return out


# ============================================================================
# SUGGESTION ENGINE
# ============================================================================

@dataclass
class FixSuggestion:
    original: str
    suggested: str
    fix_type: str
    confidence: float
    reason: str


def generate_suggestions(
    citations: List[str],
    c2r: List[Dict[str, Any]],
    missing_rows: List[Dict[str, Any]],
    references: List[RefAY],
    ref_map: Dict[str, str]
) -> Dict[str, Any]:
    """Master Suggestion Engine - non-invasive, UI-ready."""
    
    citation_suggestions = []
    seen = set()
    
    for citation in citations[:500]:  # Limit for performance
        if citation in seen:
            continue
        seen.add(citation)
        suggestion = _generate_citation_fixes(citation, references, ref_map)
        if suggestion:
            citation_suggestions.append({
                "citation": suggestion.original,
                "suggested": suggestion.suggested,
                "type": suggestion.fix_type,
                "confidence": suggestion.confidence,
                "reason": suggestion.reason,
                "action": "review_required"
            })
    
    missing_suggestions = []
    seen_missing = set()
    for missing in missing_rows[:200]:
        citation = missing.get("citation_in_text", "")
        if citation and citation not in seen_missing:
            seen_missing.add(citation)
            suggestion = _generate_citation_fixes(citation, references, ref_map)
            if suggestion:
                missing_suggestions.append({
                    "citation": suggestion.original,
                    "suggested": suggestion.suggested,
                    "type": suggestion.fix_type,
                    "confidence": suggestion.confidence,
                    "reason": suggestion.reason,
                    "action": "add_reference"
                })
    
    unmatched_suggestions = []
    seen_unmatched = set()
    for item in c2r[:500]:
        if item.get("status") == "not_found":
            citation = item.get("in_text", "")
            if citation and citation not in seen_unmatched:
                seen_unmatched.add(citation)
                suggestion = _generate_citation_fixes(citation, references, ref_map)
                if suggestion:
                    unmatched_suggestions.append({
                        "citation": suggestion.original,
                        "suggested": suggestion.suggested,
                        "type": suggestion.fix_type,
                        "confidence": suggestion.confidence,
                        "reason": suggestion.reason,
                        "action": "review_required"
                    })
    
    reference_suggestions = []
    for ref in references[:200]:
        ref_suggestions = _generate_reference_fixes(ref)
        for s in ref_suggestions:
            reference_suggestions.append({
                "original": s.original,
                "suggested": s.suggested,
                "type": s.fix_type,
                "confidence": s.confidence,
                "reason": s.reason,
                "action": "optional_fix"
            })
    
    all_suggestions = citation_suggestions + missing_suggestions + unmatched_suggestions + reference_suggestions
    
    high_conf = [s for s in all_suggestions if s["confidence"] >= 0.85]
    med_conf = [s for s in all_suggestions if 0.70 <= s["confidence"] < 0.85]
    low_conf = [s for s in all_suggestions if s["confidence"] < 0.70]
    
    by_type = {}
    for s in all_suggestions:
        by_type[s["type"]] = by_type.get(s["type"], 0) + 1
    
    return {
        "citations": citation_suggestions,
        "missing": missing_suggestions,
        "unmatched": unmatched_suggestions,
        "references": reference_suggestions,
        "statistics": {
            "total": len(all_suggestions),
            "high_confidence": len(high_conf),
            "medium_confidence": len(med_conf),
            "low_confidence": len(low_conf),
            "by_type": by_type
        },
        "summary": {
            "auto_fixable": len(high_conf),
            "needs_review": len(med_conf) + len(low_conf)
        }
    }


def _generate_citation_fixes(
    citation: str, 
    references: List[RefAY],
    ref_map: Dict[str, str]
) -> Optional[FixSuggestion]:
    """Generate fix suggestions for problematic citations."""
    
    parsed = _parse_author_year_from_cite(citation)
    if not parsed:
        return None
    
    auth, year = parsed
    
    # Fix malformed year (204 -> 2024)
    if len(year) < 4 and year.isdigit():
        year_int = int(year)
        possible_years = []
        if len(year) == 3:
            possible_years = [2000 + year_int, 2000 + year_int + 10, 2000 + year_int + 20, 1900 + year_int]
        elif len(year) == 2:
            possible_years = [2000 + year_int, 1900 + year_int]
        elif len(year) == 1:
            possible_years = [2000 + year_int, 2000 + year_int + 10, 2000 + year_int + 20]
        
        author_variations = [auth]
        if ' & ' in auth:
            author_variations.extend(auth.split(' & '))
        if ' and ' in auth:
            author_variations.extend(auth.split(' and '))
        
        for alt_year in possible_years:
            alt_year_str = str(alt_year)
            for test_auth in author_variations:
                test_auth = test_auth.strip()
                if not test_auth:
                    continue
                alt_key = f"{test_auth}|{alt_year_str}".lower()
                if alt_key in ref_map:
                    alt_citation = re.sub(r'\b' + re.escape(year) + r'\b', alt_year_str, citation)
                    return FixSuggestion(
                        original=citation,
                        suggested=alt_citation,
                        fix_type="year_malformed",
                        confidence=0.90,
                        reason=f"Malformed year '{year}' corrected to '{alt_year_str}'"
                    )
    
    # Year typo (off by 1 or more)
    if len(year) == 4 and year.isdigit():
        try:
            year_int = int(year[:4])
            for offset in [-5, -4, -3, -2, -1, 1, 2, 3, 4, 5]:
                alt_year = str(year_int + offset)
                if len(alt_year) != 4:
                    continue
                alt_key = f"{auth}|{alt_year}".lower()
                if alt_key in ref_map:
                    alt_citation = citation.replace(year, alt_year)
                    confidence = 0.95 if abs(offset) <= 2 else 0.80
                    return FixSuggestion(
                        original=citation,
                        suggested=alt_citation,
                        fix_type="year_typo",
                        confidence=confidence,
                        reason=f"Year {year} corrected to {alt_year} (off by {abs(offset)})"
                    )
        except (ValueError, TypeError):
            pass
    
    return None


def _generate_reference_fixes(ref: RefAY) -> List[FixSuggestion]:
    """Generate fix suggestions for reference entries."""
    suggestions = []
    ref_text = ref.reference_full
    
    # Add DOI prefix if missing
    if "doi:" not in ref_text.lower() and "https://doi.org" not in ref_text.lower():
        doi_match = _DOI_RE.search(ref_text)
        if doi_match:
            doi = doi_match.group(0)
            fixed = re.sub(rf"({re.escape(doi)})", r"DOI: \1", ref_text, flags=re.I)
            if fixed != ref_text:
                suggestions.append(FixSuggestion(
                    original=ref_text,
                    suggested=fixed,
                    fix_type="add_doi_prefix",
                    confidence=0.95,
                    reason="Added 'DOI:' prefix"
                ))
    
    # Add missing period at end
    if ref_text and not ref_text.rstrip().endswith('.'):
        suggestions.append(FixSuggestion(
            original=ref_text,
            suggested=ref_text.rstrip() + '.',
            fix_type="add_period",
            confidence=0.60,
            reason="Added trailing period"
        ))
    
    return suggestions


# ============================================================================
# Main Public API
# ============================================================================

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
    """Main entry point for cross-checking citations and references."""
    
    name = filename.lower().strip()
    style_s = style.lower().strip()
    is_numeric = "ieee" in style_s or "vancouver" in style_s
    style_hint = "numeric" if is_numeric else "apa"
    
    # Extract document content
    if name.endswith(".docx"):
        main_text, ref_block_lines, ref_msg = read_docx_split_main_and_refs(file_bytes)
        references_raw = _merge_reference_lines(ref_block_lines)
        if style_hint == "numeric":
            references_raw = _split_embedded_numeric_refs(references_raw)
    elif name.endswith(".pdf"):
        try:
            pdf_data = process_pdf(file_bytes)
            main_text = pdf_data["main_text"]
            references_raw = pdf_data["references"]
            ref_msg = f"PDF converted. Found {len(references_raw)} references."
        except Exception as e:
            return {"error": "PDF conversion failed", "note": str(e), "filename": filename}
    else:
        return {"error": "Upload a DOCX or PDF"}
    
    # Process based on style
    if style_hint == "apa":
        cites = extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw if r]
        refs = [r for r in refs if r]
        
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_author_year(cites, refs)
        ref_count = len(refs)
        
    elif style_s == "ieee":
        cites = extract_ieee_citations(main_text)
        refs = [parse_reference_numeric(r, "ieee") for r in references_raw if r]
        refs = [r for r in refs if r]
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(cites, refs, "ieee")
        ref_count = len(refs)
        
    elif style_s == "vancouver":
        cites = extract_vancouver_citations(main_text)
        refs = parse_vancouver_references(references_raw)
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_vancouver(cites, refs)
        ref_count = len(refs)
        
    else:
        # Default to APA
        cites = extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw if r]
        refs = [r for r in refs if r]
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_author_year(cites, refs)
        ref_count = len(refs)
    
    missing_unique = len(missing_rows)
    match_rate = round(100 * (intext_count - missing_unique) / max(intext_count, 1), 1)
    
    return {
        "filename": filename,
        "style": style_s,
        "main_text": main_text,
        "engine_build": ENGINE_BUILD,
        "reference_detection_message": ref_msg,
        "summary": {
            "in_text_citations_found": intext_count,
            "reference_entries_found": ref_count,
            "missing_in_references": missing_unique,
            "uncited_references": len(uncited_refs),
            "match_rate": match_rate,
        },
        "missing_in_references": missing_rows,
        "uncited_references": uncited_refs,
        "reconciliation_intext_to_reference": c2r,
        "reconciliation_reference_to_intext": r2c,
        "references_raw": references_raw,
    }


def run_crosscheck_with_autofix(
    file_bytes: bytes,
    filename: str,
    style: str = "apa",
    verify_online: bool = False,
    verify_mode: str = "all",
    max_verify: int = 0,
    throttle_s: float = 0.12,
    use_crossref: bool = True,
    use_openalex: bool = True,
    enable_autofix: bool = False,
) -> Dict[str, Any]:
    """Run crosscheck with auto-fix suggestions (non-invasive)."""
    
    result = run_crosscheck(
        file_bytes=file_bytes,
        filename=filename,
        style=style,
        verify_online=verify_online,
        verify_mode=verify_mode,
        max_verify=max_verify,
        throttle_s=throttle_s,
        use_crossref=use_crossref,
        use_openalex=use_openalex
    )
    
    if enable_autofix and "error" not in result and style.lower() not in ["ieee", "vancouver"]:
        references_raw = result.get("references_raw", [])
        refs = [parse_reference_author_year(r) for r in references_raw if r]
        refs = [r for r in refs if r]
        ref_map = {r.key: r.reference_full for r in refs}
        
        main_text = result.get("main_text", "")
        citations = extract_author_year_citations(main_text)
        
        suggestions_data = generate_suggestions(
            citations=citations,
            c2r=result.get("reconciliation_intext_to_reference", []),
            missing_rows=result.get("missing_in_references", []),
            references=refs,
            ref_map=ref_map
        )
        
        result["autofix"] = {
            "enabled": True,
            "suggestions": suggestions_data,
            "summary": suggestions_data["summary"]
        }
    
    return result
