# engine.py
__version__ = "1.3.0"

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
    re.I
)

# Headings that indicate end of reference list
POST_REF_HEADINGS = [
    r"^\s*appendix\s*",
    r"^\s*appendices\s*",
    r"^\s*supplementary\s+(?:materials?|information|data)",
    r"^\s*supporting\s+(?:information|materials?)",
    r"^\s*online\s+resources?",
    r"^\s*additional\s+materials?",
    r"^\s*acknowledgements?\s*$",
    r"^\s*acknowledgments?\s*$",
]

DISCOURSE_PREFIXES = {
    "see", "e.g", "eg", "i.e", "ie",
    "as", "in", "for", "from", "to", "at", "on", "by", "with", "within",
    "according", "adapted", "based", "cited", "citing", "reported",
}

# Common false positive terms that aren't author names
FALSE_POSITIVE_AUTHORS = {
    "survey", "field", "fieldwork", "field work", "study", "studies",
    "research", "analysis", "data", "results", "figure", "table",
    "chapter", "section", "appendix", "supplement", "supplementary",
    "online", "web", "website", "retrieved", "accessed", "available",
    "university", "college", "institute", "department", "laboratory",
    "lab", "experiment", "experimental", "method", "methodology",
    "review", "literature", "systematic", "meta-analysis", "meta analysis",
    "trial", "clinical", "patient", "patients", "group", "groups",
    "participant", "participants", "author", "authors", "et al",
    "unpublished", "manuscript", "submitted", "forthcoming", "in press",
    "personal communication", "pers comm", "personal observation",
    "observation", "observations", "preprint", "pre-print", "archive",
    "database", "dataset", "data set", "code", "software", "package",
    "library", "version", "release", "manual", "documentation",
    "report", "technical report", "tech report", "working paper",
    "discussion paper", "conference paper", "conference proceeding",
    "proceeding", "proceedings", "abstract", "poster", "presentation",
    "talk", "keynote", "panel", "symposium", "workshop", "meeting",
    "annual meeting", "annual conference", "international conference",
    "national conference", "regional conference", "local conference",
    "email", "e-mail", "message", "correspondence", "conversation",
    "discussion", "interview", "phone call", "telephone call",
    "skype call", "zoom call", "video call", "video conference",
    "webinar", "seminar", "colloquium", "lecture", "class", "course",
    "thesis", "dissertation", "doctoral dissertation", "phd thesis",
    "master's thesis", "masters thesis", "undergraduate thesis",
    "honors thesis", "honours thesis", "capstone", "final project",
    "research project", "research paper", "term paper", "student paper",
    "student project", "class project", "group project", "team project",
    "collaborative project", "collaboration", "partnership",
    "consortium", "network", "association", "society", "academy",
    "foundation", "fund", "grant", "fellowship", "scholarship",
    "award", "prize", "honor", "honour", "distinction",
    "center", "centre", "unit", "division", "branch", "section",
    "office", "bureau", "agency", "administration", "government",
    "ministry", "department", "organization", "organisation",
    "company", "corporation", "firm", "business", "enterprise",
    "industry", "sector", "market", "economy", "economic",
    "social", "society", "cultural", "political", "policy",
    "public", "private", "nonprofit", "non-profit", "ngo",
    "international", "national", "regional", "local", "global",
    "world", "worldwide", "global", "international",
    "north", "south", "east", "west", "northern", "southern",
    "eastern", "western", "central", "rural", "urban", "suburban",
    "developed", "developing", "underdeveloped", "industrialized",
    "industrialised", "emerging", "transitional", "transitioning",
    "rich", "wealthy", "poor", "impoverished", "low-income",
    "middle-income", "high-income", "low-resource", "resource-poor",
    "resource-rich", "resource-wealthy", "resource-dependent",
    "resource-based", "resource-driven", "resource-focused",
    "resource-oriented", "resource-related", "resource-associated",
}

# Common first names and initials to filter out as author surnames
COMMON_FIRST_NAMES = {
    "john", "james", "robert", "michael", "william", "david", "richard",
    "charles", "joseph", "thomas", "christopher", "daniel", "paul",
    "mark", "donald", "george", "kenneth", "steven", "edward", "brian",
    "ronald", "anthony", "kevin", "jason", "matthew", "gary", "timothy",
    "jose", "larry", "jeffrey", "frank", "scott", "eric", "stephen",
    "andrew", "raymond", "gregory", "joshua", "jerry", "dennis",
    "mary", "patricia", "jennifer", "linda", "elizabeth", "barbara",
    "susan", "jessica", "sarah", "karen", "lisa", "nancy", "betty",
    "helen", "sandra", "donna", "carol", "ruth", "sharon", "michelle",
    "laura", "sarah", "kimberly", "deborah", "jessica", "shirley",
    "cynthia", "angela", "melissa", "brenda", "amy", "anna", "rebecca",
    "virginia", "kathleen", "pamela", "martha", "debra", "amanda",
    "stephanie", "carolyn", "christine", "marie", "janet", "catherine",
    "frances", "ann", "joyce", "diane", "alice", "julie", "heather",
    "teresa", "doris", "gloria", "evelyn", "jean", "cheryl", "mildred",
    "katherine", "joan", "ashley", "judith", "rose", "janice", "kelly",
    "nicole", "judy", "christina", "kathy", "theresa", "beverly",
    "denise", "tammy", "irene", "jane", "lori", "rachel", "marilyn",
    "andrea", "kathryn", "louise", "sara", "anne", "jacqueline",
    "wanda", "bonnie", "julia", "ruby", "lois", "tina", "phyllis",
    "norma", "paula", "diana", "annie", "lillian", "emily", "robin",
    "peggy", "crystal", "gladys", "rita", "dawn", "connie", "florence",
    "tracy", "edna", "tiffany", "carmen", "rosa", "cindy", "grace",
    "wendy", "victoria", "edith", "kim", "sherry", "sylvia", "josephine",
    "thelma", "shannon", "sheila", "ethel", "ellen", "elaine", "marjorie",
    "carrie", "charlotte", "monica", "esther", "pauline", "emma",
    "juanita", "anita", "rhonda", "hazel", "amber", "eva", "debbie",
    "april", "leslie", "clara", "lucille", "jamie", "joanne", "eleanor",
    "valerie", "danielle", "megan", "alicia", "suzanne", "michele",
    "gail", "bertha", "darlene", "veronica", "jill", "ernestine",
    "geraldine", "lauren", "cathy", "joann", "josephine", "lynn",
    "sally", "julie", "martha", "kathryn", "jennie", "nora", "margie",
    "nina", "cassandra", "leah", "penny", "kay", "priscilla", "naomi",
    "carole", "brandy", "olga", "billie", "dianne", "tracey", "leona",
    "jenny", "felicia", "sonia", "miriam", "velma", "becky", "bobbie",
    "violet", "kristina", "toni", "misty", "mae", "shelly", "daisy",
    "ramona", "sherri", "erika", "katrina", "claire", "lindsey",
    "lindsay", "geneva", "guadalupe", "belinda", "margarita", "sheryl",
    "cora", "faye", "ada", "natasha", "sabrina", "isabel", "margret",
    "hilda", "gwen", "jodi", "candace", "kenya", "alma", "kellie",
    "flora", "tanya", "maya", "jeanette", "phyllis", "grady", "bryce",
    "dewayne", "garret", "houston", "kasey", "kendall", "kent", "kip",
    "kory", "kurtis", "lacy", "lamar", "lando", "lane", "langston",
    "lashawn", "latrell", "laurance", "leif", "len", "lenny", "leon",
    "leonard", "les", "lesley", "lester", "levi", "lewis", "lincoln",
    "lindsay", "linwood", "lionel", "lloyd", "logan", "lon", "lonnie",
    "louie", "louis", "lowell", "loyd", "lucas", "luke", "lynwood",
}

# Common academic terms that might be mistaken for author names
ACADEMIC_TERMS = {
    "study", "studies", "research", "analysis", "analyses", "data",
    "results", "finding", "findings", "conclusion", "conclusions",
    "discussion", "method", "methods", "methodology", "methodologies",
    "approach", "approaches", "framework", "frameworks", "model",
    "models", "theory", "theories", "concept", "concepts", "construct",
    "constructs", "variable", "variables", "factor", "factors",
    "dimension", "dimensions", "component", "components", "element",
    "elements", "aspect", "aspects", "feature", "features",
    "characteristic", "characteristics", "property", "properties",
    "attribute", "attributes", "quality", "qualities", "indicator",
    "indicators", "measure", "measures", "measurement", "measurements",
    "assessment", "assessments", "evaluation", "evaluations",
    "examination", "examinations", "investigation", "investigations",
    "exploration", "explorations", "inquiry", "inquiries", "enquiry",
    "enquiries", "survey", "surveys", "questionnaire", "questionnaires",
    "interview", "interviews", "observation", "observations",
    "experiment", "experiments", "trial", "trials", "test", "tests",
    "testing", "pilot", "pilots", "case", "cases", "example", "examples",
    "instance", "instances", "sample", "samples", "population",
    "populations", "participant", "participants", "subject", "subjects",
    "respondent", "respondents", "informant", "informants", "group",
    "groups", "cohort", "cohorts", "panel", "panels", "wave", "waves",
    "phase", "phases", "stage", "stages", "step", "steps", "process",
    "processes", "procedure", "procedures", "protocol", "protocols",
    "technique", "techniques", "tool", "tools", "instrument",
    "instruments", "apparatus", "equipment", "device", "devices",
    "material", "materials", "stimulus", "stimuli", "item", "items",
    "question", "questions", "scale", "scales", "index", "indexes",
    "indices", "score", "scores", "rating", "ratings", "rank", "ranks",
    "ranking", "rankings", "classification", "classifications",
    "category", "categories", "type", "types", "kind", "kinds",
    "form", "forms", "mode", "modes", "pattern", "patterns",
    "trend", "trends", "theme", "themes", "topic", "topics",
    "subject", "subjects", "domain", "domains", "area", "areas",
    "field", "fields", "discipline", "disciplines", "specialty",
    "specialties", "specialization", "specializations",
}


# -----------------------------
# Small helpers
# -----------------------------
def norm_space(s: str) -> str:
    s = s or ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def soft_lower(s: str) -> str:
    return norm_space(s).lower()


def strip_punct(s: str) -> str:
    s = soft_lower(s)
    s = re.sub(r"[“”\"'’`]", "", s)
    s = re.sub(r"[^a-z0-9\s\-&]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_likely_author_name(text: str) -> bool:
    """Check if text is likely an author name (not a false positive)."""
    if not text:
        return False
    
    text_lower = text.lower().strip()
    
    # Check against false positive lists
    if text_lower in FALSE_POSITIVE_AUTHORS:
        return False
    
    if text_lower in COMMON_FIRST_NAMES:
        return False
    
    if text_lower in ACADEMIC_TERMS:
        return False
    
    # Check if it's a single common word
    words = text_lower.split()
    if len(words) == 1:
        single_word = words[0]
        if len(single_word) <= 2:  # Too short
            return False
        if single_word in FALSE_POSITIVE_AUTHORS:
            return False
        if single_word in COMMON_FIRST_NAMES:
            return False
        if single_word in ACADEMIC_TERMS:
            return False
    
    # Check for common false positive patterns
    false_patterns = [
        r"^(the|a|an|this|that|these|those|some|any|no|all|both|each|every|few|many|most|several)\s+",
        r"^(survey|study|research|analysis|data|results)\s+(of|on|in|from|by|with|about|regarding|concerning)$",
        r"^(figure|table|chapter|section|appendix)\s+\d+$",
        r"^(vol|volume|no|number|issue|part|suppl|supplement)\s+\d+$",
        r"^(p|pp|page|pages)\s+\d+$",
        r"^(et al|and others|and colleagues)\s*$",
        r"^(unpublished|submitted|forthcoming|in press)\s*$",
        r"^(personal communication|pers comm|personal observation)\s*$",
        r"^(conference|symposium|workshop|meeting|proceeding)\s+",
        r"^(university|college|institute|school|department)\s+",
        r"^(laboratory|lab|center|centre|unit|division)\s+",
        r"^(government|ministry|agency|bureau|office)\s+",
        r"^(organization|organisation|company|corporation|firm)\s+",
        r"^(international|national|regional|local|global)\s+",
    ]
    
    for pattern in false_patterns:
        if re.search(pattern, text_lower):
            return False
    
    # Check if it contains any non-name characters
    if re.search(r"[0-9_+=<>@#$%^&*()\[\]{}|\\:;\"',.?/~`]", text):
        # Allow commas, periods, hyphens in names
        allowed = re.sub(r"[,\-\.\s]", "", text)
        if re.search(r"[0-9_+=<>@#$%^&*()\[\]{}|\\:;\"'/?~`]", allowed):
            return False
    
    # Check if it's all uppercase (acronyms are often organizations)
    if text.isupper() and len(text) <= 8:
        return True  # Acronyms can be valid (e.g., NASA, WHO)
    
    # Check if it has at least one capital letter (likely a name)
    if not any(c.isupper() for c in text if c.isalpha()):
        # All lowercase might be a false positive
        if len(words) == 1 and len(text) > 3:
            return False
    
    return True


def _first_author_or_org_key(author_left: str) -> str:
    """Return a stable key using the *first* author surname or an acronym in brackets.
    Examples:
      - "Bartlett, J. E., Kotrlik, J. W., & Higgins, C. C." -> "bartlett"
      - "United Nations Conference on Trade and Development (UNCTAD)" -> "unctad"
      - "Adam's" -> "adam"
    """
    s = norm_space(author_left)

    # Prefer acronym in brackets, e.g. "(UNCTAD)"
    m = re.search(r"\(([A-Z]{2,10})\)", s)
    if m:
        acronym = m.group(1)
        # Check if acronym is likely valid
        if acronym.isupper() and 2 <= len(acronym) <= 8:
            return strip_punct(acronym)

    # Remove numbering
    s = re.sub(r"^\[\s*\d{1,4}\s*\]\s*", "", s).strip()
    s = re.sub(r"^\d{1,4}[.)]\s*", "", s).strip()

    # Remove year if it leaked in
    s = re.sub(r"\(\s*(?:1[6-9]\d{2}|20\d{2})(?:[a-z])?\s*\).*", "", s).strip()

    # Remove possessive on author token
    s = re.sub(r"(’s|'s)\b", "", s)

    # Split on separators, keep the first author part
    s0 = re.split(r"\s+(?:&|and)\s+|,", s, maxsplit=1)[0].strip()

    # Handle "et al."
    s0 = re.sub(r"\bet\s+al\.?\b", "", s0, flags=re.I).strip()

    # Filter out false positives
    if not is_likely_author_name(s0):
        # Try to extract a meaningful part
        words = s0.split()
        if words:
            # Use the last word if it's capitalized (likely surname)
            last_word = words[-1]
            if last_word and last_word[0].isupper():
                return strip_punct(last_word)
        return ""

    toks = [t for t in re.split(r"\s+", s0) if t and re.search(r"[A-Za-z0-9]", t)]
    if not toks:
        return ""
    # Use the last alpha-numeric token as the surname/acronym
    return strip_punct(toks[-1])


def _looks_like_toc_references_line(s: str, tail: str) -> bool:
    """Detect TOC/header lines like:
      - 'REFERENCES 60'
      - 'References ....... 234'
    """
    if not s:
        return False
    tail = (tail or "").strip()
    if tail and re.fullmatch(r"\d{1,4}", tail):
        return True
    if re.search(r"\.{2,}\s*\d{1,4}\s*$", s):
        return True
    return False


def _is_post_reference_heading(s: str) -> bool:
    """Check if line indicates end of reference list (appendix, supplementary, etc.)."""
    s_lower = s.lower().strip()
    for pat in POST_REF_HEADINGS:
        if re.search(pat, s_lower, re.I):
            return True
    return False


# -----------------------------
# DOCX extraction (robust)
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
    """Extract DOCX text from XML parts to catch:
    - textboxes/shapes
    - headers/footers
    - footnotes/endnotes
    """
    import zipfile
    import xml.etree.ElementTree as ET

    NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

    def _extract_from_xml(xml_bytes: bytes) -> List[str]:
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

    lines: List[str] = []
    try:
        lines = _docx_xml_text(file_bytes)
    except Exception:
        lines = []

    if not lines:
        doc = Document(io.BytesIO(file_bytes))
        lines = list(_iter_docx_text(doc))

    main_lines: List[str] = []
    ref_lines: List[str] = []
    in_refs = False
    heading_line = ""

    for t in lines:
        # Check for post-reference headings when in reference section
        if in_refs and _is_post_reference_heading(t):
            in_refs = False
            main_lines.append(t)  # Add to main text instead of references
            continue

        if not in_refs:
            # strict heading
            for pat in REF_HEADINGS:
                if re.search(pat, t, flags=re.I):
                    in_refs = True
                    heading_line = t
                    break

            # relaxed heading with TOC guard
            if not in_refs:
                m = REF_HEADING_RELAXED.search(t)
                if m and m.start() <= 4 and len(t) <= 160:
                    tail = t[m.end():].strip(" :-\t")
                    if _looks_like_toc_references_line(t, tail):
                        main_lines.append(t)
                        continue
                    in_refs = True
                    heading_line = t
                    if tail:
                        ref_lines.append(tail)
                    continue

        if in_refs:
            ref_lines.append(t)
        else:
            main_lines.append(t)

    msg = f"Found References heading: {heading_line}" if in_refs else "No References heading found."
    return "\n".join(main_lines).strip(), ref_lines, msg


# -----------------------------
# PDF extraction + heading detection
# -----------------------------
def read_pdf_text(file_bytes: bytes) -> str:
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")

    out = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            out.append((text or "").replace("\x00", " "))
    return "\n".join(out)


def _looks_like_new_numeric_reference_start(s: str) -> bool:
    s0 = (s or "").strip()
    if not s0:
        return False

    if re.match(r"^\[\s*\d{1,4}\s*\]\s+\S", s0):
        return True
    if re.match(r"^\(\s*\d{1,4}\s*\)\s+\S", s0):
        return True

    m = re.match(r"^(\d{1,4})([.)])\s+(.+)$", s0)
    if m:
        num = m.group(1)
        if YEAR_RE.fullmatch(num):
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
        a = re.sub(r"[^A-Za-z,\.\-\s&]", "", a).strip()
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
        lookahead = [ln for ln in lines[i + 1:i + 31] if (ln or "").strip()]
        if _count_reference_like(lookahead, style_hint=style_hint) >= 3:
            return i, tail

    return -1, ""


# -----------------------------
# Merge and split reference lines
# -----------------------------
def _merge_reference_lines(raw_lines: List[str]) -> List[str]:
    raw_lines = [ln.strip() for ln in raw_lines if ln and ln.strip()]
    if not raw_lines:
        return []

    merged: List[str] = []
    cur = ""
    for ln in raw_lines:
        s = ln.strip()
        if not s:
            continue

        is_new = _looks_like_new_numeric_reference_start(s) or _looks_like_new_apa_reference_start(s)
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

    return [m for m in merged if m and len(m) >= 8]


def _split_embedded_numeric_refs(merged: List[str]) -> List[str]:
    """Split when multiple numeric references got glued together in PDFs,
    e.g. '[36] ... [37] ... [38] ...'
    """
    out: List[str] = []
    br_pat = re.compile(r"(?=(\[\s*\d{1,4}\s*\]\s+))")
    dot_pat = re.compile(r"(?=(\b\d{1,4}[\.\)]\s+))")

    for s in merged:
        s = (s or "").strip()
        if not s:
            continue

        cuts = []

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

    return [x for x in out if x and len(x) >= 10]


# -----------------------------
# Citation extractors
# -----------------------------
def extract_author_year_citations(text: str) -> List[str]:
    t = (text or "").replace("\u2019", "'")

    # Parenthetical: (Author, 2020; Author2, 2021)
    paren_pat = re.compile(
        r"\(([^()]{0,260}?\b(?:19|20)\d{2}[a-z]?\b[^()]{0,260}?)\)"
    )

    # Narrative: Bartlett, Kotrlik, and Higgins (2001); Adam's (2020); Bartlett & Higgins (2001); Bartlett et al. (2001)
    S = r"[A-Z][A-Za-z'\-]+(?:'s)?"
    narr_pat = re.compile(
        rf"\b("
        rf"(?:{S}(?:\s*,\s*{S}){{0,4}}(?:\s*,?\s*(?:&|and)\s*{S})?)"
        rf"|(?:{S}\s+(?:&|and)\s+{S})"
        rf"|(?:{S}\s+et\s+al\.)"
        rf")\s*\(\s*((?:19|20)\d{{2}}[a-z]?)\s*\)"
    )

    out: List[str] = []

    # parenthetical chunks
    for m in paren_pat.finditer(t):
        inside = m.group(1)
        chunks = [c.strip() for c in inside.split(";") if c.strip()]
        for ch in chunks:
            ch2 = re.sub(r"\b(p|pp)\.?\s*\d+(\s*[-–]\s*\d+)?\b", "", ch, flags=re.I).strip()
            if YEAR_RE.search(ch2):
                # Extract author part and check if likely a real author
                author_part = re.sub(r",\s*\d{4}[a-z]?\s*$", "", ch2)
                author_part = re.sub(r"\s*\(\d{4}[a-z]?\)\s*$", "", author_part)
                if is_likely_author_name(author_part):
                    out.append(norm_space(ch2))

    # narrative
    for m in narr_pat.finditer(t):
        author = m.group(1).strip()
        year = m.group(2).strip()
        # remove possessive: Adam's (2020) -> Adam (2020)
        author = re.sub(r"(’s|'s)\b", "", author).strip()
        # Check if author is likely a real author
        if is_likely_author_name(author):
            out.append
