# main.py
import io
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

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
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas
except Exception:
    canvas = None


app = FastAPI(title="Citation Crosschecker", version="1.2.0")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# -----------------------------
# Helpers
# -----------------------------
def normalize_style(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("apa", "apa7", "apa-7", "harvard", "author-year"):
        return "apa"
    if s in ("ieee",):
        return "ieee"
    if s in ("vancouver", "van", "numeric"):
        return "vancouver"
    return "apa"

def normalize_verify_mode(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("doi", "doi_only", "doi-only"):
        return "doi_only"
    return "metadata"

def safe_get(d: Dict[str, Any], key: str, default=None):
    return d.get(key, default) if isinstance(d, dict) else default

def _add_row_numbers(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for i, r in enumerate(rows or []):
        rr = dict(r) if isinstance(r, dict) else {"value": str(r)}
        rr["no"] = rr.get("no", i + 1)
        out.append(rr)
    return out

def extract_tables(result: Dict[str, Any]) -> Dict[str, Any]:
    summary = safe_get(result, "summary", {}) or {}

    missing_rows = safe_get(result, "missing_in_references_table", []) or []
    uncited_rows = safe_get(result, "uncited_references_table", []) or []
    recon_rows = safe_get(result, "intext_to_reference_table", []) or []

    online_rows = safe_get(result, "online_verification_table", []) or []
    online_meta = safe_get(result, "online_verification_meta", {}) or {}

    missing_rows = _add_row_numbers(missing_rows)
    uncited_rows = _add_row_numbers(uncited_rows)
    recon_rows = _add_row_numbers(recon_rows)
    online_rows = _add_row_numbers(online_rows)

    itc = int(summary.get("in_text_citations_found", 0) or 0)
    refn = int(summary.get("reference_entries_found", 0) or 0)
    miss = int(summary.get("missing_in_references", 0) or 0)
    unct = int(summary.get("uncited_references", 0) or 0)

    match_rate = 0.0
    if itc > 0:
        match_rate = max(0.0, (itc - miss) / itc) * 100.0

    dashboard = {
        "in_text_citations_found": itc,
        "reference_entries_found": refn,
        "missing_in_references": miss,
        "uncited_references": unct,
        "match_rate_pct": round(match_rate, 1),

        "online_verification_enabled": bool(summary.get("online_verification_enabled", False)),
        "online_verified_rows": int(summary.get("online_verified_rows", 0) or 0),
        "online_mode": str(summary.get("online_verification_mode", "")),
        "online_elapsed_seconds": float(online_meta.get("elapsed_seconds", 0) or 0),

        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    return {
        "summary": summary,
        "missing_rows": missing_rows,
        "uncited_rows": uncited_rows,
        "recon_rows": recon_rows,
        "online_rows": online_rows,
        "online_meta": online_meta,
        "dashboard": dashboard,
    }


# -----------------------------
# Export builders
# -----------------------------
def make_excel_bytes(result: Dict[str, Any]) -> bytes:
    if pd is None:
        raise RuntimeError("pandas not installed. Add pandas + openpyxl to requirements.txt")

    tables = extract_tables(result)
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame([tables["dashboard"]]).to_excel(writer, index=False, sheet_name="Dashboard")
        pd.DataFrame(tables["missing_rows"]).to_excel(writer, index=False, sheet_name="Missing in References")
        pd.DataFrame(tables["uncited_rows"]).to_excel(writer, index=False, sheet_name="Uncited References")
        pd.DataFrame(tables["recon_rows"]).to_excel(writer, index=False, sheet_name="Reconciliation")
        pd.DataFrame(tables["online_rows"]).to_excel(writer, index=False, sheet_name="Online Verification")

    return output.getvalue()


def make_word_bytes(result: Dict[str, Any]) -> bytes:
    if Document is None:
        raise RuntimeError("python-docx not installed. Add python-docx to requirements.txt")

    tables = extract_tables(result)
    doc = Document()
    doc.add_heading("Citation Crosschecker Report", level=1)

    meta = doc.add_paragraph()
    meta.add_run(f"Generated: {tables['dashboard']['timestamp']}\n")

    doc.add_heading("Dashboard", level=2)
    dash = tables["dashboard"]
    t = doc.add_table(rows=1, cols=2)
    t.style = "Table Grid"
    t.rows[0].cells[0].text = "Metric"
    t.rows[0].cells[1].text = "Value"
    for k in [
        "in_text_citations_found",
        "reference_entries_found",
        "missing_in_references",
        "uncited_references",
        "match_rate_pct",
        "online_verification_enabled",
        "online_verified_rows",
        "online_mode",
        "online_elapsed_seconds",
    ]:
        row = t.add_row().cells
        row[0].text = k
        row[1].text = str(dash.get(k, ""))

    def add_table(title: str, rows: List[Dict[str, Any]], cols: List[Tuple[str, str]]):
        doc.add_heading(title, level=2)
        if not rows:
            doc.add_paragraph("None.")
            return
        table = doc.add_table(rows=1, cols=len(cols))
        table.style = "Table Grid"
        for i, (_, label) in enumerate(cols):
            table.rows[0].cells[i].text = label
        for r in rows:
            cells = table.add_row().cells
            for i, (key, _) in enumerate(cols):
                cells[i].text = str(r.get(key, ""))

    add_table(
        "Missing in References",
        tables["missing_rows"],
        [("no", "No."), ("citation_in_text", "Citation in Text"), ("count_in_text", "Count")],
    )
    add_table(
        "Uncited References",
        tables["uncited_rows"],
        [("no", "No."), ("reference", "Reference"), ("note", "Note")],
    )
    add_table(
        "Reconciliation",
        tables["recon_rows"],
        [("no", "No."), ("status", "Status"), ("in_text", "In-text"), ("matched_reference", "Matched Reference")],
    )
    add_table(
        "Online Verification",
        tables["online_rows"],
        [
            ("no", "No."),
            ("doi_verified", "DOI Found"),
            ("source", "Source"),
            ("title_guess", "Title Guess"),
            ("author_guess", "Author Guess"),
            ("year_guess", "Year"),
            ("note", "Note"),
        ],
    )

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def make_pdf_bytes(result: Dict[str, Any]) -> bytes:
    if canvas is None:
        raise RuntimeError("reportlab not installed. Add reportlab to requirements.txt")

    tables = extract_tables(result)
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    width, height = A4

    x = 2 * cm
    y = height - 2 * cm

    def line(text: str, dy=14):
        nonlocal y
        c.drawString(x, y, (text or "")[:140])
        y -= dy
        if y < 2 * cm:
            c.showPage()
            y = height - 2 * cm

    c.setFont("Helvetica-Bold", 16)
    line("Citation Crosschecker Report", dy=20)

    c.setFont("Helvetica", 10)
    dash = tables["dashboard"]
    line(f"Generated: {dash['timestamp']}", dy=16)
    line(f"In-text citations found: {dash['in_text_citations_found']}")
    line(f"Reference entries found: {dash['reference_entries_found']}")
    line(f"Missing in references: {dash['missing_in_references']}")
    line(f"Uncited references: {dash['uncited_references']}")
    line(f"Match rate (%): {dash['match_rate_pct']}")
    line(f"Online verification enabled: {dash['online_verification_enabled']}")
    line(f"Online verified rows: {dash['online_verified_rows']}")
    line(f"Online elapsed (s): {dash['online_elapsed_seconds']}", dy=18)

    def section(title: str):
        c.setFont("Helvetica-Bold", 12)
        line(title, dy=16)
        c.setFont("Helvetica", 9)

    section("Missing in References")
    if not tables["missing_rows"]:
        line("None.", dy=14)
    else:
        for r in tables["missing_rows"][:120]:
            line(f"{r.get('no')}. {r.get('citation_in_text','')} (count: {r.get('count_in_text','')})")

    section("Uncited References")
    if not tables["uncited_rows"]:
        line("None.", dy=14)
    else:
        for r in tables["uncited_rows"][:120]:
            line(f"{r.get('no')}. {r.get('reference','')}"[:140])

    section("Online Verification (first 60)")
    if not tables["online_rows"]:
        line("None.", dy=14)
    else:
        for r in tables["online_rows"][:60]:
            doi = r.get("doi_verified", "") or "-"
            src = r.get("source", "") or ""
            line(f"{r.get('no')}. DOI: {doi}  {src}"[:140])

    c.save()
    return out.getvalue()


def filename_base(upload_name: str) -> str:
    name = (upload_name or "document").rsplit(".", 1)[0]
    safe = "".join(ch for ch in name if ch.isalnum() or ch in (" ", "_", "-")).strip()
    return safe or "document"


def error_json(message: str, details: str = "") -> JSONResponse:
    return JSONResponse({"error": message, "details": details[:2000]}, status_code=500)


# -----------------------------
# Routes
# -----------------------------
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
    verify_mode: str = Form("metadata"),   # "metadata" | "doi_only"
    max_verify: int = Form(20),
):
    t0 = time.time()
    try:
        file_bytes = await file.read()
        filename = file.filename or "uploaded"

        style_norm = normalize_style(style)
        verify_mode_norm = normalize_verify_mode(verify_mode)

        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_norm,
            verify_online=bool(verify_online),
            verify_mode=verify_mode_norm,
            max_verify=int(max_verify) if max_verify else 20,
        )

        result["style"] = style_norm
        result["elapsed_seconds"] = round(time.time() - t0, 3)

        tables = extract_tables(result)
        result["_ui"] = tables
        return JSONResponse(result)

    except Exception as e:
        return error_json("Check failed", str(e))


@app.post("/export/excel")
async def export_excel(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: bool = Form(False),
    verify_mode: str = Form("metadata"),
    max_verify: int = Form(20),
):
    try:
        file_bytes = await file.read()
        filename = file.filename or "uploaded"

        style_norm = normalize_style(style)
        verify_mode_norm = normalize_verify_mode(verify_mode)

        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_norm,
            verify_online=bool(verify_online),
            verify_mode=verify_mode_norm,
            max_verify=int(max_verify) if max_verify else 20,
        )

        xlsx = make_excel_bytes(result)
        base = filename_base(filename)
        out_name = f"{base}_citation_report.xlsx"

        return StreamingResponse(
            io.BytesIO(xlsx),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
        )
    except Exception as e:
        return error_json("Excel export failed", str(e))


@app.post("/export/word")
async def export_word(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: bool = Form(False),
    verify_mode: str = Form("metadata"),
    max_verify: int = Form(20),
):
    try:
        file_bytes = await file.read()
        filename = file.filename or "uploaded"

        style_norm = normalize_style(style)
        verify_mode_norm = normalize_verify_mode(verify_mode)

        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_norm,
            verify_online=bool(verify_online),
            verify_mode=verify_mode_norm,
            max_verify=int(max_verify) if max_verify else 20,
        )

        docx_bytes = make_word_bytes(result)
        base = filename_base(filename)
        out_name = f"{base}_citation_report.docx"

        return StreamingResponse(
            io.BytesIO(docx_bytes),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
        )
    except Exception as e:
        return error_json("Word export failed", str(e))


@app.post("/export/pdf")
async def export_pdf(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: bool = Form(False),
    verify_mode: str = Form("metadata"),
    max_verify: int = Form(20),
):
    try:
        file_bytes = await file.read()
        filename = file.filename or "uploaded"

        style_norm = normalize_style(style)
        verify_mode_norm = normalize_verify_mode(verify_mode)

        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_norm,
            verify_online=bool(verify_online),
            verify_mode=verify_mode_norm,
            max_verify=int(max_verify) if max_verify else 20,
        )

        pdf_bytes = make_pdf_bytes(result)
        base = filename_base(filename)
        out_name = f"{base}_citation_report.pdf"

        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
        )
    except Exception as e:
        return error_json("PDF export failed", str(e))
