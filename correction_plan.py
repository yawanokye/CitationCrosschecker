"""Convert detailed engine results into a student-friendly correction plan."""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Dict, List
import re

from reference_formatter import format_reference, validate_reference
from reference_safety import is_review_instruction, identity_text, notice_kind
import hashlib
from evidence_resolution import (
    build_evidence_resolution_workspace,
    claim_requires_resolution,
    normalise_claim_support_status,
    normalise_verification_status,
    verification_requires_resolution,
    verification_status_explanation,
)


def _rows(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("citation", "citation_in_text", "reference", "claim", "claim_text", "sentence", "passage"):
            if value.get(key):
                return str(value[key])
    return str(value or "")


def _main_text(result: Dict[str, Any]) -> str:
    return str(result.get("main_text") or result.get("full_text") or result.get("document_text") or result.get("text") or "")


def _locate(text: str, needle: str, supplied: Any = None) -> Dict[str, Any]:
    if isinstance(supplied, dict) and supplied:
        return supplied
    needle = re.sub(r"\s+", " ", str(needle or "")).strip()
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n+", text or "") if p.strip()]
    section = "Document body"
    words_before = 0
    for p_index, paragraph in enumerate(paragraphs):
        if len(paragraph.split()) <= 14 and (paragraph.isupper() or re.match(r"^(chapter|section|\d+(?:\.\d+)*)\b", paragraph, re.I)):
            section = paragraph[:160]
        if needle and (needle.lower() in paragraph.lower() or paragraph.lower() in needle.lower()):
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", paragraph) if s.strip()]
            sentence_number = next((i + 1 for i, sentence in enumerate(sentences) if needle[:80].lower() in sentence.lower() or sentence[:80].lower() in needle.lower()), 1)
            return {
                "page_estimate": words_before // 500 + 1,
                "section": section,
                "paragraph": p_index + 1,
                "sentence": sentence_number,
                "page_note": "Estimated from extracted text. Confirm against the annotated manuscript.",
            }
        words_before += len(paragraph.split())
    return {"section": section, "location_note": "Exact text location was not recovered. Use the evidence text to search the manuscript."}


def _metadata(row: Any) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    keys = ("source", "status", "canonical_status", "support_status", "canonical_support_status", "citation", "claim", "claim_text", "source_title", "doi", "matched_doi", "matched_title", "matched_authors", "matched_year", "matched_journal", "matched_volume", "matched_issue", "matched_pages", "url", "evidence_url", "confidence_reason", "score", "support_score")
    return {key: row.get(key) for key in keys if row.get(key) not in (None, "", [])}


def _detected_reference_style(result: Dict[str, Any]) -> str:
    summary = result.get("summary") or {}

    def canonical(value: Any) -> str:
        raw = str(value or "").strip().lower().replace("-", "_")
        if not raw or raw == "auto":
            return ""
        if "apa6" in raw or "apa 6" in raw:
            return "apa6"
        if "apa" in raw:
            return "apa7"
        if "harvard" in raw:
            return "harvard"
        if "superscript" in raw or any(name in raw for name in ("ama", "nature", "rsc", "acs")):
            return "numeric_superscript"
        if "round" in raw:
            return "numeric_round"
        if "numeric" in raw or any(name in raw for name in ("vancouver", "ieee", "nlm", "elsevier", "springer")):
            return "numeric_square"
        return ""

    # A user's explicit selection must win over secondary detector fields. The
    # old implementation concatenated every hint, so a stale numeric detector
    # value could override an explicitly selected APA style.
    selected = result.get("selected_style") or summary.get("selected_style")
    selected_style = canonical(selected)
    if selected_style:
        return selected_style

    for value in (
        result.get("style"), result.get("style_family"), result.get("citation_style"),
        summary.get("style"), summary.get("style_family"), summary.get("citation_style"),
    ):
        detected = canonical(value)
        if detected:
            return detected
    return "apa7"


def _normalise_identity(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _parse_original_reference(original: str) -> Dict[str, Any]:
    """Conservatively recover display metadata without replacing source identity."""
    text = re.sub(r"^\s*(?:\[\d+\]|\(\d+\)|\d+[.)])\s*", "", str(original or "")).strip()
    doi_match = re.search(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\d{4,9}/\S+)", text, re.I)
    doi = doi_match.group(1).rstrip(".,;\u2060") if doi_match else ""
    isbn_match = re.search(r"\bISBN(?:-1[03])?\s*:?\s*([0-9Xx-]{10,20})", text, re.I)
    isbn = re.sub(r"[^0-9Xx]", "", isbn_match.group(1)) if isbn_match else ""
    url_match = re.search(r"https?://\S+", text, re.I)
    url = url_match.group(0).rstrip(".,;\u2060") if url_match else ""
    clean = text
    if isbn_match:
        clean = clean.replace(isbn_match.group(0), "").strip()
    if url:
        clean = clean.replace(url_match.group(0), "").strip()
    clean = re.sub(r"\s*doi:\s*10\.\S+", "", clean, flags=re.I).strip()
    match = re.match(r"^(.*?)\s*\((\d{4}[a-z]?|n\.d\.)\)\.\s*(.+)$", clean, re.I)
    if not match:
        numeric = re.match(r"^(.*?)\.\s+(.+?)\.\s+(.+?)\.\s+(\d{4});\s*([^(:\s]+)(?:\(([^)]+)\))?(?:\s*:\s*([^.;]+))?", clean)
        if numeric:
            return {"authors": numeric.group(1).strip(), "year": numeric.group(4), "title": numeric.group(2).strip(), "source": numeric.group(3).strip(), "volume": numeric.group(5).rstrip("."), "issue": numeric.group(6) or "", "pages": (numeric.group(7) or "").strip(), "publisher": "", "edition": "", "doi": doi, "isbn": isbn, "url": url, "type": "article", "parse_confidence": "high"}
        return {"authors": [], "year": "", "title": "", "source": "", "publisher": "", "doi": doi, "isbn": isbn, "url": url, "type": "unknown", "parse_confidence": "low"}
    author_block, year, remainder = (part.strip() for part in match.groups())
    segments = re.split(r"\.\s+", remainder, maxsplit=1)
    title_part = segments[0].strip().rstrip(".")
    publication = segments[1].strip().rstrip(".") if len(segments) > 1 else ""
    edition_match = re.search(r"\(([^()]*(?:ed\.|edition))\)\s*$", title_part, re.I)
    edition = edition_match.group(1).strip() if edition_match else ""
    if edition_match:
        title_part = title_part[:edition_match.start()].strip()
    article = re.match(r"^(.+?),\s*(\d+)(?:\(([^)]+)\))?(?:,\s*|:\s*)([A-Za-z0-9]+(?:\s*[–—-]\s*[A-Za-z0-9]+)?)", publication)
    ref = {"authors": author_block, "year": year, "title": title_part, "source": "", "volume": "", "issue": "", "pages": "", "publisher": "", "edition": edition, "doi": doi, "isbn": isbn, "url": url}
    if article:
        ref.update({"source": article.group(1).strip(), "volume": article.group(2), "issue": article.group(3) or "", "pages": article.group(4), "type": "article", "parse_confidence": "high"})
    else:
        ref.update({"publisher": publication, "type": "book" if edition or publication else "report", "parse_confidence": "high" if publication else "medium"})
    return ref


def _reference_identity_gate(original_ref: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    original_doi = _normalise_identity(original_ref.get("doi"))
    matched_doi = _normalise_identity(row.get("matched_doi") or row.get("doi"))
    original_isbn = re.sub(r"[^0-9x]", "", str(original_ref.get("isbn") or "").casefold())
    matched_isbn_raw = row.get("matched_isbn") or row.get("isbn") or ""
    if isinstance(matched_isbn_raw, list):
        matched_isbn_raw = matched_isbn_raw[0] if matched_isbn_raw else ""
    matched_isbn = re.sub(r"[^0-9x]", "", str(matched_isbn_raw).casefold())
    original_title = _normalise_identity(original_ref.get("title"))
    matched_title = _normalise_identity(row.get("matched_title") or row.get("title"))
    original_year = _normalise_identity(original_ref.get("year"))
    matched_year = _normalise_identity(row.get("matched_year") or row.get("year"))
    original_author = _normalise_identity(original_ref.get("authors")).split(" ")[0] if original_ref.get("authors") else ""
    matched_authors = row.get("matched_authors_full") or row.get("matched_authors") or row.get("authors") or []
    matched_author_text = " ".join(str(value) for value in matched_authors) if isinstance(matched_authors, list) else str(matched_authors or "")
    author_match = bool(original_author and original_author in _normalise_identity(matched_author_text).split())
    title_score = SequenceMatcher(None, original_title, matched_title).ratio() if original_title and matched_title else 0.0
    doi_exact = bool(original_doi and matched_doi and original_doi == matched_doi)
    isbn_exact = bool(original_isbn and matched_isbn and original_isbn == matched_isbn)
    year_match = bool(original_year and matched_year and original_year == matched_year)
    role_conflict = bool(notice_kind(original_ref.get("title"))) != bool(notice_kind(row.get("matched_title") or row.get("title")))
    accepted = (doi_exact or isbn_exact or (title_score >= .95 and author_match and year_match)) and not role_conflict
    reason = "Exact DOI" if doi_exact else ("Exact ISBN" if isbn_exact else ("Title, author and year agree" if accepted else "External record may represent a different publication"))
    return {"accepted": accepted, "doi_exact": doi_exact, "isbn_exact": isbn_exact, "title_similarity": round(title_score, 3), "author_match": author_match, "year_match": year_match, "reason": reason}


def _reference_audit(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    verification = result.get("online_verification") or {}
    rows = _rows(verification.get("rows") if isinstance(verification, dict) else verification)
    style = _detected_reference_style(result)
    audited = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        original = str(row.get("reference") or row.get("original_reference") or "").strip()
        if not original:
            continue
        original_ref = _parse_original_reference(original)
        identity = _reference_identity_gate(original_ref, row)
        # Preserve author, year and title from the manuscript. External metadata
        # may fill publication fields only after the source identity gate passes.
        ref = dict(original_ref)
        if identity["accepted"]:
            ref["source"] = ref.get("source") or row.get("matched_container_title") or row.get("matched_journal") or row.get("journal") or row.get("source") or ""
            ref["volume"] = ref.get("volume") or row.get("matched_volume") or row.get("volume") or ""
            ref["issue"] = ref.get("issue") or row.get("matched_issue") or row.get("issue") or ""
            ref["pages"] = ref.get("pages") or row.get("matched_pages") or row.get("pages") or ""
            ref["doi"] = ref.get("doi") or row.get("matched_doi") or row.get("doi") or ""
            ref["publisher"] = ref.get("publisher") or row.get("publisher") or ""
        missing = validate_reference(ref)
        if not ref.get("authors"):
            missing.append("author information")
        if not ref.get("title"):
            missing.append("title")
        missing = list(dict.fromkeys(missing))
        formatted_markup = re.sub(r"\s+", " ", format_reference(ref, style)).strip()
        segments = []
        for segment_index, segment in enumerate(re.split(r"(\*[^*]+\*)", formatted_markup)):
            if not segment:
                continue
            italic = segment.startswith("*") and segment.endswith("*")
            segments.append({"text": segment[1:-1] if italic else segment, "italic": italic})
        formatted = re.sub(r"\*", "", formatted_markup).strip()
        if re.search(r"\bet\s+al\b", str(original_ref.get("authors") or ""), re.I):
            # A truncated author list cannot be reconstructed from surname-only
            # matches. Preserve it until the user supplies complete metadata.
            formatted = original
            segments = [{"text": original, "italic": False}]
        number_match = re.match(r"^\s*(?:\[(\d+)\]|\((\d+)\)|(\d+)[.)])\s*", original)
        if style.startswith("numeric_") and number_match and formatted != original:
            number = next(group for group in number_match.groups() if group)
            marker = f"[{number}]" if style == "numeric_square" else (f"({number})" if style == "numeric_round" else f"{number}.")
            formatted = f"{marker} {formatted}".strip()
            segments.insert(0, {"text": marker + " ", "italic": False})
        elif style.startswith("numeric_") and not number_match:
            marker = f"[{index + 1}]" if style == "numeric_square" else (f"({index + 1})" if style == "numeric_round" else f"{index + 1}.")
            formatted = f"{marker} {formatted}".strip()
            segments.insert(0, {"text": marker + " ", "italic": False})
        comparable_original = re.sub(r"\s+", " ", original).strip().rstrip(".")
        comparable_formatted = re.sub(r"\s+", " ", formatted).strip().rstrip(".")
        audited.append({
            "index": index + 1,
            "original": original,
            "formatted": formatted,
            "missing": missing,
            "needs_formatting": bool(formatted and comparable_original != comparable_formatted),
            "style": style,
            "status": row.get("status") or "needs_review",
            "identity": identity,
            "url": (row.get("matched_url") or row.get("evidence_url") or row.get("url") or (f"https://doi.org/{ref['doi']}" if ref.get("doi") else "")) if identity["accepted"] else (ref.get("url") or (f"https://doi.org/{ref['doi']}" if ref.get("doi") else "")),
            "metadata": ref,
            "format_segments": segments,
        })
    return audited


def build_correction_plan(result: Dict[str, Any]) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    manuscript_text = _main_text(result)
    saved_decisions = result.get("correction_decisions") or {}
    saved_candidates = result.get("correction_source_candidates") or {}
    saved_search_reports = result.get("correction_source_search_reports") or {}

    def add(
        priority: str,
        category: str,
        title: str,
        rows: List[Any],
        action: str,
        why: str,
        confidence: str = "high",
        limit: int = 200,
        id_prefix: str = "",
    ):
        for i, row in enumerate(rows[:limit]):
            item_id = f"{id_prefix or category}-{i + 1}"
            if category == "claim_support" and isinstance(row, dict):
                evidence = str(row.get("claim") or row.get("claim_text") or row.get("context") or row.get("sentence") or "")[:900]
            else:
                evidence = _text(row)[:900]
            decision = saved_decisions.get(item_id) or {}
            candidates = saved_candidates.get(item_id) or (row.get("suggestions") if isinstance(row, dict) else []) or (row.get("suggested_sources") if isinstance(row, dict) else []) or []
            proposed = decision.get("proposed_replacement") or ((row.get("proposed_replacement") or row.get("suggested_reference") or row.get("formatted_reference") or "") if isinstance(row, dict) else "")
            available_actions = {
                "missing_reference": ["find_source", "add_reference"],
                "citation_needed": ["find_source", "insert_citation"],
                "uncited_reference": ["cite_reference", "delete_reference"],
                "claim_support": ["find_source", "add_supporting_citation", "revise_claim"],
                "source_verification": ["find_source", "replace_reference", "mark_valid_not_indexed"],
                "reference_incomplete": ["find_source", "replace_reference"],
                "reference_identity_conflict": ["find_source", "replace_reference"],
            }.get(category, ["accept", "reject", "ignore"])
            items.append({
                "id": item_id,
                "priority": (row.get("_priority_override") or priority) if isinstance(row, dict) else priority,
                "category": category,
                "title": title,
                "what_is_wrong": title,
                "why_it_matters": why,
                "evidence": evidence,
                "location": _locate(manuscript_text, evidence, row.get("location") if isinstance(row, dict) else None),
                "recommended_action": action,
                "coach_explanation": f"{why} {action}",
                "supporting_metadata": _metadata(row),
                "confidence": (row.get("confidence") or row.get("score") or confidence) if isinstance(row, dict) else confidence,
                "evidence_link": (row.get("url") or row.get("evidence_url") or row.get("matched_url") or "") if isinstance(row, dict) else "",
                "proposed_replacement": proposed,
                "secondary_replacement": decision.get("secondary_replacement") or "",
                "original_text": decision.get("original_text") or evidence,
                "source_candidates": candidates,
                "approved_source": decision.get("approved_source") or {},
                "approved_action": decision.get("action") or "",
                "track_operation": decision.get("track_operation") or "replace",
                "available_actions": available_actions,
                "auto_apply_allowed": bool(isinstance(row, dict) and category in {"reference_metadata", "formatting"} and float(row.get("confidence", 0) or 0) >= .95),
                "decision": decision.get("decision", "pending"),
                "decision_note": decision.get("note", ""),
            })

    add("critical", "missing_reference", "In-text citation has no matching reference", _rows(result.get("missing_in_references")), "Add and verify the complete reference or correct the in-text citation.", "Readers cannot identify or verify the cited source.")
    add("important", "uncited_reference", "Reference is not cited in the manuscript", _rows(result.get("uncited_references")), "Cite the source where it supports the argument or remove it from the reference list.", "An unused reference weakens reference-list accuracy and may suggest padding or an editing oversight.")

    verification = result.get("online_verification") or {}
    verification_rows = _rows(verification.get("rows") if isinstance(verification, dict) else verification)
    risky = []
    for row in verification_rows:
        if not isinstance(row, dict) or not verification_requires_resolution(row):
            continue
        enriched = dict(row)
        enriched["canonical_status"] = normalise_verification_status(row)
        enriched["confidence_reason"] = enriched.get("confidence_reason") or verification_status_explanation(row)
        enriched["_priority_override"] = (
            "critical"
            if enriched["canonical_status"] in {"serious_identity_conflict"}
            else "important"
        )
        risky.append(enriched)
    add(
        "critical", "source_verification", "Reference requires evidence resolution", risky,
        "Search the exact bibliographic identity first, open the candidate, and verify title, authors, year, journal, volume, pages and DOI before approval.",
        "Readers must be able to identify the intended source. A failed lookup or not-found result does not mean the source is fabricated.",
        "medium",
    )

    claim_rows = _rows(result.get("claim_support"))
    claim_groups = {
        "mapping_incomplete": [],
        "weak_or_unclear": [],
        "insufficient_evidence": [],
    }
    for row in claim_rows:
        if not isinstance(row, dict) or not claim_requires_resolution(row):
            continue
        enriched = dict(row)
        enriched["canonical_support_status"] = normalise_claim_support_status(row)
        claim_groups.setdefault(enriched["canonical_support_status"], []).append(enriched)
    add(
        "critical", "claim_support", "Claim-to-source mapping is incomplete",
        claim_groups["mapping_incomplete"],
        "Find the intended source or a suitable supporting source, open it, and confirm the exact claim before approval.",
        "Without a completed mapping, CiteIntegrity cannot determine which publication should be checked against the claim.",
        "medium",
        id_prefix="claim-mapping-incomplete",
    )
    add(
        "important", "claim_support", "Claim has weak or unclear support",
        claim_groups["weak_or_unclear"],
        "Revise or qualify the claim, or find a stronger source and confirm that it directly supports the wording.",
        "A source on the same topic may not support the specific strength, scope or causal wording of the claim.",
        "medium",
        id_prefix="claim-weak-support",
    )
    add(
        "critical", "claim_support", "Claim has insufficient supporting evidence",
        claim_groups["insufficient_evidence"],
        "Add a verified supporting source, narrow the claim, or remove unsupported wording after review.",
        "Unsupported factual, empirical or causal claims create a direct submission risk.",
        "medium",
        id_prefix="claim-insufficient-evidence",
    )
    add("critical", "citation_needed", "Claim may require a citation", _rows(result.get("citation_needed_claims")), "Add an appropriate source, qualify the statement, or identify it as a result of the present study.", "Unsupported factual, empirical or causal claims reduce scholarly credibility.", "medium")

    for audit in _reference_audit(result):
        item_id = f"reference-incomplete-{audit['index']}" if audit["missing"] else f"reference-style-{audit['index']}"
        decision = saved_decisions.get(item_id) or {}
        if not audit["identity"].get("accepted") and audit["identity"].get("title_similarity", 0) > 0:
            conflict_id = f"reference-identity-conflict-{audit['index']}"
            conflict_decision = saved_decisions.get(conflict_id) or {}
            items.append({
                "id": conflict_id, "priority": "critical" if audit["identity"].get("title_similarity", 0) < .70 else "important", "category": "reference_identity_conflict",
                "title": "Online metadata appears to describe a different publication",
                "what_is_wrong": audit["identity"].get("reason"),
                "why_it_matters": "Using this metadata could replace the intended source with an unrelated article, review, book or report.",
                "evidence": audit["original"], "original_text": audit["original"], "location": _locate(manuscript_text, audit["original"]),
                "recommended_action": "Keep the original source identity. Find and verify an exact DOI, ISBN or matching title-author-year record before replacement.",
                "coach_explanation": "Formatting and source replacement are separate. A style correction must never change the cited work.",
                "supporting_metadata": {"detected_style": audit["style"], "source_identity_check": audit["identity"]},
                "confidence": "high", "evidence_link": audit["url"], "proposed_replacement": "",
                "source_candidates": saved_candidates.get(conflict_id) or [], "approved_source": conflict_decision.get("approved_source") or {},
                "approved_action": conflict_decision.get("action") or "", "track_operation": conflict_decision.get("track_operation") or "replace",
                "available_actions": ["find_source", "replace_reference", "reject", "ignore"], "auto_apply_allowed": False,
                "decision": conflict_decision.get("decision", "pending"), "decision_note": conflict_decision.get("note", ""),
            })
        if audit["missing"]:
            items.append({
                "id": item_id, "priority": "important", "category": "reference_incomplete",
                "title": "Reference metadata is incomplete",
                "what_is_wrong": "Missing: " + ", ".join(audit["missing"]),
                "why_it_matters": "A complete reference allows readers to identify and retrieve the source.",
                "evidence": audit["original"], "original_text": audit["original"],
                "location": _locate(manuscript_text, audit["original"]),
                "recommended_action": "Find and verify the complete source metadata before replacing this reference.",
                "coach_explanation": "Do not guess missing bibliographic fields. Open and verify a candidate source before approval.",
                "supporting_metadata": {"detected_style": audit["style"], "missing_fields": audit["missing"], "verification_status": audit["status"], "source_identity_check": audit["identity"]},
                "confidence": "high", "evidence_link": audit["url"], "proposed_replacement": "",
                "source_candidates": saved_candidates.get(item_id) or [], "approved_source": decision.get("approved_source") or {},
                "approved_action": decision.get("action") or "", "track_operation": decision.get("track_operation") or "replace",
                "available_actions": ["find_source", "replace_reference", "reject", "ignore"], "auto_apply_allowed": False,
                "decision": decision.get("decision", "pending"), "decision_note": decision.get("note", ""),
            })
        elif audit["needs_formatting"]:
            items.append({
                "id": item_id, "priority": "important", "category": "reference_style",
                "title": "Reference does not follow the detected style",
                "what_is_wrong": f"The entry differs from the detected {audit['style']} reference-list format.",
                "why_it_matters": "A consistent reference list improves readability and submission readiness.",
                "evidence": audit["original"], "original_text": audit["original"],
                "location": _locate(manuscript_text, audit["original"]),
                "recommended_action": "Replace with: " + audit["formatted"],
                "coach_explanation": "Confirm that the formatted entry preserves the intended source, then approve it for Track Changes.",
                "supporting_metadata": {"detected_style": audit["style"], "verification_status": audit["status"], "source_identity_check": audit["identity"], "external_metadata_used": bool(audit["identity"].get("accepted")), "reference_format_segments": audit["format_segments"]},
                "confidence": "high" if str(audit["status"]).lower() in {"verified", "likely"} else "medium",
                "evidence_link": audit["url"], "proposed_replacement": audit["formatted"],
                "secondary_replacement": "", "approved_action": decision.get("action") or "",
                "track_operation": decision.get("track_operation") or "replace", "available_actions": ["accept", "reject", "ignore"],
                "auto_apply_allowed": False, "decision": decision.get("decision", "pending"), "decision_note": decision.get("note", ""),
            })

    autofix = result.get("autofix") or {}
    suggestions = autofix.get("suggestions") or {}
    seen_autofixes = set()
    for kind, category in (("citations", "citation_formatting"), ("references", "reference_metadata")):
        for i, row in enumerate(_rows(suggestions.get(kind))):
            if not isinstance(row, dict) or not row.get("original") or not row.get("suggested"):
                continue
            issue_type = str(row.get("issue_type") or row.get("type") or "").lower()
            if issue_type in {"missing_reference", "uncited_reference"} or issue_type.endswith("_uncited_numbered_reference"):
                # Missing/uncited records already have first-class correction
                # items with the correct cite/delete workflow.
                continue
            original = re.sub(r"\s+", " ", str(row.get("original") or "")).strip()
            suggested = re.sub(r"\s+", " ", str(row.get("suggested") or "")).strip()
            if is_review_instruction(suggested) or row.get("fix_type") == "review_required":
                continue
            confidence_value = float(row.get("confidence", 0) or 0)
            reason_lower = str(row.get("reason") or "").lower()
            speculative = any(marker in reason_lower for marker in (
                "may be a typo", "possible author-name variation", "unique reference year",
                "review before changing", "citation was not matched", "similar author",
            ))
            # Do not turn fuzzy author/year guesses into correction actions.
            # They belong in manual verification only after external metadata
            # confirms that both forms identify the same source.
            if original.lower() == suggested.lower() or confidence_value < .80 or speculative:
                continue
            dedupe_key = (category, original.lower(), suggested.lower())
            if dedupe_key in seen_autofixes:
                continue
            seen_autofixes.add(dedupe_key)
            item_id = f"{category}-{i + 1}"
            decision = saved_decisions.get(item_id) or {}
            bibliographic_safe = category == "reference_metadata" and confidence_value >= .95 and row.get("fix_type") not in {"review_required", "source_replacement", "new_citation"}
            items.append({
                "id": item_id,
                "priority": "important" if confidence_value >= .85 else "optional",
                "category": category,
                "title": "High-confidence bibliographic correction" if bibliographic_safe else "Formatting correction requires review",
                "what_is_wrong": row.get("reason") or row.get("issue_type") or "The citation or reference differs from the recommended form.",
                "why_it_matters": "Accurate and consistent bibliographic details help readers retrieve the intended source.",
                "evidence": original[:900],
                "location": _locate(manuscript_text, original),
                "recommended_action": f"Replace with: {row.get('suggested')}",
                "coach_explanation": "Compare the original and suggested forms. Accept only when they refer to the same source and preserve the author's intended citation.",
                "supporting_metadata": _metadata(row),
                "confidence": confidence_value,
                "evidence_link": row.get("url") or "",
                "proposed_replacement": suggested,
                "secondary_replacement": decision.get("secondary_replacement") or "",
                "original_text": original,
                "approved_action": decision.get("action") or "",
                "track_operation": decision.get("track_operation") or "replace",
                "auto_apply_allowed": bibliographic_safe,
                "decision": decision.get("decision", "pending"),
                "decision_note": decision.get("note", ""),
            })

    source_risks = (result.get("source_risk_review") or {}).get("risks") or []
    for row in source_risks:
        risk_name = str(row.get("risk") or "").strip().lower()
        if risk_name in {"metadata_not_found", "publication_status_unchecked"}:
            identity = identity_text(row.get("original_reference") or row.get("reference"))
            existing = next((item for item in items
                             if item.get("category") in {"source_verification", "reference_incomplete"}
                             and identity and identity_text(item.get("evidence")) == identity), None)
            if existing:
                existing.setdefault("supporting_metadata", {}).setdefault("source_risks", []).append(dict(row))
                if risk_name == "publication_status_unchecked":
                    existing["why_it_matters"] += " Publication status also remains unchecked."
                continue
        stable_key = "|".join([risk_name, str(row.get("reference_index") or ""), str(row.get("doi") or row.get("reference") or "")])
        item_id = "source-risk-" + hashlib.sha256(stable_key.encode()).hexdigest()[:16]
        decision = saved_decisions.get(item_id) or {}
        items.append({
            "id": item_id,
            "priority": row.get("priority", "important"),
            "category": "source_risk",
            "title": str(row.get("risk", "Source requires review")).replace("_", " ").title(),
            "what_is_wrong": str(row.get("risk", "Source requires review")).replace("_", " ").title(),
            "why_it_matters": row.get("qualification") or "This metadata signal may affect the reliability or suitability of the cited source.",
            "evidence": str(row.get("reference") or "")[:900],
            "location": {"section": "References", "reference_index": row.get("reference_index"), "location_note": "See the original reference and DOI in Source Verification."},
            "recommended_action": row.get("action", "Review the source metadata manually."),
            "coach_explanation": row.get("action", "Review the source metadata manually."),
            "supporting_metadata": {**(row.get("metadata") or {}), "publication_status": row.get("publication_status"), "events": row.get("events") or [], "doi": row.get("doi"), "data_version": row.get("data_version")},
            "confidence": row.get("confidence", "medium"),
            "evidence_link": row.get("url", ""),
            "auto_apply_allowed": False,
            "decision": decision.get("decision", "pending"),
            "decision_note": decision.get("note", ""),
        })

    voice_settings = result.get("academic_voice_settings") or {}
    voice_enabled = voice_settings.get("enabled") is True
    voice = result.get("academic_voice_review") or {}
    voice_items_added = 0
    for voice_index, row in enumerate(_rows(voice.get("signals") if voice_enabled and isinstance(voice, dict) else [])):
        # Citation-support questions belong to the dedicated citation-needed and
        # claim-support checks. Repeating low-confidence voice heuristics in the
        # correction plan creates duplicate, high-volume false positives.
        if row.get("signal") in {"claim_without_nearby_citation", "possible_claim_needing_source_review"}:
            continue
        if voice_items_added >= 12:
            break
        item_id = f"voice-{voice_index + 1}"
        decision = saved_decisions.get(item_id) or {}
        items.append({
            "id": item_id,
            "priority": "optional",
            "category": "academic_voice",
            "title": row.get("signal", "Writing pattern requires review").replace("_", " ").title(),
            "what_is_wrong": row.get("why_flagged", "The passage contains a writing pattern that needs review."),
            "why_it_matters": "Clear, specific and well-supported prose helps readers evaluate the author's own argument.",
            "evidence": row.get("passage", "")[:900],
            "location": row.get("location"),
            "recommended_action": row.get("recommended_action", "Review the passage for clarity and accurate support."),
            "coach_explanation": row.get("recommended_action", "Review the passage for clarity and accurate support."),
            "supporting_metadata": {},
            "confidence": row.get("confidence", "medium"),
            "evidence_link": "",
            "auto_apply_allowed": False,
            "decision": decision.get("decision", "pending"),
            "decision_note": decision.get("note", ""),
        })
        voice_items_added += 1

    # Every student-approved voice revision becomes a first-class correction
    # item so the existing Track Changes generator can apply it safely.
    for revision_id, revision in (result.get("academic_voice_revisions") or {}).items():
        if not isinstance(revision, dict) or not revision.get("original_text") or not revision.get("proposed_replacement"):
            continue
        items.append({
            "id": revision_id,
            "priority": "optional",
            "category": "academic_voice_revision",
            "title": "Approved academic voice revision",
            "what_is_wrong": revision.get("reason") or "The passage was selected for clarity and natural-voice revision.",
            "why_it_matters": "The student reviewed and approved this wording change.",
            "evidence": revision.get("original_text"),
            "location": _locate(manuscript_text, revision.get("original_text")),
            "recommended_action": "Replace with the approved revision using Track Changes.",
            "coach_explanation": "This revision was approved explicitly and must remain reviewable in Word.",
            "supporting_metadata": {"model": revision.get("model"), "confidence": revision.get("confidence")},
            "confidence": revision.get("confidence", "reviewed"),
            "evidence_link": "",
            "original_text": revision.get("original_text"),
            "proposed_replacement": revision.get("proposed_replacement"),
            "secondary_replacement": "",
            "track_operation": "replace",
            "auto_apply_allowed": False,
            "decision": "accepted",
            "decision_note": "Student approved this revision for insertion with Track Changes.",
        })

    coach = result.get("citation_improvement_coach") or {}
    for row in _rows(coach.get("lessons") if isinstance(coach, dict) else []):
        if str(row.get("pattern") or "").lower() == "unused_reference_entries":
            # The dedicated uncited-reference item already provides cite or
            # delete actions and should be the single source of truth.
            continue
        item_id = f"coach-{len(items) + 1}"
        decision = saved_decisions.get(item_id) or {}
        items.append({
            "id":item_id, "priority":row.get("priority","important"), "category":"citation_coach",
            "title":str(row.get("pattern","Citation practice issue")).replace("_"," ").title(),
            "what_is_wrong":row.get("explanation"), "why_it_matters":row.get("explanation"),
            "evidence":row.get("passage","")[:900], "location":row.get("location"),
            "recommended_action":row.get("recommended_action"), "coach_explanation":row.get("explanation"),
            "supporting_metadata":{}, "confidence":"medium", "evidence_link":"", "auto_apply_allowed":False,
            "decision":decision.get("decision","pending"), "decision_note":decision.get("note","")
        })

    for item in items:
        if item.get("category") in {"source_verification", "reference_style", "reference_incomplete", "reference_identity_conflict", "reference_metadata", "uncited_reference"}:
            location = item.get("location") or {}
            if isinstance(location, dict) and location.get("section") == "Document body" and location.get("location_note"):
                item["location"] = {**location, "section": "References"}
        item["source_search_report"] = saved_search_reports.get(item.get("id")) or {}

    rank = {"critical": 0, "important": 1, "optional": 2}
    items.sort(key=lambda item: (rank.get(item["priority"], 9), item["category"]))
    pending_items = [item for item in items if item.get("decision") not in {"accepted", "rejected", "ignored", "resolved"}]
    counts = Counter(item["priority"] for item in pending_items)
    status = "ready" if not counts["critical"] and counts["important"] <= 2 else "not_ready"
    workspace = build_evidence_resolution_workspace(items)
    return {
        "readiness": status,
        "headline": (
            "No critical citation-integrity issues were identified. Complete the remaining review items before submission."
            if status == "ready" else
            f"Complete {counts['critical']} critical and {counts['important']} important corrections before submission."
        ),
        "counts": {"critical": counts["critical"], "important": counts["important"], "optional": counts["optional"], "total": len(pending_items), "all_items": len(items), "decided": len(items) - len(pending_items)},
        "items": items,
        "evidence_resolution_workspace": workspace,
        "human_review_required": True,
    }


def compare_revision_results(original: Dict[str, Any], revised: Dict[str, Any]) -> Dict[str, Any]:
    original_plan = build_correction_plan(original)
    revised_plan = build_correction_plan(revised)
    before = original_plan["counts"]
    after = revised_plan["counts"]
    original_acii = (original.get("acii") or {}).get("ACII", (original.get("acii") or {}).get("score"))
    revised_acii = (revised.get("acii") or {}).get("ACII", (revised.get("acii") or {}).get("score"))
    def metrics(data: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, int]:
        verification = data.get("online_verification") or {}
        verification_rows = _rows(verification.get("rows") if isinstance(verification, dict) else verification)
        claim_rows = _rows(data.get("claim_support"))
        return {
            "missing_references": len(_rows(data.get("missing_in_references"))),
            "uncited_references": len(_rows(data.get("uncited_references"))),
            "verified_sources": sum(normalise_verification_status(row) == "verified" for row in verification_rows),
            "unsupported_claims": sum(claim_requires_resolution(row) for row in claim_rows),
            "formatting_issues": sum(item.get("category") in {"citation_formatting", "reference_metadata"} and item.get("decision") not in {"resolved", "accepted"} for item in plan.get("items") or []),
            "remaining_submission_risks": plan.get("counts", {}).get("critical", 0) + plan.get("counts", {}).get("important", 0),
        }
    before_metrics = metrics(original, original_plan)
    after_metrics = metrics(revised, revised_plan)
    return {
        "journey": {"run_1": "Diagnose the manuscript", "correction_period_days": 90, "run_2": "Verify the revised manuscript", "final_output": "Before-and-after submission-readiness report"},
        "before": {"corrections": before, "acii": original_acii, **before_metrics},
        "after": {"corrections": after, "acii": revised_acii, **after_metrics},
        "change": {
            "critical_resolved": max(0, before["critical"] - after["critical"]),
            "important_resolved": max(0, before["important"] - after["important"]),
            "total_resolved": max(0, before["total"] - after["total"]),
            "acii_change": (revised_acii - original_acii) if isinstance(original_acii, (int, float)) and isinstance(revised_acii, (int, float)) else None,
            "missing_references_resolved": max(0, before_metrics["missing_references"] - after_metrics["missing_references"]),
            "uncited_references_resolved": max(0, before_metrics["uncited_references"] - after_metrics["uncited_references"]),
            "verified_sources_increased": after_metrics["verified_sources"] - before_metrics["verified_sources"],
            "unsupported_claims_resolved": max(0, before_metrics["unsupported_claims"] - after_metrics["unsupported_claims"]),
            "formatting_errors_resolved": max(0, before_metrics["formatting_issues"] - after_metrics["formatting_issues"]),
        },
        "readiness": revised_plan["readiness"],
        "remaining_plan": revised_plan,
    }
