# main.py
import io
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, File, Form, UploadFile, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Engine
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


app = FastAPI(title="Citation Crosschecker", version="1.1.0")

# Static + templates
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


def _clean_uncited_value(x: Any) -> str:
    """
    Fix the user-reported issue: uncited references showing as {'reference_full': '...'}.
    Accepts strings or dicts and returns clean string.
    """
    if isinstance(x, dict):
        for k in ("reference", "reference_full", "raw", "text", "value"):
            v = x.get(k)
            if v:
                return str(v)
        return str(x)
    return str(x) if x is not None else ""


def _add_row_numbers(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for i, r in enumerate(rows or []):
        rr = dict(r) if isinstance(r, dict) else {"value": str(r)}
        rr["no"] = i + 1
        out.append(rr)
    return out


def extract_tables(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalise engine output into consistent numbered tables for UI + exports.
    Supports both legacy keys and the newer *_table keys from updated engine.py.
    """

    summary = safe_get(result, "summary", {}) or {}

    # Prefer new tables if present
    missing = safe_get(result, "missing_in_references_table", None)
    if missing is None:
        missing = safe_get(result, "missing_in_references", []) or []

    uncited = safe_get(result, "uncited_references_table", None)
    if uncited is None:
        uncited = safe_get(result, "uncited_references", []) or []

    recon = safe_get(result, "intext_to_reference_table", None)
    if recon is None:
        recon = safe_get(result, "reconciliation_intext_to_reference", []) or []

    # Online verification (optional)
    online = safe_get(result, "online_verification_table", []) or []
    online_meta = safe_get(result, "online_verification_meta", {}) or {}

    # Missing rows
    missing_rows = []
    for x in missing:
        if isinstance(x, dict):
            missing_rows.append(
                {
                    "citation_in_text": x.get("citation_in_text", x.get("citation", "")),
                    "count_in_text": x.get("count_in_text", x.get("count", "")),
                }
            )
        else:
            missing_rows.append({"citation_in_text": str(x), "count_in_text": ""})

    # Uncited rows (clean reference_full etc.)
    uncited_rows = []
    for x in uncited:
        if isinstance(x, dict):
            uncited_rows.append(
                {
                    "reference": _clean_uncited_value(x),
                    "note": x.get("note", ""),
                }
            )
        else:
            uncited_rows.append({"reference": _clean_uncited_value(x), "note": ""})

    # Reconciliation rows
    recon_rows = []
    for x in recon:
        if isinstance(x, dict):
            recon_rows.append(
                {
                    "status": x.get("status", ""),
                    "in_text": x.get("in_text", ""),
                    "matched_reference": x.get("matched_reference", x.get("reference", "")),
                }
            )
        else:
            recon_rows.append({"status": "", "in_text": str(x), "matched_reference": ""})

    # Online verification rows
    online_rows = []
    for x in online:
        if isinstance(x, dict):
            online_rows.append(
                {
                    "reference": x.get("reference", ""),
                    "title_guess": x.get("title_guess", ""),
                    "author_guess": x.get("author_guess", ""),
                    "year_guess": x.get("year_guess", ""),
                    "doi_extracted": x.get("doi_extracted", ""),
                    "doi_verified": x.get("doi_verified", ""),
                    "source": x.get("source", ""),
                    "note": x.get("note", ""),
                }
            )
        else:
            online_rows.append(
                {
                    "reference": str(x),
                    "title_guess": "",
                    "author_guess": "",
                    "year_guess": "",
                    "doi_extracted": "",
                    "doi_verified": "",
                    "source": "",
                    "note": "",
                }
            )

    # Number everything
    missing_rows = _add_row_numbers(missing_rows)
    uncited_rows = _add_row_numbers(uncited_rows)
    recon_rows = _add_row_numbers(recon_rows)
    online_rows = _add_row_numbers(online_rows)

    # Dashboard KPIs
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
    hdr = t.rows[0].cells
    hdr[0].text = "Metric"
    hdr[1].text = "Value"
    for k in [
        "in_text_citations_found",
        "reference_entries_found",
        "missing_in_references",
        "uncited_references",
        "match_rate_pct",
        "online_verification_enabled",
        "online_verified_rows",
        "online_mode",
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
    line(f"Online verified rows: {dash['online_verified_rows']}", dy=18)

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

    section("Online Verification (first 50)")
    if not tables["online_rows"]:
        line("None.", dy=14)
    else:
        for r in tables["online_rows"][:50]:
            doi = r.get("doi_verified", "") or "-"
            src = r.get("source", "") or ""
            line(f"{r.get('no')}. DOI: {doi}  {src}"[:140])

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

    # NEW: optional online verification controls
    verify_online: bool = Form(False),
    verify_mode: str = Form("metadata"),   # "metadata" or "doi_only"
    max_verify: int = Form(20),
):
    t0 = time.time()
    file_bytes = await file.read()
    filename = file.filename or "uploaded"

    style_norm = normalize_style(style)
    verify_mode_norm = normalize_verify_mode(verify_mode)

    # Run engine (crosscheck first, then optional verification)
    result = run_crosscheck(
        file_bytes=file_bytes,
        filename=filename,
        style=style_norm,
        verify_online=bool(verify_online),
        verify_mode=verify_mode_norm,
        max_verify=int(max_verify) if max_verify else 20,
    )

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
    verify_online: bool = Form(False),
    verify_mode: str = Form("metadata"),
    max_verify: int = Form(20),
):
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


@app.post("/export/word")
async def export_word(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: bool = Form(False),
    verify_mode: str = Form("metadata"),
    max_verify: int = Form(20),
):
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


@app.post("/export/pdf")
async def export_pdf(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: bool = Form(False),
    verify_mode: str = Form("metadata"),
    max_verify: int = Form(20),
):
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
