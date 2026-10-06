"""Keep bibliographic display metadata separate from fuzzy matching tokens.

Provider dates describe different versions of a work. Registration timestamps
are never publication dates, and a source lead is never an approved correction.
"""
import re
from difflib import SequenceMatcher

from reference_safety import identity_text, plain_metadata, notice_kind


def _year(value):
    match = re.match(r"^((?:18|19|20|21)\d{2})", str(value or ""))
    return match.group(1) if match else ""


def provider_metadata(source, item):
    """Preserve every supplied contributor, order, spelling and date provenance."""
    source = str(source or "").replace("_direct", "")
    item = item if isinstance(item, dict) else {}
    authors, supplied, dates = [], [], {}

    def add_date(kind, value):
        parts = (value.get("date-parts") or [[]])[0] if isinstance(value, dict) else []
        if parts and _year(parts[0]):
            dates[kind] = {"year": _year(parts[0]), "date": "-".join(
                str(part) if i == 0 else str(part).zfill(2) for i, part in enumerate(parts[:3]))}
        elif not isinstance(value, dict) and _year(value):
            dates[kind] = {"year": _year(value), "date": str(value)}

    truncated = False
    if source == "crossref":
        supplied = item.get("author") or []
        for author in supplied:
            if not isinstance(author, dict):
                continue
            family, given, name = (plain_metadata(author.get(k)) for k in ("family", "given", "name"))
            if family or name:
                authors.append({"family": family, "given": given, **({"name": name} if name else {})})
        for kind in ("published-print", "published-online", "published", "issued"):
            add_date(kind, item.get(kind))
    elif source == "openalex":
        supplied = item.get("authorships") or []
        for author in supplied:
            name = plain_metadata((author.get("author") or {}).get("display_name")) if isinstance(author, dict) else ""
            if name:
                authors.append(name)
        add_date("publication", item.get("publication_date") or item.get("publication_year"))
        try:
            reported_count = int(item.get("authors_count") or 0)
        except (ValueError, TypeError):
            reported_count = 0
        truncated = bool(item.get("is_authors_truncated") or reported_count > len(supplied))
    elif source == "datacite":
        attrs = item.get("attributes", item)
        supplied = attrs.get("creators") or []
        for author in supplied:
            if not isinstance(author, dict):
                continue
            family, given, name = (plain_metadata(author.get(k)) for k in ("familyName", "givenName", "name"))
            if family:
                authors.append({"family": family, "given": given})
            elif name:
                authors.append({"name": name, "nameType": author.get("nameType", "")})
        add_date("publication", attrs.get("publicationYear"))
    else:
        supplied = item.get("authors") or []
        for author in supplied:
            name = plain_metadata(author.get("name")) if isinstance(author, dict) else plain_metadata(author)
            if name:
                authors.append(name)
        if source == "pubmed":
            add_date("publication", item.get("pubdate"))
            add_date("published-online", item.get("epubdate"))
        elif source == "europepmc":
            if not authors:
                supplied = [name.strip() for name in str(item.get("authorString") or "").split(",") if name.strip()]
                authors = supplied[:]
            add_date("publication", item.get("pubYear"))
            add_date("published-online", item.get("firstPublicationDate"))
        else:
            add_date("publication", item.get("publicationDate") or item.get("year"))
    from reference_formatter import parse_author
    display = [parse_author(author) for author in authors]
    complete = bool(authors) and len(authors) == len(supplied) and not truncated
    # A provider can supply surname-only metadata. Keep it for matching but do
    # not present it as a complete personal author name in a replacement.
    from reference_formatter import _is_corporate_author
    if any((isinstance(author, dict) and author.get("family") and not author.get("given") and not _is_corporate_author(author["family"])) or
           (isinstance(author, str) and len(author.split()) == 1) for author in authors):
        complete = False
    basis = next((kind for kind in ("published-print", "published", "publication", "issued", "published-online") if kind in dates), "")
    container = item.get("container-title") or []
    journal = container[0] if isinstance(container, list) and container else (container if isinstance(container, str) else "")
    biblio = item.get("biblio") or {}
    if source == "openalex":
        journal = ((item.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    first, last = biblio.get("first_page"), biblio.get("last_page")
    pages = item.get("page") or item.get("article-number") or (str(first) + ("-" + str(last) if last and last != first else "") if first else last) or ""
    return {"authors_full": authors, "authors_display": display, "author_count": len(authors),
            "authors_complete": complete, "authors_truncated": truncated,
            "publication_dates": dates, "publication_years": sorted({date["year"] for date in dates.values()}),
            "year": dates.get(basis, {}).get("year", ""), "year_basis": basis,
            "metadata_provider": source, "journal": plain_metadata(journal),
            "volume": str(item.get("volume") or biblio.get("volume") or ""),
            "issue": str(item.get("issue") or biblio.get("issue") or ""), "pages": str(pages),
            "publisher": plain_metadata(item.get("publisher")), "publication_type": item.get("type") or "article"}


def row_candidate(row):
    """Map a verification row without reinterpreting surname CSV as one person."""
    authors = row.get("matched_authors_full") or row.get("authors_full")
    legacy = row.get("matched_authors") or []
    if not authors:
        authors = [name.strip() for name in legacy.split(",") if name.strip()] if isinstance(legacy, str) else legacy
    return {"authors": authors, "authors_full": authors,
            "authors_complete": row.get("matched_authors_complete", bool(row.get("matched_authors_full"))),
            "authors_truncated": row.get("matched_authors_truncated", False),
            "author_count": row.get("matched_author_count") or len(authors),
            "publication_dates": row.get("matched_publication_dates") or {},
            "publication_years": row.get("matched_publication_years") or [],
            "year_basis": row.get("matched_year_basis") or "provider_publication_year",
            "year": str(row.get("matched_year") or ""), "title": row.get("matched_title") or "",
            "doi": row.get("matched_doi") or row.get("doi") or "",
            "journal": row.get("matched_container_title") or row.get("matched_journal") or "",
            "publisher": row.get("matched_publisher") or row.get("publisher") or "",
            "volume": row.get("matched_volume") or "", "issue": row.get("matched_issue") or "",
            "pages": row.get("matched_pages") or "", "publication_type": row.get("matched_type") or "article"}


def prepare_reference_candidate(candidate, original=None):
    """Annotate a proposed reference; preserve a documented manuscript year."""
    from reference_formatter import parse_authors
    out = dict(candidate)
    original = original or {}
    out["authors"] = out.get("authors_full") or out.get("authors") or []
    dates = out.get("publication_dates") or {}
    years = list(dict.fromkeys([str(y) for y in out.get("publication_years") or []] +
                             [str(date.get("year")) for date in dates.values() if isinstance(date, dict) and date.get("year")]))
    if not years and _year(out.get("year")):
        years = [_year(out["year"])]
    out["publication_years"] = years
    old_year = str(original.get("year") or "")
    if _year(old_year) in years and not out.get("year_selected_by_user"):
        out["year"] = old_year  # Preserve citation disambiguation suffix too.
        out["year_basis"] = "manuscript_year_matches_publication_date"
    if "authors_complete" not in out or out["authors_complete"] is None:
        from reference_formatter import _is_corporate_author
        names = parse_authors(out["authors"])
        out["authors_complete"] = bool(names) and all("," in name or _is_corporate_author(name) for name in names) and not bool(re.search(
            r"\bet\s+al\b|\bothers\b|…|\.\.\.", str(out["authors"]), re.I))
    out["date_review_required"] = len(set(years)) > 1 or bool(old_year and str(out.get("year") or "") != old_year)
    old_names, new_names = parse_authors(original.get("authors")), parse_authors(out["authors"])
    old_families = [identity_text(name.split(",", 1)[0]) for name in old_names]
    new_families = [identity_text(name.split(",", 1)[0]) for name in new_names]
    out["author_review_required"] = bool(old_families and old_families != new_families)
    warnings = []
    if out.get("authors_complete") is False or out.get("authors_truncated"):
        warnings.append("The indexed author list is incomplete. Recover the full list from the publisher before approval.")
    if out["author_review_required"]:
        warnings.append("The proposed author list differs from the manuscript. Check every author and their order.")
    if out["date_review_required"]:
        warnings.append("Online and final publication years can differ. Choose the year of the version cited; both the reference and linked citations will use it.")
    if not out.get("year"):
        warnings.append("No publication year is supplied. A creation or deposit date cannot fill this field.")
    out["metadata_warnings"] = warnings
    return out


def reference_identity_error(candidate, original=None):
    """Withhold unrelated publications even when they fit the manuscript topic."""
    original = original or {}
    old_doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(original.get("doi") or ""), flags=re.I).rstrip(".").casefold()
    new_doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(candidate.get("doi") or ""), flags=re.I).rstrip(".").casefold()
    if old_doi and new_doi and old_doi != new_doi:
        return "This candidate has a different DOI. Find the intended publication before replacing the reference."
    if original.get("parse_confidence") == "high" and original.get("title"):
        a, b = identity_text(original["title"]), identity_text(candidate.get("title"))
        if SequenceMatcher(None, a, b).ratio() < .8 or bool(notice_kind(original["title"])) != bool(notice_kind(candidate.get("title"))):
            return "The candidate title describes a different publication. It cannot replace this reference."
    return ""


def reference_approval_error(candidate, original=None):
    """Server-side safeguards apply even if the browser bypasses its controls."""
    from reference_formatter import parse_authors
    original = original or {}
    prepared = prepare_reference_candidate(candidate, original)
    if not parse_authors(prepared.get("authors")) or prepared.get("authors_complete") is False or prepared.get("authors_truncated"):
        return "The complete author list must be recovered and checked before approval."
    if not _year(candidate.get("year")):
        return "Supply and check a publication year before approval."
    identity_error = reference_identity_error(prepared, original)
    if identity_error:
        return identity_error
    raw_year_change = bool(original.get("year") and str(candidate.get("year") or "") != str(original["year"]))
    if (prepared["date_review_required"] or raw_year_change) and candidate.get("year_choice_confirmed") is not True:
        return "Choose and confirm the publication year of the version you cited before approval."
    if prepared["author_review_required"] and candidate.get("author_list_confirmed") is not True:
        return "Confirm the complete author list and its order against the publisher before changing authors."
    return ""


def format_candidate_reference(candidate, style, marker=""):
    from reference_formatter import format_reference
    ref = {key: candidate.get(key) or "" for key in (
        "year", "title", "volume", "issue", "pages", "doi", "publisher", "edition", "url", "isbn")}
    ref.update(authors=candidate.get("authors_full") or candidate.get("authors") or [],
               source=candidate.get("journal") or candidate.get("source_title") or "",
               type=candidate.get("publication_type") or "article")
    formatted = re.sub(r"\*", "", format_reference(ref, style)).strip()
    return (str(marker) + " " + formatted).strip() if marker else formatted


def format_candidate_citation(candidate):
    from reference_formatter import parse_authors
    names = [name.split(",", 1)[0] for name in parse_authors(candidate.get("authors_full") or candidate.get("authors"))]
    if not names:
        return ""
    author_text = names[0] + " et al." if len(names) > 2 else " & ".join(names)
    return "(" + author_text + ", " + str(candidate.get("year") or "n.d.") + ")"


def export_reference_metadata(row):
    """Formatting an export must not silently substitute an unapproved source."""
    from correction_plan import _parse_original_reference, _reference_identity_gate
    original = _parse_original_reference(row.get("reference") or row.get("original_reference") or "")
    candidate = row_candidate(row)
    identity = _reference_identity_gate(original, row)
    ref, warnings = dict(original), []
    if identity["accepted"]:
        for key in ("volume", "issue", "pages", "doi", "publisher"):
            ref[key] = ref.get(key) or candidate.get(key) or ""
        ref["source"] = ref.get("source") or candidate.get("journal") or ""
        # Complete authors may fill a missing/truncated list only when identity
        # is corroborated. An existing complete list always remains authoritative
        # for formatting until the user approves a scholarly metadata correction.
        abbreviated = bool(re.search(r"\bet\s+al\b|\bothers\b|…|\.\.\.", str(ref.get("authors") or ""), re.I))
        if (not ref.get("authors") or abbreviated) and candidate.get("authors_complete") and identity.get("title_similarity", 0) >= .8:
            ref["authors"] = candidate["authors"]
        if not ref.get("title") and identity.get("doi_exact"):
            ref["title"] = candidate["title"]
        if not ref.get("year"):
            warnings.append("Publication year requires review before a replacement can be approved.")
    else:
        warnings.append("External metadata was not substituted because the intended publication was not confirmed.")
    if not candidate.get("authors_complete"):
        warnings.append("The indexed author list is incomplete; manuscript authors were preserved.")
    if len(candidate.get("publication_years") or []) > 1:
        warnings.append("Multiple publication years are recorded; the manuscript year was preserved.")
    return ref, warnings
