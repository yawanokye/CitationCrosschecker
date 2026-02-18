import io
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, File, Form, UploadFile, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Your engine
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


app = FastAPI(title="Citation Crosschecker", version="1.0.0")

# Static + templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# -----------------------------
# Helpers
# -----------------------------
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


def extract_tables(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalise the engine output into consistent tables for UI + exports.
    Expected engine keys (based on your current outputs):
      - summary: {in_text_citations_found, reference_entries_found, missing_in_references, uncited_references}
      - missing_in_references: list[ {citation_in_text, count_in_text} ]
      - uncited_references: list[ ... ] (strings or dicts)
      - reconciliation_intext_to_reference: list[ {in_text, status, matched_reference} ]
    """
    summary = safe_get(result, "summary", {}) or {}

    missing = safe_get(result, "missing_in_references", []) or []
    uncited = safe_get(result, "uncited_references", []) or []
    recon = safe_get(result, "reconciliation_intext_to_reference", []) or []

    # Make everything list-of-dicts for easier table rendering
    missing_rows = []
    for x in missing:
        if isinstance(x, dict):
            missing_rows.append(
                {
                    "citation_in_text": x.get("citation_in_text", ""),
                    "count_in_text": x.get("count_in_text", ""),
                }
            )
        else:
            missing_rows.append({"citation_in_text": str(x), "count_in_text": ""})

    uncited_rows = []
    for x in uncited:
        if isinstance(x, dict):
            uncited_rows.append(
                {
                    "reference": x.get("reference", x.get("text", str(x))),
                    "note": x.get("note", ""),
                }
            )
        else:
            uncited_rows.append({"reference": str(x), "note": ""})

    recon_rows = []
    for x in recon:
        if isinstance(x, dict):
            recon_rows.append(
                {
                    "in_text": x.get("in_text", ""),
                    "status": x.get("status", ""),
                    "matched_reference": x.get("matched_reference", ""),
                }
            )
        else:
            recon_rows.append({"in_text": str(x), "status": "", "matched_reference": ""})

    # Extra stats (nice dashboard)
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
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    return {
        "summary": summary,
        "missing_rows": missing_rows,
        "uncited_rows": uncited_rows,
        "recon_rows": recon_rows,
        "dashboard": dashboard,
    }


def make_excel_bytes(result: Dict[str, Any]) -> bytes:
    if pd is None:
        raise RuntimeError("pandas not installed. Add pandas + openpyxl to requirements.txt")

    tables = extract_tables(result)
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        # Summary
        summary_df = pd.DataFrame([tables["dashboard"]])
        summary_df.to_excel(writer, index=False, sheet_name="Dashboard")

        missing_df = pd.DataFrame(tables["missing_rows"])
        missing_df.to_excel(writer, index=False, sheet_name="Missing in References")

        uncited_df = pd.DataFrame(tables["uncited_rows"])
        uncited_df.to_excel(writer, index=False, sheet_name="Uncited References")

        recon_df = pd.DataFrame(tables["recon_rows"])
        recon_df.to_excel(writer, index=False, sheet_name="Reconciliation")

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
    hdr = t.rows[0].cells
    hdr[0].text = "Metric"
    hdr[1].text = "Value"
    for k in [
        "in_text_citations_found",
        "reference_entries_found",
        "missing_in_references",
        "uncited_references",
        "match_rate_pct",
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
        [("citation_in_text", "Citation in Text"), ("count_in_text", "Count")],
    )

    add_table(
        "Uncited References",
        tables["uncited_rows"],
        [("reference", "Reference"), ("note", "Note")],
    )

    add_table(
        "Reconciliation",
        tables["recon_rows"],
        [("in_text", "In-text"), ("status", "Status"), ("matched_reference", "Matched Reference")],
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
        c.drawString(x, y, text[:1200])
        y -= dy
        if y < 2 * cm:
            c.showPage()
            y = height - 2 * cm

    c.setFont("Helvetica-Bold", 16)
    line("Citation Crosschecker Report", dy=20)

    c.setFont("Helvetica", 10)
    line(f"Generated: {tables['dashboard']['timestamp']}", dy=16)
    dash = tables["dashboard"]
    line(f"In-text citations found: {dash['in_text_citations_found']}")
    line(f"Reference entries found: {dash['reference_entries_found']}")
    line(f"Missing in references: {dash['missing_in_references']}")
    line(f"Uncited references: {dash['uncited_references']}")
    line(f"Match rate (%): {dash['match_rate_pct']}", dy=18)

    c.setFont("Helvetica-Bold", 12)
    line("Missing in References", dy=16)
    c.setFont("Helvetica", 10)
    if not tables["missing_rows"]:
        line("None.", dy=14)
    else:
        for r in tables["missing_rows"]:
            line(f"- {r.get('citation_in_text','')}  (count: {r.get('count_in_text','')})")

    c.setFont("Helvetica-Bold", 12)
    line("Uncited References", dy=16)
    c.setFont("Helvetica", 10)
    if not tables["uncited_rows"]:
        line("None.", dy=14)
    else:
        for r in tables["uncited_rows"]:
            line(f"- {r.get('reference','')}"[:120])

    c.setFont("Helvetica-Bold", 12)
    line("Reconciliation (first 50)", dy=16)
    c.setFont("Helvetica", 9)
    for r in tables["recon_rows"][:50]:
        line(f"- {r.get('status','')} | {r.get('in_text','')}"[:120])
        mr = (r.get("matched_reference", "") or "")[:120]
        if mr:
            line(f"  -> {mr}", dy=12)

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
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/check")
async def check(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    t0 = time.time()
    file_bytes = await file.read()
    filename = file.filename or "uploaded"

    style_norm = normalize_style(style)

    # engine returns the full report dict
    result = run_crosscheck(file_bytes, filename, style_norm) if "style" in run_crosscheck.__code__.co_varnames else run_crosscheck(file_bytes, filename)

    # add a few top-level fields for UI convenience
    elapsed = round(time.time() - t0, 3)
    result["style"] = style_norm
    result["elapsed_seconds"] = elapsed

    # include normalized tables for UI
    tables = extract_tables(result)
    result["_ui"] = tables

    return JSONResponse(result)


@app.post("/export/excel")
async def export_excel(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    result = run_crosscheck(file_bytes, filename, style_norm) if "style" in run_crosscheck.__code__.co_varnames else run_crosscheck(file_bytes, filename)

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
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    result = run_crosscheck(file_bytes, filename, style_norm) if "style" in run_crosscheck.__code__.co_varnames else run_crosscheck(file_bytes, filename)

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
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    result = run_crosscheck(file_bytes, filename, style_norm) if "style" in run_crosscheck.__code__.co_varnames else run_crosscheck(file_bytes, filename)

    pdf_bytes = make_pdf_bytes(result)
    base = filename_base(filename)
    out_name = f"{base}_citation_report.pdf"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )
