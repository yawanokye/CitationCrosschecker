"""Shared, provider-independent identity and edit safety rules.

No benchmark answers or publication-specific overrides belong in this module.
"""
from html import unescape
import re
import unicodedata


def plain_metadata(value):
    text = unescape(str(value or ""))
    text = re.sub(r"<[^>]*>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def identity_text(value):
    text = unicodedata.normalize("NFKD", plain_metadata(value)).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def notice_kind(title):
    text = plain_metadata(title)
    match = re.match(
        r"^\s*(?:notice\s+of\s+)?(editorial\s+expression\s+of\s+concern|"
        r"expression\s+of\s+concern|retraction(?:\s+and\s+replacement)?|"
        r"withdrawal|correction|corrigendum|erratum|reinstatement)\s*[:.\-–—]",
        text, re.I,
    )
    if not match:
        return ""
    kind = match.group(1).lower()
    if "concern" in kind:
        return "expression_of_concern"
    if kind in {"corrigendum", "erratum"}:
        return "correction"
    return kind.replace(" ", "_")


def is_review_instruction(value):
    return bool(re.match(
        r"^(?:review\s+(?:the\s+)?reference\s+manually|manual\s+review\s+required|"
        r"verify\s+(?:this\s+)?reference\s+manually|check\s+(?:the\s+)?publisher\s+record)\b",
        plain_metadata(value), re.I,
    ))
