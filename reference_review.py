"""Grounded source leads and reviewable citation edits for unresolved references."""

import re
from urllib.parse import urlparse

from reference_formatter import format_reference, parse_authors
from evidence_resolution import normalise_verification_status, reference_resolution_group, verification_status_explanation


def reference_resolution_counts(rows):
    counts = {"verified": 0, "metadata_differences": 0, "needs_review": 0, "not_found": 0, "lookup_failed": 0}
    keys = {"verified": "verified", "valid_but_not_digitally_indexed": "verified",
            "verified_with_metadata_differences": "metadata_differences", "not_found": "not_found",
            "lookup_failed": "lookup_failed"}
    for row in rows:
        if isinstance(row, dict):
            status = normalise_verification_status(row)
            key = keys.get(status, "needs_review")
            counts[key] += 1
    return counts


def _source_url(doi, url):
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(doi or "").strip(), flags=re.I)
    if re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        return "https://doi.org/" + doi
    url = str(url or "").strip()
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def reference_review_details(row, original, style):
    """Extract manuscript hints separately from a possible external record.

    Never turn the manuscript's own text into an independently verified source.
    A source lead retains its uncertainty until the user checks it.
    """
    extracted = {key: original.get(key) or "" for key in (
        "authors", "year", "title", "source", "publisher", "volume", "issue", "pages", "doi", "url")}
    candidates = []
    from reference_metadata import row_candidate, prepare_reference_candidate, format_candidate_reference, reference_identity_error
    candidate = prepare_reference_candidate(row_candidate(row), original)
    title, authors, year = candidate['title'], candidate['authors'], candidate['year']
    doi = candidate['doi']
    url = _source_url(doi, row.get("matched_url") or row.get("evidence_url") or row.get("url"))
    identity_error = reference_identity_error(candidate, original)
    if not identity_error and title and authors and year and url and (candidate['journal'] or candidate['publisher']):
        marker = re.match(r"^\s*(\[\d+\]|\(\d+\)|\d+[.)])\s*", str(row.get("reference") or ""))
        formatted = format_candidate_reference(candidate, style, marker.group(1) if style.startswith("numeric_") and marker else "")
        candidates.append({**candidate, "formatted_reference": formatted, "url": url,
            "metadata_version": "2.0.13", "extraction_origin": "online_verification",
            "approval_confirmation_type": "same_publication_identity",
            "identity_fit": {"status": "identity_requires_manual_confirmation",
                "warning": "A possible external record, not a verified replacement. Compare title, authors, year and identifier."},
            "approval_warning": "Open the record and confirm the intended publication before approval."})
    return {"resolution_group": reference_resolution_group(row),
            "resolution_explanation": verification_status_explanation(row),
            "extracted_reference": extracted, "extracted_source_candidates": candidates,
            "withheld_source_reason": identity_error}


def approved_reference_citation_edits(manuscript, original, source, style, peer_references=()):
    """Prepare exact, paragraph-anchored author/year edits for the same source.

    Numeric markers keep their reference number. Ambiguous author/year keys are
    left for individual review. No topical replacement is inferred here.
    """
    if style.startswith("numeric_"):
        return []
    old_authors = parse_authors(original.get("authors"))
    new_authors = parse_authors(source.get("authors"))
    old_year, new_year = str(original.get("year") or ""), str(source.get("year") or "")
    if not old_authors or not new_authors or not all(re.fullmatch(r"(?:19|20)\d{2}[a-z]?", year, re.I) for year in (old_year, new_year)):
        return []
    old_names = [author.split(",", 1)[0] for author in old_authors]
    new_names = [author.split(",", 1)[0] for author in new_authors]
    same_key = 0
    for peer in peer_references:
        names = parse_authors(peer.get("authors"))
        if names and names[0].split(",", 1)[0].casefold() == old_names[0].casefold() and str(peer.get("year") or "").casefold() == old_year.casefold():
            same_key += 1
    if same_key > 1:
        return []
    if len(old_names) == 1:
        author_pattern = re.escape(old_names[0])
    elif len(old_names) == 2:
        author_pattern = re.escape(old_names[0]) + r"\s+(?:and|&)\s+" + re.escape(old_names[1])
    else:
        author_pattern = re.escape(old_names[0]) + r"\s+et\s+al\."
    pattern = re.compile(r"(?<!\w)(?P<authors>" + author_pattern + r")\s*(?P<separator>,\s*|\(\s*)" + re.escape(old_year) + r"\b(?P<close>\s*\))?", re.I)
    body = re.split(r"(?im)^\s*(?:references|bibliography|works cited)\s*$", str(manuscript or ""), maxsplit=1)[0]
    paragraphs = [part.strip() for part in body.splitlines() if part.strip()]
    edits, seen = [], set()
    for paragraph in paragraphs:
        if paragraphs.count(paragraph) != 1:
            continue
        for match in pattern.finditer(paragraph):
            narrative = match.group("separator").lstrip().startswith("(")
            if len(new_names) == 1:
                names = new_names[0]
            elif len(new_names) == 2:
                names = (" and " if narrative or style == "harvard" else " & ").join(new_names)
            else:
                names = new_names[0] + " et al."
            replacement = names + (" (" if narrative else ", ") + new_year + (match.group("close") or "")
            raw = match.group(0)
            key = (paragraph, raw)
            if raw == replacement or key in seen:
                continue
            seen.add(key)
            edits.append({"anchor": paragraph, "original_text": raw, "proposed_replacement": replacement,
                          "expected_occurrences": paragraph.count(raw), "kind": "in_text_citation"})
    return edits
