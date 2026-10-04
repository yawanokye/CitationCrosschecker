"""Generate reviewable DOCX outputs without silently changing scholarly content."""

from __future__ import annotations

import io
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor


def _docx_bytes(document: Document) -> bytes:
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _load_docx(original_bytes: bytes | None) -> Tuple[Document, bool]:
    if original_bytes and original_bytes[:2] == b"PK":
        try:
            return Document(io.BytesIO(original_bytes)), True
        except Exception:
            pass
    return Document(), False


def _insert_annotation_after(paragraph, text: str) -> None:
    p = OxmlElement("w:p")
    r = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "B45309")
    bold = OxmlElement("w:b")
    rpr.extend([color, bold])
    t = OxmlElement("w:t")
    t.text = text
    r.extend([rpr, t])
    p.append(r)
    paragraph._p.addnext(p)


def build_annotated_document(original_bytes: bytes | None, plan: Dict[str, Any], original_name: str = "manuscript.docx") -> bytes:
    document, copied_original = _load_docx(original_bytes)
    if not copied_original:
        document.add_heading("CiteIntegrity Annotated Manuscript Review", level=1)
        document.add_paragraph(f"The original file {original_name} could not be reconstructed as DOCX. This document contains the complete annotation register.")
    items = plan.get("items") or []
    unmatched: List[Dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        evidence = str(item.get("evidence") or "").strip()
        matched = False
        if copied_original and evidence:
            key = evidence[:120].lower()
            for paragraph in document.paragraphs:
                if key and key in paragraph.text.lower():
                    for run in paragraph.runs:
                        if key[:40] in run.text.lower():
                            run.font.highlight_color = WD_COLOR_INDEX.YELLOW
                    _insert_annotation_after(
                        paragraph,
                        f"CiteIntegrity {item.get('id')}: {item.get('priority','').upper()} | {item.get('what_is_wrong') or item.get('title')} | Recommended action: {item.get('recommended_action')} | Confidence: {item.get('confidence','review required')} | Decision: {item.get('decision','pending')}",
                    )
                    matched = True
                    break
        if not matched:
            unmatched.append(item)

    document.add_page_break()
    document.add_heading("CiteIntegrity Correction Register", level=1)
    document.add_paragraph(plan.get("headline") or "Human review is required before submission.")
    table = document.add_table(rows=1, cols=7)
    table.style = "Table Grid"
    headers = ["ID", "Priority", "Location", "What is wrong", "Why it matters", "Recommended correction", "Decision"]
    for cell, header in zip(table.rows[0].cells, headers):
        cell.text = header
    for item in items:
        cells = table.add_row().cells
        values = [
            item.get("id"), item.get("priority"), str(item.get("location") or ""),
            item.get("what_is_wrong") or item.get("title"), item.get("why_it_matters"),
            item.get("recommended_action"), item.get("decision", "pending"),
        ]
        for cell, value in zip(cells, values):
            cell.text = str(value or "")
    document.add_paragraph("Annotations are advisory. New citations, source replacements, claims and interpretations require the student's explicit approval and verification.")
    return _docx_bytes(document)


def _normalised_raw_span(raw_text: str, target_text: str):
    """Map a whitespace-normalised target back to its exact Word text span."""
    target = " ".join(str(target_text or "").split())
    if not raw_text or not target:
        return None
    normalised_chars = []
    raw_positions = []
    in_space = False
    for raw_index, char in enumerate(raw_text):
        if char.isspace():
            if not in_space:
                normalised_chars.append(" ")
                raw_positions.append(raw_index)
            in_space = True
        else:
            normalised_chars.append(char)
            raw_positions.append(raw_index)
            in_space = False
    normalised = "".join(normalised_chars)
    start = normalised.find(target)
    if start < 0:
        return None
    end_index = start + len(target) - 1
    if end_index >= len(raw_positions):
        return None
    return raw_positions[start], raw_positions[end_index] + 1


def _tracked_replace(paragraph, original: str, replacement: str, change_id: int, replacement_segments=None) -> bool:
    full_text = paragraph.text
    start = full_text.find(original)
    if start < 0:
        span = _normalised_raw_span(full_text, original)
        if not span:
            return False
        start, end = span
    else:
        end = start + len(original)
    p = paragraph._p
    runs = [child for child in p if child.tag == qn("w:r")]
    # A hyperlink, field, drawing, break, or prior complex revision requires
    # more precise structural handling than a safe text replacement can offer.
    if any(child.tag not in {qn("w:rPr"), qn("w:t")} for run in runs for child in run):
        return False
    raw = "".join("".join(node.text or "" for node in run if node.tag == qn("w:t")) for run in runs)
    if raw != full_text:
        return False
    segments = replacement_segments if isinstance(replacement_segments, list) and replacement_segments else [{"text": replacement}]

    def copy_text_run(source, text):
        run = OxmlElement("w:r")
        props = source.find(qn("w:rPr"))
        if props is not None:
            run.append(deepcopy(props))
        node = OxmlElement("w:t"); node.set(qn("xml:space"), "preserve"); node.text = text
        run.append(node)
        return run

    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    deletion = OxmlElement("w:del"); deletion.set(qn("w:id"), str(change_id)); deletion.set(qn("w:author"), "CiteIntegrity"); deletion.set(qn("w:date"), stamp)
    insertion = OxmlElement("w:ins"); insertion.set(qn("w:id"), str(change_id + 1)); insertion.set(qn("w:author"), "CiteIntegrity"); insertion.set(qn("w:date"), stamp)
    offset = 0
    matches = []
    for run in runs:
        text = "".join(node.text or "" for node in run if node.tag == qn("w:t"))
        run_start, run_end = offset, offset + len(text)
        offset = run_end
        if run_start < end and run_end > start:
            matches.append((run, text, run_start, run_end))
    if not matches:
        return False
    for run, text, run_start, run_end in matches:
        left = max(0, start - run_start)
        right = min(len(text), end - run_start)
        if left:
            run.addprevious(copy_text_run(run, text[:left]))
        old_run = copy_text_run(run, text[left:right])
        old_run.find(qn("w:t")).tag = qn("w:delText")
        deletion.append(old_run)
        if run is matches[-1][0]:
            run.addprevious(deletion)
            # Word retains formatting around the changed span unless the
            # replacement explicitly requests italic reference segments.
            old_properties = matches[0][0].find(qn("w:rPr"))
            for segment in segments:
                text_value = str((segment or {}).get("text") or "")
                if not text_value:
                    continue
                new_run = OxmlElement("w:r")
                if old_properties is not None:
                    new_run.append(deepcopy(old_properties))
                if (segment or {}).get("italic"):
                    props = new_run.find(qn("w:rPr"))
                    if props is None:
                        props = OxmlElement("w:rPr"); new_run.insert(0, props)
                    props.append(OxmlElement("w:i"))
                    props.append(OxmlElement("w:iCs"))
                node = OxmlElement("w:t"); node.set(qn("xml:space"), "preserve"); node.text = text_value
                new_run.append(node); insertion.append(new_run)
            run.addprevious(insertion)
        if right < len(text):
            run.addprevious(copy_text_run(run, text[right:]))
        p.remove(run)
    return True


def _tracked_insert_after(paragraph, anchor: str, insertion_text: str, change_id: int) -> bool:
    full_text = paragraph.text
    start = full_text.find(anchor)
    if start < 0:
        span = _normalised_raw_span(full_text, anchor)
        if not span:
            return False
        start, split_at = span
    else:
        split_at = start + len(anchor)
    p = paragraph._p
    runs = [child for child in p if child.tag == qn("w:r")]
    if any(child.tag not in {qn("w:rPr"), qn("w:t")} for run in runs for child in run):
        return False
    if "".join("".join(node.text or "" for node in run if node.tag == qn("w:t")) for run in runs) != full_text:
        return False
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    insertion = OxmlElement("w:ins"); insertion.set(qn("w:id"), str(change_id)); insertion.set(qn("w:author"), "CiteIntegrity"); insertion.set(qn("w:date"), stamp)
    ir = OxmlElement("w:r"); it = OxmlElement("w:t"); it.set(qn("xml:space"), "preserve"); it.text = " " + insertion_text.strip(); ir.append(it); insertion.append(ir)
    offset = 0
    for run in runs:
        text = "".join(node.text or "" for node in run if node.tag == qn("w:t"))
        next_offset = offset + len(text)
        if split_at <= next_offset:
            within = split_at - offset
            if within == 0:
                run.addprevious(insertion)
            elif within == len(text):
                run.addnext(insertion)
            else:
                suffix = deepcopy(run)
                for node in suffix.findall(qn("w:t")):
                    suffix.remove(node)
                for node in run.findall(qn("w:t")):
                    run.remove(node)
                before = OxmlElement("w:t"); before.set(qn("xml:space"), "preserve"); before.text = text[:within]
                after = OxmlElement("w:t"); after.set(qn("xml:space"), "preserve"); after.text = text[within:]
                run.append(before); suffix.append(after)
                run.addnext(suffix)
                suffix.addprevious(insertion)
            return True
        offset = next_offset
    return False


def _tracked_append_reference(document: Document, reference_text: str, change_id: int) -> bool:
    if not reference_text.strip():
        return False
    paragraph = document.add_paragraph()
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    insertion = OxmlElement("w:ins"); insertion.set(qn("w:id"), str(change_id)); insertion.set(qn("w:author"), "CiteIntegrity"); insertion.set(qn("w:date"), stamp)
    run = OxmlElement("w:r"); text = OxmlElement("w:t"); text.set(qn("xml:space"), "preserve"); text.text = reference_text.strip(); run.append(text); insertion.append(run); paragraph._p.append(insertion)
    return True


def build_tracked_changes_document(original_bytes: bytes | None, plan: Dict[str, Any], original_name: str = "manuscript.docx") -> Tuple[bytes, Dict[str, Any]]:
    document, copied_original = _load_docx(original_bytes)
    applied: List[str] = []
    skipped: List[str] = []
    if not copied_original:
        document.add_heading("CiteIntegrity Track Changes Report", level=1)
        document.add_paragraph(f"Track Changes could not be applied because {original_name} was not available as a valid DOCX file. Upload the DOCX version for document-level changes.")
    else:
        change_id = 1
        for item in plan.get("items") or []:
            original = str(item.get("original_text") or item.get("evidence") or "")
            replacement = str(item.get("proposed_replacement") or "")
            explicitly_accepted = item.get("decision") == "accepted"
            safe_auto = item.get("auto_apply_allowed") is True and item.get("decision") == "pending"
            operation = str(item.get("track_operation") or "replace")
            if not (explicitly_accepted or safe_auto):
                continue
            from reference_safety import is_review_instruction
            if is_review_instruction(replacement) or is_review_instruction(item.get("secondary_replacement")):
                skipped.append(item.get("id"))
                continue
            changed = False
            if operation == "insert_after_and_append_reference" and original and replacement:
                for paragraph in document.paragraphs:
                    if _tracked_insert_after(paragraph, original, replacement, change_id):
                        changed = True; change_id += 1; break
                secondary = str(item.get("secondary_replacement") or "")
                if changed and secondary:
                    if _tracked_append_reference(document, secondary, change_id):
                        change_id += 1
                    else:
                        changed = False
            elif operation == "append_reference" and replacement:
                changed = _tracked_append_reference(document, replacement, change_id)
                if changed: change_id += 1
            elif operation == "insert_after" and original and replacement:
                for paragraph in document.paragraphs:
                    if _tracked_insert_after(paragraph, original, replacement, change_id):
                        changed = True; change_id += 1; break
            elif operation == "delete" and original:
                for paragraph in document.paragraphs:
                    if _tracked_replace(paragraph, original, "", change_id):
                        changed = True; change_id += 2; break
            elif operation == "replace_all" and original and replacement:
                for paragraph in document.paragraphs:
                    # Each replacement removes the original from the ordinary
                    # runs; revision XML retains the deletion for Word review.
                    for _ in range(100):
                        if not _tracked_replace(paragraph, original, replacement, change_id):
                            break
                        changed = True; change_id += 2
            elif replacement:
                replacement_segments = ((item.get("supporting_metadata") or {}).get("reference_format_segments") if item.get("category") == "reference_style" else None)
                for paragraph in document.paragraphs:
                    if _tracked_replace(paragraph, original, replacement, change_id, replacement_segments):
                        changed = True; change_id += 2; break
            (applied if changed else skipped).append(item.get("id"))
        document.add_paragraph("CiteIntegrity change-control note: Accepted citations, recovered references, claim revisions, academic-voice revisions and uncited-reference actions were applied as tracked changes only after explicit approval. No scholarly source or claim change was applied silently.")
    return _docx_bytes(document), {"applied": applied, "skipped": skipped, "applied_count": len(applied)}
