# claim_checker.py

from typing import List, Dict, Any, Tuple
import re
from citation_suggester import extract_context, split_citation_cluster, suggest_from_context, build_claim_validation_queries
from claim_support_scorer import score_claim_support, fetch_openalex_metadata_by_doi

CLAIM_CHECKER_VERSION = "1.5.36"
CLAIM_CHECKER_BUILD = "commercial-2026-05-21-forced-claim-sourcewise-judgement-FINAL"

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

    # 5. Final output. Do not return a placeholder sentence as a claim.
    # Placeholder text must never be scored as if it were a manuscript claim.
    return "", "extraction_failed"

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


# ============================================================
# SOURCE-WISE CLAIM SUPPORT HELPERS
# ============================================================
# These helpers keep the same architecture but correct the judgement logic:
# one claim can be extracted from a citation cluster, then that same claim is
# compared separately with each linked source. No group-level judgement is made.

_CLAIM_FAILURE_PHRASES = (
    "claim could not be extracted",
    "claim not extracted",
    "review the cited sentence manually",
    "extraction_failed",
    "mapping was incomplete",
)


def _is_claim_extraction_failure(claim: str, claim_source: str = "") -> bool:
    claim_l = str(claim or "").strip().lower()
    source_l = str(claim_source or "").strip().lower()
    if not claim_l:
        return True
    if source_l in {"extraction_failed", "claim_extraction_failed"}:
        return True
    return any(p in claim_l for p in _CLAIM_FAILURE_PHRASES)


def _claim_extraction_confidence(claim_source: str = "") -> str:
    source = str(claim_source or "").lower()
    if source in {"extraction_failed", "claim_extraction_failed", "mapping_incomplete"}:
        return "failed"
    if source.startswith("style_aware_") or source in {"cluster_context"}:
        return "high"
    if source in {"extract_context", "extract_context_fallback", "fallback_sentence_window"}:
        return "medium"
    if source.startswith("row_") or source.startswith("forced_"):
        return "low"
    return "low" if source else "failed"


def _normalise_lookup_text(text: str) -> str:
    text = str(text or "").translate(_SUP_TO_NORMAL_CLAIM).lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\bdoi\s*:?\s*10\.\S+", " ", text, flags=re.I)
    text = re.sub(r"\b10\.\d{4,9}/\S+", " ", text, flags=re.I)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _citation_key(citation: str) -> str:
    text = str(citation or "").translate(_SUP_TO_NORMAL_CLAIM).strip().lower()
    text = text.strip("[](){} ")
    text = re.sub(r"\s+", " ", text)
    return text


def _numeric_id_from_citation(citation: str) -> str:
    raw = str(citation or "").translate(_SUP_TO_NORMAL_CLAIM)
    if re.search(rf"\b{_YEAR_RE_CLAIM}\b", raw):
        return ""
    nums = re.findall(r"\d{1,4}", raw)
    return nums[0] if nums else ""


def _extract_author_year_for_match(citation: str) -> Tuple[str, str]:
    _author_part, author, year = _extract_author_year_parts(citation)
    return (author or "").lower(), (year or "")[:4]


def _iter_reference_entries(result: Dict[str, Any]):
    result = result or {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    for key in [
        "references_raw", "references", "reference_entries", "reference_list",
        "raw_references", "reference_texts", "refs"
    ]:
        value = result.get(key) or data.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    txt = item.strip()
                elif isinstance(item, dict):
                    txt = (
                        item.get("reference") or item.get("raw") or item.get("text")
                        or item.get("reference_text") or item.get("full_reference") or ""
                    ).strip()
                else:
                    txt = str(item or "").strip()
                if txt:
                    yield txt
        elif isinstance(value, str) and value.strip():
            for line in re.split(r"\n+", value):
                line = line.strip()
                if len(line) >= 12:
                    yield line


def _build_citation_to_reference_lookup(c2r_rows, result):
    """Map individual citation forms to matched reference strings."""
    lookup = {}
    for row in c2r_rows or []:
        if not isinstance(row, dict):
            continue
        ref = row.get("matched_reference") or row.get("reference") or row.get("reference_text") or ""
        if not ref:
            continue
        citation = row.get("in_text") or row.get("citation") or row.get("citation_in_text") or ""
        if not citation:
            continue
        split_items = split_citation_cluster(citation)
        # If this row is a cluster but only one matched_reference was provided,
        # do NOT assign that same reference to every split item. Individual
        # citations should resolve through their own c2r rows or the reference
        # list. This prevents one claim cluster being judged repeatedly against
        # the same source.
        if len(split_items) == 1:
            key = _citation_key(split_items[0])
            if key and key not in lookup:
                lookup[key] = ref

        key = _citation_key(citation)
        if key and key not in lookup:
            lookup[key] = ref

    # Add a conservative reference-list resolver so clustered citations can map
    # to different sources even when the engine returned a single cluster row.
    refs = list(_iter_reference_entries(result))
    for ref in refs:
        ref_norm = ref.lower()
        year_match = re.search(rf"\b({_YEAR_RE_CLAIM})\b", ref_norm)
        year = year_match.group(1)[:4] if year_match else ""
        if not year:
            continue
        # First surname before the year is normally enough for author-year matching.
        author_segment = ref_norm[:year_match.start()]
        surnames = [x for x in re.findall(r"[a-zà-öø-ÿ'\-]{3,}", author_segment) if x not in {"and", "the", "for", "with", "from"}]
        if surnames:
            key = _citation_key(f"{surnames[0]}, {year}")
            lookup.setdefault(key, ref)

    return lookup


def _resolve_reference_for_citation(citation: str, current_row: Dict[str, Any], lookup: Dict[str, str], result: Dict[str, Any], default_ref: str = "") -> str:
    key = _citation_key(citation)
    if key and key in lookup:
        return lookup[key]

    # Author-year fallback against reference list.
    author, year = _extract_author_year_for_match(citation)
    if author and year:
        for ref in _iter_reference_entries(result):
            ref_l = ref.lower()
            if author in ref_l[:180] and year in ref_l:
                return ref

    # Numeric fallback: find reference-list entry by reference number, then list position.
    num = _numeric_id_from_citation(citation)
    if num:
        refs = list(_iter_reference_entries(result))
        for ref in refs:
            if re.match(rf"^\s*(?:\[{re.escape(num)}\]|{re.escape(num)}[\.)])\s+", ref):
                return ref
        try:
            idx = int(num) - 1
            if 0 <= idx < len(refs):
                return refs[idx]
        except Exception:
            pass

    return default_ref or (current_row or {}).get("matched_reference", "") or ""


def _build_verification_lookups(verify_rows):
    exact_all, norm_all = {}, {}
    exact_trusted, norm_trusted = {}, {}
    for vr in verify_rows or []:
        if not isinstance(vr, dict):
            continue
        refs = [
            vr.get("reference"), vr.get("original_reference"), vr.get("matched_reference"),
            vr.get("source_title"), vr.get("matched_title"), vr.get("title"),
        ]
        status = str(vr.get("status") or "").lower()
        for ref in refs:
            if not ref:
                continue
            ref_s = str(ref)
            exact_all.setdefault(ref_s, vr)
            norm_all.setdefault(_normalise_lookup_text(ref_s), vr)
            if status in {"verified", "likely"}:
                exact_trusted.setdefault(ref_s, vr)
                norm_trusted.setdefault(_normalise_lookup_text(ref_s), vr)
    return exact_trusted, norm_trusted, exact_all, norm_all


def _lookup_verification_row(reference: str, exact_map: Dict[str, Any], norm_map: Dict[str, Any]) -> Dict[str, Any]:
    if not reference:
        return {}
    if reference in exact_map:
        return exact_map[reference]
    return norm_map.get(_normalise_lookup_text(reference), {}) or {}


def _empty_support_row(
    *, citation, claim, claim_source, reference, source_title="No source found", doi="",
    support_status="no_evidence_found", evidence_used="none", score_explanation="",
    match_note="", verification_status="unknown", cluster_id="", cluster_size=1,
):
    return {
        "claim_cluster_id": cluster_id,
        "cluster_size": cluster_size,
        "judgement_scope": "single_source_against_shared_claim",
        "citation": citation,
        "claim": claim,
        "claim_source": claim_source,
        "claim_extraction_confidence": _claim_extraction_confidence(claim_source),
        "reference": reference,
        "source_title": source_title or "No source found",
        "doi": doi or "",
        "support_score": 0,
        "support_status": support_status,
        "evidence_used": evidence_used,
        "alternative_sources": [],
        "title_overlap": 0,
        "abstract_overlap": 0,
        "keyword_overlap": 0,
        "direction_overlap": 0,
        "relation_overlap": 0,
        "partial_support": False,
        "concept_matches": [],
        "match_note": match_note or "",
        "verification_status": verification_status,
        "source_verification_status": verification_status,
        "review_required": True,
        "score_explanation": score_explanation or "Human review is required.",
    }

def build_claim_support_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build claim-to-source support rows.

    Commercial interpretation rule:
    - A clustered citation contains one manuscript claim.
    - The claim is extracted once from the full cluster.
    - The same claim is then compared separately with each linked source.
    - No group-level judgement is produced.
    - Placeholder/extraction-failure text is never scored.
    """
    out = []

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

    citation_ref_lookup = _build_citation_to_reference_lookup(c2r_rows, result)
    verified_exact, verified_norm, all_exact, all_norm = _build_verification_lookups(verify_rows)

    for row_index, row in enumerate(c2r_rows, start=1):
        if not isinstance(row, dict):
            continue

        citation_text = (
            row.get("in_text", "")
            or row.get("citation", "")
            or row.get("citation_in_text", "")
            or ""
        )
        default_ref = row.get("matched_reference", "") or ""

        if not citation_text:
            continue

        style_hint = _claim_style_hint(result, row)
        citation_items = split_citation_cluster(citation_text) or [citation_text]
        cluster_id = f"claim_cluster_{row_index}"
        cluster_size = len(citation_items)

        # Extract the claim once from the full citation/cluster. If this fails,
        # individual items can still try forced extraction, but the output is kept
        # source-wise rather than group-level.
        cluster_claim, cluster_claim_source = force_claim_candidate(
            full_text=full_text,
            citation=citation_text,
            row=row,
            window=600,
            style_hint=style_hint,
        )
        cluster_claim = clean_extracted_claim_text(cluster_claim)

        for cit in citation_items:
            claim = cluster_claim
            claim_source = cluster_claim_source

            if _is_claim_extraction_failure(claim, claim_source):
                claim, claim_source = force_claim_candidate(
                    full_text=full_text,
                    citation=cit,
                    row=row,
                    window=600,
                    style_hint=style_hint,
                )
                claim = clean_extracted_claim_text(claim)

            source_ref = _resolve_reference_for_citation(
                citation=cit,
                current_row=row,
                lookup=citation_ref_lookup,
                result=result,
                default_ref=default_ref,
            )

            if not source_ref:
                display_claim = claim or "Claim could not be extracted because no citation-bearing sentence or row context was available."
                out.append(_empty_support_row(
                    citation=cit,
                    claim=display_claim,
                    claim_source=claim_source or "mapping_incomplete",
                    reference="",
                    source_title="No source found",
                    support_status="mapping_incomplete",
                    evidence_used="none",
                    score_explanation="Citation-reference mapping was incomplete, so source-support checking could not be performed.",
                    verification_status="unknown",
                    cluster_id=cluster_id,
                    cluster_size=cluster_size,
                ))
                continue

            trusted_vr = _lookup_verification_row(source_ref, verified_exact, verified_norm)
            fallback_vr = _lookup_verification_row(source_ref, all_exact, all_norm)
            active_vr = trusted_vr or fallback_vr or {}
            verification_status = str(active_vr.get("status") or "unknown").lower()
            source_title = active_vr.get("matched_title") or active_vr.get("title") or active_vr.get("source_title") or ""
            doi = active_vr.get("doi", "") or ""
            match_note = active_vr.get("match_note", "") or ""

            # If no real claim exists, never score the row. This prevents
            # 'Claim could not be extracted...' from becoming related evidence.
            if _is_claim_extraction_failure(claim, claim_source):
                display_claim = claim or "Claim could not be extracted because no citation-bearing sentence or row context was available."
                out.append(_empty_support_row(
                    citation=cit,
                    claim=display_claim,
                    claim_source=claim_source or "extraction_failed",
                    reference=source_ref,
                    source_title=source_title or "No source found",
                    doi=doi,
                    support_status="claim_not_extracted",
                    evidence_used="none",
                    score_explanation="Claim support was not scored because no manuscript claim was extracted.",
                    match_note=match_note,
                    verification_status=verification_status,
                    cluster_id=cluster_id,
                    cluster_size=cluster_size,
                ))
                continue

            # Only verified/likely sources can be scored in the fast path. Other
            # statuses remain review-required with no support score.
            if not trusted_vr:
                reason = "No trusted source evidence was available."
                if fallback_vr:
                    if int(fallback_vr.get("author_mismatch_flag", 0) or 0) == 1:
                        source_title = "Source excluded, author mismatch"
                        reason = "Matched source was excluded because of author mismatch."
                    elif verification_status in {"needs_review", "not_found", "offline"}:
                        reason = f"Matched source was not trusted because verification status is {verification_status}."

                out.append(_empty_support_row(
                    citation=cit,
                    claim=claim,
                    claim_source=claim_source,
                    reference=source_ref,
                    source_title=source_title or "No source found",
                    doi=doi,
                    support_status="no_evidence_found",
                    evidence_used="none",
                    score_explanation=reason,
                    match_note=match_note,
                    verification_status=verification_status,
                    cluster_id=cluster_id,
                    cluster_size=cluster_size,
                ))
                continue

            # Fast mode: do not fetch OpenAlex metadata here. Claim support uses
            # the verified/likely source title only; deeper validation can run later.
            if not source_title:
                alternative_sources = suggest_alternative_sources_for_claim(
                    claim=claim,
                    citation=cit,
                    current_source_title=source_title,
                    top_k=3,
                )
                row_out = _empty_support_row(
                    citation=cit,
                    claim=claim,
                    claim_source=claim_source,
                    reference=source_ref,
                    source_title="No source found",
                    doi=doi,
                    support_status="no_evidence_found",
                    evidence_used="none",
                    score_explanation="No usable source title, abstract, or concepts were available for support checking.",
                    match_note=match_note,
                    verification_status=verification_status,
                    cluster_id=cluster_id,
                    cluster_size=cluster_size,
                )
                row_out["alternative_sources"] = alternative_sources
                out.append(row_out)
                continue

            support = score_claim_support(
                claim=claim,
                source_title=source_title,
                source_abstract="",
                source_concepts=[],
                source_metadata={},
            ) or {}

            out.append({
                "claim_cluster_id": cluster_id,
                "cluster_size": cluster_size,
                "judgement_scope": "single_source_against_shared_claim",
                "citation": cit,
                "claim": claim,
                "claim_source": claim_source,
                "claim_extraction_confidence": _claim_extraction_confidence(claim_source),
                "reference": source_ref,
                "source_title": source_title,
                "doi": doi,
                "support_score": support.get("score", 0),
                "support_status": support.get("status", "insufficient_evidence"),
                "alternative_sources": [],
                "evidence_used": "title_only",
                "title_overlap": support.get("title_overlap", 0),
                "abstract_overlap": support.get("abstract_overlap", 0),
                "keyword_overlap": support.get("keyword_overlap", 0),
                "direction_overlap": support.get("direction_overlap", 0),
                "relation_overlap": support.get("relation_overlap", 0),
                "partial_support": support.get("partial_support", False),
                "concept_matches": support.get("concept_matches", []),
                "match_note": match_note,
                "verification_status": verification_status,
                "source_verification_status": verification_status,
                "review_required": True,
                "score_explanation": (
                    f"title={support.get('title_overlap', 0)}, "
                    f"abstract={support.get('abstract_overlap', 0)}, "
                    f"keyword={support.get('keyword_overlap', 0)}, "
                    f"direction={support.get('direction_overlap', 0)}, "
                    f"relation={support.get('relation_overlap', 0)}"
                ),
            })

    # Add transparent query plans for deep claim validation. These are not
    # executed in the fast path; they explain what the paid/deep layer should search.
    for _row in out:
        try:
            _row.setdefault(
                "validation_query_plan",
                build_claim_validation_query_plan(
                    claim="" if _is_claim_extraction_failure(_row.get("claim", ""), _row.get("claim_source", "")) else _row.get("claim", ""),
                    source_title=_row.get("source_title", ""),
                    doi=_row.get("doi", ""),
                    citation=_row.get("citation", ""),
                )
            )
            _row.setdefault("review_required", True)
        except Exception:
            pass

    return out
