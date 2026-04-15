# =========================================
# formatter.py — CiteIntegrity Core Formatter + Repair Engine
# =========================================

import re
import requests


# =========================================
# 1. PARSE REFERENCE
# =========================================
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
    }

    # Year
    year_match = re.search(r"\b(19|20)\d{2}[a-z]?\b", raw)
    if year_match:
        parsed["year"] = year_match.group(0)

    # DOI
    doi_match = re.search(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", raw, re.I)
    if doi_match:
        parsed["doi"] = doi_match.group(1).rstrip(".,;")

    # URL
    url_match = re.search(r"(https?://\S+)", raw, re.I)
    if url_match:
        parsed["url"] = url_match.group(1).rstrip(".,;")

    # Volume, issue, pages
    vip = re.search(r"(\d+)\s*\((\d+)\)\s*,\s*([\d\-–]+)", raw)
    if vip:
        parsed["volume"] = vip.group(1)
        parsed["issue"] = vip.group(2)
        parsed["pages"] = vip.group(3)

    # Split using year
    if parsed["year"]:
        parts = re.split(rf"\(?{parsed['year']}\)?", raw, maxsplit=1)

        if len(parts) >= 2:
            parsed["authors"] = parts[0].strip(" .,()")
            remainder = parts[1].strip(" .,()")

            title_split = re.split(r"\.\s+", remainder, maxsplit=1)

            if len(title_split) >= 1:
                parsed["title"] = title_split[0]

            if len(title_split) == 2:
                parsed["source"] = title_split[1]

    return parsed


# =========================================
# 2. DOI FETCH
# =========================================
def fetch_from_doi(doi: str) -> dict:
    try:
        url = f"https://api.crossref.org/works/{doi}"
        res = requests.get(url, timeout=5)

        if res.status_code != 200:
            return {}

        data = res.json()["message"]

        return {
            "authors": ", ".join(
                [f"{a.get('family','')} {a.get('given','')}" for a in data.get("author", [])]
            ),
            "year": str(data.get("issued", {}).get("date-parts", [[None]])[0][0]),
            "title": data.get("title", [""])[0],
            "source": data.get("container-title", [""])[0],
            "volume": data.get("volume", ""),
            "issue": data.get("issue", ""),
            "pages": data.get("page", ""),
            "publisher": data.get("publisher", ""),
            "doi": doi,
            "url": data.get("URL", "")
        }

    except Exception:
        return {}


# =========================================
# 3. AUTHOR CLEANING
# =========================================
def clean_authors(authors: str) -> str:
    parts = [a.strip() for a in authors.split(",") if a.strip()]
    formatted = []

    for p in parts:
        names = p.split()
        if len(names) >= 2:
            last = names[-1]
            initials = " ".join([n[0] + "." for n in names[:-1]])
            formatted.append(f"{last}, {initials}")
        else:
            formatted.append(p)

    return ", ".join(formatted)


# =========================================
# 4. FORMATTERS
# =========================================
def format_apa7(p):
    authors = clean_authors(p["authors"])
    year = f"({p['year']})." if p["year"] else "(n.d.)."

    ref = f"{authors} {year} {p['title']}. {p['source']}"
    if p["volume"]:
        ref += f", {p['volume']}"
    if p["issue"]:
        ref += f"({p['issue']})"
    if p["pages"]:
        ref += f", {p['pages']}"
    ref += "."

    if p["doi"]:
        ref += f" https://doi.org/{p['doi']}"
    elif p["url"]:
        ref += f" {p['url']}"

    return " ".join(ref.split())


def format_apa6(p):
    authors = clean_authors(p["authors"])
    year = f"({p['year']})." if p["year"] else "(n.d.)."

    ref = f"{authors} {year} {p['title']}. {p['source']}"
    if p["volume"]:
        ref += f", {p['volume']}"
    if p["issue"]:
        ref += f"({p['issue']})"
    if p["pages"]:
        ref += f", {p['pages']}"
    ref += "."

    if p["doi"]:
        ref += f" doi:{p['doi']}"
    elif p["url"]:
        ref += f" Retrieved from {p['url']}"

    return " ".join(ref.split())


def format_harvard(p):
    year = p["year"] or "n.d."

    ref = f"{p['authors']} ({year}) '{p['title']}', {p['source']}"
    if p["volume"]:
        ref += f", {p['volume']}"
    if p["issue"]:
        ref += f"({p['issue']})"
    if p["pages"]:
        ref += f", pp. {p['pages']}"

    if p["doi"]:
        ref += f", doi: {p['doi']}."
    elif p["url"]:
        ref += f". Available at: {p['url']}."
    else:
        ref += "."

    return " ".join(ref.split())


def format_reference(parsed, style):
    style = style.lower()

    if style == "apa7":
        return format_apa7(parsed)
    elif style == "apa6":
        return format_apa6(parsed)
    elif style == "harvard":
        return format_harvard(parsed)

    raise ValueError("Invalid style")


# =========================================
# 5. REPAIR ENGINE
# =========================================
def compute_repair_score(p):
    score = 100

    if not p["authors"]:
        score -= 20
    if not p["year"]:
        score -= 20
    if not p["title"]:
        score -= 20
    if not p["source"]:
        score -= 15
    if not p["doi"] and not p["url"]:
        score -= 10

    return max(score, 0)


def repair_reference(raw, style, source_type, auto_enhance=True):
    parsed = parse_reference(raw, source_type)
    log = []

    if auto_enhance and parsed.get("doi"):
        doi_data = fetch_from_doi(parsed["doi"])
        for k, v in doi_data.items():
            if v and not parsed.get(k):
                parsed[k] = v
                log.append(f"Filled {k} from DOI")

    formatted = format_reference(parsed, style)
    score = compute_repair_score(parsed)

    return {
        "original": raw,
        "formatted": formatted,
        "parsed": parsed,
        "repair_log": log,
        "confidence": score
    }


# =========================================
# 6. BULK FUNCTIONS
# =========================================
def process_references(raw_text, style, variant=None, source_type="journal", auto_enhance=True):
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

    formatted = []
    warnings = []

    for i, line in enumerate(lines, 1):
        parsed = parse_reference(line, source_type)

        if auto_enhance and parsed.get("doi"):
            doi_data = fetch_from_doi(parsed["doi"])
            for k, v in doi_data.items():
                if v and not parsed.get(k):
                    parsed[k] = v

        formatted.append(format_reference(parsed, style))

        if not parsed["title"]:
            warnings.append(f"Reference {i}: Missing title")

    return {
        "formatted": "\n\n".join(formatted),
        "warnings": warnings
    }


def repair_references_bulk(raw_text, style, source_type="journal", auto_enhance=True):
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

    return [
        repair_reference(line, style, source_type, auto_enhance)
        for line in lines
    ]
