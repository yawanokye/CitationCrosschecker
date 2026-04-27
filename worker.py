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
from engine import run_crosscheck_with_autofix

try:
    from citation_suggester import suggest_for_unverified
except Exception:
    suggest_for_unverified = None
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
    """Extract year from citation or reference text, including suffixes like 2020a."""
    if not text:
        return None

    match = re.search(r'\b((?:19|20)\d{2}[a-z]?)\b', str(text), flags=re.I)
    return match.group(1) if match else None



def extract_authors_from_citation(citation):
    """
    Extract author surnames from in-text citation.
    Handles:
    (Adam, 2020)
    (Adam & Mensah, 2020)
    Adam and Mensah (2020)
    Adam et al. (2020)
    (World Bank, 2020)
    """
    if not citation:
        return []

    text = str(citation).strip()

    # Remove year and brackets
    text = re.sub(r'\b(?:19|20)\d{2}[a-z]?\b', '', text, flags=re.I)
    text = re.sub(r'[()]', ' ', text)
    text = re.sub(r'\bet\s+al\.?\b', '', text, flags=re.I)
    text = text.replace('&', ' and ')
    text = re.sub(r'\s+', ' ', text).strip(" ,;.")

    if not text:
        return []

    parts = re.split(r'\s+and\s+|;', text, flags=re.I)

    authors = []
    for part in parts:
        part = part.strip(" ,.")
        if not part:
            continue

        # For names such as "van der Merwe", keep last token as surname fallback
        tokens = part.split()
        if len(tokens) >= 2 and tokens[0].lower() in {"van", "von", "de", "da", "di", "der", "al"}:
            surname = " ".join(tokens)
        else:
            surname = tokens[-1]

        surname = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ'\- ]", "", surname).strip()
        if len(surname) >= 2:
            authors.append(surname)

    # Remove duplicates while preserving order
    seen = set()
    clean = []
    for a in authors:
        key = a.lower()
        if key not in seen:
            seen.add(key)
            clean.append(a)

    return clean


def extract_authors_from_reference(ref_text):
    """
    Extract author surnames from reference list entry.
    Handles:
    Adam, A. M., Mensah, K., & Boateng, E. (2021).
    Adam, A. M., & Mensah, K. (2021).
    World Bank. (2020).
    """
    if not ref_text:
        return []

    text = str(ref_text).strip()

    year_match = re.search(r'\b(?:19|20)\d{2}[a-z]?\b', text, flags=re.I)
    if not year_match:
        return []

    author_part = text[:year_match.start()].strip()
    author_part = re.sub(r'\s+', ' ', author_part)
    author_part = author_part.replace('&', ',')

    # Remove brackets and trailing punctuation
    author_part = author_part.strip(" .,")

    # APA personal author pattern: Surname, Initials
    surnames = re.findall(
        r'\b([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ\'\-]+)\s*,\s*(?:[A-Z]\.?\s*)+',
        author_part
    )

    if surnames:
        seen = set()
        clean = []
        for s in surnames:
            key = s.lower()
            if key not in seen:
                seen.add(key)
                clean.append(s)
        return clean

    # Organisational author fallback
    org = author_part.strip(" .,")
    if org and len(org.split()) <= 8:
        return [org]

    return []


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
    """
    Detect misuse of et al.
    APA 7:
    - One author: use one author.
    - Two authors: cite both authors.
    - Three or more authors: use first author et al.
    """
    if not citation or not reference:
        return {"misuse": False}

    citation_uses_et_al = bool(re.search(r'\bet\s+al\.?\b', citation, flags=re.I))
    if not citation_uses_et_al:
        return {"misuse": False}

    ref_authors = extract_authors_from_reference(reference)

    if style.lower() == "apa" and len(ref_authors) <= 2:
        return {
            "misuse": True,
            "citation_authors": extract_authors_from_citation(citation),
            "ref_authors": ref_authors,
            "reason": "APA 7 uses 'et al.' for three or more authors. For one or two authors, list the author names."
        }

    return {"misuse": False}


def build_citation_string(authors, year, citation_type="parenthetical", style="apa"):
    """Build a corrected in-text citation string."""
    authors = authors or []
    year = year or ""

    if not authors or not year:
        return ""

    if style.lower() == "apa":
        if len(authors) == 1:
            author_str_parenthetical = authors[0]
            author_str_narrative = authors[0]
        elif len(authors) == 2:
            author_str_parenthetical = f"{authors[0]} & {authors[1]}"
            author_str_narrative = f"{authors[0]} and {authors[1]}"
        else:
            author_str_parenthetical = f"{authors[0]} et al."
            author_str_narrative = f"{authors[0]} et al."
    else:
        if len(authors) == 1:
            author_str_parenthetical = authors[0]
            author_str_narrative = authors[0]
        elif len(authors) == 2:
            author_str_parenthetical = f"{authors[0]} & {authors[1]}"
            author_str_narrative = f"{authors[0]} and {authors[1]}"
        else:
            author_str_parenthetical = f"{authors[0]} et al."
            author_str_narrative = f"{authors[0]} et al."

    if citation_type == "parenthetical":
        return f"({author_str_parenthetical}, {year})"

    return f"{author_str_narrative} ({year})"


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
            # Build corrected citation
            suggested = build_citation_string(
                author_check["suggested_authors"],
                citation_year,
                "narrative" if is_narrative else "parenthetical",
                style
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
                "narrative" if is_narrative else "parenthetical",
                style
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


def scenario_4_combined_mismatch(c2r_rows, style="apa"):
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
                "narrative" if is_narrative else "parenthetical",
                style
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


def detect_reference_quality_issues(references_raw, style="apa", enable_online_suggestions=False):
    """
    Detect reference-quality issues:
    - DOI format issue
    - Missing DOI
    - Incomplete reference
    - Missing journal details
    - HTTP to HTTPS
    - Missing final period
    """
    suggestions = []

    for ref in references_raw:
        original = ref or ""
        ref = original.strip()

        if not ref:
            continue

        year = extract_year_from_text(ref)

        # DOI detection
        doi_match = re.search(r'\b10\.\d{4,9}/[^\s\)]+' , ref, flags=re.I)
        has_doi_url = bool(re.search(r'https?://doi\.org/10\.\d{4,9}/[^\s\)]+' , ref, flags=re.I))
        has_doi_label = bool(re.search(r'\bdoi\s*:\s*10\.\d{4,9}/[^\s\)]+' , ref, flags=re.I))

        # 1. DOI exists but is not in APA URL format
        if doi_match and style.lower() == "apa" and not has_doi_url:
            doi = doi_match.group(0).rstrip(".,")
            suggested_ref = re.sub(r'\bdoi\s*:\s*', '', ref, flags=re.I)
            suggested_ref = suggested_ref.replace(doi, f"https://doi.org/{doi}")

            suggestions.append({
                "original": original,
                "suggested": suggested_ref,
                "confidence": 0.95,
                "issue_type": "doi_format",
                "reason": "APA 7 recommends DOI in URL format, for example https://doi.org/xxxxx.",
                "fix_type": "suggested_fix",
                "category": "reference_quality",
                "field": "doi"
            })

        # 2. HTTP to HTTPS
        if "http://" in ref:
            suggestions.append({
                "original": original,
                "suggested": ref.replace("http://", "https://"),
                "confidence": 0.90,
                "issue_type": "http_to_https",
                "reason": "Use HTTPS for stable and secure links.",
                "fix_type": "suggested_fix",
                "category": "reference_quality",
                "field": "url"
            })

        # 3. Incomplete reference check
        has_title_like_text = bool(year and len(ref[ref.find(str(year)) + len(str(year)):].strip()) > 20)
        has_journal_markers = bool(re.search(
            r'\b(journal|review|proceedings|conference|press|publisher|international|volume|vol\.|no\.|\d+\s*\(\d+\)|pp\.)\b',
            ref,
            flags=re.I
        ))
        has_pages = bool(re.search(r'\b\d{1,4}\s*[-–]\s*\d{1,4}\b', ref))
        has_volume_issue = bool(re.search(r'\b\d+\s*\(\d+\)', ref))

        if not year or not has_title_like_text:
            suggestions.append({
                "original": original,
                "suggested": "Review reference manually",
                "confidence": 0.80,
                "issue_type": "incomplete_reference",
                "reason": "The reference appears to be missing a year or a clear title.",
                "fix_type": "review_required",
                "category": "reference_quality",
                "field": "bibliographic_details"
            })

        elif not has_journal_markers and not has_doi_url and not has_doi_label:
            suggestions.append({
                "original": original,
                "suggested": "Add source title or publisher/journal details",
                "confidence": 0.75,
                "issue_type": "missing_journal_details",
                "reason": "The reference may be missing journal, publisher, conference, or source details.",
                "fix_type": "review_required",
                "category": "reference_quality",
                "field": "source_title"
            })

        elif has_journal_markers and not has_pages and not has_doi_url and not has_doi_label:
            suggestions.append({
                "original": original,
                "suggested": "Add page range and DOI if available",
                "confidence": 0.70,
                "issue_type": "incomplete_reference",
                "reason": "The reference may be missing page range and DOI.",
                "fix_type": "review_required",
                "category": "reference_quality",
                "field": "pages_or_doi"
            })

        # 4. Missing DOI candidate from online suggestion
        if enable_online_suggestions and not doi_match and suggest_for_unverified:
            try:
                candidates = suggest_for_unverified(ref, top_k=1)
                if candidates:
                    cand = candidates[0]
                    cand_doi = cand.get("doi", "")
                    if cand_doi:
                        suggestions.append({
                            "original": original,
                            "suggested": f"https://doi.org/{cand_doi}",
                            "confidence": min(0.90, cand.get("score", 80) / 100),
                            "issue_type": "missing_doi",
                            "reason": "A DOI candidate was found from online metadata. Verify before applying.",
                            "fix_type": "review_required",
                            "category": "reference_quality",
                            "field": "doi",
                            "candidate_title": cand.get("title", ""),
                            "candidate_year": cand.get("year", ""),
                            "candidate_authors": cand.get("authors", [])
                        })
            except Exception as e:
                print(f"[WARN] DOI lookup failed for reference: {e}")

        # 5. Missing final period
        if ref and not ref.rstrip().endswith("."):
            suggestions.append({
                "original": original,
                "suggested": ref.rstrip() + ".",
                "confidence": 0.65,
                "issue_type": "missing_period",
                "reason": "The reference may need a final period depending on the selected style.",
                "fix_type": "optional_fix",
                "category": "reference_quality",
                "field": "punctuation"
            })

    return dedupe_suggestions_by_priority(suggestions)

def dedupe_suggestions_by_priority(suggestions):
    """
    Keep the strongest suggestion for each original text.
    Prevents combined author-year mismatch from being replaced by weaker year-only mismatch.
    """
    priority = {
        "author_year_mismatch": 1,
        "potential_wrong_reference": 2,
        "author_mismatch": 3,
        "year_mismatch": 4,
        "author_order_mismatch": 5,
        "et_al_misuse": 6,
        "missing_doi": 7,
        "doi_format": 8,
        "incomplete_reference": 9,
        "missing_journal_details": 10,
        "http_to_https": 11,
        "missing_period": 12,
    }

    best = {}

    for s in suggestions:
        original = s.get("original", "")
        if not original:
            continue

        issue_type = s.get("issue_type", "")
        new_rank = priority.get(issue_type, 99)
        new_conf = s.get("confidence", 0)

        if original not in best:
            best[original] = s
            continue

        old = best[original]
        old_rank = priority.get(old.get("issue_type", ""), 99)
        old_conf = old.get("confidence", 0)

        if new_rank < old_rank or (new_rank == old_rank and new_conf > old_conf):
            best[original] = s

    return list(best.values())
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
        
        # Scenario 4 first: Combined author + year mismatch
        combined_suggestions = scenario_4_combined_mismatch(c2r_rows, style)
        all_suggestions.extend(combined_suggestions)
        print(f"🔀 Scenario 4 - Combined mismatches: {len(combined_suggestions)}")
        
        # Scenario 6: Potential wrong reference
        wrong_ref_suggestions = scenario_6_potential_wrong_reference(c2r_rows)
        all_suggestions.extend(wrong_ref_suggestions)
        print(f"⚠️ Scenario 6 - Potential wrong references: {len(wrong_ref_suggestions)}")
        
        # Scenario 2: Author name mismatch (spelling, missing parts)
        author_suggestions = scenario_2_author_name_mismatch(c2r_rows, style)
        all_suggestions.extend(author_suggestions)
        print(f"👤 Scenario 2 - Author name mismatches: {len(author_suggestions)}")
        
        # Scenario 1: Year mismatch
        year_suggestions = scenario_1_year_mismatch(c2r_rows)
        all_suggestions.extend(year_suggestions)
        print(f"📅 Scenario 1 - Year mismatches: {len(year_suggestions)}")
        
        # Scenario 3: Author order mismatch
        order_suggestions = scenario_3_author_order_mismatch(c2r_rows, style)
        all_suggestions.extend(order_suggestions)
        print(f"🔄 Scenario 3 - Author order mismatches: {len(order_suggestions)}")
        
        # Scenario 5: Et al. misuse
        et_al_suggestions = scenario_5_et_al_misuse(c2r_rows, style)
        all_suggestions.extend(et_al_suggestions)
        print(f"📝 Scenario 5 - Et al. misuse: {len(et_al_suggestions)}")

# Reference quality issues
ref_suggestions = detect_reference_quality_issues(
    references_raw,
    style=style,
    enable_online_suggestions=False
)
all_suggestions.extend(ref_suggestions)
print(f"📚 Reference quality issues: {len(ref_suggestions)}")
        
        # Remove duplicates (by original text)
        unique_suggestions = dedupe_suggestions_by_priority(all_suggestions)
        
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
            citation_suggestions = [
                s for s in unique_suggestions
                if s.get("category") not in {"reference", "reference_quality"}
            ]
        
            reference_suggestions = [
                s for s in unique_suggestions
                if s.get("category") in {"reference", "reference_quality"}
            ]
        
            result["autofix"]["suggestions"]["citations"] = citation_suggestions
            result["autofix"]["suggestions"]["references"] = reference_suggestions
        
            result["suggestions"] = result["autofix"]["suggestions"]
            
            # Add statistics
            result["autofix"]["statistics"] = {
                "total": len(unique_suggestions),
                "citation_accuracy_total": len(citation_suggestions),
                "reference_quality_total": len(reference_suggestions),
                "high_confidence": len([s for s in unique_suggestions if s.get("confidence", 0) >= 0.85]),
                "medium_confidence": len([s for s in unique_suggestions if 0.70 <= s.get("confidence", 0) < 0.85]),
                "low_confidence": len([s for s in unique_suggestions if s.get("confidence", 0) < 0.70]),
                "by_category": dict(categories),
                "by_issue_type": dict(Counter(s.get("issue_type", "other") for s in unique_suggestions))
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
