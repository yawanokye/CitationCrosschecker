# =========================================
# formatter.py — CiteIntegrity Pro Complete
# Smart Reference Detection + DOI Auto-discovery + Retraction Detection
# =========================================

import re
import json
import time
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from difflib import SequenceMatcher
import traceback

# Try to import Flask
try:
    from flask import Flask, request, jsonify, render_template_string
    from flask_cors import CORS
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False
    print("Warning: Flask not installed. Run: pip install Flask flask-cors")

import requests

# =========================================
# CONFIGURATION
# =========================================
CACHE_DOI_LOOKUPS = {}
CACHE_SEARCH_RESULTS = {}
CACHE_RETRACTION_STATUS = {}
OPENALEX_MAX_RESULTS = 5
CROSSREF_TIMEOUT = 10
OPENALEX_TIMEOUT = 10
RETRACTION_WATCH_TIMEOUT = 10
MAX_RETRIES = 2

# =========================================
# 1. RETRACTION DETECTION
# =========================================

def check_retraction_status(doi: str) -> Dict:
    """Check if an article has been retracted using multiple sources"""
    if not doi:
        return {"is_retracted": False, "source": None, "reason": None}
    
    # Check cache
    if doi in CACHE_RETRACTION_STATUS:
        return CACHE_RETRACTION_STATUS[doi]
    
    result = {"is_retracted": False, "source": None, "reason": None}
    
    # Method 1: Check Crossref retraction notices
    try:
        url = f"https://api.crossref.org/works/{doi}"
        response = requests.get(url, timeout=CROSSREF_TIMEOUT)
        
        if response.status_code == 200:
            data = response.json().get("message", {})
            
            # Check for retraction notice
            if data.get("relation", {}).get("has-retraction"):
                result["is_retracted"] = True
                result["source"] = "Crossref"
                result["reason"] = "Retraction notice found"
            
            # Check for retraction status in update policy
            update_policy = data.get("update-policy")
            if update_policy and "retract" in str(update_policy).lower():
                result["is_retracted"] = True
                result["source"] = "Crossref"
                result["reason"] = "Marked as retracted in update policy"
    
    except Exception as e:
        print(f"Crossref retraction check error: {e}")
    
    # Method 2: Check OpenAlex for retraction status
    if not result["is_retracted"]:
        try:
            url = f"https://api.openalex.org/works/https://doi.org/{doi}"
            response = requests.get(url, timeout=OPENALEX_TIMEOUT)
            
            if response.status_code == 200:
                data = response.json()
                
                # Check for retraction status in OpenAlex
                if data.get("retraction"):
                    result["is_retracted"] = True
                    result["source"] = "OpenAlex"
                    result["reason"] = data.get("retraction_reason", "Retracted")
                
                # Check for duplicate with retraction notice
                duplicate_of = data.get("duplicate_of")
                if duplicate_of and "retract" in str(duplicate_of).lower():
                    result["is_retracted"] = True
                    result["source"] = "OpenAlex"
                    result["reason"] = "Duplicate of retracted article"
        
        except Exception as e:
            print(f"OpenAlex retraction check error: {e}")
    
    # Cache the result
    CACHE_RETRACTION_STATUS[doi] = result
    return result

def is_retracted(doi: str) -> bool:
    """Quick check if article is retracted"""
    return check_retraction_status(doi)["is_retracted"]

# =========================================
# 2. ENHANCED REFERENCE DETECTION
# =========================================

def detect_messy_reference(text: str) -> Dict:
    """Detect if a reference is messy and identify its type"""
    text_lower = text.lower()
    
    # Calculate messiness score (0 = clean, 100 = very messy)
    messiness_score = 0
    
    # Check for missing punctuation
    if not any(p in text for p in ['.', ',', ';', ':']):
        messiness_score += 30
    
    # Check for excessive spaces
    if re.search(r'\s{3,}', text):
        messiness_score += 20
    
    # Check for missing author format
    if not re.search(r'[A-Z][a-z]+,\s+[A-Z]\.', text) and not re.search(r'[A-Z][a-z]+\s+[A-Z]\.', text):
        messiness_score += 15
    
    # Check for missing year
    if not re.search(r'\b(19|20)\d{2}\b', text):
        messiness_score += 20
    
    # Check for missing title capitalization
    if text.isupper():
        messiness_score += 15
    
    # Detect reference type
    ref_type = "journal"  # default
    if "book" in text_lower or "press" in text_lower or "university" in text_lower:
        ref_type = "book"
    elif "www." in text_lower or "http" in text_lower:
        ref_type = "webpage"
    elif "report" in text_lower or "working paper" in text_lower:
        ref_type = "report"
    elif "conference" in text_lower or "proceedings" in text_lower:
        ref_type = "conference"
    
    return {
        "is_messy": messiness_score > 30,
        "messiness_score": messiness_score,
        "ref_type": ref_type,
        "suggested_fixes": []
    }

def smart_parse_reference(raw_reference: str, source_type: str = "auto") -> dict:
    """Intelligently parse any reference format"""
    raw = " ".join(raw_reference.strip().split())
    
    # First, detect if it's messy
    detection = detect_messy_reference(raw)
    
    # Try to normalize the text first
    normalized = normalize_messy_text(raw)
    
    parsed = {
        "authors": "",
        "year": "",
        "title": "",
        "source": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "publisher": "",
        "doi": "",
        "url": "",
        "original_text": raw_reference,
        "is_messy": detection["is_messy"],
        "messiness_score": detection["messiness_score"],
        "ref_type": detection["ref_type"] if source_type == "auto" else source_type,
        "is_retracted": False,
        "retraction_reason": None
    }
    
    # Auto-detect source type if not specified
    if source_type == "auto":
        source_type = detection["ref_type"]
    
    # Extract DOI first (most reliable)
    doi_match = re.search(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", normalized, re.I)
    if doi_match:
        parsed["doi"] = doi_match.group(1).rstrip(".,;")
    
    # Extract URL
    url_match = re.search(r"(https?://\S+)", normalized, re.I)
    if url_match:
        parsed["url"] = url_match.group(1).rstrip(".,;")
    
    # Extract year
    year_match = re.search(r"\b(19|20)\d{2}[a-z]?\b", normalized)
    if year_match:
        parsed["year"] = year_match.group(0)
    
    # Smart author extraction
    parsed["authors"] = smart_extract_authors(normalized, parsed["year"])
    
    # Smart title extraction
    parsed["title"] = smart_extract_title(normalized, parsed["year"], parsed["authors"])
    
    # Extract source (journal/book title)
    parsed["source"] = smart_extract_source(normalized, parsed["title"], parsed["year"])
    
    # Extract volume, issue, pages
    vip_patterns = [
        r"(\d+)\s*\((\d+)\)\s*[,:]\s*([\d\-–]+)",  # 14(2), 363-389
        r"(\d+)\s*[,:]\s*([\d\-–]+)",               # 14, 363-389
        r"vol\.?\s*(\d+)[,;\s]+(?:no\.?|iss\.?)\s*(\d+)[,;\s]+pp?\.?\s*([\d\-–]+)",  # vol.14, no.2, pp.363-389
        r"(\d+)[\s\-]+([\d\-–]+)$"                  # 14 363-389 at end
    ]
    
    for pattern in vip_patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            groups = match.groups()
            if len(groups) >= 2:
                parsed["volume"] = groups[0]
                if len(groups) >= 3:
                    parsed["issue"] = groups[1]
                    parsed["pages"] = groups[2]
                else:
                    parsed["pages"] = groups[1]
            break
    
    # If no pages found, try simple page pattern
    if not parsed["pages"]:
        pages_match = re.search(r"pp?\.?\s*([\d\-–]+)", normalized, re.I)
        if pages_match:
            parsed["pages"] = pages_match.group(1)
    
    # Clean up all fields
    parsed = clean_parsed_fields(parsed)
    
    return parsed

def normalize_messy_text(text: str) -> str:
    """Normalize messy reference text"""
    # Remove excessive whitespace
    text = re.sub(r'\s+', ' ', text)
    
    # Fix common OCR errors
    replacements = {
        r'ﬁ': 'fi',
        r'ﬂ': 'fl',
        r'ﬀ': 'ff',
        r'’': "'",
        r'“': '"',
        r'”': '"',
        r'–': '-',
        r'—': '-',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    
    # Fix spacing around punctuation
    text = re.sub(r'\s+([.,;:!?])', r'\1', text)
    text = re.sub(r'([.,;:!?])\s+', r'\1 ', text)
    
    # Fix common patterns
    text = re.sub(r'\.([A-Z])', r'. \1', text)  # Add space after period before capital
    
    return text.strip()

def smart_extract_authors(text: str, year: str) -> str:
    """Intelligently extract authors from messy text"""
    if not year:
        # Try to find author pattern at beginning
        author_match = re.match(r'^([A-Z][a-z]+(?:[,;]\s*[A-Z][a-z]+)*)', text)
        if author_match:
            authors = author_match.group(1)
            return clean_authors(authors)
        return ""
    
    # Split by year
    parts = re.split(rf'\(?{re.escape(year)}\)?', text, maxsplit=1)
    if len(parts) >= 2:
        author_part = parts[0].strip(" .,;:()")
        
        # Remove common non-author patterns
        author_part = re.sub(r'^(In|In:|In\s+)?', '', author_part)
        author_part = re.sub(r'\s+(and|&)\s+', ' & ', author_part)
        
        # Clean up
        author_part = re.sub(r'[,;]\s*$', '', author_part)
        
        return clean_authors(author_part)
    
    return ""

def smart_extract_title(text: str, year: str, authors: str) -> str:
    """Intelligently extract title from messy text"""
    if not year:
        return ""
    
    # Remove authors and year
    remaining = text
    if authors and authors in remaining:
        remaining = remaining.replace(authors, "", 1)
    
    # Split by year
    parts = re.split(rf'\(?{re.escape(year)}\)?', remaining, maxsplit=1)
    if len(parts) >= 2:
        title_part = parts[1].strip(" .,;:()")
        
        # Title usually ends before source or volume
        # Look for patterns that indicate end of title
        end_patterns = [
            r'\.\s+[A-Z][a-z]+',  # period followed by capital word (source)
            r'\.\s+\d+',           # period followed by number (volume)
            r'\.\s+In\s+',         # period followed by "In"
            r'\.\s+https?://',     # period followed by URL
        ]
        
        for pattern in end_patterns:
            match = re.search(pattern, title_part)
            if match:
                title_part = title_part[:match.start()]
                break
        
        # Clean up title
        title_part = re.sub(r'["\'\[\]{}]', '', title_part)
        title_part = title_part.strip(" .,;:")
        
        # Capitalize first letter of title
        if title_part and title_part[0].islower():
            title_part = title_part[0].upper() + title_part[1:]
        
        return title_part
    
    return ""

def smart_extract_source(text: str, title: str, year: str) -> str:
    """Intelligently extract source (journal/book title)"""
    if not title or not year:
        return ""
    
    # Remove authors, year, and title
    remaining = text
    if title in remaining:
        remaining = remaining.replace(title, "", 1)
    
    # Split by year
    parts = re.split(rf'\(?{re.escape(year)}\)?', remaining, maxsplit=1)
    if len(parts) >= 2:
        source_part = parts[1].strip(" .,;:()")
        
        # Source is usually before volume/pages
        # Remove volume, issue, pages
        source_part = re.sub(r'\d+\s*\(?\d*\)?\s*[,:]\s*[\d\-–]+.*$', '', source_part)
        source_part = re.sub(r'vol\.?\s*\d+.*$', '', source_part, re.I)
        
        # Clean up
        source_part = re.sub(r'^\.\s+', '', source_part)
        source_part = source_part.strip(" .,;:")
        
        return source_part
    
    return ""

def clean_parsed_fields(parsed: dict) -> dict:
    """Clean all parsed fields"""
    for key in parsed:
        if isinstance(parsed[key], str):
            # Remove extra spaces
            parsed[key] = re.sub(r'\s+', ' ', parsed[key]).strip()
            # Remove trailing punctuation
            parsed[key] = re.sub(r'[,;:.]$', '', parsed[key])
    
    return parsed

# =========================================
# 3. ENHANCED DOI DISCOVERY (Crossref + OpenAlex)
# =========================================

def search_doi_by_metadata(title: str, authors: str = "", year: str = "") -> Optional[str]:
    """Search for DOI using metadata (title, authors, year)"""
    if not title:
        return None
    
    # Create cache key
    cache_key = f"{title}_{authors}_{year}"
    if cache_key in CACHE_SEARCH_RESULTS:
        return CACHE_SEARCH_RESULTS[cache_key]
    
    # Try Crossref search first
    doi = search_crossref_by_metadata(title, authors, year)
    if doi:
        CACHE_SEARCH_RESULTS[cache_key] = doi
        return doi
    
    # Try OpenAlex search
    doi = search_openalex_by_metadata(title, authors, year)
    if doi:
        CACHE_SEARCH_RESULTS[cache_key] = doi
        return doi
    
    CACHE_SEARCH_RESULTS[cache_key] = None
    return None

def search_crossref_by_metadata(title: str, authors: str = "", year: str = "") -> Optional[str]:
    """Search Crossref API for DOI by metadata"""
    try:
        # Build query
        query = title
        if authors:
            # Extract last name of first author
            first_author = authors.split('&')[0].strip()
            if ',' in first_author:
                author_last = first_author.split(',')[0].strip()
                query += f" author:{author_last}"
        
        url = f"https://api.crossref.org/works?query.bibliographic={requests.utils.quote(query)}&rows=5"
        
        if year:
            url += f"&filter=from-pub-date:{year}"
        
        response = requests.get(url, timeout=CROSSREF_TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            items = data.get("message", {}).get("items", [])
            
            for item in items:
                # Check title similarity
                item_title = item.get("title", [""])[0] if item.get("title") else ""
                if item_title and similar_text(title, item_title) > 0.6:
                    doi = item.get("DOI")
                    if doi:
                        return doi
            
            # Return first result if any
            if items:
                doi = items[0].get("DOI")
                if doi:
                    return doi
        
        return None
        
    except Exception as e:
        print(f"Crossref search error: {e}")
        return None

def search_openalex_by_metadata(title: str, authors: str = "", year: str = "") -> Optional[str]:
    """Search OpenAlex API for DOI by metadata"""
    try:
        # Build search query
        query = title
        
        url = f"https://api.openalex.org/works?search={requests.utils.quote(query)}&per-page=5"
        
        if year:
            url += f"&filter=publication_year:{year}"
        
        response = requests.get(url, timeout=OPENALEX_TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            results = data.get("results", [])
            
            for result in results:
                result_title = result.get("title", "")
                if result_title and similar_text(title, result_title) > 0.5:
                    doi = result.get("doi", "")
                    if doi:
                        # Extract DOI from URL if needed
                        doi_match = re.search(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', doi, re.I)
                        if doi_match:
                            return doi_match.group(0)
        
        return None
        
    except Exception as e:
        print(f"OpenAlex search error: {e}")
        return None

def fetch_full_metadata_by_doi(doi: str, prefer_openalex: bool = True) -> dict:
    """Fetch complete metadata for a DOI from Crossref or OpenAlex"""
    if doi in CACHE_DOI_LOOKUPS:
        return CACHE_DOI_LOOKUPS[doi]
    
    result = {}
    
    # Try OpenAlex first if preferred
    if prefer_openalex:
        result = fetch_from_openalex_full(doi)
        if result and result.get("title"):
            CACHE_DOI_LOOKUPS[doi] = result
            return result
    
    # Try Crossref
    result = fetch_from_crossref_full(doi)
    if result and result.get("title"):
        CACHE_DOI_LOOKUPS[doi] = result
        return result
    
    # Try OpenAlex as fallback
    if not prefer_openalex:
        result = fetch_from_openalex_full(doi)
        if result and result.get("title"):
            CACHE_DOI_LOOKUPS[doi] = result
            return result
    
    CACHE_DOI_LOOKUPS[doi] = {}
    return {}

def fetch_from_crossref_full(doi: str) -> dict:
    """Fetch full metadata from Crossref"""
    try:
        url = f"https://api.crossref.org/works/{doi}"
        response = requests.get(url, timeout=CROSSREF_TIMEOUT)
        
        if response.status_code != 200:
            return {}
        
        data = response.json().get("message", {})
        
        authors = []
        for a in data.get("author", [])[:10]:
            family = a.get("family", "")
            given = a.get("given", "")
            if family:
                authors.append(f"{family}, {given}".strip() if given else family)
        
        # Get pages properly
        page = data.get("page", "")
        if page and "-" not in page and page.isdigit():
            # Just a single page number
            pass
        
        return {
            "authors": ", ".join(authors) if authors else "",
            "year": str(data.get("issued", {}).get("date-parts", [[None]])[0][0]) if data.get("issued") else "",
            "title": data.get("title", [""])[0] if data.get("title") else "",
            "source": data.get("container-title", [""])[0] if data.get("container-title") else "",
            "volume": data.get("volume", ""),
            "issue": data.get("issue", ""),
            "pages": page,
            "publisher": data.get("publisher", ""),
            "doi": doi,
            "url": data.get("URL", ""),
            "source_api": "Crossref"
        }
        
    except Exception as e:
        print(f"Crossref fetch error: {e}")
        return {}

def fetch_from_openalex_full(doi: str) -> dict:
    """Fetch full metadata from OpenAlex"""
    try:
        url = f"https://api.openalex.org/works/https://doi.org/{doi}"
        response = requests.get(url, timeout=OPENALEX_TIMEOUT)
        
        if response.status_code != 200:
            return {}
        
        data = response.json()
        
        authors = []
        for authorship in data.get("authorships", [])[:10]:
            author = authorship.get("author", {})
            if author.get("display_name"):
                # Format as "Last, First" for consistency
                name = author["display_name"]
                if ' ' in name:
                    parts = name.rsplit(' ', 1)
                    authors.append(f"{parts[1]}, {parts[0]}")
                else:
                    authors.append(name)
        
        source_name = ""
        if data.get("host_venue"):
            source_name = data.get("host_venue", {}).get("display_name", "")
        elif data.get("primary_location", {}).get("source"):
            source_name = data.get("primary_location", {}).get("source", {}).get("display_name", "")
        
        biblio = data.get("biblio", {})
        pages = ""
        if biblio.get("first_page") or biblio.get("last_page"):
            first = biblio.get("first_page", "")
            last = biblio.get("last_page", "")
            pages = f"{first}-{last}" if first and last else first or last
        
        return {
            "authors": " & ".join(authors) if authors else "",
            "year": str(data.get("publication_year", "")),
            "title": data.get("title", ""),
            "source": source_name,
            "volume": biblio.get("volume", ""),
            "issue": biblio.get("issue", ""),
            "pages": pages,
            "publisher": data.get("host_venue", {}).get("publisher", ""),
            "doi": doi,
            "url": data.get("doi", ""),
            "source_api": "OpenAlex"
        }
        
    except Exception as e:
        print(f"OpenAlex fetch error: {e}")
        return {}

def similar_text(text1: str, text2: str) -> float:
    """Calculate similarity between two strings"""
    if not text1 or not text2:
        return 0.0
    return SequenceMatcher(None, text1.lower(), text2.lower()).ratio()

# =========================================
# 4. AUTHOR CLEANING
# =========================================

def clean_authors(authors: str) -> str:
    if not authors:
        return ""
    
    # Remove excessive spaces
    authors = re.sub(r'\s+', ' ', authors).strip()
    
    # Fix "et. al" -> "et al"
    authors = re.sub(r'et\.\s+al\.?', 'et al.', authors, re.I)
    
    # Fix multiple commas
    authors = re.sub(r',,+', ',', authors)
    
    # Fix "&" formatting
    authors = re.sub(r'\s+&\s+', ' & ', authors)
    
    # Handle "and" -> "&"
    authors = re.sub(r'\s+and\s+', ' & ', authors, re.I)
    
    # Remove trailing punctuation
    authors = re.sub(r'[,;:]$', '', authors)
    
    # Format authors properly for APA
    parts = re.split(r'\s*&\s*', authors)
    formatted_parts = []
    
    for part in parts:
        part = part.strip()
        if ',' in part:
            # Already in "Last, First" format
            formatted_parts.append(part)
        else:
            # Try to parse "First Last" format
            names = part.split()
            if len(names) >= 2:
                last = names[-1]
                initials = ' '.join([f"{n[0]}." for n in names[:-1] if n and n[0].isalpha()])
                formatted_parts.append(f"{last}, {initials}".strip())
            else:
                formatted_parts.append(part)
    
    result = ' & '.join(formatted_parts)
    
    return result

# =========================================
# 5. FORMATTERS (FIXED - APA7, APA6, Harvard, Vancouver)
# =========================================

def format_apa7(p):
    """Format reference in APA 7th edition"""
    # Authors
    authors = p.get("authors", "")
    if not authors or authors == "Author Unknown":
        authors = "Author Unknown"
    
    # Year
    year = p.get("year", "")
    year_part = f"({year})." if year else "(n.d.)."
    
    # Title (sentence case for APA)
    title = p.get("title", "")
    if title and title != "No title":
        # Convert to sentence case (first letter capital, rest lower except proper nouns)
        title = title[0].upper() + title[1:].lower() if len(title) > 1 else title.upper()
    else:
        title = "No title"
    
    # Source (journal/book title in italics - title case)
    source = p.get("source", "")
    if source:
        # Title case for source
        source = ' '.join(word.capitalize() if word not in ['and', 'of', 'the', 'in', 'for'] else word 
                         for word in source.split())
    
    # Build the reference
    ref_parts = [authors, year_part, title]
    
    if source:
        ref_parts.append(source)
    
    # Volume, issue, pages
    volume = p.get("volume", "")
    issue = p.get("issue", "")
    pages = p.get("pages", "")
    
    if volume:
        vol_issue = volume
        if issue:
            vol_issue += f"({issue})"
        ref_parts.append(vol_issue)
    
    if pages:
        ref_parts.append(pages)
    
    # DOI or URL
    doi = p.get("doi", "")
    url = p.get("url", "")
    
    if doi:
        ref_parts.append(f"https://doi.org/{doi}")
    elif url:
        ref_parts.append(url)
    
    # Join with spaces and ensure proper punctuation
    ref = " ".join(ref_parts)
    
    # Ensure periods after author and year
    ref = re.sub(r'(Author Unknown|&|\w+,\s\w+\.)\s+\(', r'\1. (', ref)
    ref = re.sub(r'(\([^)]+\))\s+([A-Z])', r'\1. \2', ref)
    
    # Add period at the end if missing
    if ref and not ref.endswith('.'):
        ref += '.'
    
    return ref

def format_apa6(p):
    """Format reference in APA 6th edition"""
    authors = p.get("authors", "")
    if not authors:
        authors = "Author Unknown"
    
    year = p.get("year", "")
    year_part = f"({year})." if year else "(n.d.)."
    
    title = p.get("title", "No title")
    source = p.get("source", "")
    
    ref = f"{authors} {year_part} {title}"
    
    if source:
        ref += f". {source}"
    
    volume = p.get("volume", "")
    issue = p.get("issue", "")
    pages = p.get("pages", "")
    
    if volume:
        ref += f", {volume}"
        if issue:
            ref += f"({issue})"
    
    if pages:
        ref += f", {pages}"
    
    ref += "."
    
    doi = p.get("doi", "")
    url = p.get("url", "")
    
    if doi:
        ref += f" doi:{doi}"
    elif url:
        ref += f" Retrieved from {url}"
    
    return ref

def format_harvard(p):
    """Format reference in Harvard style"""
    year = p.get("year", "n.d.")
    
    authors = p.get("authors", "")
    if authors and "&" in authors:
        # For Harvard, use first author et al if multiple
        first_author = authors.split('&')[0].strip()
        if ',' in first_author:
            first_author = first_author.split(',')[0]
        authors = first_author
    
    if not authors:
        authors = "Unknown"
    
    title = p.get("title", "No title")
    if title and title != "No title":
        title = title[0].upper() + title[1:].lower()
    
    source = p.get("source", "")
    
    ref = f"{authors} ({year}) '{title}'"
    
    if source:
        ref += f", {source}"
    
    volume = p.get("volume", "")
    issue = p.get("issue", "")
    pages = p.get("pages", "")
    
    if volume:
        ref += f", Vol. {volume}"
        if issue:
            ref += f"({issue})"
    
    if pages:
        ref += f", pp. {pages}"
    
    doi = p.get("doi", "")
    url = p.get("url", "")
    
    if doi:
        ref += f", doi: {doi}"
    elif url:
        ref += f". Available at: {url}"
    
    return ref + "."

def format_vancouver(p):
    """Format reference in Vancouver style"""
    authors = p.get("authors", "")
    if authors:
        # Format authors for Vancouver (Last name + initials)
        author_parts = []
        for a in authors.split('&')[:6]:
            a = a.strip()
            if ',' in a:
                last = a.split(',')[0].strip()
                initials = a.split(',')[1].strip() if len(a.split(',')) > 1 else ""
                author_parts.append(f"{last} {initials}")
            else:
                names = a.split()
                if len(names) >= 2:
                    last = names[-1]
                    initials = ''.join([n[0] for n in names[:-1] if n and n[0].isalpha()])
                    author_parts.append(f"{last} {initials}")
                else:
                    author_parts.append(a)
        
        authors = ", ".join(author_parts)
        if len(author_parts) > 6:
            authors += ", et al"
    else:
        authors = "Anonymous"
    
    title = p.get("title", "No title")
    source = p.get("source", "")
    
    ref = f"{authors}. {title}"
    
    if source:
        ref += f". {source}"
    
    year = p.get("year", "")
    if year:
        ref += f". {year}"
    
    volume = p.get("volume", "")
    issue = p.get("issue", "")
    pages = p.get("pages", "")
    
    if volume:
        ref += f";{volume}"
        if issue:
            ref += f"({issue})"
    
    if pages:
        ref += f":{pages}"
    
    doi = p.get("doi", "")
    if doi:
        ref += f". doi: {doi}"
    
    return ref + "."

def format_reference(parsed, style):
    """Format reference according to specified style"""
    style = style.lower()
    
    formatters = {
        "apa7": format_apa7,
        "apa6": format_apa6,
        "harvard": format_harvard,
        "vancouver": format_vancouver
    }
    
    formatter = formatters.get(style, format_apa7)
    return formatter(parsed)

# =========================================
# 6. COMPLETE REPAIR ENGINE
# =========================================

def compute_repair_score(p):
    """Calculate confidence score for the repaired reference"""
    score = 100
    
    if not p.get("authors"):
        score -= 20
    if not p.get("year"):
        score -= 20
    if not p.get("title"):
        score -= 25
    if not p.get("source"):
        score -= 15
    if not p.get("doi") and not p.get("url"):
        score -= 10
    
    # Penalty for messy formatting
    if p.get("is_messy"):
        score -= min(20, p.get("messiness_score", 0) // 5)
    
    # Bonus for having DOI from reliable source
    if p.get("doi"):
        score += 5
    
    return max(0, min(100, score))

def get_repair_issues(p):
    """Get list of issues found in the reference"""
    issues = []
    
    if not p.get("authors"):
        issues.append("Missing author(s)")
    elif len(p.get("authors", "")) < 3:
        issues.append("Author name seems incomplete")
    
    if not p.get("year"):
        issues.append("Missing publication year")
    
    if not p.get("title"):
        issues.append("Missing title")
    
    if not p.get("source"):
        issues.append("Missing journal/book title")
    
    if not p.get("doi") and not p.get("url"):
        issues.append("No DOI or URL found")
    
    if p.get("is_messy"):
        issues.append(f"Original had messy formatting")
    
    # Check for retraction
    if p.get("is_retracted"):
        issues.append(f"⚠️ RETRACTED ARTICLE: {p.get('retraction_reason', 'This article has been retracted')}")
    
    return issues

def repair_reference(raw, style, source_type="auto", auto_enhance=True, auto_find_doi=True):
    """Complete reference repair with DOI discovery and retraction detection"""
    parsed = smart_parse_reference(raw, source_type)
    log = []
    
    # Step 1: Try to find DOI if missing
    doi_found = False
    if auto_find_doi and not parsed.get("doi"):
        # Search by title and authors
        found_doi = search_doi_by_metadata(
            parsed.get("title", ""),
            parsed.get("authors", ""),
            parsed.get("year", "")
        )
        
        if found_doi:
            parsed["doi"] = found_doi
            log.append(f"✓ Found DOI via search: {found_doi}")
            doi_found = True
    
    # Step 2: If we have a DOI, fetch full metadata and check retraction
    if parsed.get("doi"):
        # Check retraction status
        retraction = check_retraction_status(parsed["doi"])
        if retraction["is_retracted"]:
            parsed["is_retracted"] = True
            parsed["retraction_reason"] = retraction.get("reason", "Article has been retracted")
            log.append(f"⚠️ RETRACTION DETECTED: {parsed['retraction_reason']}")
        
        # Fetch full metadata
        if auto_enhance:
            metadata = fetch_full_metadata_by_doi(parsed["doi"], prefer_openalex=True)
            
            if metadata and metadata.get("title"):
                # Update parsed with fetched metadata (prioritize fetched data)
                for key in ["authors", "year", "title", "source", "volume", "issue", "pages", "publisher"]:
                    if metadata.get(key):
                        # Only update if fetched data is better quality
                        if not parsed.get(key) or len(metadata[key]) > len(parsed.get(key, "")):
                            parsed[key] = metadata[key]
                            if key not in [k for k in log if k in str(log)]:
                                log.append(f"✓ Enhanced {key} from {metadata.get('source_api', 'DOI')}")
    
    # Step 3: Format the reference using the complete metadata
    formatted = format_reference(parsed, style)
    
    # Step 4: Calculate metrics
    score = compute_repair_score(parsed)
    issues = get_repair_issues(parsed)
    
    return {
        "original": raw,
        "formatted": formatted,
        "parsed": parsed,
        "repair_log": log,
        "confidence": score,
        "issues": issues,
        "has_doi": bool(parsed.get("doi")),
        "needs_review": score < 70 or len(issues) > 2 or parsed.get("is_retracted", False),
        "doi_source": "found" if doi_found else ("original" if parsed.get("doi") else "missing"),
        "is_retracted": parsed.get("is_retracted", False),
        "retraction_reason": parsed.get("retraction_reason")
    }

def process_references(raw_text, style, source_type="auto", auto_enhance=True, auto_find_doi=True):
    """Process multiple references"""
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    
    formatted = []
    warnings = []
    repair_results = []
    
    for i, line in enumerate(lines, 1):
        result = repair_reference(line, style, source_type, auto_enhance, auto_find_doi)
        formatted.append(result["formatted"])
        repair_results.append(result)
        
        if result["issues"]:
            for issue in result["issues"]:
                warnings.append(f"Ref {i}: {issue}")
        
        # Log DOI discovery
        if result.get("doi_source") == "found":
            warnings.append(f"Ref {i}: DOI auto-discovered via Crossref/OpenAlex")
        
        # Log retraction
        if result.get("is_retracted"):
            warnings.append(f"Ref {i}: ⚠️ RETRACTED ARTICLE - {result.get('retraction_reason', 'Please verify')}")
    
    return {
        "formatted": "\n\n".join(formatted),
        "warnings": warnings,
        "repair_results": repair_results,
        "total_references": len(lines),
        "average_confidence": sum(r["confidence"] for r in repair_results) / len(repair_results) if repair_results else 0,
        "references_with_doi": sum(1 for r in repair_results if r["has_doi"]),
        "dois_found": sum(1 for r in repair_results if r.get("doi_source") == "found"),
        "retracted_count": sum(1 for r in repair_results if r.get("is_retracted"))
    }

def export_to_dict(repair_results):
    return [
        {
            "original": r["original"],
            "formatted": r["formatted"],
            "confidence": r["confidence"],
            "issues": r["issues"],
            "has_doi": r["has_doi"],
            "needs_review": r.get("needs_review", False),
            "repair_log": r["repair_log"],
            "parsed": r["parsed"],
            "doi_source": r.get("doi_source", "missing"),
            "is_retracted": r.get("is_retracted", False),
            "retraction_reason": r.get("retraction_reason")
        }
        for r in repair_results
    ]

# =========================================
# 7. FLASK WEB SERVER
# =========================================

HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CiteIntegrity Pro | Smart Reference Formatter</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #f5f7fb;
            margin: 0;
            padding: 20px;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 12px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
        }
        h1 {
            margin-top: 0;
            color: #1f2937;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .badge {
            background: #19b36b;
            color: white;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: normal;
        }
        .badge-retracted {
            background: #dc2626;
        }
        .subtitle {
            color: #6b7280;
            margin-bottom: 25px;
            padding-bottom: 15px;
            border-bottom: 2px solid #e5e7eb;
        }
        .grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 25px;
        }
        label {
            font-weight: 600;
            display: block;
            margin-bottom: 6px;
            color: #374151;
        }
        textarea {
            width: 100%;
            height: 300px;
            padding: 12px;
            border-radius: 8px;
            border: 1px solid #d1d5db;
            resize: vertical;
            font-size: 13px;
            font-family: 'Courier New', monospace;
            box-sizing: border-box;
            background: #fafafa;
        }
        textarea:focus {
            outline: none;
            border-color: #19b36b;
            box-shadow: 0 0 0 3px rgba(25, 179, 107, 0.1);
        }
        select, button {
            padding: 10px 12px;
            border-radius: 8px;
            border: 1px solid #d1d5db;
            font-size: 14px;
            background: white;
            cursor: pointer;
            transition: all 0.2s;
        }
        .controls {
            display: flex;
            gap: 10px;
            margin-top: 15px;
            margin-bottom: 10px;
        }
        button {
            background: #19b36b;
            color: white;
            border: none;
            font-weight: 600;
        }
        button:hover:not(:disabled) {
            background: #158f57;
        }
        button:disabled {
            background: #9ca3af;
            cursor: not-allowed;
        }
        .output {
            background: #f9fafb;
            border: 1px solid #e5e7eb;
            padding: 15px;
            border-radius: 8px;
            min-height: 300px;
            max-height: 400px;
            overflow-y: auto;
            white-space: pre-wrap;
            font-family: 'Segoe UI', Tahoma, sans-serif;
            font-size: 13px;
            line-height: 1.5;
        }
        .copy-btn {
            margin-top: 10px;
            background: #111827;
            width: 100%;
        }
        .warnings {
            margin-top: 15px;
            background: #fff7ed;
            border-left: 4px solid #f59e0b;
            padding: 12px;
            border-radius: 8px;
            font-size: 13px;
        }
        .warnings ul {
            margin: 0;
            padding-left: 20px;
        }
        .warnings li {
            margin: 5px 0;
        }
        .retraction-warning {
            background: #fee2e2;
            border-left-color: #dc2626;
            color: #991b1b;
        }
        .stats {
            margin-top: 15px;
            padding: 12px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 8px;
            color: white;
            display: flex;
            justify-content: space-around;
            text-align: center;
        }
        .stat-item {
            flex: 1;
        }
        .stat-value {
            font-size: 24px;
            font-weight: bold;
        }
        .stat-label {
            font-size: 12px;
            opacity: 0.9;
            margin-top: 5px;
        }
        .hidden {
            display: none;
        }
        .checkbox-group {
            margin: 10px 0;
            display: flex;
            gap: 20px;
        }
        .checkbox-group label {
            font-weight: normal;
            cursor: pointer;
        }
        @media (max-width: 900px) {
            .grid { grid-template-columns: 1fr; }
            .stats { flex-direction: column; gap: 10px; }
        }
    </style>
</head>
<body>
<div class="container">
    <h1>
        🔧 CiteIntegrity Pro
        <span class="badge">Smart DOI Discovery</span>
        <span class="badge badge-retracted">Retraction Detection</span>
    </h1>
    <div class="subtitle">
        Automatically detects messy references, formats them, finds missing DOIs, and flags retracted articles
    </div>

    <div class="grid">
        <div>
            <label for="raw_reference">📝 References to Format/Repair</label>
            <textarea id="raw_reference" placeholder="Paste messy or clean references (one per line)...&#10;&#10;Examples:&#10;Eklemet I MacCarthy J Gyamfaa E 2024 Moderating Role of Risk Management between Risk Exposure and Bank Performance Theoretical Economics Letters 14 2 363-389&#10;&#10;Smith J 2020 Understanding AI Journal of Technology 15 2 45-67"></textarea>

            <div class="controls">
                <select id="style">
                    <option value="apa7">APA 7th Edition</option>
                    <option value="apa6">APA 6th Edition</option>
                    <option value="harvard">Harvard (Author-Date)</option>
                    <option value="vancouver">Vancouver (Medical)</option>
                </select>
                <select id="source_type">
                    <option value="auto">Auto-detect</option>
                    <option value="journal">Journal Article</option>
                    <option value="book">Book</option>
                    <option value="webpage">Website</option>
                    <option value="report">Report</option>
                </select>
            </div>

            <div class="checkbox-group">
                <label><input type="checkbox" id="auto_enhance" checked> 🔍 Auto-enhance from DOI</label>
                <label><input type="checkbox" id="auto_find_doi" checked> 🌐 Auto-find missing DOIs</label>
            </div>

            <button id="formatBtn" style="width: 100%;">🚀 Format & Repair References</button>
        </div>

        <div>
            <label>✅ Formatted & Repaired Output</label>
            <div id="formattedOutput" class="output">Your formatted references will appear here...</div>
            <button id="copyBtn" class="copy-btn">📋 Copy to Clipboard</button>

            <div id="statsPanel" class="stats hidden">
                <div class="stat-item">
                    <div class="stat-value" id="totalRefs">0</div>
                    <div class="stat-label">Total References</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" id="doiCount">0</div>
                    <div class="stat-label">With DOI</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" id="doiFound">0</div>
                    <div class="stat-label">DOIs Found</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" id="retractedCount">0</div>
                    <div class="stat-label">Retracted</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" id="avgConfidence">0%</div>
                    <div class="stat-label">Avg. Confidence</div>
                </div>
            </div>

            <div id="warningsBox" class="warnings hidden">
                <strong>⚠️ Warnings & Issues</strong>
                <ul id="warningsList"></ul>
            </div>
        </div>
    </div>
</div>

<script>
    const formatBtn = document.getElementById('formatBtn');
    const rawReference = document.getElementById('raw_reference');
    const formattedOutput = document.getElementById('formattedOutput');
    const copyBtn = document.getElementById('copyBtn');
    const warningsBox = document.getElementById('warningsBox');
    const warningsList = document.getElementById('warningsList');
    const style = document.getElementById('style');
    const sourceType = document.getElementById('source_type');
    const autoEnhance = document.getElementById('auto_enhance');
    const autoFindDoi = document.getElementById('auto_find_doi');
    const statsPanel = document.getElementById('statsPanel');
    const totalRefs = document.getElementById('totalRefs');
    const doiCount = document.getElementById('doiCount');
    const doiFound = document.getElementById('doiFound');
    const retractedCount = document.getElementById('retractedCount');
    const avgConfidence = document.getElementById('avgConfidence');

    formatBtn.addEventListener('click', async function() {
        const rawText = rawReference.value.trim();
        
        if (!rawText) {
            formattedOutput.textContent = '⚠️ Please paste at least one reference.';
            return;
        }
        
        formattedOutput.textContent = '🔄 Processing references with smart DOI discovery and retraction check...';
        formatBtn.disabled = true;
        
        const formData = new URLSearchParams();
        formData.append('raw_reference', rawText);
        formData.append('style', style.value);
        formData.append('source_type', sourceType.value);
        formData.append('auto_enhance', autoEnhance.checked);
        formData.append('auto_find_doi', autoFindDoi.checked);
        
        try {
            const response = await fetch('/api/format-reference', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                },
                body: formData
            });
            
            const data = await response.json();
            
            if (!response.ok || !data.success) {
                formattedOutput.textContent = data.message || '❌ Formatting failed.';
                return;
            }
            
            // Display formatted output with line breaks
            formattedOutput.innerHTML = (data.formatted || 'No output returned.').replace(/\\n/g, '<br>');
            
            // Update stats
            if (data.total_references) {
                statsPanel.classList.remove('hidden');
                totalRefs.textContent = data.total_references;
                doiCount.textContent = data.references_with_doi || 0;
                doiFound.textContent = data.dois_found || 0;
                retractedCount.textContent = data.retracted_count || 0;
                avgConfidence.textContent = Math.round(data.average_confidence || 0) + '%';
            }
            
            // Update warnings
            if (data.warnings && data.warnings.length > 0) {
                const hasRetraction = data.warnings.some(w => w.includes('RETRACTED'));
                if (hasRetraction) {
                    warningsBox.classList.add('retraction-warning');
                } else {
                    warningsBox.classList.remove('retraction-warning');
                }
                warningsList.innerHTML = data.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('');
                warningsBox.classList.remove('hidden');
            } else {
                warningsBox.classList.add('hidden');
            }
            
        } catch (error) {
            console.error('Error:', error);
            formattedOutput.textContent = '❌ Error: ' + error.message;
        } finally {
            formatBtn.disabled = false;
        }
    });
    
    copyBtn.addEventListener('click', async function() {
        const text = formattedOutput.textContent;
        if (text && !text.includes('Processing') && !text.includes('Please paste')) {
            await navigator.clipboard.writeText(text);
            copyBtn.textContent = '✓ Copied!';
            setTimeout(() => copyBtn.textContent = '📋 Copy to Clipboard', 2000);
        }
    });
    
    function escapeHtml(str) {
        if (!str) return '';
        return str.replace(/[&<>]/g, function(m) {
            if (m === '&') return '&amp;';
            if (m === '<') return '&lt;';
            if (m === '>') return '&gt;';
            return m;
        });
    }
    
    // Ctrl+Enter shortcut
    rawReference.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault();
            formatBtn.click();
        }
    });
</script>
</body>
</html>
'''

if FLASK_AVAILABLE:
    app = Flask(__name__)
    CORS(app, origins=['*'], allow_headers=['Content-Type'], methods=['GET', 'POST', 'OPTIONS'])
    
    @app.route('/')
    def index():
        return render_template_string(HTML_TEMPLATE)
    
    @app.route('/api/format-reference', methods=['POST', 'OPTIONS'])
    def format_reference_api():
        if request.method == 'OPTIONS':
            return jsonify({}), 200
            
        try:
            # Get form data
            raw_text = request.form.get('raw_reference', '')
            style = request.form.get('style', 'apa7')
            source_type = request.form.get('source_type', 'auto')
            auto_enhance = request.form.get('auto_enhance', 'true').lower() == 'true'
            auto_find_doi = request.form.get('auto_find_doi', 'true').lower() == 'true'
            
            print(f"Processing: style={style}, source_type={source_type}, auto_enhance={auto_enhance}, auto_find_doi={auto_find_doi}")
            
            if not raw_text or not raw_text.strip():
                return jsonify({'success': False, 'message': 'Please provide at least one reference.'}), 400
            
            # Process references
            result = process_references(raw_text, style, source_type, auto_enhance, auto_find_doi)
            
            response_data = {
                'success': True,
                'formatted': result['formatted'],
                'warnings': result['warnings'],
                'repair_results': export_to_dict(result['repair_results']),
                'total_references': result['total_references'],
                'average_confidence': round(result['average_confidence'], 1),
                'references_with_doi': result['references_with_doi'],
                'dois_found': result.get('dois_found', 0),
                'retracted_count': result.get('retracted_count', 0),
                'needs_review': sum(1 for r in result['repair_results'] if r.get('needs_review'))
            }
            
            print(f"Success: {result['total_references']} references, {result.get('dois_found', 0)} DOIs found, {result.get('retracted_count', 0)} retracted")
            return jsonify(response_data), 200
            
        except Exception as e:
            print(f"Error: {traceback.format_exc()}")
            return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500
    
    @app.route('/api/health', methods=['GET'])
    def health_check():
        return jsonify({
            'status': 'healthy',
            'version': '3.1.0',
            'features': [
                'Smart messy reference detection',
                'DOI auto-discovery (Crossref/OpenAlex)',
                'APA7, APA6, Harvard, Vancouver',
                'Retraction detection',
                'Auto-source type detection'
            ]
        }), 200
    
    if __name__ == '__main__':
        print("=" * 60)
        print("🔧 CiteIntegrity Pro - Smart Reference Formatter")
        print("=" * 60)
        print(f"✅ Messy Reference Detection: ENABLED")
        print(f"✅ DOI Auto-discovery (Crossref/OpenAlex): ENABLED")
        print(f"✅ Retraction Detection: ENABLED")
        print(f"✅ Auto-source type detection: ENABLED")
        print(f"✅ All Citation Styles: ENABLED")
        print("=" * 60)
        print("\n📱 Access the application at: http://localhost:5000")
        print("📋 API endpoint: POST /api/format-reference")
        print("\n💡 Features:")
        print("   - Detects messy/unformatted references")
        print("   - Finds missing DOIs from Crossref/OpenAlex")
        print("   - Flags retracted articles")
        print("   - Auto-detects source type")
        print("   - Formats in APA7, APA6, Harvard, Vancouver")
        print("\n⚠️  Press Ctrl+C to stop the server\n")
        app.run(debug=True, host='0.0.0.0', port=5000)

else:
    print("=" * 60)
    print("CiteIntegrity Pro - Smart Reference Formatter")
    print("=" * 60)
    print("Flask not installed. To run the web server:")
    print("  pip install Flask flask-cors requests")
    print("  python formatter.py")
    print("=" * 60)
    
    # Test functionality
    if __name__ == '__main__':
        test_refs = [
            "Eklemet I MacCarthy J Gyamfaa E 2024 Moderating Role of Risk Management between Risk Exposure and Bank Performance Theoretical Economics Letters 14 2 363-389",
            "Smith J 2020 Understanding AI Journal of Technology 15 2 45-67"
        ]
        
        print("\n📝 Testing smart reference repair:")
        print("-" * 50)
        
        for ref in test_refs:
            detection = detect_messy_reference(ref)
            print(f"\nOriginal: {ref}")
            print(f"Messy detection: {detection['is_messy']} (score: {detection['messiness_score']})")
            
            result = repair_reference(ref, "apa7", "auto", True, True)
            print(f"Formatted: {result['formatted']}")
            print(f"Confidence: {result['confidence']}%")
            print(f"Has DOI: {result['has_doi']}")
            if result.get('doi_source') == 'found':
                print(f"DOI Source: AUTO-DISCOVERED")
            if result.get('is_retracted'):
                print(f"⚠️ RETRACTED: {result.get('retraction_reason')}")
