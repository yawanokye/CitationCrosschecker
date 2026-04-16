# =========================================
# formatter.py — CiteIntegrity Pro Complete
# Smart Reference Detection + DOI Search by Title/Author/Year
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
CROSSREF_TIMEOUT = 15
OPENALEX_TIMEOUT = 15
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
# 2. ENHANCED REFERENCE PARSING
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
    ref_type = "journal"
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

def extract_authors_from_text(text: str) -> str:
    """Extract authors from various formats"""
    # Try to find author patterns
    
    # Pattern 1: Last, F. & Last, F.
    pattern1 = r'([A-Z][a-z]+,\s+[A-Z]\.(?:\s+[A-Z]\.)?(?:\s+&\s+[A-Z][a-z]+,\s+[A-Z]\.)*)'
    match = re.search(pattern1, text)
    if match:
        return match.group(1)
    
    # Pattern 2: F. Last & F. Last
    pattern2 = r'([A-Z]\.\s+[A-Z][a-z]+(?:\s+&\s+[A-Z]\.\s+[A-Z][a-z]+)*)'
    match = re.search(pattern2, text)
    if match:
        # Convert to Last, F. format
        authors = match.group(1)
        parts = re.split(r'\s+&\s+', authors)
        formatted = []
        for part in parts:
            if '. ' in part:
                initial, last = part.split('. ', 1)
                formatted.append(f"{last}, {initial}.")
            else:
                formatted.append(part)
        return ' & '.join(formatted)
    
    # Pattern 3: Just names without initials
    pattern3 = r'^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)(?=\s+\(?\d{4})'
    match = re.search(pattern3, text)
    if match:
        return match.group(1)
    
    return ""

def extract_year_from_text(text: str) -> str:
    """Extract year from text"""
    year_match = re.search(r'\b(19|20)\d{2}\b', text)
    return year_match.group(0) if year_match else ""

def extract_title_from_text(text: str, year: str, authors: str) -> str:
    """Extract title from text"""
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
        
        return title_part
    
    return ""

def extract_source_from_text(text: str, title: str, year: str) -> str:
    """Extract source (journal/book title) from text"""
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
        source_part = re.sub(r'\d+\s*\(?\d*\)?\s*[,:]\s*[\d\-–]+.*$', '', source_part)
        source_part = re.sub(r'vol\.?\s*\d+.*$', '', source_part, re.I)
        source_part = re.sub(r'^\.\s+', '', source_part)
        source_part = source_part.strip(" .,;:")
        
        return source_part
    
    return ""

def extract_volume_issue_pages(text: str) -> Tuple[str, str, str]:
    """Extract volume, issue, and pages from text"""
    volume = ""
    issue = ""
    pages = ""
    
    # Pattern 1: volume(issue), pages
    pattern1 = r'(\d+)\s*\((\d+)\)\s*[,:]\s*([\d\-–]+)'
    match = re.search(pattern1, text)
    if match:
        volume = match.group(1)
        issue = match.group(2)
        pages = match.group(3)
        return volume, issue, pages
    
    # Pattern 2: volume, pages
    pattern2 = r'(\d+)\s*[,:]\s*([\d\-–]+)'
    match = re.search(pattern2, text)
    if match:
        volume = match.group(1)
        pages = match.group(2)
        return volume, issue, pages
    
    # Pattern 3: just pages
    pattern3 = r'pp?\.?\s*([\d\-–]+)'
    match = re.search(pattern3, text, re.I)
    if match:
        pages = match.group(1)
    
    return volume, issue, pages

def smart_parse_reference(raw_reference: str, source_type: str = "auto") -> dict:
    """Intelligently parse any reference format"""
    raw = " ".join(raw_reference.strip().split())
    
    # First, detect if it's messy
    detection = detect_messy_reference(raw)
    
    # Extract components
    authors = extract_authors_from_text(raw)
    year = extract_year_from_text(raw)
    title = extract_title_from_text(raw, year, authors)
    source = extract_source_from_text(raw, title, year)
    volume, issue, pages = extract_volume_issue_pages(raw)
    
    # Extract DOI
    doi_match = re.search(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", raw, re.I)
    doi = doi_match.group(1).rstrip(".,;") if doi_match else ""
    
    # Extract URL
    url_match = re.search(r"(https?://\S+)", raw, re.I)
    url = url_match.group(1).rstrip(".,;") if url_match else ""
    
    parsed = {
        "authors": authors,
        "year": year,
        "title": title,
        "source": source,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "publisher": "",
        "doi": doi,
        "url": url,
        "original_text": raw_reference,
        "is_messy": detection["is_messy"],
        "messiness_score": detection["messiness_score"],
        "ref_type": detection["ref_type"] if source_type == "auto" else source_type,
        "is_retracted": False,
        "retraction_reason": None
    }
    
    return parsed

# =========================================
# 3. DOI SEARCH USING TITLE + AUTHOR + YEAR
# =========================================

def search_doi_by_title_author_year(title: str, authors: str = "", year: str = "") -> Optional[str]:
    """Search for DOI using title, author, and year - PRIMARY METHOD"""
    if not title:
        return None
    
    # Clean up title for better search
    clean_title = re.sub(r'[^\w\s]', '', title).strip()
    
    # Create cache key
    cache_key = f"{clean_title}_{authors}_{year}"
    if cache_key in CACHE_SEARCH_RESULTS:
        return CACHE_SEARCH_RESULTS[cache_key]
    
    print(f"Searching DOI for: Title='{clean_title[:50]}...', Author='{authors[:30]}', Year='{year}'")
    
    # Try Crossref first (more reliable for DOIs)
    doi = search_crossref_by_title_author(clean_title, authors, year)
    if doi:
        print(f"✓ DOI found via Crossref: {doi}")
        CACHE_SEARCH_RESULTS[cache_key] = doi
        return doi
    
    # Try OpenAlex as fallback
    doi = search_openalex_by_title_author(clean_title, authors, year)
    if doi:
        print(f"✓ DOI found via OpenAlex: {doi}")
        CACHE_SEARCH_RESULTS[cache_key] = doi
        return doi
    
    print("✗ No DOI found")
    CACHE_SEARCH_RESULTS[cache_key] = None
    return None

def search_crossref_by_title_author(title: str, authors: str = "", year: str = "") -> Optional[str]:
    """Search Crossref API using title and author"""
    try:
        # Build query
        query = title
        
        # Add author last name if available
        if authors:
            # Extract first author's last name
            first_author = authors.split('&')[0].strip()
            if ',' in first_author:
                author_last = first_author.split(',')[0].strip()
            else:
                # Try to get last word as last name
                author_words = first_author.split()
                author_last = author_words[-1] if author_words else ""
            
            if author_last:
                query += f" author:{author_last}"
        
        url = f"https://api.crossref.org/works?query.bibliographic={requests.utils.quote(query)}&rows=5"
        
        if year:
            url += f"&filter=from-pub-date:{year}"
        
        print(f"Crossref query URL: {url}")
        
        response = requests.get(url, timeout=CROSSREF_TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            items = data.get("message", {}).get("items", [])
            
            for item in items:
                item_title = item.get("title", [""])[0] if item.get("title") else ""
                item_year = str(item.get("issued", {}).get("date-parts", [[None]])[0][0]) if item.get("issued") else ""
                
                # Calculate similarity scores
                title_similarity = similar_text(title, item_title)
                
                print(f"  Comparing: '{item_title[:50]}...' (sim: {title_similarity:.2f})")
                
                # If title similarity is high enough, return DOI
                if title_similarity > 0.6:
                    doi = item.get("DOI")
                    if doi:
                        return doi
                
                # Also check if year matches
                if year and item_year == year and title_similarity > 0.4:
                    doi = item.get("DOI")
                    if doi:
                        return doi
            
            # Return first result if nothing matches well
            if items:
                doi = items[0].get("DOI")
                if doi:
                    return doi
        
        return None
        
    except Exception as e:
        print(f"Crossref search error: {e}")
        return None

def search_openalex_by_title_author(title: str, authors: str = "", year: str = "") -> Optional[str]:
    """Search OpenAlex API using title and author"""
    try:
        # Build search query
        query = title
        
        # Add author if available
        if authors:
            first_author = authors.split('&')[0].strip()
            if ',' in first_author:
                author_name = first_author.split(',')[0].strip()
            else:
                author_name = first_author.split()[-1] if first_author.split() else ""
            
            if author_name:
                query += f" author:{author_name}"
        
        url = f"https://api.openalex.org/works?search={requests.utils.quote(query)}&per-page=5"
        
        if year:
            url += f"&filter=publication_year:{year}"
        
        print(f"OpenAlex query URL: {url}")
        
        response = requests.get(url, timeout=OPENALEX_TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            results = data.get("results", [])
            
            for result in results:
                result_title = result.get("title", "")
                result_year = str(result.get("publication_year", ""))
                
                # Calculate similarity
                title_similarity = similar_text(title, result_title)
                
                print(f"  Comparing: '{result_title[:50]}...' (sim: {title_similarity:.2f})")
                
                if title_similarity > 0.5:
                    doi = result.get("doi", "")
                    if doi:
                        doi_match = re.search(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', doi, re.I)
                        if doi_match:
                            return doi_match.group(0)
                
                if year and result_year == year and title_similarity > 0.3:
                    doi = result.get("doi", "")
                    if doi:
                        doi_match = re.search(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', doi, re.I)
                        if doi_match:
                            return doi_match.group(0)
        
        return None
        
    except Exception as e:
        print(f"OpenAlex search error: {e}")
        return None

def fetch_full_metadata_by_doi(doi: str) -> dict:
    """Fetch complete metadata for a DOI from Crossref or OpenAlex"""
    if doi in CACHE_DOI_LOOKUPS:
        return CACHE_DOI_LOOKUPS[doi]
    
    print(f"Fetching metadata for DOI: {doi}")
    
    # Try Crossref first (better metadata)
    result = fetch_from_crossref_full(doi)
    if result and result.get("title"):
        print(f"✓ Metadata from Crossref: {result.get('title', '')[:50]}...")
        CACHE_DOI_LOOKUPS[doi] = result
        return result
    
    # Try OpenAlex as fallback
    result = fetch_from_openalex_full(doi)
    if result and result.get("title"):
        print(f"✓ Metadata from OpenAlex: {result.get('title', '')[:50]}...")
        CACHE_DOI_LOOKUPS[doi] = result
        return result
    
    print(f"✗ No metadata found for DOI: {doi}")
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
                if given:
                    # Get initials
                    initials = '. '.join([f"{g[0]}" for g in given.split()]) + '.' if given else ''
                    authors.append(f"{family}, {initials}")
                else:
                    authors.append(family)
        
        # Get pages properly
        page = data.get("page", "")
        if page and "-" not in page and page.isdigit():
            pass
        
        return {
            "authors": " & ".join(authors) if authors else "",
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
                name = author["display_name"]
                if ' ' in name:
                    parts = name.rsplit(' ', 1)
                    # Get initials
                    initials = parts[0][0] + '.' if parts[0] else ''
                    authors.append(f"{parts[1]}, {initials}")
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
# 4. FORMATTERS (APA7, APA6, Harvard, Vancouver)
# =========================================

def format_apa7(parsed: dict) -> str:
    """Format reference in APA 7th edition using stored data"""
    # Authors
    authors = parsed.get("authors", "")
    if not authors:
        authors = "Author Unknown"
    
    # Year
    year = parsed.get("year", "")
    year_part = f"({year})." if year else "(n.d.)."
    
    # Title (sentence case for APA)
    title = parsed.get("title", "")
    if title and title != "No title":
        # Convert to sentence case
        title = title[0].upper() + title[1:].lower() if len(title) > 1 else title.upper()
    else:
        title = "No title"
    
    # Source (journal/book title in italics - title case)
    source = parsed.get("source", "")
    
    # Build the reference
    ref_parts = [authors, year_part, title]
    
    if source:
        # Title case for source
        source_words = source.split()
        source = ' '.join(word.capitalize() if word.lower() not in ['and', 'of', 'the', 'in', 'for', 'a', 'an'] 
                         else word.lower() for word in source_words)
        ref_parts.append(source)
    
    # Volume, issue, pages
    volume = parsed.get("volume", "")
    issue = parsed.get("issue", "")
    pages = parsed.get("pages", "")
    
    if volume:
        vol_issue = volume
        if issue:
            vol_issue += f"({issue})"
        ref_parts.append(vol_issue)
    
    if pages:
        ref_parts.append(pages)
    
    # DOI or URL
    doi = parsed.get("doi", "")
    url = parsed.get("url", "")
    
    if doi:
        ref_parts.append(f"https://doi.org/{doi}")
    elif url:
        ref_parts.append(url)
    
    # Join with spaces
    ref = " ".join(ref_parts)
    
    # Add period at the end if missing
    if ref and not ref.endswith('.'):
        ref += '.'
    
    return ref

def format_apa6(parsed: dict) -> str:
    """Format reference in APA 6th edition"""
    authors = parsed.get("authors", "")
    if not authors:
        authors = "Author Unknown"
    
    year = parsed.get("year", "")
    year_part = f"({year})." if year else "(n.d.)."
    
    title = parsed.get("title", "No title")
    source = parsed.get("source", "")
    
    ref = f"{authors} {year_part} {title}"
    
    if source:
        ref += f". {source}"
    
    volume = parsed.get("volume", "")
    issue = parsed.get("issue", "")
    pages = parsed.get("pages", "")
    
    if volume:
        ref += f", {volume}"
        if issue:
            ref += f"({issue})"
    
    if pages:
        ref += f", {pages}"
    
    ref += "."
    
    doi = parsed.get("doi", "")
    url = parsed.get("url", "")
    
    if doi:
        ref += f" doi:{doi}"
    elif url:
        ref += f" Retrieved from {url}"
    
    return ref

def format_harvard(parsed: dict) -> str:
    """Format reference in Harvard style"""
    year = parsed.get("year", "n.d.")
    
    authors = parsed.get("authors", "")
    if authors and "&" in authors:
        first_author = authors.split('&')[0].strip()
        if ',' in first_author:
            first_author = first_author.split(',')[0]
        authors = first_author
    
    if not authors:
        authors = "Unknown"
    
    title = parsed.get("title", "No title")
    if title and title != "No title":
        title = title[0].upper() + title[1:].lower()
    
    source = parsed.get("source", "")
    
    ref = f"{authors} ({year}) '{title}'"
    
    if source:
        ref += f", {source}"
    
    volume = parsed.get("volume", "")
    issue = parsed.get("issue", "")
    pages = parsed.get("pages", "")
    
    if volume:
        ref += f", Vol. {volume}"
        if issue:
            ref += f"({issue})"
    
    if pages:
        ref += f", pp. {pages}"
    
    doi = parsed.get("doi", "")
    url = parsed.get("url", "")
    
    if doi:
        ref += f", doi: {doi}"
    elif url:
        ref += f". Available at: {url}"
    
    return ref + "."

def format_vancouver(parsed: dict) -> str:
    """Format reference in Vancouver style"""
    authors = parsed.get("authors", "")
    if authors:
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
    
    title = parsed.get("title", "No title")
    source = parsed.get("source", "")
    
    ref = f"{authors}. {title}"
    
    if source:
        ref += f". {source}"
    
    year = parsed.get("year", "")
    if year:
        ref += f". {year}"
    
    volume = parsed.get("volume", "")
    issue = parsed.get("issue", "")
    pages = parsed.get("pages", "")
    
    if volume:
        ref += f";{volume}"
        if issue:
            ref += f"({issue})"
    
    if pages:
        ref += f":{pages}"
    
    doi = parsed.get("doi", "")
    if doi:
        ref += f". doi: {doi}"
    
    return ref + "."

def format_reference(parsed: dict, style: str) -> str:
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
# 5. COMPLETE REPAIR ENGINE
# =========================================

def compute_repair_score(parsed: dict) -> int:
    """Calculate confidence score for the repaired reference"""
    score = 100
    
    if not parsed.get("authors"):
        score -= 20
    if not parsed.get("year"):
        score -= 20
    if not parsed.get("title"):
        score -= 25
    if not parsed.get("source"):
        score -= 15
    if not parsed.get("doi") and not parsed.get("url"):
        score -= 10
    
    # Penalty for messy formatting
    if parsed.get("is_messy"):
        score -= min(20, parsed.get("messiness_score", 0) // 5)
    
    # Bonus for having DOI
    if parsed.get("doi"):
        score += 5
    
    return max(0, min(100, score))

def get_repair_issues(parsed: dict) -> List[str]:
    """Get list of issues found in the reference"""
    issues = []
    
    if not parsed.get("authors"):
        issues.append("Missing author(s)")
    
    if not parsed.get("year"):
        issues.append("Missing publication year")
    
    if not parsed.get("title"):
        issues.append("Missing title")
    
    if not parsed.get("source"):
        issues.append("Missing journal/book title")
    
    if not parsed.get("doi") and not parsed.get("url"):
        issues.append("No DOI or URL found")
    
    if parsed.get("is_messy"):
        issues.append(f"Original had messy formatting")
    
    if parsed.get("is_retracted"):
        issues.append(f"⚠️ RETRACTED: {parsed.get('retraction_reason', 'This article has been retracted')}")
    
    return issues

def repair_reference(raw: str, style: str, source_type: str = "auto", 
                     auto_enhance: bool = True, auto_find_doi: bool = True) -> Dict:
    """Complete reference repair with DOI search by title/author/year"""
    
    # Step 1: Parse the reference
    parsed = smart_parse_reference(raw, source_type)
    repair_log = []
    
    print(f"\n{'='*50}")
    print(f"Processing: {raw[:80]}...")
    print(f"Parsed - Title: {parsed.get('title', 'N/A')[:50]}...")
    print(f"Parsed - Authors: {parsed.get('authors', 'N/A')[:30]}")
    print(f"Parsed - Year: {parsed.get('year', 'N/A')}")
    
    # Step 2: Search for DOI using title + author + year
    doi_found = False
    if auto_find_doi and not parsed.get("doi"):
        if parsed.get("title"):
            found_doi = search_doi_by_title_author_year(
                parsed["title"],
                parsed.get("authors", ""),
                parsed.get("year", "")
            )
            
            if found_doi:
                parsed["doi"] = found_doi
                repair_log.append(f"✓ Found DOI via search: {found_doi}")
                doi_found = True
    
    # Step 3: If we have a DOI, fetch full metadata
    if parsed.get("doi"):
        # Check retraction status
        retraction = check_retraction_status(parsed["doi"])
        if retraction["is_retracted"]:
            parsed["is_retracted"] = True
            parsed["retraction_reason"] = retraction.get("reason", "Article has been retracted")
            repair_log.append(f"⚠️ RETRACTION DETECTED: {parsed['retraction_reason']}")
        
        # Fetch full metadata
        if auto_enhance:
            metadata = fetch_full_metadata_by_doi(parsed["doi"])
            
            if metadata and metadata.get("title"):
                # Store all fetched data
                for key in ["authors", "year", "title", "source", "volume", "issue", "pages", "publisher"]:
                    if metadata.get(key):
                        # Prefer fetched data over parsed data (it's more accurate)
                        if not parsed.get(key) or len(metadata[key]) > len(parsed.get(key, "")):
                            parsed[key] = metadata[key]
                            repair_log.append(f"✓ Enhanced {key} from {metadata.get('source_api', 'DOI')}")
    
    # Step 4: Format using the stored data
    formatted = format_reference(parsed, style)
    
    # Step 5: Calculate metrics
    score = compute_repair_score(parsed)
    issues = get_repair_issues(parsed)
    
    print(f"Result - Confidence: {score}%, Has DOI: {bool(parsed.get('doi'))}, Retracted: {parsed.get('is_retracted', False)}")
    print(f"Formatted: {formatted[:100]}...")
    
    return {
        "original": raw,
        "formatted": formatted,
        "parsed": parsed,
        "repair_log": repair_log,
        "confidence": score,
        "issues": issues,
        "has_doi": bool(parsed.get("doi")),
        "needs_review": score < 70 or len(issues) > 2 or parsed.get("is_retracted", False),
        "doi_source": "found" if doi_found else ("original" if parsed.get("doi") else "missing"),
        "is_retracted": parsed.get("is_retracted", False),
        "retraction_reason": parsed.get("retraction_reason")
    }

def process_references(raw_text: str, style: str, source_type: str = "auto", 
                       auto_enhance: bool = True, auto_find_doi: bool = True) -> Dict:
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
        
        if result.get("doi_source") == "found":
            warnings.append(f"Ref {i}: DOI auto-discovered via Crossref/OpenAlex")
        
        if result.get("is_retracted"):
            warnings.append(f"Ref {i}: ⚠️ RETRACTED - {result.get('retraction_reason', 'Please verify')}")
    
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

def export_to_dict(repair_results: List[Dict]) -> List[Dict]:
    """Export repair results to dictionary format"""
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
# 6. FLASK WEB SERVER
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
            flex-wrap: wrap;
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
        .badge-doi {
            background: #3b82f6;
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
            height: 350px;
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
            min-height: 350px;
            max-height: 450px;
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
            flex-wrap: wrap;
        }
        .stat-item {
            flex: 1;
            min-width: 80px;
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
        .loading {
            opacity: 0.6;
            pointer-events: none;
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
        <span class="badge">Smart DOI Search</span>
        <span class="badge badge-doi">Title+Author+Year</span>
        <span class="badge badge-retracted">Retraction Detection</span>
    </h1>
    <div class="subtitle">
        Automatically searches for DOIs using title, author, and year. Fetches complete metadata and formats in any style.
    </div>

    <div class="grid">
        <div>
            <label for="raw_reference">📝 References to Format/Repair</label>
            <textarea id="raw_reference" placeholder="Paste references (one per line)...&#10;&#10;Examples:&#10;Eklemet, I., MacCarthy, J., & Gyamfaa, E. (2024). Moderating Role of Risk Management between Risk Exposure and Bank Performance. Theoretical Economics Letters, 14(2), 363-389.&#10;&#10;Smith, J. (2020). Understanding AI. Journal of Technology, 15(2), 45-67."></textarea>

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
                <label><input type="checkbox" id="auto_find_doi" checked> 🌐 Auto-find missing DOIs (Title+Author+Year)</label>
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
                    <div class="stat-label">Total</div>
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
                    <div class="stat-label">Confidence</div>
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
        
        formattedOutput.innerHTML = '<span style="color:#19b36b;">🔍 Processing references - searching for DOIs by title, author, and year...</span>';
        formatBtn.disabled = true;
        formatBtn.classList.add('loading');
        
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
                formattedOutput.innerHTML = `<span style="color:#ef4444;">❌ ${data.message || 'Formatting failed.'}</span>`;
                return;
            }
            
            // Display formatted output
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
            
            // Show success message if DOIs were found
            if (data.dois_found > 0) {
                const toast = document.createElement('div');
                toast.textContent = `✓ Found ${data.dois_found} new DOI${data.dois_found > 1 ? 's' : ''} via Crossref/OpenAlex`;
                toast.style.cssText = 'position: fixed; bottom: 20px; right: 20px; background: #19b36b; color: white; padding: 10px 20px; border-radius: 8px; z-index: 10000;';
                document.body.appendChild(toast);
                setTimeout(() => toast.remove(), 3000);
            }
            
        } catch (error) {
            console.error('Error:', error);
            formattedOutput.innerHTML = `<span style="color:#ef4444;">❌ Error: ${escapeHtml(error.message)}</span>`;
        } finally {
            formatBtn.disabled = false;
            formatBtn.classList.remove('loading');
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
            
            print(f"\n{'='*60}")
            print(f"📥 Request received")
            print(f"   Style: {style}")
            print(f"   Source Type: {source_type}")
            print(f"   Auto-enhance: {auto_enhance}")
            print(f"   Auto-find DOI: {auto_find_doi}")
            print(f"   Text length: {len(raw_text)} chars")
            
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
            
            print(f"\n📤 Response summary:")
            print(f"   Total references: {result['total_references']}")
            print(f"   DOIs found: {result.get('dois_found', 0)}")
            print(f"   Retracted: {result.get('retracted_count', 0)}")
            print(f"   Avg confidence: {round(result['average_confidence'], 1)}%")
            print(f"{'='*60}\n")
            
            return jsonify(response_data), 200
            
        except Exception as e:
            print(f"❌ Error: {traceback.format_exc()}")
            return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500
    
    @app.route('/api/health', methods=['GET'])
    def health_check():
        return jsonify({
            'status': 'healthy',
            'version': '3.2.0',
            'features': [
                'DOI search by Title + Author + Year',
                'Smart reference parsing',
                'Crossref and OpenAlex integration',
                'Retraction detection',
                'APA7, APA6, Harvard, Vancouver'
            ]
        }), 200
    
    if __name__ == '__main__':
        print("=" * 70)
        print("🔧 CiteIntegrity Pro - Smart Reference Formatter")
        print("=" * 70)
        print(f"✅ DOI Search: Title + Author + Year (Crossref & OpenAlex)")
        print(f"✅ Metadata Storage: Complete reference data stored")
        print(f"✅ Retraction Detection: ENABLED")
        print(f"✅ Formatting: APA7, APA6, Harvard, Vancouver")
        print("=" * 70)
        print("\n📱 Access the application at: http://localhost:5000")
        print("📋 API endpoint: POST /api/format-reference")
        print("\n💡 How it works:")
        print("   1. Parses reference to extract title, author, year")
        print("   2. Searches Crossref/OpenAlex using title+author+year")
        print("   3. Fetches complete metadata when DOI is found")
        print("   4. Stores all extracted data")
        print("   5. Formats using stored data in selected style")
        print("   6. Flags retracted articles")
        print("\n⚠️  Press Ctrl+C to stop the server\n")
        app.run(debug=True, host='0.0.0.0', port=5000)

else:
    print("=" * 70)
    print("CiteIntegrity Pro - Smart Reference Formatter")
    print("=" * 70)
    print("Flask not installed. To run the web server:")
    print("  pip install Flask flask-cors requests")
    print("  python formatter.py")
    print("=" * 70)
    
    # Test functionality
    if __name__ == '__main__':
        test_refs = [
            "Smith, J. (2020). Understanding AI. Journal of Technology, 15(2), 45-67.",
            "Eklemet, I., MacCarthy, J., & Gyamfaa, E. (2024). Moderating Role of Risk Management between Risk Exposure and Bank Performance. Theoretical Economics Letters, 14(2), 363-389."
        ]
        
        print("\n📝 Testing smart reference repair with DOI search:")
        print("-" * 60)
        
        for ref in test_refs:
            print(f"\n📄 Original: {ref[:80]}...")
            
            # Parse first
            parsed = smart_parse_reference(ref, "auto")
            print(f"   Parsed - Title: {parsed.get('title', 'N/A')[:50]}")
            print(f"   Parsed - Authors: {parsed.get('authors', 'N/A')[:30]}")
            print(f"   Parsed - Year: {parsed.get('year', 'N/A')}")
            
            # Search for DOI
            if parsed.get('title'):
                doi = search_doi_by_title_author_year(
                    parsed['title'],
                    parsed.get('authors', ''),
                    parsed.get('year', '')
                )
                if doi:
                    print(f"   ✓ DOI found: {doi}")
                    parsed['doi'] = doi
                    
                    # Fetch metadata
                    metadata = fetch_full_metadata_by_doi(doi)
                    if metadata:
                        print(f"   ✓ Metadata fetched: {metadata.get('source_api')}")
                        for key in ['authors', 'title', 'source', 'volume', 'issue', 'pages']:
                            if metadata.get(key):
                                print(f"     - {key}: {metadata[key][:40]}...")
                                parsed[key] = metadata[key]
            
            # Format
            formatted = format_reference(parsed, "apa7")
            print(f"\n   📝 Formatted (APA7): {formatted}")
            print("-" * 60)
