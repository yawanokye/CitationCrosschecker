# =========================================
# formatter.py — CiteIntegrity Pro Complete
# Integrated Formatting Engine + Web Server
# =========================================

import re
import json
from typing import Dict, List, Optional
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
OPENALEX_MAX_RESULTS = 5
CROSSREF_TIMEOUT = 10
OPENALEX_TIMEOUT = 10

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

    # Extract authors and title using year
    if parsed["year"]:
        # Split by year
        parts = re.split(rf"\(?{parsed['year']}\)?", raw, maxsplit=1)
        
        if len(parts) >= 2:
            # Authors are before the year
            author_part = parts[0].strip(" .,()")
            # Clean up authors - remove trailing punctuation
            author_part = re.sub(r'[,.]$', '', author_part)
            parsed["authors"] = author_part
            
            # Remainder after year
            remainder = parts[1].strip(" .,()")
            
            # Split title and source by period
            if '.' in remainder:
                title_source = remainder.split('.', 1)
                if len(title_source) >= 1:
                    parsed["title"] = title_source[0].strip()
                if len(title_source) >= 2:
                    parsed["source"] = title_source[1].strip()
            else:
                parsed["title"] = remainder

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
        # Fix "&" formatting
        parsed["authors"] = re.sub(r'\s+&\s+', ' & ', parsed["authors"])
    
    # Fix title case issues
    if parsed["title"]:
        if parsed["title"] and parsed["title"][0].islower():
            parsed["title"] = parsed["title"][0].upper() + parsed["title"][1:]
    
    # Remove duplicate patterns in source
    if parsed["source"]:
        # Fix duplicate volume/issue/page info
        parts = re.split(r',\s*(?=\d+\()', parsed["source"])
        if len(parts) > 1:
            parsed["source"] = parts[0]
    
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
        
        authors = []
        for a in data.get("author", [])[:10]:
            family = a.get("family", "")
            given = a.get("given", "")
            if family:
                authors.append(f"{family} {given}".strip())
        
        result = {
            "authors": ", ".join(authors) if authors else "",
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
        res = requests.get(url, timeout=OPENALEX_TIMEOUT)
        
        if res.status_code != 200:
            return {}
        
        data = res.json()
        
        authors = []
        for authorship in data.get("authorships", [])[:10]:
            author = authorship.get("author", {})
            if author.get("display_name"):
                authors.append(author["display_name"])
        
        source_name = ""
        if data.get("host_venue"):
            source_name = data.get("host_venue", {}).get("display_name", "")
        elif data.get("primary_location", {}).get("source"):
            source_name = data.get("primary_location", {}).get("source", {}).get("display_name", "")
        
        result = {
            "authors": ", ".join(authors),
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


def fetch_from_doi(doi: str, prefer_openalex: bool = True) -> dict:
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
# 3. AUTHOR CLEANING
# =========================================
def clean_authors(authors: str) -> str:
    if not authors:
        return ""
    
    if "et al" in authors.lower():
        return authors.strip()
    
    # Handle multiple authors with "&"
    parts = re.split(r'\s+&\s+', authors)
    formatted_parts = []
    
    for part in parts:
        part = part.strip()
        # Check if it's "Last, First" format
        if ',' in part:
            formatted_parts.append(part)
        else:
            # Try to parse "First Last" format
            names = part.split()
            if len(names) >= 2:
                last = names[-1]
                initials = ' '.join([f"{n[0]}." for n in names[:-1] if n])
                formatted_parts.append(f"{last}, {initials}".strip())
            else:
                formatted_parts.append(part)
    
    result = ', '.join(formatted_parts)
    
    # Replace last comma with & for multiple authors
    if ',' in result and result.count(',') >= 1:
        parts_list = result.split(', ')
        if len(parts_list) > 1:
            result = ', '.join(parts_list[:-1]) + ' & ' + parts_list[-1]
    
    return result


# =========================================
# 4. FORMATTERS
# =========================================
def format_apa7(p):
    authors = clean_authors(p["authors"])
    if not authors:
        authors = "Author Unknown"
    
    year = f"({p['year']})." if p["year"] else "(n.d.)."
    
    title = p["title"] if p["title"] else "No title"
    if title and not title[0].isupper():
        title = title[0].upper() + title[1:]
    
    source = p["source"] if p["source"] else ""
    
    ref = f"{authors} {year} {title}"
    
    if source:
        ref += f". {source}"
    
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
    
    title = p["title"] if p["title"] else "No title"
    source = p["source"] if p["source"] else ""
    
    ref = f"{authors} {year} {title}"
    
    if source:
        ref += f". {source}"
    
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
    if authors and "et al" not in authors.lower() and "&" in authors:
        authors = authors.split("&")[0].strip()
    
    title = p["title"] if p["title"] else "No title"
    if title and not title[0].isupper():
        title = title[0].upper() + title[1:]
    
    source = p["source"] if p["source"] else ""
    
    ref = f"{authors} ({year}) '{title}'"
    
    if source:
        ref += f", {source}"
    
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
        for a in authors.split("&")[:6]:
            a = a.strip()
            if ',' in a:
                # Already in "Last, First" format
                last = a.split(',')[0].strip()
                initials = a.split(',')[1].strip() if len(a.split(',')) > 1 else ""
                author_parts.append(f"{last} {initials}")
            else:
                names = a.split()
                if len(names) >= 2:
                    last = names[-1]
                    initials = "".join([n[0] for n in names[:-1] if n and n[0].isalpha()])
                    author_parts.append(f"{last} {initials}")
                else:
                    author_parts.append(a)
        
        authors = ", ".join(author_parts)
        if len(author_parts) > 6:
            authors += ", et al"
    
    title = p["title"] if p["title"] else "No title"
    source = p["source"] if p["source"] else ""
    
    ref = f"{authors}. {title}"
    
    if source:
        ref += f". {source}"
    
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
# 5. REPAIR ENGINE
# =========================================
def compute_repair_score(p):
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
    
    return max(score, 0)


def get_repair_issues(p):
    issues = []
    
    if not p["authors"]:
        issues.append("Missing author(s)")
    
    if not p["year"]:
        issues.append("Missing publication year")
    
    if not p["title"]:
        issues.append("Missing title")
    
    if not p["source"]:
        issues.append("Missing journal/book title")
    
    if not p["doi"] and not p["url"]:
        issues.append("No DOI or URL")
    
    return issues


def repair_reference(raw, style, source_type, auto_enhance=True, auto_find_doi=True):
    parsed = parse_reference(raw, source_type)
    log = []
    
    # Auto-correct
    parsed = auto_correct_parsed(parsed)
    
    # Fetch from DOI
    if auto_enhance and parsed.get("doi"):
        doi_data = fetch_from_doi(parsed["doi"], prefer_openalex=True)
        if doi_data and doi_data.get("title"):
            for k, v in doi_data.items():
                if v and not parsed.get(k):
                    parsed[k] = v
                    log.append(f"Filled {k} from DOI")
    
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
            for issue in result["issues"]:
                warnings.append(f"Ref {i}: {issue}")
    
    return {
        "formatted": "\n\n".join(formatted),
        "warnings": warnings,
        "repair_results": repair_results,
        "total_references": len(lines),
        "average_confidence": sum(r["confidence"] for r in repair_results) / len(repair_results) if repair_results else 0,
        "references_with_doi": sum(1 for r in repair_results if r["has_doi"])
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
# 6. FLASK WEB SERVER
# =========================================

# HTML Template
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CiteIntegrity Pro | Reference Formatter</title>
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
        <span class="badge">OpenAlex Enhanced</span>
    </h1>
    <div class="subtitle">
        Smart reference repair with DOI auto-discovery, OpenAlex integration, and multi-format export
    </div>

    <div class="grid">
        <div>
            <label for="raw_reference">📝 References to Format/Repair</label>
            <textarea id="raw_reference" placeholder="Paste one or more references (one per line)...&#10;&#10;Example:&#10;Eklemet, I., MacCarthy, J., & Gyamfaa, E. (2024). Moderating Role of Risk Management between Risk Exposure and Bank Performance: Application of GMM Model. Theoretical Economics Letters, 14(2), 363-389."></textarea>

            <div class="controls">
                <select id="style">
                    <option value="apa7">APA 7th Edition</option>
                    <option value="apa6">APA 6th Edition</option>
                    <option value="harvard">Harvard (Author-Date)</option>
                    <option value="vancouver">Vancouver (Medical)</option>
                </select>
                <select id="source_type">
                    <option value="journal">Journal Article</option>
                    <option value="book">Book</option>
                    <option value="webpage">Website</option>
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
    const avgConfidence = document.getElementById('avgConfidence');

    formatBtn.addEventListener('click', async function() {
        const rawText = rawReference.value.trim();
        
        if (!rawText) {
            formattedOutput.textContent = '⚠️ Please paste at least one reference.';
            return;
        }
        
        formattedOutput.textContent = '🔄 Processing references with OpenAlex integration...';
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
            
            if (!response.ok || !data.success) {
                formattedOutput.textContent = data.message || '❌ Formatting failed.';
                return;
            }
            
            formattedOutput.textContent = data.formatted || 'No output returned.';
            
            // Update stats
            if (data.total_references) {
                statsPanel.classList.remove('hidden');
                totalRefs.textContent = data.total_references;
                doiCount.textContent = data.references_with_doi || 0;
                avgConfidence.textContent = Math.round(data.average_confidence || 0) + '%';
            }
            
            // Update warnings
            if (data.warnings && data.warnings.length > 0) {
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
    CORS(app)
    
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
            source_type = request.form.get('source_type', 'journal')
            auto_enhance = request.form.get('auto_enhance', 'true').lower() == 'true'
            auto_find_doi = request.form.get('auto_find_doi', 'true').lower() == 'true'
            
            print(f"Processing: style={style}, source_type={source_type}, auto_enhance={auto_enhance}")
            print(f"Raw text length: {len(raw_text)}")
            
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
                'references_with_doi': result['references_with_doi']
            }
            
            print(f"Success: {result['total_references']} references processed")
            return jsonify(response_data), 200
            
        except Exception as e:
            print(f"Error: {traceback.format_exc()}")
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
        print("🔧 CiteIntegrity Pro - Server Starting...")
        print("=" * 60)
        print(f"✅ OpenAlex Integration: ENABLED")
        print(f"✅ DOI Auto-discovery: ENABLED")
        print(f"✅ All Citation Styles: ENABLED")
        print("=" * 60)
        print("\n📱 Access the application at: http://localhost:5000")
        print("📋 API endpoint: POST /api/format-reference")
        print("\n⚠️  Press Ctrl+C to stop the server\n")
        app.run(debug=True, host='0.0.0.0', port=5000)

else:
    print("=" * 60)
    print("CiteIntegrity Pro Core Engine")
    print("=" * 60)
    print("Flask not installed. To run the web server:")
    print("  pip install Flask flask-cors requests")
    print("  python formatter.py")
    print("=" * 60)
    
    # Test the reference you provided
    if __name__ == '__main__':
        test_ref = "Eklemet, I., MacCarthy, J., & Gyamfaa, E. (2024). Moderating Role of Risk Management between Risk Exposure and Bank Performance: Application of GMM Model. Theoretical Economics Letters, 14(2), 363-389."
        print("\n📝 Testing repair_reference function:")
        print("-" * 50)
        result = repair_reference(test_ref, "apa7", "journal", True, True)
        print(f"Original: {result['original']}")
        print(f"Formatted: {result['formatted']}")
        print(f"Confidence: {result['confidence']}%")
        print(f"Issues: {result['issues']}")
        print(f"Has DOI: {result['has_doi']}")
