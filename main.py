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


app = FastAPI(title="Citation Crosschecker", version="1.0.1")

# Static + templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# -----------------------------
# Helpers
# -----------------------------
def normalize_style(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("apa", "apa7", "apa-7", "harvard"):
        return "apa"
    if s in ("ieee",):
        return "ieee"
    if s in ("vancouver", "van", "numeric"):
        return "vancouver"
    return "apa"


def safe_get(d: Dict[str, Any], key: str, default=None):
    return d.get(key, default) if isinstance(d, dict) else default


def _as_text(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, dict):
        for k in ("reference", "reference_full", "text", "raw", "full"):
            if k in x and isinstance(x[k], str):
                return x[k].strip()
        vals = [v.strip() for v in x.values() if isinstance(v, str) and v.strip()]
        return " ".join(vals).strip()
    if isinstance(x, (list, tuple)):
        return " ".join(_as_text(i) for i in x if _as_text(i)).strip()
    return str(x).strip()


def _ensure_row_ids(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for i, r in enumerate(rows or []):
        if not isinstance(r, dict):
            out.append({"row_id": i + 1, "value": _as_text(r)})
        else:
            rr = dict(r)
            rr.setdefault("row_id", i + 1)
            out.append(rr)
    return out


def extract_tables(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalise the engine output into consistent numbered tables for UI + exports.

    Preferred keys (from updated engine.py):
      - missing_in_references_table
      - uncited_references_table
      - intext_to_reference_table
      - reference_to_intext_table
      - online_verification_table
      - summary
    Fallbacks supported for older engine outputs.
    """
    summary = safe_get(result, "summary", {}) or {}

    # Prefer engine export-friendly tables
    missing_rows = safe_get(result, "missing_in_references_table", None)
    uncited_rows = safe_get(result, "uncited_references_table", None)
    recon_rows = safe_get(result, "intext_to_reference_table", None)
    ref_to_intext_rows = safe_get(result, "reference_to_intext_table", None)
    verify_rows = safe_get(result, "online_verification_table", None)

    # Fallbacks if those keys aren’t present
    if missing_rows is None:
        missing = safe_get(result, "missing_in_references", []) or []
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
                missing_rows.append({"citation_in_text": _as_text(x), "count_in_text": ""})

    if uncited_rows is None:
        uncited = safe_get(result, "uncited_references", []) or []
        uncited_rows = []
        for x in uncited:
            uncited_rows.append({"reference": _as_text(x), "note": ""})

    if recon_rows is None:
        recon = safe_get(result, "reconciliation_intext_to_reference", []) or []
        recon_rows = []
        for x in recon:
            if isinstance(x, dict):
                recon_rows.append(
                    {
                        "in_text": _as_text(x.get("in_text", "")),
                        "status": _as_text(x.get("status", "")),
                        "matched_reference": _as_text(x.get("matched_reference", "")),
                    }
                )
            else:
                recon_rows.append({"in_text": _as_text(x), "status": "", "matched_reference": ""})

    if ref_to_intext_rows is None:
        ref_to_intext_rows = safe_get(result, "reconciliation_reference_to_intext", []) or []
        # ensure a joined column for easier exports
        tmp = []
        for x in ref_to_intext_rows:
            if isinstance(x, dict):
                cited_by = x.get("cited_by", []) or []
                cited_by_joined = " | ".join(_as_text(c) for c in cited_by if _as_text(c))
                tmp.append(
                    {
                        "reference": _as_text(x.get("reference", "")),
                        "times_cited": int(x.get("times_cited", 0) or 0),
                        "cited_by_joined": cited_by_joined,
                    }
                )
            else:
                tmp.append({"reference": _as_text(x), "times_cited": 0, "cited_by_joined": ""})
        ref_to_intext_rows = tmp

    if verify_rows is None:
        verify_rows = safe_get(result, "online_verification", []) or []
        tmp = []
        for x in verify_rows:
            if isinstance(x, dict):
                tmp.append(x)
            else:
                tmp.append({"reference": _as_text(x)})
        verify_rows = tmp

    # Enforce numbering
    missing_rows = _ensure_row_ids(missing_rows)
    uncited_rows = _ensure_row_ids(uncited_rows)
    recon_rows = _ensure_row_ids(recon_rows)
    ref_to_intext_rows = _ensure_row_ids(ref_to_intext_rows)
    verify_rows = _ensure_row_ids(verify_rows)

    # Dashboard stats
    itc = int(summary.get("in_text_citations_found", 0) or 0)
    refn = int(summary.get("reference_entries_found", 0) or 0)
    miss = int(summary.get("missing_in_references", len(missing_rows)) or 0)
    unct = int(summary.get("uncited_references", len(uncited_rows)) or 0)

    match_rate = 0.0
    if itc > 0:
        match_rate = max(0.0, (itc - miss) / itc) * 100.0

    dashboard = {
        "in_text_citations_found": itc,
        "reference_entries_found": refn,
        "missing_in_references": miss,
        "uncited_references": unct,
        "match_rate_pct": round(match_rate, 1),
        "verified_rows": len(verify_rows or []),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    return {
        "summary": summary,
        "missing_rows": missing_rows,
        "uncited_rows": uncited_rows,
        "recon_rows": recon_rows,
        "ref_to_intext_rows": ref_to_intext_rows,
        "verify_rows": verify_rows,
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
        pd.DataFrame(tables["recon_rows"]).to_excel(writer, index=False, sheet_name="Intext to Reference")
        pd.DataFrame(tables["ref_to_intext_rows"]).to_excel(writer, index=False, sheet_name="Reference to Intext")
        pd.DataFrame(tables["verify_rows"]).to_excel(writer, index=False, sheet_name="Crossref OpenAlex")

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
        "verified_rows",
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
        [("row_id", "#"), ("citation_in_text", "Citation in Text"), ("count_in_text", "Count")],
    )

    add_table(
        "Uncited References",
        tables["uncited_rows"],
        [("row_id", "#"), ("reference", "Reference"), ("note", "Note")],
    )

    add_table(
        "In-text to Reference",
        tables["recon_rows"],
        [("row_id", "#"), ("in_text", "In-text"), ("status", "Status"), ("matched_reference", "Matched Reference")],
    )

    add_table(
        "Reference to In-text",
        tables["ref_to_intext_rows"],
        [("row_id", "#"), ("reference", "Reference"), ("times_cited", "Times Cited"), ("cited_by_joined", "Cited By")],
    )

    add_table(
        "Crossref and OpenAlex Verification",
        tables["verify_rows"],
        [
            ("row_id", "#"),
            ("reference", "Reference"),
            ("doi_extracted", "DOI"),
            ("crossref_found", "Crossref"),
            ("crossref_doi", "Crossref DOI"),
            ("openalex_found", "OpenAlex"),
            ("openalex_id", "OpenAlex ID"),
            ("openalex_year", "Year"),
            ("openalex_cited_by", "Cited By"),
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
        c.drawString(x, y, (text or "")[:110])
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
    line(f"Verified rows (Crossref/OpenAlex): {dash['verified_rows']}", dy=18)

    c.setFont("Helvetica-Bold", 12)
    line("Missing in References", dy=16)
    c.setFont("Helvetica", 10)
    if not tables["missing_rows"]:
        line("None.", dy=14)
    else:
        for r in tables["missing_rows"][:150]:
            line(f"{r.get('row_id')}. {r.get('citation_in_text','')} (count: {r.get('count_in_text','')})", dy=12)

    c.setFont("Helvetica-Bold", 12)
    line("Uncited References", dy=16)
    c.setFont("Helvetica", 10)
    if not tables["uncited_rows"]:
        line("None.", dy=14)
    else:
        for r in tables["uncited_rows"][:150]:
            line(f"{r.get('row_id')}. {r.get('reference','')}", dy=12)

    c.setFont("Helvetica-Bold", 12)
    line("Crossref and OpenAlex Verification (first 50)", dy=16)
    c.setFont("Helvetica", 9)
    if not tables["verify_rows"]:
        line("No online verification results.", dy=12)
    else:
        for r in tables["verify_rows"][:50]:
            line(f"{r.get('row_id')}. DOI: {r.get('doi_extracted','')}", dy=11)
            line(f"   Crossref: {r.get('crossref_found','')}  OpenAlex: {r.get('openalex_found','')}", dy=11)

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

    # Engine returns the full report dict
    result = run_crosscheck(file_bytes, filename, style=style_norm)

    elapsed = round(time.time() - t0, 3)
    result["style"] = style_norm
    result["elapsed_seconds"] = elapsed

    # Include normalized tables for UI
    result["_ui"] = extract_tables(result)

    return JSONResponse(result)


@app.post("/export/excel")
async def export_excel(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    result = run_crosscheck(file_bytes, filename, style=style_norm)

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

    result = run_crosscheck(file_bytes, filename, style=style_norm)

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

    result = run_crosscheck(file_bytes, filename, style=style_norm)

    pdf_bytes = make_pdf_bytes(result)
    base = filename_base(filename)
    out_name = f"{base}_citation_report.pdf"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )
