"""Generate reviewable DOCX outputs without silently changing scholarly content."""

from __future__ import annotations

import io
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


def _tracked_replace(paragraph, original: str, replacement: str, change_id: int) -> bool:
    full_text = paragraph.text
    start = full_text.find(original)
    if start < 0:
        return False
    before, after = full_text[:start], full_text[start + len(original):]
    p = paragraph._p
    for child in list(p):
        if child.tag != qn("w:pPr"):
            p.remove(child)

    def normal_run(text: str):
        if not text: return
        run = OxmlElement("w:r"); node = OxmlElement("w:t"); node.set(qn("xml:space"), "preserve"); node.text = text; run.append(node); p.append(run)
    normal_run(before)
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    deletion = OxmlElement("w:del"); deletion.set(qn("w:id"), str(change_id)); deletion.set(qn("w:author"), "CiteIntegrity"); deletion.set(qn("w:date"), stamp)
    dr = OxmlElement("w:r"); dt = OxmlElement("w:delText"); dt.set(qn("xml:space"), "preserve"); dt.text = original; dr.append(dt); deletion.append(dr); p.append(deletion)
    insertion = OxmlElement("w:ins"); insertion.set(qn("w:id"), str(change_id + 1)); insertion.set(qn("w:author"), "CiteIntegrity"); insertion.set(qn("w:date"), stamp)
    ir = OxmlElement("w:r"); it = OxmlElement("w:t"); it.set(qn("xml:space"), "preserve"); it.text = replacement; ir.append(it); insertion.append(ir); p.append(insertion)
    normal_run(after)
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
            if not replacement or not (explicitly_accepted or safe_auto):
                continue
            changed = False
            for paragraph in document.paragraphs:
                if _tracked_replace(paragraph, original, replacement, change_id):
                    changed = True; change_id += 2; break
            (applied if changed else skipped).append(item.get("id"))
        document.add_paragraph("CiteIntegrity change-control note: Only explicitly accepted corrections and very-high-confidence bibliographic corrections were eligible. Claims, new citations and source replacements were not changed automatically.")
    return _docx_bytes(document), {"applied": applied, "skipped": skipped, "applied_count": len(applied)}

