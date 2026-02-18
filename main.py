import io
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, File, Form, UploadFile, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from engine import run_crosscheck

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


app = FastAPI(title="Citation Crosschecker", version="1.0.0")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# -----------------------------
# Helpers
# -----------------------------
BUILD_ID = "BUILD_2026_02_18_002"


def normalize_style(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("apa", "apa7", "apa-7"):
        return "apa"
    if s in ("ieee",):
        return "ieee"
    if s in ("vancouver", "van", "numeric"):
        return "vancouver"
    return "apa"


def safe_get(d: Dict[str, Any], key: str, default=None):
    return d.get(key, default) if isinstance(d, dict) else default


def parse_bool(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def parse_int(v: str, default: int = 0) -> int:
    try:
        return int((v or "").strip())
    except Exception:
        return default


def extract_tables(result: Dict[str, Any]) -> Dict[str, Any]:
    summary = safe_get(result, "summary", {}) or {}

    missing = safe_get(result, "missing_in_references", []) or []
    uncited = safe_get(result, "uncited_references", []) or []
    recon = safe_get(result, "reconciliation_intext_to_reference", []) or []
    online = safe_get(result, "online_verification", None)

    # Missing rows: add row numbers
    missing_rows = []
    for i, x in enumerate(missing, start=1):
        if isinstance(x, dict):
            missing_rows.append(
                {
                    "no": i,
                    "citation_in_text": x.get("citation_in_text", ""),
                    "count_in_text": x.get("count_in_text", ""),
                }
            )
        else:
            missing_rows.append({"no": i, "citation_in_text": str(x), "count_in_text": ""})

    # Uncited rows: add row numbers
    uncited_rows = []
    for i, x in enumerate(uncited, start=1):
        if isinstance(x, dict):
            uncited_rows.append(
                {
                    "no": i,
                    "reference": x.get("reference", x.get("text", str(x))),
                    "note": x.get("note", ""),
                }
            )
        else:
            uncited_rows.append({"no": i, "reference": str(x), "note": ""})

    # Recon rows: add row numbers
    recon_rows = []
    for i, x in enumerate(recon, start=1):
        if isinstance(x, dict):
            recon_rows.append(
                {
                    "no": i,
                    "in_text": x.get("in_text", ""),
                    "status": x.get("status", ""),
                    "matched_reference": x.get("matched_reference", ""),
                }
            )
        else:
            recon_rows.append({"no": i, "in_text": str(x), "status": "", "matched_reference": ""})

    itc = int(summary.get("in_text_citations_found", 0) or 0)
    refn = int(summary.get("reference_entries_found", 0) or 0)
    miss = int(summary.get("missing_in_references", 0) or 0)
    unct = int(summary.get("uncited_references", 0) or 0)

    match_rate = 0.0
    if itc > 0:
        match_rate = max(0.0, (itc - miss) / itc) * 100.0

    # Online KPI
    online_attempted = 0
    online_verified = 0
    online_rows = []
    if isinstance(online, dict):
        online_attempted = int(online.get("attempted", 0) or 0)
        online_verified = int(online.get("verified", 0) or 0)
        for i, r in enumerate(online.get("results", []) or [], start=1):
            online_rows.append(
                {
                    "no": i,
                    "verified": "Verified" if r.get("verified") else "Not verified",
                    "doi": r.get("doi", "") or "",
                    "reference": r.get("reference", "") or "",
                    "reason": r.get("reason", "") or "",
                }
            )

    dashboard = {
        "build_id": BUILD_ID,
        "in_text_citations_found": itc,
        "reference_entries_found": refn,
        "missing_in_references": miss,
        "uncited_references": unct,
        "match_rate_pct": round(match_rate, 1),
        "online_attempted": online_attempted,
        "online_verified": online_verified,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    return {
        "summary": summary,
        "missing_rows": missing_rows,
        "uncited_rows": uncited_rows,
        "recon_rows": recon_rows,
        "online_rows": online_rows,
        "dashboard": dashboard,
    }


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
        if tables["online_rows"]:
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
    meta.add_run(f"Build: {tables['dashboard']['build_id']}\n")

    doc.add_heading("Dashboard", level=2)
    dash = tables["dashboard"]
    t = doc.add_table(rows=1, cols=2)
    t.style = "Table Grid"
    hdr = t.rows[0].cells
    hdr[0].text = "Metric"
    hdr[1].text = "Value"
    for k in [
        "in_text_citations_found",
        "reference_entries_found",
        "missing_in_references",
        "uncited_references",
        "match_rate_pct",
        "online_attempted",
        "online_verified",
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
        header_cells = table.rows[0].cells
        for i, (_, label) in enumerate(cols):
            header_cells[i].text = label
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
        [("no", "No."), ("in_text", "In-text"), ("status", "Status"), ("matched_reference", "Matched Reference")],
    )

    add_table(
        "Online Verification (Crossref)",
        tables["online_rows"],
        [("no", "No."), ("verified", "Status"), ("doi", "DOI"), ("reason", "Reason"), ("reference", "Reference")],
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
        c.drawString(x, y, text[:140])
        y -= dy
        if y < 2 * cm:
            c.showPage()
            y = height - 2 * cm

    c.setFont("Helvetica-Bold", 16)
    line("Citation Crosschecker Report", dy=20)

    c.setFont("Helvetica", 10)
    dash = tables["dashboard"]
    line(f"Generated: {dash['timestamp']}", dy=16)
    line(f"Build: {dash['build_id']}", dy=16)
    line(f"In-text citations found: {dash['in_text_citations_found']}")
    line(f"Reference entries found: {dash['reference_entries_found']}")
    line(f"Missing in references: {dash['missing_in_references']}")
    line(f"Uncited references: {dash['uncited_references']}")
    line(f"Match rate (%): {dash['match_rate_pct']}", dy=18)

    line(f"Online attempted: {dash['online_attempted']}")
    line(f"Online verified: {dash['online_verified']}", dy=18)

    c.setFont("Helvetica-Bold", 12)
    line("Online Verification (first 40)", dy=16)
    c.setFont("Helvetica", 9)
    for r in tables["online_rows"][:40]:
        line(f"{r.get('no')}. {r.get('verified')} | DOI: {r.get('doi','')}")
        line((r.get("reference", "") or "")[:140], dy=12)

    c.save()
    return out.getvalue()


def filename_base(upload_name: str) -> str:
    name = (upload_name or "document").rsplit(".", 1)[0]
    safe = "".join(ch for ch in name if ch.isalnum() or ch in (" ", "_", "-")).strip()
    return safe or "document"


# -----------------------------
# Routes
# -----------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "build_id": BUILD_ID})


@app.get("/health")
async def health():
    return {"status": "ok", "build_id": BUILD_ID, "time": datetime.utcnow().isoformat() + "Z"}


@app.post("/check")
async def check(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: str = Form("0"),
    max_verify: str = Form("0"),  # 0 = ALL
):
    t0 = time.time()
    file_bytes = await file.read()
    filename = file.filename or "uploaded"

    style_norm = normalize_style(style)
    vo = parse_bool(verify_online)
    mv = parse_int(max_verify, default=0)

    result = run_crosscheck(
        file_bytes=file_bytes,
        filename=filename,
        style=style_norm,
        verify_online=vo,
        max_verify=mv,
    )

    elapsed = round(time.time() - t0, 3)
    result["style"] = style_norm
    result["elapsed_seconds"] = elapsed

    tables = extract_tables(result)
    result["_ui"] = tables

    return JSONResponse(result)


@app.post("/export/excel")
async def export_excel(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: str = Form("0"),
    max_verify: str = Form("0"),
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    vo = parse_bool(verify_online)
    mv = parse_int(max_verify, default=0)

    result = run_crosscheck(file_bytes, filename, style=style_norm, verify_online=vo, max_verify=mv)

    xlsx = make_excel_bytes(result)
    base = filename_base(filename)
    out_name = f"{base}_citation_report.xlsx"

    return StreamingResponse(
        io.BytesIO(xlsx),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )


@app.post("/export/word")
async def export_word(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: str = Form("0"),
    max_verify: str = Form("0"),
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    vo = parse_bool(verify_online)
    mv = parse_int(max_verify, default=0)

    result = run_crosscheck(file_bytes, filename, style=style_norm, verify_online=vo, max_verify=mv)

    docx_bytes = make_word_bytes(result)
    base = filename_base(filename)
    out_name = f"{base}_citation_report.docx"

    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )


@app.post("/export/pdf")
async def export_pdf(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: str = Form("0"),
    max_verify: str = Form("0"),
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    vo = parse_bool(verify_online)
    mv = parse_int(max_verify, default=0)

    result = run_crosscheck(file_bytes, filename, style=style_norm, verify_online=vo, max_verify=mv)

    pdf_bytes = make_pdf_bytes(result)
    base = filename_base(filename)
    out_name = f"{base}_citation_report.pdf"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )
