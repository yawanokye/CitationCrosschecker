"""Conservative table and figure cross-reference checks for manuscripts."""

from __future__ import annotations

import io
import re
from collections import Counter
from typing import Any, Dict, List

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph


LABEL = re.compile(r"\b(?P<kind>Tables?|Figures?|Figs?\.)\s+(?P<number>\d+(?:\.\d+)*(?:[A-Z])?|[A-Z]\d*|[A-Z])\b", re.I)
CAPTION = re.compile(r"^\s*(?P<kind>Table|Figure|Fig\.)\s+(?P<number>\d+(?:\.\d+)*(?:[A-Z])?|[A-Z]\d*|[A-Z])\s*(?:[.:–—-]\s*|\s+)(?P<title>\S.*)$", re.I)
BARE_CAPTION = re.compile(r"^\s*(?:Table|Figure|Fig\.)\s+(?:[A-Z]|\d+)(?:\.\d+)*\s*$", re.I)
TITLE_VERBS = re.compile(r"^(?:shows?|presents?|illustrates?|depicts?|reports?|summari[sz]es?|lists?|provides?|compares?|describes?|displays?|indicates?|demonstrates?)\b", re.I)
SKIP_HEADING = re.compile(r"^(?:list of tables|list of figures|table of contents|contents|references|bibliography)\b", re.I)


def _normalise_kind(value: str) -> str:
    return "table" if value.casefold().startswith("table") else "figure"


def _key(kind: str, number: str) -> str:
    return f"{kind}:{number.upper()}"


def _mentioned_numbers(text: str, match: re.Match) -> List[str]:
    """Expand a small explicit list or range: 'Tables 1 and 2', 'Figs. 2–4'."""
    numbers = [match.group("number")]
    tail = text[match.end():]
    for _ in range(30):
        link = re.match(r"^\s*(?P<connector>,|/|&|\band\b|[-–—])\s*(?P<number>\d+(?:\.\d+)*(?:[A-Z])?|[A-Z]\d*|[A-Z])\b", tail, re.I)
        if not link:
            break
        next_number = link.group("number")
        if link.group("connector") in {"-", "–", "—"} and numbers[-1].isdigit() and next_number.isdigit():
            start, stop = int(numbers[-1]), int(next_number)
            if start < stop and stop - start <= 30:
                numbers.extend(str(n) for n in range(start + 1, stop + 1))
        else:
            numbers.append(next_number)
        tail = tail[link.end():]
    return numbers


def _docx_blocks(file_bytes: bytes) -> List[Dict[str, Any]]:
    doc = Document(io.BytesIO(file_bytes))
    blocks: List[Dict[str, Any]] = []
    section = "Document body"
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, doc._body)
            text = " ".join(paragraph.text.split())
            if not text and not child.xpath(".//w:drawing|.//w:pict"):
                continue
            style = paragraph.style.name if paragraph.style else ""
            if re.match(r"^Heading\s*\d", style, re.I):
                section = text[:160] or section
            blocks.append({"text": text, "style": style, "section": section,
                           "graphic": bool(child.xpath(".//w:drawing|.//w:pict")), "table": False})
        elif child.tag == qn("w:tbl"):
            blocks.append({"text": "", "style": "", "section": section, "graphic": False, "table": True})
    return blocks


def _text_blocks(text: str) -> List[Dict[str, Any]]:
    blocks = []
    section = "Document body"
    for line in str(text or "").splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        if re.match(r"^(?:chapter\s+\w+|\d+(?:\.\d+)*\s+\w+)", line, re.I) and len(line) < 120:
            section = line
        blocks.append({"text": line, "style": "", "section": section,
                       "graphic": False, "table": False})
    return blocks


def _introduction_target(blocks: List[Dict[str, Any]], caption: Dict[str, Any]) -> Dict[str, str]:
    """Choose an opening passage in the same section, before its display item."""
    index = caption["index"]
    section = caption["section"]
    preceding = [(i, block) for i, block in enumerate(blocks[:index]) if block["section"] == section]
    heading = next((block for _, block in reversed(preceding)
                    if re.match(r"^Heading\s*\d", block.get("style") or "", re.I)), None)
    body = [block for _, block in preceding if block["text"] and block is not heading
            and not block["table"] and not block["graphic"]
            and not re.match(r"^Heading\s*\d", block.get("style") or "", re.I)
            and not CAPTION.match(block["text"]) and not SKIP_HEADING.match(block["text"])]
    # The earliest substantive paragraph gives a predictable section opening.
    anchor = (body[0]["text"] if heading else body[-1]["text"]) if body else (heading or {}).get("text", "")
    return {"suggested_anchor": anchor or caption["text"],
            "intro_operation": "insert_paragraph_after" if anchor else "insert_paragraph_before",
            "suggested_sentence": f"{caption['kind'].title()} {caption['number']} presents [describe the content accurately]."}


def audit_tables_figures(file_bytes: bytes | None, filename: str, main_text: str = "") -> Dict[str, Any]:
    """Return reviewable findings; never infer a missing illustration from PDF text."""
    docx = str(filename or "").lower().endswith(".docx") and bool(file_bytes)
    try:
        blocks = _docx_blocks(file_bytes) if docx else _text_blocks(main_text)
    except Exception:
        blocks, docx = _text_blocks(main_text), False
    captions: List[Dict[str, Any]] = []
    callouts: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []
    skip = False
    for index, block in enumerate(blocks):
        text = block["text"]
        if SKIP_HEADING.match(text) and len(text) < 90:
            skip = True
            continue
        if skip:
            if re.match(r"^(?:chapter\s+\w+|\d+(?:\.\d+)*\s+\w+)", text, re.I) and len(text) < 120:
                skip = False
            else:
                continue
        if not text:
            continue
        caption = CAPTION.match(text)
        neighbor = any(b["table"] or b["graphic"] for b in blocks[max(0, index-2):min(len(blocks), index+3)])
        caption_style = "caption" in block["style"].casefold()
        # Do not count "Table 3 shows..." as a caption.
        is_caption = bool(caption and not TITLE_VERBS.match(caption.group("title")) and
                          (caption_style or neighbor or bool(re.match(r"^\s*(?:Table|Figure|Fig\.)\s+\S+\s*[.:–—-]", text, re.I))))
        if is_caption:
            kind = _normalise_kind(caption.group("kind"))
            number = caption.group("number")
            captions.append({"kind": kind, "number": number, "title": caption.group("title"),
                             "text": text, "index": index, "section": block["section"]})
            continue
        if BARE_CAPTION.match(text) and (caption_style or neighbor):
            findings.append({"id": f"table-figure-missing-title-{index}", "type": "missing_caption_title",
                             "priority": "important", "evidence": text, "section": block["section"],
                             "message": "The caption has a number but no descriptive title."})
        if caption_style and not LABEL.match(text):
            findings.append({"id": f"table-figure-missing-number-{index}", "type": "missing_caption_number",
                             "priority": "important", "evidence": text, "section": block["section"],
                             "message": "The caption does not begin with a numbered Table or Figure label."})
        for match in LABEL.finditer(text):
            for number in _mentioned_numbers(text, match):
                callouts.append({"kind": _normalise_kind(match.group("kind")),
                                 "number": number, "text": text,
                                 "label": f"{match.group('kind')} {number}", "index": index, "section": block["section"]})

    caption_keys = {_key(c["kind"], c["number"]) for c in captions}
    callout_keys = {_key(c["kind"], c["number"]) for c in callouts}
    caption_counts = Counter(_key(c["kind"], c["number"]) for c in captions)
    for caption in captions:
        key = _key(caption["kind"], caption["number"])
        if caption_counts[key] > 1:
            findings.append({"id": f"table-figure-duplicate-{caption['index']}", "type": "duplicate_number",
                             "priority": "important", "evidence": caption["text"], "section": caption["section"],
                             "message": f"More than one {caption['kind']} uses number {caption['number']}."})
        if key not in callout_keys:
            findings.append({"id": f"table-figure-unreferenced-{caption['index']}", "type": "unreferenced",
                             "priority": "important", "evidence": caption["text"], "section": caption["section"],
                             "message": f"{caption['kind'].title()} {caption['number']} has no matching mention in the manuscript text.",
                             **_introduction_target(blocks, caption)})
        elif min(c["index"] for c in callouts if _key(c["kind"], c["number"]) == key) > caption["index"]:
            findings.append({"id": f"table-figure-late-callout-{caption['index']}", "type": "first_mention_after",
                             "priority": "optional", "evidence": caption["text"], "section": caption["section"],
                             "message": "The first mention appears after this caption. Introduce it before the display item.",
                             **_introduction_target(blocks, caption)})
    for callout in callouts:
        key = _key(callout["kind"], callout["number"])
        if key in caption_keys:
            continue
        other = _key("figure" if callout["kind"] == "table" else "table", callout["number"])
        mismatch = other in caption_keys
        findings.append({"id": f"table-figure-unknown-{callout['index']}-{callout['kind']}-{callout['number']}",
                         "type": "label_mismatch" if mismatch else "missing_target",
                         "priority": "important", "evidence": callout["text"], "section": callout["section"],
                         "message": f"{callout['label']} has no matching caption" + ("; the same number exists under the other label." if mismatch else ".")})
    for kind in ("table", "figure"):
        nums = [int(c["number"]) for c in captions if c["kind"] == kind and c["number"].isdigit()]
        if len(nums) > 1 and any(right != left + 1 for left, right in zip(nums, nums[1:])):
            findings.append({"id": f"table-figure-order-{kind}", "type": "numbering_sequence",
                             "priority": "important", "evidence": ", ".join(map(str, nums)), "section": "Document body",
                             "message": f"The {kind} numbers do not follow a simple sequence. Confirm chapter or appendix numbering before changing them."})
    if docx:
        for index, block in enumerate(blocks):
            if not block["table"]:
                continue
            adjacent = any(c["kind"] == "table" and abs(c["index"] - index) <= 2 for c in captions)
            if not adjacent:
                findings.append({"id": f"table-figure-uncaptioned-table-{index}", "type": "missing_caption",
                                 "priority": "important", "evidence": "Table without a nearby numbered caption",
                                 "section": block["section"], "message": "This Word table has no nearby numbered caption."})
    summary = {"tables": sum(c["kind"] == "table" for c in captions),
               "figures": sum(c["kind"] == "figure" for c in captions),
               "unreferenced": sum(f["type"] == "unreferenced" for f in findings),
               "missing_targets": sum(f["type"] in {"missing_target", "label_mismatch"} for f in findings),
               "numbering_or_caption": sum(f["type"] in {"duplicate_number", "numbering_sequence", "missing_caption", "missing_caption_title", "missing_caption_number"} for f in findings),
               "needs_review": len(findings)}
    return {"summary": summary, "captions": captions, "findings": findings,
            "coverage": "docx_structural" if docx else "text_only",
            "note": "Text-only extraction cannot establish that every image or table is present." if not docx else "Captions and Word tables were checked. Floating objects may require manual review."}
