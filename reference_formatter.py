# reference_formatter.py

import re
import io
import json
import argparse
from typing import List, Dict, Any, Optional, Union
from datetime import datetime

try:
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False
    print("[WARNING] python-docx not installed. DOCX export disabled.")

# ============================================================
# REFERENCE STYLES CONFIGURATION
# ============================================================

REFERENCE_STYLES = {
    "apa6": {
        "name": "APA 6th Edition",
        "description": "American Psychological Association, 6th edition (2009)",
        "example": "Smith, J. A. (2020). Title of article. *Journal Name*, 15(2), 123-145. doi:10.1234/example"
    },
    "apa7": {
        "name": "APA 7th Edition",
        "description": "American Psychological Association, 7th edition (2020)",
        "example": "Smith, J. A. (2020). Title of article. *Journal Name*, 15(2), 123-145. https://doi.org/10.1234/example"
    },
    "harvard": {
        "name": "Harvard Style",
        "description": "Harvard referencing style",
        "example": "Smith, JA (2020) 'Title of article', *Journal Name*, 15(2), pp. 123-145. doi:10.1234/example"
    }
}


def format_reference_numeric(reference: Dict[str, Any]) -> str:
    """Format a numbered-list entry in a conservative Vancouver-like order."""
    authors = parse_authors(reference.get("authors"))
    author_text = ", ".join(author.replace(", ", " ") for author in authors)
    title = str(reference.get("title") or "").strip().rstrip(".")
    source = str(reference.get("source") or reference.get("publisher") or "").strip().rstrip(".")
    year = str(reference.get("year") or "").strip()
    volume = str(reference.get("volume") or "").strip()
    issue = str(reference.get("issue") or "").strip()
    pages = str(reference.get("pages") or reference.get("article_number") or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(reference.get("doi") or "").strip(), flags=re.I)
    parts = []
    if author_text:
        parts.append(author_text.rstrip(".") + ".")
    if title:
        parts.append(title + ".")
    if reference.get("edition"):
        parts.append(str(reference["edition"]).strip().rstrip(".") + ".")
    publication = source
    if year:
        publication += (". " if publication else "") + year
    if volume:
        publication += (";" if year else ";") + volume
    if issue:
        publication += f"({issue})"
    if pages:
        publication += f":{pages}"
    if publication:
        parts.append(publication.rstrip(".") + ".")
    if doi:
        parts.append(f"doi:{doi}")
    return " ".join(parts).strip()

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def parse_author(author: Union[str, Dict]) -> str:
    """
    Parse various author formats to 'Last, F.' or 'Last, F. M.'
    
    Handles:
    - "abubakari, gross, boateng" -> error (needs first names)
    - "Smith, John A." -> "Smith, J. A."
    - "John A. Smith" -> "Smith, J. A."
    - "Smith, J. A." -> "Smith, J. A."
    - {"last": "Smith", "first": "John A."} -> "Smith, J. A."
    """
    # Handle dict input
    if isinstance(author, dict):
        last = author.get("last", "") or author.get("family", "")
        first = author.get("first", "") or author.get("given", "")
        if last:
            if first:
                # Extract initials
                initials = ''.join([name[0].upper() + '.' for name in first.split() if name[0].isalpha()])
                return f"{_name_case(last)}, {initials}"
            return _name_case(last)
        return ""
    
    # Handle string input
    if not author or not isinstance(author, str):
        return ""
    
    author = author.strip()
    if _is_corporate_author(author):
        return _name_case(author)
    if "," not in author:
        numeric_author = re.fullmatch(r"(.+?)\s+([^\W\d_]{1,5})", author, flags=re.UNICODE)
        if numeric_author and numeric_author.group(2).isupper():
            return f"{_name_case(numeric_author.group(1))}, " + " ".join(c + "." for c in numeric_author.group(2))
    
    # Already formatted as "Last, F." or "Last, F. M."
    if ',' in author:
        parts = author.split(',', 1)
        last = _name_case(parts[0].strip())
        first_part = parts[1].strip() if len(parts) > 1 else ""
        
        # Extract initials from first part
        if first_part:
            # Handle "J. A." or "John A." format
            initials = []
            for token in first_part.split():
                if token and token[0].isalpha():
                    initials.append('-'.join(part[0].upper() + '.' for part in token.split('-') if part and part[0].isalpha()))
            return f"{last}, {' '.join(initials)}"
        return last
    
    # "First Last" or "First Middle Last" format
    parts = author.split()
    if len(parts) >= 2:
        last = _name_case(parts[-1])
        initials = [p[0].upper() + '.' for p in parts[:-1] if p and p[0].isalpha()]
        return f"{last}, {' '.join(initials)}"
    
    # Single name
    return _name_case(author)


def _name_case(value: str) -> str:
    """Restore display capitalisation without altering already mixed-case names."""
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value or not (value.islower() or value.isupper()):
        return value
    def cap_piece(piece: str) -> str:
        return "-".join("'".join(part[:1].upper() + part[1:].lower() for part in bit.split("'")) for bit in piece.split("-"))
    return " ".join(cap_piece(piece) for piece in value.split())


def _is_corporate_author(value: str) -> bool:
    text = str(value or "").strip()
    markers = (
        "bank", "commission", "committee", "department", "directorate", "government", "institute",
        "ministry", "organisation", "organization", "project", "service", "university", "programme",
        "agency", "authority", "council", "office", "oecd", "undp", "unesco", "united nations",
        "editors", "consortium", "collaboration",
    )
    lower = text.casefold()
    return "," not in text and any(marker in lower for marker in markers)


def parse_authors(authors: Union[List, str, None]) -> List[str]:
    """Parse a list of authors from various input formats."""
    if not authors:
        return []
    
    if isinstance(authors, list):
        return [parse_author(a) for a in authors if a]
    
    if isinstance(authors, str):
        text = authors.strip()
        # Parse the named authors, never interpret "et al." as a personal name.
        text = re.sub(r",?\s*\bet\s+al\.?\s*$", "", text, flags=re.I).strip()
        numeric_parts = [part.strip() for part in text.split(",") if part.strip()]
        if numeric_parts and all(re.fullmatch(r".+?\s+[^\W\d_]{1,5}", part, re.UNICODE)
                                 and part.split()[-1].isupper() for part in numeric_parts):
            return [parse_author(part) for part in numeric_parts]
        # APA-style personal-author strings contain repeating "Surname, initials"
        # groups. A plain organisation name must remain one author.
        apa_initials = re.compile(r"([^,;&]+),\s*((?:[^\W\d_]\.(?:-[^\W\d_]\.)?\s*)+)(?=\s*,|\s*&|$)", re.UNICODE)
        pairs = list(apa_initials.finditer(text))
        if pairs and not re.search(r"\w", apa_initials.sub("", text).replace("&", "")):
            return [parse_author(m.group(1).strip() + ", " + m.group(2).strip()) for m in pairs]
        personal = re.findall(r"([^,;&]+,\s*(?:[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’-]*\.?\s*){1,4})(?=,\s*(?:&\s*)?[^,;&]+,|\s*&\s*|\s+and\s+|$)", text)
        if personal:
            return [parse_author(value.strip(" ,")) for value in personal]
        author_list = re.split(r'\s+and\s+|\s*&\s*|;\s*', text)
        return [parse_author(a.strip()) for a in author_list if a.strip()]
    
    return []


def sentence_case(text: str) -> str:
    """Preserve display spelling while capitalising the title and subtitle starts.

    Blind lowercasing corrupts proper nouns, acronyms and author-supplied names.
    The identity-safe audit uses the manuscript title, so this function makes
    only the deterministic changes that do not require semantic guessing.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""
    value = re.sub(r"^([^A-Za-zÀ-ÖØ-öø-ÿ]*)([A-Za-zÀ-ÖØ-öø-ÿ])", lambda m: m.group(1) + m.group(2).upper(), value)
    value = re.sub(r":\s*([A-Za-zÀ-ÖØ-öø-ÿ])", lambda m: ": " + m.group(1).upper(), value)
    return value


def format_doi(doi: str, style: str = "apa7") -> str:
    """Format DOI according to citation style."""
    if not doi:
        return ""
    
    # Clean DOI
    doi = doi.strip()
    doi = re.sub(r'^https?://doi\.org/', '', doi)
    doi = re.sub(r'^doi:', '', doi, flags=re.IGNORECASE)
    
    if style == "apa6":
        return f"doi:{doi}"
    elif style == "apa7":
        return f"https://doi.org/{doi}"
    elif style == "harvard":
        return f"doi:{doi}"
    else:
        return f"https://doi.org/{doi}"


# ============================================================
# APA 6th FORMATTER
# ============================================================

def _format_authors_apa6(authors: List[str], max_authors: int = 7) -> str:
    """Format authors for APA 6th style."""
    if not authors:
        return ""
    
    formatted = [a for a in authors if a]
    
    if len(formatted) > max_authors:
        formatted = formatted[:max_authors]
        formatted.append("…")
    
    if len(formatted) == 1:
        return formatted[0]
    elif len(formatted) == 2:
        return f"{formatted[0]}, & {formatted[1]}"
    else:
        return ", ".join(formatted[:-1]) + ", & " + formatted[-1]


def format_reference_apa6(reference: Dict[str, Any]) -> str:
    """Format a reference in APA 6th edition style."""
    # Extract with safe defaults
    authors = parse_authors(reference.get("authors", []))
    year = reference.get("year", "") or ""
    title = reference.get("title", "") or ""
    source = reference.get("source", "") or ""
    volume = reference.get("volume", "") or ""
    issue = reference.get("issue", "") or ""
    pages = reference.get("pages", "") or ""
    doi = reference.get("doi", "") or ""
    
    # Handle missing data
    if not authors and not title:
        return "[Incomplete reference]"
    
    authors_str = _format_authors_apa6(authors)
    year_str = f"({year})" if year else "(n.d.)"
    
    # Sentence case for article titles
    title_str = sentence_case(title) if title else "[No title]"
    
    # Journal/source name in italics
    source_str = f"*{source}*" if source else ""
    
    # Volume, issue, pages
    vol_issue_pages = ""
    if volume:
        vol_issue_pages = f", *{volume}*"
        if issue and issue != volume:
            vol_issue_pages += f"({issue})"
        if pages:
            vol_issue_pages += f", {pages}"
    
    # DOI
    doi_str = format_doi(doi, "apa6")
    
    # Build reference
    parts = [f"{authors_str} {year_str}.", f"{title_str}."]
    if source_str:
        parts.append(source_str + vol_issue_pages + ".")
    if doi_str:
        parts.append(doi_str)
    
    return " ".join(filter(None, parts))


# ============================================================
# APA 7th FORMATTER
# ============================================================

def _format_authors_apa7(authors: List[str], max_authors: int = 20) -> str:
    """Format authors for APA 7th style."""
    if not authors:
        return ""
    
    formatted = [a for a in authors if a]
    
    if len(formatted) > max_authors:
        formatted = formatted[:max_authors]
        formatted.append("…")
    
    if len(formatted) == 1:
        return formatted[0]
    elif len(formatted) == 2:
        return f"{formatted[0]}, & {formatted[1]}"
    else:
        return ", ".join(formatted[:-1]) + ", & " + formatted[-1]


def format_reference_apa7(reference: Dict[str, Any]) -> str:
    """Format a reference in APA 7th edition style."""
    # Extract with safe defaults
    authors = parse_authors(reference.get("authors", []))
    year = reference.get("year", "") or ""
    title = reference.get("title", "") or ""
    source = reference.get("source", "") or ""
    volume = reference.get("volume", "") or ""
    issue = reference.get("issue", "") or ""
    pages = reference.get("pages", "") or ""
    doi = reference.get("doi", "") or ""
    publisher = reference.get("publisher", "") or ""
    
    # Handle missing data
    if not authors and not title:
        return "[Incomplete reference]"
    
    authors_str = _format_authors_apa7(authors)
    year_str = f"({year})" if year else "(n.d.)"
    
    # Sentence case for article titles
    title_str = sentence_case(title) if title else "[No title]"
    
    # Determine reference type
    is_book = bool(publisher) and not source
    is_chapter = bool(reference.get("book_title", ""))
    
    if is_chapter:
        # Book chapter format
        book_title = reference.get("book_title", "") or ""
        editors = parse_authors(reference.get("editors", []))
        edition = reference.get("edition", "") or ""
        
        book_info = sentence_case(book_title) if book_title else ""
        if editors:
            editor_str = ", ".join(editors)
            if len(editors) == 1:
                editor_str += " (Ed.)"
            else:
                editor_str += " (Eds.)"
            book_info = f"In {editor_str}, {book_info}"
        
        if edition:
            book_info += f" ({edition} ed.)"
        
        if publisher:
            book_info += f" {publisher}"
        
        if pages:
            book_info += f" (pp. {pages})"
        
        parts = [f"{authors_str} {year_str}.", f"{title_str}."]
        if book_info:
            parts.append(book_info + ".")
        if doi:
            parts.append(format_doi(doi, "apa7"))
        
        return " ".join(filter(None, parts))
    
    elif is_book:
        # Book format
        edition = reference.get("edition", "") or ""
        
        book_info = f"*{sentence_case(title)}*"
        if edition:
            edition_text = str(edition).strip()
            if not re.search(r"\bed\.?$", edition_text, re.I):
                edition_text += " ed."
            book_info += f" ({edition_text})"
        author_identity = re.sub(r"[^a-z0-9]", "", authors_str.casefold())
        publisher_identity = re.sub(r"[^a-z0-9]", "", str(publisher).casefold())
        if publisher and author_identity != publisher_identity:
            book_info += f". {publisher}"
        
        parts = [f"{authors_str} {year_str}.", f"{book_info}."]
        if doi:
            parts.append(format_doi(doi, "apa7"))
        
        return " ".join(filter(None, parts))
    
    else:
        # Journal article format
        # Journal name in italics
        source_str = f"*{source}*" if source else ""
        
        # Volume, issue, pages (APA 7th uses no comma before volume)
        vol_issue_pages = ""
        if volume:
            if source_str:
                vol_issue_pages = f", *{volume}*"
            else:
                vol_issue_pages = f" {volume}"
            if issue and issue != volume:
                vol_issue_pages += f"({issue})"
            if pages:
                vol_issue_pages += f", {pages}"
        
        parts = [f"{authors_str} {year_str}.", f"{title_str}."]
        if source_str:
            parts.append(source_str + vol_issue_pages + ".")
        if doi:
            parts.append(format_doi(doi, "apa7"))
        
        return " ".join(filter(None, parts))


# ============================================================
# HARVARD FORMATTER
# ============================================================

def _format_authors_harvard(authors: List[str], max_authors: int = 3) -> str:
    """Format authors for Harvard style."""
    if not authors:
        return ""
    
    formatted = [a for a in authors if a]
    
    if len(formatted) > max_authors:
        return f"{formatted[0]} et al."
    
    if len(formatted) == 1:
        return formatted[0]
    elif len(formatted) == 2:
        return f"{formatted[0]} and {formatted[1]}"
    else:
        return ", ".join(formatted[:-1]) + ", and " + formatted[-1]


def format_reference_harvard(reference: Dict[str, Any]) -> str:
    """Format a reference in Harvard style."""
    # Extract with safe defaults
    authors = parse_authors(reference.get("authors", []))
    year = reference.get("year", "") or ""
    title = reference.get("title", "") or ""
    source = reference.get("source", "") or ""
    volume = reference.get("volume", "") or ""
    issue = reference.get("issue", "") or ""
    pages = reference.get("pages", "") or ""
    doi = reference.get("doi", "") or ""
    publisher = reference.get("publisher", "") or ""
    
    # Handle missing data
    if not authors and not title:
        return "[Incomplete reference]"
    
    authors_str = _format_authors_harvard(authors)
    year_str = f"({year})" if year else "(n.d.)"
    
    # Sentence case - Harvard uses single quotes around article titles
    title_str = sentence_case(title) if title else "[No title]"
    
    if publisher and not source:
        edition = str(reference.get("edition") or "").strip()
        book = f"*{title_str}*"
        if edition:
            book += f" ({edition})"
        return " ".join(filter(None, [f"{authors_str} {year_str}.", f"{book}.", f"{publisher}.", doi_str])).strip()

    # Journal name in italics
    source_str = f"*{source}*" if source else ""
    
    # Volume, issue, pages
    vol_issue_pages = ""
    if volume:
        vol_issue_pages = f", {volume}"
        if issue:
            vol_issue_pages += f"({issue})"
        if pages:
            vol_issue_pages += f", pp. {pages}"
    
    # DOI
    doi_str = format_doi(doi, "harvard")
    
    # Build reference - note: no quotes around title in Harvard for journals
    parts = [f"{authors_str} {year_str}.", f"'{title_str}'."]
    if source_str:
        parts.append(source_str + vol_issue_pages + ".")
    if doi_str:
        parts.append(doi_str)
    
    return " ".join(filter(None, parts))


# ============================================================
# MAIN FORMAT FUNCTION
# ============================================================

def format_reference(reference: Dict[str, Any], style: str = "apa7") -> str:
    """
    Format a reference in the specified style.
    
    Args:
        reference: Dict with keys: authors, year, title, source, volume, issue, pages, doi, publisher
        style: "apa6", "apa7", or "harvard"
    
    Returns:
        Formatted reference string
    """
    style_lower = style.lower()
    
    if style_lower.startswith("numeric_") or style_lower in {"vancouver", "ieee", "ama", "nature"}:
        return format_reference_numeric(reference)
    if style_lower == "apa6":
        return format_reference_apa6(reference)
    elif style_lower == "harvard":
        return format_reference_harvard(reference)
    else:
        return format_reference_apa7(reference)


def validate_reference(ref: Dict[str, Any]) -> List[str]:
    """Return list of missing required fields."""
    missing = []
    
    # At least one of authors or title should exist
    if not ref.get("authors") and not ref.get("title"):
        missing.append("authors or title")
    
    # Year is recommended
    if not ref.get("year"):
        missing.append("year (recommended)")
    
    # For journal articles, source is important
    if ref.get("type") == "article" and not ref.get("source"):
        missing.append("source/journal name")
    
    return missing


def format_verified_reference_list(
    verification_rows: List[Dict[str, Any]],
    style: str = "apa7"
) -> List[Dict[str, Any]]:
    """
    Format all verified references into a structured list.
    
    Args:
        verification_rows: List of verification results
        style: "apa6", "apa7", or "harvard"
    
    Returns:
        List of formatted references with metadata
    """
    formatted_refs = []
    
    for i, row in enumerate(verification_rows):
        status = row.get("status", "unknown")
        
        # Include verified, likely, and needs_review
        if status not in ["verified", "likely", "needs_review"]:
            continue
        
        # Build reference dict with all possible fields
        ref_dict = {
            "authors": row.get("matched_authors_full", row.get("matched_authors", row.get("authors", []))),
            "year": row.get("matched_year", row.get("year", "")),
            "title": row.get("matched_title", row.get("title", "")),
            "doi": row.get("doi", ""),
            "source": row.get("matched_container_title", row.get("journal", "")),
            "volume": row.get("matched_volume", row.get("volume", "")),
            "issue": row.get("matched_issue", row.get("issue", "")),
            "pages": row.get("matched_pages", row.get("pages", "")),
            "publisher": row.get("publisher", ""),
            "book_title": row.get("book_title", ""),
            "editors": row.get("editors", []),
            "edition": row.get("edition", ""),
            "type": row.get("type", "article")
        }
        
        # Format the reference
        formatted = format_reference(ref_dict, style)
        
        # Get validation issues
        issues = validate_reference(ref_dict)
        
        formatted_refs.append({
            "index": i + 1,
            "original_reference": row.get("reference", ""),
            "formatted_reference": formatted,
            "status": status,
            "doi": row.get("doi", ""),
            "title": ref_dict["title"],
            "year": ref_dict["year"],
            "authors": parse_authors(ref_dict["authors"]),
            "verification_score": row.get("score", 0),
            "validation_issues": issues
        })
    
    return formatted_refs


# ============================================================
# DOCX EXPORT FUNCTIONS
# ============================================================

def export_references_to_docx(
    formatted_refs: List[Dict[str, Any]],
    style: str = "apa7",
    title: str = "Verified Reference List"
) -> Optional[io.BytesIO]:
    """
    Generate a DOCX file with formatted reference list.
    
    Args:
        formatted_refs: List of formatted references
        style: Citation style used
        title: Document title
    
    Returns:
        BytesIO object containing DOCX file, or None if docx not available
    """
    if not DOCX_AVAILABLE:
        return None
    
    document = Document()
    
    # Add title
    title_para = document.add_heading(title, level=1)
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    # Add metadata
    doc_info = document.add_paragraph()
    doc_info.add_run(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    doc_info.add_run(f"Citation Style: {REFERENCE_STYLES.get(style, {}).get('name', style.upper())}\n")
    doc_info.add_run(f"Total References: {len(formatted_refs)}")
    doc_info.style.font.size = Pt(10)
    doc_info.style.font.italic = True
    
    document.add_paragraph()  # Spacer
    
    # Add summary statistics
    if formatted_refs:
        document.add_heading("Summary", level=2)
        
        status_counts = {}
        for ref in formatted_refs:
            status = ref.get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        
        summary_text = document.add_paragraph()
        summary_text.add_run(f"✅ Verified: {status_counts.get('verified', 0)}\n")
        summary_text.add_run(f"🔍 Likely: {status_counts.get('likely', 0)}\n")
        summary_text.add_run(f"⚠️ Needs Review: {status_counts.get('needs_review', 0)}")
        summary_text.style.font.size = Pt(11)
        
        document.add_paragraph()  # Spacer
    
    # Add references
    document.add_heading("References", level=2)
    
    for ref in formatted_refs:
        p = document.add_paragraph()
        
        # Add reference number
        p.add_run(f"{ref['index']}. ").bold = True
        
        # Add formatted reference
        p.add_run(ref['formatted_reference'])
        
        # Add status badge if needed
        if ref['status'] != 'verified':
            status_text = f" [{ref['status'].upper()}]"
            status_run = p.add_run(status_text)
            if ref['status'] == 'needs_review':
                status_run.font.color.rgb = RGBColor(255, 165, 0)
            elif ref['status'] == 'likely':
                status_run.font.color.rgb = RGBColor(0, 128, 0)
        
        # Add validation warnings
        if ref.get('validation_issues'):
            p.add_run(f"\n  ⚠️ Missing: {', '.join(ref['validation_issues'])}")
        
        # Add DOI link if available
        if ref.get('doi'):
            p.add_run(f"\n  DOI: {ref['doi']}")
        
        p.paragraph_format.space_after = Pt(6)
    
    # Add footer
    document.add_page_break()
    footer_para = document.add_paragraph()
    footer_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer_para.add_run(f"Generated by CiteIntegrity - {datetime.now().strftime('%Y-%m-%d')}")
    footer_para.style.font.size = Pt(8)
    
    # Save to BytesIO
    docx_buffer = io.BytesIO()
    document.save(docx_buffer)
    docx_buffer.seek(0)
    
    return docx_buffer


def export_references_to_html(
    formatted_refs: List[Dict[str, Any]],
    style: str = "apa7",
    title: str = "Verified Reference List"
) -> str:
    """
    Generate HTML version of formatted reference list.
    """
    style_name = REFERENCE_STYLES.get(style, {}).get("name", style.upper())
    
    # Count statuses
    status_counts = {}
    for ref in formatted_refs:
        status = ref.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>{title}</title>
        <style>
            body {{
                font-family: 'Times New Roman', Times, serif;
                font-size: 12pt;
                line-height: 1.5;
                margin: 1in;
            }}
            h1 {{
                text-align: center;
                font-size: 18pt;
                margin-bottom: 20px;
            }}
            h2 {{
                font-size: 14pt;
                margin-top: 20px;
                margin-bottom: 10px;
            }}
            .metadata {{
                font-size: 10pt;
                color: #666;
                margin-bottom: 20px;
                text-align: center;
            }}
            .reference {{
                margin-bottom: 12px;
                margin-left: 0.5in;
                text-indent: -0.5in;
            }}
            .reference-number {{
                font-weight: bold;
            }}
            .status-badge {{
                display: inline-block;
                padding: 2px 6px;
                border-radius: 4px;
                font-size: 9pt;
                font-weight: bold;
                margin-left: 8px;
            }}
            .status-verified {{ background-color: #d4edda; color: #155724; }}
            .status-likely {{ background-color: #d1ecf1; color: #0c5460; }}
            .status-needs_review {{ background-color: #fff3cd; color: #856404; }}
            .validation-warning {{
                color: #856404;
                font-size: 9pt;
                margin-left: 0.5in;
            }}
            .doi {{
                font-size: 9pt;
                color: #0066cc;
                margin-left: 0.5in;
            }}
            .summary {{
                background-color: #f8f9fa;
                padding: 10px;
                border-radius: 5px;
                margin-bottom: 20px;
            }}
            hr {{
                margin: 20px 0;
            }}
        </style>
    </head>
    <body>
        <h1>{title}</h1>
        <div class="metadata">
            Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}<br>
            Citation Style: {style_name}<br>
            Total References: {len(formatted_refs)}
        </div>
        
        <div class="summary">
            <strong>Summary:</strong><br>
            ✅ Verified: {status_counts.get('verified', 0)}<br>
            🔍 Likely: {status_counts.get('likely', 0)}<br>
            ⚠️ Needs Review: {status_counts.get('needs_review', 0)}
        </div>
        <hr>
    """
    
    for ref in formatted_refs:
        status_class = f"status-{ref['status']}"
        status_display = ref['status'].upper().replace('_', ' ')
        
        html += f"""
        <div class="reference">
            <span class="reference-number">{ref['index']}.</span> {ref['formatted_reference']}
            <span class="status-badge {status_class}">{status_display}</span>
        """
        
        if ref.get('validation_issues'):
            html += f"""
            <div class="validation-warning">
                ⚠️ Missing: {', '.join(ref['validation_issues'])}
            </div>
            """
        
        if ref.get('doi'):
            html += f"""
            <div class="doi">
                DOI: <a href="https://doi.org/{ref['doi']}" target="_blank">{ref['doi']}</a>
            </div>
            """
        
        html += "</div>"
    
    html += """
    </body>
    </html>
    """
    
    return html


# ============================================================
# COMMAND-LINE INTERFACE
# ============================================================

def load_references_from_file(filepath: str) -> List[Dict[str, Any]]:
    """Load references from JSON or text file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        if filepath.endswith('.json'):
            data = json.load(f)
            if isinstance(data, list):
                return data
            elif isinstance(data, dict) and 'references' in data:
                return data['references']
            else:
                return [data]
        else:
            # Assume plain text with one reference per line
            lines = f.readlines()
            return [{"raw_reference": line.strip(), "title": line.strip()} for line in lines if line.strip()]


def main():
    parser = argparse.ArgumentParser(description="Format references in APA6, APA7, or Harvard style")
    parser.add_argument("input", help="Input file (JSON or text)")
    parser.add_argument("--style", "-s", choices=["apa6", "apa7", "harvard"], default="apa7",
                        help="Citation style (default: apa7)")
    parser.add_argument("--output", "-o", help="Output file (.docx, .html, or .txt)")
    parser.add_argument("--format", "-f", choices=["docx", "html", "text"], default="text",
                        help="Output format (default: text)")
    parser.add_argument("--title", "-t", default="Verified Reference List",
                        help="Document title for DOCX/HTML output")
    
    args = parser.parse_args()
    
    # Load references
    try:
        references = load_references_from_file(args.input)
    except Exception as e:
        print(f"Error loading file: {e}")
        return
    
    # Format references
    formatted_refs = []
    for i, ref in enumerate(references):
        formatted = format_reference(ref, args.style)
        formatted_refs.append({
            "index": i + 1,
            "formatted_reference": formatted,
            "status": "verified",
            "doi": ref.get("doi", ""),
            "title": ref.get("title", ""),
            "year": ref.get("year", ""),
            "authors": parse_authors(ref.get("authors", [])),
            "validation_issues": validate_reference(ref)
        })
    
    # Output
    if args.output:
        if args.format == "docx":
            if DOCX_AVAILABLE:
                buffer = export_references_to_docx(formatted_refs, args.style, args.title)
                with open(args.output, 'wb') as f:
                    f.write(buffer.getvalue())
                print(f"Saved to {args.output}")
            else:
                print("ERROR: python-docx not installed. Install with: pip install python-docx")
        elif args.format == "html":
            html = export_references_to_html(formatted_refs, args.style, args.title)
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(html)
            print(f"Saved to {args.output}")
        else:
            with open(args.output, 'w', encoding='utf-8') as f:
                for ref in formatted_refs:
                    f.write(ref['formatted_reference'] + '\n\n')
            print(f"Saved to {args.output}")
    else:
        # Print to console
        for ref in formatted_refs:
            print(f"{ref['index']}. {ref['formatted_reference']}")
            if ref.get('validation_issues'):
                print(f"   ⚠️ Missing: {', '.join(ref['validation_issues'])}")
            print()


if __name__ == "__main__":
    main()
