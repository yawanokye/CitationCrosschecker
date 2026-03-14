# engine.py
__version__ = "2.0.0"

import io
import re
import unicodedata
from collections import defaultdict

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


# --------------------------------------------------
# Helper utilities
# --------------------------------------------------

def _safe_str(x):
    try:
        return str(x)
    except Exception:
        return ""


def norm_space(s: str) -> str:
    s = s or ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def soft_lower(s: str) -> str:
    return norm_space(s).lower()


# --------------------------------------------------
# DOCX extraction
# --------------------------------------------------

def read_docx_split_main_and_refs(file_bytes):

    if not DOCX_OK:
        raise RuntimeError("python-docx not installed")

    doc = Document(io.BytesIO(file_bytes))

    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]

    text = "\n".join(paragraphs)

    return split_main_and_references(text)


# --------------------------------------------------
# PDF extraction
# --------------------------------------------------

def read_pdf_split_main_and_refs(file_bytes):

    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")

    text_blocks = []

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text_blocks.append(t)

    except Exception as e:
        raise RuntimeError(f"PDF parsing failed: {str(e)}")

    text = "\n".join(text_blocks)

    return split_main_and_references(text)


# --------------------------------------------------
# Reference section detection
# --------------------------------------------------

REFERENCE_HEADINGS = [
    "references",
    "reference",
    "bibliography",
    "works cited",
    "literature cited"
]


def split_main_and_references(full_text):

    lines = full_text.splitlines()

    ref_index = None

    for i, line in enumerate(lines):

        clean = soft_lower(line)

        for h in REFERENCE_HEADINGS:
            if clean.startswith(h):
                ref_index = i
                break

        if ref_index is not None:
            break

    if ref_index is None:

        return (
            full_text,
            [],
            "Reference section not detected"
        )

    main_text = "\n".join(lines[:ref_index])

    references = [
        l.strip() for l in lines[ref_index + 1:]
        if l.strip()
    ]

    return main_text, references, "Reference section detected"


# --------------------------------------------------
# Citation extraction
# --------------------------------------------------

CITATION_PATTERNS = [

    # APA / Harvard
    r"\(([^()]*?\d{4}[a-z]?[^()]*)\)",

    # IEEE
    r"\[(\d+)\]",

    # Vancouver
    r"\b\d+\b"
]


def extract_intext_citations(text):

    citations = []

    for pat in CITATION_PATTERNS:

        matches = re.findall(pat, text)

        citations.extend(matches)

    return citations


# --------------------------------------------------
# Reference matching
# --------------------------------------------------

def match_citation_to_reference(citation, references):

    citation_low = soft_lower(citation)

    for ref in references:

        ref_low = soft_lower(ref)

        if citation_low in ref_low:
            return ref

    return None


# --------------------------------------------------
# Core crosscheck engine
# --------------------------------------------------

def run_crosscheck(file_bytes, filename, style="apa", verify_online=False):

    name = filename.lower()

    # ------------------------------
    # Load document
    # ------------------------------

    if name.endswith(".docx"):

        main_text, refs, msg = read_docx_split_main_and_refs(file_bytes)

    elif name.endswith(".pdf"):

        try:
            main_text, refs, msg = read_pdf_split_main_and_refs(file_bytes)

        except Exception as e:

            return {
                "filename": filename,
                "error": str(e),
                "summary": {
                    "in_text_citations_found": 0,
                    "reference_entries_found": 0,
                    "missing_in_references": 0,
                    "uncited_references": 0,
                    "match_rate": 0
                },
                "missing_in_references": [],
                "uncited_references": [],
                "reconciliation_intext_to_reference": [],
                "reconciliation_reference_to_intext": [],
                "references_raw": []
            }

    else:

        raise RuntimeError("Unsupported file type")


    # ------------------------------
    # Extract citations
    # ------------------------------

    citations = extract_intext_citations(main_text)

    reconciliation = []

    missing = []

    matched_refs = set()

    for c in citations:

        ref = match_citation_to_reference(c, refs)

        if ref:

            matched_refs.add(ref)

            reconciliation.append({

                "status": "matched",

                "in_text": c,

                "matched_reference": ref,

                "flags": ""
            })

        else:

            missing.append(c)

            reconciliation.append({

                "status": "missing",

                "in_text": c,

                "matched_reference": "",

                "flags": "not_found"
            })


    # ------------------------------
    # Uncited references
    # ------------------------------

    uncited = []

    for r in refs:

        if r not in matched_refs:
            uncited.append(r)


    # ------------------------------
    # Summary
    # ------------------------------

    count = len(citations)

    match_rate = 0

    if count:
        match_rate = round(
            100 * (count - len(missing)) / count,
            2
        )


    summary = {

        "in_text_citations_found": count,

        "reference_entries_found": len(refs),

        "missing_in_references": len(missing),

        "uncited_references": len(uncited),

        "match_rate": match_rate
    }


    # ------------------------------
    # Return result
    # ------------------------------

    result = {

        "filename": filename,

        "summary": summary,

        "missing_in_references": missing,

        "uncited_references": uncited,

        "reconciliation_intext_to_reference": reconciliation,

        "reconciliation_reference_to_intext": [],

        "references_raw": refs
    }

    return result
