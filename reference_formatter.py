# reference_formatter.py

import re
import io
from typing import List, Dict, Any, Optional
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

# ============================================================
# APA 6th FORMATTER
# ============================================================

def _format_authors_apa6(authors: List[str], max_authors: int = 7) -> str:
    """Format authors for APA 6th style."""
    if not authors:
        return ""
    
    formatted = []
    for i, author in enumerate(authors[:max_authors]):
        if isinstance(author, str):
            if ',' in author:
                formatted.append(author)
            else:
                parts = author.split()
                if len(parts) >= 2:
                    last = parts[-1]
                    first = parts[0][0] + "."
                    formatted.append(f"{last}, {first}")
                else:
                    formatted.append(author)
        elif isinstance(author, dict):
            last = author.get("last", "")
            first = author.get("first", "")
            if last:
                formatted.append(f"{last}, {first[0]}." if first else last)
    
    if len(authors) > max_authors:
        formatted.append("…")
    
    if len(formatted) == 1:
        return formatted[0]
    elif len(formatted) == 2:
        return f"{formatted[0]} & {formatted[1]}"
    else:
        return ", ".join(formatted[:-1]) + ", & " + formatted[-1]


def format_reference_apa6(reference: Dict[str, Any]) -> str:
    """Format a reference in APA 6th edition style."""
    authors = reference.get("authors", [])
    year = reference.get("year", "")
    title = reference.get("title", "")
    source = reference.get("source", "")
    volume = reference.get("volume", "")
    issue = reference.get("issue", "")
    pages = reference.get("pages", "")
    doi = reference.get("doi", "")
    
    authors_str = _format_authors_apa6(authors)
    year_str = f"({year})" if year else "(n.d.)"
    
    # Sentence case for article titles
    if title:
        title_str = title[0].upper() + title[1:].lower() if title else ""
    else:
        title_str = ""
    
    # Journal name in italics
    source_str = f" *{source}*" if source else ""
    
    # Volume, issue, pages
    vol_issue_pages = ""
    if volume:
        vol_issue_pages = f", *{volume}*"
        if issue and issue != volume:
            vol_issue_pages += f"({issue})"
        if pages:
            vol_issue_pages += f", {pages}"
    
    # DOI (APA 6th uses "doi:" prefix)
    doi_str = f" doi:{doi}" if doi else ""
    
    parts = [authors_str, year_str, f"{title_str}."]
    if source_str:
        parts.append(source_str + vol_issue_pages + ".")
    if doi_str:
        parts.append(doi_str)
    
    return " ".join(parts)


# ============================================================
# APA 7th FORMATTER
# ============================================================

def _format_authors_apa7(authors: List[str], max_authors: int = 20) -> str:
    """Format authors for APA 7th style."""
    if not authors:
        return ""
    
    formatted = []
    for i, author in enumerate(authors[:max_authors]):
        if isinstance(author, str):
            if ',' in author:
                formatted.append(author)
            else:
                parts = author.split()
                if len(parts) >= 2:
                    last = parts[-1]
                    first = parts[0][0] + "."
                    formatted.append(f"{last}, {first}")
                else:
                    formatted.append(author)
        elif isinstance(author, dict):
            last = author.get("last", "")
            first = author.get("first", "")
            if last:
                formatted.append(f"{last}, {first[0]}." if first else last)
    
    if len(authors) > max_authors:
        formatted.append("…")
    
    if len(formatted) == 1:
        return formatted[0]
    elif len(formatted) == 2:
        return f"{formatted[0]} & {formatted[1]}"
    else:
        return ", ".join(formatted[:-1]) + ", & " + formatted[-1]


def format_reference_apa7(reference: Dict[str, Any]) -> str:
    """Format a reference in APA 7th edition style."""
    authors = reference.get("authors", [])
    year = reference.get("year", "")
    title = reference.get("title", "")
    source = reference.get("source", "")
    volume = reference.get("volume", "")
    issue = reference.get("issue", "")
    pages = reference.get("pages", "")
    doi = reference.get("doi", "")
    
    authors_str = _format_authors_apa7(authors)
    year_str = f"({year})" if year else "(n.d.)"
    
    # Sentence case for article titles
    if title:
        title_str = title[0].upper() + title[1:].lower() if title else ""
    else:
        title_str = ""
    
    # Journal name in italics
    source_str = f" *{source}*" if source else ""
    
    # Volume, issue, pages (APA 7th uses no comma before volume)
    vol_issue_pages = ""
    if volume:
        vol_issue_pages = f" *{volume}*"
        if issue and issue != volume:
            vol_issue_pages += f"({issue})"
        if pages:
            vol_issue_pages += f", {pages}"
    
    # DOI (APA 7th uses https://doi.org/)
    doi_str = f" https://doi.org/{doi}" if doi else ""
    
    parts = [authors_str, year_str, f"{title_str}."]
    if source_str:
        parts.append(source_str + vol_issue_pages + ".")
    if doi_str:
        parts.append(doi_str)
    
    return " ".join(parts)


# ============================================================
# HARVARD FORMATTER
# ============================================================

def _format_authors_harvard(authors: List[str], max_authors: int = 3) -> str:
    """Format authors for Harvard style."""
    if not authors:
        return ""
    
    formatted = []
    for i, author in enumerate(authors[:max_authors]):
        if isinstance(author, str):
            if ',' in author:
                formatted.append(author)
            else:
                parts = author.split()
                if len(parts) >= 2:
                    last = parts[-1]
                    first = parts[0][0] + "."
                    formatted.append(f"{last}, {first}")
                else:
                    formatted.append(author)
        elif isinstance(author, dict):
            last = author.get("last", "")
            first = author.get("first", "")
            if last:
                formatted.append(f"{last}, {first[0]}." if first else last)
    
    if len(authors) > max_authors:
        return f"{formatted[0]} et al."
    
    if len(formatted) == 1:
        return formatted[0]
    elif len(formatted) == 2:
        return f"{formatted[0]} and {formatted[1]}"
    else:
        return ", ".join(formatted[:-1]) + ", and " + formatted[-1]


def format_reference_harvard(reference: Dict[str, Any]) -> str:
    """Format a reference in Harvard style."""
    authors = reference.get("authors", [])
    year = reference.get("year", "")
    title = reference.get("title", "")
    source = reference.get("source", "")
    volume = reference.get("volume", "")
    issue = reference.get("issue", "")
    pages = reference.get("pages", "")
    doi = reference.get("doi", "")
    
    authors_str = _format_authors_harvard(authors)
    year_str = f"({year})" if year else "(n.d.)"
    
    # Sentence case with single quotes for Harvard
    if title:
        title_str = title[0].upper() + title[1:].lower() if title else ""
    else:
        title_str = ""
    
    # Journal name in italics
    source_str = f" *{source}*" if source else ""
    
    # Volume, issue, pages
    vol_issue_pages = ""
    if volume:
        vol_issue_pages = f", {volume}"
        if issue:
            vol_issue_pages += f"({issue})"
        if pages:
            vol_issue_pages += f", pp. {pages}"
    
    # DOI
    doi_str = f" doi:{doi}" if doi else ""
    
    parts = [authors_str, year_str, f"'{title_str}'."]
    if source_str:
        parts.append(source_str + vol_issue_pages + ".")
    if doi_str:
        parts.append(doi_str)
    
    return " ".join(parts)


# ============================================================
# MAIN FORMAT FUNCTION
# ============================================================

def format_reference(reference: Dict[str, Any], style: str = "apa7") -> str:
    """
    Format a reference in the specified style.
    
    Args:
        reference: Dict with keys: authors, year, title, source, volume, issue, pages, doi
        style: "apa6", "apa7", or "harvard"
    
    Returns:
        Formatted reference string
    """
    style_lower = style.lower()
    
    if style_lower == "apa6":
        return format_reference_apa6(reference)
    elif style_lower == "harvard":
        return format_reference_harvard(reference)
    else:
        return format_reference_apa7(reference)


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
        
        # Parse authors from string or list
        authors = row.get("matched_authors", "")
        if isinstance(authors, str):
            authors_list = [a.strip() for a in authors.split(",") if a.strip()]
        else:
            authors_list = authors or []
        
        # Build reference dict
        ref_dict = {
            "authors": authors_list,
            "year": row.get("matched_year", ""),
            "title": row.get("matched_title", ""),
            "doi": row.get("doi", ""),
            "source": row.get("source", "")
        }
        
        # Format the reference
        formatted = format_reference(ref_dict, style)
        
        formatted_refs.append({
            "index": i + 1,
            "original_reference": row.get("reference", ""),
            "formatted_reference": formatted,
            "status": status,
            "doi": row.get("doi", ""),
            "title": ref_dict["title"],
            "year": ref_dict["year"],
            "authors": authors_list,
            "verification_score": row.get("score", 0)
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
        summary_para = document.add_heading("Summary", level=2)
        
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
    references_heading = document.add_heading("References", level=2)
    
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
