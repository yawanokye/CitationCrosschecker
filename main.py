# main.py
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

try:
    from docx import Document
except Exception:
    Document = None

from engine import run_crosscheck

app = FastAPI(title="Citation Crosschecker", version="1.0.0")

# -----------------------------
# Static + homepage (fixes {"detail":"Not Found"} on /)
# -----------------------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Serve /static/style.css
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def home():
    """
    Serves the dashboard UI.
    Put your HTML at: templates/index.html
    Put your CSS at:  static/style.css
    """
    index_path = TEMPLATES_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse(
            "<h3>index.html not found</h3>"
            "<p>Create <b>templates/index.html</b> and <b>static/style.css</b>.</p>",
            status_code=500,
        )
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


# -----------------------------
# CORS
# -----------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


def _build_ui_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Converts engine.run_crosscheck output into the exact shape index.html expects:
      data._ui.dashboard
      data._ui.missing_rows
      data._ui.uncited_rows
      data._ui.recon_rows
      data.elapsed_seconds
    """
    summary = result.get("summary") or {}
    missing_rows = result.get("missing_in_references") or []
    uncited_raw = result.get("uncited_references") or []
    recon_rows = result.get("reconciliation_intext_to_reference") or []

    cites = int(summary.get("in_text_citations_found", 0) or 0)
    refs = int(summary.get("reference_entries_found", 0) or 0)

    matched = 0
    for r in recon_rows:
        if str(r.get("status", "")).strip().lower() == "matched":
            matched += 1
    match_rate = (matched / cites * 100.0) if cites > 0 else 0.0

    ui = {
        "dashboard": {
            "in_text_citations_found": cites,
            "reference_entries_found": refs,
            "missing_in_references": int(summary.get("missing_in_references", 0) or 0),
            "uncited_references": int(summary.get("uncited_references", 0) or 0),
            "match_rate_pct": int(round(match_rate)),
        },
        "missing_rows": missing_rows,
        "uncited_rows": [{"reference": r, "note": "Not cited in text"} for r in uncited_raw],
        "recon_rows": recon_rows,
    }
    return ui


def _to_excel_bytes(sheets: Dict[str, List[Dict[str, Any]]]) -> BytesIO:
    bio = BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        for sheet_name, rows in sheets.items():
            df = pd.DataFrame(rows or [])
            if df.empty:
                df = pd.DataFrame([{"note": "No rows"}])
            safe_name = sheet_name[:31]
            df.to_excel(writer, index=False, sheet_name=safe_name)
    bio.seek(0)
    return bio


def _build_word_report(filename: str, block: dict) -> BytesIO:
    if Document is None:
        raise RuntimeError("python-docx not installed")

    summary = (block or {}).get("summary") or {}
    rows = (block or {}).get("rows") or []

    doc = Document()
    doc.add_heading("Citation Crosschecker Online Verification Report", level=0)
    doc.add_paragraph(f"File: {filename}")

    doc.add_heading("Summary", level=1)
    for k in ["verified", "likely", "needs_review", "not_found", "offline", "total"]:
        doc.add_paragraph(f"{k}: {summary.get(k, 0)}")

    doc.add_heading("Rows", level=1)
    if not rows:
        doc.add_paragraph("No rows returned.")
    else:
        table = doc.add_table(rows=1, cols=6)
        hdr = table.rows[0].cells
        hdr[0].text = "Status"
        hdr[1].text = "Score"
        hdr[2].text = "DOI"
        hdr[3].text = "Source"
        hdr[4].text = "Matched Title"
        hdr[5].text = "Reference"

        for r in rows:
            row = table.add_row().cells
            row[0].text = str(r.get("status", ""))
            row[1].text = str(r.get("score", ""))
            row[2].text = str(r.get("doi", ""))
            row[3].text = str(r.get("source", ""))
            row[4].text = str(r.get("matched_title", ""))[:120]
            row[5].text = str(r.get("reference", ""))[:200]

    bio = BytesIO()
    doc.save(bio)
    bio.seek(0)
    return bio


def _build_pdf_report(filename: str, block: dict) -> BytesIO:
    summary = (block or {}).get("summary") or {}
    rows = (block or {}).get("rows") or []

    bio = BytesIO()
    c = canvas.Canvas(bio, pagesize=A4)
    width, height = A4

    y = height - 50
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, "Citation Crosschecker Online Verification Report")
    y -= 20
    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"File: {filename}")
    y -= 25

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, "Summary")
    y -= 16
    c.setFont("Helvetica", 10)
    for k in ["verified", "likely", "needs_review", "not_found", "offline", "total"]:
        c.drawString(40, y, f"{k}: {summary.get(k, 0)}")
        y -= 14

    y -= 10
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, "Rows")
    y -= 16
    c.setFont("Helvetica", 9)

    if not rows:
        c.drawString(40, y, "No rows returned.")
        c.showPage()
        c.save()
        bio.seek(0)
        return bio

    for i, r in enumerate(rows, start=1):
        line = f"{i}. {r.get('status','')} | score={r.get('score','')} | doi={r.get('doi','')} | {r.get('reference','')}"
        while line:
            c.drawString(40, y, line[:120])
            line = line[120:]
            y -= 12
            if y < 60:
                c.showPage()
                y = height - 50
                c.setFont("Helvetica", 9)
        y -= 6
        if y < 60:
            c.showPage()
            y = height - 50
            c.setFont("Helvetica", 9)

    c.save()
    bio.seek(0)
    return bio


@app.post("/check")
async def check(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    t0 = time.time()
    contents = await file.read()

    result = run_crosscheck(
        file_bytes=contents,
        filename=file.filename,
        style=style,
        verify_online=False,
    )

    ui = _build_ui_payload(result)
    elapsed = round(time.time() - t0, 3)

    return JSONResponse({**result, "_ui": ui, "elapsed_seconds": elapsed})


@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_mode: str = Form("missing"),
    max_verify: int = Form(0),
    throttle_s: float = Form(0.25),
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
):
    contents = await file.read()

    result = run_crosscheck(
        file_bytes=contents,
        filename=file.filename,
        style=style,
        verify_online=True,
        verify_mode=verify_mode,
        max_verify=max_verify,
        throttle_s=throttle_s,
        use_crossref=use_crossref,
        use_openalex=use_openalex,
    )

    block = result.get("online_verification_block") or {
        "summary": (result.get("online_verification_summary") or {}),
        "rows": (result.get("online_verification") or []),
    }

    return JSONResponse({"online_verification": block})


@app.post("/export/excel")
async def export_excel(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    contents = await file.read()

    result = run_crosscheck(
        file_bytes=contents,
        filename=file.filename,
        style=style,
        verify_online=False,
    )

    ui = _build_ui_payload(result)

    sheets = {
        "Dashboard": [ui.get("dashboard", {})],
        "Missing_in_References": ui.get("missing_rows", []),
        "Uncited_References": ui.get("uncited_rows", []),
        "Reconciliation": ui.get("recon_rows", []),
    }
    bio = _to_excel_bytes(sheets)

    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="citation_crosschecker_report.xlsx"'},
    )


@app.post("/export/word")
async def export_word(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_mode: str = Form("missing"),
    max_verify: int = Form(0),
    throttle_s: float = Form(0.25),
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
):
    contents = await file.read()

    result = run_crosscheck(
        file_bytes=contents,
        filename=file.filename,
        style=style,
        verify_online=True,
        verify_mode=verify_mode,
        max_verify=max_verify,
        throttle_s=throttle_s,
        use_crossref=use_crossref,
        use_openalex=use_openalex,
    )
    block = result.get("online_verification_block") or {"summary": {}, "rows": []}
    bio = _build_word_report(file.filename, block)

    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="verification_report.docx"'},
    )


@app.post("/export/pdf")
async def export_pdf(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_mode: str = Form("missing"),
    max_verify: int = Form(0),
    throttle_s: float = Form(0.25),
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
):
    contents = await file.read()

    result = run_crosscheck(
        file_bytes=contents,
        filename=file.filename,
        style=style,
        verify_online=True,
        verify_mode=verify_mode,
        max_verify=max_verify,
        throttle_s=throttle_s,
        use_crossref=use_crossref,
        use_openalex=use_openalex,
    )
    block = result.get("online_verification_block") or {"summary": {}, "rows": []}
    bio = _build_pdf_report(file.filename, block)

    return StreamingResponse(
        bio,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="verification_report.pdf"'},
    )
