# citation_suggester.py

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Tuple

from verify import (
    _query_crossref,
    _query_openalex,
    _candidate_fields,
    _score,
    _score_candidate,
    _extract_fields_by_style,
)

CITATION_SUGGESTER_VERSION = "1.5.38"
CITATION_SUGGESTER_BUILD = "commercial-2026-05-22-recovery-guidance-manual-query-FINAL"

# ============================================================
# HELPER FUNCTIONS FOR ROBUST CONTEXT EXTRACTION
# ============================================================

_YEAR_RE = r"(?:19|20)\d{2}[a-z]?"
_SENT_BOUNDARY_RE = r"[.!?;]"

def _norm_ws(s: str) -> str:
    """Normalize whitespace in a string."""
    return re.sub(r"\s+", " ", (s or "")).strip()

def _extract_author_year_bits(citation: str):
    """Extract author surname and year from citation for fallback matching."""
    citation = citation or ""
    years = re.findall(_YEAR_RE, citation)
    year = years[0] if years else ""

    # crude author token extraction, keeps likely surnames
    tokens = re.findall(r"[A-Z][a-zA-Z'`-]{2,}", citation)
    stop = {"And", "Et", "Al"}
    authors = [t for t in tokens if t not in stop]

    surname = authors[0] if authors else ""
    return surname, year

def _find_sentence_span(text: str, pos: int):
    """Find the sentence boundaries around a position in text."""
    if pos < 0:
        return (0, len(text))
    left = text.rfind(".", 0, pos)
    left_q = text.rfind("?", 0, pos)
    left_e = text.rfind("!", 0, pos)
    left_s = text.rfind(";", 0, pos)
    left = max(left, left_q, left_e, left_s)
    left = 0 if left == -1 else left + 1

    candidates = [p for p in [
        text.find(".", pos),
        text.find("?", pos),
        text.find("!", pos),
        text.find(";", pos),
    ] if p != -1]
    right = min(candidates) + 1 if candidates else len(text)
    return left, right

def _expand_to_parenthetical_cluster(text: str, cit_start: int, cit_end: int):
    """
    If a detected citation is inside a parenthetical citation cluster,
    expand it to the full cluster.

    Example:
    Stored citation: Cohen, 1988
    Manuscript text: (Cohen, 1988; Button et al., 2013)
    Returns the span for the full parenthetical cluster.
    """
    if not text or cit_start < 0:
        return None

    # Look for nearest opening parenthesis before the citation
    left_paren = text.rfind("(", 0, cit_start + 1)
    if left_paren == -1:
        return None

    # Look for nearest closing parenthesis after the citation
    right_paren = text.find(")", cit_end)
    if right_paren == -1:
        return None

    # Avoid expanding across sentence boundaries before the parenthesis
    prev_sentence = max(
        text.rfind(".", 0, cit_start),
        text.rfind("?", 0, cit_start),
        text.rfind("!", 0, cit_start),
        text.rfind("\n", 0, cit_start)
    )

    if prev_sentence > left_paren:
        return None

    cluster = text[left_paren:right_paren + 1]

    # Confirm it looks like a citation cluster
    has_year = re.search(r"(?:19|20)\d{2}[a-z]?", cluster)
    has_author_like = re.search(r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]{2,}", cluster)

    if not has_year or not has_author_like:
        return None

    # Avoid huge accidental captures
    if len(cluster) > 350:
        return None

    return left_paren, right_paren + 1, cluster

_SUP_DIGITS_SPLIT = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_SUP_TO_NORMAL_SPLIT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻−–—", "0123456789----")
_NORMAL_TO_SUP_SPLIT = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")


def _expand_numeric_cluster_numbers(text: str, max_range: int = 60) -> List[str]:
    """Return numeric items from comma/range clusters, expanding small ranges."""
    raw = str(text or "").translate(_SUP_TO_NORMAL_SPLIT)
    raw = raw.replace("–", "-").replace("—", "-").replace("−", "-")
    out = []

    for part in re.split(r"\s*[,;]\s*", raw):
        part = part.strip()
        if not part:
            continue

        m = re.fullmatch(r"(\d{1,4})\s*-\s*(\d{1,4})", part)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if start <= end and (end - start) <= max_range:
                out.extend(str(i) for i in range(start, end + 1))
            else:
                out.extend([m.group(1), m.group(2)])
            continue

        out.extend(re.findall(r"\d{1,4}", part))

    seen = set()
    clean = []
    for n in out:
        if n not in seen:
            seen.add(n)
            clean.append(n)
    return clean


def split_citation_cluster(citation_text: str) -> List[str]:
    """
    Split clustered citations into individual citation strings.

    Covers author-year clusters and major numeric families:
    - (Beck et al., 2021; Zhang et al., 2022)
    - [1,2,3], [1-3], [1–3]
    - (1,2,3), (1-3) when the content is numeric only
    - Unicode superscript clusters: ¹,²,³ and ¹–³

    The claim should be extracted once from the full cluster, but each returned
    citation item can then be judged separately against its own matched source.
    """
    if not citation_text:
        return []

    c = citation_text.strip()

    # Numeric square: [1,2], [1-3]
    if re.fullmatch(r"\[\s*\d{1,4}(?:\s*[,;\-–—]\s*\d{1,4})*\s*\]", c):
        nums = _expand_numeric_cluster_numbers(c.strip("[]"))
        return [f"[{n}]" for n in nums] or [c]

    # Numeric round: (1,2), (1-3). Avoid author-year parentheticals by requiring numeric-only content.
    if re.fullmatch(r"\(\s*\d{1,4}(?:\s*[,;\-–—]\s*\d{1,4})*\s*\)", c):
        nums = _expand_numeric_cluster_numbers(c.strip("()"))
        return [f"({n})" for n in nums] or [c]

    # Unicode superscript clusters: ¹,²,³ or ¹–³.
    if re.fullmatch(rf"[\s{_SUP_DIGITS_SPLIT},;\-–—⁻−]+", c) and re.search(rf"[{_SUP_DIGITS_SPLIT}]", c):
        nums = _expand_numeric_cluster_numbers(c)
        return [str(n).translate(_NORMAL_TO_SUP_SPLIT) for n in nums] or [c]

    # Author-year parenthetical clusters split by semicolon.
    if c.startswith("(") and c.endswith(")"):
        inner = c[1:-1]
        if re.search(_YEAR_RE, inner) and ";" in inner:
            parts = [p.strip() for p in re.split(r"\s*;\s*", inner) if p.strip()]
            return [f"({p})" for p in parts]


    return [c]


def _clean_context_candidate_for_claim(claim: str) -> str:
    """Clean claim candidates returned by extract_context()."""
    claim = re.sub(r"\s+", " ", str(claim or "")).strip()
    if not claim:
        return ""
    claim = re.sub(r"\([^()]{0,260}\b(?:19|20)\d{2}[a-z]?[^()]{0,260}\)", " ", claim, flags=re.I)
    claim = re.sub(r"\[[0-9,;\-–—\s]+\]", " ", claim)
    claim = re.sub(r"\s*\([^)]*\b(?:19|20)\d{2}[a-z]?[^)]*$", "", claim, flags=re.I)
    claim = re.sub(r"\s*[\(\[\{]+\s*$", "", claim)
    claim = re.sub(r"^[\s\)\]\.,;:]+", "", claim)
    claim = re.sub(r"\s+([.,;:!?])", r"\1", claim)
    claim = re.sub(r"\s+", " ", claim).strip(" ,;:-.")
    # Reject near-pure citation fragments.
    if re.fullmatch(r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+et\s+al\.?)?(?:\s*(?:&|and)\s*[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+)?\s*,?\s*(?:19|20)\d{2}[a-z]?\)?", claim, flags=re.I):
        return ""
    return claim

# ============================================================
# CORE FUNCTIONS
# ============================================================

def extract_citation_author_year(citation: str) -> Tuple[List[str], str]:
    """
    Extract author surname(s) and year from a citation text.
    
    Args:
        citation: The citation text (e.g., "(Beck et al., 2021)" or "Beck et al. (2021)")
    
    Returns:
        Tuple of (list of author surnames, year string)
    """
    citation = (citation or "").strip()
    
    # Extract year
    year_match = re.search(r"\b((?:19|20)\d{2}[a-z]?)\b", citation)
    year = year_match.group(1) if year_match else ""
    
    # Remove year and brackets for author extraction
    left = re.sub(r"\b(?:19|20)\d{2}[a-z]?\b", "", citation)
    left = re.sub(r"[\(\)]", " ", left)
    left = re.sub(r"\bet\s+al\.?\b", "", left, flags=re.I)
    left = left.replace("&", " and ")
    
    authors = []
    # Split by common separators
    parts = re.split(r"\band\b|,|;", left, flags=re.I)
    for p in parts:
        p = p.strip()
        if not p:
            continue
        toks = p.split()
        if toks:
            # Take the last token as surname
            surname = re.sub(r"[^A-Za-z'\-]", "", toks[-1]).lower()
            if len(surname) >= 2:
                authors.append(surname)
    
    # Deduplicate while preserving order
    seen = set()
    out = []
    for a in authors:
        if a not in seen:
            seen.add(a)
            out.append(a)
    
    return out[:2], year  # Return at most 2 authors


def extract_context(text: str, citation: str, window: int = 400) -> str:
    """
    AGGRESSIVE claim extraction for narrative citations.
    For narrative citations (Author Year verb...), returns EVERYTHING after the citation.
    """
    if not text or not citation:
        return ""

    raw_text = text
    raw_cit = citation.strip()

    # ============================================================
    # FIND CITATION POSITION
    # ============================================================
    idx = -1
    
    # Exact match
    idx = raw_text.find(raw_cit)
    
    # Normalized match
    if idx == -1:
        text_norm = re.sub(r"\s+", " ", raw_text)
        cit_norm = re.sub(r"\s+", " ", raw_cit)
        idx_norm = text_norm.find(cit_norm)
        if idx_norm != -1:
            probe = cit_norm[:50]
            for match in re.finditer(re.escape(probe[:20]), raw_text):
                idx = match.start()
                break
    
        # Author-year pattern match with citation variants
        if idx == -1:
            authors, year = extract_citation_author_year(raw_cit)
    
            if authors and year:
                author1 = re.escape(authors[0])
                year_base = re.escape(year[:4])
    
                # Allow small citation-format variations:
                # (Author, 2001), Author (2001), Author, 2001, Author and Coauthor (2001)
                variant_patterns = [
                    # Parenthetical single-author form: (Author, 2019)
                    rf"\(\s*{author1}\s*,\s*{year_base}[a-z]?\s*\)",
                
                    # Narrative single-author form: Author (2019)
                    rf"{author1}\s*\(\s*{year_base}[a-z]?\s*\)",
                
                    # Loose author-year form: Author, 2019
                    rf"{author1}\s*,\s*{year_base}[a-z]?",
                
                    # Parenthetical et al. form: (Author et al., 2019)
                    rf"\(\s*{author1}\s+et\s+al\.?\s*,\s*{year_base}[a-z]?\s*\)",
                
                    # Narrative et al. form: Author et al. (2019)
                    rf"{author1}\s+et\s+al\.?\s*\(\s*{year_base}[a-z]?\s*\)",
                
                    # Loose et al. form: Author et al., 2019
                    rf"{author1}\s+et\s+al\.?\s*,?\s*{year_base}[a-z]?",
                ]
    
                if len(authors) >= 2:
                    author2 = re.escape(authors[1])
                    variant_patterns.extend([
                        rf"\(\s*{author1}\s*(?:&|and)\s*{author2}\s*,\s*{year_base}[a-z]?\s*\)",
                        rf"{author1}\s*(?:&|and)\s*{author2}\s*\(\s*{year_base}[a-z]?\s*\)",
                        rf"{author1}\s*(?:&|and)\s*{author2}\s*,\s*{year_base}[a-z]?",
                    ])
    
                for pat in variant_patterns:
                    m = re.search(pat, raw_text, flags=re.I)
                    if m:
                        idx = m.start()
                        raw_cit = raw_text[m.start():m.end()]
                        break
            # Cluster fallback:
            # If the individual citation is part of a parenthetical cluster,
            # find the full cluster that contains the author-year pair.
            if idx == -1:
                authors, year = extract_citation_author_year(raw_cit)
        
                if authors and year:
                    author1 = re.escape(authors[0])
                    year_base = re.escape(year[:4])
        
                    cluster_pattern = rf"\([^)]*{author1}[^)]*(?:{year_base}[a-z]?)[^)]*\)"
                    m = re.search(cluster_pattern, raw_text, flags=re.I)
                    
                    # Extra fallback: author and year appear within the same 120-character citation cluster
                    if not m:
                        cluster_pattern = rf"\([^)]{{0,120}}{author1}[^)]{{0,120}}{year_base}[a-z]?[^)]{{0,120}}\)"
                        m = re.search(cluster_pattern, raw_text, flags=re.I)
        
                    if m:
                        idx = m.start()
                        raw_cit = raw_text[m.start():m.end()]

        if idx == -1:
            return ""

    cit_start = idx
    cit_end = min(len(raw_text), idx + len(raw_cit))
    
    # If the detected citation is inside a parenthetical cluster,
    # expand to the full cluster before deciding parenthetical/narrative.
    expanded_cluster = _expand_to_parenthetical_cluster(raw_text, cit_start, cit_end)
    if expanded_cluster:
        cit_start, cit_end, raw_cit = expanded_cluster

# ============================================================
# DETECT NARRATIVE VS PARENTHETICAL
# ============================================================

    # ============================================================
    # DETECT NARRATIVE VS PARENTHETICAL
    # ============================================================
    
    is_parenthetical = raw_cit.startswith("(") and raw_cit.endswith(")")
    
    # Check for narrative patterns
    pattern1 = re.search(r'[A-Z][a-z]+(?:\s+et\s+al\.?)?\s*\(\s*(?:19|20)\d{2}\s*\)', raw_cit)
    pattern2 = re.search(r'[A-Z][a-z]+(?:\s+et\s+al\.?)?\s+(?:19|20)\d{2}', raw_cit)
    
    is_narrative = (pattern1 or pattern2) and not is_parenthetical
    
    # Check for narrative verbs after citation
    after_text = raw_text[cit_end:min(cit_end + 150, len(raw_text))]
    narrative_verbs = {
        'argues', 'argue', 'stated', 'states', 'state', 'claimed', 'claims', 'claim',
        'suggested', 'suggests', 'suggest', 'found', 'finds', 'find', 'showed', 'shows', 'show',
        'demonstrated', 'demonstrates', 'demonstrate', 'reported', 'reports', 'report',
        'proposed', 'proposes', 'propose', 'described', 'describes', 'describe',
        'examined', 'examines', 'examine', 'investigated', 'investigates', 'investigate',
        'analyzed', 'analyzes', 'analyze', 'concluded', 'concludes', 'conclude',
        'noted', 'notes', 'note', 'observed', 'observes', 'observe',
        'emphasized', 'emphasizes', 'emphasize', 'highlighted', 'highlights', 'highlight',
        'indicated', 'indicates', 'indicate', 'explained', 'explains', 'explain',
        'wrote', 'writes', 'write', 'published', 'publishes', 'publish'
    }
    
    after_clean = after_text.lstrip()
    first_word = after_clean.split()[0].lower() if after_clean.split() else ""
    
    if first_word in narrative_verbs:
        is_narrative = True

    # ============================================================
    # EXTRACT CLAIM - FORCE RIGHT SIDE FOR NARRATIVE
    # ============================================================
    
    # PARENTHETICAL: claim is BEFORE
    if is_parenthetical:
        sent_start = max(
            raw_text.rfind('.', 0, cit_start),
            raw_text.rfind('!', 0, cit_start),
            raw_text.rfind('?', 0, cit_start),
            raw_text.rfind('\n', 0, cit_start)
        ) + 1
        if sent_start == 0:
            sent_start = max(0, cit_start - window)
        
        claim = raw_text[sent_start:cit_start].strip()
        if claim:
            return _clean_context_candidate_for_claim(claim)
    
    # NARRATIVE: claim is AFTER (take up to window characters or until sentence ends)
    if is_narrative:
        # Find sentence end (period, question mark, exclamation)
        sent_end = raw_text.find('.', cit_end)
        if sent_end == -1 or sent_end > cit_end + window:
            sent_end = min(len(raw_text), cit_end + window)
        else:
            sent_end = sent_end + 1  # Include the period
        
        claim = raw_text[cit_end:sent_end].strip()
        
        # If claim is too short, take more
        if len(claim) < 30 and cit_end + window < len(raw_text):
            claim = raw_text[cit_end:cit_end + window].strip()
        
        # Clean up
        claim = re.sub(r'^[\s,;:]+', '', claim)
        claim = re.sub(r'\s+', ' ', claim)
        
        if claim and len(claim) > 10:
            return _clean_context_candidate_for_claim(claim)
    
    # ============================================================
    # FALLBACK: Try right side first (academic writing prefers claim after citation)
    # ============================================================
    
    right_end = min(len(raw_text), cit_end + window)
    right_claim = raw_text[cit_end:right_end].strip()
    right_claim = re.sub(r'^[\s,;:]+', '', right_claim)
    
    if right_claim and len(right_claim) > 15:
        return _clean_context_candidate_for_claim(right_claim)
    
    # Last resort: left side
    left_start = max(0, cit_start - window)
    left_claim = raw_text[left_start:cit_start].strip()
    
    if left_claim:
        return _clean_context_candidate_for_claim(left_claim)
    
    return ""



# ============================================================
# SMART CONTEXT / CLAIM-BASED SOURCE SEARCH
# ============================================================
# These functions are deliberately conservative. They improve Advanced Recovery,
# Claim Support alternatives, and Citation Needed enrichment without moving the
# expensive external lookups into the fast verification path. The worker still
# decides when to call these functions.

_SMART_STOPWORDS = {
    "this", "that", "with", "from", "using", "used", "study", "studies", "analysis",
    "method", "methods", "approach", "results", "table", "figure", "paper", "article",
    "research", "journal", "review", "these", "those", "their", "there", "where",
    "would", "could", "should", "might", "what", "when", "which", "while", "during",
    "about", "into", "through", "based", "claim", "claims", "citation", "source",
    "sources", "evidence", "support", "supported", "manual", "required", "context",
    "chapter", "section", "thesis", "dissertation", "manuscript", "finding", "findings",
    "effect", "effects", "impact", "impacts", "relationship", "relationships", "role",
    "model", "models", "framework", "conceptual", "empirical", "significant", "positive",
    "negative", "increase", "decrease", "higher", "lower", "within", "between", "among",
    "therefore", "however", "although", "because", "also", "more", "most", "such", "than",
    "then", "they", "them", "were", "been", "have", "has", "had", "will", "may", "can"
}

_DOMAIN_KEEP_WORDS = {
    "procurement", "sustainable", "literacy", "ethics", "ethical", "behaviour", "behavior",
    "attitude", "digital", "centralisation", "centralization", "governance", "finance",
    "financial", "performance", "inflation", "exchange", "education", "learning", "banking",
    "credit", "risk", "climate", "policy", "public", "health", "supply", "chain", "logistics",
    "culture", "gender", "women", "empowerment", "retirement", "planning", "employee",
    "motivation", "personality", "corporate", "board", "audit", "accountability"
}

def _clean_search_text(text: str, max_len: int = 260) -> str:
    text = re.sub(r"https?://\S+", " ", str(text or ""), flags=re.I)
    text = re.sub(r"\b10\.\d{4,9}/\S+", " ", text, flags=re.I)
    text = re.sub(r"\([^)]*(?:19|20)\d{2}[a-z]?[^)]*\)", " ", text)
    text = re.sub(r"\[[0-9,\-–\s]+\]", " ", text)
    text = re.sub(r"[^A-Za-z0-9\s:&,\-'’]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]

def _word_tokens(text: str) -> List[str]:
    words = re.findall(r"[A-Za-z][A-Za-z\-'’]{2,}", str(text or "").lower())
    out, seen = [], set()
    for w in words:
        w = w.strip("-'’ ")
        if len(w) < 4:
            continue
        if w in _SMART_STOPWORDS and w not in _DOMAIN_KEEP_WORDS:
            continue
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out

def _significant_terms(text: str, limit: int = 12) -> List[str]:
    return _word_tokens(text)[:limit]

def extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from text for search queries."""
    return _significant_terms(text, limit=10)

def _claim_phrases(text: str, max_phrases: int = 4) -> List[str]:
    """Create short phrase queries from adjacent significant words."""
    clean = _clean_search_text(text, max_len=420)
    raw = re.findall(r"[A-Za-z][A-Za-z\-'’]{2,}", clean.lower())
    filtered = []
    for w in raw:
        w = w.strip("-'’ ")
        if len(w) < 4:
            continue
        if w in _SMART_STOPWORDS and w not in _DOMAIN_KEEP_WORDS:
            continue
        filtered.append(w)
    phrases, seen = [], set()
    for n in (4, 3, 2):
        for i in range(0, max(0, len(filtered) - n + 1)):
            phrase = " ".join(filtered[i:i+n])
            if phrase in seen:
                continue
            seen.add(phrase)
            phrases.append(phrase)
            if len(phrases) >= max_phrases:
                return phrases
    return phrases

def _candidate_url(cand: Dict[str, Any], doi: str = "") -> str:
    item = cand.get("item") if isinstance(cand, dict) else None
    item = item if isinstance(item, dict) else cand
    if not isinstance(item, dict):
        return f"https://doi.org/{doi}" if doi else ""
    if item.get("URL"):
        return item.get("URL")
    primary = item.get("primary_location") or {}
    if isinstance(primary, dict) and primary.get("landing_page_url"):
        return primary.get("landing_page_url")
    if item.get("id") and str(item.get("id")).startswith("http"):
        return item.get("id")
    return f"https://doi.org/{doi}" if doi else ""


def _safe_candidate_fields(cand: Any) -> Tuple[str, str, str, List[str]]:
    """Normalise verification candidates across legacy tuple and current dict APIs."""
    if not isinstance(cand, dict):
        return "", "", "", []
    try:
        fields = _candidate_fields(cand)
    except Exception:
        return "", "", "", []
    if isinstance(fields, dict):
        item = cand.get("item") or {}
        authors = []
        if cand.get("source") == "crossref":
            for author in item.get("author") or []:
                if isinstance(author, dict):
                    family, given = str(author.get("family") or "").strip(), str(author.get("given") or "").strip()
                    full = f"{family}, {given}".strip(" ,")
                    if full:
                        authors.append(full)
        elif cand.get("source") == "openalex":
            for authorship in item.get("authorships") or []:
                name = str(((authorship or {}).get("author") or {}).get("display_name") or "").strip()
                if name:
                    authors.append(name)
        authors = authors or fields.get("authors") or []
        if not isinstance(authors, list):
            authors = [str(authors)] if authors else []
        return (
            str(fields.get("doi") or ""),
            str(fields.get("title") or ""),
            str(fields.get("year") or ""),
            [str(author) for author in authors if author],
        )
    if isinstance(fields, (tuple, list)) and len(fields) >= 4:
        doi, title, year, authors = fields[:4]
        return str(doi or ""), str(title or ""), str(year or ""), list(authors or [])
    return "", "", "", []


def _openalex_abstract(item: Dict[str, Any], max_words: int = 180) -> str:
    inverted = item.get("abstract_inverted_index") or {}
    if not isinstance(inverted, dict):
        return ""
    positions = []
    for word, indexes in inverted.items():
        for position in indexes or []:
            if isinstance(position, int):
                positions.append((position, str(word)))
    positions.sort(key=lambda pair: pair[0])
    return " ".join(word for _position, word in positions[:max_words])


def _candidate_publication_metadata(cand: Dict[str, Any]) -> Dict[str, Any]:
    from reference_metadata import provider_metadata
    bibliographic = provider_metadata(cand.get("source"), cand.get("item"))
    item_value = cand.get("item") if isinstance(cand, dict) else {}
    item = item_value if isinstance(item_value, dict) else {}
    if cand.get("source") == "crossref":
        container = item.get("container-title") or item.get("short-container-title") or []
        if isinstance(container, str):
            container = [container]
        elif not isinstance(container, list):
            container = []
        abstract = re.sub(r"<[^>]+>", " ", str(item.get("abstract") or ""))
        abstract = _norm_ws(abstract)[:1800]
        return {
            **bibliographic,
            "journal": str(container[0] if container else ""),
            "volume": str(item.get("volume") or ""),
            "issue": str(item.get("issue") or ""),
            "pages": str(item.get("page") or item.get("article-number") or ""),
            "publisher": str(item.get("publisher") or ""),
            "publication_type": str(item.get("type") or "article"),
            "abstract_excerpt": abstract,
            "concepts": [],
        }
    if cand.get("source") == "openalex":
        primary = item.get("primary_location") or {}
        source_value = primary.get("source") if isinstance(primary, dict) else {}
        source = source_value if isinstance(source_value, dict) else {}
        biblio = item.get("biblio") or {}
        if not isinstance(biblio, dict):
            biblio = {}
        concepts = [
            str((concept or {}).get("display_name") or "")
            for concept in (item.get("concepts") or [])[:12]
            if isinstance(concept, dict) and (concept or {}).get("display_name")
        ]
        return {
            **bibliographic,
            "journal": str(source.get("display_name") or ""),
            "volume": str(biblio.get("volume") or ""),
            "issue": str(biblio.get("issue") or ""),
            "pages": str(biblio.get("first_page") or "") + (("-" + str(biblio.get("last_page"))) if biblio.get("last_page") else ""),
            "publisher": "",
            "publication_type": str(item.get("type") or "article"),
            "abstract_excerpt": _openalex_abstract(item)[:1800],
            "concepts": concepts,
        }
    return bibliographic

def _author_match_score(query_authors: List[str], candidate_authors: List[str]) -> int:
    if not query_authors or not candidate_authors:
        return 0
    cand_l = " ".join(candidate_authors).lower()
    score = 0
    for a in query_authors[:2]:
        if a and a.lower() in cand_l:
            score += 10
    return min(score, 20)

def _title_claim_score(title: str, context: str) -> Dict[str, Any]:
    title_terms = set(_significant_terms(title, limit=20))
    context_terms = set(_significant_terms(context, limit=24))
    overlap = sorted(title_terms & context_terms)
    phrase_hits = [p for p in _claim_phrases(context, max_phrases=6) if p and p in str(title or "").lower()]
    if not title_terms or not context_terms:
        ratio = 0.0
    else:
        ratio = len(overlap) / max(1, min(len(title_terms), len(context_terms)))
    return {
        "overlap_terms": overlap[:10],
        "phrase_hits": phrase_hits[:5],
        "overlap_ratio": ratio,
        "overlap_score": min(45, int(round(ratio * 45)) + (8 * min(len(phrase_hits), 2))),
    }

def _quality_label(score: int) -> str:
    if score >= 82:
        return "strong_candidate"
    if score >= 68:
        return "good_candidate"
    if score >= 55:
        return "possible_candidate"
    return "weak_candidate"

def _build_context_queries(context: str, citation: str = "", *, use_citation_hint: bool = True) -> List[Dict[str, str]]:
    context = _clean_search_text(context, max_len=420)
    keywords = _significant_terms(context, limit=12)
    phrases = _claim_phrases(context, max_phrases=4)
    authors, year = extract_citation_author_year(citation)

    queries: List[Dict[str, str]] = []
    def add(q: str, strategy: str):
        q = _clean_search_text(q, max_len=220)
        if len(q) >= 6 and q.lower() not in {x["query"].lower() for x in queries}:
            queries.append({"query": q, "strategy": strategy})

    # Claim/context-first queries. These are best for alternative sources and citation-needed claims.
    if phrases:
        add(" ".join(phrases[:2]), "claim_phrase_query")
    if keywords:
        add(" ".join(keywords[:8]), "claim_keyword_query")
        add(" ".join(keywords[:5]), "compact_claim_keyword_query")

    # Citation hint is useful for missing-reference recovery, but it should not dominate claim alternatives.
    if use_citation_hint and (authors or year) and keywords:
        add(" ".join(authors[:2] + ([year] if year else []) + keywords[:5]), "citation_plus_claim_query")
    elif use_citation_hint and (authors or year):
        add(" ".join(authors[:2] + ([year] if year else [])), "citation_only_query")

    return queries[:5]

def suggest_from_context(
    context: str,
    citation: str = "",
    top_k: int = 3,
    *,
    use_citation_hint: bool = True,
    min_relevance: int = 55,
    strict_citation_identity: bool = False,
    diagnostics: Dict[str, Any] = None,
) -> List[Dict[str, Any]]:
    """
    Suggest review-only scholarly sources from a claim/context.

    The previous version gave a high base score to any Crossref/OpenAlex result.
    This version ranks candidates by concept overlap between the manuscript claim
    and candidate title, with only small boosts for author/year. This reduces
    attractive but weak alternatives.
    """
    diag = diagnostics if isinstance(diagnostics, dict) else {}
    diag.update({
        "providers_attempted": ["openalex", "crossref"],
        "provider_result_counts": {"openalex": 0, "crossref": 0},
        "query_count": 0,
        "query_strategies": [],
        "raw_candidate_count": 0,
        "ranked_candidate_count": 0,
        "returned_candidate_count": 0,
        "errors": [],
    })
    context = _clean_search_text(context, max_len=500)
    if not context or len(_significant_terms(context, limit=4)) < 2:
        diag["outcome"] = "insufficient_search_context"
        return []

    citation_authors, citation_year = extract_citation_author_year(citation)
    queries = _build_context_queries(context, citation, use_citation_hint=use_citation_hint)
    if not queries:
        diag["outcome"] = "no_search_query_generated"
        return []
    diag["query_count"] = len(queries)
    diag["query_strategies"] = [q.get("strategy") for q in queries if q.get("strategy")]

    candidates = []
    lookup_jobs = []
    for query_spec in queries:
        lookup_jobs.extend([
            ("openalex", _query_openalex, query_spec),
            ("crossref", _query_crossref, query_spec),
        ])
    try:
        requested_workers = int(os.getenv("SOURCE_SEARCH_PARALLEL_REQUESTS", "4") or "4")
    except (TypeError, ValueError):
        requested_workers = 4
    max_workers = max(1, min(requested_workers, len(lookup_jobs)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(func, query_spec["query"], 8): (provider, query_spec)
            for provider, func, query_spec in lookup_jobs
        }
        for future in as_completed(futures):
            provider, query_spec = futures[future]
            try:
                rows = future.result() or []
                diag["provider_result_counts"][provider] += len(rows)
                for cand in rows:
                    if not isinstance(cand, dict):
                        continue
                    cand["_query_used"] = query_spec["query"]
                    cand["_query_strategy"] = query_spec["strategy"]
                    candidates.append(cand)
            except Exception as exc:
                diag["errors"].append({"provider": provider, "error_type": type(exc).__name__})

    diag["raw_candidate_count"] = len(candidates)

    seen = set()
    suggestions = []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        doi, title, cand_year, authors_list = _safe_candidate_fields(cand)
        title = (title or "").strip()
        if not title:
            continue

        key = f"{title.lower()}|{cand_year}|{doi.lower()}"
        if key in seen:
            continue
        seen.add(key)

        title_score = _title_claim_score(title, context)
        author_boost = _author_match_score(citation_authors, authors_list)
        publication_years = _candidate_fields(cand).get("publication_years") or [str(cand_year)[:4]]
        year_boost = 8 if (citation_year and citation_year[:4] in publication_years) else 0
        doi_boost = 5 if doi else 0

        relevance = min(100, 35 + title_score["overlap_score"] + author_boost + year_boost + doi_boost)

        # Conservative gate: do not return candidates with no concept signal unless author+year is strong.
        has_concept_signal = bool(title_score["overlap_terms"] or title_score["phrase_hits"])
        raw_metadata = json.dumps(cand.get("item") or cand, ensure_ascii=False).lower()
        identity_author_match = any(author.lower() in raw_metadata for author in citation_authors if author)
        strong_citation_signal = bool((author_boost >= 10 or identity_author_match) and year_boost > 0)
        if strict_citation_identity and not strong_citation_signal:
            continue
        if relevance < min_relevance or not (has_concept_signal or strong_citation_signal):
            continue

        suggestions.append({
            "title": title,
            "year": cand_year,
            "authors": authors_list,
            "doi": doi,
            "url": _candidate_url(cand, doi),
            "type": "context",
            "source": cand.get("source", "scholarly_lookup"),
            "relevance": relevance,
            "candidate_quality": _quality_label(relevance),
            "query_used": cand.get("_query_used", ""),
            "query_strategy": cand.get("_query_strategy", ""),
            "match_basis": {
                "title_claim_overlap_terms": title_score["overlap_terms"],
                "title_phrase_hits": title_score["phrase_hits"],
                "title_claim_overlap_ratio": round(title_score["overlap_ratio"], 3),
                "author_boost": author_boost,
                "year_boost": year_boost,
                "doi_boost": doi_boost,
            },
            "suggestion_type": "context_specific_source",
            "reason": "Ranked by claim-title concept overlap and citation metadata. Review before using.",
            "review_required": True,
            **_candidate_publication_metadata(cand),
        })

    suggestions.sort(key=lambda x: (x.get("relevance", 0), bool(x.get("doi"))), reverse=True)
    diag["ranked_candidate_count"] = len(suggestions)
    returned = suggestions[:top_k]
    diag["returned_candidate_count"] = len(returned)
    diag["outcome"] = (
        "candidates_returned" if returned
        else "providers_returned_no_records_or_records_failed_relevance_screen"
    )
    return returned

def _extract_fields_multi_style(ref: str, style: str = "apa") -> Dict[str, Any]:
    styles = []
    if style:
        styles.append(style)
    styles.extend(["apa", "author_year", "numeric_square", "numeric_superscript", "numeric_round"])
    seen = set()
    best = {}
    for st in styles:
        if st in seen:
            continue
        seen.add(st)
        try:
            fields = _extract_fields_by_style(ref, st) or {}
        except Exception:
            fields = {}
        title = fields.get("title") or ""
        doi = fields.get("doi") or ""
        if doi or len(title) > len(best.get("title", "") or ""):
            best = fields
        if doi and title:
            break
    return best


def _reference_title_similarity(reference_title: str, candidate_title: str) -> int:
    import difflib
    a = _clean_search_text(reference_title, max_len=260).lower()
    b = _clean_search_text(candidate_title, max_len=260).lower()
    if not a or not b:
        return 0
    return int(round(difflib.SequenceMatcher(None, a, b).ratio() * 100))


def _reference_title_terms(reference_title: str, candidate_title: str) -> List[str]:
    return sorted(set(_significant_terms(reference_title, limit=24)) & set(_significant_terms(candidate_title, limit=24)))[:12]


def _strict_reference_candidate_pass(fields: Dict[str, Any], cand_title: str, cand_year: str, cand_authors: List[str], cand_doi: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    ref_title = fields.get("title", "") or ""
    ref_year = str(fields.get("year", "") or "")[:4]
    ref_doi = str(fields.get("doi", "") or "").lower().strip()
    ref_authors = fields.get("authors", []) or []

    title_similarity = _reference_title_similarity(ref_title, cand_title)
    overlap_terms = _reference_title_terms(ref_title, cand_title)
    year_match = bool(ref_year and ref_year in (meta.get("publication_years") or [str(cand_year)[:4]]))
    doi_match = bool(ref_doi and cand_doi and ref_doi == str(cand_doi).lower().strip())
    author_similarity = int(meta.get("author_similarity", 0) or 0)

    doi_conflict = bool(ref_doi and cand_doi and ref_doi != str(cand_doi).lower().strip())
    strict_pass = not doi_conflict and bool(
        doi_match
        or (title_similarity >= 90 and year_match and (author_similarity >= 50 or not ref_authors))
        or (title_similarity >= 95 and author_similarity >= 80)
        or (title_similarity >= 82 and year_match and author_similarity >= 75 and len(overlap_terms) >= 6)
    )

    fit_score = 0
    if doi_match:
        fit_score += 100
    fit_score += min(70, title_similarity)
    fit_score += min(20, len(overlap_terms) * 4)
    if year_match:
        fit_score += 8
    if author_similarity:
        fit_score += min(12, int(author_similarity / 10))

    return {
        "strict_pass": strict_pass,
        "reference_title_similarity": title_similarity,
        "reference_title_overlap_terms": overlap_terms,
        "reference_year_match": year_match,
        "reference_doi_match": doi_match,
        "reference_author_similarity": author_similarity,
        "reference_fit_score": min(100, fit_score),
    }

def suggest_for_unverified(
    ref: str,
    top_k: int = 3,
    style: str = "apa",
    strict_reference: bool = True,
    diagnostics: Dict[str, Any] = None,
) -> List[Dict[str, Any]]:
    """
    Suggest corrected references for unverified/needs_review references.

    This version is style-aware and requires strong title/metadata evidence.
    It is intended for Deep Recovery, not automatic replacement.
    """
    diag = diagnostics if isinstance(diagnostics, dict) else {}
    diag.update({
        "providers_attempted": ["crossref", "openalex"],
        "provider_result_counts": {"crossref": 0, "openalex": 0},
        "query_count": 1,
        "raw_candidate_count": 0,
        "ranked_candidate_count": 0,
        "returned_candidate_count": 0,
        "errors": [],
    })
    fields = _extract_fields_multi_style(ref, style=style)
    title = fields.get("title", "") or ""
    authors = fields.get("authors", []) or []
    year = fields.get("year", "") or ""
    doi = fields.get("doi", "") or ""

    query_parts = []
    if doi:
        query_parts.append(doi)
    if title:
        query_parts.append(title)
    if authors:
        query_parts.extend(authors[:2])
    if year:
        query_parts.append(year)

    query = _clean_search_text(" ".join(query_parts), max_len=260)
    if not query:
        diag["outcome"] = "no_reference_identity_query_generated"
        return []

    candidates = []
    try:
        crossref_rows = _query_crossref(query, rows=10) or []
        diag["provider_result_counts"]["crossref"] = len(crossref_rows)
        candidates.extend(crossref_rows)
    except Exception as exc:
        diag["errors"].append({"provider": "crossref", "error_type": type(exc).__name__})
    try:
        openalex_rows = _query_openalex(query, rows=10) or []
        diag["provider_result_counts"]["openalex"] = len(openalex_rows)
        candidates.extend(openalex_rows)
    except Exception as exc:
        diag["errors"].append({"provider": "openalex", "error_type": type(exc).__name__})
    diag["raw_candidate_count"] = len(candidates)

    seen = set()
    suggestions = []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        cand_doi, cand_title, cand_year, cand_authors = _safe_candidate_fields(cand)
        if not cand_title:
            continue

        meta = _score_candidate(fields, cand)
        doi_match = bool(doi and cand_doi and doi.lower().strip() == cand_doi.lower().strip())
        title_score = float(meta.get("title_score", 0) or 0)
        overall = float(meta.get("score", 0) or 0)

        strict_fit = _strict_reference_candidate_pass(fields, cand_title, cand_year, cand_authors, cand_doi, meta)

        # Keep only correction candidates with strong evidence. In strict mode,
        # broad author/year hits are not enough; the candidate title must be very
        # close to the reference title, or there must be an exact DOI match.
        if strict_reference and not strict_fit.get("strict_pass"):
            continue
        if not strict_reference and not doi_match and title_score < 78:
            continue
        if not strict_reference and not doi_match and overall < 65:
            continue

        key = f"{cand_title.lower()}|{cand_year}|{cand_doi.lower()}"
        if key in seen:
            continue
        seen.add(key)

        relevance = min(100, int(max(overall, title_score) + (10 if doi_match else 0)))
        suggestions.append({
            "title": cand_title,
            "year": cand_year,
            "authors": cand_authors,
            "doi": cand_doi,
            "url": _candidate_url(cand, cand_doi),
            "score": meta.get("score", 0),
            "title_score": meta.get("title_score", 0),
            "author_similarity": meta.get("author_similarity", 0),
            "year_match": meta.get("year_match", 0),
            "doi_match": doi_match,
            "type": "correction",
            "source": cand.get("source", "scholarly_lookup"),
            "relevance": relevance,
            "candidate_quality": _quality_label(max(relevance, int(strict_fit.get("reference_fit_score", 0) or 0))),
            "query_used": query,
            "query_strategy": "strict_reference_metadata_correction" if strict_reference else "reference_metadata_correction",
            "suggestion_type": "reference_correction_candidate",
            "match_basis": {
                "reference_title": title,
                "reference_title_similarity": strict_fit.get("reference_title_similarity", 0),
                "reference_title_overlap_terms": strict_fit.get("reference_title_overlap_terms", []),
                "reference_year_match": strict_fit.get("reference_year_match", False),
                "reference_doi_match": strict_fit.get("reference_doi_match", False),
                "reference_author_similarity": strict_fit.get("reference_author_similarity", 0),
                "reference_fit_score": strict_fit.get("reference_fit_score", 0),
            },
            "reason": "Suggested from strict reference metadata. Accept only after confirming author, year, title and DOI.",
            "candidate_type": "exact_reference_candidate" if strict_reference else "reference_metadata_candidate",
            "problem_detected": "The original reference could not be verified automatically.",
            "why_it_failed": "Automated verification could not confirm the reference with sufficient confidence from the original metadata.",
            "recommended_action": "Use Manual Verify in the Verification tab to confirm the candidate before accepting it.",
            "manual_search_query": query,
            "action_target": "verification_tab_manual_verify",
            "manual_verify_available": True,
            "review_required": True,
            "is_real_source": True,
            **_candidate_publication_metadata(cand),
        })

    suggestions.sort(key=lambda x: (x.get("doi_match", False), x.get("score", 0), x.get("title_score", 0)), reverse=True)
    diag["ranked_candidate_count"] = len(suggestions)
    returned = suggestions[:top_k]
    diag["returned_candidate_count"] = len(returned)
    diag["outcome"] = "candidates_returned" if returned else "no_strong_identity_candidate_returned"
    return returned

def build_claim_validation_queries(claim: str, source_title: str = "", doi: str = "", citation: str = "") -> List[Dict[str, str]]:
    """
    Build transparent queries for deep claim validation.
    The worker/claim checker may use these to fetch source metadata without
    adding latency to the initial verification path.
    """
    queries = []
    def add(q: str, strategy: str):
        q = _clean_search_text(q, max_len=220)
        if len(q) >= 6 and q.lower() not in {x["query"].lower() for x in queries}:
            queries.append({"query": q, "strategy": strategy})
    if doi:
        add(doi, "doi_exact")
    if source_title:
        add(source_title, "matched_source_title")
    for q in _build_context_queries(claim, citation, use_citation_hint=False):
        add(q["query"], q["strategy"])
    return queries[:5]
