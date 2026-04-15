# =========================================
# formatter.py — CiteIntegrity Pro Core Engine
# Can be imported or run as API server
# =========================================

import re
import json
from typing import Dict, List, Optional
from datetime import datetime
from difflib import SequenceMatcher

# Try to import Flask (optional - for API mode)
try:
    from flask import Flask, request, jsonify
    from flask_cors import CORS
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False
    print("Warning: Flask not installed. Running in library mode only.")

import requests

# =========================================
# CONFIGURATION
# =========================================
CACHE_DOI_LOOKUPS = {}
OPENALEX_MAX_RESULTS = 5
CROSSREF_TIMEOUT = 5
OPENALEX_TIMEOUT = 5

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
        # Fix "M, A" pattern to "M.A."
        if re.search(r'[A-Z],\s+[A-Z]', parsed["authors"]):
            parts = parsed["authors"].split(',')
            if len(parts) >= 2:
                last = parts[0]
                initials = ''.join([p.strip() for p in parts[1:]])
                parsed["authors"] = f"{last}, {initials}."
    
    # Fix title case issues
    if parsed["title"]:
        if parsed["title"][0].islower():
            parsed["title"] = parsed["title"][0].upper() + parsed["title"][1:]
    
    # Remove duplicate patterns in source
    if parsed["source"]:
        parts = re.split(r',\s*(?=\d+\()', parsed["source"])
        if len(parts) > 1:
            parsed["source"] = parts[0]
    
    # Extract DOI from title if missing
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
        res = requests.get(url, timeout=5)
        
        if res.status_code != 200:
            return {}
        
        data = res.json()["message"]
        
        authors = []
        for a in data.get("author", []):
            family = a.get("family", "")
            given = a.get("given", "")
            if family or given:
                authors.append(f"{family} {given}".strip())
        
        result = {
            "authors": ", ".join(authors[:10]),
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
        
    except Exception as e:
        print(f"Crossref error for {doi}: {e}")
        return {}


def fetch_from_openalex(doi: str) -> dict:
    """Fetch metadata from OpenAlex"""
    try:
        url = f"https://api.openalex.org/works/https://doi.org/{doi}"
        res = requests.get(url, timeout=5)
        
        if res.status_code != 200:
            return {}
        
        data = res.json()
        
        authors = []
        for authorship in data.get("authorships", []):
            author = authorship.get("author", {})
            if author.get("display_name"):
                authors.append(author["display_name"])
        
        source_name = ""
        if data.get("host_venue"):
            source_name = data.get("host_venue", {}).get("display_name", "")
        elif data.get("primary_location", {}).get("source"):
            source_name = data.get("primary_location", {}).get("source", {}).get("display_name", "")
        
        result = {
            "authors": ", ".join(authors[:10]),
            "year": str(data.get("publication_year", "")),
            "title": data.get("title", ""),
            "source": source_name,
            "volume": data.get("biblio", {}).get("volume", ""),
            "issue": data.get("biblio", {}).get("issue", ""),
            "pages": f"{data.get('biblio', {}).get('first_page', '')}-{data.get('biblio', {}).get('last_page', '')}".strip("-"),
            "publisher": data.get("host_venue", {}).get("publisher", ""),
            "doi": doi,
            "url": data.get("doi", ""),
            "source_api": "OpenAlex"
        }
        
        return result
        
    except Exception as e:
        print(f"OpenAlex error for {doi}: {e}")
        return {}


def fetch_from_doi(doi: str, prefer_openalex: bool = False) -> dict:
    """Fetch DOI metadata with fallback"""
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
# 3. OPENALEX SEARCH (for missing DOIs)
# =========================================

def search_openalex_by_metadata(title: str, author: str = None, year: str = None) -> List[Dict]:
    """Search OpenAlex by title/author/year to find DOI"""
    if not title:
        return []
    
    clean_title = re.sub(r'[^\w\s]', '', title)[:200]
    query = f"https://api.openalex.org/works?search={requests.utils.quote(clean_title)}"
    
    if author and len(author) > 2:
        author_last = author.split(",")[0].split()[-1] if author else ""
        if author_last:
            query += f"&filter=authorships.author.display_name:{author_last}"
    
    if year and year.isdigit():
        query += f"&filter=publication_year:{year}"
    
    query += f"&per-page={OPENALEX_MAX_RESULTS}"
    
    try:
        response = requests.get(query, timeout=5)
        if response.status_code != 200:
            return []
        
        data = response.json()
        results = []
        
        for work in data.get("results", []):
            if work.get("doi"):
                score = work.get("relevance_score", 0) * 100
                work_title = work.get("title", "")
                if work_title and work_title.lower() == title.lower():
                    score += 30
                elif work_title and title.lower() in work_title.lower():
                    score += 15
                
                results.append({
                    "doi": work["doi"].replace("https://doi.org/", ""),
                    "score": score,
                    "title": work_title,
                    "source": "OpenAlex",
                    "year": str(work.get("publication_year", "")),
                    "authors": work.get("authorships", [{}])[0].get("author", {}).get("display_name", "") if work.get("authorships") else ""
                })
        
        return sorted(results, key=lambda x: x["score"], reverse=True)
        
    except Exception as e:
        print(f"OpenAlex search error: {e}")
        return []


def find_doi_for_reference(parsed: dict) -> List[Dict]:
    """Find DOI matches for a reference without DOI"""
    if not parsed["title"]:
        return []
    
    return search_openalex_by_metadata(
        parsed["title"], 
        parsed["authors"].split(",")[0] if parsed["authors"] else None,
        parsed["year"]
    )


def rank_doi_matches(matches: List[Dict], original_parsed: dict) -> List[Dict]:
    """Rank multiple DOI matches by confidence"""
    for match in matches:
        confidence = match.get("score", 50)
        
        if original_parsed["authors"] and match.get("authors"):
            author_similarity = SequenceMatcher(
                None, 
                original_parsed["authors"].lower(), 
                match["authors"].lower()
            ).ratio()
            confidence += author_similarity * 30
        
        if original_parsed["year"] and match.get("year"):
            if original_parsed["year"] == match["year"]:
                confidence += 20
        
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
# 4. AUTHOR CLEANING
# =========================================
def clean_authors(authors: str) -> str:
    if not authors:
        return "Author Unknown"
    
    if "et al" in authors.lower():
        return authors.strip()
    
    parts = [a.strip() for a in authors.split(",") if a.strip()]
    formatted = []
    
    for p in parts[:20]:
        names = p.split()
        if len(names) >= 2:
            last = names[-1]
            initials = []
            for n in names[:-1]:
                if n and n[0].isalpha():
                    initials.append(n[0] + ".")
            formatted.append(f"{last}, {' '.join(initials)}".strip())
        else:
            formatted.append(p)
    
    result = ", ".join(formatted)
    
    if len(parts) > 20:
        result = result.split(",")[0] + " et al."
    
    return result if result else "Author Unknown"


# =========================================
# 5. FORMATTERS
# =========================================
def format_apa7(p):
    authors = clean_authors(p["authors"])
    year = f"({p['year']})." if p["year"] else "(n.d.)."
    
    title = p["title"] if p["title"] else "No title"
    if title and not title[0].isupper():
        title = title[0].upper() + title[1:]
    
    source = p["source"] if p["source"] else "Unknown source"
    
    ref = f"{authors} {year} {title}. {source}"
    
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
    year = f"({p['year']})." if p["year"] else "(n.d.)."
    
    title = p["title"] if p["title"] else "No title"
    source = p["source"] if p["source"] else "Unknown source"
    
    ref = f"{authors} {year} {title}. {source}"
    
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
    year = p["year"] if p["year"] else "n.d."
    
    authors = p["authors"] if p["authors"] else "Unknown"
    if authors and "et al" not in authors.lower() and "," in authors:
        authors = authors.split(",")[0]
    
    title = p["title"] if p["title"] else "No title"
    source = p["source"] if p["source"] else "Unknown source"
    
    ref = f"{authors} ({year}) '{title}', {source}"
    
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
    authors = p["authors"] if p["authors"] else "Anonymous"
    
    if authors and authors != "Anonymous" and authors != "Author Unknown":
        author_parts = []
        for a in authors.split(",")[:6]:
            a = a.strip()
            names = a.split()
            if len(names) >= 2:
                last = names[-1]
                initials = "".join([n[0] for n in names[:-1] if n and n[0].isalpha()])
                author_parts.append(f"{last} {initials}")
            else:
                author_parts.append(a)
        
        authors = ", ".join(author_parts)
        if len(authors.split(",")) > 6:
            authors += ", et al"
    
    title = p["title"] if p["title"] else "No title"
    source = p["source"] if p["source"] else "Unknown source"
    
    ref = f"{authors}. {title}. {source}"
    
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
    
    return format_apa7(parsed)


# =========================================
# 6. REPAIR ENGINE
# =========================================
def compute_repair_score(p):
    score = 100
    
    if not p["authors"] or p["authors"] == "Author Unknown":
        score -= 20
    if not p["year"]:
        score -= 20
    if not p["title"] or p["title"] == "No title":
        score -= 25
    if not p["source"] or p["source"] == "Unknown source":
        score -= 15
    if not p["doi"] and not p["url"]:
        score -= 10
    
    return max(score, 0)


def get_repair_issues(p):
    issues = []
    
    if not p["authors"] or p["authors"] == "Author Unknown":
        issues.append("Missing author(s)")
    elif len(p["authors"]) < 5 and "," not in p["authors"]:
        issues.append("Author name seems incomplete")
    
    if not p["year"]:
        issues.append("Missing publication year")
    
    if not p["title"] or p["title"] == "No title":
        issues.append("Missing title")
    
    if not p["source"] or p["source"] == "Unknown source":
        issues.append("Missing journal/book title")
    
    if not p["doi"] and not p["url"]:
        issues.append("No DOI or URL")
    
    return issues


def repair_reference(raw, style, source_type, auto_enhance=True, auto_find_doi=True):
    parsed = parse_reference(raw, source_type)
    log = []
    alternative_dois = []
    
    # Auto-correct
    parsed = auto_correct_parsed(parsed)
    if parsed != parse_reference(raw, source_type):
        log.append("Applied auto-corrections")
    
    # Fetch from DOI
    if auto_enhance and parsed.get("doi"):
        doi_data = fetch_from_doi(parsed["doi"], prefer_openalex=True)
        if doi_data and doi_data.get("title"):
            for k, v in doi_data.items():
                if v and not parsed.get(k):
                    parsed[k] = v
                    log.append(f"Filled {k} from DOI")
    
    # Find DOI if missing
    if auto_find_doi and not parsed.get("doi") and parsed.get("title") and parsed["title"] != "No title":
        log.append("Searching for DOI via OpenAlex...")
        matches = find_doi_for_reference(parsed)
        
        if matches:
            ranked = rank_doi_matches(matches, parsed)
            alternative_dois = ranked
            
            if ranked and ranked[0]["final_confidence"] > 85:
                best_match = ranked[0]
                parsed["doi"] = best_match["doi"]
                log.append(f"Auto-assigned DOI: {best_match['doi']}")
                
                doi_data = fetch_from_doi(parsed["doi"], prefer_openalex=True)
                if doi_data and doi_data.get("title"):
                    for k, v in doi_data.items():
                        if v and not parsed.get(k):
                            parsed[k] = v
    
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
        "alternative_dois": alternative_dois[:3],
        "has_doi": bool(parsed.get("doi")),
        "needs_review": score < 70 or len(issues) > 2
    }


def process_references(raw_text, style, source_type="journal", auto_enhance=True, auto_find_doi=True):
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
            warnings.append(f"Reference {i}: {', '.join(result['issues'])}")
    
    return {
        "formatted": "\n\n".join(formatted),
        "warnings": warnings,
        "repair_results": repair_results,
        "total_references": len(lines),
        "average_confidence": sum(r["confidence"] for r in repair_results) / len(repair_results) if repair_results else 0
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
            "parsed": r["parsed"]
        }
        for r in repair_results
    ]


# =========================================
# 7. FLASK SERVER (OPTIONAL)
# =========================================

if FLASK_AVAILABLE:
    app = Flask(__name__)
    CORS(app)
    
    @app.route('/')
    def index():
        return '''
        <!DOCTYPE html>
        <html>
        <head>
            <title>CiteIntegrity Pro</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 40px; background: #f5f7fb; }
                .container { max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 12px; }
                h1 { color: #1f2937; }
                .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
                textarea { width: 100%; height: 300px; padding: 10px; border: 1px solid #ddd; border-radius: 8px; font-family: monospace; }
                button { background: #19b36b; color: white; padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; }
                select, .controls { margin: 10px 0; padding: 8px; }
                .output { background: #f9fafb; border: 1px solid #e5e7eb; padding: 15px; border-radius: 8px; min-height: 300px; white-space: pre-wrap; font-family: monospace; font-size: 13px; }
                .warnings { background: #fff7ed; border-left: 4px solid #f59e0b; padding: 10px; margin-top: 15px; }
                .hidden { display: none; }
                .checkbox-group { margin: 10px 0; }
                .checkbox-group label { margin-right: 20px; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🔧 CiteIntegrity Pro</h1>
                <p>Smart reference repair with OpenAlex integration, DOI auto-discovery, and multi-format export</p>
                
                <div class="grid">
                    <div>
                        <h3>📝 References to Format/Repair</h3>
                        <textarea id="raw_reference" placeholder="Paste one or more references (one per line)...&#10;&#10;Example:&#10;Adam, M, A. (2020). Financial literacy and behaviour: Journal of Finance, 12(2), 45-60."></textarea>
                        
                        <div class="controls">
                            <select id="style">
                                <option value="apa7">APA 7th Edition</option>
                                <option value="apa6">APA 6th Edition</option>
                                <option value="harvard">Harvard</option>
                                <option value="vancouver">Vancouver</option>
                            </select>
                            <select id="source_type">
                                <option value="journal">Journal Article</option>
                                <option value="book">Book</option>
                                <option value="webpage">Website</option>
                            </select>
                        </div>
                        
                        <div class="checkbox-group">
                            <label><input type="checkbox" id="auto_enhance" checked> 🔍 Auto-enhance from DOI</label>
                            <label><input type="checkbox" id="auto_find_doi" checked> 🌐 Auto-find missing DOIs (OpenAlex)</label>
                        </div>
                        
                        <button id="formatBtn">🚀 Format & Repair References</button>
                    </div>
                    
                    <div>
                        <h3>✅ Formatted & Repaired Output</h3>
                        <div id="formattedOutput" class="output">Your formatted references will appear here...</div>
                        <button id="copyBtn" style="margin-top: 10px;">📋 Copy to Clipboard</button>
                        <div id="warningsBox" class="warnings hidden">
                            <strong>⚠️ Warnings:</strong>
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
                
                formatBtn.addEventListener('click', async function() {
                    const rawText = rawReference.value.trim();
                    if (!rawText) {
                        formattedOutput.textContent = 'Please paste at least one reference.';
                        return;
                    }
                    
                    formattedOutput.textContent = '🔄 Processing with OpenAlex...';
                    formatBtn.disabled = true;
                    
                    const formData = new FormData();
                    formData.append('raw_reference', rawText);
                    formData.append('style', style.value);
                    formData.append('source_type', sourceType.value);
                    formData.append('auto_enhance', autoEnhance.checked);
                    formData.append('auto_find_doi', autoFindDoi.checked);
                    
                    try {
                        const response = await fetch('/api/format-reference', {
                            method: 'POST',
                            body: formData
                        });
                        
                        const data = await response.json();
                        
                        if (!data.success) {
                            formattedOutput.textContent = data.message || 'Formatting failed.';
                            return;
                        }
                        
                        formattedOutput.textContent = data.formatted || 'No output returned.';
                        
                        if (data.warnings && data.warnings.length > 0) {
                            warningsList.innerHTML = data.warnings.map(w => `<li>${w}</li>`).join('');
                            warningsBox.classList.remove('hidden');
                        } else {
                            warningsBox.classList.add('hidden');
                        }
                    } catch (error) {
                        formattedOutput.textContent = 'Error: ' + error.message;
                    } finally {
                        formatBtn.disabled = false;
                    }
                });
                
                copyBtn.addEventListener('click', async function() {
                    const text = formattedOutput.textContent;
                    if (text && text !== 'Your formatted references will appear here...' && !text.includes('Processing')) {
                        await navigator.clipboard.writeText(text);
                        copyBtn.textContent = '✓ Copied!';
                        setTimeout(() => copyBtn.textContent = '📋 Copy to Clipboard', 2000);
                    }
                });
            </script>
        </body>
        </html>
        '''
    
    @app.route('/api/format-reference', methods=['POST'])
    def format_reference_api():
        try:
            if request.is_json:
                data = request.get_json()
                raw_text = data.get('raw_reference') or data.get('raw_text', '')
                style = data.get('style', 'apa7')
                source_type = data.get('source_type', 'journal')
                auto_enhance = data.get('auto_enhance', True)
                auto_find_doi = data.get('auto_find_doi', True)
            else:
                raw_text = request.form.get('raw_reference', '')
                style = request.form.get('style', 'apa7')
                source_type = request.form.get('source_type', 'journal')
                auto_enhance = request.form.get('auto_enhance', 'true').lower() == 'true'
                auto_find_doi = request.form.get('auto_find_doi', 'true').lower() == 'true'
            
            if not raw_text or not raw_text.strip():
                return jsonify({'success': False, 'message': 'Please provide at least one reference.'}), 400
            
            result = process_references(raw_text, style, source_type, auto_enhance, auto_find_doi)
            
            response_data = {
                'success': True,
                'formatted': result['formatted'],
                'warnings': result['warnings'],
                'repair_results': export_to_dict(result['repair_results']),
                'total_references': result['total_references'],
                'average_confidence': round(result['average_confidence'], 1),
            }
            
            return jsonify(response_data), 200
            
        except Exception as e:
            print(f"Error: {e}")
            return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500
    
    @app.route('/api/health', methods=['GET'])
    def health_check():
        return jsonify({
            'status': 'healthy',
            'version': '2.0.0',
            'features': ['OpenAlex', 'DOI auto-discovery', 'APA7', 'APA6', 'Harvard', 'Vancouver']
        }), 200
    
    if __name__ == '__main__':
        print("=" * 60)
        print("🔧 CiteIntegrity Pro - Running from formatter.py")
        print("=" * 60)
        print(f"✅ OpenAlex Integration: ENABLED")
        print(f"✅ DOI Auto-discovery: ENABLED")
        print(f"✅ All Citation Styles: ENABLED")
        print("=" * 60)
        print("\n📱 Access the application at: http://localhost:5000")
        print("⚠️  Press Ctrl+C to stop the server\n")
        app.run(debug=True, host='0.0.0.0', port=5000)

else:
    # Library mode - no Flask
    print("=" * 60)
    print("CiteIntegrity Pro Core Engine")
    print("=" * 60)
    print("Running in library mode. To start the web server, install Flask:")
    print("  pip install Flask flask-cors")
    print("=" * 60)
    
    # Example usage when run directly
    if __name__ == '__main__':
        test_ref = "Adam, M, A. (2020). Financial literacy and behaviour: Journal of Finance, 12(2), 45-60."
        print("\nTesting repair_reference function:")
        print("-" * 40)
        result = repair_reference(test_ref, "apa7", "journal", True, True)
        print(f"Original: {result['original']}")
        print(f"Formatted: {result['formatted']}")
        print(f"Confidence: {result['confidence']}%")
        print(f"Issues: {result['issues']}")
        print(f"Has DOI: {result['has_doi']}")
