# =========================================
# formatter.py — CiteIntegrity Core Formatter + Repair Engine
# =========================================

import re
import requests
from typing import List, Dict, Optional, Tuple
from difflib import SequenceMatcher
import time

# =========================================
# 0. CONFIGURATION
# =========================================
OPENALEX_MAX_RESULTS = 5
CROSSREF_TIMEOUT = 5
OPENALEX_TIMEOUT = 5
CACHE_DOI_LOOKUPS = {}  # Simple in-memory cache

# =========================================
# 1. PARSE REFERENCE
# =========================================
def parse_reference(raw_reference: str, source_type: str) -> dict:
    raw = " ".join(raw_reference.strip().split())

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
    }

    # Year
    year_match = re.search(r"\b(19|20)\d{2}[a-z]?\b", raw)
    if year_match:
        parsed["year"] = year_match.group(0)

    # DOI
    doi_match = re.search(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", raw, re.I)
    if doi_match:
        parsed["doi"] = doi_match.group(1).rstrip(".,;")

    # URL
    url_match = re.search(r"(https?://\S+)", raw, re.I)
    if url_match:
        parsed["url"] = url_match.group(1).rstrip(".,;")

    # Volume, issue, pages
    vip = re.search(r"(\d+)\s*\((\d+)\)\s*,\s*([\d\-–]+)", raw)
    if vip:
        parsed["volume"] = vip.group(1)
        parsed["issue"] = vip.group(2)
        parsed["pages"] = vip.group(3)

    # Alternative page pattern (without issue)
    pages_only = re.search(r"pp?\.?\s*([\d\-–]+)", raw, re.I)
    if not parsed["pages"] and pages_only:
        parsed["pages"] = pages_only.group(1)

    # Split using year
    if parsed["year"]:
        parts = re.split(rf"\(?{parsed['year']}\)?", raw, maxsplit=1)

        if len(parts) >= 2:
            parsed["authors"] = parts[0].strip(" .,()")
            remainder = parts[1].strip(" .,()")

            title_split = re.split(r"\.\s+", remainder, maxsplit=1)

            if len(title_split) >= 1:
                parsed["title"] = title_split[0]

            if len(title_split) == 2:
                parsed["source"] = title_split[1]

    # Auto-correct common issues
    parsed = auto_correct_parsed(parsed)
    
    return parsed


def auto_correct_parsed(parsed: dict) -> dict:
    """Auto-correct common reference errors"""
    
    # Fix author formatting
    if parsed["authors"]:
        # Remove excessive spaces
        parsed["authors"] = re.sub(r'\s+', ' ', parsed["authors"]).strip()
        # Fix "et. al" -> "et al"
        parsed["authors"] = re.sub(r'et\.\s+al', 'et al', parsed["authors"], re.I)
        # Fix multiple commas
        parsed["authors"] = re.sub(r',,+', ',', parsed["authors"])
    
    # Fix title case issues (first letter capital)
    if parsed["title"]:
        if parsed["title"][0].islower():
            parsed["title"] = parsed["title"][0].upper() + parsed["title"][1:]
    
    # Extract DOI from title if missing (sometimes DOIs hide there)
    if not parsed["doi"] and parsed["title"]:
        doi_in_title = re.search(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", parsed["title"], re.I)
        if doi_in_title:
            parsed["doi"] = doi_in_title.group(1)
            parsed["title"] = re.sub(r'\s*10\.\d{4,9}/[-._;()/:A-Z0-9]+\s*', '', parsed["title"])
    
    return parsed


# =========================================
# 2. DOI FETCH (Crossref + OpenAlex)
# =========================================

def fetch_from_crossref(doi: str) -> dict:
    """Fetch metadata from Crossref"""
    if doi in CACHE_DOI_LOOKUPS:
        return CACHE_DOI_LOOKUPS[doi]
    
    try:
        url = f"https://api.crossref.org/works/{doi}"
        res = requests.get(url, timeout=CROSSREF_TIMEOUT)
        
        if res.status_code != 200:
            return {}
        
        data = res.json()["message"]
        
        result = {
            "authors": ", ".join(
                [f"{a.get('family', '')} {a.get('given', '')}".strip() 
                 for a in data.get("author", []) if a.get('family') or a.get('given')]
            ),
            "year": str(data.get("issued", {}).get("date-parts", [[None]])[0][0]) if data.get("issued") else "",
            "title": data.get("title", [""])[0] if data.get("title") else "",
            "source": data.get("container-title", [""])[0] if data.get("container-title") else "",
            "volume": data.get("volume", ""),
            "issue": data.get("issue", ""),
            "pages": data.get("page", ""),
            "publisher": data.get("publisher", ""),
            "doi": doi,
            "url": data.get("URL", ""),
            "source_api": "Crossref"
        }
        
        CACHE_DOI_LOOKUPS[doi] = result
        return result
        
    except Exception:
        return {}


def fetch_from_openalex(doi: str) -> dict:
    """Fetch metadata from OpenAlex (better coverage for preprints/grey literature)"""
    try:
        url = f"https://api.openalex.org/works/https://doi.org/{doi}"
        res = requests.get(url, timeout=OPENALEX_TIMEOUT)
        
        if res.status_code != 200:
            return {}
        
        data = res.json()
        
        # Parse OpenAlex format
        authors = []
        for author in data.get("authorships", []):
            a = author.get("author", {})
            if a.get("display_name"):
                authors.append(a["display_name"])
        
        result = {
            "authors": ", ".join(authors[:10]),  # Limit to 10 authors
            "year": str(data.get("publication_year", "")),
            "title": data.get("title", ""),
            "source": data.get("host_venue", {}).get("display_name", ""),
            "volume": data.get("biblio", {}).get("volume", ""),
            "issue": data.get("biblio", {}).get("issue", ""),
            "pages": f"{data.get('biblio', {}).get('first_page', '')}-{data.get('biblio', {}).get('last_page', '')}".strip("-"),
            "publisher": data.get("host_venue", {}).get("publisher", ""),
            "doi": doi,
            "url": data.get("doi", ""),
            "source_api": "OpenAlex"
        }
        
        return result
        
    except Exception:
        return {}


def fetch_from_doi(doi: str, prefer_openalex: bool = False) -> dict:
    """Fetch DOI metadata with fallback between Crossref and OpenAlex"""
    
    if prefer_openalex:
        result = fetch_from_openalex(doi)
        if result and result.get("title"):
            return result
        return fetch_from_crossref(doi)
    else:
        result = fetch_from_crossref(doi)
        if result and result.get("title"):
            return result
        return fetch_from_openalex(doi)


# =========================================
# 2.5 OPENALEX SEARCH (for missing DOIs)
# =========================================

def search_openalex_by_metadata(title: str, author: str = None, year: str = None) -> List[Dict]:
    """Search OpenAlex by title/author/year to find DOI"""
    
    query = f"https://api.openalex.org/works?search={title}"
    
    if author:
        # Extract last name for better matching
        author_last = author.split(",")[0].split()[-1]
        query += f"&filter=authorships.author.display_name:{author_last}"
    
    if year:
        query += f"&filter=publication_year:{year}"
    
    query += f"&per-page={OPENALEX_MAX_RESULTS}"
    
    try:
        response = requests.get(query, timeout=OPENALEX_TIMEOUT)
        if response.status_code != 200:
            return []
        
        data = response.json()
        results = []
        
        for work in data.get("results", []):
            if work.get("doi"):
                # Calculate relevance score
                score = work.get("relevance_score", 0) * 100
                
                # Boost exact title match
                if work.get("title", "").lower() == title.lower():
                    score += 30
                
                results.append({
                    "doi": work["doi"].replace("https://doi.org/", ""),
                    "score": score,
                    "title": work.get("title", ""),
                    "source": "OpenAlex",
                    "year": str(work.get("publication_year", "")),
                    "authors": work.get("authorships", [{}])[0].get("author", {}).get("display_name", "")
                })
        
        return sorted(results, key=lambda x: x["score"], reverse=True)
        
    except Exception:
        return []


def search_crossref_by_metadata(title: str, author: str = None, year: str = None) -> List[Dict]:
    """Search Crossref by metadata as fallback"""
    
    query = f"https://api.crossref.org/works?query.title={title}"
    
    if author:
        query += f"&query.author={author}"
    
    if year:
        query += f"&filter=from-pub-date:{year},until-pub-date:{year}"
    
    query += f"&rows={OPENALEX_MAX_RESULTS}"
    
    try:
        response = requests.get(query, timeout=CROSSREF_TIMEOUT)
        if response.status_code != 200:
            return []
        
        data = response.json()
        results = []
        
        for item in data.get("message", {}).get("items", []):
            if item.get("DOI"):
                score = 70  # Base score for Crossref match
                
                # Boost exact title
                if item.get("title", [""])[0].lower() == title.lower():
                    score += 30
                
                results.append({
                    "doi": item["DOI"],
                    "score": score,
                    "title": item.get("title", [""])[0],
                    "source": "Crossref",
                    "year": str(item.get("issued", {}).get("date-parts", [[None]])[0][0]),
                    "authors": ", ".join([f"{a.get('family', '')}" for a in item.get("author", [])[:3]])
                })
        
        return sorted(results, key=lambda x: x["score"], reverse=True)
        
    except Exception:
        return []


def find_doi_for_reference(parsed: dict) -> List[Dict]:
    """Find DOI matches for a reference without DOI"""
    
    if not parsed["title"]:
        return []
    
    # Try OpenAlex first (better coverage)
    matches = search_openalex_by_metadata(
        parsed["title"], 
        parsed["authors"].split(",")[0] if parsed["authors"] else None,
        parsed["year"]
    )
    
    # If no OpenAlex matches, try Crossref
    if not matches:
        matches = search_crossref_by_metadata(
            parsed["title"],
            parsed["authors"].split(",")[0] if parsed["authors"] else None,
            parsed["year"]
        )
    
    return matches


def rank_doi_matches(matches: List[Dict], original_parsed: dict) -> List[Dict]:
    """Rank multiple DOI matches by confidence"""
    
    for match in matches:
        confidence = match.get("score", 50)
        
        # Compare authors
        if original_parsed["authors"] and match.get("authors"):
            author_similarity = SequenceMatcher(
                None, 
                original_parsed["authors"].lower(), 
                match["authors"].lower()
            ).ratio()
            confidence += author_similarity * 30
        
        # Compare year
        if original_parsed["year"] and match.get("year"):
            if original_parsed["year"] == match["year"]:
                confidence += 20
        
        # Compare title
        if original_parsed["title"] and match.get("title"):
            title_similarity = SequenceMatcher(
                None,
                original_parsed["title"].lower(),
                match["title"].lower()
            ).ratio()
            confidence += title_similarity * 25
        
        match["final_confidence"] = min(confidence, 100)
    
    return sorted(matches, key=lambda x: x["final_confidence"], reverse=True)


# =========================================
# 3. AUTHOR CLEANING
# =========================================
def clean_authors(authors: str) -> str:
    if not authors:
        return ""
    
    parts = [a.strip() for a in authors.split(",") if a.strip()]
    formatted = []
    
    for p in parts:
        # Handle "et al"
        if "et al" in p.lower():
            formatted.append(p.strip())
            continue
        
        names = p.split()
        if len(names) >= 2:
            last = names[-1]
            initials = " ".join([n[0] + "." for n in names[:-1] if n[0].isalpha()])
            formatted.append(f"{last}, {initials}".strip())
        else:
            formatted.append(p)
    
    result = ", ".join(formatted)
    
    # Limit to 20 authors for APA (use "et al." after 20)
    author_count = result.count(",") + 1
    if author_count > 20:
        result = result.split(",")[0] + " et al."
    
    return result


# =========================================
# 4. FORMATTERS
# =========================================
def format_apa7(p):
    authors = clean_authors(p["authors"])
    if not authors:
        authors = "Author Unknown"
    
    year = f"({p['year']})." if p["year"] else "(n.d.)."
    
    # Title sentence case
    title = p["title"]
    if title and not title[0].isupper():
        title = title[0].upper() + title[1:]
    
    ref = f"{authors} {year} {title}. {p['source']}"
    
    if p["volume"]:
        ref += f", {p['volume']}"
        if p["issue"]:
            ref += f"({p['issue']})"
    
    if p["pages"]:
        ref += f", {p['pages']}"
    
    ref += "."
    
    if p["doi"]:
        ref += f" https://doi.org/{p['doi']}"
    elif p["url"]:
        ref += f" {p['url']}"
    
    return " ".join(ref.split())


def format_apa6(p):
    authors = clean_authors(p["authors"])
    if not authors:
        authors = "Author Unknown"
    
    year = f"({p['year']})." if p["year"] else "(n.d.)."
    
    ref = f"{authors} {year} {p['title']}. {p['source']}"
    
    if p["volume"]:
        ref += f", {p['volume']}"
        if p["issue"]:
            ref += f"({p['issue']})"
    
    if p["pages"]:
        ref += f", {p['pages']}"
    
    ref += "."
    
    if p["doi"]:
        ref += f" doi:{p['doi']}"
    elif p["url"]:
        ref += f" Retrieved from {p['url']}"
    
    return " ".join(ref.split())


def format_harvard(p):
    year = p["year"] or "n.d."
    
    # Harvard author-date format
    authors = p["authors"]
    if authors and "et al" not in authors.lower() and "," in authors:
        authors = authors.split(",")[0]  # First author only for in-text style
    
    ref = f"{authors} ({year}) '{p['title']}', {p['source']}"
    
    if p["volume"]:
        ref += f", {p['volume']}"
        if p["issue"]:
            ref += f"({p['issue']})"
    
    if p["pages"]:
        ref += f", pp. {p['pages']}"
    
    if p["doi"]:
        ref += f", doi: {p['doi']}."
    elif p["url"]:
        ref += f". Available at: {p['url']}."
    else:
        ref += "."
    
    return " ".join(ref.split())


def format_vancouver(p):
    """Vancouver style (numbered, minimal punctuation)"""
    authors = p["authors"]
    if authors:
        # Vancouver: Last name + initials without spaces
        author_parts = []
        for a in authors.split(",")[:6]:  # Max 6 authors
            names = a.strip().split()
            if len(names) >= 2:
                last = names[-1]
                initials = "".join([n[0] for n in names[:-1]])
                author_parts.append(f"{last} {initials}")
            else:
                author_parts.append(a)
        
        authors = ", ".join(author_parts)
        if len(authors.split(",")) > 6:
            authors += ", et al"
    else:
        authors = "Anonymous"
    
    ref = f"{authors}. {p['title']}. {p['source']}"
    
    if p["year"]:
        ref += f". {p['year']}"
    
    if p["volume"]:
        ref += f";{p['volume']}"
        if p["issue"]:
            ref += f"({p['issue']})"
    
    if p["pages"]:
        ref += f":{p['pages']}"
    
    if p["doi"]:
        ref += f". doi: {p['doi']}"
    
    return ref + "."


def format_reference(parsed, style):
    style = style.lower()
    
    if style == "apa7":
        return format_apa7(parsed)
    elif style == "apa6":
        return format_apa6(parsed)
    elif style == "harvard":
        return format_harvard(parsed)
    elif style == "vancouver":
        return format_vancouver(parsed)
    
    raise ValueError(f"Invalid style: {style}. Use 'apa7', 'apa6', 'harvard', or 'vancouver'")


# =========================================
# 5. REPAIR ENGINE (ENHANCED)
# =========================================
def compute_repair_score(p):
    """Compute confidence score for parsed reference completeness"""
    score = 100
    
    if not p["authors"]:
        score -= 20
    if not p["year"]:
        score -= 20
    if not p["title"]:
        score -= 25
    if not p["source"]:
        score -= 15
    if not p["doi"] and not p["url"]:
        score -= 10
    if not p["volume"] and not p["pages"]:
        score -= 5  # Minor penalty
    
    return max(score, 0)


def get_repair_issues(p):
    """Return list of issues found in reference"""
    issues = []
    
    if not p["authors"]:
        issues.append("Missing author(s)")
    elif len(p["authors"]) < 3:
        issues.append("Author name seems incomplete")
    
    if not p["year"]:
        issues.append("Missing publication year")
    elif not re.match(r"19|20", p["year"]):
        issues.append("Unusual year format")
    
    if not p["title"]:
        issues.append("Missing title")
    elif len(p["title"]) < 5:
        issues.append("Title seems too short")
    
    if not p["source"]:
        issues.append("Missing journal/book title")
    
    if not p["doi"] and not p["url"]:
        issues.append("No DOI or URL")
    elif not p["doi"] and p["url"]:
        issues.append("Has URL but no DOI (preferred)")
    
    return issues


def repair_reference(raw, style, source_type, auto_enhance=True, auto_find_doi=True):
    """Enhanced repair with DOI finding and ranking"""
    
    parsed = parse_reference(raw, source_type)
    log = []
    alternative_dois = []
    
    # Step 1: Auto-correct common issues
    parsed = auto_correct_parsed(parsed)
    if parsed != parse_reference(raw, source_type):
        log.append("Applied auto-corrections")
    
    # Step 2: Fetch from DOI if present
    if auto_enhance and parsed.get("doi"):
        doi_data = fetch_from_doi(parsed["doi"])
        for k, v in doi_data.items():
            if v and not parsed.get(k):
                parsed[k] = v
                log.append(f"Filled {k} from DOI ({doi_data.get('source_api', 'Crossref')})")
    
    # Step 3: Try to find DOI if missing
    if auto_find_doi and not parsed.get("doi") and parsed.get("title"):
        log.append("Searching for DOI...")
        matches = find_doi_for_reference(parsed)
        
        if matches:
            ranked = rank_doi_matches(matches, parsed)
            alternative_dois = ranked
            
            # Auto-select best match if confidence > 85%
            if ranked and ranked[0]["final_confidence"] > 85:
                best_match = ranked[0]
                parsed["doi"] = best_match["doi"]
                log.append(f"Auto-assigned DOI: {best_match['doi']} (confidence: {best_match['final_confidence']:.0f}%)")
                
                # Fetch full metadata for the found DOI
                doi_data = fetch_from_doi(parsed["doi"], prefer_openalex=True)
                for k, v in doi_data.items():
                    if v and not parsed.get(k):
                        parsed[k] = v
                        log.append(f"Filled {k} from found DOI")
    
    # Step 4: Format
    formatted = format_reference(parsed, style)
    score = compute_repair_score(parsed)
    issues = get_repair_issues(parsed)
    
    return {
        "original": raw,
        "formatted": formatted,
        "parsed": parsed,
        "repair_log": log,
        "confidence": score,
        "issues": issues,
        "alternative_dois": alternative_dois,
        "has_doi": bool(parsed.get("doi")),
        "needs_review": score < 70 or len(issues) > 2
    }


# =========================================
# 6. BULK FUNCTIONS
# =========================================
def process_references(raw_text, style, variant=None, source_type="journal", auto_enhance=True):
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    
    formatted = []
    warnings = []
    repair_results = []
    
    for i, line in enumerate(lines, 1):
        result = repair_reference(line, style, source_type, auto_enhance)
        formatted.append(result["formatted"])
        repair_results.append(result)
        
        if result["issues"]:
            warnings.append(f"Reference {i}: {', '.join(result['issues'])}")
        if result.get("needs_review"):
            warnings.append(f"Reference {i}: Low confidence ({result['confidence']}%) - needs review")
    
    return {
        "formatted": "\n\n".join(formatted),
        "warnings": warnings,
        "repair_results": repair_results,
        "total_references": len(lines),
        "average_confidence": sum(r["confidence"] for r in repair_results) / len(repair_results) if repair_results else 0
    }


def repair_references_bulk(raw_text, style, source_type="journal", auto_enhance=True, auto_find_doi=True):
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    
    return [
        repair_reference(line, style, source_type, auto_enhance, auto_find_doi)
        for line in lines
    ]


def get_repair_summary(repair_results):
    """Generate summary statistics for batch repair"""
    
    total = len(repair_results)
    with_doi = sum(1 for r in repair_results if r["has_doi"])
    needs_review = sum(1 for r in repair_results if r.get("needs_review", False))
    avg_confidence = sum(r["confidence"] for r in repair_results) / total if total else 0
    
    # Count issues
    issue_counts = {}
    for result in repair_results:
        for issue in result.get("issues", []):
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    
    return {
        "total_references": total,
        "references_with_doi": with_doi,
        "doi_coverage": (with_doi / total * 100) if total else 0,
        "needs_review": needs_review,
        "average_confidence": avg_confidence,
        "issue_frequencies": issue_counts
    }


# =========================================
# 7. EXPORT FUNCTIONS
# =========================================
def export_to_dict(repair_results):
    """Export repair results as dict (JSON-ready)"""
    return [
        {
            "original": r["original"],
            "formatted": r["formatted"],
            "confidence": r["confidence"],
            "issues": r["issues"],
            "has_doi": r["has_doi"],
            "repair_log": r["repair_log"],
            "parsed": r["parsed"]
        }
        for r in repair_results
    ]


def export_to_csv_rows(repair_results):
    """Export to CSV-compatible rows"""
    rows = []
    for r in repair_results:
        rows.append({
            "Original Reference": r["original"],
            "Formatted Reference": r["formatted"],
            "Confidence (%)": r["confidence"],
            "Issues": "; ".join(r["issues"]),
            "Has DOI": r["has_doi"],
            "Repair Actions": "; ".join(r["repair_log"]),
            "DOI": r["parsed"].get("doi", ""),
            "Year": r["parsed"].get("year", ""),
            "Authors": r["parsed"].get("authors", ""),
            "Title": r["parsed"].get("title", ""),
            "Source": r["parsed"].get("source", "")
        })
    return rows
