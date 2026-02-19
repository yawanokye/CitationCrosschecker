# main.py
from io import BytesIO
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

try:
    from docx import Document
except Exception:
    Document = None

from engine import run_crosscheck

app = FastAPI(title="Citation Crosschecker", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later if you want
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/check")
async def check(
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
    return JSONResponse(result)


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

    # IMPORTANT: stable response contract
    return JSONResponse({"online_verification": block})


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

    # Print rows in a simple readable format
    for i, r in enumerate(rows, start=1):
        line = f"{i}. {r.get('status','')} | score={r.get('score','')} | doi={r.get('doi','')} | {r.get('reference','')}"
        # wrap by slicing
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
        headers={"Content-Disposition": f"attachment; filename=verification_report.docx"},
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
        headers={"Content-Disposition": f"attachment; filename=verification_report.pdf"},
    )
