# claim_checker.py

from typing import List, Dict, Any, Tuple
import re
from citation_suggester import extract_context, split_citation_cluster, suggest_from_context, build_claim_validation_queries
from claim_support_scorer import score_claim_support, fetch_openalex_metadata_by_doi

CLAIM_CHECKER_VERSION = "1.5.35"
CLAIM_CHECKER_BUILD = "commercial-2026-05-21-style-aware-claim-context-extraction-FINAL"

def clean_extracted_claim_text(claim: str) -> str:
    """
    Remove citation residue from extracted claim text.
    Keeps the manuscript claim but removes fragments such as:
    'Button et al., 2013).' or 'Lohr, 2010).'
    """
    import re

    claim = claim or ""
    claim = re.sub(r"\s+", " ", claim).strip()

    # Remove leading broken closing punctuation from citation clusters
    claim = re.sub(r"^[\s\)\]\.,;:]+", "", claim)

    # Remove leading single citation fragment
    claim = re.sub(
        r"^[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+et\s+al\.?)?\s*,?\s*(?:19|20)\d{2}[a-z]?\)?[\s\.,;:]*",
        "",
        claim,
        flags=re.I
    )

    # Remove leading multiple citation fragments
    claim = re.sub(
        r"^(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s*(?:&|and)\s*[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+)?\s*,?\s*(?:19|20)\d{2}[a-z]?\s*;?\s*)+\)?[\s\.,;:]*",
        "",
        claim,
        flags=re.I
    )

    return claim.strip(" ,;:-")



# ============================================================
# STYLE-AWARE CLAIM CONTEXT EXTRACTION
# ============================================================
# This keeps the existing architecture intact: claim_checker.py remains the
# claim-support module, worker.py still calls build_claim_support_rows(), and
# citation_suggester.extract_context() remains available as a fallback.
# The improvement is that Claim Support now first uses the same citation-style
# logic already used by the worker for Recovery: parenthetical = left claim,
# narrative = right claim, numeric = full sentence with marker removed.

_SUP_DIGITS_CLAIM = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_SUP_TO_NORMAL_CLAIM = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻−–—", "0123456789----")
_NORMAL_TO_SUP_CLAIM = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")
_YEAR_RE_CLAIM = r"(?:19|20)\d{2}[a-z]?"


def _claim_style_token(style: str = "") -> str:
    s = str(style or "auto").strip().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[\s\-/]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    aliases = {
        "author_year": "author_year",
        "apa": "author_year",
        "harvard": "author_year",
        "chicago": "author_year",
        "chicago_author_date": "author_year",
        "apa_harvard_chicago": "author_year",
        "ieee": "numeric_square",
        "ieee_square": "numeric_square",
        "ieee_square_bracket": "numeric_square",
        "vancouver": "numeric_square",
        "vancouver_square": "numeric_square",
        "vancouver_square_bracket": "numeric_square",
        "nlm": "numeric_square",
        "nlm_square": "numeric_square",
        "elsevier": "numeric_square",
        "elsevier_square": "numeric_square",
        "elsevier_numbered": "numeric_square",
        "springer": "numeric_square",
        "springer_square": "numeric_square",
        "springer_numbered": "numeric_square",
        "square_numeric": "numeric_square",
        "numeric_square": "numeric_square",
        "ama": "numeric_superscript",
        "ama_superscript": "numeric_superscript",
        "nature": "numeric_superscript",
        "nature_superscript": "numeric_superscript",
        "rsc": "numeric_superscript",
        "rsc_superscript": "numeric_superscript",
        "acs": "numeric_superscript",
        "acs_superscript": "numeric_superscript",
        "elsevier_superscript": "numeric_superscript",
        "superscript_numeric": "numeric_superscript",
        "numeric_superscript": "numeric_superscript",
        "vancouver_round": "numeric_round",
        "acs_round": "numeric_round",
        "round_numeric": "numeric_round",
        "numeric_round": "numeric_round",
    }
    return aliases.get(s, s or "auto")


def _claim_style_family(style: str = "") -> str:
    token = _claim_style_token(style)
    if token in {"numeric_square", "numeric_superscript", "numeric_round"}:
        return token
    if token == "auto":
        return "auto"
    return "author_year"


def _claim_style_hint(result: Dict[str, Any] = None, row: Dict[str, Any] = None) -> str:
    result = result or {}
    row = row or {}
    return (
        row.get("selected_style")
        or row.get("style_family")
        or row.get("style")
        or result.get("selected_style")
        or result.get("style_family")
        or result.get("style")
        or (result.get("summary") or {}).get("selected_style")
        or "auto"
    )


def _to_claim_superscript(num_text: str) -> str:
    return str(num_text or "").translate(_NORMAL_TO_SUP_CLAIM)


def _normalise_claim_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _sentence_span_around(text: str, pos: int, window: int = 600) -> Tuple[int, int]:
    """Find safe sentence-like boundaries around a citation position."""
    if not text:
        return 0, 0
    if pos < 0:
        return 0, min(len(text), window)

    left = max(
        text.rfind(".", 0, pos),
        text.rfind("?", 0, pos),
        text.rfind("!", 0, pos),
        text.rfind(";", 0, pos),
        text.rfind("\n", 0, pos),
    )
    left = 0 if left == -1 else left + 1

    rights = [
        text.find(".", pos),
        text.find("?", pos),
        text.find("!", pos),
        text.find(";", pos),
        text.find("\n", pos),
    ]
    rights = [r for r in rights if r != -1]
    right = min(rights) + 1 if rights else min(len(text), pos + window)

    # Avoid extreme accidental captures in PDFs with missing punctuation.
    if right - left > window:
        left = max(0, pos - window // 2)
        right = min(len(text), pos + window // 2)

    return left, right


def _extract_author_year_parts(citation: str) -> Tuple[str, str, str]:
    c = str(citation or "").strip().strip("()[] ")
    m = re.search(rf"\b({_YEAR_RE_CLAIM})\b", c, flags=re.I)
    year = m.group(1) if m else ""
    author_part = c[:m.start()].strip(" ,;()[]") if m else c.strip(" ,;()[]")
    tokens = [
        t for t in re.findall(r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]{2,}", author_part)
        if t.lower() not in {"et", "al", "and"}
    ]
    author = tokens[0] if tokens else ""
    return author_part, author, year


def _numeric_citation_variants(citation: str) -> List[str]:
    raw = str(citation or "").strip()
    norm = raw.translate(_SUP_TO_NORMAL_CLAIM)
    nums = re.findall(r"\d{1,4}", norm)
    variants = set()

    if not nums:
        return [raw] if raw else []

    joined_comma = ",".join(nums)
    joined_comma_sp = ", ".join(nums)
    joined_dash = f"{nums[0]}–{nums[-1]}" if len(nums) > 1 else nums[0]
    joined_hyphen = f"{nums[0]}-{nums[-1]}" if len(nums) > 1 else nums[0]

    for n in set(nums + [joined_comma, joined_comma_sp, joined_dash, joined_hyphen]):
        if not n:
            continue
        variants.add(n)
        variants.add(f"[{n}]")
        variants.add(f"({n})")
        sup = _to_claim_superscript(n.replace(", ", ","))
        variants.add(sup)
        variants.add(f" {sup}")
        variants.add(f",{sup}")
        variants.add(f".{sup}")

    variants.add(raw)
    return sorted((v for v in variants if v), key=len, reverse=True)


def _author_year_citation_variants(citation: str) -> List[str]:
    raw = str(citation or "").strip()
    base = raw.strip("() ")
    variants = {raw, base, f"({base})"}

    author_part, author, year = _extract_author_year_parts(raw)
    if year and author_part:
        variants.add(f"{author_part}, {year}")
        variants.add(f"({author_part}, {year})")
        variants.add(f"{author_part} ({year})")
        variants.add(f"{author_part}, {year[:4]}")
        variants.add(f"({author_part}, {year[:4]})")
        variants.add(f"{author_part} ({year[:4]})")

        if " and " in author_part.lower():
            amp_author = re.sub(r"\s+and\s+", " & ", author_part, flags=re.I)
            variants.add(f"{amp_author}, {year}")
            variants.add(f"({amp_author}, {year})")
            variants.add(f"{amp_author} ({year})")

        if "&" in author_part:
            and_author = author_part.replace("&", "and")
            variants.add(f"{and_author}, {year}")
            variants.add(f"({and_author}, {year})")
            variants.add(f"{and_author} ({year})")

    return sorted((v for v in variants if v), key=len, reverse=True)


def _is_numeric_citation(citation: str, style_hint: str = "") -> bool:
    family = _claim_style_family(style_hint)
    raw = str(citation or "").strip()
    norm = raw.translate(_SUP_TO_NORMAL_CLAIM)
    if family.startswith("numeric_"):
        return True
    if not re.search(r"\d", norm):
        return False
    if re.search(rf"\b{_YEAR_RE_CLAIM}\b", norm):
        return False
    return bool(re.fullmatch(r"[\s\[\](),.;:\-–—0-9⁰¹²³⁴⁵⁶⁷⁸⁹]+", raw))


def _find_variant_match(text: str, variants: List[str]):
    for variant in variants or []:
        variant = str(variant or "").strip()
        if not variant:
            continue
        m = re.search(re.escape(variant), text, flags=re.I)
        if m:
            return m.start(), m.end(), text[m.start():m.end()]
    return -1, -1, ""


def _find_author_year_fallback_match(text: str, citation: str):
    author_part, author, year = _extract_author_year_parts(citation)
    if not (text and author and year):
        return -1, -1, ""

    author_re = re.escape(author)
    year_re = re.escape(year[:4])
    patterns = [
        rf"\([^)]{{0,220}}{author_re}[^)]{{0,220}}{year_re}[a-z]?[^)]{{0,220}}\)",
        rf"{author_re}\s*(?:et\s+al\.?)?\s*\(\s*{year_re}[a-z]?\s*\)",
        rf"{author_re}\s*(?:et\s+al\.?)?\s*,?\s*{year_re}[a-z]?",
    ]

    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.start(), m.end(), text[m.start():m.end()]
    return -1, -1, ""


def _expand_author_year_parenthetical_cluster(text: str, start: int, end: int):
    if not text or start < 0:
        return None

    left_paren = text.rfind("(", 0, start + 1)
    right_paren = text.find(")", end)
    if left_paren == -1 or right_paren == -1:
        return None

    prev_boundary = max(
        text.rfind(".", 0, start),
        text.rfind("?", 0, start),
        text.rfind("!", 0, start),
        text.rfind("\n", 0, start),
    )
    if prev_boundary > left_paren:
        return None

    cluster = text[left_paren:right_paren + 1]
    if len(cluster) > 350:
        return None
    if not re.search(rf"\b{_YEAR_RE_CLAIM}\b", cluster):
        return None
    if not re.search(r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]{2,}", cluster):
        return None

    return left_paren, right_paren + 1, cluster


def _remove_citation_markers(sentence: str, citation: str, variants: List[str], style_hint: str = "") -> str:
    out = str(sentence or "")

    # Remove exact variants first.
    for variant in sorted(set(variants or []), key=len, reverse=True):
        if not variant or len(variant.strip()) == 0:
            continue
        out = re.sub(re.escape(variant), " ", out, flags=re.I)

    if _is_numeric_citation(citation, style_hint):
        out = re.sub(r"\[(?:\s*\d{1,4}\s*(?:[-,;–—]\s*\d{1,4}\s*)*)\]", " ", out)
        out = re.sub(r"\(\s*\d{1,4}\s*(?:[-,;–—]\s*\d{1,4}\s*)*\)", " ", out)
        out = re.sub(r"(?<=[A-Za-z\)\]])\s*[⁰¹²³⁴⁵⁶⁷⁸⁹]+(?:\s*[,;\-–—]\s*[⁰¹²³⁴⁵⁶⁷⁸⁹]+)*", " ", out)
    else:
        out = re.sub(rf"\([^()]*\b{_YEAR_RE_CLAIM}\b[^()]*\)", " ", out, flags=re.I)

    out = re.sub(r"\s+([.,;:!?])", r"\1", out)
    out = re.sub(r"\s+", " ", out)
    return _normalise_claim_text(out).strip(" ,;:-.")


def _extract_claim_style_aware(
    full_text: str,
    citation: str,
    row: Dict[str, Any] = None,
    style_hint: str = "auto",
    window: int = 600,
) -> Tuple[str, str]:
    """
    Extract the manuscript claim around a citation using citation-family rules.

    Rules:
    - Parenthetical author-year: use the left-side claim before the citation cluster.
    - Narrative author-year: use the right-side claim after Author (Year).
    - Numeric styles: use the full citation-bearing sentence, then remove the marker.
    - Clusters: extract the claim once and reuse it for all citations in the cluster.
    """
    text = str(full_text or "")
    citation = str(citation or "").strip()
    if not text or not citation:
        return "", ""

    style_hint = style_hint or (row or {}).get("selected_style") or "auto"
    is_numeric = _is_numeric_citation(citation, style_hint)

    if is_numeric:
        variants = _numeric_citation_variants(citation)
        start, end, matched = _find_variant_match(text, variants)
        if start >= 0:
            left, right = _sentence_span_around(text, start, window=window)
            sentence = text[left:right]
            claim = _remove_citation_markers(sentence, citation, variants, style_hint)
            if len(claim) >= 10:
                return claim, f"style_aware_{_claim_style_family(style_hint)}_sentence"
        return "", ""

    variants = _author_year_citation_variants(citation)
    start, end, matched = _find_variant_match(text, variants)

    if start < 0:
        start, end, matched = _find_author_year_fallback_match(text, citation)

    if start < 0:
        return "", ""

    expanded = _expand_author_year_parenthetical_cluster(text, start, end)
    if expanded:
        start, end, matched = expanded

    left, right = _sentence_span_around(text, start, window=window)
    sentence = text[left:right]

    is_parenthetical = matched.strip().startswith("(") and matched.strip().endswith(")")

    if is_parenthetical:
        claim = text[left:start]
        claim = _remove_citation_markers(claim, citation, variants, style_hint)
        if len(claim) >= 10:
            return claim, "style_aware_parenthetical_left_context"

        # If the left side is too short, use the full sentence without the citation.
        claim = _remove_citation_markers(sentence, citation, variants, style_hint)
        if len(claim) >= 10:
            return claim, "style_aware_parenthetical_sentence_fallback"

    else:
        after = text[end:right]
        after = _remove_citation_markers(after, citation, variants, style_hint)
        if len(after) >= 10:
            return after, "style_aware_narrative_right_context"

        claim = _remove_citation_markers(sentence, citation, variants, style_hint)
        if len(claim) >= 10:
            return claim, "style_aware_narrative_sentence_fallback"

    return "", ""


def force_claim_candidate(full_text: str, citation: str, row: Dict[str, Any], window: int = 600, style_hint: str = "auto"):
    """
    Always return a claim candidate once a citation exists.

    Priority, without changing the existing architecture:
    1. Use local style-aware extractor for parenthetical, narrative, numeric, and clusters.
    2. Use context fields from the reconciliation row.
    3. Use citation_suggester.extract_context() as compatibility fallback.
    4. Use author-year fallback sentence search.
    5. Return extraction_failed marker.
    """
    citation = (citation or "").strip()
    row = row or {}
    style_hint = style_hint or row.get("selected_style") or row.get("style_family") or row.get("style") or "auto"

    # 1. Style-aware extractor shared by Recovery/Validation logic in this module.
    claim, claim_source = _extract_claim_style_aware(
        full_text=full_text,
        citation=citation,
        row=row,
        style_hint=style_hint,
        window=window,
    )
    claim = (claim or "").strip()
    if len(claim) >= 10:
        return claim, claim_source

    # 2. Reconciliation row fallback.
    for key in ["context", "sentence", "citation_context", "nearby_text", "left_context", "right_context"]:
        val = (row.get(key, "") or "").strip()
        if len(val) >= 10:
            return val, f"row_{key}"

    # 3. Existing citation_suggester fallback retained for compatibility.
    try:
        claim = extract_context(full_text, citation, window=window)
        claim = (claim or "").strip()
        if len(claim) >= 10:
            return claim, "extract_context_fallback"
    except Exception:
        pass

    # 4. Last manuscript-text fallback using author-year pieces.
    years = re.findall(rf"{_YEAR_RE_CLAIM}", citation)
    year = years[0] if years else ""

    tokens = re.findall(r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]{2,}", citation)
    stop = {"And", "Et", "Al"}
    authors = [t for t in tokens if t not in stop]
    author = authors[0] if authors else ""

    if full_text and author and year:
        pattern = rf"{re.escape(author)}[^.?!;]{{0,180}}(?:\(\s*)?{re.escape(year[:4])}[a-z]?(?:\s*\))?"
        m = re.search(pattern, full_text, flags=re.I)

        if m:
            pos = m.start()
            left, right = _sentence_span_around(full_text, pos, window=window)
            candidate = full_text[left:right]
            candidate = _normalise_claim_text(candidate).strip(" ,;:-")

            if len(candidate) >= 10:
                return candidate, "fallback_sentence_window"

    # 5. Final forced output.
    return (
        f"Claim could not be extracted from the manuscript context. Citation searched: {citation}",
        "extraction_failed"
    )

def suggest_alternative_sources_for_claim(
    claim: str,
    citation: str = "",
    current_source_title: str = "",
    top_k: int = 3
):
    """
    Suggest alternative sources when the current matched source gives no evidence,
    weak evidence, or is excluded.

    Commercial logic:
    - The query is claim-first, not author/year-first. This avoids simply finding
      the same weak cited source again.
    - Author/year from the existing citation is not allowed to dominate the search.
    - Candidates are kept only when the smart suggester reports a usable relevance
      signal from title/claim concept overlap or strong citation metadata.
    - Output remains review-only and does not replace citations automatically.
    """
    claim = (claim or "").strip()
    citation = (citation or "").strip()
    current_source_title = (current_source_title or "").strip().lower()

    if len(claim) < 20:
        return []

    # Do not create alternatives from extraction-failure placeholders.
    if claim.lower().startswith("claim could not be extracted"):
        return []

    try:
        candidates = suggest_from_context(
            context=claim,
            citation=citation,
            top_k=top_k + 8,
            use_citation_hint=False,   # important: search by claim, not by the weak source's author/year
            min_relevance=55,
        )
    except Exception as e:
        print(f"[ALT SOURCE ERROR] {citation}: {e}")
        return []

    suggestions = []
    seen = set()

    for cand in candidates or []:
        title = (cand.get("title", "") or "").strip()
        doi = (cand.get("doi", "") or "").strip()
        year = cand.get("year", "")
        authors = cand.get("authors", []) or []
        relevance = float(cand.get("relevance", 0) or 0)

        if not title or relevance < 55:
            continue

        title_key = title.lower()

        # Avoid suggesting the same weak/current source again.
        if current_source_title and (
            title_key == current_source_title
            or title_key in current_source_title
            or current_source_title in title_key
        ):
            continue

        key = f"{title_key}|{year}|{doi.lower()}"
        if key in seen:
            continue
        seen.add(key)

        suggestions.append({
            "title": title,
            "year": year,
            "authors": authors,
            "doi": doi,
            "url": cand.get("url", ""),
            "relevance": relevance,
            "candidate_quality": cand.get("candidate_quality", "possible_candidate"),
            "query_used": cand.get("query_used", ""),
            "query_strategy": cand.get("query_strategy", "claim_keyword_query"),
            "match_basis": cand.get("match_basis", {}),
            "suggestion_type": "alternative_source",
            "review_required": True,
            "reason": (
                "Suggested from the manuscript claim because the current matched source "
                "gave weak, insufficient, or no evidence. Review before using."
            ),
        })

        if len(suggestions) >= top_k:
            break

    return suggestions


def build_claim_validation_query_plan(claim: str, source_title: str = "", doi: str = "", citation: str = "") -> Dict[str, Any]:
    """
    Expose the deep validation search plan for debugging and UI transparency.
    The heavy lookup can still run later in the deep enrichment/payment path.
    """
    return {
        "claim": (claim or "")[:500],
        "source_title": source_title or "",
        "doi": doi or "",
        "citation": citation or "",
        "queries": build_claim_validation_queries(
            claim=claim,
            source_title=source_title,
            doi=doi,
            citation=citation,
        ),
        "note": "Queries are used for deep claim-support validation; they are not run in the fast path unless enabled by the worker.",
    }

def build_claim_support_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build claim-to-source support rows.
    Never silently drops a row.

    Clear separation:
    - claim field: what was extracted, or "No claim extracted"
    - source_title field: matched source, or reason source is unavailable
    - support_status field: support decision, including "no_evidence_found"
    """
    out = []
    MAX_ALT_SOURCE_ROWS = 100
    alt_source_count = 0
    
    full_text = (
        result.get("main_text", "")
        or result.get("full_text", "")
        or result.get("data", {}).get("main_text", "")
        or result.get("data", {}).get("full_text", "")
        or ""
    )
    print("[CLAIM DEBUG] full_text length:", len(full_text or ""))
    c2r_rows = result.get("reconciliation_intext_to_reference", []) or []

    online_verification = result.get("online_verification", {}) or {}
    verify_rows = online_verification.get("rows", []) or []

    verified_lookup = {}
    all_verify_lookup = {}

    for row in verify_rows:
        ref = row.get("reference", "") or ""
        if ref:
            all_verify_lookup[ref] = row

        status = row.get("status", "")
        if status in ["verified", "likely"] and ref:
            verified_lookup[ref] = row

    for row in c2r_rows:
        matched_ref = row.get("matched_reference", "") or ""
        citation_text = (
            row.get("in_text", "")
            or row.get("citation", "")
            or row.get("citation_in_text", "")
            or ""
        )

        if not matched_ref or not citation_text:
            claim = ""
            claim_source = "mapping_incomplete"
        
            if citation_text:
                claim, claim_source = force_claim_candidate(
                    full_text=full_text,
                    citation=citation_text,
                    row=row,
                    window=600,
                    style_hint=_claim_style_hint(result, row)
                )
                claim = clean_extracted_claim_text(claim)
        
            if not claim or claim_source == "extraction_failed":
                claim = "Claim not extracted because citation-reference mapping was incomplete."
        
            out.append({
                "citation": citation_text,
                "claim": claim,
                "claim_source": claim_source,
                "reference": matched_ref,
                "source_title": "No source found",
                "doi": "",
                "support_score": 0,
                "support_status": "mapping_incomplete",
                "evidence_used": "none",
                "alternative_sources": [],
                "title_overlap": 0,
                "abstract_overlap": 0,
                "keyword_overlap": 0,
                "direction_overlap": 0,
                "relation_overlap": 0,
                "partial_support": False,
                "concept_matches": [],
                "score_explanation": "Citation-reference mapping was incomplete, so source-support checking could not be performed."
            })
            continue

        vr = verified_lookup.get(matched_ref)

        if not vr:
            fallback_vr = all_verify_lookup.get(matched_ref, {})
            mismatch_flag = int(fallback_vr.get("author_mismatch_flag", 0))
        
            source_label = "No source found"
            note = "No trusted source evidence was available."
        
            if fallback_vr:
                source_label = fallback_vr.get("matched_title", "") or "No source found"
                if mismatch_flag == 1:
                    source_label = "Source excluded, author mismatch"
                    note = "Matched source was excluded because of author mismatch."
                elif fallback_vr.get("status") in {"needs_review", "not_found"}:
                    note = f"Matched source was not trusted because verification status is {fallback_vr.get('status')}."
        
            claim, claim_source = force_claim_candidate(
                full_text=full_text,
                citation=citation_text,
                row=row,
                window=600,
                style_hint=_claim_style_hint(result, row)
            )
            claim = clean_extracted_claim_text(claim)
        
            alternative_sources = []
        
            
        
            out.append({
                "citation": citation_text,
                "claim": claim,
                "claim_source": claim_source,
                "reference": matched_ref,
                "source_title": source_label,
                "doi": fallback_vr.get("doi", "") or "",
                "support_score": 0,
                "support_status": "no_evidence_found",
                "evidence_used": "none",
                "alternative_sources": alternative_sources,
                "title_overlap": 0,
                "abstract_overlap": 0,
                "keyword_overlap": 0,
                "direction_overlap": 0,
                "relation_overlap": 0,
                "partial_support": False,
                "concept_matches": [],
                "match_note": fallback_vr.get("match_note", ""),
                "score_explanation": note
            })
            continue

        citation_items = split_citation_cluster(citation_text)

        # First extract claim using the full citation or full citation cluster.
        # This is important because individual split citations may not appear
        # as standalone text in the manuscript. The style-aware extractor handles
        # parenthetical, narrative, numeric-square, numeric-superscript, and
        # numeric-round contexts before falling back to citation_suggester.
        style_hint = _claim_style_hint(result, row)
        cluster_claim, cluster_claim_source = _extract_claim_style_aware(
            full_text=full_text,
            citation=citation_text,
            row=row,
            style_hint=style_hint,
            window=600,
        )
        cluster_claim = (cluster_claim or "").strip()
        if not cluster_claim or len(cluster_claim) < 10:
            try:
                cluster_claim = extract_context(full_text, citation_text, window=600)
                cluster_claim = (cluster_claim or "").strip()
                cluster_claim_source = "extract_context_fallback" if cluster_claim else ""
            except Exception:
                cluster_claim = ""
                cluster_claim_source = ""
        
        for cit in citation_items:
            claim = cluster_claim
            claim_source = cluster_claim_source or ("cluster_context" if claim and len(claim) >= 10 else "")
            claim = clean_extracted_claim_text(claim)
        
            # If cluster-level extraction fails, use the forced claim candidate fallback.
            if not claim or len(claim) < 10:
                claim, claim_source = force_claim_candidate(
                    full_text=full_text,
                    citation=cit,
                    row=row,
                    window=600,
                    style_hint=_claim_style_hint(result, row)
                )
                claim = clean_extracted_claim_text(claim)
                            
            source_title = vr.get("matched_title", "") or ""
            doi = vr.get("doi", "") or ""
            
            if claim_source == "extraction_failed":
                out.append({
                    "citation": cit,
                    "claim": claim,
                    "claim_source": claim_source,
                    "reference": matched_ref,
                    "source_title": source_title or "No source found",
                    "doi": doi,
                    "support_score": 0,
                    "support_status": "claim_not_extracted",
                    "evidence_used": "claim_extraction_failed",
                    "title_overlap": 0,
                    "abstract_overlap": 0,
                    "keyword_overlap": 0,
                    "direction_overlap": 0,
                    "relation_overlap": 0,
                    "partial_support": False,
                    "concept_matches": [],
                    "match_note": vr.get("match_note", ""),
                    "score_explanation": "A citation was detected, but the system could not extract a meaningful manuscript claim around it."
                })
                continue

            # Fast mode: do not fetch OpenAlex metadata during the main verification flow.
            # Claim support will use the already-verified source title only.
            metadata = {}
            source_abstract = ""
            source_concepts = []

            if not source_title and not source_abstract and not source_concepts:
                alternative_sources = suggest_alternative_sources_for_claim(
                    claim=claim,
                    citation=cit,
                    current_source_title=source_title,
                    top_k=3
                )
            
                out.append({
                    "citation": cit,
                    "claim": claim,
                    "claim_source": claim_source,
                    "reference": matched_ref,
                    "source_title": "No source found",
                    "doi": doi,
                    "support_score": 0,
                    "support_status": "no_evidence_found",
                    "evidence_used": "none",
                    "alternative_sources": alternative_sources,
                    "title_overlap": 0,
                    "abstract_overlap": 0,
                    "keyword_overlap": 0,
                    "direction_overlap": 0,
                    "relation_overlap": 0,
                    "partial_support": False,
                    "concept_matches": [],
                    "match_note": vr.get("match_note", ""),
                    "score_explanation": "No usable source title, abstract, or concepts were available for support checking."
                })
                continue

            support = score_claim_support(
                claim=claim,
                source_title=source_title,
                source_abstract=source_abstract,
                source_concepts=source_concepts,
                source_metadata=metadata
            )
            support_status = support.get("status", "insufficient_evidence")
            support_score = support.get("score", 0)
            
            alternative_sources = []
            
            alternative_sources = []
            out.append({
                "citation": cit,
                "claim": claim,
                "claim_source": claim_source,
                "reference": matched_ref,
                "source_title": source_title,
                "doi": doi,
                "support_score": support_score,
                "support_status": support_status,
                "alternative_sources": alternative_sources,
                "evidence_used": (
                    "title+abstract+concepts"
                    if source_concepts else
                    ("title+abstract" if source_abstract else "title_only")
                ),
                "title_overlap": support.get("title_overlap", 0),
                "abstract_overlap": support.get("abstract_overlap", 0),
                "keyword_overlap": support.get("keyword_overlap", 0),
                "direction_overlap": support.get("direction_overlap", 0),
                "relation_overlap": support.get("relation_overlap", 0),
                "partial_support": support.get("partial_support", False),
                "concept_matches": support.get("concept_matches", []),
                "match_note": vr.get("match_note", ""),
                "score_explanation": (
                    f"title={support.get('title_overlap', 0)}, "
                    f"abstract={support.get('abstract_overlap', 0)}, "
                    f"keyword={support.get('keyword_overlap', 0)}, "
                    f"direction={support.get('direction_overlap', 0)}, "
                    f"relation={support.get('relation_overlap', 0)}"
                )
            })

    # Add transparent query plans for deep claim validation.
    # These are not executed in the fast path; they explain what the deep layer
    # should search when the row is paid/enriched or manually triggered.
    for _row in out:
        try:
            _row.setdefault(
                "validation_query_plan",
                build_claim_validation_query_plan(
                    claim=_row.get("claim", ""),
                    source_title=_row.get("source_title", ""),
                    doi=_row.get("doi", ""),
                    citation=_row.get("citation", ""),
                )
            )
            _row.setdefault("review_required", True)
        except Exception:
            pass

    return out
