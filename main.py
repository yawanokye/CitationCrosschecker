# main.py
import io
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from engine import run_crosscheck  # expects: run_crosscheck(file_bytes, filename, style?) or returns dict


APP_TITLE = "Citation Crosschecker"
APP_VERSION = "1.0.0"

app = FastAPI(title=APP_TITLE, version=APP_VERSION)

# Static + templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ---- Simple in-memory analytics (resets when server restarts) ----
STATS = {
    "total_checks": 0,
    "total_files": 0,
    "total_chars": 0,
    "last_check_utc": None,
    "by_style": {"apa": 0, "ieee": 0, "vancouver": 0, "unknown": 0},
}


# ----------------------------
# Models for export endpoints
# ----------------------------
class ReconciliationRow(BaseModel):
    in_text: Optional[str] = None
    status: Optional[str] = None
    matched_reference: Optional[str] = None


class MissingRow(BaseModel):
    citation_in_text: Optional[str] = None
    count_in_text: Optional[int] = None


class UncitedRow(BaseModel):
    reference_entry: Optional[str] = None


class CrosscheckResult(BaseModel):
    filename: str = ""
    style: str = "unknown"
    reference_detection_message: Optional[str] = None
    text_length: Optional[int] = 0
    main_text_length: Optional[int] = 0
    references_detected: Optional[int] = 0

    summary: Dict[str, Any] = Field(default_factory=dict)

    missing_in_references: List[MissingRow] = Field(default_factory=list)
    uncited_references: List[UncitedRow] = Field(default_factory=list)
    reconciliation_intext_to_reference: List[ReconciliationRow] = Field(default_factory=list)


# ----------------------------
# Helper: safe style
# ----------------------------
def normalize_style(style: Optional[str]) -> str:
    s = (style or "").strip().lower()
    if s in {"apa", "ieee", "vancouver"}:
        return s
    return "unknown"


# ----------------------------
# Routes
# ----------------------------
@app.get("/", response_class=HTMLResponse)
def ui(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "app_version": APP_VERSION,
        },
    )


@app.get("/health")
def health():
    return {"ok": True, "app": APP_TITLE, "version": APP_VERSION, "time_utc": datetime.utcnow().isoformat() + "Z"}


@app.get("/stats")
def stats():
    return STATS


@app.post("/check")
async def check(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    t0 = time.time()

    # read bytes
    file_bytes = await file.read()
    filename = file.filename or "uploaded"

    style_norm = normalize_style(style)

    # call engine
    # Your engine may accept only (bytes, filename) or might accept style too.
    try:
        try:
            result = run_crosscheck(file_bytes=file_bytes, filename=filename, style=style_norm)  # type: ignore
        except TypeError:
            result = run_crosscheck(file_bytes=file_bytes, filename=filename)  # type: ignore
            # ensure we still annotate style
            if isinstance(result, dict) and "style" not in result:
                result["style"] = style_norm
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    if not isinstance(result, dict):
        return JSONResponse({"error": "Engine returned invalid result."}, status_code=500)

    # update analytics
    STATS["total_checks"] += 1
    STATS["total_files"] += 1
    STATS["total_chars"] += int(result.get("text_length") or result.get("chars") or 0)
    STATS["last_check_utc"] = datetime.utcnow().isoformat() + "Z"
    STATS["by_style"][style_norm] = STATS["by_style"].get(style_norm, 0) + 1

    # include timing
    result["_meta"] = {
        "processed_ms": int((time.time() - t0) * 1000),
        "received_bytes": len(file_bytes),
    }

    return JSONResponse(result)


# ----------------------------
# Export helpers
# ----------------------------
def _flatten_missing(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["citation_in_text", "count_in_text"])
    return pd.DataFrame(rows)[["citation_in_text", "count_in_text"]].fillna("")


def _flatten_uncited(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["reference_entry"])
    df = pd.DataFrame(rows)
    if "reference_entry" not in df.columns:
        df["reference_entry"] = ""
    return df[["reference_entry"]].fillna("")


def _flatten_recon(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["in_text", "status", "matched_reference"])
    df = pd.DataFrame(rows)
    for c in ["in_text", "status", "matched_reference"]:
        if c not in df.columns:
            df[c] = ""
    return df[["in_text", "status", "matched_reference"]].fillna("")


def _summary_lines(payload: Dict[str, Any]) -> List[str]:
    s = payload.get("summary") or {}
    lines = []
    for k in [
        "in_text_citations_found",
        "reference_entries_found",
        "missing_in_references",
        "uncited_references",
        "matched",
    ]:
        if k in s:
            lines.append(f"{k}: {s.get(k)}")
    if not lines:
        # fallback
        for k, v in list(s.items())[:10]:
            lines.append(f"{k}: {v}")
    return lines


# ----------------------------
# Export: Excel
# ----------------------------
@app.post("/export/excel")
async def export_excel(request: Request):
    payload = await request.json()

    # validate lightly
    result = CrosscheckResult.model_validate(payload).model_dump()

    missing = _flatten_missing([r for r in result.get("missing_in_references", [])])
    uncited = _flatten_uncited([r for r in result.get("uncited_references", [])])
    recon = _flatten_recon([r for r in result.get("reconciliation_intext_to_reference", [])])

    summary = pd.DataFrame(
        [{"metric": k, "value": v} for k, v in (result.get("summary") or {}).items()]
    ) if (result.get("summary") or {}) else pd.DataFrame(columns=["metric", "value"])

    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        summary.to_excel(writer, index=False, sheet_name="Summary")
        recon.to_excel(writer, index=False, sheet_name="Matches")
        missing.to_excel(writer, index=False, sheet_name="Missing in References")
        uncited.to_excel(writer, index=False, sheet_name="Uncited References")

    bio.seek(0)
    fname = (result.get("filename") or "crosscheck").replace(".docx", "").replace(".pdf", "")
    out_name = f"{fname}_crosscheck.xlsx"

    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )


# ----------------------------
# Export: Word
# ----------------------------
@app.post("/export/word")
async def export_word(request: Request):
    payload = await request.json()
    result = CrosscheckResult.model_validate(payload).model_dump()

    try:
        from docx import Document
    except Exception:
        return JSONResponse({"error": "python-docx not installed."}, status_code=500)

    doc = Document()
    doc.add_heading("Citation Crosscheck Report", level=1)

    doc.add_paragraph(f"File: {result.get('filename', '')}")
    doc.add_paragraph(f"Style: {result.get('style', '')}")
    doc.add_paragraph(f"Generated: {datetime.utcnow().isoformat()}Z")
    msg = result.get("reference_detection_message") or ""
    if msg:
        doc.add_paragraph(f"Reference detection: {msg}")

    doc.add_heading("Summary", level=2)
    for line in _summary_lines(result):
        doc.add_paragraph(line, style="List Bullet")

    # Matches table
    doc.add_heading("In-text ↔ Reference reconciliation", level=2)
    recon_rows = result.get("reconciliation_intext_to_reference", []) or []
    if recon_rows:
        table = doc.add_table(rows=1, cols=3)
        hdr = table.rows[0].cells
        hdr[0].text = "In-text"
        hdr[1].text = "Status"
        hdr[2].text = "Matched reference"
        for r in recon_rows[:1000]:
            row = table.add_row().cells
            row[0].text = str(r.get("in_text", "") or "")
            row[1].text = str(r.get("status", "") or "")
            row[2].text = str(r.get("matched_reference", "") or "")
    else:
        doc.add_paragraph("No reconciliation rows returned.")

    # Missing
    doc.add_heading("Missing in References", level=2)
    miss = result.get("missing_in_references", []) or []
    if miss:
        table = doc.add_table(rows=1, cols=2)
        hdr = table.rows[0].cells
        hdr[0].text = "Citation in text"
        hdr[1].text = "Count"
        for r in miss[:2000]:
            row = table.add_row().cells
            row[0].text = str(r.get("citation_in_text", "") or "")
            row[1].text = str(r.get("count_in_text", "") or "")
    else:
        doc.add_paragraph("None.")

    # Uncited
    doc.add_heading("Uncited References", level=2)
    un = result.get("uncited_references", []) or []
    if un:
        table = doc.add_table(rows=1, cols=1)
        table.rows[0].cells[0].text = "Reference entry"
        for r in un[:2000]:
            table.add_row().cells[0].text = str(r.get("reference_entry", "") or "")
    else:
        doc.add_paragraph("None.")

    bio = io.BytesIO()
    doc.save(bio)
    bio.seek(0)

    fname = (result.get("filename") or "crosscheck").replace(".docx", "").replace(".pdf", "")
    out_name = f"{fname}_crosscheck.docx"

    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )


# ----------------------------
# Export: PDF
# ----------------------------
@app.post("/export/pdf")
async def export_pdf(request: Request):
    payload = await request.json()
    result = CrosscheckResult.model_validate(payload).model_dump()

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except Exception:
        return JSONResponse({"error": "reportlab not installed."}, status_code=500)

    bio = io.BytesIO()
    c = canvas.Canvas(bio, pagesize=A4)
    width, height = A4

    def draw_line(y, text, size=10):
        c.setFont("Helvetica", size)
        c.drawString(40, y, text[:120])
        return y - (size + 4)

    y = height - 50
    y = draw_line(y, "Citation Crosscheck Report", 16)
    y -= 10
    y = draw_line(y, f"File: {result.get('filename', '')}", 11)
    y = draw_line(y, f"Style: {result.get('style', '')}", 11)
    y = draw_line(y, f"Generated: {datetime.utcnow().isoformat()}Z", 11)

    msg = result.get("reference_detection_message") or ""
    if msg:
        y = draw_line(y, f"Reference detection: {msg}", 10)

    y -= 10
    y = draw_line(y, "Summary", 13)
    for line in _summary_lines(result):
        if y < 70:
            c.showPage()
            y = height - 50
        y = draw_line(y, f"- {line}", 10)

    # Missing
    y -= 10
    if y < 120:
        c.showPage()
        y = height - 50
    y = draw_line(y, "Missing in References (top 50)", 13)
    miss = result.get("missing_in_references", []) or []
    for r in miss[:50]:
        if y < 70:
            c.showPage()
            y = height - 50
        cit = str(r.get("citation_in_text", "") or "")
        cnt = str(r.get("count_in_text", "") or "")
        y = draw_line(y, f"- {cit}  (count: {cnt})", 10)

    # Uncited
    y -= 10
    if y < 120:
        c.showPage()
        y = height - 50
    y = draw_line(y, "Uncited References (top 30)", 13)
    un = result.get("uncited_references", []) or []
    for r in un[:30]:
        if y < 70:
            c.showPage()
            y = height - 50
        entry = str(r.get("reference_entry", "") or "")
        y = draw_line(y, f"- {entry}", 9)

    c.showPage()
    c.save()
    bio.seek(0)

    fname = (result.get("filename") or "crosscheck").replace(".docx", "").replace(".pdf", "")
    out_name = f"{fname}_crosscheck.pdf"

    return StreamingResponse(
        bio,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )
