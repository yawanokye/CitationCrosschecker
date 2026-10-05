"""Conservative author/year boundaries shared by bibliography extraction.

These rules recognise bibliographic syntax, not source validity. In particular,
a year in a title, a page range, or a DOI does not start a new reference.
"""
import re

_NAME_WORD = r"[^\W\d_]+(?:[-’'][^\W\d_]+)*"
_SURNAME = rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,4}}"
_INITIALS = r"(?:[A-ZÀ-ÖØ-ÞİŁŚŻŹĆŃÓŠŽĐ]\.\s*){1,6}"
_PERSON = rf"{_SURNAME},\s*{_INITIALS}"
_AUTHOR_LIST = rf"{_PERSON}(?:(?:,\s*(?:&\s*|and\s*)?|\s*(?:&|and)\s+){_PERSON})*"
_PERSON_PREFIX = re.compile(rf"^{_AUTHOR_LIST}\s*[,;.]?\s*$")
_BARE_YEAR = re.compile(r"\b(?:1[6-9]\d{2}|20\d{2})[a-z]?\b", re.I)
_PERSON_CANDIDATE = re.compile(rf"(?<![\w’'\-])(?={_PERSON})")
_ORG_CUE = re.compile(
    r"\b(?:university|organisation|organization|ministry|department|agency|"
    r"association|institute|institution|council|commission|bank|service|bureau|"
    r"authority|office|foundation|nations)\b", re.I
)


def author_list_without_year(text):
    """A complete author prefix whose publication year is on the next line."""
    text = (text or "").strip()
    return bool(not _BARE_YEAR.search(text) and _PERSON_PREFIX.fullmatch(text))


def bare_author_year_start(text):
    """Accept author lists followed by a bare publication year and punctuation."""
    text = (text or "").strip()
    year = _BARE_YEAR.search(text)
    if not year or year.start() > 1800:
        return False
    # Parenthesised dates already have their own established extraction rules.
    if text[:year.start()].rstrip().endswith("("):
        return False
    if not re.match(r"\s*[.,:]", text[year.end():]):
        return False
    author = text[:year.start()].strip()
    if _PERSON_PREFIX.fullmatch(author):
        return True
    # Limit corporate-author support to explicitly named institutions/acronyms.
    # A sentence such as 'Results from the survey in 2021.' must stay a tail.
    org = author.rstrip(". ,;")
    if not org or len(org) > 160 or re.search(r"[.!?]", org):
        return False
    return bool(
        re.fullmatch(r"[A-Z]{2,12}", org)
        or (_ORG_CUE.search(org) and all(
            word[:1].isupper() or word.lower() in {"of", "the", "and", "for", "in", "&"}
            for word in org.split()
        ))
    )


def split_embedded_bare_year_references(text):
    """Split flattened entries only after a completed, dated reference.

    Requiring a date before each cut prevents co-authors (including long lists
    wrapped before their year) from becoming separate references.
    """
    text = (text or "").strip()
    cuts = []
    previous = 0
    for match in _PERSON_CANDIDATE.finditer(text):
        pos = match.start()
        if not pos:
            continue
        prefix = text[previous:pos].rstrip()
        if not _BARE_YEAR.search(prefix):
            continue
        if not re.search(r"[.!?]\s*$|https?://\S+\s*$", prefix):
            continue
        if bare_author_year_start(text[pos:]):
            cuts.append(pos)
            previous = pos
    if not cuts:
        return [text] if text else []
    starts = [0] + cuts
    ends = cuts + [len(text)]
    return [text[start:end].strip() for start, end in zip(starts, ends)]
