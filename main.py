# main.py
import io
import traceback
from typing import Any, Dict

from fastapi import FastAPI, File, Form, UploadFile, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from engine import run_crosscheck

# Optional export libs
try:
    import pandas as pd
except Exception:
    pd = None

try:
    from docx import Document
except Exception:
    Document = None

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
except Exception:
    canvas = None


app = FastAPI(title="Citation Crosschecker", version="2.1.0")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def normalize_style(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("apa", "apa7", "apa-7"):
        return "apa"
    if s in ("ieee",):
        return "ieee"
    if s in ("vancouver", "van", "numeric"):
        return "vancouver"
    return "apa"


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "ok"}


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
        if not file:
            return JSONResponse({"error": "No file received"}, status_code=400)

        file_bytes = await file.read()
        if not file_bytes:
            return JSONResponse({"error": "Uploaded file is empty"}, status_code=400)

        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=file.filename or "uploaded",
            style=normalize_style(style),

            verify_online=verify_online,
            use_crossref=use_crossref,
            use_openalex=use_openalex,
            throttle=throttle,
            max_verify=max_verify,
        )

        return JSONResponse(result)

    except Exception as e:
        tb = traceback.format_exc()
        return JSONResponse(
            {
                "error": str(e),
                "traceback": tb
            },
            status_code=500
        )


@app.post("/export/excel")
async def export_excel(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    if pd is None:
        return JSONResponse({"error": "pandas not installed"}, status_code=500)

    try:
        file_bytes = await file.read()
        result = run_crosscheck(file_bytes, file.filename or "uploaded", normalize_style(style))

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            pd.DataFrame([result.get("summary", {})]).to_excel(writer, index=False, sheet_name="Summary")
            pd.DataFrame(result.get("missing_in_references", [])).to_excel(writer, index=False, sheet_name="Missing")
            pd.DataFrame(result.get("uncited_references", [])).to_excel(writer, index=False, sheet_name="Uncited")
            pd.DataFrame(result.get("verification", [])).to_excel(writer, index=False, sheet_name="Verification")

        return StreamingResponse(
            io.BytesIO(output.getvalue()),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="citation_report.xlsx"'},
        )

    except Exception as e:
        return JSONResponse({"error": str(e), "traceback": traceback.format_exc()}, status_code=500)


@app.post("/export/word")
async def export_word(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    if Document is None:
        return JSONResponse({"error": "python-docx not installed"}, status_code=500)

    try:
        file_bytes = await file.read()
        result = run_crosscheck(file_bytes, file.filename or "uploaded", normalize_style(style))

        doc = Document()
        doc.add_heading("Citation Crosschecker Report", level=1)

        doc.add_heading("Summary", level=2)
        for k, v in (result.get("summary", {}) or {}).items():
            doc.add_paragraph(f"{k}: {v}")

        out = io.BytesIO()
        doc.save(out)

        return StreamingResponse(
            io.BytesIO(out.getvalue()),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": 'attachment; filename="citation_report.docx"'},
        )

    except Exception as e:
        return JSONResponse({"error": str(e), "traceback": traceback.format_exc()}, status_code=500)


@app.post("/export/pdf")
async def export_pdf(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    if canvas is None:
        return JSONResponse({"error": "reportlab not installed"}, status_code=500)

    try:
        file_bytes = await file.read()
        result = run_crosscheck(file_bytes, file.filename or "uploaded", normalize_style(style))

        out = io.BytesIO()
        c = canvas.Canvas(out)

        y = 800
        c.drawString(50, y, "Citation Crosschecker Report")
        y -= 30

        for k, v in (result.get("summary", {}) or {}).items():
            c.drawString(50, y, f"{k}: {v}")
            y -= 18

        c.save()

        return StreamingResponse(
            io.BytesIO(out.getvalue()),
            media_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="citation_report.pdf"'},
        )

    except Exception as e:
        return JSONResponse({"error": str(e), "traceback": traceback.format_exc()}, status_code=500)
