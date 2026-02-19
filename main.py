# main.py
import io
import time
import traceback
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


app = FastAPI(title="Citation Crosschecker", version="2.0.0")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# -----------------------------
# Helpers
# -----------------------------
def normalize_style(s: str) -> str:
    s = (s or "").strip().lower()
    # Treat APA/Harvard as the same author-year family for your engine
    if s in ("apa", "apa7", "apa-7", "harvard", "apa/harvard", "author-year"):
        return "apa"
    if s in ("ieee",):
        return "ieee"
    if s in ("vancouver", "van", "numeric"):
        return "vancouver"
    return "apa"


def safe_get(d: Dict[str, Any], key: str, default=None):
    return d.get(key, default) if isinstance(d, dict) else default


def extract_tables(result: Dict[str, Any]) -> Dict[str, Any]:
    summary = safe_get(result, "summary", {}) or {}

    missing = safe_get(result, "missing_in_references", []) or []
    uncited = safe_get(result, "uncited_references", []) or []
    recon = safe_get(result, "reconciliation_intext_to_reference", []) or []
    verify = safe_get(result, "online_verification", []) or []

    def as_rows(items, mapping_fn):
        rows = []
        for i, x in enumerate(items, start=1):
            try:
                rows.append(mapping_fn(i, x))
            except Exception:
                rows.append({"no": i, "text": str(x)})
        return rows

    missing_rows = as_rows(
        missing,
        lambda i, x: {
            "no": i,
            "citation_in_text": x.get("citation_in_text", "") if isinstance(x, dict) else str(x),
            "count_in_text": x.get("count_in_text", "") if isinstance(x, dict) else "",
        },
    )

    uncited_rows = as_rows(
        uncited,
        lambda i, x: {
            "no": i,
            "reference": (
                (x.get("reference") or x.get("reference_full") or x.get("text") or "")
                if isinstance(x, dict)
                else str(x)
            ),
            "note": x.get("note", "") if isinstance(x, dict) else "",
        },
    )

    recon_rows = as_rows(
        recon,
        lambda i, x: {
            "no": i,
            "in_text": x.get("in_text", "") if isinstance(x, dict) else str(x),
            "status": x.get("status", "") if isinstance(x, dict) else "",
            "matched_reference": x.get("matched_reference", "") if isinstance(x, dict) else "",
        },
    )

    verify_rows = as_rows(
        verify,
        lambda i, x: {
            "no": i,
            "reference": x.get("reference", "") if isinstance(x, dict) else str(x),
            "status": x.get("status", "") if isinstance(x, dict) else "",
            "source": x.get("source", "") if isinstance(x, dict) else "",
            "score": x.get("score", "") if isinstance(x, dict) else "",
            "doi": x.get("doi", "") if isinstance(x, dict) else "",
            "matched_year": x.get("matched_year", "") if isinstance(x, dict) else "",
            "matched_author": x.get("matched_author", x.get("matched_first_author", "")) if isinstance(x, dict) else "",
            "matched_title": x.get("matched_title", "") if isinstance(x, dict) else "",
            "query_used": x.get("query_used", "") if isinstance(x, dict) else "",
            "error": x.get("error", "") if isinstance(x, dict) else "",
        },
    )

    itc = int(summary.get("in_text_citations_found", 0) or 0)
    refn = int(summary.get("reference_entries_found", 0) or 0)
    miss = int(summary.get("missing_in_references", 0) or 0)
    unct = int(summary.get("uncited_references", 0) or 0)

    match_rate = 0.0
    if itc > 0:
        match_rate = max(0.0, (itc - miss) / itc) * 100.0

    verified_count = 0
    if verify_rows:
        for r in verify_rows:
            if (r.get("status") or "").lower() == "verified":
                verified_count += 1

    dashboard = {
        "in_text_citations_found": itc,
        "reference_entries_found": refn,
        "missing_in_references": miss,
        "uncited_references": unct,
        "match_rate_pct": round(match_rate, 1),
        "verified_online": verified_count,
        "verified_online_total": len(verify_rows),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    return {
        "summary": summary,
        "missing_rows": missing_rows,
        "uncited_rows": uncited_rows,
        "recon_rows": recon_rows,
        "verify_rows": verify_rows,
        "dashboard": dashboard,
    }


def filename_base(upload_name: str) -> str:
    name = (upload_name or "document").rsplit(".", 1)[0]
    safe = "".join(ch for ch in name if ch.isalnum() or ch in (" ", "_", "-")).strip()
    return safe or "document"


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
        pd.DataFrame(tables["verify_rows"]).to_excel(writer, index=False, sheet_name="Online Verification")

    return output.getvalue()


def make_word_bytes(result: Dict[str, Any]) -> bytes:
    if Document is None:
        raise RuntimeError("python-docx not installed. Add python-docx to requirements.txt")

    tables = extract_tables(result)

    doc = Document()
    doc.add_heading("Citation Crosschecker Report", level=1)
    doc.add_paragraph(f"Generated: {tables['dashboard']['timestamp']}")

    doc.add_paragraph("© Prof Anokye M. Adam, University of Cape Coast")
    doc.add_paragraph("Disclaimer: This checker can make mistakes. Always cross-check critical citations manually.")

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
        "verified_online",
        "verified_online_total",
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
        [("status", "Status"), ("in_text", "In-text"), ("matched_reference", "Matched Reference")],
    )

    add_table(
        "Online Verification",
        tables["verify_rows"],
        [
            ("status", "Status"),
            ("doi", "DOI"),
            ("matched_year", "Year"),
            ("matched_author", "Author"),
            ("matched_title", "Title"),
            ("source", "Source"),
            ("score", "Score"),
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
        c.drawString(x, y, text[:1200])
        y -= dy
        if y < 2 * cm:
            c.showPage()
            y = height - 2 * cm

    c.setFont("Helvetica-Bold", 16)
    line("Citation Crosschecker Report", dy=20)

    c.setFont("Helvetica", 10)
    line(f"Generated: {tables['dashboard']['timestamp']}", dy=16)
    line("© Prof Anokye M. Adam, University of Cape Coast")
    line("Disclaimer: This checker can make mistakes. Always cross-check critical citations manually.", dy=16)

    dash = tables["dashboard"]
    line(f"In-text citations found: {dash['in_text_citations_found']}")
    line(f"Reference entries found: {dash['reference_entries_found']}")
    line(f"Missing in references: {dash['missing_in_references']}")
    line(f"Uncited references: {dash['uncited_references']}")
    line(f"Match rate (%): {dash['match_rate_pct']}")
    line(f"Online verified: {dash['verified_online']} / {dash['verified_online_total']}", dy=18)

    c.setFont("Helvetica-Bold", 12)
    line("Online Verification (first 40)", dy=16)
    c.setFont("Helvetica", 9)
    for r in tables["verify_rows"][:40]:
        line(f"- {r.get('status','')} | DOI: {r.get('doi','')}"[:120])
        mt = (r.get("matched_title", "") or "")[:120]
        if mt:
            line(f"  {mt}", dy=12)

    c.save()
    return out.getvalue()


def error_payload(e: Exception) -> Dict[str, Any]:
    return {
        "error": str(e),
        "traceback": traceback.format_exc(limit=8),
    }


# -----------------------------
# Routes
# -----------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "ok"}


# -----------------------------
# FAST CHECK (NO ONLINE VERIFY)
# -----------------------------
@app.post("/check")
async def check(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    try:
        t0 = time.time()
        file_bytes = await file.read()
        filename = file.filename or "uploaded"
        style_norm = normalize_style(style)

        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_norm,
            verify_online=False,
        )

        result["style"] = style_norm
        result["elapsed_seconds"] = round(time.time() - t0, 3)
        result["_ui"] = extract_tables(result)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse(error_payload(e), status_code=500)


# -----------------------------
# ONLINE VERIFICATION (SEPARATE)
# -----------------------------
@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
    throttle_s: float = Form(0.25),
    max_verify: int = Form(0),          # 0 = all
    verify_mode: str = Form("missing"), # "missing" faster default
):
    try:
        t0 = time.time()
        file_bytes = await file.read()
        filename = file.filename or "uploaded"
        style_norm = normalize_style(style)

        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_norm,
            verify_online=True,
            verify_mode=(verify_mode or "missing"),
            max_verify=int(max_verify or 0),
            throttle_s=float(throttle_s or 0.25),
            use_crossref=bool(use_crossref),
            use_openalex=bool(use_openalex),
        )

        result["style"] = style_norm
        result["verify_elapsed_seconds"] = round(time.time() - t0, 3)
        result["_ui"] = extract_tables(result)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse(error_payload(e), status_code=500)


# -----------------------------
# Exports (use /check logic: no verify by default)
# -----------------------------
@app.post("/export/excel")
async def export_excel(file: UploadFile = File(...), style: str = Form("apa")):
    try:
        file_bytes = await file.read()
        filename = file.filename or "uploaded"
        style_norm = normalize_style(style)

        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_norm,
            verify_online=False,
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
        return JSONResponse(error_payload(e), status_code=500)


@app.post("/export/word")
async def export_word(file: UploadFile = File(...), style: str = Form("apa")):
    try:
        file_bytes = await file.read()
        filename = file.filename or "uploaded"
        style_norm = normalize_style(style)

        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_norm,
            verify_online=False,
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
        return JSONResponse(error_payload(e), status_code=500)


@app.post("/export/pdf")
async def export_pdf(file: UploadFile = File(...), style: str = Form("apa")):
    try:
        file_bytes = await file.read()
        filename = file.filename or "uploaded"
        style_norm = normalize_style(style)

        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_norm,
            verify_online=False,
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
        return JSONResponse(error_payload(e), status_code=500)
