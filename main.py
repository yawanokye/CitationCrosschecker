# main.py
import io
import time
from datetime import datetime
from typing import Any, Dict, List

from fastapi import FastAPI, File, Form, UploadFile, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import pandas as pd
from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas

from engine import run_crosscheck

app = FastAPI(title="Citation Crosschecker", version="2.0")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------
# Helpers
# ---------------------------------------------------

def normalize_style(s: str) -> str:
    s = (s or "").lower()
    if s in ("apa", "apa7"):
        return "apa"
    if s in ("ieee",):
        return "ieee"
    if s in ("vancouver", "numeric"):
        return "vancouver"
    return "apa"


def extract_ui_tables(result: Dict[str, Any]) -> Dict[str, Any]:
    summary = result.get("summary", {})

    return {
        "dashboard": summary,
        "missing_rows": result.get("missing_in_references", []),
        "uncited_rows": result.get("uncited_references", []),
        "recon_rows": result.get("reconciliation_intext_to_reference", []),
        "verification_rows": result.get("verification", []),
    }


# ---------------------------------------------------
# Export generators
# ---------------------------------------------------

def make_excel_bytes(result: Dict[str, Any]) -> bytes:

    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:

        pd.DataFrame([result.get("summary", {})]).to_excel(
            writer, sheet_name="Dashboard", index=False
        )

        pd.DataFrame(result.get("missing_in_references", [])).to_excel(
            writer, sheet_name="Missing", index=False
        )

        pd.DataFrame(result.get("uncited_references", [])).to_excel(
            writer, sheet_name="Uncited", index=False
        )

        pd.DataFrame(result.get("verification", [])).to_excel(
            writer, sheet_name="Verification", index=False
        )

    return output.getvalue()


def make_word_bytes(result: Dict[str, Any]) -> bytes:

    doc = Document()
    doc.add_heading("Citation Crosschecker Report", level=1)

    doc.add_heading("Summary", level=2)
    for k, v in result.get("summary", {}).items():
        doc.add_paragraph(f"{k}: {v}")

    def add_table(title, rows):

        doc.add_heading(title, level=2)

        if not rows:
            doc.add_paragraph("None")
            return

        table = doc.add_table(rows=1, cols=len(rows[0]))

        for i, key in enumerate(rows[0].keys()):
            table.rows[0].cells[i].text = key

        for row in rows:
            cells = table.add_row().cells
            for i, val in enumerate(row.values()):
                cells[i].text = str(val)

    add_table("Missing", result.get("missing_in_references", []))
    add_table("Uncited", result.get("uncited_references", []))
    add_table("Verification", result.get("verification", []))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_pdf_bytes(result):

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    y = 800

    c.drawString(50, y, "Citation Crosschecker Report")
    y -= 40

    for k, v in result.get("summary", {}).items():
        c.drawString(50, y, f"{k}: {v}")
        y -= 20

    c.save()
    return buf.getvalue()


# ---------------------------------------------------
# Routes
# ---------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):

    return templates.TemplateResponse(
        "index.html",
        {"request": request}
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------
# CROSSCHECK
# ---------------------------------------------------

@app.post("/check")
async def check(
    file: UploadFile = File(...),
    style: str = Form("apa"),

    verify_online: bool = Form(False),
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
    throttle: float = Form(0.25),
    max_verify: int = Form(0),
):

    try:

        file_bytes = await file.read()

        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=file.filename,
            style=normalize_style(style),

            verify_online=verify_online,
            use_crossref=use_crossref,
            use_openalex=use_openalex,
            throttle=throttle,
            max_verify=max_verify,
        )

        result["_ui"] = extract_ui_tables(result)

        return JSONResponse(result)

    except Exception as e:

        return JSONResponse(
            {"error": str(e)},
            status_code=500
        )


# ---------------------------------------------------
# EXPORTS
# ---------------------------------------------------

@app.post("/export/excel")
async def export_excel(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):

    result = run_crosscheck(
        await file.read(),
        file.filename,
        normalize_style(style),
    )

    data = make_excel_bytes(result)

    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=report.xlsx"},
    )


@app.post("/export/word")
async def export_word(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):

    result = run_crosscheck(
        await file.read(),
        file.filename,
        normalize_style(style),
    )

    data = make_word_bytes(result)

    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": "attachment; filename=report.docx"},
    )


@app.post("/export/pdf")
async def export_pdf(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):

    result = run_crosscheck(
        await file.read(),
        file.filename,
        normalize_style(style),
    )

    data = make_pdf_bytes(result)

    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=report.pdf"},
    )
