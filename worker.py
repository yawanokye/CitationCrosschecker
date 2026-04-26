# worker.py - Complete with all mismatch detection (COVERS ALL TEST SCENARIOS)
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
    if not a or not b:
        return 0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def extract_year_from_text(text):
    """Extract year from citation or reference text."""
    if not text:
        return None
    match = re.search(r'\b(19|20)\d{2}\b', str(text))
    return match.group(0) if match else None


def extract_authors_from_citation(citation):
    """Extract all author surnames from citation.
    Handles: (Adam & Mensah, 2020), Adam and Mensah (2020), Adam et al. (2020)
    Returns list of author surnames in order."""
    if not citation:
        return []
    
    authors = []
    
    # Remove the year and parentheses for easier parsing
    clean_citation = re.sub(r'\b(19|20)\d{2}\b', '', citation)
    clean_citation = re.sub(r'[()]', '', clean_citation)
    clean_citation = clean_citation.strip()
    
    # Check for "et al."
    if 'et al' in clean_citation.lower():
        # Extract first author before "et al"
        first_author_match = re.match(r'^([A-Z][a-z]+(?:\s*[-–][A-Z][a-z]+)?)', clean_citation)
        if first_author_match:
            authors.append(first_author_match.group(1))
        return authors
    
    # Split by "&" or "and"
    if '&' in clean_citation:
        parts = clean_citation.split('&')
    elif ' and ' in clean_citation.lower():
        parts = re.split(r'\s+and\s+', clean_citation, flags=re.I)
    else:
        parts = [clean_citation]
    
    for part in parts:
        part = part.strip()
        if part:
            # Extract surname (first word)
            surname_match = re.match(r'^([A-Z][a-z]+(?:\s*[-–][A-Z][a-z]+)?)', part)
            if surname_match:
                authors.append(surname_match.group(1))
    
    return authors


def extract_authors_from_reference(ref_text):
    """Extract all author surnames from reference in order.
    Handles: Adam, A. M., & Mensah, K. (2021)
    Returns list of author surnames in order."""
    if not ref_text:
        return []
    
    authors = []
    
    # Get everything before the year
    year_match = re.search(r'\b(19|20)\d{2}\b', ref_text)
    if not year_match:
        return authors
    
    author_part = ref_text[:year_match.start()].strip()
    
    # Split by & or "and"
    if '&' in author_part:
        parts = author_part.split('&')
    elif ' and ' in author_part.lower():
        parts = re.split(r'\s+and\s+', author_part, flags=re.I)
    else:
        parts = [author_part]
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
        
        # Extract surname (first word before comma or space)
        # Format: "Adam, A. M." or "Adam A. M." or "Adam"
        surname_match = re.match(r'^([A-Z][a-z]+(?:\s*[-–][A-Z][a-z]+)?)', part)
        if surname_match:
            authors.append(surname_match.group(1))
    
    return authors


def extract_full_author_string_from_reference(ref_text):
    """Extract the full author string from reference for replacement."""
    if not ref_text:
        return ""
    
    year_match = re.search(r'\b(19|20)\d{2}\b', ref_text)
    if not year_match:
        return ""
    
    return ref_text[:year_match.start()].strip()


def is_narrative_citation(citation):
    """Check if citation is narrative (Adam and Mensah, 2020)."""
    if not citation:
        return False
    # Narrative: Author names BEFORE the parenthesis with year inside
    return bool(re.match(r'^[A-Z][a-z]', citation)) and '(' in citation


def is_parenthetical_citation(citation):
    """Check if citation is parenthetical (Adam & Mensah, 2020)."""
    if not citation:
        return False
    return citation.startswith('(')


# ============================================================
# MISMATCH DETECTION FUNCTIONS
# ============================================================

def detect_year_mismatch(citation, reference):
    """Detect year mismatch between citation and reference."""
    citation_year = extract_year_from_text(citation)
    ref_year = extract_year_from_text(reference)
    
    if citation_year and ref_year and citation_year != ref_year:
        return {
            "citation_year": citation_year,
            "ref_year": ref_year,
            "mismatch": True
        }
    return {"mismatch": False}


def detect_author_mismatch(citation, reference):
    """Detect author mismatches including spelling, missing parts, and order."""
    citation_authors = extract_authors_from_citation(citation)
    ref_authors = extract_authors_from_reference(reference)
    
    if not citation_authors or not ref_authors:
        return {"mismatch": False}
    
    # Check for exact match
    if citation_authors == ref_authors:
        return {"mismatch": False}
    
    # Check for partial match (missing part like -Koduah)
    for i, ca in enumerate(citation_authors):
        if i < len(ref_authors):
            # Check if citation author is contained in reference author
            if ca.lower() in ref_authors[i].lower() or ref_authors[i].lower() in ca.lower():
                if ca != ref_authors[i]:
                    return {
                        "mismatch": True,
                        "type": "partial_match",
                        "citation_authors": citation_authors,
                        "ref_authors": ref_authors,
                        "suggested_authors": ref_authors
                    }
    
    # Check for order mismatch (same authors but different order)
    if sorted(citation_authors) == sorted(ref_authors) and citation_authors != ref_authors:
        return {
            "mismatch": True,
            "type": "order_mismatch",
            "citation_authors": citation_authors,
            "ref_authors": ref_authors,
            "suggested_authors": ref_authors
        }
    
    # Check for spelling errors using similarity
    for i, ca in enumerate(citation_authors):
        if i < len(ref_authors):
            ratio = similarity_ratio(ca, ref_authors[i])
            if 0.8 <= ratio < 1.0:
                return {
                    "mismatch": True,
                    "type": "spelling_error",
                    "citation_authors": citation_authors,
                    "ref_authors": ref_authors,
                    "suggested_authors": ref_authors
                }
    
    return {"mismatch": False}


def detect_et_al_misuse(citation, reference, style="apa"):
    """Detect if 'et al.' is used when full authors should be listed."""
    citation_authors = extract_authors_from_citation(citation)
    ref_authors = extract_authors_from_reference(reference)
    
    # Check if citation uses "et al."
    if 'et al' in citation.lower() and len(ref_authors) <= 3 and style == "apa":
        # First citation should have all authors if 3 or fewer
        return {
            "misuse": True,
            "citation_authors": citation_authors,
            "ref_authors": ref_authors,
            "reason": "For first citation with 3 or fewer authors, list all authors instead of 'et al.'"
        }
    
    return {"misuse": False}


def build_citation_string(authors, year, citation_type="parenthetical"):
    """Build a citation string from authors and year."""
    if citation_type == "parenthetical":
        if len(authors) == 2:
            author_str = f"{authors[0]} & {authors[1]}"
        elif len(authors) > 2:
            author_str = f"{authors[0]} et al."
        else:
            author_str = authors[0]
        return f"({author_str}, {year})"
    else:  # narrative
        if len(authors) == 2:
            author_str = f"{authors[0]} and {authors[1]}"
        elif len(authors) > 2:
            author_str = f"{authors[0]} et al."
        else:
            author_str = authors[0]
        return f"{author_str} ({year})"


# ============================================================
# MAIN DETECTION FUNCTIONS FOR EACH SCENARIO
# ============================================================

def scenario_1_year_mismatch(c2r_rows):
    """Detect year mismatches (Scenario 1)."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if not in_text or not matched_ref:
            continue
        
        year_check = detect_year_mismatch(in_text, matched_ref)
        
        if year_check["mismatch"]:
            # Preserve original format
            suggested = in_text.replace(year_check["citation_year"], year_check["ref_year"])
            
            # Determine confidence based on year difference
            year_diff = abs(int(year_check["citation_year"]) - int(year_check["ref_year"]))
            if year_diff == 1:
                confidence = 0.95
            elif year_diff <= 3:
                confidence = 0.90
            else:
                confidence = 0.80
            
            suggestions.append({
                "original": in_text,
                "suggested": suggested,
                "confidence": confidence,
                "issue_type": "year_mismatch",
                "reason": f"Year mismatch: '{year_check['citation_year']}' should be '{year_check['ref_year']}' to match reference",
                "fix_type": "required_fix",
                "category": "year"
            })
    
    return suggestions


def scenario_2_author_name_mismatch(c2r_rows):
    """Detect author name mismatches (spelling, missing parts like -Koduah)."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if not in_text or not matched_ref:
            continue
        
        author_check = detect_author_mismatch(in_text, matched_ref)
        
        if author_check["mismatch"]:
            # Determine citation type
            is_narrative = is_narrative_citation(in_text)
            citation_year = extract_year_from_text(in_text)
            
            # Build corrected citation
            suggested = build_citation_string(
                author_check["suggested_authors"], 
                citation_year, 
                "narrative" if is_narrative else "parenthetical"
            )
            
            # Set confidence based on mismatch type
            if author_check["type"] == "spelling_error":
                confidence = 0.90
                reason = f"Author name spelling error: '{author_check['citation_authors'][0]}' should be '{author_check['suggested_authors'][0]}'"
            elif author_check["type"] == "partial_match":
                confidence = 0.85
                reason = f"Author name incomplete: '{author_check['citation_authors'][0]}' should be '{author_check['suggested_authors'][0]}'"
            else:
                confidence = 0.75
                reason = f"Author name mismatch: Expected '{author_check['suggested_authors'][0]}'"
            
            suggestions.append({
                "original": in_text,
                "suggested": suggested,
                "confidence": confidence,
                "issue_type": "author_mismatch",
                "reason": reason,
                "fix_type": "required_fix" if confidence >= 0.85 else "review_required",
                "category": "author",
                "mismatch_type": author_check["type"]
            })
    
    return suggestions


def scenario_3_author_order_mismatch(c2r_rows):
    """Detect author order mismatches (Scenario 3)."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if not in_text or not matched_ref:
            continue
        
        citation_authors = extract_authors_from_citation(in_text)
        ref_authors = extract_authors_from_reference(matched_ref)
        
        # Check if same authors but different order
        if (citation_authors and ref_authors and 
            sorted(citation_authors) == sorted(ref_authors) and 
            citation_authors != ref_authors):
            
            is_narrative = is_narrative_citation(in_text)
            citation_year = extract_year_from_text(in_text)
            
            suggested = build_citation_string(
                ref_authors, 
                citation_year, 
                "narrative" if is_narrative else "parenthetical"
            )
            
            suggestions.append({
                "original": in_text,
                "suggested": suggested,
                "confidence": 0.80,
                "issue_type": "author_order_mismatch",
                "reason": f"Author order mismatch: Expected '{ref_authors[0]} and {ref_authors[1]}' based on reference",
                "fix_type": "review_required",
                "category": "author_order"
            })
    
    return suggestions


def scenario_4_combined_mismatch(c2r_rows):
    """Detect combined author and year mismatches (Scenario 4)."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if not in_text or not matched_ref:
            continue
        
        year_check = detect_year_mismatch(in_text, matched_ref)
        author_check = detect_author_mismatch(in_text, matched_ref)
        
        if year_check["mismatch"] and author_check["mismatch"]:
            is_narrative = is_narrative_citation(in_text)
            citation_year = extract_year_from_text(in_text)
            
            # Build corrected citation with both fixes
            suggested_author = build_citation_string(
                author_check["suggested_authors"], 
                citation_year, 
                "narrative" if is_narrative else "parenthetical"
            )
            suggested = suggested_author.replace(citation_year, year_check["ref_year"])
            
            confidence = 0.85
            
            suggestions.append({
                "original": in_text,
                "suggested": suggested,
                "confidence": confidence,
                "issue_type": "author_year_mismatch",
                "reason": f"Both author name and year differ from reference: Author '{author_check['citation_authors'][0]}' should be '{author_check['suggested_authors'][0]}', Year '{year_check['citation_year']}' should be '{year_check['ref_year']}'",
                "fix_type": "required_fix",
                "category": "combined"
            })
    
    return suggestions


def scenario_5_et_al_misuse(c2r_rows, style="apa"):
    """Detect et al. misuse (Scenario 5)."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        
        if not in_text or not matched_ref:
            continue
        
        et_al_check = detect_et_al_misuse(in_text, matched_ref, style)
        
        if et_al_check["misuse"]:
            is_narrative = is_narrative_citation(in_text)
            citation_year = extract_year_from_text(in_text)
            
            suggested = build_citation_string(
                et_al_check["ref_authors"], 
                citation_year, 
                "narrative" if is_narrative else "parenthetical"
            )
            
            suggestions.append({
                "original": in_text,
                "suggested": suggested,
                "confidence": 0.75,
                "issue_type": "et_al_misuse",
                "reason": et_al_check["reason"],
                "fix_type": "review_required",
                "category": "style"
            })
    
    return suggestions


def scenario_6_potential_wrong_reference(c2r_rows):
    """Detect potential wrong reference matches (Scenario 6 - Low confidence)."""
    suggestions = []
    
    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        status = row.get("status", "")
        
        if not in_text or not matched_ref:
            continue
        
        # Check for potential wrong reference (status might be "likely" or "needs_review")
        if status in ["likely", "needs_review"]:
            citation_authors = extract_authors_from_citation(in_text)
            ref_authors = extract_authors_from_reference(matched_ref)
            citation_year = extract_year_from_text(in_text)
            ref_year = extract_year_from_text(matched_ref)
            
            # If authors match but years differ significantly
            if citation_authors and ref_authors and citation_authors[0] == ref_authors[0]:
                if citation_year and ref_year and citation_year != ref_year:
                    year_diff = abs(int(citation_year) - int(ref_year))
                    if year_diff > 3:
                        suggestions.append({
                            "original": in_text,
                            "suggested": "Verify reference - Year mismatch suggests different paper",
                            "confidence": 0.60,
                            "issue_type": "potential_wrong_reference",
                            "reason": f"No exact match found. Closest reference has different year ({ref_year} vs {citation_year}). Verify this is the correct source.",
                            "fix_type": "review_required",
                            "category": "verification"
                        })
    
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
        
        # COLLECT ALL SUGGESTIONS BY SCENARIO
        all_suggestions = []
        
        # Scenario 1: Year mismatch
        year_suggestions = scenario_1_year_mismatch(c2r_rows)
        all_suggestions.extend(year_suggestions)
        print(f"📅 Scenario 1 - Year mismatches: {len(year_suggestions)}")
        
        # Scenario 2: Author name mismatch (spelling, missing parts)
        author_suggestions = scenario_2_author_name_mismatch(c2r_rows)
        all_suggestions.extend(author_suggestions)
        print(f"👤 Scenario 2 - Author name mismatches: {len(author_suggestions)}")
        
        # Scenario 3: Author order mismatch
        order_suggestions = scenario_3_author_order_mismatch(c2r_rows)
        all_suggestions.extend(order_suggestions)
        print(f"🔄 Scenario 3 - Author order mismatches: {len(order_suggestions)}")
        
        # Scenario 4: Combined author + year mismatch
        combined_suggestions = scenario_4_combined_mismatch(c2r_rows)
        all_suggestions.extend(combined_suggestions)
        print(f"🔀 Scenario 4 - Combined mismatches: {len(combined_suggestions)}")
        
        # Scenario 5: Et al. misuse
        et_al_suggestions = scenario_5_et_al_misuse(c2r_rows, style)
        all_suggestions.extend(et_al_suggestions)
        print(f"📝 Scenario 5 - Et al. misuse: {len(et_al_suggestions)}")
        
        # Scenario 6: Potential wrong reference
        wrong_ref_suggestions = scenario_6_potential_wrong_reference(c2r_rows)
        all_suggestions.extend(wrong_ref_suggestions)
        print(f"⚠️ Scenario 6 - Potential wrong references: {len(wrong_ref_suggestions)}")
        
        # Reference formatting issues
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
        
        # Print details of each suggestion for debugging
        for i, s in enumerate(unique_suggestions):
            print(f"  Suggestion {i+1}: [{s.get('issue_type')}] {s.get('original')} -> {s.get('suggested')}")
        
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
        print("📋 Detection scenarios enabled:")
        print("   Scenario 1: Year mismatches")
        print("   Scenario 2: Author name mismatches (spelling, missing parts)")
        print("   Scenario 3: Author order mismatches")
        print("   Scenario 4: Combined author + year mismatches")
        print("   Scenario 5: Et al. misuse")
        print("   Scenario 6: Potential wrong references")
        print("   Reference formatting issues")
        worker.work(burst=False)
