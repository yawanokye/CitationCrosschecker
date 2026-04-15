import re


def parse_reference(raw_reference: str, source_type: str) -> dict:
    raw = " ".join(raw_reference.strip().split())

    parsed = {
        "authors": "",
        "year": "",
        "title": "",
        "source": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "publisher": "",
        "doi": "",
        "url": "",
        "access_date": ""
    }

    year_match = re.search(r"\b(19|20)\d{2}[a-z]?\b", raw)
    if year_match:
        parsed["year"] = year_match.group(0)

    doi_match = re.search(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", raw, re.I)
    if doi_match:
        parsed["doi"] = doi_match.group(1).rstrip(".,;")

    url_match = re.search(r"(https?://\S+)", raw, re.I)
    if url_match:
        parsed["url"] = url_match.group(1).rstrip(".,;")

    volume_issue_pages = re.search(r"(\d+)\s*\((\d+)\)\s*,\s*([\d\-–]+)", raw)
    if volume_issue_pages:
        parsed["volume"] = volume_issue_pages.group(1)
        parsed["issue"] = volume_issue_pages.group(2)
        parsed["pages"] = volume_issue_pages.group(3)
    else:
        volume_pages = re.search(r"(\d+)\s*,\s*([\d\-–]+)", raw)
        if volume_pages:
            parsed["volume"] = volume_pages.group(1)
            parsed["pages"] = volume_pages.group(2)

    if parsed["year"]:
        split_pattern = re.split(rf"\(?{re.escape(parsed['year'])}\)?", raw, maxsplit=1)
        if len(split_pattern) >= 2:
            parsed["authors"] = split_pattern[0].strip(" .,()")
            remainder = split_pattern[1].strip(" .,()")

            title_split = re.split(r"\.\s+", remainder, maxsplit=1)
            if len(title_split) >= 1:
                parsed["title"] = title_split[0].strip(" .")

            if len(title_split) == 2:
                parsed["source"] = title_split[1].strip(" .")
    else:
        first_period = raw.find(".")
        if first_period != -1:
            parsed["authors"] = raw[:first_period].strip()
            rest = raw[first_period + 1:].strip()
            second_period = rest.find(".")
            if second_period != -1:
                parsed["title"] = rest[:second_period].strip()
                parsed["source"] = rest[second_period + 1:].strip()
            else:
                parsed["title"] = rest

    if source_type in {"book", "report"} and not parsed["publisher"]:
        if parsed["source"]:
            parsed["publisher"] = parsed["source"]

    return parsed


def format_reference(parsed: dict, style: str, variant: str, source_type: str) -> str:
    style = style.lower().strip()

    if style == "apa7":
        return format_apa7(parsed, source_type)

    if style == "apa6":
        return format_apa6(parsed, source_type)

    if style == "harvard":
        return format_harvard(parsed, source_type, variant)

    raise ValueError("Unsupported style selected.")


def format_apa7(parsed: dict, source_type: str) -> str:
    authors = parsed["authors"].strip()
    year = f"({parsed['year']})." if parsed["year"] else "(n.d.)."
    title = parsed["title"].strip()
    source = parsed["source"].strip()

    if source_type == "journal":
        ref = f"{authors} {year} {title}. {source}"
        if parsed["volume"]:
            ref += f", {parsed['volume']}"
        if parsed["issue"]:
            ref += f"({parsed['issue']})"
        if parsed["pages"]:
            ref += f", {parsed['pages']}"
        ref += "."
        if parsed["doi"]:
            ref += f" https://doi.org/{parsed['doi']}"
        elif parsed["url"]:
            ref += f" {parsed['url']}"
        return " ".join(ref.split())

    if source_type == "book":
        ref = f"{authors} {year} {title}."
        if parsed["publisher"]:
            ref += f" {parsed['publisher']}."
        elif source:
            ref += f" {source}."
        if parsed["doi"]:
            ref += f" https://doi.org/{parsed['doi']}"
        return " ".join(ref.split())

    if source_type == "webpage":
        ref = f"{authors} {year} {title}."
        if source:
            ref += f" {source}."
        if parsed["url"]:
            ref += f" {parsed['url']}"
        return " ".join(ref.split())

    if source_type == "report":
        ref = f"{authors} {year} {title}."
        if parsed["publisher"]:
            ref += f" {parsed['publisher']}."
        elif source:
            ref += f" {source}."
        if parsed["url"]:
            ref += f" {parsed['url']}"
        return " ".join(ref.split())

    return f"{authors} {year} {title}. {source}".strip()


def format_apa6(parsed: dict, source_type: str) -> str:
    authors = parsed["authors"].strip()
    year = f"({parsed['year']})." if parsed["year"] else "(n.d.)."
    title = parsed["title"].strip()
    source = parsed["source"].strip()

    if source_type == "journal":
        ref = f"{authors} {year} {title}. {source}"
        if parsed["volume"]:
            ref += f", {parsed['volume']}"
        if parsed["issue"]:
            ref += f"({parsed['issue']})"
        if parsed["pages"]:
            ref += f", {parsed['pages']}"
        ref += "."
        if parsed["doi"]:
            ref += f" doi:{parsed['doi']}"
        elif parsed["url"]:
            ref += f" Retrieved from {parsed['url']}"
        return " ".join(ref.split())

    if source_type == "book":
        ref = f"{authors} {year} {title}."
        if parsed["publisher"]:
            ref += f" {parsed['publisher']}."
        elif source:
            ref += f" {source}."
        if parsed["url"] and not parsed["doi"]:
            ref += f" Retrieved from {parsed['url']}"
        return " ".join(ref.split())

    if source_type == "webpage":
        ref = f"{authors} {year} {title}."
        if source:
            ref += f" {source}."
        if parsed["url"]:
            ref += f" Retrieved from {parsed['url']}"
        return " ".join(ref.split())

    if source_type == "report":
        ref = f"{authors} {year} {title}."
        if parsed["publisher"]:
            ref += f" {parsed['publisher']}."
        elif source:
            ref += f" {source}."
        if parsed["url"]:
            ref += f" Retrieved from {parsed['url']}"
        return " ".join(ref.split())

    return f"{authors} {year} {title}. {source}".strip()


def format_harvard(parsed: dict, source_type: str, variant: str = "generic") -> str:
    authors = parsed["authors"].strip()
    year = parsed["year"] if parsed["year"] else "n.d."
    title = parsed["title"].strip()
    source = parsed["source"].strip()

    if source_type == "journal":
        ref = f"{authors} ({year}) '{title}', {source}"
        if parsed["volume"]:
            ref += f", {parsed['volume']}"
        if parsed["issue"]:
            ref += f"({parsed['issue']})"
        if parsed["pages"]:
            ref += f", pp. {parsed['pages']}"
        if parsed["doi"]:
            ref += f", doi: {parsed['doi']}."
        elif parsed["url"]:
            ref += f". Available at: {parsed['url']}."
        else:
            ref += "."
        return " ".join(ref.split())

    if source_type == "book":
        publisher = parsed["publisher"] or source
        ref = f"{authors} ({year}) {title}."
        if publisher:
            ref += f" {publisher}."
        return " ".join(ref.split())

    if source_type == "webpage":
        ref = f"{authors} ({year}) {title}."
        if source:
            ref += f" {source}."
        if parsed["url"]:
            ref += f" Available at: {parsed['url']}."
        return " ".join(ref.split())

    if source_type == "report":
        publisher = parsed["publisher"] or source
        ref = f"{authors} ({year}) {title}."
        if publisher:
            ref += f" {publisher}."
        if parsed["url"]:
            ref += f" Available at: {parsed['url']}."
        return " ".join(ref.split())

    return f"{authors} ({year}) {title}. {source}".strip()


def build_warnings(parsed: dict, source_type: str) -> list:
    warnings = []

    if not parsed["authors"]:
        warnings.append("Author not clearly detected.")
    if not parsed["year"]:
        warnings.append("Year not clearly detected.")
    if not parsed["title"]:
        warnings.append("Title not clearly detected.")

    if source_type == "journal" and not parsed["source"]:
        warnings.append("Journal name not clearly detected.")

    if source_type in {"book", "report"} and not (parsed["publisher"] or parsed["source"]):
        warnings.append("Publisher or organisation not clearly detected.")

    return warnings


def process_references(raw_reference: str, style: str, variant: str, source_type: str) -> dict:
    lines = [line.strip() for line in raw_reference.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Please paste at least one reference.")

    formatted_refs = []
    warnings = []

    for i, line in enumerate(lines, start=1):
        parsed = parse_reference(line, source_type)
        formatted = format_reference(
            parsed=parsed,
            style=style,
            variant=variant,
            source_type=source_type
        )
        formatted_refs.append(formatted)

        row_warnings = build_warnings(parsed, source_type)
        for warning in row_warnings:
            warnings.append(f"Reference {i}: {warning}")

    return {
        "formatted": "\n\n".join(formatted_refs),
        "warnings": warnings
    }
