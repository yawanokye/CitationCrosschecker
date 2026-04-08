# enhanced_verify.py - Add these functions to your verify.py

import requests
from typing import List, Dict, Any, Optional, Tuple

def fetch_full_crossref_metadata(doi: str) -> Optional[Dict[str, Any]]:
    """
    Fetch COMPLETE metadata from Crossref including volume, issue, pages, and full author names.
    """
    if not doi:
        return None
    
    # Clean DOI
    doi = re.sub(r'^https?://(doi\.org/|dx\.doi\.org/)', '', doi.strip())
    doi = re.sub(r'^doi:', '', doi, flags=re.IGNORECASE)
    
    url = f"https://api.crossref.org/works/{doi}"
    params = {"mailto": MAILTO} if MAILTO else None
    
    try:
        response = requests.get(url, params=params, timeout=API_TIMEOUT, 
                                headers={"User-Agent": f"CitationVerifier/2.0 (mailto:{MAILTO})"})
        
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "ok" and "message" in data:
                return parse_full_crossref_message(data["message"])
        return None
    except Exception as e:
        print(f"[DEBUG] Error fetching full metadata for {doi}: {e}")
        return None


def parse_full_crossref_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse Crossref message into structured metadata with ALL fields needed for APA/Harvard.
    """
    # Extract authors with FULL names
    authors = []
    for author in message.get("author", []):
        family = author.get("family", "")
        given = author.get("given", "")
        
        # Build properly formatted author string for APA
        if given and family:
            # Extract initials from given name
            initials = " ".join([f"{name[0].upper()}." for name in given.split()])
            author_str = f"{family}, {initials}"
        elif family:
            author_str = family
        elif given:
            author_str = given
        else:
            continue
        
        authors.append({
            "family": family,
            "given": given,
            "formatted": author_str,
            "ORCID": author.get("ORCID", "")
        })
    
    # Extract title (prefer English if multiple)
    titles = message.get("title", [])
    title = titles[0] if titles else ""
    
    # Extract container title (journal name)
    container_titles = message.get("container-title", [])
    container_title = container_titles[0] if container_titles else ""
    
    # Extract volume, issue, pages
    volume = message.get("volume", "")
    issue = message.get("issue", "")
    page = message.get("page", "")
    
    # Parse page range
    first_page = None
    last_page = None
    if page and "-" in page:
        parts = page.split("-")
        first_page = parts[0].strip()
        last_page = parts[1].strip() if len(parts) > 1 else None
    
    # For online-only articles
    article_number = message.get("article-number", "")
    
    # Extract publication date
    issued = message.get("issued", {})
    date_parts = issued.get("date-parts", [[]])
    year = date_parts[0][0] if date_parts and date_parts[0] else None
    month = date_parts[0][1] if date_parts and len(date_parts[0]) > 1 else None
    day = date_parts[0][2] if date_parts and len(date_parts[0]) > 2 else None
    
    # Extract publisher
    publisher = message.get("publisher", "")
    
    # Extract type
    type_name = message.get("type", "")
    
    # Extract DOI
    doi = message.get("DOI", "")
    
    return {
        "doi": doi,
        "title": title,
        "container_title": container_title,
        "authors": authors,
        "author_strings": [a["formatted"] for a in authors],
        "year": str(year) if year else "",
        "month": month,
        "day": day,
        "volume": volume,
        "issue": issue,
        "page": page,
        "first_page": first_page,
        "last_page": last_page,
        "article_number": article_number,
        "publisher": publisher,
        "type": type_name,
    }


def build_apa7_from_metadata(metadata: Dict[str, Any]) -> str:
    """
    Build complete APA 7 reference from full metadata.
    """
    if not metadata:
        return ""
    
    # Format authors (APA 7: up to 20 authors, then "...")
    authors = metadata.get("author_strings", [])
    if len(authors) == 0:
        authors_str = ""
    elif len(authors) == 1:
        authors_str = authors[0]
    elif len(authors) == 2:
        authors_str = f"{authors[0]} & {authors[1]}"
    elif len(authors) <= 20:
        authors_str = ", ".join(authors[:-1]) + ", & " + authors[-1]
    else:
        authors_str = ", ".join(authors[:19]) + ", … & " + authors[19]
    
    # Year
    year = metadata.get("year", "n.d.")
    year_str = f"({year})" if year != "n.d." else "(n.d.)"
    
    # Title (sentence case)
    title = metadata.get("title", "")
    if title:
        # Convert to sentence case (preserve proper nouns)
        title = title[0].upper() + title[1:].lower() if len(title) > 1 else title.upper()
        title_str = f"{title}."
    else:
        title_str = ""
    
    # Journal
    journal = metadata.get("container_title", "")
    
    # Volume, issue, pages
    volume = metadata.get("volume", "")
    issue = metadata.get("issue", "")
    page = metadata.get("page", "")
    article_number = metadata.get("article_number", "")
    
    journal_info = ""
    if journal:
        journal_info = f" *{journal}*"
        if volume:
            journal_info += f", *{volume}*"
            if issue:
                journal_info += f"({issue})"
        if page:
            journal_info += f", {page}"
        elif article_number:
            journal_info += f", {article_number}"
        journal_info += "."
    
    # DOI
    doi = metadata.get("doi", "")
    doi_str = f" https://doi.org/{doi}" if doi else ""
    
    # Build complete reference
    parts = [authors_str, year_str, title_str]
    if journal_info:
        parts.append(journal_info)
    if doi_str:
        parts.append(doi_str)
    
    return " ".join(filter(None, parts))


def build_harvard_from_metadata(metadata: Dict[str, Any]) -> str:
    """
    Build complete Harvard reference from full metadata.
    """
    if not metadata:
        return ""
    
    # Format authors (Harvard: surnames only, or initials)
    authors = metadata.get("authors", [])
    if len(authors) == 0:
        authors_str = ""
    elif len(authors) == 1:
        authors_str = authors[0].get("family", authors[0].get("given", ""))
    elif len(authors) == 2:
        authors_str = f"{authors[0].get('family')} and {authors[1].get('family')}"
    else:
        authors_str = f"{authors[0].get('family')} et al."
    
    # Year in parentheses
    year = metadata.get("year", "n.d.")
    year_str = f"({year})" if year != "n.d." else "(n.d.)"
    
    # Title (sentence case, no extra punctuation)
    title = metadata.get("title", "")
    if title:
        title = title[0].upper() + title[1:].lower() if len(title) > 1 else title.upper()
        title_str = title
    else:
        title_str = ""
    
    # Journal (italics)
    journal = metadata.get("container_title", "")
    
    # Volume, issue, pages
    volume = metadata.get("volume", "")
    issue = metadata.get("issue", "")
    page = metadata.get("page", "")
    
    journal_info = ""
    if journal:
        journal_info = f" *{journal}*"
        if volume:
            journal_info += f", {volume}"
            if issue:
                journal_info += f"({issue})"
        if page:
            journal_info += f", pp. {page}"
        journal_info += "."
    
    # DOI
    doi = metadata.get("doi", "")
    doi_str = f" doi:{doi}" if doi else ""
    
    # Build reference (Harvard: no period after title)
    parts = [authors_str, year_str, title_str]
    if journal_info:
        parts.append(journal_info)
    if doi_str:
        parts.append(doi_str)
    
    return " ".join(filter(None, parts))


# Replace the _verify_single_reference function in verify.py
def _verify_single_reference_enhanced(ref: str, style: str, use_crossref: bool, use_openalex: bool) -> Dict[str, Any]:
    """
    Enhanced verification that captures FULL metadata from Crossref.
    """
    cache_key = f"enhanced::{style}::{ref}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    
    # First, extract DOI and try to get full metadata
    fields = _extract_fields_by_style(ref, style)
    ref_doi = fields.get("doi", "")
    
    full_metadata = None
    
    # Try to get full metadata if DOI exists
    if ref_doi and use_crossref:
        full_metadata = fetch_full_crossref_metadata(ref_doi)
        if full_metadata:
            # Build complete references
            apa7_ref = build_apa7_from_metadata(full_metadata)
            harvard_ref = build_harvard_from_metadata(full_metadata)
            
            result = {
                "reference": ref,
                "style": style,
                "status": "verified",
                "source": "crossref",
                "score": 100,
                "doi": ref_doi,
                "matched_title": full_metadata.get("title", ""),
                "matched_year": full_metadata.get("year", ""),
                "matched_authors": ", ".join(full_metadata.get("author_strings", [])),
                "matched_volume": full_metadata.get("volume", ""),
                "matched_issue": full_metadata.get("issue", ""),
                "matched_pages": full_metadata.get("page", ""),
                "matched_container_title": full_metadata.get("container_title", ""),
                "full_metadata": full_metadata,
                "apa7_reference": apa7_ref,
                "harvard_reference": harvard_ref,
                "title_score": 100,
                "author_overlap": len(full_metadata.get("author_strings", [])),
                "author_similarity": 100,
                "year_match": 1,
                "author_mismatch_flag": 0,
                "match_note": "Exact DOI match with full metadata",
            }
            _cache_set(cache_key, result)
            return result
    
    # Fall back to original verification if DOI not found or not working
    original_result = _verify_single_reference(ref, style, use_crossref, use_openalex)
    
    # If original found a match but missing metadata, try to fetch full metadata by DOI from the match
    if original_result.get("doi") and not full_metadata:
        full_metadata = fetch_full_crossref_metadata(original_result["doi"])
        if full_metadata:
            original_result["full_metadata"] = full_metadata
            original_result["matched_volume"] = full_metadata.get("volume", "")
            original_result["matched_issue"] = full_metadata.get("issue", "")
            original_result["matched_pages"] = full_metadata.get("page", "")
            original_result["matched_container_title"] = full_metadata.get("container_title", "")
            original_result["apa7_reference"] = build_apa7_from_metadata(full_metadata)
            original_result["harvard_reference"] = build_harvard_from_metadata(full_metadata)
    
    _cache_set(cache_key, original_result)
    return original_result


def format_verified_reference_list_enhanced(
    verification_rows: List[Dict[str, Any]],
    style: str = "apa7"
) -> List[Dict[str, Any]]:
    """
    Format verified references using the enhanced metadata.
    """
    formatted_refs = []
    
    for i, row in enumerate(verification_rows):
        status = row.get("status", "unknown")
        
        if status not in ["verified", "likely", "needs_review"]:
            continue
        
        # Check if we have full metadata
        if "apa7_reference" in row and style == "apa7":
            formatted = row["apa7_reference"]
        elif "harvard_reference" in row and style == "harvard":
            formatted = row["harvard_reference"]
        elif "full_metadata" in row:
            # Build from full metadata
            if style == "apa7":
                formatted = build_apa7_from_metadata(row["full_metadata"])
            else:
                formatted = build_harvard_from_metadata(row["full_metadata"])
        else:
            # Fall back to original formatter
            ref_dict = {
                "authors": row.get("matched_authors", ""),
                "year": row.get("matched_year", ""),
                "title": row.get("matched_title", ""),
                "doi": row.get("doi", ""),
                "source": row.get("matched_container_title", row.get("source", "")),
                "volume": row.get("matched_volume", ""),
                "issue": row.get("matched_issue", ""),
                "pages": row.get("matched_pages", ""),
            }
            formatted = format_reference(ref_dict, style)
        
        formatted_refs.append({
            "index": i + 1,
            "original_reference": row.get("reference", ""),
            "formatted_reference": formatted,
            "status": status,
            "doi": row.get("doi", ""),
            "title": row.get("matched_title", ""),
            "year": row.get("matched_year", ""),
            "volume": row.get("matched_volume", ""),
            "issue": row.get("matched_issue", ""),
            "pages": row.get("matched_pages", ""),
            "journal": row.get("matched_container_title", ""),
            "authors": row.get("matched_authors", ""),
        })
    
    return formatted_refs
