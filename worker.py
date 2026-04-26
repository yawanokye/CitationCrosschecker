# worker.py - Complete with all mismatch detection
import os
import sys
import json
import re
import time
import redis
import psycopg2
from collections import Counter
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
# MISMATCH DETECTION FUNCTIONS
# ============================================================

def extract_year_from_text(text):
    """Extract year from citation or reference text."""
    match = re.search(r'\b(19|20)\d{2}\b', str(text))
    return match.group(0) if match else None


def extract_surname_from_citation(citation):
    """Extract author surname from citation like (Smith, 2020) or Smith (2020)."""
    # Pattern for (Smith, 2020)
    match = re.search(r'\(([A-Z][a-z]+(?:\s*[-–]\s*[A-Z][a-z]+)?)', citation)
    if match:
        return match.group(1).lower()
    # Pattern for Smith (2020)
    match = re.search(r'^([A-Z][a-z]+(?:\s*[-–]\s*[A-Z][a-z]+)?)\s*\(', citation)
    if match:
        return match.group(1).lower()
    return None


def extract_surname_from_reference(ref_text):
    """Extract author surname from reference like Smith, J. (2020)."""
    match = re.search(r'^([A-Z][a-z]+(?:\s*[-–]\s*[A-Z][a-z]+)?)', ref_text)
    return match.group(1).lower() if match else None


def extract_initial_from_citation(citation):
    """Extract initial from citation like (J. Smith, 2020)."""
    match = re.search(r'\(([A-Z])\.\s+[A-Z][a-z]+', citation)
    return match.group(1) if match else None


def extract_initial_from_reference(ref_text):
    """Extract initial from reference like Smith, J. (2020)."""
    match = re.search(r'^[A-Z][a-z]+,\s+([A-Z])\.', ref_text)
    return match.group(1) if match else None


def detect_year_mismatches(c2r_rows):
    """Detect year mismatches between citations and references."""
    suggestions = []
    
    for row in c2r_rows:
        status = row.get("status", "")
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if status != "matched" or not in_text or not matched_ref:
            continue
        
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
                confidence = 0.95 if year_diff <= 2 else 0.85
                issue_type = "year_mismatch"
                reason = f"Year mismatch: '{citation_year}' should be '{ref_year}'"
            
            suggestions.append({
                "original": in_text,
                "suggested": suggested,
                "confidence": confidence,
                "issue_type": issue_type,
                "reason": reason,
                "fix_type": "required_fix",
                "category": "year"
            })
    
    return suggestions


def detect_author_surname_mismatches(c2r_rows):
    """Detect author surname mismatches between citations and references."""
    suggestions = []
    
    for row in c2r_rows:
        status = row.get("status", "")
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if status != "matched" or not in_text or not matched_ref:
            continue
        
        citation_surname = extract_surname_from_citation(in_text)
        ref_surname = extract_surname_from_reference(matched_ref)
        
        if citation_surname and ref_surname and citation_surname != ref_surname:
            # Check if it's a simple spelling variation
            if len(citation_surname) == len(ref_surname) and sum(1 for a, b in zip(citation_surname, ref_surname) if a != b) <= 2:
                confidence = 0.90
                reason = f"Author surname spelling error: '{citation_surname.capitalize()}' should be '{ref_surname.capitalize()}'"
            elif citation_surname in ref_surname or ref_surname in citation_surname:
                confidence = 0.85
                reason = f"Author surname missing part: '{citation_surname.capitalize()}' should be '{ref_surname.capitalize()}'"
            else:
                confidence = 0.75
                reason = f"Author surname mismatch: '{citation_surname.capitalize()}' should be '{ref_surname.capitalize()}'"
            
            suggested = re.sub(r'\([A-Z][a-z]+', f'({ref_surname.capitalize()}', in_text, count=1)
            suggested = re.sub(r'^[A-Z][a-z]+\s*\(', f'{ref_surname.capitalize()} (', suggested)
            
            suggestions.append({
                "original": in_text,
                "suggested": suggested,
                "confidence": confidence,
                "issue_type": "author_surname_mismatch",
                "reason": reason,
                "fix_type": "required_fix" if confidence >= 0.85 else "review_required",
                "category": "author"
            })
    
    return suggestions


def detect_author_initial_mismatches(c2r_rows):
    """Detect author initial mismatches between citations and references."""
    suggestions = []
    
    for row in c2r_rows:
        status = row.get("status", "")
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if status != "matched" or not in_text or not matched_ref:
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


def detect_document_wide_initial_inconsistency(c2r_rows):
    """Detect if document has mix of citations with and without initials."""
    import re
    
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
            for citation in citations_without_initials[:10]:  # Limit to 10 examples
                surname = extract_surname_from_citation(citation)
                year = extract_year_from_text(citation)
                if surname and year:
                    # Try to find common initial from existing citations
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
        "with_and_symbol": 0,
        "with_and_word": 0,
    }
    
    citation_examples = {key: [] for key in styles_detected.keys()}
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        if not in_text:
            continue
        
        # Parenthetical vs Narrative
        if re.match(r'\([^)]+\)', in_text):
            styles_detected["parenthetical"] += 1
            citation_examples["parenthetical"].append(in_text)
        elif re.match(r'^[A-Z][a-z]+\s*\(', in_text):
            styles_detected["narrative"] += 1
            citation_examples["narrative"].append(in_text)
        
        # APA vs Harvard (comma)
        if ',' in in_text and re.search(r',\s*\d{4}', in_text):
            styles_detected["apa_comma"] += 1
            citation_examples["apa_comma"].append(in_text)
        elif re.search(r'\([A-Z][a-z]+\s+\d{4}\)', in_text):
            styles_detected["harvard_no_comma"] += 1
            citation_examples["harvard_no_comma"].append(in_text)
        
        # "&" vs "and"
        if '&' in in_text:
            styles_detected["with_and_symbol"] += 1
            citation_examples["with_and_symbol"].append(in_text)
        elif re.search(r'\band\b', in_text):
            styles_detected["with_and_word"] += 1
            citation_examples["with_and_word"].append(in_text)
    
    suggestions = []
    
    # Check parenthetical vs narrative
    if styles_detected["parenthetical"] > 0 and styles_detected["narrative"] > 0:
        total = styles_detected["parenthetical"] + styles_detected["narrative"]
        for citation in citation_examples["narrative"][:5]:
            match = re.search(r'^([A-Z][a-z]+)\s*\((\d{4})\)', citation)
            if match:
                suggested = f"({match.group(1)}, {match.group(2)})"
                suggestions.append({
                    "original": citation,
                    "suggested": suggested,
                    "confidence": 0.85,
                    "issue_type": "mixed_style_parenthetical_vs_narrative",
                    "reason": f"Document has both parenthetical ({styles_detected['parenthetical']}) and narrative ({styles_detected['narrative']}) citations. Convert to parenthetical.",
                    "fix_type": "review_required",
                    "category": "style"
                })
                break
    
    # Check APA vs Harvard
    if styles_detected["apa_comma"] > 0 and styles_detected["harvard_no_comma"] > 0:
        for citation in citation_examples["harvard_no_comma"][:5]:
            suggested = re.sub(r'\(([A-Z][a-z]+)\s+(\d{4})\)', r'(\1, \2)', citation)
            if suggested != citation:
                suggestions.append({
                    "original": citation,
                    "suggested": suggested,
                    "confidence": 0.80,
                    "issue_type": "mixed_style_apa_vs_harvard",
                    "reason": f"Document has APA ({styles_detected['apa_comma']}) and Harvard ({styles_detected['harvard_no_comma']}) styles. Add comma for APA consistency.",
                    "fix_type": "review_required",
                    "category": "style"
                })
                break
    
    # Check "&" vs "and"
    if styles_detected["with_and_symbol"] > 0 and styles_detected["with_and_word"] > 0:
        for citation in citation_examples["with_and_word"][:5]:
            suggested = citation.replace(" and ", " & ")
            if suggested != citation:
                suggestions.append({
                    "original": citation,
                    "suggested": suggested,
                    "confidence": 0.75,
                    "issue_type": "mixed_style_and_symbol",
                    "reason": f"Document has both '&' ({styles_detected['with_and_symbol']}) and 'and' ({styles_detected['with_and_word']}). Use '&' for consistency.",
                    "fix_type": "review_required",
                    "category": "style"
                })
                break
    
    return suggestions


def detect_reference_formatting_issues(references_raw):
    """Detect formatting issues in reference entries."""
    suggestions = []
    
    for ref in references_raw:
        original = ref
        
        # Fix 1: Add DOI prefix
        doi_match = re.search(r'\b10\.\d{4,9}/[^\s)]+', ref)
        if doi_match and 'doi:' not in ref.lower() and 'https://doi.org' not in ref.lower():
            doi = doi_match.group(0)
            suggested = ref.replace(doi, f"DOI: {doi}")
            suggestions.append({
                "original": original[:100],
                "suggested": suggested[:100],
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
                "original": original[:100],
                "suggested": suggested[:100],
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
                "original": original[:100],
                "suggested": suggested[:100],
                "confidence": 0.70,
                "issue_type": "missing_period",
                "reason": "Add trailing period at end of reference",
                "fix_type": "optional_fix",
                "category": "reference"
            })
    
    # Remove duplicates (by original text)
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
        
        # 1. Year mismatches
        year_suggestions = detect_year_mismatches(c2r_rows)
        all_suggestions.extend(year_suggestions)
        print(f"📅 Year mismatches: {len(year_suggestions)}")
        
        # 2. Author surname mismatches
        surname_suggestions = detect_author_surname_mismatches(c2r_rows)
        all_suggestions.extend(surname_suggestions)
        print(f"👤 Author surname mismatches: {len(surname_suggestions)}")
        
        # 3. Author initial mismatches
        initial_suggestions = detect_author_initial_mismatches(c2r_rows)
        all_suggestions.extend(initial_suggestions)
        print(f"🔤 Author initial mismatches: {len(initial_suggestions)}")
        
        # 4. Document-wide initial inconsistency
        style_initial_suggestions = detect_document_wide_initial_inconsistency(c2r_rows)
        all_suggestions.extend(style_initial_suggestions)
        print(f"📝 Document-wide initial inconsistencies: {len(style_initial_suggestions)}")
        
        # 5. Mixed referencing styles
        mixed_style_suggestions = detect_mixed_referencing_styles(c2r_rows)
        all_suggestions.extend(mixed_style_suggestions)
        print(f"🎨 Mixed referencing styles: {len(mixed_style_suggestions)}")
        
        # 6. Reference formatting issues
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
        print("   - Year mismatches")
        print("   - Author surname mismatches")
        print("   - Author initial mismatches")
        print("   - Document-wide initial inconsistency")
        print("   - Mixed referencing styles")
        print("   - Reference formatting issues")
        worker.work(burst=False)
