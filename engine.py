# engine.py (COMPLETE - with non-invasive Suggestion Engine)
__version__ = "1.5.8"

import re
import io
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from collections import defaultdict, Counter
try:
    from pdf_to_docx_pipeline import process_pdf
    PDF_PIPELINE_OK = True
except Exception:
    process_pdf = None
    PDF_PIPELINE_OK = False

ENGINE_BUILD = "commercial-2026-05-17-safe-numeric-styles"

# Fuzzy matching (optional)
try:
    from rapidfuzz import fuzz
    FUZZ_OK = True
except Exception:
    fuzz = None
    FUZZ_OK = False

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
    import fitz  # PyMuPDF, used for commercial-grade PDF extraction
    PYMUPDF_OK = True
except Exception:
    fitz = None
    PYMUPDF_OK = False

PDF_PARSE_BUILD = "commercial-pdf-parser-2026-05-15"


# ============================================================================
# Define dataclasses FIRST
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
# Constants and patterns
# ============================================================================

YEAR = r"(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?"
YEAR_RE = re.compile(rf"\b({YEAR})\b", re.I)

DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s]+", re.I)
DOI_ONLY_RE = re.compile(
    r"^\s*(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)?10\.\d{4,9}/\S+\s*$",
    re.I,
)

REF_HEADINGS = [
    r"^\s*references?\s*(?:list)?\s*$",
    r"^\s*bibliograph(?:y|ies)\s*$",
    r"^\s*works\s+cited\s*$",
    r"^\s*literature\s+cited\s*$",
    r"^\s*REFERENCES\s*$",
    r"^\s*BIBLIOGRAPHY\s*$",
    r"^\s*REFERENCES\s*\[.*\]\s*$",
    r"^\s*REFERENCES AND NOTES\s*$",
]

REF_HEADING_RELAXED = re.compile(
    r"^\s*(references?|bibliography|works\s+cited|literature\s+cited|REFERENCES|BIBLIOGRAPHY)\b",
    re.I,
)

DISCOURSE_PREFIXES = {
    "see", "e.g", "eg", "i.e", "ie",
    "as", "in", "for", "from", "to", "at", "on", "by", "with", "within",
    "according", "adapted", "based", "cited", "citing", "reported",
    "like",

    # discourse / transition words
    "however", "similarly", "regrettably", "traditionally", "notably",
    "therefore", "thus", "hence", "consequently", "moreover", "furthermore",
    "additionally", "meanwhile", "nonetheless", "nevertheless", "overall",
    "generally", "specifically", "particularly", "importantly", "indeed",
    "likely", "likewise", "uncertainty", "meanwhile", "firstly", "lastly",
    "finally", "also", "then", "next", "again", "still", "subsequently",
    "previously", "earlier", "later", "recently", "currently", "today",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "last", "initially", "subsequent",
    "comparatively", "conversely", "alternatively", "accordingly",
    "interestingly", "relatedly", "moreso", "state",

    # phrases
    "for instance", "instance",
    "for example", "example",
    "more so",
}

REF_END_HEADINGS = [
    r"^\s*appendix(?:es)?\b",
    r"^\s*annex(?:es)?\b",
    r"^\s*supplement(?:ary)?\b",
    r"^\s*supporting\s+information\b",
    r"^\s*supporting\s+documents?\b",
    r"^\s*additional\s+materials?\b",
    r"^\s*online\s+appendix\b",
]
REF_END_HEADING_RE = re.compile("|".join(REF_END_HEADINGS), re.I)

NON_NAME_AUTHOR_KEYS = {
    # data / method / document words
    "survey", "field", "work", "fieldwork", "fieldwork", "data", "dataset",
    "sample", "sampling", "questionnaire", "respondent", "respondents",
    "interview", "interviews", "observation", "observations",
    "experiment", "experiments", "variable", "variables",

    # research/reporting words
    "table", "tables", "figure", "fig", "figures",
    "chapter", "section", "appendix", "appendices", "annex",
    "equation", "eq", "model", "models", "analysis", "analyses",
    "results", "result", "finding", "findings",
    "method", "methods", "methodology", "discussion",
    "introduction", "conclusion", "study", "studies",
    "paper", "thesis", "dissertation", "report", "policy", "policies",
    "source", "sources", "author", "authors",
    "construct", "constructs", "estimation", "estimated", "estimate", "estimates",
    "trend", "trends", "state", "states", "census", "survey", "surveys",

    # discourse words
    "however", "similarly", "regrettably", "traditionally", "notably",
    "therefore", "thus", "hence", "consequently", "moreover", "furthermore",
    "additionally", "meanwhile", "nonetheless", "nevertheless", "overall",
    "generally", "specifically", "particularly", "importantly", "indeed",
    "instance", "example", "likely", "likewise", "uncertainty",
    "finally", "also", "then", "next", "again", "still", "subsequently",
    "previously", "earlier", "later", "recently", "currently", "today",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "last", "initially", "subsequent",
    "comparatively", "conversely", "alternatively", "accordingly",
    "interestingly", "relatedly", "moreso", "more so",
}

NARRATIVE_SINGLE_TOKENS = {
    "crisis", "war", "scandal", "revolution", "katrina",
    "pandemic", "covid", "covid19", "covid-19",
}

NARRATIVE_PHRASE_PATTERNS = [
    r"\byear\s+on\s+year\b",
    r"\bgrowth\s+rate\b",
    r"\ball\s+share\s+index\b",
    r"\bselected\s+african\s+countries\b",
    r"\btop\s+four\s+african\s+countries\b",
    r"\baccording\s+to\b",
]

_DECADE_YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20\d{2})s\b", re.I)


# -----------------------------
# Safe numeric citation profiles
# -----------------------------
# These profiles add numeric-style support without changing the existing
# APA/Harvard, IEEE and Vancouver branches. Square-bracket and true
# superscript citations are safe by default. Round-bracket numeric citations
# are style-gated because manuscripts use round brackets heavily for statistics.
_SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_NORMAL_DIGITS = "0123456789"
SUPERSCRIPT_TO_NORMAL = str.maketrans(_SUPERSCRIPT_DIGITS + "⁻−–—", _NORMAL_DIGITS + "----")
NORMAL_TO_SUPERSCRIPT = str.maketrans(_NORMAL_DIGITS + "-", _SUPERSCRIPT_DIGITS + "⁻")

SAFE_SQUARE_NUMERIC_STYLES = {
    "ieee_square", "numeric_square", "vancouver_square", "nlm", "nlm_square",
    "elsevier", "elsevier_numbered", "elsevier_square",
    "springer", "springer_numbered", "springer_square",
}

SAFE_SUPERSCRIPT_NUMERIC_STYLES = {
    "ama", "ama_superscript", "nature", "nature_superscript",
    "rsc", "rsc_superscript", "acs_superscript", "numeric_superscript",
}

ROUND_NUMERIC_STYLES = {
    "vancouver_round", "acs_round", "numeric_round",
}

UNSUPPORTED_NUMERIC_NOTE_STYLES = {
    "chicago_notes", "chicago_note", "chicago_notes_bibliography", "notes_bibliography",
}


def _style_token(style: str) -> str:
    s = (style or "").strip().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[\s\-/]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    aliases = {
        "ieee_square_bracket": "ieee_square",
        "vancouver_square_bracket": "vancouver_square",
        "nlm_numbered": "nlm",
        "nlm_square_bracket": "nlm_square",
        "elsevier_numeric": "elsevier_numbered",
        "springer_numeric": "springer_numbered",
        "ama_numbered": "ama_superscript",
        "nature_numbered": "nature_superscript",
        "rsc_numbered": "rsc_superscript",
        "acs_numbered": "acs_superscript",
        "acs_super": "acs_superscript",
        "ama_super": "ama_superscript",
        "nature_super": "nature_superscript",
        "rsc_super": "rsc_superscript",
        "round_numeric": "numeric_round",
        "square_numeric": "numeric_square",
        "superscript_numeric": "numeric_superscript",
    }
    return aliases.get(s, s)


def _is_supported_numeric_style(style: str) -> bool:
    s = _style_token(style)
    return (
        s in SAFE_SQUARE_NUMERIC_STYLES
        or s in SAFE_SUPERSCRIPT_NUMERIC_STYLES
        or s in ROUND_NUMERIC_STYLES
    )


def _numeric_style_forms(style: str) -> set:
    s = _style_token(style)
    forms = set()
    if s in SAFE_SQUARE_NUMERIC_STYLES:
        forms.add("square")
    if s in SAFE_SUPERSCRIPT_NUMERIC_STYLES:
        forms.add("superscript")
    if s in ROUND_NUMERIC_STYLES:
        forms.add("round")
    return forms


def _is_round_numeric_style(style: str) -> bool:
    return _style_token(style) in ROUND_NUMERIC_STYLES


def _is_superscript_numeric_style(style: str) -> bool:
    return _style_token(style) in SAFE_SUPERSCRIPT_NUMERIC_STYLES


def _to_unicode_superscript(text: str) -> str:
    """Preserve DOCX superscript digits as Unicode superscripts for citation detection."""
    out = []
    for ch in text or "":
        if ch in _NORMAL_DIGITS or ch == "-":
            out.append(ch.translate(NORMAL_TO_SUPERSCRIPT))
        else:
            out.append(ch)
    return "".join(out)


# -----------------------------
# Small helpers
# -----------------------------
def norm_space(s: str) -> str:
    s = s or ""

    # Keep true superscript numeric citation markers intact. NFKC would turn
    # ¹²³ into ordinary 123, which makes AMA/Nature/RSC citations unsafe to
    # distinguish from baseline statistical digits.
    protected = {}
    for i, ch in enumerate("⁰¹²³⁴⁵⁶⁷⁸⁹⁻"):
        token = f"@@SUP{i}@@"
        if ch in s:
            protected[token] = ch
            s = s.replace(ch, token)

    s = unicodedata.normalize("NFKC", s)

    for token, ch in protected.items():
        s = s.replace(token, ch)

    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def _fold_diacritics(s: str) -> str:
    """Remove accents/diacritics for matching only, not for display."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _is_doi_only_reference(s: str) -> bool:
    return bool(DOI_ONLY_RE.match(norm_space(s or "")))


def _is_digitised_artifact_line(s: str) -> bool:
    """
    Remove repository/header/footer artefacts that come from digitised theses.
    Kept narrow so legitimate references mentioning universities are preserved.
    """
    x = soft_lower(s or "")
    if not x:
        return False
    if "https://ir.ucc.edu.gh/xmlui" in x:
        return True
    if x.startswith("digitized by sam jonah library"):
        return True
    if x in {"digitized by sam jonah library", "digitised by sam jonah library"}:
        return True
    return False


def _clean_extracted_lines(lines: List[str]) -> List[str]:
    out = []
    for line in lines or []:
        s = norm_space(line)
        if not s:
            continue
        if _is_digitised_artifact_line(s):
            continue
        out.append(s)
    return out


def soft_lower(s: str) -> str:
    return norm_space(s).lower()


def strip_punct(s: str) -> str:
    s = _fold_diacritics(soft_lower(s))
    s = re.sub(r"[“”\"'’`]", "", s)
    s = re.sub(r"[^a-z0-9\s\-&/\u2013\u2014-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

_CLEAN_DISCOURSE_PREFIX_CACHE: Optional[List[str]] = None


def _clean_discourse_prefixes() -> List[str]:
    global _CLEAN_DISCOURSE_PREFIX_CACHE
    if _CLEAN_DISCOURSE_PREFIX_CACHE is None:
        _CLEAN_DISCOURSE_PREFIX_CACHE = sorted(
            {_clean_discourse_token(x) for x in DISCOURSE_PREFIXES if x},
            key=len,
            reverse=True,
        )
    return _CLEAN_DISCOURSE_PREFIX_CACHE


def _clean_discourse_token(s: str) -> str:
    return strip_punct(s).strip(" ,.;:()[]{}")


def _strip_discourse_prefixes(left: str) -> str:
    """
    Remove leading discourse words/phrases before author parsing.

    This catches:
    - "Similarly, Smith, 2020" -> "Smith"
    - "Likely, 2020" -> ""
    - "For example, Adam, 2021" -> "Adam"
    """
    left = norm_space(left)
    if not left:
        return ""

    prefixes = _clean_discourse_prefixes()

    for _ in range(5):
        old = left

        for pref in prefixes:
            if not pref:
                continue

            pat = re.compile(
                r"^\s*" + re.escape(pref) + r"(?:\s*,\s*|\s+|[,:;.\-]+\s*|$)",
                re.I
            )
            left = pat.sub("", left, count=1).strip(" ,;:()[]{}")

            if left != old:
                break

        if left == old:
            break

    return left



_COMMON_CITATION_TYPO_REPLACEMENTS = [
    # Keep these narrow. They correct common OCR/typing errors observed in thesis PDFs.
    (re.compile(r"\bOCED\b", re.I), "OECD"),
    (re.compile(r"\bHail\s+Jr\s+et\s+la\b", re.I), "Hair Jr et al."),
    (re.compile(r"\bHail\s+et\s+la\b", re.I), "Hair et al."),
    (re.compile(r"\bet\s+la\b", re.I), "et al."),
]


def _normalise_common_citation_typos(s: str) -> str:
    s = norm_space(s or "")
    for pat, repl in _COMMON_CITATION_TYPO_REPLACEMENTS:
        s = pat.sub(repl, s)
    s = re.sub(r"\b(Hair)\s+Jr\.?\s+et\s+al\.", r"\1 et al.", s, flags=re.I)
    return s


def _citation_context_is_non_citation(text: str, start: int, end: int, candidate: str = "") -> bool:
    """
    Suppress table notes, figure notes, field-survey notes and source labels.
    These often look like author-year citations but are not references.
    """
    text = text or ""
    candidate_key = strip_punct(candidate or "")
    blocked_candidate_heads = {
        "construct", "author construct", "authors construct",
        "field survey", "survey", "field", "source", "table", "figure",
        "estimation", "estimated", "trend", "state", "policy", "census",
    }
    if candidate_key in blocked_candidate_heads:
        return True

    before = text[max(0, start - 180):start]
    after = text[end:min(len(text), end + 80)]
    ctx = soft_lower(before + " " + after)

    context_patterns = [
        r"source\s*:\s*$",
        r"source\s*:\s*.{0,90}$",
        r"author[’'`s]*\s+construct\s*$",
        r"author[’'`s]*\s+computation\s*$",
        r"field\s+survey\s*$",
        r"estimated\s+from\s+field\s+data\s*$",
        r"table\s+\d+[\w\.:-]*\s*$",
        r"figure\s+\d+[\w\.:-]*\s*$",
        r"valid\s+n\s*\(listwise\)\s*$",
    ]
    return any(re.search(p, ctx, flags=re.I | re.S) for p in context_patterns)


def _split_author_year_chunk(chunk: str) -> List[str]:
    """
    Split malformed/joined APA-Harvard chunks before matching.

    Examples:
    - "OECD, 2019, 2020" -> ["OECD, 2019", "OECD, 2020"]
    - "Aiko & Logan 2014, Besley & Persson, 2014" -> two citations
    - "Integrated Business Establishment Survey II, 2014: Minta, 2020" -> two citations
    """
    s = _normalise_extracted_author_year_citation(chunk)
    if not s:
        return []

    s = re.sub(r"\s*:\s*(?=[A-ZÀ-ÖØ-Þ])", "; ", s)
    years = list(YEAR_RE.finditer(s))
    if len(years) <= 1:
        return [s]

    out: List[str] = []
    last_author = ""
    prev_end = 0

    for ym in years:
        left = s[prev_end:ym.start()].strip(" ,;:()[]{}")
        year = ym.group(1)

        # If this is a second year for the same author, reuse the previous author.
        if not left or not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", left):
            author = last_author
        else:
            # Remove any carry-over punctuation from the prior citation.
            left = re.sub(r"^[,;:\s]+", "", left).strip(" ,;:()[]{}")
            # If the segment still contains an earlier year, keep only text after it.
            earlier_years = list(YEAR_RE.finditer(left))
            if earlier_years:
                left = left[earlier_years[-1].end():].strip(" ,;:()[]{}")
            author = left or last_author

        if author:
            # Convert common institutional report-title variants into usable keys.
            if re.search(r"\bintegrated\s+business\s+establishment\s+survey\b", author, re.I):
                author = "IBES II" if re.search(r"\bII\b", author) else "IBES"
            out.append(norm_space(f"{author}, {year}"))
            last_author = author

        prev_end = ym.end()

    return [x for x in out if x]

def _normalise_extracted_author_year_citation(cite: str) -> str:
    """
    Clean display text for extracted APA/Harvard citations.

    Commercial purpose:
    - Keeps genuine author-year citations.
    - Removes transition/narrative lead-ins that PDF extraction often attaches.

    Examples:
    - "Meanwhile, Claessens and Djankov, 1999" -> "Claessens and Djankov, 1999"
    - "Lastly, Baiden, 2020" -> "Baiden, 2020"
    """
    s = norm_space(cite)
    if not s:
        return ""

    s = _normalise_common_citation_typos(s)

    # Common extraction/typing variants that should not create false misses.
    s = re.sub(r"\bet\s*\.?\s*al\s*\.?", "et al.", s, flags=re.I)
    s = re.sub(r"\bet\s+la\b", "et al.", s, flags=re.I)
    s = re.sub(r"(?<=\S)&", " &", s)
    s = re.sub(r"&(?=\S)", "& ", s)
    # Possessives used as theory labels: Mauss' (1925), Levi-Strauss' (1949).
    s = re.sub(r"\b([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+)'\s*,\s*((?:19|20)\d{2})", r"\1, \2", s)
    s = re.sub(r"\b([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+)'\s*\(\s*((?:19|20)\d{2})\s*\)", r"\1, \2", s)
    # Missing comma before year, especially Hair et al.2020 / Kaspera et al. 2014.
    s = re.sub(r"\b(et\s+al\.)\s*((?:19|20)\d{2}[a-z]?)", r"\1, \2", s, flags=re.I)
    s = re.sub(r"\b([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+(?:\s*(?:&|and)\s*[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+)?)\s+((?:19|20)\d{2}[a-z]?)\b", r"\1, \2", s)
    s = re.sub(r"\s+", " ", s)

    s = s.strip(" ,;:()[]{}")
    s = re.sub(r"^(?:and|but|or)\s+", "", s, flags=re.I).strip(" ,;:")
    s = _strip_discourse_prefixes(s).strip(" ,;:()[]{}")

    # Remove one or more transition words that may remain before the real author.
    noise_words = _clean_discourse_prefixes()

    for _ in range(4):
        old = s
        for word in noise_words:
            if not word:
                continue
            s = re.sub(
                r"^" + re.escape(word) + r"(?:\s*,\s*|\s+|[,:;.\-]+\s*)",
                "",
                s,
                count=1,
                flags=re.I,
            ).strip(" ,;:()[]{}")
            if s != old:
                break
        if s == old:
            break

    # Correct common malformed joins introduced by PDF extraction.
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*,+", ",", s)
    return s.strip(" ,;:()[]{}")


def _citation_has_blocked_narrative_lead(cite: str) -> bool:
    """
    Reject extracted strings where the only author-like token is a narrative word.
    Do not reject when a real author remains after cleaning.
    """
    raw = norm_space(cite)
    cleaned = _normalise_extracted_author_year_citation(raw)
    if not cleaned:
        return True

    ym = YEAR_RE.search(cleaned)
    if not ym:
        return False

    left = cleaned[: ym.start()].strip(" ,;:()[]{}")
    if not left:
        return True

    left_key = strip_punct(left)
    return _is_non_author_key(left_key)


_NON_AUTHOR_BLOCK_CACHE: Optional[set] = None


def _non_author_block() -> set:
    global _NON_AUTHOR_BLOCK_CACHE
    if _NON_AUTHOR_BLOCK_CACHE is None:
        _NON_AUTHOR_BLOCK_CACHE = {
            strip_punct(x)
            for x in (set(NON_NAME_AUTHOR_KEYS) | set(DISCOURSE_PREFIXES))
            if x
        }
    return _NON_AUTHOR_BLOCK_CACHE


def _is_non_author_key(key: str) -> bool:
    """
    Prevent ordinary discourse, method, and document words from becoming author keys.
    """
    k = strip_punct(key)
    if not k:
        return True

    block = _non_author_block()

    extra_phrases = {
        "field survey", "survey field", "survey data", "field data",
        "field work", "fieldwork data", "research survey",
        "questionnaire survey", "sample survey",
        "likely similarly", "similarly likely",
    }

    if k in block or k in extra_phrases:
        return True

    toks = [t for t in k.split() if t]
    if toks and all(t in block for t in toks):
        return True

    if toks and toks[-1] in block and len(toks) <= 3:
        return True

    return False


def _is_bad_author_left(left: str) -> bool:
    """
    Reject full author-left phrases that are clearly not author names.
    """
    l = strip_punct(left)
    if not l:
        return True

    if _is_non_author_key(l):
        return True

    bad_patterns = [
        r"^(field|survey|data|sample|questionnaire)\s+",
        r"\s+(survey|field|data|sample|questionnaire)$",
        r"^(likely|similarly|however|moreover|therefore|thus|hence|meanwhile|lastly|finally|also|then|next|first|second|third|last)$",
    ]

    return any(re.search(p, l, re.I) for p in bad_patterns)
def _base_year(y: str) -> str:
    y = (y or "").strip()
    m = re.match(r"^((?:19|20)\d{2})", y)
    return m.group(1) if m else y


def _surnames_from_author_blob(left: str) -> List[str]:
    """
    Extract surname keys from citation or reference author text.

    Handles:
    - "Adam, 2017" -> ["adam"]
    - "Adam, Frimpong & Boadu, 2017" -> ["adam", "frimpong", "boadu"]
    - "Adam et al., 2017" -> ["adam"]
    - "Adam, A. M., Frimpong, S., & Boadu, M. O. (2017)" -> ["adam", "frimpong", "boadu"]
    """
    s = norm_space(left or "")
    if not s:
        return []

    s = _strip_discourse_prefixes(s)
    if _is_bad_author_left(s):
        return []

    s = re.sub(r"\bet\s*\.?\s*al\.?\b", "", s, flags=re.I)
    s = re.sub(r"(’s|'s)\b", "", s)
    s = re.sub(r"(?<=\S)&", " &", s)
    s = re.sub(r"&(?=\S)", "& ", s)
    s = s.replace("＆", "&")

    out: List[str] = []

    # APA/reference style: Surname, Initials. Capture every surname before initials.
    apa_names = re.findall(
        r"\b([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+)\s*,\s*(?:[A-Z]\.\s*){1,5}",
        s,
    )
    if apa_names:
        out.extend(apa_names)
    else:
        # Citation style: Adam, Frimpong & Boadu; Adam and Boadu; Adam et al.
        tmp = s.replace("&", ",")
        tmp = re.sub(r"\band\b", ",", tmp, flags=re.I)
        parts = [p.strip(" ,.;:()[]{}") for p in tmp.split(",")]
        for p in parts:
            if not p:
                continue
            # Remove initials or isolated capital letters.
            p = re.sub(r"\b[A-Z]\.\b", " ", p)
            p = re.sub(r"\b[A-Z]\b", " ", p)
            words = re.findall(r"[A-ZÀ-ÖØ-Þ]?[A-Za-zÀ-ÖØ-öø-ÿ'’\-]{2,}", p)
            if not words:
                continue
            out.append(words[-1])

    final: List[str] = []
    seen = set()
    for cand in out:
        key = strip_punct(cand)
        if not key or len(key) < 2:
            continue
        if key in {"available", "ssrn", "university", "press", "journal"}:
            continue
        if _is_non_author_key(key):
            continue
        if key not in seen:
            seen.add(key)
            final.append(key)
    return final[:6]

def _looks_like_toc_references_line(s: str, tail: str) -> bool:
    if not s:
        return False
    tail = (tail or "").strip()
    if tail and re.fullmatch(r"\d{1,4}", tail):
        return True
    if re.search(r"\.{2,}\s*\d{1,4}\s*$", s):
        return True
    return False


def _looks_like_heading_line(s: str) -> bool:
    s0 = (s or "").strip()
    if not s0:
        return False
    if len(s0) > 120:
        return False
    if s0.endswith(".") and len(s0) > 25:
        return False
    letters = re.sub(r"[^A-Za-z]", "", s0)
    if letters and letters.isupper() and len(letters) >= 6:
        return True
    if re.match(r"^[A-Z][A-Za-z0-9\s\-,:]{3,}$", s0):
        return True
    return False


def _is_likely_narrative_citation(left: str, year: str, full_cite: str) -> bool:
    l = (left or "").strip()
    if not l:
        return True

    s_full = (full_cite or "").lower()
    for pat in NARRATIVE_PHRASE_PATTERNS:
        if re.search(pat, s_full, flags=re.I):
            return True

    if year and isinstance(year, str) and year.lower().endswith("s"):
        if _DECADE_YEAR_RE.search(full_cite or ""):
            return True

    l_norm = soft_lower(l)
    if re.fullmatch(r"[a-z\-']+", l_norm) and l_norm in NARRATIVE_SINGLE_TOKENS:
        return True

    return False


# -----------------------------
# Reference acceptance
# -----------------------------
_LEAD_NUM_RE = re.compile(r"^\s*(?:\[\s*\d{1,4}\s*\]|\(?\s*\d{1,4}\s*\)?|\d{1,4})\s*[\.)\]]\s*")


def _strip_leading_reference_number(s: str) -> str:
    s0 = norm_space(s)
    s0 = _LEAD_NUM_RE.sub("", s0)
    return s0.strip()


def _looks_like_person_author(s: str) -> bool:
    s0 = norm_space(s)
    if re.search(r"\b[A-Z][A-Za-z'\-]+,\s*(?:[A-Z]\.\s*){1,4}(?:[A-Z]\.\s*)?", s0):
        return True
    if re.search(r"\b[A-Z][A-Za-z'\-]+\s+(?:[A-Z]\.?)\s*(?:[A-Z]\.?)\b", s0):
        return True
    if re.search(r"\b[A-Z][A-Za-z'\-]+\s+et\s+al\.", s0):
        return True
    return False


def _looks_like_org_author(s: str) -> bool:
    s0 = norm_space(s)

    if re.search(r"\(([A-Z]{2,10})\)", s0):
        return True

    head = re.sub(r"[^A-Za-z0-9\s/&\-]", " ", s0)
    toks = [t for t in head.split() if t]
    if toks:
        t0 = toks[0]
        t0_clean = re.sub(r"[^A-Za-z]", "", t0)
        if t0_clean and t0_clean.isupper() and len(t0_clean) >= 2:
            return True

    def titleish(w: str) -> bool:
        wc = re.sub(r"[^A-Za-z]", "", w)
        if not wc:
            return False
        if wc.isupper() and 2 <= len(wc) <= 12:
            return True
        return bool(re.match(r"^[A-Z][a-z]{2,}$", wc))

    run = 0
    best = 0
    for w in toks[:16]:
        if titleish(w):
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best >= 2


def _looks_like_title_piece(s: str) -> bool:
    s0 = norm_space(s)
    if len(s0) < 6:
        return False
    letters = re.findall(r"[A-Za-z]", s0)
    if len(letters) < 5:
        return False
    if re.fullmatch(r"(?i)(?:vol(?:ume)?|issue|no\.?|pp\.?|pages?|doi)\b.*", s0):
        return False
    if re.fullmatch(r"\d{1,4}(?:\s*[-–]\s*\d{1,4})?", s0):
        return False

    word_count = len([w for w in re.split(r"\s+", s0) if w])
    if word_count >= 3:
        return True
    if ":" in s0 or "–" in s0 or "-" in s0:
        return True
    return True


def _is_plausible_reference_entry(s: str) -> bool:
    s0 = _strip_leading_reference_number(s)
    if not s0 or len(s0) < 18:
        return False

    ym = YEAR_RE.search(s0)
    if not ym:
        return False

    left = s0[: ym.start()].strip()
    author_ok = (
        _looks_like_person_author(left)
        or _looks_like_org_author(left)
        or _looks_like_person_author(s0[:120])
        or _looks_like_org_author(s0[:120])
    )
    if not author_ok:
        cue_ok = bool(re.search(r"\b(ssrn|arxiv|working\s+paper|available\s+at|retrieved\s+from|doi|report|policy\s+brief)\b", s0, re.I))
        after = s0[ym.end():].lstrip(" ).,;:-")
        after_title = after.split(".", 1)[0].strip()
        if len(after_title) < 6 and "," in after:
            after_title = after.split(",", 1)[0].strip()

        before = s0[: ym.start()].strip(" .;:-")
        before_parts = [p.strip() for p in before.split(".") if p.strip()]
        before_title = before_parts[-1] if before_parts else ""

        if cue_ok or _looks_like_title_piece(after_title) or _looks_like_title_piece(before_title):
            return True
        return False

    after = s0[ym.end():].lstrip(" ).,;:-")
    after_title = after.split(".", 1)[0].strip()
    if len(after_title) < 6 and "," in after:
        after_title = after.split(",", 1)[0].strip()

    before = s0[: ym.start()].strip(" .;:-")
    before_parts = [p.strip() for p in before.split(".") if p.strip()]
    before_title = before_parts[-1] if before_parts else ""

    return _looks_like_title_piece(after_title) or _looks_like_title_piece(before_title)


def _first_author_or_org_key(author_left: str) -> str:
    s = norm_space(author_left)

    s = _strip_discourse_prefixes(s)

    if _is_bad_author_left(s):
        return ""

    m = re.search(r"\(([A-Z][A-Z0-9/&\-]{1,15})\)", s)
    if m:
        key = strip_punct(m.group(1))
        return "" if _is_non_author_key(key) else key

    s = _strip_leading_reference_number(s)
    s = re.sub(r"\(\s*(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?\s*\).*", "", s).strip()
    s = re.sub(r"(’s|'s)\b", "", s)

    s = _strip_discourse_prefixes(s)

    if _is_bad_author_left(s):
        return ""

    m_si = re.match(r"^\s*([A-Z][A-Za-z'\-]+)\s+[A-Z]{1,3}\b", s)
    if m_si:
        key = strip_punct(m_si.group(1))
        return "" if _is_non_author_key(key) else key

    s0 = re.split(r"\s+(?:&|and|＆)\s+|,", s, maxsplit=1)[0].strip()
    s0 = re.sub(r"\bet\s+al\.?\b", "", s0, flags=re.I).strip()

    if _is_bad_author_left(s0):
        return ""

    toks = [t for t in re.split(r"\s+", s0) if t and re.search(r"[A-Za-z0-9]", t)]
    if not toks:
        return ""

    key = strip_punct(toks[-1])

    if _is_non_author_key(key):
        return ""

    return key


def _org_acronym(text: str) -> str:
    """
    Build an acronym from an organisation name.
    Example: United Nations Conference on Trade and Development -> UNCTAD.
    """
    text = norm_space(text or "")
    if not text:
        return ""

    # If an explicit acronym is given in brackets, prefer it.
    m = re.search(r"\(([A-Z][A-Z0-9/&\-]{1,15})\)", text)
    if m:
        return strip_punct(m.group(1))

    # Preserve all-uppercase author tokens such as IFC, GSS, NEIP.
    head_tokens = re.findall(r"\b[A-Z][A-Z0-9/&\-]{1,15}\b", text)
    if head_tokens:
        joined = "".join(head_tokens)
        if 2 <= len(joined) <= 15:
            return strip_punct(joined)

    words = re.findall(r"\b[A-Za-z][A-Za-z\-]*\b", text)
    stop = {
        "the", "of", "and", "for", "in", "on", "at", "to", "a", "an",
        "from", "with", "by", "department", "ministry", "press", "limited",
    }
    letters = []
    for w in words[:18]:
        wl = w.lower().strip("-")
        if wl in stop:
            continue
        if len(wl) <= 1:
            continue
        letters.append(w[0].lower())

    acr = "".join(letters)
    if 2 <= len(acr) <= 15:
        return acr
    return ""


_INSTITUTIONAL_ALIAS_PHRASES = {
    "oecd": [
        "organisation for economic co-operation and development",
        "organization for economic co-operation and development",
        "organisation for economic cooperation and development",
        "organization for economic cooperation and development",
    ],
    "oced": [
        "organisation for economic co-operation and development",
        "organization for economic co-operation and development",
    ],
    "ifs": [
        "institute for fiscal studies",
        "institute of fiscal studies",
    ],
    "ibes": [
        "integrated business establishment survey",
        "integrated business establishment survey ii",
    ],
    "unctad": [
        "united nations conference on trade and development",
    ],
    "neip": [
        "national entrepreneurship and innovation programme",
        "national entrepreneurship and innovation program",
    ],
    "gifec": [
        "ghana investment fund for electronic communications",
        "ghana investment funds for electronic communication",
        "ghana investment fund for electronic communication",
    ],
    "ifc": [
        "international finance corporation",
    ],
    "gss": [
        "ghana statistical service",
    ],
    "pwc": [
        "pricewaterhousecoopers",
        "price waterhouse coopers",
    ],
    "isser": [
        "institute of statistical social and economic research",
        "institute of statistical, social and economic research",
    ],
    "worldbank": [
        "world bank",
    ],
}


def _institution_acronym_aliases(text: str) -> List[str]:
    raw = norm_space(text or "")
    folded = strip_punct(_fold_diacritics(raw))
    aliases = []

    generic = _org_acronym(raw)
    if generic:
        aliases.append(generic)

    for acr, phrases in _INSTITUTIONAL_ALIAS_PHRASES.items():
        for phrase in phrases:
            if strip_punct(phrase) in folded:
                aliases.append(acr)
                break

    # Also support "Ghana, G. S. S." style malformed institutional references.
    compact_caps = re.sub(r"[^A-Z]", "", raw)
    if 2 <= len(compact_caps) <= 12:
        aliases.append(compact_caps.lower())

    seen = set()
    out = []
    for a in aliases:
        a = strip_punct(a)
        if a and a not in seen and not _is_non_author_key(a):
            seen.add(a)
            out.append(a)
    return out



def _institution_aliases_for_citation_left(left: str) -> List[str]:
    """Return institutional aliases only when the citation-left is institution-like.
    Prevents person names such as "Hair et al." or "Aiko & Logan" becoming acronyms.
    """
    raw = norm_space(left or "")
    if not raw:
        return []
    folded = strip_punct(_fold_diacritics(raw))
    has_explicit_acronym = bool(re.search(r"\b[A-Z]{2,12}\b", raw))
    known_phrase = False
    for phrases in _INSTITUTIONAL_ALIAS_PHRASES.values():
        for phrase in phrases:
            if strip_punct(phrase) in folded:
                known_phrase = True
                break
        if known_phrase:
            break
    org_starts = (
        "organisation", "organization", "institute", "world bank", "ghana statistical",
        "integrated business", "ghana revenue", "international monetary", "transparency international",
    )
    starts_like_org = folded.startswith(org_starts)
    if has_explicit_acronym or known_phrase or starts_like_org:
        return _institution_acronym_aliases(raw)
    return []

def _normalise_author_for_matching(value: str) -> str:
    value = norm_space(value or "")
    value = _fold_diacritics(value)
    value = re.sub(r"\bet\s*\.?\s*al\s*\.?", "", value, flags=re.I)
    value = re.sub(r"(?<=\S)&", " &", value)
    value = re.sub(r"&(?=\S)", "& ", value)
    value = value.replace("&", " and ")
    value = re.sub(r"[^A-Za-z0-9\s\-]", " ", value)
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def _truncate_reference_block(lines: List[str], style_hint: str) -> List[str]:
    out: List[str] = []
    ref_like_seen = 0

    def _is_ref_like(ln: str) -> bool:
        if style_hint == "numeric":
            return _looks_like_new_numeric_reference_start(ln)
        return _looks_like_new_apa_reference_start(ln)

    for i, ln in enumerate(lines):
        s = (ln or "").strip()
        if not s:
            continue

        if _is_ref_like(s):
            ref_like_seen += 1

        if ref_like_seen >= 3 and (
            REF_END_HEADING_RE.search(s)
            or (
                _looks_like_heading_line(s)
                and re.search(r"\b(appendix|appendices|annex|supplement|supporting|additional)\b", s, re.I)
            )
        ):
            look = [x for x in lines[i : i + 25] if (x or "").strip()]
            look_ref = sum(1 for x in look if _is_ref_like((x or "").strip()))
            if look_ref <= 1:
                break

        out.append(ln)

    return out


# -----------------------------
# DOCX extraction
# -----------------------------
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


def _docx_xml_text(file_bytes: bytes) -> List[str]:
    import zipfile
    import xml.etree.ElementTree as ET

    NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

    def _extract_from_xml(xml_bytes: bytes) -> List[str]:
        out: List[str] = []
        try:
            root = ET.fromstring(xml_bytes)
        except Exception:
            return out
        w_val = "{" + NS["w"] + "}val"
        for p in root.findall(".//w:p", NS):
            parts: List[str] = []

            # Run-level extraction preserves superscript citation markers.
            # Plain paragraph text loses this formatting and turns AMA/Nature
            # citations into ordinary digits, which is unsafe to auto-detect.
            runs = p.findall(".//w:r", NS)
            if runs:
                for rnode in runs:
                    vert = rnode.find(".//w:vertAlign", NS)
                    is_super = bool(
                        vert is not None
                        and (vert.attrib.get(w_val, "") or "").lower() == "superscript"
                    )
                    for tnode in rnode.findall(".//w:t", NS):
                        if tnode.text:
                            parts.append(_to_unicode_superscript(tnode.text) if is_super else tnode.text)
            else:
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
    if not DOCX_OK:
        raise RuntimeError("python-docx not installed")

    try:
        lines = _docx_xml_text(file_bytes)
    except Exception:
        lines = []

    if not lines:
        doc = Document(io.BytesIO(file_bytes))
        lines = list(_iter_docx_text(doc))

    lines = _clean_extracted_lines(lines)

    def _ref_like(line: str) -> bool:
        s = (line or "").strip()
        if not s:
            return False
        if re.match(r"^\s*(\[\s*\d{1,4}\s*\]|\(\s*\d{1,4}\s*\)|\d{1,4}[\.)])\s+\S", s):
            return True
        if YEAR_RE.search(s) and re.match(r"^[A-Z][A-Za-z\-’'\.]+", s):
            return True
        if "doi:" in s.lower() or "https://doi.org/" in s.lower():
            return True
        return False

    def _lookahead_is_real_refs(idx: int) -> bool:
        seen = 0
        checked = 0
        j = idx + 1
        while j < len(lines) and checked < 20:
            s = (lines[j] or "").strip()
            j += 1
            if not s:
                continue
            checked += 1
            if _ref_like(s):
                seen += 1
        return seen >= 2

    main_lines: List[str] = []
    ref_lines: List[str] = []
    in_refs = False
    heading_line = ""

    i = 0
    while i < len(lines):
        t = lines[i]

        if not in_refs:
            hit = False
            for pat in REF_HEADINGS:
                if re.search(pat, t, flags=re.I):
                    if _looks_like_toc_references_line(t, ""):
                        break
                    if _lookahead_is_real_refs(i):
                        in_refs = True
                        heading_line = t
                        hit = True
                    break
            if hit:
                i += 1
                continue

            m = REF_HEADING_RELAXED.search(t)
            if m and m.start() <= 4 and len(t) <= 160:
                tail = t[m.end():].strip(" :-\t")
                if _looks_like_toc_references_line(t, tail):
                    main_lines.append(t)
                    i += 1
                    continue
                if _lookahead_is_real_refs(i):
                    in_refs = True
                    heading_line = t
                    if tail:
                        ref_lines.append(tail)
                    i += 1
                    continue

        if in_refs:
            ref_lines.append(t)
        else:
            main_lines.append(t)

        i += 1

    if in_refs:
        ref_lines = _truncate_reference_block(ref_lines, style_hint="apa")
        ref_lines = _truncate_reference_block(ref_lines, style_hint="numeric")

    msg = f"Found References heading: {heading_line}" if in_refs else "No References heading found."
    return "\n".join(main_lines).strip(), ref_lines, msg


# -----------------------------
# PDF extraction (fallback, but process_pdf is preferred)
# -----------------------------
def read_pdf_text(file_bytes: bytes) -> str:
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")

    out: List[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            try:
                text = page.extract_text() or ""
                text = text.replace("\x00", " ")
                text = re.sub(r"-\n", "", text)
                text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
            except Exception:
                text = ""
            out.append(text)
    return "\n".join(out)



# -----------------------------
# Commercial PDF parser for CiteIntegrity
# -----------------------------
def parse_pdf_commercial(
    file_bytes: bytes,
    filename: str = "document.pdf",
    style_hint: str = "apa",
    min_page_chars: int = 120,
) -> Dict[str, Any]:
    """
    Commercial-grade PDF parsing wrapper for CiteIntegrity.

    Design:
    - Runs the existing PDF-to-DOCX pipeline when available.
    - Runs a hybrid page-level PyMuPDF/pdfplumber extraction.
    - Scores all candidates and chooses the strongest one.
    - Returns PDF quality metadata and caution warnings for the UI.

    Important product rule:
    Even strong PDF parsing is not treated as equal to DOCX. The returned
    pdf_quality field should be shown to users when the file is a PDF.
    """
    warnings: List[str] = []
    candidates: List[Dict[str, Any]] = []

    if not file_bytes:
        return {
            "ok": False,
            "main_text": "",
            "references": [],
            "ref_msg": "PDF parsing failed: empty file.",
            "pdf_quality": _pdf_quality_report(filename, 0, "none", "", [], [], ["No PDF bytes received."]),
            "pdf_warnings": ["No PDF bytes received."],
        }

    # Candidate 1: existing conversion pipeline, if installed.
    if PDF_PIPELINE_OK and process_pdf is not None:
        try:
            converted = process_pdf(file_bytes)
            conv_main = norm_space(converted.get("main_text", ""))
            conv_refs = converted.get("references", []) or []
            conv_refs = [norm_space(str(r)) for r in conv_refs if norm_space(str(r))]
            if style_hint == "numeric":
                conv_refs = _split_embedded_numeric_refs(conv_refs)
            if conv_main or conv_refs:
                candidates.append(_make_pdf_candidate(
                    source="pdf_to_docx_pipeline",
                    main_text=conv_main,
                    references=conv_refs,
                    page_texts=[],
                    style_hint=style_hint,
                    message=f"PDF conversion pipeline extracted {len(conv_refs)} references.",
                ))
        except Exception as exc:
            warnings.append(f"PDF conversion pipeline failed: {exc}")
    else:
        warnings.append("PDF conversion pipeline is not available. Hybrid text extraction was used.")

    # Candidate 2: hybrid page-level extraction.
    try:
        hybrid = _extract_pdf_hybrid_text(file_bytes)
        hybrid_text = hybrid.get("text", "")
        page_texts = hybrid.get("page_texts", []) or []
        page_engines = hybrid.get("page_engines", []) or []
        if hybrid_text:
            h_main, h_refs, h_msg = _split_pdf_text_main_refs(
                hybrid_text,
                style_hint=style_hint,
            )
            if style_hint == "numeric":
                h_refs = _split_embedded_numeric_refs(h_refs)
            candidates.append(_make_pdf_candidate(
                source="hybrid_pymupdf_pdfplumber",
                main_text=h_main,
                references=h_refs,
                page_texts=page_texts,
                style_hint=style_hint,
                message=f"{h_msg} Hybrid extraction used {len(set(page_engines))} engine(s).",
            ))
    except Exception as exc:
        warnings.append(f"Hybrid PDF extraction failed: {exc}")

    if not candidates:
        warnings.append("No usable text could be extracted from the PDF. The file may be scanned or image-based.")
        return {
            "ok": False,
            "main_text": "",
            "references": [],
            "ref_msg": "PDF parsing failed. Upload the DOCX version for reliable analysis.",
            "pdf_quality": _pdf_quality_report(filename, 0, "none", "", [], [], warnings),
            "pdf_warnings": warnings,
        }

    best = max(candidates, key=lambda c: c.get("score", 0.0))

    # Use the strongest reference list if it is clearly better than the selected candidate.
    best_ref_candidate = max(candidates, key=lambda c: len(c.get("references", []) or []))
    if len(best_ref_candidate.get("references", []) or []) > len(best.get("references", []) or []) + 2:
        best["references"] = best_ref_candidate.get("references", [])
        best["message"] = (
            f"{best.get('message', '')} Reference list strengthened using "
            f"{best_ref_candidate.get('source', 'another PDF parser')} extraction."
        )

    main_text = best.get("main_text", "") or ""
    references = best.get("references", []) or []
    page_texts = best.get("page_texts", []) or []

    if style_hint == "apa":
        references = [r for r in references if _is_plausible_reference_entry(r)]
    else:
        references = [r for r in references if norm_space(r)]

    references = _dedupe_keep_order(references)

    quality = _pdf_quality_report(
        filename=filename,
        page_count=len(page_texts),
        source=best.get("source", "unknown"),
        main_text=main_text,
        references=references,
        page_texts=page_texts,
        warnings=warnings,
    )

    warnings = list(dict.fromkeys(warnings + quality.get("warnings", [])))

    ref_msg = (
        f"PDF parsed with commercial hybrid parser ({best.get('source', 'unknown')}). "
        f"Found {len(references)} references. "
        f"PDF quality: {quality.get('extraction_quality', 'unknown')} "
        f"({quality.get('confidence_score', 0)}%). "
        f"{best.get('message', '')}"
    ).strip()

    return {
        "ok": True,
        "main_text": main_text,
        "references": references,
        "ref_msg": ref_msg,
        "pdf_quality": quality,
        "pdf_warnings": warnings,
        "pdf_parser_build": PDF_PARSE_BUILD,
    }


def _make_pdf_candidate(
    source: str,
    main_text: str,
    references: List[str],
    page_texts: List[str],
    style_hint: str,
    message: str,
) -> Dict[str, Any]:
    main_text = _normalise_pdf_extracted_text(main_text or "")
    references = [norm_space(str(r)) for r in (references or []) if norm_space(str(r))]
    references = _dedupe_keep_order(references)

    citation_count = len(extract_author_year_citations(main_text)) if style_hint == "apa" else len(extract_ieee_citations(main_text))
    word_count = len(re.findall(r"\b[\w'-]+\b", main_text))
    char_count = len(main_text)

    score = 0.0
    score += min(char_count / 5000.0, 20.0)
    score += min(word_count / 1000.0, 20.0)
    score += min(len(references) * 2.5, 35.0)
    score += min(citation_count * 0.75, 20.0)

    if source == "pdf_to_docx_pipeline":
        score += 3.0
    if page_texts:
        weak_ratio = sum(1 for p in page_texts if len(norm_space(p)) < 120) / max(1, len(page_texts))
        score -= min(weak_ratio * 20.0, 20.0)

    return {
        "source": source,
        "main_text": main_text,
        "references": references,
        "page_texts": page_texts,
        "score": round(max(score, 0.0), 3),
        "message": message,
    }


def _extract_pdf_hybrid_text(file_bytes: bytes) -> Dict[str, Any]:
    pymu_pages = _extract_pdf_pages_pymupdf(file_bytes) if PYMUPDF_OK else []
    plumber_pages = _extract_pdf_pages_pdfplumber(file_bytes) if PDF_OK else []

    page_count = max(len(pymu_pages), len(plumber_pages))
    selected_pages: List[str] = []
    page_engines: List[str] = []

    for i in range(page_count):
        choices: List[Tuple[str, str, float]] = []
        if i < len(pymu_pages):
            txt = pymu_pages[i]
            choices.append(("pymupdf", txt, _score_pdf_page_text(txt)))
        if i < len(plumber_pages):
            txt = plumber_pages[i]
            choices.append(("pdfplumber", txt, _score_pdf_page_text(txt)))

        if not choices:
            selected_pages.append("")
            page_engines.append("none")
            continue

        engine, text, _score = max(choices, key=lambda item: item[2])
        selected_pages.append(_clean_pdf_page_text(text))
        page_engines.append(engine)

    selected_pages = _remove_repeated_pdf_headers_footers(selected_pages)

    full_text = "\n\n".join(
        f"[PAGE {i + 1}]\n{txt.strip()}"
        for i, txt in enumerate(selected_pages)
        if txt and txt.strip()
    )

    return {
        "text": _normalise_pdf_extracted_text(full_text),
        "page_texts": selected_pages,
        "page_engines": page_engines,
    }


def _extract_pdf_pages_pymupdf(file_bytes: bytes) -> List[str]:
    pages: List[str] = []
    if not PYMUPDF_OK or fitz is None:
        return pages

    doc = None
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        for page in doc:
            block_text = ""
            plain_text = ""
            try:
                blocks = page.get_text("blocks", sort=True) or []
                block_parts: List[Tuple[float, float, str]] = []
                for block in blocks:
                    if len(block) < 5:
                        continue
                    x0, y0, _x1, _y1, txt = block[:5]
                    block_type = block[6] if len(block) >= 7 else 0
                    if block_type != 0:
                        continue
                    txt = _clean_pdf_page_text(str(txt))
                    if txt:
                        block_parts.append((float(y0), float(x0), txt))
                block_parts.sort(key=lambda item: (item[0], item[1]))
                block_text = "\n".join(part[2] for part in block_parts)
            except Exception:
                block_text = ""

            try:
                plain_text = page.get_text("text", sort=True) or ""
            except Exception:
                plain_text = ""

            best = block_text if _score_pdf_page_text(block_text) >= _score_pdf_page_text(plain_text) else plain_text
            pages.append(_clean_pdf_page_text(best))
    finally:
        try:
            if doc is not None:
                doc.close()
        except Exception:
            pass

    return pages


def _extract_pdf_pages_pdfplumber(file_bytes: bytes) -> List[str]:
    pages: List[str] = []
    if not PDF_OK:
        return pages

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text_default = ""
            text_layout = ""
            try:
                text_default = page.extract_text(x_tolerance=1.5, y_tolerance=3, layout=False) or ""
            except Exception:
                text_default = ""
            try:
                text_layout = page.extract_text(x_tolerance=1.5, y_tolerance=3, layout=True) or ""
            except Exception:
                text_layout = ""

            best = text_layout if _score_pdf_page_text(text_layout) > _score_pdf_page_text(text_default) else text_default
            pages.append(_clean_pdf_page_text(best))

    return pages


def _split_pdf_text_main_refs(text: str, style_hint: str = "apa") -> Tuple[str, List[str], str]:
    text = _normalise_pdf_extracted_text(text)
    lines = [ln.strip() for ln in text.splitlines() if ln and ln.strip()]

    if not lines:
        return "", [], "No extractable PDF text was found."

    idx, tail = _find_reference_heading(lines, style_hint=style_hint)
    if idx >= 0:
        main_lines = lines[:idx]
        ref_lines = []
        if tail:
            ref_lines.append(tail)
        ref_lines.extend(lines[idx + 1:])
        ref_lines = _truncate_reference_block(ref_lines, style_hint=style_hint)
        refs = _merge_reference_lines(ref_lines, style_hint=style_hint)
        if style_hint == "numeric":
            refs = _split_embedded_numeric_refs(refs)
        refs = _dedupe_keep_order(refs)
        return "\n".join(main_lines).strip(), refs, "Reference heading detected in PDF text."

    recovered = recover_references_for_verification(text, style_hint=style_hint)
    recovered = _dedupe_keep_order(recovered)
    msg = "No reliable reference heading detected in PDF text. Used reference recovery heuristics."
    return text, recovered, msg


def _normalise_pdf_extracted_text(text: str) -> str:
    text = text or ""
    text = text.replace("\x00", " ")
    text = text.replace("\u00a0", " ")
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl")
    text = text.replace("\u2019", "'")
    text = re.sub(r"([A-Za-z])-\s*\n\s*([a-z])", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_pdf_page_text(text: str) -> str:
    text = _normalise_pdf_extracted_text(text)
    lines: List[str] = []
    for line in text.splitlines():
        s = norm_space(line)
        if not s:
            lines.append("")
            continue
        if re.fullmatch(r"\d{1,4}", s):
            continue
        if _is_digitised_artifact_line(s):
            continue
        s = re.sub(r"\s+", " ", s)
        lines.append(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _score_pdf_page_text(text: str) -> float:
    text = _clean_pdf_page_text(text)
    if not text:
        return 0.0

    chars = len(text)
    words = len(re.findall(r"\b[\w'-]+\b", text))
    years = len(YEAR_RE.findall(text))
    citations = len(re.findall(r"\([^)]{1,180}\b(?:19|20)\d{2}[a-z]?\b[^)]{0,180}\)", text))
    doi_count = len(re.findall(r"\b10\.\d{4,9}/\S+", text, flags=re.I))
    replacement = text.count(" ")
    odd_spacing = len(re.findall(r"\b[A-Za-z]\s+[A-Za-z]\s+[A-Za-z]\b", text))

    score = 0.0
    score += min(chars / 500.0, 10.0)
    score += min(words / 100.0, 10.0)
    score += min(years * 0.5, 5.0)
    score += min(citations * 1.0, 5.0)
    score += min(doi_count * 1.5, 5.0)
    score -= min(replacement * 0.5, 5.0)
    score -= min(odd_spacing * 0.35, 6.0)
    return round(max(score, 0.0), 3)


def _remove_repeated_pdf_headers_footers(page_texts: List[str]) -> List[str]:
    if len(page_texts) < 3:
        return page_texts

    counts: Dict[str, int] = defaultdict(int)
    for text in page_texts:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for line in (lines[:4] + lines[-4:]):
            key = _normalise_repeated_pdf_line(line)
            if 4 <= len(key) <= 120:
                counts[key] += 1

    threshold = max(3, int(len(page_texts) * 0.45))
    repeated = {k for k, v in counts.items() if v >= threshold}
    if not repeated:
        return page_texts

    cleaned_pages: List[str] = []
    for text in page_texts:
        kept = []
        for line in text.splitlines():
            key = _normalise_repeated_pdf_line(line)
            if key in repeated:
                continue
            kept.append(line)
        cleaned_pages.append("\n".join(kept).strip())
    return cleaned_pages


def _normalise_repeated_pdf_line(line: str) -> str:
    s = soft_lower(line)
    s = re.sub(r"\bpage\s+\d+\b", "page #", s)
    s = re.sub(r"^\d{1,4}$", "#", s)
    s = re.sub(r"\b\d{1,4}\b", "#", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _pdf_quality_report(
    filename: str,
    page_count: int,
    source: str,
    main_text: str,
    references: List[str],
    page_texts: List[str],
    warnings: List[str],
) -> Dict[str, Any]:
    main_text = main_text or ""
    page_count = int(page_count or len(page_texts or []) or 0)
    total_chars = len(main_text)
    total_words = len(re.findall(r"\b[\w'-]+\b", main_text))
    refs_count = len(references or [])
    citation_count = len(extract_author_year_citations(main_text)) if main_text else 0

    low_text_pages = 0
    if page_texts:
        low_text_pages = sum(1 for p in page_texts if len(norm_space(p)) < 120)
    low_ratio = low_text_pages / max(1, len(page_texts)) if page_texts else (1.0 if total_chars < 500 else 0.0)

    avg_chars = total_chars / max(1, page_count)
    confidence = 100.0
    quality_warnings = list(warnings or [])

    if total_chars < 500 or avg_chars < 120:
        confidence -= 45
        quality_warnings.append("The PDF has very little extractable text. It may be scanned or image-based.")
    elif avg_chars < 350:
        confidence -= 20
        quality_warnings.append("The PDF text extraction is weak on some pages.")

    if low_ratio >= 0.60:
        confidence -= 30
        quality_warnings.append("Most PDF pages have low text extraction quality. DOCX is strongly recommended.")
    elif low_ratio >= 0.30:
        confidence -= 15
        quality_warnings.append("Several PDF pages have low text extraction quality. Some citations may be missed.")

    if refs_count == 0:
        confidence -= 25
        quality_warnings.append("No reference entries were confidently reconstructed from the PDF.")
    elif refs_count < 3:
        confidence -= 10
        quality_warnings.append("Only a small number of reference entries were reconstructed from the PDF.")

    if citation_count == 0 and total_words > 300:
        confidence -= 15
        quality_warnings.append("No author-year in-text citations were confidently detected in the extracted PDF text.")

    confidence = round(max(0.0, min(100.0, confidence)), 2)

    if confidence >= 80 and refs_count > 0:
        extraction_quality = "good"
        pdf_type = "text_based"
    elif confidence >= 55:
        extraction_quality = "moderate"
        pdf_type = "mixed_or_layout_complex"
    else:
        extraction_quality = "poor"
        pdf_type = "scanned_or_poorly_structured"

    if extraction_quality != "good":
        quality_warnings.append("PDF analysis is less reliable than DOCX. Ask the user to upload DOCX for the most accurate report.")

    quality_warnings = list(dict.fromkeys([w for w in quality_warnings if w]))

    return {
        "filename": filename,
        "parser_build": PDF_PARSE_BUILD,
        "source": source,
        "page_count": page_count,
        "total_chars": total_chars,
        "total_words": total_words,
        "average_chars_per_page": round(avg_chars, 2),
        "low_text_pages": low_text_pages,
        "low_text_page_ratio": round(low_ratio, 3),
        "reference_entries_found": refs_count,
        "estimated_author_year_citations": citation_count,
        "pdf_type": pdf_type,
        "extraction_quality": extraction_quality,
        "confidence_score": confidence,
        "recommended_format": "DOCX",
        "caution": "PDF output depends on extraction quality. DOCX remains the recommended format for full CiteIntegrity analysis.",
        "warnings": quality_warnings,
    }

def _looks_like_new_numeric_reference_start(s: str) -> bool:
    s0 = (s or "").strip()
    if not s0:
        return False
    
    if re.match(r"^\[\s*\d{1,4}\s*\]\s+\S", s0):
        return True
    if re.match(r"^\(\s*\d{1,4}\s*\)\s+\S", s0):
        return True
    
    m = re.match(r"^(\d{1,4})[\.)]\s+(.+)$", s0)
    if m:
        num = m.group(1)
        num_int = int(num)
        if 1900 <= num_int <= 2099:
            rest = m.group(2)
            if YEAR_RE.search(rest) or len(rest) > 30:
                return True
            return False
        return True
    
    m = re.match(r"^(\d{1,4})\s+([A-Z].+)$", s0)
    if m:
        num = m.group(1)
        num_int = int(num)
        if 1900 <= num_int <= 2099:
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
        a = re.sub(r"[^A-Za-z,\.\-\s&/\u2013\u2014-]", "", a).strip()
        return len(a) >= 3
    return False


def _count_reference_like(lines: List[str], style_hint: str) -> int:
    c = 0
    for ln in lines:
        s = (ln or "").strip()
        if not s:
            continue
        if style_hint == "numeric":
            if _looks_like_new_numeric_reference_start(s):
                c += 1
        else:
            if _looks_like_new_apa_reference_start(s):
                c += 1
    return c


def _find_reference_heading(lines: List[str], style_hint: str) -> Tuple[int, str]:
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
        lookahead = [ln for ln in lines[i + 1: i + 31] if (ln or "").strip()]
        if _count_reference_like(lookahead, style_hint=style_hint) >= 3:
            return i, tail

    return -1, ""


# ============================================================================
# Reference Extraction Functions
# ============================================================================

def detect_reference_format(lines: List[str], start_idx: int) -> str:
    sample_lines = []
    for i in range(start_idx + 1, min(start_idx + 20, len(lines))):
        line = lines[i].strip()
        if line:
            sample_lines.append(line)
    
    ieee_count = sum(1 for l in sample_lines if re.match(r'^\[\d+\]', l))
    numbered_count = sum(1 for l in sample_lines if re.match(r'^\d+\.', l) and not re.match(r'^\d{4}\.', l))
    apa_count = sum(1 for l in sample_lines if re.search(r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)', l))
    harvard_count = sum(1 for l in sample_lines if re.search(r'[A-Z][a-z]+\s+\(\d{4}[a-z]?\)', l))
    
    formats = {
        'ieee': ieee_count,
        'numbered': numbered_count,
        'apa': apa_count,
        'harvard': harvard_count
    }
    
    best_format = max(formats, key=formats.get)
    return best_format if formats[best_format] > 0 else "unknown"


def join_reference_lines(current: str, next_line: str) -> str:
    if current.endswith('-'):
        return current[:-1] + next_line
    elif re.search(r'[a-z]$', current) and re.search(r'^[a-z]', next_line):
        return current + next_line
    else:
        return current + " " + next_line


def clean_reference(ref: str) -> str:
    ref = re.sub(r'\s+', ' ', ref).strip()
    ref = re.sub(r'-\s+', '', ref)
    ref = re.sub(r'\s+-\s+', '-', ref)
    ref = ref.strip('.,;:')
    return ref


def extract_references_generalized(text: str) -> List[str]:
    lines = text.splitlines()
    
    ref_start = -1
    heading_patterns = [
        r'^\s*REFERENCES\s*$',
        r'^\s*BIBLIOGRAPHY\s*$',
        r'^\s*WORKS\s+CITED\s*$',
        r'^\s*LITERATURE\s+CITED\s*$',
        r'^\s*REFERENCES\s*\[.*\]\s*$',
        r'^\s*REFERENCES AND NOTES\s*$',
    ]
    
    for i, line in enumerate(lines):
        for pattern in heading_patterns:
            if re.search(pattern, line, re.I):
                if i > len(lines) * 0.6:
                    ref_start = i
                    break
        if ref_start != -1:
            break
    
    if ref_start == -1:
        ref_candidates = []
        for i, line in enumerate(lines):
            if i > len(lines) * 0.6:
                line = line.strip()
                if re.match(r'^\[\d+\]\s+[A-Z]\.?\s+[A-Z][a-z]', line):
                    ref_candidates.append((i, line))
                elif re.match(r'^\d+\.\s+[A-Z][a-z]', line) and not re.match(r'^\d{4}\.', line):
                    ref_candidates.append((i, line))
                elif re.search(r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)', line):
                    ref_candidates.append((i, line))
        
        if ref_candidates:
            ref_start = ref_candidates[0][0] - 1
    
    if ref_start == -1:
        return []
    
    references = []
    current_ref = ""
    ref_format = detect_reference_format(lines, ref_start)
    
    for i in range(ref_start + 1, min(ref_start + 500, len(lines))):
        line = lines[i].strip()
        
        if not line and not current_ref:
            continue
        
        if not line:
            if current_ref:
                references.append(clean_reference(current_ref))
                current_ref = ""
            continue
        
        is_new_ref = False
        
        if ref_format == "ieee":
            is_new_ref = bool(re.match(r'^\[\d+\]', line))
        elif ref_format == "numbered":
            is_new_ref = bool(re.match(r'^\d+\.', line)) and not re.match(r'^\d{4}\.', line)
        elif ref_format == "apa":
            is_new_ref = bool(re.search(r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)', line[:100]))
        elif ref_format == "harvard":
            is_new_ref = bool(re.search(r'[A-Z][a-z]+\s+\(\d{4}[a-z]?\)', line[:100]))
        else:
            is_new_ref = (
                bool(re.match(r'^\[\d+\]', line)) or
                (bool(re.match(r'^\d+\.', line)) and not re.match(r'^\d{4}\.', line)) or
                bool(re.search(r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)', line[:100]))
            )
        
        if is_new_ref:
            if current_ref:
                references.append(clean_reference(current_ref))
            current_ref = line
        elif current_ref:
            current_ref = join_reference_lines(current_ref, line)
    
    if current_ref:
        references.append(clean_reference(current_ref))
    
    cleaned_refs = []
    for ref in references:
        if len(ref) > 30 and (
            re.search(r'\d{4}', ref) or
            re.search(r'\[\d+\]', ref) or
            re.search(r'[A-Z][a-z]+,\s+[A-Z]\.', ref)
        ):
            cleaned_refs.append(ref)
    
    return cleaned_refs


def extract_references_pattern_based(text: str) -> List[str]:
    patterns = [
        (r'\[\d+\]\s+[A-Z][A-Za-z\.\s]+,\s+[A-Z][A-Za-z\.\s]+,\s+["“].+?["”]', re.MULTILINE | re.DOTALL),
        (r'^\d+\.\s+[A-Z][A-Za-z\.\s]+,\s+[A-Z][A-Za-z\.\s]+,\s+["“].+?["”]', re.MULTILINE | re.DOTALL),
        (r'[A-Z][a-z]+,\s+[A-Z]\.\s+\(\d{4}\)\.\s+[A-Z][a-zA-Z\s]+\.', re.MULTILINE | re.DOTALL),
    ]
    
    references = []
    for pattern, flags in patterns:
        matches = re.findall(pattern, text, flags)
        references.extend([clean_reference(m) for m in matches if len(m) > 30])
    
    return references


def extract_references_heuristic(text: str) -> List[str]:
    lines = text.splitlines()
    references = []
    current_ref = ""
    
    start_idx = int(len(lines) * 0.7)
    
    for i in range(start_idx, len(lines)):
        line = lines[i].strip()
        if not line:
            if current_ref and len(current_ref) > 30:
                references.append(clean_reference(current_ref))
                current_ref = ""
            continue
        
        has_year = bool(re.search(r'\b(19|20)\d{2}\b', line))
        has_bracket_num = bool(re.search(r'\[\d+\]', line))
        has_author = bool(re.search(r'[A-Z][a-z]+,\s+[A-Z]\.', line))
        has_caps_words = len(re.findall(r'\b[A-Z][a-z]{2,}\b', line)) >= 2
        
        if has_year or has_bracket_num or (has_author and has_caps_words):
            if not current_ref:
                current_ref = line
            else:
                if (has_bracket_num or 
                    (re.match(r'^\d+\.', line) and not re.match(r'^\d{4}\.', line)) or
                    (has_author and len(current_ref) > 50)):
                    if current_ref:
                        references.append(clean_reference(current_ref))
                    current_ref = line
                else:
                    current_ref += " " + line
        elif current_ref:
            current_ref += " " + line
    
    if current_ref and len(current_ref) > 30:
        references.append(clean_reference(current_ref))
    
    return references


def extract_references_enhanced(text: str) -> List[str]:
    refs = extract_references_generalized(text)
    if len(refs) < 5:
        refs = extract_references_pattern_based(text)
    if len(refs) < 5:
        refs = extract_references_heuristic(text)
    return refs

def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for x in items:
        k = norm_space(x).lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(norm_space(x))
    return out


def recover_references_for_verification(text: str, style_hint: str = "apa") -> List[str]:
    if not text:
        return []

    refs = extract_references_enhanced(text)

    if style_hint == "apa":
        refs = [r for r in refs if _is_plausible_reference_entry(r)]
    else:
        refs = [r for r in refs if r and len(norm_space(r)) >= 10]

    refs = _dedupe_keep_order(refs)

    if style_hint == "numeric":
        refs = _split_embedded_numeric_refs(refs)

    return refs
    
def _split_embedded_apa_refs(merged: List[str]) -> List[str]:
    """
    Split reference blocks where PDF/DOCX extraction merged two APA references.

    Improvements:
    - Handles long multi-author APA starts such as:
      "Aryeetey, E., Baah-Nuakoh, A., Duggleby, T., ... (1994)"
    - Handles names with diacritics such as Artüz and Brüggen.
    - Splits only after a boundary that looks like the end of a previous entry.
    """
    out: List[str] = []

    UPPER = r"A-ZÀ-ÖØ-Þ"
    NAME_BODY = r"A-Za-zÀ-ÖØ-öø-ÿ'’\-"
    SURNAME = rf"[{UPPER}][{NAME_BODY}]+"
    INITIALS = r"(?:[A-Z]\.?\s*){1,5}"
    YEAR_IN_PARENS = r"\(\s*(?:1[6-9]\d{2}|20\d{2})[a-z]?\s*\)"
    PERSON = rf"{SURNAME},\s*{INITIALS}"

    # Narrow start, good for simple two-author entries.
    person_start = re.compile(
        rf"(?=(?:{PERSON}(?:(?:,\s*|,\s*&\s*|\s*&\s*|\s+and\s+){PERSON}){{0,12}}\s*{YEAR_IN_PARENS}))"
    )

    # Broad start, catches multi-author blocks when initials/spacing are imperfect.
    person_start_broad = re.compile(
        rf"(?=(?:{SURNAME},\s*.{{1,220}}?{YEAR_IN_PARENS}))"
    )

    # Institutional author APA start: "Ghana Statistical Service. (2021)"
    org_start = re.compile(
        r"(?=(?:[A-Z][A-Za-z&/\-]+(?:\s+[A-Z][A-Za-z&/\-]+){1,12}"
        r"\.\s*\(\s*(?:1[6-9]\d{2}|20\d{2})[a-z]?\s*\)))"
    )

    for ref in merged or []:
        s = norm_space(ref)
        if not s:
            continue

        cuts = []
        for pat in (person_start, person_start_broad, org_start):
            for m in pat.finditer(s):
                pos = m.start()
                if pos <= 0:
                    continue

                # Require the embedded entry to start after a sentence/URL/DOI boundary.
                prefix_window = s[max(0, pos - 12):pos]
                left_part = s[:pos]
                boundary_ok = (
                    bool(re.search(r"[\.\?\!]\s*$", prefix_window))
                    or bool(re.search(r"\b(?:Retrieved from|Available at)\s*$", prefix_window, re.I))
                    or bool(DOI_RE.search(left_part))
                    or bool(re.search(r"https?://\S+\s*$", left_part, re.I))
                )
                if boundary_ok:
                    cuts.append(pos)

        cuts = sorted(set(cuts))
        if not cuts:
            out.append(s)
            continue

        prev = 0
        for pos in cuts:
            part = norm_space(s[prev:pos])
            if part:
                out.append(part)
            prev = pos
        tail = norm_space(s[prev:])
        if tail:
            out.append(tail)

    return [x for x in out if x]

def _clean_reference_list(refs: List[str], style_hint: str = "apa") -> List[str]:
    cleaned: List[str] = []
    for ref in refs or []:
        s = norm_space(str(ref))
        if not s:
            continue
        if _is_digitised_artifact_line(s):
            continue
        if _is_doi_only_reference(s):
            continue
        if style_hint == "apa" and not _is_plausible_reference_entry(s):
            continue
        cleaned.append(s)
    return _dedupe_keep_order(cleaned)



def _looks_like_author_list_continuation(cur: str, line: str) -> bool:
    """
    Detect PDF line wraps inside the author-list part of an APA reference.

    Prevents false standalone references such as:
    - "Chen, W., & Plank, G. (2021)" when the previous line is
      "Afenyo-Agbe, E., Afram, A., ... Sefa-Nyarko, C.,"
    - "Vaz, A. (2012)" when the previous line is
      "Alkire, S., ..., Seymour, G., &"
    - "& Acheampong, P. P. (2023)" when the previous line contains earlier co-authors.
    """
    cur = norm_space(cur or "")
    line = norm_space(line or "")
    if not cur or not line:
        return False

    if YEAR_RE.search(cur):
        return False

    if re.match(r"^(?:&|and|＆)\s+", line, flags=re.I):
        return True

    if re.search(r"(?:,|&|and|＆)\s*$", cur, flags=re.I):
        return True

    prev_author_markers = len(re.findall(
        r"\b[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+,\s*(?:[A-Z]\.?\s*){1,5}",
        cur,
    ))
    next_starts_author = bool(re.match(
        r"^[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+,\s*(?:[A-Z]\.?\s*){1,5}",
        line,
    ))
    if prev_author_markers >= 1 and next_starts_author:
        return True

    return False



def _looks_like_wrapped_apa_reference_start_without_year(line: str) -> bool:
    """
    Detect the first line of a new APA reference where the year appears on the
    next wrapped line. Example:
    "Amponsah, D., Awunyo-Vitor, D., ... Sunday, O. A.,"
    followed by "& Acheampong, P. P. (2023). ...".
    """
    s = norm_space(line or "")
    if not s or YEAR_RE.search(s):
        return False
    if re.match(r"^(?:&|and|＆)\s+", s, flags=re.I):
        return False
    markers = re.findall(
        r"\b[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+,\s*(?:[A-Z]\.?\s*){1,5}",
        s,
    )
    return bool(markers) and bool(re.search(r",\s*$", s))

def _add_alias_once(alias_map: Dict[str, str], key: str, ref: str, prefer: bool = False) -> None:
    """
    Add alias without overwriting earlier, more specific references.
    Use prefer=True only for highly specific aliases such as two-author keys.
    """
    key = (key or "").lower()
    if not key:
        return
    if prefer or key not in alias_map:
        alias_map[key] = ref

def _merge_reference_lines(raw_lines: List[str], style_hint: str = "apa") -> List[str]:
    raw_lines = [ln.strip() for ln in raw_lines if ln and ln.strip()]
    if not raw_lines:
        return []

    merged: List[str] = []
    cur = ""
    for ln in raw_lines:
        s = ln.strip()
        if not s:
            continue

        is_new = (
            _looks_like_new_numeric_reference_start(s)
            or _looks_like_new_apa_reference_start(s)
            or (cur and YEAR_RE.search(cur) and _looks_like_wrapped_apa_reference_start_without_year(s))
        )

        # Important PDF/DOCX repair: do not split a reference in the middle of a
        # wrapped author list. This is what caused Chen & Plank, Vaz, and
        # & Acheampong to appear as separate "uncited references".
        if cur and is_new and _looks_like_author_list_continuation(cur, s):
            is_new = False

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
    if style_hint == "numeric":
        merged = _split_embedded_numeric_refs(merged)
        merged = _clean_reference_list(merged, style_hint="numeric")
    else:
        merged = _split_embedded_apa_refs(merged)
        merged = _clean_reference_list(merged, style_hint="apa")
    return merged


def _split_embedded_numeric_refs(merged: List[str]) -> List[str]:
    out: List[str] = []
    br_pat = re.compile(r"(?=(\[\s*\d{1,4}\s*\]\s+))")
    dot_pat = re.compile(r"(?=(\b\d{1,4}[\.\)]\s+))")

    for s in merged:
        s = (s or "").strip()
        if not s:
            continue

        cuts: List[int] = []

        for m in br_pat.finditer(s):
            pos = m.start(1)
            if pos > 0:
                cuts.append(pos)

        for m in dot_pat.finditer(s):
            pos = m.start(1)
            if pos > 0:
                token = m.group(1).strip()
                num = re.match(r"^(\d{1,4})", token)
                if num and YEAR_RE.fullmatch(num.group(1)):
                    continue
                cuts.append(pos)

        if not cuts:
            out.append(s)
            continue

        cuts = sorted(set(cuts))
        prev = 0
        for pos in cuts:
            part = s[prev:pos].strip()
            if part:
                out.append(part)
            prev = pos
        tail = s[prev:].strip()
        if tail:
            out.append(tail)

    out = [x for x in out if x and len(x) >= 10 and not _is_doi_only_reference(x)]
    out = [x for x in out if not _is_digitised_artifact_line(x)]
    return _dedupe_keep_order(out)


# ============================================================================
# APA/HARVARD STYLE
# ============================================================================

def _parse_author_year_from_cite(cite: str) -> Optional[Tuple[str, str]]:
    """Parse author and year from APA/Harvard citation.
    
    Now handles malformed years (2-3 digits like '204' -> '2024')
    """
    s = norm_space(cite)
    s = _normalise_extracted_author_year_citation(s)
    if not s:
        return None

    s = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", s, flags=re.I).strip()
    
    # First try to match standard 4-digit years
    ym = YEAR_RE.search(s)
    year = None
    year_start = None
    
    if ym:
        year = ym.group(1)
        year_start = ym.start()
    else:
        # Try to find malformed years (2-3 digit numbers that could be years)
        malformed_pat = re.compile(r"[,&]\s*([A-Za-z\s]+?)?\s*(\d{2,3})\s*[\),]")
        malformed_match = malformed_pat.search(s)
        
        if malformed_match:
            year_candidate = malformed_match.group(2)
            if year_candidate.isdigit() and 0 <= int(year_candidate) <= 999:
                year = year_candidate
                year_start = malformed_match.start(2)
        
        if not year:
            standalone_pat = re.compile(r"\b(\d{2,3})\b")
            standalone_match = standalone_pat.search(s)
            if standalone_match:
                year_candidate = standalone_match.group(1)
                if year_candidate.isdigit() and 0 <= int(year_candidate) <= 999:
                    year = year_candidate
                    year_start = standalone_match.start(1)
    
    if not year:
        return None
    
    if year_start:
        left = s[:year_start].strip(" ,;()")
    else:
        left = ""

    if left:
        left = _strip_discourse_prefixes(left)

    for _ in range(3):
        if "," not in left:
            break
        first, rest = left.split(",", 1)
        if re.search(r"\b[A-Z][A-Za-z'\-]+\b", first):
            break
        left = rest.strip(" ,;()")

    left = re.sub(r"(’s|'s)\b", "", left).strip()
    left = _strip_discourse_prefixes(left)

    if _is_bad_author_left(left):
        return None
    if _is_likely_narrative_citation(left, year, s):
        return None

    inst_aliases = _institution_aliases_for_citation_left(left)
    author_key = inst_aliases[0] if inst_aliases else _first_author_or_org_key(left)
    if not author_key:
        return None
    if _is_non_author_key(author_key):
        return None
    return author_key, year


def extract_author_year_citations(text: str) -> List[str]:
    t = (text or "").replace("\u2019", "'")

    # Normalise common OCR/typing issues before citation extraction.
    # Example in UCC thesis exports: "Artüz and and Bayraktar (2021)".
    t = _normalise_common_citation_typos(t)
    t = re.sub(r"\band\s+and\b", "and", t, flags=re.I)
    t = re.sub(r"(?<=\S)&", " &", t)
    t = re.sub(r"&(?=\S)", "& ", t)

    paren_pat = re.compile(r"\(([^()]{0,260}?\b(?:19|20)\d{2}[a-z]?\b[^()]{0,260}?)\)")

    # Unicode-aware name pattern. This prevents surnames such as Artüz, Brüggen,
    # Dženopoljac and Proença from being reduced to the last author only.
    NAME = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+(?:'s)?"
    AMP = r"(?:&|and|＆)"
    AUTHOR_LIST = rf"{NAME}(?:\s*,\s*{NAME}){{0,10}}(?:\s*,?\s*{AMP}\s*{NAME})?"

    narr_pat = re.compile(
        rf"\b("
        rf"(?:{AUTHOR_LIST})"
        rf"|(?:{NAME}\s+{AMP}\s+{NAME})"
        rf"|(?:{NAME}\s+et\s+al\.)"
        rf")\s*\(\s*((?:19|20)\d{{2}}[a-z]?)\s*\)?"
    )

    out: List[str] = []

    for m in paren_pat.finditer(t):
        inside = (m.group(1) or "").strip()
        if YEAR_RE.fullmatch(inside) and not re.search(r"[A-Za-z]", inside):
            continue

        chunks = [c.strip() for c in re.split(r";", inside) if c.strip()]
        for ch in chunks:
            ch2 = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", ch, flags=re.I).strip()
            if not YEAR_RE.search(ch2):
                continue
            if _citation_context_is_non_citation(t, m.start(), m.end(), ch2):
                continue
            for piece in _split_author_year_chunk(ch2):
                if piece and YEAR_RE.search(piece):
                    out.append(norm_space(piece))

    for m in narr_pat.finditer(t):
        author = m.group(1).strip()
        years_block = m.group(2).strip()
    
        if not years_block:
            continue
    
        if _citation_context_is_non_citation(t, m.start(), m.end(), author):
            continue

        author = re.sub(r"(’s|'s)\b", "", author).strip()
        years = re.split(r"[;,]\s*", years_block)
    
        for y in years:
            y = y.strip()
            if YEAR_RE.fullmatch(y):
                out.append(norm_space(f"{author}, {y}"))

    cleaned = []

    for c in out:
        c = _normalise_extracted_author_year_citation(c)
        if not c:
            continue

        if _citation_has_blocked_narrative_lead(c):
            continue

        # Keep repeated occurrences. Reference-to-citation counts must reflect
        # real frequency in the text, not only unique citation strings.
        if _parse_author_year_from_cite(c):
            cleaned.append(c)

    return cleaned


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

    key = f"{author_key}|{year}".lower()
    return RefAY(reference_full=s_clean, key=key)


def reconcile_author_year(citations: List[str], references: List[RefAY]) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    # Do not let duplicate first-author/year keys overwrite earlier entries.
    # Example: Adam (2017) and Adam, Frimpong & Boadu (2017) share "adam|2017".
    # The single-author reference should remain available for "Adam, 2017".
    ref_map: Dict[str, str] = {}
    for r in references:
        _add_alias_once(ref_map, r.key, r.reference_full)
    alias_map: Dict[str, str] = dict(ref_map)

    refs_by_year: Dict[str, List[RefAY]] = defaultdict(list)
    for r in references:
        ym_r = YEAR_RE.search(r.reference_full)
        if ym_r:
            refs_by_year[_base_year(ym_r.group(1))].append(r)

    for r in references:
        try:
            auth, y = r.key.split("|", 1)
        except Exception:
            continue
        by = _base_year(y)
        if by and by != y:
            _add_alias_once(alias_map, f"{auth}|{by}".lower(), r.reference_full)

        s_full = r.reference_full
        ym = YEAR_RE.search(s_full)
        if not ym:
            continue
        year_full = ym.group(1)
        year_base = _base_year(year_full)

        left = s_full[: ym.start()].strip(" ,;()")

        # Institutional acronym aliases, e.g. UNCTAD -> United Nations Conference...
        for alias in _institution_acronym_aliases(left):
            _add_alias_once(alias_map, f"{alias}|{year_full}".lower(), r.reference_full)
            if year_base and year_base != year_full:
                _add_alias_once(alias_map, f"{alias}|{year_base}".lower(), r.reference_full)

        names = _surnames_from_author_blob(left)
        if not names:
            continue

        for nm in names[:2]:
            _add_alias_once(alias_map, f"{nm}|{year_full}".lower(), r.reference_full)
            if year_base and year_base != year_full:
                _add_alias_once(alias_map, f"{nm}|{year_base}".lower(), r.reference_full)

        if len(names) >= 2:
            a, b = names[0], names[1]
            _add_alias_once(alias_map, f"{a}+{b}|{year_full}".lower(), r.reference_full, prefer=True)
            _add_alias_once(alias_map, f"{b}+{a}|{year_full}".lower(), r.reference_full, prefer=True)
            _add_alias_once(alias_map, f"{a}+etal|{year_full}".lower(), r.reference_full, prefer=True)
            if year_base and year_base != year_full:
                _add_alias_once(alias_map, f"{a}+{b}|{year_base}".lower(), r.reference_full, prefer=True)
                _add_alias_once(alias_map, f"{b}+{a}|{year_base}".lower(), r.reference_full, prefer=True)
                _add_alias_once(alias_map, f"{a}+etal|{year_base}".lower(), r.reference_full, prefer=True)

    cite_counts_by_ref = Counter()
    parsed_cites: List[Tuple[str, str, str]] = []
    ambiguous_cite_samples_by_ref: Dict[str, List[str]] = defaultdict(list)

    # Same first-author/year references need special handling.
    # Example:
    #   Adam, D. (2020). Special report...
    #   Adam, A. M. (2020). Sample size determination...
    # A bare in-text citation such as Adam (2020) cannot be assigned safely
    # from author-year alone. Instead of letting the first reference absorb the
    # citation and listing the other as uncited, mark all same-key references
    # as ambiguously cited. The UI can show the ambiguity for manual review.
    refs_by_shared_author_year: Dict[str, List[str]] = defaultdict(list)
    for rr in references:
        try:
            rr_auth, rr_year = rr.key.split("|", 1)
        except Exception:
            continue
        keys_for_rr = {f"{rr_auth}|{rr_year}".lower()}
        rr_base_year = _base_year(rr_year)
        if rr_base_year and rr_base_year != rr_year:
            keys_for_rr.add(f"{rr_auth}|{rr_base_year}".lower())
        for kk in keys_for_rr:
            if rr.reference_full not in refs_by_shared_author_year[kk]:
                refs_by_shared_author_year[kk].append(rr.reference_full)

    def _citation_is_bare_same_author_year(citation_text: str, parsed_author: str, parsed_year: str) -> Tuple[bool, str, List[str]]:
        """Return True when a citation is too generic to choose among same-author/year refs."""
        base_year = _base_year(parsed_year) if parsed_year else parsed_year
        shared_key = f"{parsed_author}|{base_year or parsed_year}".lower()
        group = refs_by_shared_author_year.get(shared_key, [])
        if len(group) <= 1:
            return False, shared_key, group

        s_cite = norm_space(citation_text or "")
        ym_cite = YEAR_RE.search(s_cite)
        left_cite = s_cite[: ym_cite.start()].strip(" ,;()[]{}") if ym_cite else s_cite
        left_cite = _strip_discourse_prefixes(left_cite)
        names_cite = _surnames_from_author_blob(left_cite)

        has_explicit_initials = bool(re.search(r"\b[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+,\s*(?:[A-Z]\.?\s*){1,5}", left_cite))
        has_two_authors = len(names_cite) >= 2 or bool(re.search(r"\s+(?:&|and)\s+", left_cite, flags=re.I))
        has_etal = bool(re.search(r"\bet\s*\.?\s*al\.?\b", s_cite, flags=re.I))

        # Bare examples: "Adam, 2020" or "Adam's (2020)".
        # Non-bare examples: "Adam, A. M., 2020", "Adam & Boateng, 2020",
        # "Adam et al., 2020".
        is_bare = not has_explicit_initials and not has_two_authors and not has_etal
        return is_bare, shared_key, group

    for c in citations:
        parsed = _parse_author_year_from_cite(c)
        if not parsed:
            continue
        
        auth, year = parsed
        year_base = _base_year(year) if len(year) == 4 else year
        
        cand_keys = []

        ym = YEAR_RE.search(c)
        names = []
        if ym:
            left = (c[: ym.start()] or "").strip(" ,;()")
            names = _surnames_from_author_blob(left)

            # For multi-author citations, try the specific two-author key before
            # the generic first-author key. This avoids mapping
            # "Adam, Frimpong & Boadu, 2017" to the single-author Adam (2017).
            if len(names) >= 2:
                cand_keys.append(f"{names[0]}+{names[1]}|{ym.group(1)}".lower())
                cand_keys.append(f"{names[1]}+{names[0]}|{ym.group(1)}".lower())
                if year_base and year_base != ym.group(1):
                    cand_keys.append(f"{names[0]}+{names[1]}|{year_base}".lower())
                    cand_keys.append(f"{names[1]}+{names[0]}|{year_base}".lower())

        if re.search(r"\bet\s+al\.?", c, re.I):
            m = re.search(r"([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+)\s+et\s+al", c, re.I)
            if m:
                first_author = strip_punct(m.group(1))
                cand_keys.append(f"{first_author}+etal|{year}".lower())
                if year_base and year_base != year:
                    cand_keys.append(f"{first_author}+etal|{year_base}".lower())

        cand_keys.append(f"{auth}|{year}".lower())
        if year_base and year_base != year:
            cand_keys.append(f"{auth}|{year_base}".lower())

        if names:
            cand_keys.append(f"{names[0]}|{ym.group(1)}".lower())
            if year_base and year_base != ym.group(1):
                cand_keys.append(f"{names[0]}|{year_base}".lower())

        # Deduplicate candidate keys while preserving priority.
        seen_keys = set()
        cand_keys = [k for k in cand_keys if k and not (k in seen_keys or seen_keys.add(k))]

        # If a bare citation maps to more than one reference with the same
        # first-author/year, do not allow one reference to absorb the count and
        # leave the others as false uncited references.
        # Example: Adam (2020) with Adam, D. (2020) and Adam, A. M. (2020).
        is_bare_ambiguous, ambiguous_key, ambiguous_refs = _citation_is_bare_same_author_year(c, auth, year)
        if is_bare_ambiguous and ambiguous_refs:
            for ambiguous_ref in ambiguous_refs:
                cite_counts_by_ref[ambiguous_ref] += 1
                if len(ambiguous_cite_samples_by_ref[ambiguous_ref]) < 6:
                    ambiguous_cite_samples_by_ref[ambiguous_ref].append(c)
            parsed_cites.append((
                ambiguous_refs[0],
                c,
                f"ambiguous_same_author_year:{ambiguous_key};candidates={len(ambiguous_refs)}"
            ))
            continue

        matched_ref = None
        used_key = None
        for k in cand_keys:
            if k in alias_map:
                matched_ref = alias_map[k]
                used_key = k
                break

        if matched_ref:
            cite_counts_by_ref[matched_ref] += 1
            parsed_cites.append((matched_ref, c, f"alias:{used_key}" if used_key else ""))
        else:
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
                    score_overlap = int(round(100 * (overlap / max(1, len(set(cite_names))))))

                    score = score_overlap
                    if FUZZ_OK and fuzz and cite_names and ref_names:
                        score1 = fuzz.token_set_ratio(" ".join(cite_names), " ".join(ref_names))
                        score2 = fuzz.partial_ratio(" ".join(cite_names), " ".join(ref_names))
                        score_fuzz = int(round(0.6 * score1 + 0.4 * score2))
                        score = max(score, score_fuzz)

                    if score > best_score:
                        best_score = score
                        best_ref = rr.reference_full

            if best_ref and best_score >= 74:
                cite_counts_by_ref[best_ref] += 1
                parsed_cites.append((best_ref, c, f"fuzzy:{best_score}"))
            else:
                parsed_cites.append(("", c, ""))

    c2r: List[Dict[str, Any]] = []
    missing_counter = Counter()

    for matched_ref, c, flags in parsed_cites:
        if matched_ref:
            c2r.append({"status": "matched", "in_text": c, "matched_reference": matched_ref, "flags": flags})
        else:
            c2r.append({"status": "not_found", "in_text": c, "matched_reference": "", "flags": ""})
            missing_counter[c] += 1

    r2c: List[Dict[str, Any]] = []
    uncited_refs: List[str] = []

    cite_samples_by_ref: Dict[str, List[str]] = defaultdict(list)
    for matched_ref, c, _flags in parsed_cites:
        if matched_ref and len(cite_samples_by_ref[matched_ref]) < 6:
            cite_samples_by_ref[matched_ref].append(c)

    ref_cluster_map = _cluster_references(references)

    for r in references:
        ref_full = r.reference_full
        times = int(cite_counts_by_ref.get(ref_full, 0))

        meta = ref_cluster_map.get(ref_full) or {}
        canonical = meta.get("canonical_ref", ref_full)
        is_dup = bool(meta.get("is_duplicate", False))
        cid = meta.get("cluster_id", 0)

        canonical_times = int(cite_counts_by_ref.get(canonical, 0))
        if times == 0 and not (is_dup and canonical_times > 0):
            uncited_refs.append(ref_full)

        r2c.append({
            "times_cited": times,
            "reference": ref_full,
            "cited_by": list(dict.fromkeys(
                (cite_samples_by_ref.get(ref_full, []) or [])
                + (ambiguous_cite_samples_by_ref.get(ref_full, []) or [])
            ))[:6],
            "ambiguous_same_author_year_cited": bool(ambiguous_cite_samples_by_ref.get(ref_full)),
            "cluster_id": cid,
            "canonical_reference": canonical,
            "duplicate_of_cited": bool(is_dup and canonical_times > 0),
        })

    missing_rows = [{"citation_in_text": k, "count_in_text": int(v)} for k, v in missing_counter.most_common()]
    total_intext_count = int(len([c for c in citations if c]))
    return c2r, r2c, missing_rows, uncited_refs, total_intext_count


# ============================================================================
# IEEE STYLE
# ============================================================================

def extract_ieee_citations(text: str) -> List[str]:
    t = text or ""
    out: List[str] = []
    
    t = re.sub(r"(?:table|figure|fig\.?|eq\.?|equation)\s+(\d{1,4})", "", t, flags=re.I)
    t = re.sub(r'\]\s*\n\s*\[', '][', t)
    
    ieee_pat = re.compile(r"\[\s*(\d{1,4})(?:\s*[-–,]\s*(\d{1,4}))?(?:\s*,\s*(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?)?\s*\]")
    
    for m in ieee_pat.finditer(t):
        nums = _expand_citation_range(m)
        out.extend(nums)
    
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def parse_reference_numeric(ref: str, style: str = "ieee") -> Optional[RefNum]:
    s = norm_space(ref)
    if not s:
        return None
    
    style = style.lower()
    is_ieee = style == "ieee"
    
    if is_ieee:
        m = re.match(r"^\[\s*(\d{1,4})\s*\]\s*(.+)$", s)
        if m:
            num = m.group(1)
            body = norm_space(m.group(2))
            body = _strip_leading_reference_number(body)
            if len(body) > 20 and re.search(r'[A-Z][a-z]+', body):
                return RefNum(reference_full=s, num=num)
        return None
    
    return None


def reconcile_numeric(citations: List[str], references: List[RefNum], style: str = "ieee") -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    style = style.lower()
    is_ieee = style == "ieee"
    
    if not is_ieee:
        return [], [], [], [], 0
    
    ref_map: Dict[str, str] = {}
    ref_by_num: Dict[str, str] = {}
    
    for r in references:
        ref_map[r.num] = r.reference_full
        ref_by_num[r.num] = r.reference_full
        ref_map[f"[{r.num}]"] = r.reference_full
    
    cite_counts = Counter()
    
    for cite in citations:
        cite_str = str(cite).strip()
        
        if cite_str in ref_map:
            cite_counts[ref_map[cite_str]] += 1
        elif cite_str.isdigit() and cite_str in ref_by_num:
            cite_counts[ref_by_num[cite_str]] += 1
    
    c2r = []
    missing = Counter()
    
    for cite in citations:
        cite_str = str(cite).strip()
        matched = False
        
        if cite_str in ref_map:
            c2r.append({
                "status": "matched",
                "in_text": cite_str,
                "matched_reference": ref_map[cite_str],
                "flags": ""
            })
            matched = True
        elif cite_str.isdigit() and cite_str in ref_by_num:
            c2r.append({
                "status": "matched",
                "in_text": cite_str,
                "matched_reference": ref_by_num[cite_str],
                "flags": "number_only"
            })
            matched = True
        
        if not matched:
            c2r.append({
                "status": "not_found",
                "in_text": cite_str,
                "matched_reference": "",
                "flags": ""
            })
            missing[cite_str] += 1
    
    r2c = []
    uncited = []
    
    cite_samples = defaultdict(list)
    for cite in citations:
        cite_str = str(cite).strip()
        if cite_str in ref_map:
            if len(cite_samples[ref_map[cite_str]]) < 6:
                cite_samples[ref_map[cite_str]].append(cite_str)
        elif cite_str.isdigit() and cite_str in ref_by_num:
            if len(cite_samples[ref_by_num[cite_str]]) < 6:
                cite_samples[ref_by_num[cite_str]].append(cite_str)
    
    for r in references:
        times = cite_counts.get(r.reference_full, 0)
        if times == 0:
            uncited.append(r.reference_full)
        r2c.append({
            "times_cited": times,
            "reference": r.reference_full,
            "cited_by": cite_samples.get(r.reference_full, [])
        })
    
    missing_rows = [{"citation_in_text": k, "count_in_text": v} for k, v in missing.items()]
    unique_intext_count = len(set(citations))
    
    return c2r, r2c, missing_rows, uncited, unique_intext_count


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
            nums.append(str(start))
            nums.append(str(end))
    else:
        nums.append(str(start))
    
    if groups[2]:
        start2 = int(groups[2])
        if groups[3]:
            end2 = int(groups[3])
            if start2 <= end2 and (end2 - start2) <= 50:
                nums.extend([str(i) for i in range(start2, end2 + 1)])
            else:
                nums.append(str(start2))
                nums.append(str(end2))
        else:
            nums.append(str(start2))
    

    return nums


# ============================================================================
# SAFE NUMERIC STYLE EXTENSIONS
# ============================================================================

def _expand_numeric_citation_payload(payload: str, max_range: int = 50) -> List[str]:
    """Expand payloads such as '1', '1, 4', '1-3', '1, 4-6'."""
    payload = norm_space(payload or "")
    if not payload:
        return []
    payload = payload.translate(SUPERSCRIPT_TO_NORMAL)
    payload = payload.replace("–", "-").replace("—", "-").replace("−", "-")
    payload = re.sub(r"\s+", "", payload)

    out: List[str] = []
    for part in re.split(r"[,;]", payload):
        if not part:
            continue
        if "-" in part:
            bits = [b for b in part.split("-") if b]
            if len(bits) == 2 and bits[0].isdigit() and bits[1].isdigit():
                start, end = int(bits[0]), int(bits[1])
                if 1 <= start <= end and (end - start) <= max_range:
                    out.extend(str(i) for i in range(start, end + 1))
                else:
                    out.extend([bits[0], bits[1]])
            continue
        if part.isdigit():
            out.append(str(int(part)))
    return out


def _numeric_context_is_statistical_or_label(text: str, start: int, end: int, bracket_kind: str) -> bool:
    """
    Reject numeric candidates that are likely statistics, model labels, tables or figures.
    This is especially important for round-bracket numeric styles.
    """
    t = text or ""
    before = t[max(0, start - 100):start]
    after = t[end:min(len(t), end + 100)]
    ctx = soft_lower(before + " " + after)
    before_tail = soft_lower(before[-50:])

    if re.search(r"\b(?:table|figure|fig\.?|model|equation|eq\.?|appendix|chapter|section)\s*$", before_tail, re.I):
        return True

    if bracket_kind == "round":
        immediate_before = soft_lower(before[-35:])
        immediate_after = soft_lower(after[:20])
        tight = soft_lower(before[-12:] + " " + after[:12])

        # Reject statistical notation where the statistic label is immediately
        # before the bracket, e.g. p (1), df (2), Model (1), Table (2).
        if re.search(
            r"\b(?:p|p\s*value|t|f|z|chi|χ2|χ²|beta|β|r2|r²|adj|se|sd|mean|n|df|sig|ci|or|aor|coef|coefficient|regression|model|table|figure)\s*$",
            immediate_before,
            re.I,
        ):
            return True

        # Reject coefficient/statistical reporting close to the bracket.
        if re.search(r"[=<>≤≥%]", tight):
            return True
        if re.match(r"^\s*[=<>≤≥%]", immediate_after):
            return True

    return False


def extract_square_numeric_citations(text: str) -> List[str]:
    t = text or ""
    out: List[str] = []

    # Remove common table/figure labels before scanning.
    t = re.sub(r"(?:table|figure|fig\.?|eq\.?|equation)\s+\[?\s*\d{1,4}\s*\]?", "", t, flags=re.I)
    t = re.sub(r"\]\s*\n\s*\[", "][", t)

    bracket_range_pat = re.compile(r"\[\s*(\d{1,4})\s*\]\s*[-–—−]\s*\[\s*(\d{1,4})\s*\]")
    for m in bracket_range_pat.finditer(t):
        if _numeric_context_is_statistical_or_label(t, m.start(), m.end(), "square"):
            continue
        out.extend(_expand_numeric_citation_payload(f"{m.group(1)}-{m.group(2)}"))

    pat = re.compile(r"\[\s*(\d{1,4}(?:\s*(?:,|;|[-–—−])\s*\d{1,4})*)\s*\]")
    for m in pat.finditer(t):
        if _numeric_context_is_statistical_or_label(t, m.start(), m.end(), "square"):
            continue
        out.extend(_expand_numeric_citation_payload(m.group(1)))

    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def extract_round_numeric_citations(text: str) -> List[str]:
    """Round numeric citations are only called for explicit round-bracket styles."""
    t = text or ""
    out: List[str] = []
    pat = re.compile(r"\(\s*(\d{1,4}(?:\s*(?:,|;|[-–—−])\s*\d{1,4})*)\s*\)")
    for m in pat.finditer(t):
        if _numeric_context_is_statistical_or_label(t, m.start(), m.end(), "round"):
            continue
        out.extend(_expand_numeric_citation_payload(m.group(1)))

    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def extract_superscript_numeric_citations(text: str) -> List[str]:
    """
    Extract true superscript numeric citations.
    Supported forms include ¹, ¹,², ¹–³, ^1 and ^{1,2}.
    Ordinary baseline digits are not treated as superscript citations.
    """
    t = text or ""
    out: List[str] = []

    sup_chars = re.escape(_SUPERSCRIPT_DIGITS)
    sup_pat = re.compile(
        rf"(?<=[A-Za-z0-9\]\)\.,;:])\s*([{sup_chars}]+(?:\s*(?:,|;|⁻|[-–—−])\s*[{sup_chars}]+)*)"
    )
    for m in sup_pat.finditer(t):
        out.extend(_expand_numeric_citation_payload(m.group(1)))

    caret_pat = re.compile(r"\^(?:\{\s*)?(\d{1,4}(?:\s*(?:,|;|[-–—−])\s*\d{1,4})*)(?:\s*\})?")
    for m in caret_pat.finditer(t):
        out.extend(_expand_numeric_citation_payload(m.group(1)))

    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def extract_safe_numeric_citations(text: str, style: str = "numeric_square") -> List[str]:
    forms = _numeric_style_forms(style)
    out: List[str] = []
    if "square" in forms:
        out.extend(extract_square_numeric_citations(text))
    if "superscript" in forms:
        out.extend(extract_superscript_numeric_citations(text))
    if "round" in forms:
        out.extend(extract_round_numeric_citations(text))

    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def parse_reference_numeric_general(ref: str, style: str = "numeric_square", fallback_num: Optional[int] = None) -> Optional[RefNum]:
    s = norm_space(ref)
    if not s:
        return None

    m = re.match(r"^\s*(?:\[\s*(\d{1,4})\s*\]|\(\s*(\d{1,4})\s*\)|(\d{1,4})[\.)])\s*(.+)$", s)
    if m:
        num = m.group(1) or m.group(2) or m.group(3)
        body = norm_space(m.group(4))
        if num and 1900 <= int(num) <= 2099:
            return None
        if len(body) >= 10 and re.search(r"[A-Za-z]", body):
            return RefNum(reference_full=s, num=str(int(num)))
        return None

    # Controlled fallback for numeric lists that were stripped during conversion.
    # It is not used for IEEE, where bracketed numbers are expected.
    if fallback_num is not None and len(s) >= 20 and re.search(r"[A-Za-z]", s):
        return RefNum(reference_full=s, num=str(fallback_num))

    return None


def parse_references_numeric_general(references_raw: List[str], style: str = "numeric_square") -> List[RefNum]:
    refs: List[RefNum] = []
    allow_sequential_fallback = _style_token(style) not in {"ieee", "ieee_square"}

    for idx, ref in enumerate(references_raw or [], start=1):
        parsed = parse_reference_numeric_general(
            ref,
            style=style,
            fallback_num=idx if allow_sequential_fallback else None,
        )
        if parsed:
            refs.append(parsed)

    return refs


def _format_numeric_intext(num: str, style: str) -> str:
    s = _style_token(style)
    if s in SAFE_SUPERSCRIPT_NUMERIC_STYLES:
        return str(num).translate(NORMAL_TO_SUPERSCRIPT)
    if s in ROUND_NUMERIC_STYLES:
        return f"({num})"
    return f"[{num}]"


def reconcile_numeric_general(citations: List[str], references: List[RefNum], style: str = "numeric_square") -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    ref_by_num: Dict[str, str] = {str(r.num): r.reference_full for r in references or [] if str(r.num)}
    cite_counts: Counter = Counter()

    c2r: List[Dict[str, Any]] = []
    missing: Counter = Counter()
    cite_samples: Dict[str, List[str]] = defaultdict(list)

    for cite in citations or []:
        num = str(cite).strip()
        display = _format_numeric_intext(num, style)
        if num in ref_by_num:
            ref_full = ref_by_num[num]
            cite_counts[ref_full] += 1
            if len(cite_samples[ref_full]) < 6:
                cite_samples[ref_full].append(display)
            c2r.append({
                "status": "matched",
                "in_text": display,
                "matched_reference": ref_full,
                "flags": _style_token(style),
            })
        else:
            c2r.append({
                "status": "not_found",
                "in_text": display,
                "matched_reference": "",
                "flags": _style_token(style),
            })
            missing[display] += 1

    r2c: List[Dict[str, Any]] = []
    uncited: List[str] = []
    for r in references or []:
        times = int(cite_counts.get(r.reference_full, 0))
        if times == 0:
            uncited.append(r.reference_full)
        r2c.append({
            "times_cited": times,
            "reference": r.reference_full,
            "cited_by": cite_samples.get(r.reference_full, []),
        })

    missing_rows = [{"citation_in_text": k, "count_in_text": int(v)} for k, v in missing.items()]
    unique_intext_count = len(set(str(c).strip() for c in (citations or []) if str(c).strip()))
    return c2r, r2c, missing_rows, uncited, unique_intext_count


# ============================================================================
# VANCOUVER STYLE - SIMPLE SEQUENTIAL
# ============================================================================

def extract_vancouver_citations(text: str) -> List[str]:
    t = text or ""
    citations = []
    
    single_pat = re.compile(r'\[\s*(\d+)\s*\]')
    for m in single_pat.finditer(t):
        citations.append(m.group(1))
    
    multi_pat = re.compile(r'\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]')
    for m in multi_pat.finditer(t):
        numbers = m.group(1).split(',')
        for num in numbers:
            num = num.strip()
            if num.isdigit():
                citations.append(num)
    
    range_pat = re.compile(r'\[\s*(\d+)\s*[-–]\s*(\d+)\s*\]')
    for m in range_pat.finditer(t):
        start, end = int(m.group(1)), int(m.group(2))
        if start <= end and (end - start) <= 50:
            for i in range(start, end + 1):
                citations.append(str(i))
    
    try:
        citations = sorted(set(citations), key=lambda x: int(x))
    except:
        citations = list(dict.fromkeys(citations))
    
    return citations


def parse_vancouver_references(references_raw: List[str]) -> List[RefNum]:
    parsed_refs = []
    
    for i, ref in enumerate(references_raw, start=1):
        ref = ref.strip()
        if not ref or len(ref) < 20:
            continue
            
        num = str(i)
        
        m = re.match(r'^(\d+)\.?\s+', ref)
        if m:
            extracted_num = m.group(1)
            if extracted_num != num:
                print(f"Warning: Reference {i} has number {extracted_num}")
        
        parsed_refs.append(RefNum(
            reference_full=ref,
            num=num
        ))
    
    return parsed_refs


def reconcile_vancouver(citations: List[str], references: List[RefNum]) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str], int
]:
    ref_by_num = {r.num: r.reference_full for r in references}
    cite_counts = Counter(citations)
    
    c2r = []
    missing = Counter()
    
    for cite in citations:
        if cite in ref_by_num:
            c2r.append({
                "status": "matched",
                "in_text": f"[{cite}]",
                "matched_reference": ref_by_num[cite],
                "flags": ""
            })
        else:
            c2r.append({
                "status": "not_found",
                "in_text": f"[{cite}]",
                "matched_reference": "",
                "flags": ""
            })
            missing[f"[{cite}]"] += 1
    
    r2c = []
    uncited = []
    
    cite_samples = defaultdict(list)
    for cite in citations:
        if cite in ref_by_num and len(cite_samples[ref_by_num[cite]]) < 6:
            cite_samples[ref_by_num[cite]].append(f"[{cite}]")
    
    for r in references:
        times = cite_counts.get(r.num, 0)
        if times == 0:
            uncited.append(r.reference_full)
        r2c.append({
            "times_cited": times,
            "reference": r.reference_full,
            "cited_by": cite_samples.get(r.reference_full, [])
        })
    
    missing_rows = [{"citation_in_text": k, "count_in_text": v} for k, v in missing.items()]
    unique_intext_count = len(set(citations))
    
    return c2r, r2c, missing_rows, uncited, unique_intext_count


# ============================================================================
# Reference clustering (shared)
# ============================================================================

def _cluster_references(references: List[Any]) -> Dict[str, Dict[str, Any]]:
    by_doi: Dict[str, List[str]] = defaultdict(list)
    by_bucket: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    sigs: Dict[str, Tuple[str, str, str, str]] = {}

    for r in references:
        rf = r.reference_full
        y, a1, t, doi = _extract_ref_signature(rf)
        sigs[rf] = (y, a1, t, doi)
        if doi:
            by_doi[doi.lower()].append(rf)
        else:
            by_bucket[(y, a1)].append(rf)

    clusters: List[List[str]] = []

    for _doi, items in by_doi.items():
        clusters.append(items)

    for (y, a1), items in by_bucket.items():
        if len(items) <= 1:
            clusters.append(items)
            continue

        used = set()
        for i, rf_i in enumerate(items):
            if rf_i in used:
                continue
            used.add(rf_i)
            _, _, ti, _ = sigs[rf_i]
            cluster = [rf_i]

            for rf_j in items[i+1:]:
                if rf_j in used:
                    continue
                _, _, tj, _ = sigs[rf_j]

                if not ti or not tj:
                    continue

                if FUZZ_OK and fuzz:
                    score = max(fuzz.token_set_ratio(ti, tj), fuzz.partial_ratio(ti, tj))
                else:
                    si = set(ti.split())
                    sj = set(tj.split())
                    score = int(round(100 * (len(si & sj) / max(1, len(si), len(sj)))))

                if score >= 88:
                    used.add(rf_j)
                    cluster.append(rf_j)

            clusters.append(cluster)

    mapping: Dict[str, Dict[str, Any]] = {}
    for cid, members in enumerate(clusters, start=1):
        canonical = max(members, key=lambda x: len(x or ""))
        for rf in members:
            mapping[rf] = {
                "cluster_id": cid,
                "canonical_ref": canonical,
                "is_duplicate": (rf != canonical),
            }
    return mapping


def _extract_ref_signature(ref_full: str) -> Tuple[str, str, str, str]:
    s = ref_full or ""
    doi = ""
    mdoi = _DOI_RE.search(s)
    if mdoi:
        doi = mdoi.group(0).rstrip(".,;")

    m = YEAR_RE.search(s)
    if not m:
        t = _norm_ref_text(s)[:80]
        return ("", t[:24], t[24:60], doi)

    year = _base_year(m.group(1))
    left = (s[:m.start()] or "").strip(" ,;()")
    right = (s[m.end():] or "").strip()

    surnames = _surnames_from_author_blob(left)
    first_author = surnames[0] if surnames else _norm_ref_text(left)[:24]
    first_author = re.sub(r"[^a-z0-9\- ]+", "", _norm_ref_text(first_author))

    right = right.lstrip(" .,:;)-–—\"'[]")
    right2 = re.split(r"\.\s+|\.?$|\s+https?://|\s+doi:\s*", right, maxsplit=1, flags=re.I)[0]
    tokens = [re.sub(r"[^a-z0-9\-]+", "", t) for t in _norm_ref_text(right2).split()]
    tokens = [t for t in tokens if t and t not in _REF_STOPWORDS]
    title_stub = " ".join(tokens[:12])
    return (year, first_author, title_stub, doi)


_REF_STOPWORDS = {
    "the","a","an","and","or","of","in","on","for","to","with","from","at","by","as",
    "ed","eds","edition","vol","volume","no","number","pp","pages","page",
}


def _strip_accents(s: str) -> str:
    s = s or ""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def _norm_ref_text(s: str) -> str:
    s = _strip_accents(s.lower())
    s = s.replace("&", " and ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s)]+", re.I)


# -----------------------------
# Chunked text processing
# -----------------------------
def _iter_text_chunks(text: str, chunk_size: int = 300_000, overlap: int = 2_000):
    s = text or ""
    n = len(s)
    if n <= chunk_size:
        yield s
        return
    step = max(1, chunk_size - overlap)
    for i in range(0, n, step):
        yield s[i: min(n, i + chunk_size)]
        if i + chunk_size >= n:
            break


def _extract_author_year_citations_chunked(text: str) -> List[str]:
    # Preserve repeated occurrences so times_cited and count_in_text are correct.
    total = []
    for chunk in _iter_text_chunks(text):
        total.extend(extract_author_year_citations(chunk))
    return total


def _extract_numeric_citations_chunked(text: str, style: str = "ieee") -> List[str]:
    seen = set()
    total = []
    for chunk in _iter_text_chunks(text):
        if style == "vancouver":
            found = extract_vancouver_citations(chunk)
        elif _is_supported_numeric_style(style):
            found = extract_safe_numeric_citations(chunk, style=style)
        else:
            found = extract_ieee_citations(chunk)

        for c in found:
            if c not in seen:
                seen.add(c)
                total.append(c)
    return total


# ============================================================================
# SUGGESTION ENGINE (NON-INVASIVE - FINAL)
# ============================================================================

@dataclass
class FixSuggestion:
    original: str
    suggested: str
    fix_type: str
    confidence: float
    reason: str


# ============================================================
# CORE CITATION SUGGESTION ENGINE
# ============================================================

def generate_citation_suggestions(
    citations: List[str],
    references: List[RefAY],
    ref_map: Dict[str, str]
) -> List[Dict[str, Any]]:
    """
    Generate NON-INVASIVE citation suggestions.
    No modification of original text.
    """

    suggestions = []
    seen = set()

    for citation in citations:
        if citation in seen:
            continue
        seen.add(citation)

        suggestion = _generate_citation_fixes(
            citation,
            references,
            ref_map
        )

        if suggestion:
            suggestions.append({
                "citation": suggestion.original,
                "suggested": suggestion.suggested,
                "type": suggestion.fix_type,
                "confidence": suggestion.confidence,
                "reason": suggestion.reason,
                "action": "review_required"
            })

    return suggestions


# ============================================================
# REFERENCE SUGGESTION ENGINE
# ============================================================

def generate_reference_suggestions(
    references: List[RefAY]
) -> List[Dict[str, Any]]:
    suggestions = []

    for ref in references:
        ref_suggestions = _generate_reference_fixes(ref)

        for s in ref_suggestions:
            suggestions.append({
                "original": s.original,
                "suggested": s.suggested,
                "type": s.fix_type,
                "confidence": s.confidence,
                "reason": s.reason,
                "action": "optional_fix"
            })

    return suggestions


# ============================================================
# MASTER SUGGESTION ENGINE
# ============================================================

def _citation_text_from_missing_item(item: Any) -> str:
    if isinstance(item, dict):
        return (
            item.get("citation_in_text")
            or item.get("citation")
            or item.get("in_text")
            or ""
        )
    return str(item or "")


def _reference_author_year(ref: RefAY) -> Dict[str, Any]:
    ref_text = ref.reference_full or ""
    ym = YEAR_RE.search(ref_text)
    if not ym:
        return {"author": "", "year": "", "reference": ref_text, "left": "", "aliases": []}

    year = _base_year(ym.group(1))
    left = ref_text[:ym.start()].strip(" ,.;:()[]{}")
    author = _first_author_or_org_key(left)

    aliases = _institution_acronym_aliases(left)
    if author:
        aliases.append(author)

    seen = set()
    clean_aliases = []
    for a in aliases:
        a = strip_punct(a)
        if a and a not in seen and not _is_non_author_key(a):
            seen.add(a)
            clean_aliases.append(a)

    return {
        "author": author or "",
        "year": year or "",
        "reference": ref_text,
        "left": left,
        "aliases": clean_aliases,
        "acronym": clean_aliases[0] if clean_aliases else "",
    }


def _generate_possible_match_for_missing(
    citation: str,
    references: List[RefAY],
    reference_infos: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    parsed = _parse_author_year_from_cite(citation)
    if not parsed:
        return None

    cite_author, cite_year = parsed
    cite_author_norm = _normalise_author_for_matching(cite_author)

    ym_raw = YEAR_RE.search(citation or "")
    raw_left = (citation[:ym_raw.start()] if ym_raw else citation).strip(" ,;:()[]{}")
    raw_alpha = re.sub(r"[^A-Za-z]", "", raw_left)
    is_upper_acronym_citation = bool(
        2 <= len(raw_alpha) <= 8
        and raw_alpha.upper() == raw_alpha
        and raw_alpha.lower() == cite_author_norm
    )

    cite_year_base = _base_year(cite_year)

    best = None
    best_score = 0.0

    if reference_infos is None:
        reference_infos = [_reference_author_year(ref) for ref in (references or [])]

    for info in reference_infos or []:
        ref_author = info.get("author", "")
        ref_year = info.get("year", "")
        ref_text = info.get("reference", "")
        ref_left = info.get("left", "")
        ref_aliases = info.get("aliases", []) or []

        if not ref_year or not ref_text:
            continue

        author_score = 0.0
        matched_alias = ref_author or ""

        for alias in ref_aliases or [ref_author]:
            alias_norm = _normalise_author_for_matching(alias)
            if not alias_norm:
                continue

            score = 0.0
            if cite_author_norm == alias_norm:
                score = 100.0
            elif not is_upper_acronym_citation and cite_author_norm and alias_norm and (
                len(cite_author_norm) >= 5
                and len(alias_norm) >= 5
                and (cite_author_norm in alias_norm or alias_norm in cite_author_norm)
            ):
                score = 88.0
            elif not is_upper_acronym_citation and FUZZ_OK and fuzz and cite_author_norm and alias_norm:
                # Avoid partial_ratio because it creates false positives:
                # e.g., Ghannajeh -> Ghana, Amu -> a long author list.
                score = max(
                    fuzz.ratio(cite_author_norm, alias_norm),
                    fuzz.token_set_ratio(cite_author_norm, alias_norm),
                )

            if score > author_score:
                author_score = float(score)
                matched_alias = alias

        # Compare against full institutional author text only for non-acronym citations.
        full_author_norm = _normalise_author_for_matching(ref_left)
        if (
            not is_upper_acronym_citation
            and FUZZ_OK and fuzz
            and full_author_norm and cite_author_norm
            and len(cite_author_norm) >= 5
        ):
            full_score = fuzz.token_set_ratio(cite_author_norm, full_author_norm)
            if full_score > author_score:
                author_score = float(full_score)
                matched_alias = ref_left

        try:
            year_gap = abs(int(cite_year_base[:4]) - int(ref_year[:4]))
        except Exception:
            year_gap = 999

        same_year = cite_year_base == ref_year
        close_year = year_gap <= 5

        if is_upper_acronym_citation and author_score < 100:
            continue

        required_author_score = 84 if same_year else 92

        if author_score >= required_author_score and (same_year or close_year):
            score = author_score - min(year_gap * 4, 24)

            if score > best_score:
                best_score = score

                display_author = matched_alias or ref_author or ref_left or cite_author
                if len(display_author) > 80:
                    display_author = display_author[:77] + "..."

                if same_year and author_score < 100:
                    issue_type = "possible_match_name_variation"
                    reason = (
                        f"Possible reference match found for '{citation}', "
                        f"but the author name appears differently in the reference list."
                    )
                    suggested = citation.replace(cite_author, display_author, 1)

                elif not same_year and author_score >= 90:
                    issue_type = "possible_match_year_variation"
                    reason = (
                        f"Possible reference match found for '{citation}', "
                        f"but the reference year appears as {ref_year}."
                    )
                    suggested = citation.replace(cite_year, ref_year, 1)

                else:
                    issue_type = "possible_match_name_year_variation"
                    reason = (
                        f"Possible reference match found for '{citation}', "
                        f"but author form and year may differ in the reference list."
                    )
                    suggested = citation.replace(cite_author, display_author, 1).replace(cite_year, ref_year, 1)

                best = {
                    "original": citation,
                    "suggested": suggested,
                    "confidence": round(max(min(score / 100.0, 0.95), 0.65), 2),
                    "issue_type": issue_type,
                    "reason": reason,
                    "fix_type": "review_required",
                    "possible_reference": ref_text,
                    "matched_author_in_reference": ref_left or ref_author,
                    "matched_year_in_reference": ref_year,
                    "apply": {
                        "type": "replace_text",
                        "target": citation,
                        "replacement": suggested,
                    },
                }

    return best


def _split_missing_possible_matches(
    missing_rows: List[Dict[str, Any]],
    references: List[RefAY],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    remaining_missing: List[Dict[str, Any]] = []
    possible_matches: List[Dict[str, Any]] = []
    reference_infos = [_reference_author_year(ref) for ref in (references or [])]

    for item in missing_rows or []:
        cite = _citation_text_from_missing_item(item)
        possible = _generate_possible_match_for_missing(cite, references, reference_infos)

        if possible and float(possible.get("confidence", 0) or 0) >= 0.78:
            row = dict(item) if isinstance(item, dict) else {"citation_in_text": cite}
            row["status"] = "possible_match_with_year_or_name_variation"
            row["possible_match"] = possible
            possible_matches.append(row)
        else:
            remaining_missing.append(item)

    return remaining_missing, possible_matches


def generate_suggestions(
    citations: List[str],
    c2r: List[Dict[str, Any]],
    missing_rows: List[Dict[str, Any]],
    references: List[RefAY],
    ref_map: Dict[str, str]
) -> Dict[str, Any]:
    """
    Clean Suggestion Engine:
    - No duplication with Missing/Recovery tabs
    - Only fixable inconsistencies
    - UI-ready + action-ready
    """

    # ============================================================
    # 1. CITATION FIXES (MAIN)
    # ============================================================

    citation_suggestions = []

    for citation in citations:
        suggestion = _generate_citation_fixes(
            citation,
            references,
            ref_map
        )

        if suggestion:
            issue_type = suggestion.fix_type or "ambiguous_match"

            # ❌ skip missing_reference & uncited_reference
            if issue_type in {"missing_reference", "uncited_reference"}:
                continue

            citation_suggestions.append({
                "original": suggestion.original,
                "suggested": suggestion.suggested,
                "confidence": suggestion.confidence,
            
                # WHAT IS WRONG
                "issue_type": issue_type,
            
                # WHY IT IS WRONG
                "reason": suggestion.reason or "Inconsistency detected",
            
                # WHAT TO DO
                "fix_type": "review_required" if issue_type.startswith("possible_") or issue_type in {"author_normalization", "author_normalization_review"} else ("required_fix" if suggestion.confidence >= 0.85 else "review_required"),

                # 🔥 APPLY ACTION (ONLY ONCE)
                "apply": {
                    "type": "replace_text",
                    "target": suggestion.original,
                    "replacement": suggestion.suggested
                }
            })
    # ============================================================
    # 1B. POSSIBLE MATCHES FROM MISSING CITATIONS
    # ============================================================
    possible_match_suggestions = []
    existing_originals = {
        s.get("original")
        for s in citation_suggestions
        if isinstance(s, dict)
    }
    reference_infos = [_reference_author_year(ref) for ref in (references or [])]

    for item in missing_rows or []:
        missing_citation = _citation_text_from_missing_item(item)

        if not missing_citation or missing_citation in existing_originals:
            continue

        if isinstance(item, dict) and isinstance(item.get("possible_match"), dict):
            possible = item.get("possible_match")
        else:
            possible = _generate_possible_match_for_missing(
                missing_citation,
                references,
                reference_infos,
            )

        if possible:
            citation_suggestions.append(possible)
            possible_match_suggestions.append(possible)
            existing_originals.add(missing_citation)

    # ============================================================
    # 2. REFERENCE FIXES (FORMATTING / STYLE)
    # ============================================================

    reference_suggestions = []

    for ref in references:
        ref_fix = _generate_reference_fix(ref)

        if ref_fix:
            reference_suggestions.append({
                "original": ref.reference_full,
                "suggested": ref_fix.suggested,
                "confidence": ref_fix.confidence,
                "issue_type": "formatting_issue",
                "reason": ref_fix.reason or "Reference formatting inconsistency",
                "fix_type": "optional_fix",

                # 🔥 APPLY ACTION
                "apply": {
                    "type": "replace_text",
                    "target": ref.reference_full,
                    "replacement": ref_fix.suggested
                }
            })

    # ============================================================
    # 3. COMBINE (NO DUPLICATES FROM OTHER TABS)
    # ============================================================

    all_suggestions = citation_suggestions + reference_suggestions

    # ============================================================
    # 4. STATISTICS
    # ============================================================

    high_conf = [s for s in all_suggestions if s["confidence"] >= 0.85]
    med_conf = [s for s in all_suggestions if 0.70 <= s["confidence"] < 0.85]
    low_conf = [s for s in all_suggestions if s["confidence"] < 0.70]

    by_type = {}
    for s in all_suggestions:
        by_type[s["issue_type"]] = by_type.get(s["issue_type"], 0) + 1

    # ============================================================
    # 5. FINAL OUTPUT
    # ============================================================

    return {
        "citations": citation_suggestions,
        "references": reference_suggestions,

        # Possible matches are separate so the UI can show:
        # "the reference may exist, but year/name/acronym varies".
        "missing": possible_match_suggestions,
        "possible_matches": possible_match_suggestions,
        "unmatched": [],

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

def _generate_reference_fix(ref):
    """
    Simple reference formatting fixer (extend later)
    """
    text = ref.reference_full

    # Example fix: double spaces, punctuation, etc.
    cleaned = " ".join(text.split())

    if cleaned != text:
        return type("RefFix", (), {
            "suggested": cleaned,
            "confidence": 0.75,
            "reason": "Reference formatting cleaned"
        })

    return None
# ============================================================
# HELPER FUNCTIONS FOR SUGGESTIONS (PRESERVED FROM ORIGINAL)
# ============================================================

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
    
    # Case 0: Fix malformed year (204 -> 2024)
    if len(year) < 4 and year.isdigit():
        year_int = int(year)
        possible_years = []
        
        if len(year) == 3:
            possible_years = [
                2000 + year_int,
                2000 + year_int + 10,
                2000 + year_int + 20,
                1900 + year_int,
            ]
        elif len(year) == 2:
            possible_years = [2000 + year_int, 1900 + year_int]
        elif len(year) == 1:
            possible_years = [2000 + year_int, 2000 + year_int + 10, 2000 + year_int + 20]
        
        author_variations = [auth]
        if ' & ' in auth:
            parts = auth.split(' & ')
            author_variations.extend(parts)
        if ' and ' in auth:
            parts = auth.split(' and ')
            author_variations.extend(parts)
        
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
                        reason=f"Malformed year '{year}' corrected to '{alt_year_str}' based on reference for '{test_auth}'"
                    )
    
    # Case 1: Year typo (off by 1 or more) - only for 4-digit years
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
    
    # Case 2: Author name variation using fuzzy matching
    if FUZZ_OK and fuzz:
        auth_norm = strip_punct(auth.lower())
        best_match = None
        best_score = 0
        
        target_years = []
        if len(year) == 4:
            target_years.append(year)
        elif year.isdigit() and len(year) < 4:
            y_int = int(year)
            target_years = [str(2000 + y_int), str(2000 + y_int + 10), str(2000 + y_int + 20), str(1900 + y_int)]
        
        for ref in references:
            ym = YEAR_RE.search(ref.reference_full)
            if ym:
                ref_year = _base_year(ym.group(1))
                for target_year in target_years:
                    if ref_year == target_year or (len(target_year) == 4 and abs(int(ref_year) - int(target_year)) <= 2):
                        left = ref.reference_full[:ym.start()].strip(" ,;()")
                        ref_auth = _first_author_or_org_key(left)
                        if ref_auth:
                            score = fuzz.ratio(auth_norm, ref_auth.lower())
                            if score > best_score and score >= 75:
                                best_score = score
                                best_match = ref_auth
        
        if best_match and best_match.lower() != auth.lower():
            alt_citation = re.sub(r'\b' + re.escape(auth) + r'\b', best_match, citation, count=1)

            # Author-name fuzzy matches are helpful, but they should not be
            # treated as automatic or high-confidence fixes. "Hook" vs "Hooks"
            # and similar cases must stay review-only.
            safe_confidence = min(best_score / 100, 0.74)

            return FixSuggestion(
                original=citation,
                suggested=alt_citation,
                fix_type="author_normalization_review",
                confidence=safe_confidence,
                reason=f"Possible author-name variation: '{auth}' may correspond to '{best_match}'. Review manually before changing."
            )
    
    # Case 3: Missing "et al." pattern
    if "et al" not in citation.lower() and len(citation.split(",")[0].split()) > 2:
        first_author = auth.split()[0] if auth else ""
        for ref in references:
            if first_author and first_author.lower() in ref.reference_full.lower():
                if "et al" in ref.reference_full.lower():
                    alt_citation = f"{first_author} et al., {year}"
                    return FixSuggestion(
                        original=citation,
                        suggested=alt_citation,
                        fix_type="add_et_al",
                        confidence=0.70,
                        reason=f"Added 'et al.' for {first_author}"
                    )
    
    return None


def _normalise_doi_url_in_reference(ref_text: str) -> str:
    """Safely normalise DOI spacing without duplicating https://doi.org."""
    s = norm_space(ref_text or "")
    if not s:
        return s

    # Fix broken DOI host spacing: https://doi.org /10... or https://doi. org/10...
    s = re.sub(r"https?://(?:dx\.)?doi\.\s*org\s*/\s*", "https://doi.org/", s, flags=re.I)
    s = re.sub(r"doi\.\s*org\s*/\s*", "doi.org/", s, flags=re.I)

    # Fix DOI split after slash: 10.1016 / j... -> 10.1016/j...
    s = re.sub(r"\b(10\.\d{4,9})\s*/\s*", r"\1/", s, flags=re.I)

    # Convert bare DOI to URL only when no DOI URL is already present.
    if "doi.org/" not in s.lower():
        m = DOI_RE.search(s)
        if m:
            doi = m.group(0).rstrip(".,;)")
            s = s[:m.start()] + "https://doi.org/" + doi + s[m.end():]

    # Remove accidental doubled DOI URL forms.
    s = re.sub(r"https://doi\.org/\s*https://doi\.org/", "https://doi.org/", s, flags=re.I)
    s = re.sub(r"https://doi\.org/\s*doi\.org/", "https://doi.org/", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _generate_reference_fixes(ref: RefAY) -> List[FixSuggestion]:
    """Generate conservative, review-required suggestions for reference entries."""
    suggestions = []
    ref_text = ref.reference_full

    # DOI spacing/URL normalisation. Avoid the earlier false positive where
    # "https://doi.org /10..." became "https://doi.org /https://doi.org/10...".
    fixed_doi = _normalise_doi_url_in_reference(ref_text)
    if fixed_doi and fixed_doi != ref_text:
        suggestions.append(FixSuggestion(
            original=ref_text,
            suggested=fixed_doi,
            fix_type="doi_spacing_or_url_normalisation",
            confidence=0.88,
            reason="The DOI URL appears to contain spacing or formatting problems. Review before applying."
        ))

    # Add missing period at end, low priority only.
    if ref_text and not ref_text.rstrip().endswith('.'):
        suggestions.append(FixSuggestion(
            original=ref_text,
            suggested=ref_text.rstrip() + '.',
            fix_type="add_period",
            confidence=0.60,
            reason="The reference may need a trailing period. Review before applying."
        ))

    # Fix common URL scheme, but keep this review-required.
    if "http://" in ref_text and "https://" not in ref_text:
        fixed = ref_text.replace("http://", "https://")
        suggestions.append(FixSuggestion(
            original=ref_text,
            suggested=fixed,
            fix_type="fix_url_scheme",
            confidence=0.80,
            reason="The reference uses HTTP. Review whether HTTPS is available and appropriate."
        ))

    return suggestions


# -----------------------------
# Public API: run_crosscheck (UPDATED - includes main_text)
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
    style_s = (style or "apa").strip().lower()

    is_numeric = ("ieee" in style_s) or ("vancouver" in style_s) or ("numeric" in style_s) or _is_supported_numeric_style(style_s)
    style_hint = "numeric" if is_numeric else "apa"

    pdf_quality = None
    pdf_warnings: List[str] = []
    pdf_parser_build = ""

    if name.endswith(".docx"):
        main_text, ref_block_lines, ref_msg = read_docx_split_main_and_refs(file_bytes)
        references_raw = _merge_reference_lines(ref_block_lines, style_hint=style_hint)
        if style_hint == "numeric":
            references_raw = _split_embedded_numeric_refs(references_raw)
        
        # Fallback recovery for weak or failed extraction
        if style_hint == "apa" and len(references_raw) < 2:
            recovered = recover_references_for_verification(main_text, style_hint="apa")
            recovered = _clean_reference_list(_split_embedded_apa_refs(recovered), style_hint="apa")
            if len(recovered) > len(references_raw):
                references_raw = recovered
                ref_msg = f"{ref_msg} Fallback recovery extracted {len(references_raw)} references."
        elif style_hint == "numeric" and len(references_raw) == 0:
            recovered = recover_references_for_verification(main_text, style_hint="numeric")
            if recovered:
                references_raw = recovered
                ref_msg = f"{ref_msg} Fallback recovery extracted {len(references_raw)} references."

    elif name.endswith(".pdf"):
        pdf_data = parse_pdf_commercial(
            file_bytes=file_bytes,
            filename=filename,
            style_hint=style_hint,
        )

        pdf_quality = pdf_data.get("pdf_quality")
        pdf_warnings = pdf_data.get("pdf_warnings", []) or []
        pdf_parser_build = pdf_data.get("pdf_parser_build", PDF_PARSE_BUILD)

        if not pdf_data.get("ok"):
            return {
                "error": "PDF parsing failed",
                "note": pdf_data.get("ref_msg") or "The PDF could not be reliably parsed.",
                "filename": filename,
                "pdf_quality": pdf_quality,
                "pdf_warnings": pdf_warnings,
                "recommendation": "Upload the DOCX version for the most reliable CiteIntegrity report.",
            }

        main_text = pdf_data.get("main_text", "")
        references_raw = pdf_data.get("references", []) or []
        ref_msg = pdf_data.get("ref_msg") or f"PDF parsed. Found {len(references_raw)} references."

        if style_hint == "numeric":
            references_raw = _split_embedded_numeric_refs(references_raw)
        else:
            references_raw = _split_embedded_apa_refs(references_raw)
            references_raw = _clean_reference_list(references_raw, style_hint="apa")

        references_raw = _dedupe_keep_order(references_raw)
        
        # Fallback recovery for weak or failed extraction
        if style_hint == "apa" and len(references_raw) < 2:
            recovered = recover_references_for_verification(main_text, style_hint="apa")
            recovered = _clean_reference_list(_split_embedded_apa_refs(recovered), style_hint="apa")
            if len(recovered) > len(references_raw):
                references_raw = recovered
                ref_msg = f"{ref_msg} Fallback recovery extracted {len(references_raw)} references."
        elif style_hint == "numeric" and len(references_raw) == 0:
            recovered = recover_references_for_verification(main_text, style_hint="numeric")
            if recovered:
                references_raw = recovered
                ref_msg = f"{ref_msg} Fallback recovery extracted {len(references_raw)} references."

    else:
        return {"error": "Upload a DOCX or PDF"}

    main_text_len = len(main_text or "")
    too_large = main_text_len > 2_000_000

    possible_match_rows: List[Dict[str, Any]] = []

    if style_hint == "apa":
        cites = _extract_author_year_citations_chunked(main_text) if too_large else extract_author_year_citations(main_text)
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]

        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_author_year(cites, refs)

        # Separate likely false positives into a review-required category.
        # They are not counted as ordinary missing citations.
        missing_rows, possible_match_rows = _split_missing_possible_matches(missing_rows, refs)

        ref_count = len(refs)

    elif style_s == "ieee":
        cites_nums = []
        if too_large:
            cites_nums = _extract_numeric_citations_chunked(main_text, style="ieee")
        else:
            cites_nums = extract_ieee_citations(main_text)
        
        refs = []
        for r in references_raw:
            parsed = parse_reference_numeric(r, style="ieee")
            if parsed:
                refs.append(parsed)
        
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(
            cites_nums, refs, style="ieee"
        )
        ref_count = len(refs)

    elif style_s == "vancouver":
        print("Using Vancouver style - sequential numbering")
        
        cites_nums = []
        if too_large:
            cites_nums = _extract_numeric_citations_chunked(main_text, style="vancouver")
        else:
            cites_nums = extract_vancouver_citations(main_text)
        
        refs = parse_vancouver_references(references_raw)
        
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_vancouver(
            cites_nums, refs
        )
        ref_count = len(refs)

    elif _is_supported_numeric_style(style_s):
        numeric_forms = sorted(_numeric_style_forms(style_s))
        print(f"Using safe numeric style {style_s} with forms: {numeric_forms}")

        if too_large:
            cites_nums = _extract_numeric_citations_chunked(main_text, style=style_s)
        else:
            cites_nums = extract_safe_numeric_citations(main_text, style=style_s)

        refs = parse_references_numeric_general(references_raw, style=style_s)

        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric_general(
            cites_nums, refs, style=style_s
        )
        ref_count = len(refs)

    else:
        cites_nums = []
        if too_large:
            cites_nums = _extract_numeric_citations_chunked(main_text, style="ieee")
        else:
            cites_nums = extract_ieee_citations(main_text)
        
        refs = []
        for r in references_raw:
            parsed = parse_reference_numeric(r, style="ieee")
            if parsed:
                refs.append(parsed)
        
        c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(
            cites_nums, refs, style="ieee"
        )
        ref_count = len(refs)

    missing_unique = int(len(missing_rows or []))
    match_rate = 0.0
    if intext_count > 0:
        match_rate = 100.0 * max(0.0, float(intext_count - missing_unique)) / float(intext_count)

    result = {
        "filename": filename,
        "style": style_s,
        "main_text": main_text,
        "engine_build": ENGINE_BUILD,
        "verify_mode_used": (verify_mode or "all"),
        "reference_detection_message": ref_msg,
        "summary": {
            "in_text_citations_found": int(intext_count),
            "reference_entries_found": int(ref_count),
            "missing_in_references": int(missing_unique),
            "possible_match_variations": int(len(possible_match_rows or [])),
            "uncited_references": int(len(uncited_refs)),
            "match_rate": float(round(match_rate, 1)),
        },
        "missing_in_references": missing_rows,
        "possible_match_variations": possible_match_rows,
        "uncited_references": uncited_refs,
        "reconciliation_intext_to_reference": c2r,
        "reconciliation_reference_to_intext": r2c,
        "references_raw": references_raw,
    }

    if _is_supported_numeric_style(style_s):
        result["numeric_citation_policy"] = {
            "style": style_s,
            "forms_enabled": sorted(_numeric_style_forms(style_s)),
            "round_bracket_numeric_is_style_gated": _is_round_numeric_style(style_s),
            "ordinary_baseline_digits_are_not_auto_detected_as_superscript": True,
        }

    if pdf_quality is not None:
        result["pdf_quality"] = pdf_quality
        result["pdf_warnings"] = pdf_warnings
        result["pdf_parser_build"] = pdf_parser_build
        result["pdf_caution"] = (
            "PDF analysis is supported for text-based PDFs, but DOCX remains the recommended "
            "format for full citation integrity analysis."
        )
    
    return result

# ============================================================================
# ENHANCED API WITH AUTO-FIX (OPTIONAL - DOES NOT REPLACE ORIGINAL)
# ============================================================================

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
    enable_autofix: bool = True,
) -> Dict[str, Any]:
    """
    Clean autofix wrapper: runs crosscheck + generates suggestions
    """

    # -----------------------------
    # 1. Run base analysis
    # -----------------------------
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

    if not enable_autofix or "error" in result:
        return result

    try:
        style_s = (style or "apa").strip().lower()
        is_numeric = ("ieee" in style_s) or ("vancouver" in style_s) or ("numeric" in style_s) or _is_supported_numeric_style(style_s)

        if is_numeric:
            result["autofix"] = {
                "enabled": True,
                "message": f"Auto-fix supports APA/Harvard. Current style: {style_s}",
                "suggestions": {
                    "citations": [],
                    "missing": [],
                    "unmatched": [],
                    "references": []
                }
            }
            return result

        # -----------------------------
        # 2. Extract CORRECT data
        # -----------------------------
        references_raw = result.get("references_raw", []) or []

        c2r = (
            result.get("reconciliation_intext_to_reference", [])
            or result.get("c2r", [])
            or []
        )

        missing_base = (
            result.get("missing_in_references", [])
            or result.get("missing", [])
            or []
        )
        possible_base = result.get("possible_match_variations", []) or []
        missing = list(missing_base) + list(possible_base)

        citations = []

        for row in c2r:
            if isinstance(row, dict):
                cite = (
                    row.get("in_text")
                    or row.get("citation")
                    or row.get("citation_in_text")
                    or ""
                )
                if cite:
                    citations.append(cite)
            elif row:
                citations.append(str(row))

        for row in missing:
            if isinstance(row, dict):
                cite = (
                    row.get("citation_in_text")
                    or row.get("citation")
                    or row.get("in_text")
                    or ""
                )
                if cite:
                    citations.append(cite)
            elif row:
                citations.append(str(row))

        # Deduplicate while preserving order
        seen_cites = set()
        citations = [
            c for c in citations
            if c and not (c in seen_cites or seen_cites.add(c))
        ]

        # -----------------------------
        # 3. Parse references
        # -----------------------------
        refs = [parse_reference_author_year(r) for r in references_raw]
        refs = [r for r in refs if r is not None]

        ref_map = {r.key: r.reference_full for r in refs if hasattr(r, "key")}

        # -----------------------------
        # 4. DEBUG (VERY IMPORTANT)
        # -----------------------------
        print("📊 DEBUG COUNTS:",
              "citations:", len(citations),
              "refs:", len(refs),
              "c2r:", len(c2r),
              "missing:", len(missing))

        if not citations and not refs:
            print("⚠️ WARNING: No citations or references extracted")

        # -----------------------------
        # 5. Generate suggestions
        # -----------------------------
        suggestions_data = generate_suggestions(
            citations=citations,
            c2r=c2r,
            missing_rows=missing,
            references=refs,
            ref_map=ref_map
        )

        # -----------------------------
        # 6. Production safety
        # -----------------------------
        if not suggestions_data.get("citations") and not suggestions_data.get("missing"):
            print("ℹ️ No citation correction suggestions generated.")

        # -----------------------------
        # 7. Attach to result
        # -----------------------------
        result["autofix"] = {
            "enabled": True,
            "suggestions": suggestions_data
        }

    except Exception as e:
        print(f"[AUTOFIX ERROR] {e}")
        result["autofix"] = {
            "enabled": False,
            "suggestions": {
                "citations": [],
                "missing": [],
                "unmatched": [],
                "references": []
            }
        }

    return result
