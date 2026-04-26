# worker.py - Complete with all mismatch detection (FIXED VERSION)
import os
import sys
import json
import re
import time
import redis
import psycopg2
from collections import Counter
from difflib import SequenceMatcher
from rq import Worker, Queue, Connection
from engine import run_crosscheck, run_crosscheck_with_autofix

# Get connection strings
REDIS_URL = os.environ.get("REDIS_URL")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not REDIS_URL or not DATABASE_URL:
    print("ERROR: Missing REDIS_URL or DATABASE_URL")
    sys.exit(1)

# Connect to Redis
redis_conn = redis.from_url(REDIS_URL)


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def similarity_ratio(a, b):
    """Calculate similarity ratio between two strings."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def extract_year_from_text(text):
    """Extract year from citation or reference text."""
    if not text:
        return None
    match = re.search(r'\b(19|20)\d{2}\b', str(text))
    return match.group(0) if match else None


def extract_surname_from_citation(citation):
    """Extract author surname from citation like (Smith, 2020) or Smith (2020)."""
    if not citation:
        return None
    
    # Pattern for (Smith, 2020) or (Smith & Jones, 2020)
    match = re.search(r'\(([A-Z][a-z]+(?:\s*[-–][A-Z][a-z]+)?)', citation)
    if match:
        return match.group(1).lower()
    
    # Pattern for Smith (2020)
    match = re.search(r'^([A-Z][a-z]+(?:\s*[-–][A-Z][a-z]+)?)\s*\(', citation)
    if match:
        return match.group(1).lower()
    
    # Pattern for (Smith 2020) - no comma
    match = re.search(r'\(([A-Z][a-z]+)\s+\d{4}', citation)
    if match:
        return match.group(1).lower()
    
    return None


def extract_surname_from_reference(ref_text):
    """Extract author surname from reference like Smith, J. (2020)."""
    if not ref_text:
        return None
    
    # Pattern for "Smith, J. (2020)"
    match = re.search(r'^([A-Z][a-z]+(?:\s*[-–][A-Z][a-z]+)?)', ref_text)
    return match.group(1).lower() if match else None


def extract_all_authors_from_reference(ref_text):
    """Extract all author surnames from reference for multi-author papers."""
    if not ref_text:
        return []
    
    # Get everything before the year
    year_match = re.search(r'\b(19|20)\d{2}\b', ref_text)
    if not year_match:
        return []
    
    author_part = ref_text[:year_match.start()].strip()
    
    # Split by &, and, or commas
    authors = re.split(r'[&,]\s+|\s+and\s+', author_part)
    
    surnames = []
    for author in authors:
        author = author.strip()
        if not author:
            continue
        # Get first word (surname) for formats like "Smith, J." or "Smith J."
        surname_match = re.match(r'^([A-Z][a-z]+)', author)
        if surname_match:
            surnames.append(surname_match.group(1).lower())
    
    return surnames


def extract_initial_from_citation(citation):
    """Extract initial from citation like (J. Smith, 2020)."""
    if not citation:
        return None
    match = re.search(r'\(([A-Z])\.\s+[A-Z][a-z]+', citation)
    return match.group(1) if match else None


def extract_initial_from_reference(ref_text):
    """Extract initial from reference like Smith, J. (2020)."""
    if not ref_text:
        return None
    match = re.search(r'^[A-Z][a-z]+,\s+([A-Z])\.', ref_text)
    return match.group(1) if match else None


def is_narrative_citation(citation):
    """Check if citation is narrative (Smith, 2020) vs parenthetical (Smith, 2020)."""
    if not citation:
        return False
    # Narrative: Author name BEFORE the parenthesis
    return bool(re.match(r'^[A-Z][a-z]+', citation)) and '(' in citation


def is_parenthetical_citation(citation):
    """Check if citation is parenthetical (Smith, 2020)."""
    if not citation:
        return False
    return citation.startswith('(')


# ============================================================
# MISMATCH DETECTION FUNCTIONS
# ============================================================

def detect_year_mismatches(c2r_rows):
    """Detect year mismatches between citations and references."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        status = row.get("status", "")
        
        if not in_text or not matched_ref:
            continue
        
        # Check even if status is not "matched" - we can still suggest year fixes
        citation_year = extract_year_from_text(in_text)
        ref_year = extract_year_from_text(matched_ref)
        
        if citation_year and ref_year and citation_year != ref_year:
            # Check if it's a malformed year (2-3 digits)
            if len(citation_year) < 4:
                suggested = in_text.replace(citation_year, ref_year)
                confidence = 0.90
                issue_type = "malformed_year"
                reason = f"Malformed year '{citation_year}' corrected to '{ref_year}'"
            else:
                suggested = in_text.replace(citation_year, ref_year)
                year_diff = abs(int(citation_year) - int(ref_year))
                if year_diff <= 2:
                    confidence = 0.95
                elif year_diff <= 5:
                    confidence = 0.85
                else:
                    confidence = 0.75
                issue_type = "year_mismatch"
                reason = f"Year mismatch: '{citation_year}' should be '{ref_year}' (off by {year_diff} years)"
            
            suggestions.append({
                "original": in_text,
                "suggested": suggested,
                "confidence": confidence,
                "issue_type": issue_type,
                "reason": reason,
                "fix_type": "required_fix" if confidence >= 0.85 else "review_required",
                "category": "year"
            })
    
    # Remove duplicates
    seen = set()
    unique = []
    for s in suggestions:
        if s["original"] not in seen:
            seen.add(s["original"])
            unique.append(s)
    
    return unique


def detect_author_surname_mismatches(c2r_rows):
    """Detect author surname mismatches including spelling errors."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if not in_text or not matched_ref:
            continue
        
        citation_surname = extract_surname_from_citation(in_text)
        
        # Get all authors from reference (for multi-author papers)
        ref_surnames = extract_all_authors_from_reference(matched_ref)
        
        if not citation_surname or not ref_surnames:
            continue
        
        # Check if citation surname matches ANY author in reference
        best_match = None
        best_ratio = 0
        
        for ref_surname in ref_surnames:
            ratio = similarity_ratio(citation_surname, ref_surname)
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = ref_surname
        
        # If no good match (ratio < 0.85) or exact mismatch
        if best_match and citation_surname != best_match:
            # Spelling error detection (high similarity)
            if best_ratio >= 0.85:
                confidence = 0.90
                reason = f"Author surname spelling error: '{citation_surname.capitalize()}' should be '{best_match.capitalize()}'"
                fix_type = "required_fix"
            # Missing hyphen or name part
            elif citation_surname in best_match or best_match in citation_surname:
                confidence = 0.85
                reason = f"Author surname missing part: '{citation_surname.capitalize()}' should be '{best_match.capitalize()}'"
                fix_type = "required_fix"
            # Completely different author
            else:
                confidence = 0.70
                reason = f"Author surname mismatch: '{citation_surname.capitalize()}' should be '{best_match.capitalize()}' (check reference)"
                fix_type = "review_required"
            
            # Create suggestion
            suggested = in_text
            # Replace in parenthetical citation
            suggested = re.sub(r'\([A-Z][a-z]+', f'({best_match.capitalize()}', suggested, count=1)
            # Replace in narrative citation
            suggested = re.sub(r'^[A-Z][a-z]+\s*\(', f'{best_match.capitalize()} (', suggested)
            
            suggestions.append({
                "original": in_text,
                "suggested": suggested,
                "confidence": confidence,
                "issue_type": "author_surname_mismatch",
                "reason": reason,
                "fix_type": fix_type,
                "category": "author"
            })
    
    return suggestions


def detect_author_initial_mismatches(c2r_rows):
    """Detect author initial mismatches between citations and references."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if not in_text or not matched_ref:
            continue
        
        citation_initial = extract_initial_from_citation(in_text)
        ref_initial = extract_initial_from_reference(matched_ref)
        
        # Only flag if BOTH have initials AND they differ
        if citation_initial and ref_initial and citation_initial != ref_initial:
            suggested = re.sub(r'\([A-Z]\.', f'({ref_initial}.', in_text)
            
            suggestions.append({
                "original": in_text,
                "suggested": suggested,
                "confidence": 0.85,
                "issue_type": "author_initial_mismatch",
                "reason": f"Initial mismatch: '{citation_initial}.' in citation but '{ref_initial}.' in reference",
                "fix_type": "required_fix",
                "category": "author"
            })
    
    return suggestions


def detect_missing_references(c2r_rows):
    """Detect citations that have no matching reference."""
    suggestions = []
    
    for row in c2r_rows:
        status = row.get("status", "")
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if status == "not_found" and in_text and not matched_ref:
            suggestions.append({
                "original": in_text,
                "suggested": "Add corresponding reference entry",
                "confidence": 0.60,
                "issue_type": "missing_reference",
                "reason": f"This citation has no matching reference entry in the reference list",
                "fix_type": "review_required",
                "category": "missing"
            })
    
    return suggestions


def detect_document_wide_initial_inconsistency(c2r_rows):
    """Detect if document has mix of citations with and without initials."""
    citations_with_initials = []
    citations_without_initials = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        if not in_text:
            continue
        
        has_initial = bool(re.search(r'\([A-Z]\.\s+[A-Z][a-z]+', in_text))
        
        if has_initial:
            citations_with_initials.append(in_text)
        else:
            citations_without_initials.append(in_text)
    
    suggestions = []
    
    if citations_with_initials and citations_without_initials:
        total_with = len(citations_with_initials)
        total_without = len(citations_without_initials)
        
        if total_with >= total_without:
            # Add initials to those without
            for citation in citations_without_initials[:10]:
                surname = extract_surname_from_citation(citation)
                year = extract_year_from_text(citation)
                if surname and year:
                    common_initial = None
                    for ex in citations_with_initials:
                        if surname in ex.lower():
                            init_match = re.search(r'\(([A-Z])\.', ex)
                            if init_match:
                                common_initial = init_match.group(1)
                                break
                    
                    if common_initial:
                        suggested = f"({common_initial}. {surname.capitalize()}, {year})"
                        suggestions.append({
                            "original": citation,
                            "suggested": suggested,
                            "confidence": 0.75,
                            "issue_type": "inconsistent_initial_usage",
                            "reason": f"Document has mix: {total_with} citations with initials, {total_without} without. Add initials for consistency.",
                            "fix_type": "review_required",
                            "category": "style"
                        })
        else:
            # Remove initials from those that have them
            for citation in citations_with_initials[:10]:
                suggested = re.sub(r'\([A-Z]\.\s+', '(', citation)
                if suggested != citation:
                    suggestions.append({
                        "original": citation,
                        "suggested": suggested,
                        "confidence": 0.75,
                        "issue_type": "inconsistent_initial_usage",
                        "reason": f"Document has mix: {total_without} citations without initials, {total_with} with. Remove initials for consistency.",
                        "fix_type": "review_required",
                        "category": "style"
                    })
    
    return suggestions


def detect_mixed_referencing_styles(c2r_rows):
    """Detect mixed citation styles across the document."""
    styles_detected = {
        "parenthetical": 0,
        "narrative": 0,
        "apa_comma": 0,
        "harvard_no_comma": 0,
        "with_and_symbol_parenthetical": 0,
        "with_and_symbol_narrative": 0,
        "with_and_word_parenthetical": 0,
        "with_and_word_narrative": 0,
    }
    
    citation_examples = {key: [] for key in styles_detected.keys()}
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        if not in_text:
            continue
        
        is_narrative = is_narrative_citation(in_text)
        is_parenthetical = is_parenthetical_citation(in_text)
        
        # Parenthetical vs Narrative
        if is_parenthetical:
            styles_detected["parenthetical"] += 1
            citation_examples["parenthetical"].append(in_text)
        elif is_narrative:
            styles_detected["narrative"] += 1
            citation_examples["narrative"].append(in_text)
        
        # APA vs Harvard (comma)
        if ',' in in_text and re.search(r',\s*\d{4}', in_text):
            styles_detected["apa_comma"] += 1
            citation_examples["apa_comma"].append(in_text)
        elif re.search(r'\([A-Z][a-z]+\s+\d{4}\)', in_text) or re.search(r'[A-Z][a-z]+\s+\(\d{4}\)', in_text):
            styles_detected["harvard_no_comma"] += 1
            citation_examples["harvard_no_comma"].append(in_text)
        
        # "&" vs "and" - with context (parenthetical uses &, narrative uses and)
        if '&' in in_text:
            if is_parenthetical:
                styles_detected["with_and_symbol_parenthetical"] += 1
                citation_examples["with_and_symbol_parenthetical"].append(in_text)
            else:
                styles_detected["with_and_symbol_narrative"] += 1
                citation_examples["with_and_symbol_narrative"].append(in_text)
        elif re.search(r'\band\b', in_text):
            if is_parenthetical:
                styles_detected["with_and_word_parenthetical"] += 1
                citation_examples["with_and_word_parenthetical"].append(in_text)
            else:
                styles_detected["with_and_word_narrative"] += 1
                citation_examples["with_and_word_narrative"].append(in_text)
    
    suggestions = []
    
    # Check parenthetical vs narrative
    if styles_detected["parenthetical"] > 0 and styles_detected["narrative"] > 0:
        total_parenthetical = styles_detected["parenthetical"]
        total_narrative = styles_detected["narrative"]
        
        if total_parenthetical >= total_narrative:
            # Convert narrative to parenthetical
            for citation in citation_examples["narrative"][:5]:
                match = re.search(r'^([A-Z][a-z]+(?:\s*[-–][A-Z][a-z]+)?)\s*\((\d{4})\)', citation)
                if match:
                    suggested = f"({match.group(1)}, {match.group(2)})"
                    suggestions.append({
                        "original": citation,
                        "suggested": suggested,
                        "confidence": 0.85,
                        "issue_type": "mixed_style_parenthetical_vs_narrative",
                        "reason": f"Document has both parenthetical ({total_parenthetical}) and narrative ({total_narrative}) citations. Convert to parenthetical.",
                        "fix_type": "review_required",
                        "category": "style"
                    })
                    break
        else:
            # Convert parenthetical to narrative
            for citation in citation_examples["parenthetical"][:5]:
                match = re.search(r'\(([A-Z][a-z]+(?:\s*[-–][A-Z][a-z]+)?),\s*(\d{4})\)', citation)
                if match:
                    suggested = f"{match.group(1)} ({match.group(2)})"
                    suggestions.append({
                        "original": citation,
                        "suggested": suggested,
                        "confidence": 0.85,
                        "issue_type": "mixed_style_parenthetical_vs_narrative",
                        "reason": f"Document has both parenthetical ({total_parenthetical}) and narrative ({total_narrative}) citations. Convert to narrative.",
                        "fix_type": "review_required",
                        "category": "style"
                    })
                    break
    
    # Check APA vs Harvard
    if styles_detected["apa_comma"] > 0 and styles_detected["harvard_no_comma"] > 0:
        total_apa = styles_detected["apa_comma"]
        total_harvard = styles_detected["harvard_no_comma"]
        
        if total_apa >= total_harvard:
            # Add commas to Harvard style
            for citation in citation_examples["harvard_no_comma"][:5]:
                suggested = re.sub(r'\(([A-Z][a-z]+)\s+(\d{4})\)', r'(\1, \2)', citation)
                suggested = re.sub(r'([A-Z][a-z]+)\s+\((\d{4})\)', r'\1 (\2)', suggested)
                if suggested != citation:
                    suggestions.append({
                        "original": citation,
                        "suggested": suggested,
                        "confidence": 0.80,
                        "issue_type": "mixed_style_apa_vs_harvard",
                        "reason": f"Document has APA ({total_apa}) and Harvard ({total_harvard}) styles. Add comma for APA consistency.",
                        "fix_type": "review_required",
                        "category": "style"
                    })
                    break
        else:
            # Remove commas from APA style
            for citation in citation_examples["apa_comma"][:5]:
                suggested = re.sub(r'\(([A-Z][a-z]+),\s*(\d{4})\)', r'(\1 \2)', citation)
                suggested = re.sub(r'([A-Z][a-z]+),\s*\((\d{4})\)', r'\1 (\2)', suggested)
                if suggested != citation:
                    suggestions.append({
                        "original": citation,
                        "suggested": suggested,
                        "confidence": 0.80,
                        "issue_type": "mixed_style_apa_vs_harvard",
                        "reason": f"Document has APA ({total_apa}) and Harvard ({total_harvard}) styles. Remove comma for Harvard consistency.",
                        "fix_type": "review_required",
                        "category": "style"
                    })
                    break
    
    # Check "&" vs "and" - Parenthetical should use "&", Narrative should use "and"
    # Fix parenthetical citations using "and"
    if styles_detected["with_and_word_parenthetical"] > 0:
        for citation in citation_examples["with_and_word_parenthetical"][:5]:
            suggested = citation.replace(" and ", " & ")
            if suggested != citation:
                suggestions.append({
                    "original": citation,
                    "suggested": suggested,
                    "confidence": 0.85,
                    "issue_type": "mixed_style_and_symbol",
                    "reason": "Parenthetical citations should use '&' instead of 'and' per APA style.",
                    "fix_type": "required_fix",
                    "category": "style"
                })
                break
    
    # Fix narrative citations using "&"
    if styles_detected["with_and_symbol_narrative"] > 0:
        for citation in citation_examples["with_and_symbol_narrative"][:5]:
            suggested = citation.replace(" & ", " and ")
            if suggested != citation:
                suggestions.append({
                    "original": citation,
                    "suggested": suggested,
                    "confidence": 0.85,
                    "issue_type": "mixed_style_and_symbol",
                    "reason": "Narrative citations should use 'and' instead of '&' per APA style.",
                    "fix_type": "required_fix",
                    "category": "style"
                })
                break
    
    return suggestions


def detect_reference_formatting_issues(references_raw):
    """Detect formatting issues in reference entries."""
    suggestions = []
    
    for ref in references_raw:
        original = ref
        if not original:
            continue
        
        # Fix 1: Add DOI prefix
        doi_match = re.search(r'\b10\.\d{4,9}/[^\s)]+', ref)
        if doi_match and 'doi:' not in ref.lower() and 'https://doi.org' not in ref.lower():
            doi = doi_match.group(0)
            suggested = ref.replace(doi, f"DOI: {doi}")
            suggestions.append({
                "original": original[:150],
                "suggested": suggested[:150],
                "confidence": 0.95,
                "issue_type": "missing_doi_prefix",
                "reason": "Add 'DOI:' prefix to DOI",
                "fix_type": "required_fix",
                "category": "reference"
            })
        
        # Fix 2: HTTP to HTTPS
        elif 'http://' in ref and 'https://' not in ref:
            suggested = ref.replace('http://', 'https://')
            suggestions.append({
                "original": original[:150],
                "suggested": suggested[:150],
                "confidence": 0.90,
                "issue_type": "http_to_https",
                "reason": "Update HTTP to HTTPS for security",
                "fix_type": "required_fix",
                "category": "reference"
            })
        
        # Fix 3: Add missing period at end
        elif ref and not ref.rstrip().endswith('.'):
            suggested = ref.rstrip() + '.'
            suggestions.append({
                "original": original[:150],
                "suggested": suggested[:150],
                "confidence": 0.70,
                "issue_type": "missing_period",
                "reason": "Add trailing period at end of reference",
                "fix_type": "optional_fix",
                "category": "reference"
            })
    
    # Remove duplicates
    seen = set()
    unique_suggestions = []
    for s in suggestions:
        if s["original"] not in seen:
            seen.add(s["original"])
            unique_suggestions.append(s)
    
    return unique_suggestions


# ============================================================
# MAIN PROCESS_DOCUMENT FUNCTION
# ============================================================

def process_document(job_id, filename, style="apa", enable_autofix=False):
    """Process a document - runs in background"""
    print(f"🔥 Processing job {job_id}: {filename}")
    print(f"📋 enable_autofix flag received: {enable_autofix}")
    
    # FORCE AUTOFIX TO TRUE
    enable_autofix = True
    print(f"📋 FORCED enable_autofix to: {enable_autofix}")
    
    # Load file from Redis
    file_content = redis_conn.get(f"file:{job_id}")
    
    print(f"📦 File size from Redis: {len(file_content) if file_content else 0} bytes")
    
    if not file_content:
        raise Exception(f"❌ File not found in Redis for job {job_id}")

    try:
        # DB CONNECTION
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        
        cursor.execute(
            "UPDATE jobs SET status = 'processing', started_at = NOW() WHERE job_id = %s",
            (job_id,)
        )
        conn.commit()

        print("⚡ Running run_crosscheck_with_autofix...")

        result = run_crosscheck_with_autofix(
            file_bytes=file_content,
            filename=filename,
            style=style,
            verify_online=False,
            enable_autofix=True
        )

        print("🔍 === RESULT DEBUG ===")
        print("🔍 'autofix' in result:", "autofix" in result)
        
        # Ensure result has proper structure
        if "autofix" not in result:
            result["autofix"] = {
                "enabled": True,
                "suggestions": {
                    "citations": [],
                    "references": []
                }
            }
        
        # Get data for analysis
        c2r_rows = result.get("reconciliation_intext_to_reference", [])
        references_raw = result.get("references_raw", [])
        
        print(f"📊 Found {len(c2r_rows)} citation-reference pairs")
        print(f"📊 Found {len(references_raw)} references")
        
        # COLLECT ALL SUGGESTIONS
        all_suggestions = []
        
        # 1. Year mismatches (works on all rows, not just matched)
        year_suggestions = detect_year_mismatches(c2r_rows)
        all_suggestions.extend(year_suggestions)
        print(f"📅 Year mismatches: {len(year_suggestions)}")
        
        # 2. Author surname mismatches (spelling errors, missing parts)
        surname_suggestions = detect_author_surname_mismatches(c2r_rows)
        all_suggestions.extend(surname_suggestions)
        print(f"👤 Author surname mismatches: {len(surname_suggestions)}")
        
        # 3. Author initial mismatches
        initial_suggestions = detect_author_initial_mismatches(c2r_rows)
        all_suggestions.extend(initial_suggestions)
        print(f"🔤 Author initial mismatches: {len(initial_suggestions)}")
        
        # 4. Missing references
        missing_suggestions = detect_missing_references(c2r_rows)
        all_suggestions.extend(missing_suggestions)
        print(f"❌ Missing references: {len(missing_suggestions)}")
        
        # 5. Document-wide initial inconsistency
        style_initial_suggestions = detect_document_wide_initial_inconsistency(c2r_rows)
        all_suggestions.extend(style_initial_suggestions)
        print(f"📝 Document-wide initial inconsistencies: {len(style_initial_suggestions)}")
        
        # 6. Mixed referencing styles
        mixed_style_suggestions = detect_mixed_referencing_styles(c2r_rows)
        all_suggestions.extend(mixed_style_suggestions)
        print(f"🎨 Mixed referencing styles: {len(mixed_style_suggestions)}")
        
        # 7. Reference formatting issues
        ref_suggestions = detect_reference_formatting_issues(references_raw)
        all_suggestions.extend(ref_suggestions)
        print(f"📚 Reference formatting issues: {len(ref_suggestions)}")
        
        # Remove duplicates (by original text)
        seen = set()
        unique_suggestions = []
        for s in all_suggestions:
            key = s.get("original", "")
            if key and key not in seen:
                seen.add(key)
                unique_suggestions.append(s)
        
        print(f"💡 TOTAL UNIQUE SUGGESTIONS: {len(unique_suggestions)}")
        
        # Count by category
        categories = Counter(s.get("category", "other") for s in unique_suggestions)
        for cat, count in categories.items():
            print(f"  - {cat}: {count}")
        
        # Add suggestions to result
        if unique_suggestions:
            result["autofix"]["suggestions"]["citations"] = unique_suggestions
            result["suggestions"] = result["autofix"]["suggestions"]
            
            # Add statistics
            result["autofix"]["statistics"] = {
                "total": len(unique_suggestions),
                "high_confidence": len([s for s in unique_suggestions if s.get("confidence", 0) >= 0.85]),
                "medium_confidence": len([s for s in unique_suggestions if 0.70 <= s.get("confidence", 0) < 0.85]),
                "low_confidence": len([s for s in unique_suggestions if s.get("confidence", 0) < 0.70]),
                "by_category": dict(categories)
            }
            
            print(f"✅ Added {len(unique_suggestions)} suggestions to result")
            
            # Print sample
            for i, s in enumerate(unique_suggestions[:5]):
                print(f"  Sample {i+1}: [{s.get('category')}] {s.get('issue_type')} - {s.get('original', '')[:50]}...")
        else:
            print("⚠️ No suggestions generated")
        
        # Save result
        cursor.execute(
            "UPDATE jobs SET status = 'completed', result = %s, completed_at = NOW() WHERE job_id = %s",
            (json.dumps(result), job_id)
        )
        conn.commit()
        
        cursor.close()
        conn.close()
        
        # Cache result
        redis_conn.setex(f"result:{job_id}", 3600, json.dumps(result))
        redis_conn.delete(f"file:{job_id}")
        
        print(f"✅ Completed job {job_id}")
        return result
        
    except Exception as e:
        print(f"❌ Failed job {job_id}: {e}")
        import traceback
        traceback.print_exc()
        
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE jobs SET status = 'failed', error = %s WHERE job_id = %s",
                (str(e), job_id)
            )
            conn.commit()
            cursor.close()
            conn.close()
        except:
            pass
        
        raise e


# Start the worker
if __name__ == "__main__":
    print("🚀 Starting worker...")
    print(f"📊 Redis: {REDIS_URL[:50]}..." if REDIS_URL else "📊 Redis: NOT SET")
    print(f"💾 PostgreSQL: {'Connected' if DATABASE_URL else 'NOT SET'}")
    
    with Connection(redis_conn):
        queue = Queue("document_processing", connection=redis_conn)
        worker = Worker(["document_processing"])
        print("✅ Worker ready, waiting for jobs...")
        print("📋 Detection features enabled:")
        print("   - Year mismatches (including malformed years)")
        print("   - Author surname mismatches (spelling errors, missing parts)")
        print("   - Author initial mismatches")
        print("   - Missing references")
        print("   - Document-wide initial inconsistency")
        print("   - Mixed referencing styles (parenthetical/narrative, &/and, APA/Harvard)")
        print("   - Reference formatting issues (DOI prefix, HTTP→HTTPS, missing period)")
        worker.work(burst=False)
