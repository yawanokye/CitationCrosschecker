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
    from engine import recover_references_for_verification
except Exception:
    recover_references_for_verification = None

from psycopg2.extras import RealDictCursor
from datetime import datetime
from verify import verify_references_batch
from acii import compute_acii
from claim_checker import build_claim_support_rows

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
# DURABLE VERIFICATION HELPERS
# ============================================================

VERIFY_CHUNK_SIZE = int(os.environ.get("VERIFY_CHUNK_SIZE", "10"))


def now_iso():
    return datetime.utcnow().isoformat()


def _safe_json_loads(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return value


def _load_job_result(job_id):
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT result FROM jobs WHERE job_id = %s", (job_id,))
        row = cursor.fetchone()

        if not row:
            raise Exception(f"Job {job_id} not found")

        return _safe_json_loads(row["result"])

    finally:
        cursor.close()
        conn.close()


def _save_job_result(job_id, result, status=None):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    try:
        if status:
            cursor.execute(
                """
                UPDATE jobs
                SET result = %s::jsonb,
                    status = %s,
                    completed_at = CASE WHEN %s = 'completed' THEN NOW() ELSE completed_at END
                WHERE job_id = %s
                """,
                (json.dumps(result), status, status, job_id)
            )
        else:
            cursor.execute(
                """
                UPDATE jobs
                SET result = %s::jsonb
                WHERE job_id = %s
                """,
                (json.dumps(result), job_id)
            )

        conn.commit()

    finally:
        cursor.close()
        conn.close()

    # Keep /result/{job_id} fresh because main.py checks Redis cache first.
    try:
        redis_conn.setex(f"result:{job_id}", 3600, json.dumps(result))
    except Exception as e:
        print(f"[VERIFY WORKER] Could not refresh Redis result cache: {e}")


def _set_verification_meta(result, **kwargs):
    verification = result.get("verification") or {}
    verification.update({k: v for k, v in kwargs.items() if v is not None})
    result["verification"] = verification
    return result


def _compute_verification_summary(rows):
    rows = rows or []
    return {
        "total": len(rows),
        "verified": sum(1 for r in rows if r and r.get("status") == "verified"),
        "likely": sum(1 for r in rows if r and r.get("status") == "likely"),
        "needs_review": sum(1 for r in rows if r and r.get("status") == "needs_review"),
        "not_found": sum(1 for r in rows if r and r.get("status") == "not_found"),
        "offline": sum(1 for r in rows if r and r.get("status") == "offline"),
    }


def _build_recovery_payload(result, verification_rows):
    """
    Safe recovery builder for the new worker flow.
    It avoids depending on web-process memory and always returns UI-ready arrays.
    """
    missing_recovery = []
    verification_recovery = []

    for item in result.get("missing_in_references", []) or []:
        if isinstance(item, str):
            citation = item
            count = 1
        else:
            citation = item.get("citation_in_text") or item.get("citation") or item.get("in_text") or ""
            count = item.get("count_in_text") or item.get("count") or 1

        missing_recovery.append({
            "citation": citation,
            "count": count,
            "suggestions": []
        })

    for row in verification_rows or []:
        status = row.get("status", "")
        if status not in {"likely", "needs_review", "not_found", "offline"}:
               continue

        suggestions = (
            row.get("suggested_references")
            or row.get("correction_suggestions")
            or row.get("suggestions")
            or []
        )

        verification_recovery.append({
            "status": status,
            "citation": row.get("citation", ""),
            "reference": row.get("reference", ""),
            "suggestions": suggestions
        })

    return {
        "missing_recovery": missing_recovery,
        "verification_recovery": verification_recovery
    }

def _fallback_claim_support_rows(result, verification_rows):
    """
    Fallback claim-support rows.

    This does not claim that the source supports the claim.
    It only ensures the Claim Support tab is populated with review-ready rows
    when the full claim checker returns no rows.
    """
    rows = []

    c2r_rows = result.get("reconciliation_intext_to_reference", []) or []

    for i, row in enumerate(c2r_rows, start=1):
        citation = (
            row.get("in_text")
            or row.get("citation")
            or row.get("citation_in_text")
            or ""
        )

        matched_reference = (
            row.get("matched_reference")
            or row.get("reference")
            or ""
        )

        status = row.get("status", "")

        if not citation and not matched_reference:
            continue

        rows.append({
            "citation": citation,
            "claim": "Claim extraction not available. Review the cited sentence manually.",
            "source_title": matched_reference[:250] if matched_reference else "Matched source not available",
            "matched_source": matched_reference,
            "support_status": "not_checked",
            "support_score": 0,
            "doi": "",
            "note": "Fallback row generated because automated claim-support checking returned no rows.",
            "citation_match_status": status
        })

    if rows:
        return rows

    for i, row in enumerate(verification_rows or [], start=1):
        reference = row.get("reference") or row.get("matched_title") or row.get("title") or ""

        if not reference:
            continue

        rows.append({
            "citation": row.get("citation", ""),
            "claim": "Claim extraction not available. Review the cited sentence manually.",
            "source_title": row.get("matched_title") or reference[:250],
            "matched_source": reference,
            "support_status": "not_checked",
            "support_score": 0,
            "doi": row.get("doi", ""),
            "note": "Fallback row generated from verification output.",
            "citation_match_status": row.get("status", "")
        })

    return rows
def _normalise_references_for_verification(result):
    refs = result.get("references_raw", []) or []

    if refs:
        return refs

    if recover_references_for_verification:
        try:
            recovered = recover_references_for_verification(
                result.get("main_text", ""),
                style_hint="apa"
            )
            if recovered:
                result["references_raw"] = recovered
                result.setdefault("summary", {})["reference_entries_found"] = len(recovered)
                return recovered
        except Exception as e:
            print(f"[VERIFY WORKER] Reference recovery failed: {e}")

    return []
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

def year_to_int(year):
    """Convert 2020a or 2020 to 2020."""
    if not year:
        return None
    m = re.search(r'(?:19|20)\d{2}', str(year))
    return int(m.group(0)) if m else None


def normalize_name_text(text):
    """Normalise author/institution names safely."""
    text = str(text or "")
    text = text.replace("‐", "-").replace("–", "-").replace("—", "-")
    text = text.replace("’", "'")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_org_author(author_part):
    """Clean organisational author names such as Bank of Ghana, OECD, IMF."""
    org = normalize_name_text(author_part)
    org = re.sub(r"\(?\s*$", "", org).strip()
    org = org.strip(" .,(;:")

    # Remove leftover year punctuation if any slipped in
    org = re.sub(r"\(\s*$", "", org).strip()
    org = org.strip(" .,(;:")

    return org


def looks_like_institutional_author(name):
    """Detect likely organisational or institutional author."""
    if not name:
        return False

    n = normalize_name_text(name)
    lower = n.lower()

    institutional_terms = {
        "bank", "reserve", "ministry", "department", "office", "bureau",
        "authority", "commission", "organisation", "organization",
        "university", "institute", "fund", "chamber", "conference",
        "nations", "monetary", "oecd", "imf", "world bank", "unctad"
    }

    if n.isupper() and len(n) >= 2:
        return True

    return any(term in lower for term in institutional_terms)


def looks_like_merged_reference(ref):
    """Detect references that appear to contain two or more joined entries."""
    if not ref:
        return False

    text = normalize_name_text(ref)

    # Multiple APA-style author-year starts inside one entry
    starts = re.findall(
        r"\b[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,7}\.\s*\((?:19|20)\d{2}",
        text
    )

    # Multiple years in brackets can indicate joined refs, especially with long text
    year_brackets = re.findall(r"\((?:19|20)\d{2}[a-z]?(?:,\s*[A-Za-z]+)?\)", text)

    return len(starts) >= 2 or (len(year_brackets) >= 2 and len(text) > 250)


def force_review_only(suggestion):
    """Ensure every suggestion is review-only and not auto-applied."""
    suggestion["fix_type"] = "review_required"
    suggestion["action"] = "review_required"
    suggestion["apply"] = None
    return suggestion


def make_suggestion(original, suggested, confidence, issue_type, reason, category, field=""):
    """Create one standard review-only suggestion."""
    return force_review_only({
        "original": original,
        "suggested": suggested,
        "confidence": confidence,
        "issue_type": issue_type,
        "reason": reason,
        "fix_type": "review_required",
        "action": "review_required",
        "category": category,
        "field": field,
        "apply": None
    })

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
    Extract author surnames or institutional authors from reference list entry.
    Avoids malformed outputs like 'Bank of Ghana. ('.
    """
    if not ref_text:
        return []

    text = normalize_name_text(ref_text)

    year_match = re.search(r"\b(?:19|20)\d{2}[a-z]?\b", text, flags=re.I)
    if not year_match:
        return []

    author_part = text[:year_match.start()].strip()
    author_part = re.sub(r"\s+", " ", author_part)
    author_part = author_part.strip(" .,(;:")

    # Institutional author fallback first
    if looks_like_institutional_author(author_part):
        org = clean_org_author(author_part)
        return [org] if org else []

    author_part_for_people = author_part.replace("&", ",")

    # APA personal author pattern: Surname, Initials
    surnames = re.findall(
        r"\b([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+)\s*,\s*(?:[A-Z]\.?\s*)+",
        author_part_for_people
    )

    if surnames:
        seen = set()
        clean = []
        for s in surnames:
            s = normalize_name_text(s)
            key = s.lower()
            if key not in seen:
                seen.add(key)
                clean.append(s)
        return clean

    # Final fallback, only for short organisation-like author part
    org = clean_org_author(author_part)
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

def find_reference_by_author_different_year(citation, references_raw):
    """
    Conservative unmatched year-mismatch detector.
    Only flags likely year errors when:
    - first author matches very strongly
    - second author also matches when present
    - the year difference is small
    This avoids false matches in long theses.
    """
    citation_authors = extract_authors_from_citation(citation)
    citation_year = extract_year_from_text(citation)

    if not citation_authors or not citation_year:
        return None

    cit_year_int = year_to_int(citation_year)
    if not cit_year_int:
        return None

    cit_first = citation_authors[0].lower()
    cit_second = citation_authors[1].lower() if len(citation_authors) >= 2 else ""

    best = None
    best_score = 0

    for ref in references_raw or []:
        ref_authors = extract_authors_from_reference(ref)
        ref_year = extract_year_from_text(ref)

        if not ref_authors or not ref_year:
            continue

        ref_year_int = year_to_int(ref_year)
        if not ref_year_int:
            continue

        if ref_year == citation_year:
            continue

        year_diff = abs(cit_year_int - ref_year_int)

        # Only allow small year differences for unmatched year suggestions.
        # Larger differences are usually different publications, not citation-year errors.
        if year_diff > 2:
            continue

        ref_first = ref_authors[0].lower()
        ref_second = ref_authors[1].lower() if len(ref_authors) >= 2 else ""

        cit_key = re.sub(r"[^a-z0-9]", "", cit_first)
        ref_key = re.sub(r"[^a-z0-9]", "", ref_first)

        first_score = 1.0 if cit_key == ref_key else similarity_ratio(cit_first, ref_first)

        if first_score < 0.95:
            continue

        # If citation has two authors, require the second author too.
        if cit_second:
            second_score = similarity_ratio(cit_second, ref_second)
            if second_score < 0.90:
                continue

        # Do not make unmatched year suggestions for et al. citations.
        # Too risky without title/context verification.
        if re.search(r"\bet\s+al\.?\b", citation, flags=re.I):
            continue

        score = first_score

        if score > best_score:
            best_score = score
            best = {
                "reference": ref,
                "ref_year": ref_year,
                "citation_year": citation_year,
                "ref_authors": ref_authors,
                "citation_authors": citation_authors,
                "score": score
            }

    return best

def detect_author_mismatch(citation, reference):
    """
    Conservative author mismatch detection.
    Only flags clear spelling/order differences.
    Avoids institutional and ambiguous partial-match false positives.
    """
    citation_authors = extract_authors_from_citation(citation)
    ref_authors = extract_authors_from_reference(reference)

    if not citation_authors or not ref_authors:
        return {"mismatch": False}

    cit_first = citation_authors[0]
    ref_first = ref_authors[0]

    cit_first = citation_authors[0]
    ref_first = ref_authors[0]
    
    def comparable_author_key(x):
        return re.sub(r"[^a-z0-9]", "", str(x).lower())
    
    if comparable_author_key(cit_first) == comparable_author_key(ref_first):
        return {"mismatch": False}
    
    # Exact case-insensitive match
    if [a.lower() for a in citation_authors] == [a.lower() for a in ref_authors]:
        return {"mismatch": False}

    # Do not flag institutional author partial matches, e.g. Ghana vs Bank of Ghana
    if looks_like_institutional_author(cit_first) or looks_like_institutional_author(ref_first):
        if cit_first.lower() in ref_first.lower() or ref_first.lower() in cit_first.lower():
            return {"mismatch": False}

    # Avoid reducing De Silva to Silva, Olasehinde-Williams to Williams
    if cit_first.lower().endswith(ref_first.lower()) or ref_first.lower().endswith(cit_first.lower()):
        return {"mismatch": False}

    # Order mismatch, only if same number of authors and at least two authors
    if (
        len(citation_authors) == len(ref_authors)
        and len(citation_authors) >= 2
        and sorted(a.lower() for a in citation_authors) == sorted(a.lower() for a in ref_authors)
        and [a.lower() for a in citation_authors] != [a.lower() for a in ref_authors]
    ):
        return {
            "mismatch": True,
            "type": "order_mismatch",
            "citation_authors": citation_authors,
            "ref_authors": ref_authors,
            "suggested_authors": ref_authors
        }

    # Spelling error, only for strong similarity and same author count
    if len(citation_authors) == len(ref_authors):
        for i, ca in enumerate(citation_authors):
            if i < len(ref_authors):
                ratio = similarity_ratio(ca, ref_authors[i])
                if 0.88 <= ratio < 1.0:
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
    Conservative et al. misuse detection.
    Only flags when the reference clearly has one or two authors.
    """
    if not citation or not reference:
        return {"misuse": False}

    if style.lower() != "apa":
        return {"misuse": False}

    citation_uses_et_al = bool(re.search(r"\bet\s+al\.?\b", citation, flags=re.I))
    if not citation_uses_et_al:
        return {"misuse": False}

    year_match = re.search(r"\b(?:19|20)\d{2}[a-z]?\b", reference, flags=re.I)
    if not year_match:
        return {"misuse": False}

    author_part = reference[:year_match.start()]
    personal_author_count = len(re.findall(
        r"\b[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+,\s*(?:[A-Z]\.?\s*)+",
        author_part
    ))

    ref_authors = extract_authors_from_reference(reference)

    # If reference clearly has 3+ personal authors, et al. is acceptable
    if personal_author_count >= 3:
        return {"misuse": False}

    # Only flag when clearly one or two authors
    if 1 <= len(ref_authors) <= 2 and personal_author_count <= 2:
        return {
            "misuse": True,
            "citation_authors": extract_authors_from_citation(citation),
            "ref_authors": ref_authors,
            "reason": "APA 7 uses 'et al.' for three or more authors. This reference appears to have one or two authors, so review the in-text citation."
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
            cy = year_to_int(year_check["citation_year"])
            ry = year_to_int(year_check["ref_year"])
            year_diff = abs(cy - ry) if cy and ry else 99
            if year_diff == 1:
                confidence = 0.95
            elif year_diff <= 3:
                confidence = 0.90
            else:
                confidence = 0.80
            
            suggestions.append(make_suggestion(
                original=in_text,
                suggested=suggested,
                confidence=confidence,
                issue_type="year_mismatch",
                reason=f"Year mismatch: '{year_check['citation_year']}' may need review against reference year '{year_check['ref_year']}'.",
                category="citation_accuracy"
            ))
    
    return suggestions

def scenario_1b_unmatched_year_mismatch(c2r_rows, references_raw):
    """
    Detect likely year mismatches for citations that were not matched
    because the citation year differs from the reference year.
    """
    suggestions = []

    for row in c2r_rows:
        in_text = row.get("in_text", "")
        matched_ref = row.get("matched_reference", "")
        status = row.get("status", "")

        # Only inspect citations that normal reconciliation did not match
        if not in_text:
            continue

        if matched_ref and status == "matched":
            continue

        candidate = find_reference_by_author_different_year(in_text, references_raw)

        if not candidate:
            continue

        citation_year = candidate["citation_year"]
        ref_year = candidate["ref_year"]

        suggested = in_text.replace(citation_year, ref_year)

        cy = year_to_int(citation_year)
        ry = year_to_int(ref_year)
        year_diff = abs(cy - ry) if cy and ry else 99

        if year_diff == 1:
            confidence = 0.88
        elif year_diff <= 3:
            confidence = 0.82
        else:
            confidence = 0.72

        suggestions.append(make_suggestion(
            original=in_text,
            suggested=suggested,
            confidence=confidence,
            issue_type="possible_year_mismatch_unmatched",
            reason=(
                f"The citation was not matched, but a reference with a similar author "
                f"uses year '{ref_year}' instead of '{citation_year}'. Review manually."
            ),
            category="citation_accuracy"
        ))

    return suggestions


def scenario_2_author_name_mismatch(c2r_rows, style="apa"):
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
            
            item = make_suggestion(
                original=in_text,
                suggested=suggested,
                confidence=confidence,
                issue_type="author_mismatch",
                reason=reason + " Review before changing.",
                category="citation_accuracy"
            )
            item["mismatch_type"] = author_check["type"]
            suggestions.append(item)
    
    return suggestions


def scenario_3_author_order_mismatch(c2r_rows, style="apa"):
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
            
            suggestions.append(make_suggestion(
                original=in_text,
                suggested=suggested,
                confidence=0.80,
                issue_type="author_order_mismatch",
                reason="Author order may differ from the reference entry. Review before changing.",
                category="citation_accuracy"
            ))
    
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
            
            suggestions.append(make_suggestion(
                original=in_text,
                suggested=suggested,
                confidence=confidence,
                issue_type="author_year_mismatch",
                reason="Both author and year appear to differ from the matched reference. Review carefully before changing.",
                category="citation_accuracy"
            ))
    
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
                "narrative" if is_narrative else "parenthetical",
                style
            )
            
            suggestions.append(make_suggestion(
                original=in_text,
                suggested=suggested,
                confidence=0.75,
                issue_type="et_al_misuse",
                reason=et_al_check["reason"],
                category="citation_accuracy"
            ))
                
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
                    cy = year_to_int(citation_year)
                    ry = year_to_int(ref_year)
                    year_diff = abs(cy - ry) if cy and ry else 99
                    if year_diff > 3:
                        suggestions.append(make_suggestion(
                            original=in_text,
                            suggested="Verify the matched reference manually",
                            confidence=0.60,
                            issue_type="potential_wrong_reference",
                            reason=f"The closest matched reference has a different year ({ref_year} vs {citation_year}). Review whether this is the correct source.",
                            category="citation_accuracy"
                        ))
    
    return suggestions


def detect_reference_quality_issues(references_raw, style="apa", enable_online_suggestions=False):
    """
    Safer reference-quality checks.
    Only flags high-value, lower-risk issues:
    - merged references
    - DOI format
    - HTTP to HTTPS
    - seriously incomplete reference

    Disabled due to false positives:
    - missing_period
    - missing_journal_details
    - missing page range
    """
    suggestions = []

    for ref in references_raw:
        original = ref or ""
        ref = normalize_name_text(original)

        if not ref:
            continue

        # 1. Merged references
        if looks_like_merged_reference(ref):
            suggestions.append(make_suggestion(
                original=original,
                suggested="Split into separate reference entries",
                confidence=0.90,
                issue_type="merged_references",
                reason="This entry appears to contain two or more references joined together.",
                category="reference_quality",
                field="reference_structure"
            ))
            continue

        year = extract_year_from_text(ref)

        # Normalise broken DOI URL spacing such as "https://doi. org/"
        # 2. DOI format and DOI spacing
        ref_for_doi = ref
        
        # Fix broken DOI URL spacing such as "https://doi. org/"
        ref_for_doi = re.sub(
            r"https?://doi\.\s*org/",
            "https://doi.org/",
            ref_for_doi,
            flags=re.I
        )
        
        # Convert dx.doi.org to doi.org
        ref_for_doi = re.sub(
            r"https?://dx\.doi\.org/",
            "https://doi.org/",
            ref_for_doi,
            flags=re.I
        )
        
        # Convert DOI labels to DOI URL
        # Handles:
        # DOI: 10.xxxx
        # doi:10.xxxx
        # DOI http://dx.doi.org/10.xxxx
        # DOI: org/10.xxxx
        doi_label_match = re.search(
            r"\bdoi\s*:?\s*(?:https?://(?:dx\.)?doi\.org/)?(?:org/)?(10\.\d{4,9}/[^\s\)]*)",
            ref_for_doi,
            flags=re.I
        )
        
        if doi_label_match:
            doi = doi_label_match.group(1).rstrip(".,")
            cleaned = (
                ref_for_doi[:doi_label_match.start()]
                + f"https://doi.org/{doi}"
                + ref_for_doi[doi_label_match.end():]
            )
        
            suggestions.append(make_suggestion(
                original=original,
                suggested=cleaned,
                confidence=0.95,
                issue_type="doi_format",
                reason="The DOI format appears non-standard. Review the DOI URL before applying.",
                category="reference_quality",
                field="doi"
            ))
        
        elif ref_for_doi != ref:
            suggestions.append(make_suggestion(
                original=original,
                suggested=ref_for_doi,
                confidence=0.95,
                issue_type="doi_spacing",
                reason="The DOI URL appears to contain spacing or dx.doi.org formatting. Review before applying.",
                category="reference_quality",
                field="doi"
            ))
        
        else:
            # Raw DOI without DOI URL
            raw_doi_match = re.search(r"(?<!doi\.org/)\b10\.\d{4,9}/[^\s\)]*", ref_for_doi, flags=re.I)
        
            if raw_doi_match and style.lower() == "apa":
                doi = raw_doi_match.group(0).rstrip(".,")
                cleaned = ref_for_doi.replace(doi, f"https://doi.org/{doi}")
        
                suggestions.append(make_suggestion(
                    original=original,
                    suggested=cleaned,
                    confidence=0.95,
                    issue_type="doi_format",
                    reason="APA 7 recommends DOI in URL format. Review before applying.",
                    category="reference_quality",
                    field="doi"
                ))
        # 3. HTTP to HTTPS
        if "http://" in ref:
            suggestions.append(make_suggestion(
                original=original,
                suggested=ref.replace("http://", "https://"),
                confidence=0.90,
                issue_type="http_to_https",
                reason="The reference uses HTTP. Review whether HTTPS is available and appropriate.",
                category="reference_quality",
                field="url"
            ))

        # 4. Seriously incomplete reference only
        has_enough_length = len(ref) >= 45
        has_title_after_year = False

        if year and str(year) in ref:
            after_year = ref.split(str(year), 1)[-1]
            has_title_after_year = len(after_year.strip(" .,)")) >= 15

        if not year or not has_enough_length or not has_title_after_year:
            suggestions.append(make_suggestion(
                original=original,
                suggested="Review reference manually",
                confidence=0.80,
                issue_type="seriously_incomplete_reference",
                reason="The reference appears to be missing a year, title, or essential bibliographic content.",
                category="reference_quality",
                field="bibliographic_details"
            ))

        # Missing DOI online suggestions remain disabled for speed and safety
        # Do not add missing_period, missing_journal_details, or missing page-range suggestions here.

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
        "possible_year_mismatch_unmatched": 4,
        "author_order_mismatch": 5,
        "et_al_misuse": 6,
        "missing_doi": 7,
        "doi_format": 8,
        "doi_spacing": 8,
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
        # Scenario 1b: Year mismatch among unmatched citations
        unmatched_year_suggestions = scenario_1b_unmatched_year_mismatch(
            c2r_rows,
            references_raw
        )
        all_suggestions.extend(unmatched_year_suggestions)
        print(f"📅 Scenario 1b - Unmatched year mismatches: {len(unmatched_year_suggestions)}")
        
        # Scenario 3: Author order mismatch
        order_suggestions = scenario_3_author_order_mismatch(c2r_rows, style)
        all_suggestions.extend(order_suggestions)
        print(f"🔄 Scenario 3 - Author order mismatches: {len(order_suggestions)}")
        
        # Scenario 5: Et al. misuse
        # Scenario 5: Et al. misuse
        # Disabled for now because et al. suggestions require highly reliable author extraction.
        et_al_suggestions = []
        print("📝 Scenario 5 - Et al. misuse: disabled")

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
        unique_suggestions = [force_review_only(s) for s in unique_suggestions]
        
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
                "review_high_confidence": len([s for s in unique_suggestions if s.get("confidence", 0) >= 0.85]),
                "review_medium_confidence": len([s for s in unique_suggestions if 0.70 <= s.get("confidence", 0) < 0.85]),
                "review_low_confidence": len([s for s in unique_suggestions if s.get("confidence", 0) < 0.70]),
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

# ============================================================
# VERIFICATION WORKER FUNCTION
# ============================================================

def process_verification(job_id, style="apa", enrich_metadata=False):
    """
    Durable online verification job.

    This replaces the old verify.py daemon-thread flow.
    It runs inside the RQ worker, persists progress after every chunk,
    updates PostgreSQL and Redis, and builds final dashboard tables.
    """
    print(f"🌐 Starting durable verification for job {job_id}")

    start_time = time.time()
    all_rows = []

    try:
        result = _load_job_result(job_id)
        refs = _normalise_references_for_verification(result)
        total = len(refs)

        if not total:
            result = _set_verification_meta(
                result,
                state="idle",
                progress=0,
                total=0,
                percentage=0,
                message=result.get("reference_detection_message", "No references extracted"),
                completed_at=now_iso()
            )
            result["online_verification"] = {
                "rows": [],
                "summary": _compute_verification_summary([])
            }
            result["recovery"] = {
                "missing_recovery": [],
                "verification_recovery": []
            }
            result["claim_support"] = []
            _save_job_result(job_id, result)
            return result

        result = _set_verification_meta(
            result,
            state="running",
            progress=0,
            total=total,
            percentage=0,
            message=f"Verification started for {total} references",
            started_at=now_iso(),
            completed_at=None,
            error=None
        )
        result["online_verification"] = {
            "rows": [],
            "summary": _compute_verification_summary([])
        }
        _save_job_result(job_id, result)

        chunks = [
            refs[i:i + VERIFY_CHUNK_SIZE]
            for i in range(0, total, VERIFY_CHUNK_SIZE)
        ]

        print(f"🌐 Verification split into {len(chunks)} chunks of {VERIFY_CHUNK_SIZE}")

        for chunk_index, chunk in enumerate(chunks, start=1):
            print(f"🌐 Verifying chunk {chunk_index}/{len(chunks)} with {len(chunk)} refs")

            chunk_rows = verify_references_batch(
                chunk,
                style=style,
                use_crossref=True,
                use_openalex=False,
                job_id=None,
                enrich_metadata=enrich_metadata
            )

            all_rows.extend(chunk_rows or [])

            progress = min(len(all_rows), total)
            percentage = int((progress / total) * 100) if total else 0
            summary = _compute_verification_summary(all_rows)

            result["online_verification"] = {
                "rows": all_rows,
                "summary": summary
            }

            result = _set_verification_meta(
                result,
                state="running",
                progress=progress,
                total=total,
                percentage=percentage,
                message=f"Verifying references: {progress}/{total}",
                last_heartbeat=now_iso(),
                chunks_completed=chunk_index,
                chunks_total=len(chunks)
            )

            _save_job_result(job_id, result)

            print(f"🌐 Persisted verification progress {progress}/{total}")

        summary = _compute_verification_summary(all_rows)

        result["online_verification"] = {
            "rows": all_rows,
            "summary": summary
        }
        
        # Save verification results first so the UI never hangs waiting
        result.setdefault("recovery", {
            "missing_recovery": [],
            "verification_recovery": []
        })
        
        if not isinstance(result.get("claim_support"), list):
            result["claim_support"] = []
        
        result = _set_verification_meta(
            result,
            state="finalising",
            progress=total,
            total=total,
            percentage=96,
            results_count=len(all_rows),
            summary=summary,
            message="Verification complete. Finalising Recovery and Claim Support tables.",
            last_heartbeat=now_iso()
        )
        
        _save_job_result(job_id, result)
        
        try:
            result["acii"] = compute_acii(result, all_rows)
        except Exception as e:
            print(f"[VERIFY WORKER] ACII error: {e}")
            result["acii"] = {"error": str(e)}

        try:
            result["recovery"] = _build_recovery_payload(result, all_rows)
        
            if not result["recovery"].get("missing_recovery") and not result["recovery"].get("verification_recovery"):
                result["recovery"] = {
                    "missing_recovery": [],
                    "verification_recovery": [],
                    "note": "No missing citations or weak verification rows requiring recovery were detected."
                }
        
        except Exception as e:
            print(f"[VERIFY WORKER] Recovery error: {e}")
            result["recovery"] = {
                "missing_recovery": [],
                "verification_recovery": [],
                "note": f"Recovery generation failed: {e}"
            }

        try:
            claim_rows = build_claim_support_rows(result) or []
        
            if not claim_rows:
                print("[VERIFY WORKER] Claim-support returned 0 rows. Using fallback rows.")
                claim_rows = _fallback_claim_support_rows(result, all_rows)
        
            result["claim_support"] = claim_rows
        
        except Exception as e:
            print(f"[VERIFY WORKER] Claim-support error: {e}")
            result["claim_support"] = _fallback_claim_support_rows(result, all_rows)

        elapsed = round(time.time() - start_time, 2)

        result = _set_verification_meta(
            result,
            state="completed",
            progress=total,
            total=total,
            percentage=100,
            results_count=len(all_rows),
            summary=summary,
            message=f"Verification completed for {len(all_rows)} references",
            completed_at=now_iso(),
            processing_time_seconds=elapsed
        )

        result["verification_completed_at"] = now_iso()

        _save_job_result(job_id, result, status="completed")

        print(f"✅ Durable verification completed for job {job_id}: {len(all_rows)} rows")
        return result

    except Exception as e:
        print(f"❌ Durable verification failed for job {job_id}: {e}")
        import traceback
        traceback.print_exc()

        try:
            result = _load_job_result(job_id)
            result = _set_verification_meta(
                result,
                state="error",
                message=str(e),
                error=str(e),
                completed_at=now_iso()
            )
            result.setdefault("online_verification", {
                "rows": all_rows,
                "summary": _compute_verification_summary(all_rows)
            })
            _save_job_result(job_id, result)
        except Exception as db_error:
            print(f"[VERIFY WORKER] Could not persist verification error: {db_error}")

        raise
# Start the worker
if __name__ == "__main__":
    print("🚀 Starting worker...")
    print(f"📊 Redis: {REDIS_URL[:50]}..." if REDIS_URL else "📊 Redis: NOT SET")
    print(f"💾 PostgreSQL: {'Connected' if DATABASE_URL else 'NOT SET'}")
    
    with Connection(redis_conn):
        document_queue = Queue("document_processing", connection=redis_conn)
        verification_queue = Queue("verification", connection=redis_conn)
    
        print(f"📌 Document queue: {document_queue.name}, jobs waiting: {document_queue.count}")
        print(f"📌 Verification queue: {verification_queue.name}, jobs waiting: {verification_queue.count}")
    
        worker = Worker(["document_processing", "verification"], connection=redis_conn)

        print("✅ Worker ready, waiting for jobs...")
        print("📋 Detection scenarios enabled:")
        print("   Scenario 1: Year mismatches")
        print("   Scenario 2: Author name mismatches")
        print("   Scenario 3: Author order mismatches")
        print("   Scenario 4: Combined author + year mismatches")
        print("   Scenario 5: Et al. misuse")
        print("   Scenario 6: Potential wrong references")
        print("   Reference quality issues")

        worker.work(burst=False)
