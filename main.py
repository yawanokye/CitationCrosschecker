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


app = FastAPI(title="Citation Crosschecker", version="1.1.0")

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


def safe_get(d: Dict[str, Any], key: str, default=None):
    return d.get(key, default) if isinstance(d, dict) else default


def number_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for i, r in enumerate(rows or [], start=1):
        rr = dict(r)
        rr["no"] = i
        out.append(rr)
    return out


def extract_tables(result: Dict[str, Any]) -> Dict[str, Any]:
    summary = safe_get(result, "summary", {}) or {}

    missing = safe_get(result, "missing_in_references", []) or []
    uncited = safe_get(result, "uncited_references", []) or []
    recon = safe_get(result, "reconciliation_intext_to_reference", []) or []
    verify = safe_get(result, "online_verification", {}) or {}
    verify_rows = (verify.get("rows") or []) if isinstance(verify, dict) else []
    verify_sum = (verify.get("summary") or {}) if isinstance(verify, dict) else {}

    # Missing rows (list-of-dicts)
    missing_rows = []
    for x in missing:
        if isinstance(x, dict):
            missing_rows.append({"citation_in_text": x.get("citation_in_text", ""), "count_in_text": x.get("count_in_text", "")})
        else:
            missing_rows.append({"citation_in_text": str(x), "count_in_text": ""})

    # Uncited rows (list-of-dicts)
    uncited_rows = []
    for x in uncited:
        if isinstance(x, dict):
            # accept many legacy shapes
            ref_txt = x.get("reference") or x.get("reference_full") or x.get("text") or str(x)
            uncited_rows.append({"reference": ref_txt, "note": x.get("note", "")})
        else:
            uncited_rows.append({"reference": str(x), "note": ""})

    # Reconciliation rows
    recon_rows = []
    for x in recon:
        if isinstance(x, dict):
            recon_rows.append({"status": x.get("status", ""), "in_text": x.get("in_text", ""), "matched_reference": x.get("matched_reference", "")})
        else:
            recon_rows.append({"status": "", "in_text": str(x), "matched_reference": ""})

    itc = int(summary.get("in_text_citations_found", 0) or 0)
    refn = int(summary.get("reference_entries_found", 0) or 0)
    miss = int(summary.get("missing_in_references", 0) or 0)
    unct = int(summary.get("uncited_references", 0) or 0)

    match_rate = 0.0
    if itc > 0:
        match_rate = max(0.0, (itc - miss) / itc) * 100.0

    verified_count = int((verify_sum.get("verified") or 0))
    likely_count = int((verify_sum.get("likely") or 0))
    needs_review = int((verify_sum.get("needs_review") or 0))
    not_found = int((verify_sum.get("not_found") or 0))
    offline = int((verify_sum.get("offline") or 0))
    verify_enabled = bool(verify_sum.get("enabled", False))

    dashboard = {
        "in_text_citations_found": itc,
        "reference_entries_found": refn,
        "missing_in_references": miss,
        "uncited_references": unct,
        "match_rate_pct": round(match_rate, 1),
        "verify_enabled": verify_enabled,
        "verified": verified_count,
        "likely": likely_count,
        "needs_review": needs_review,
        "not_found": not_found,
        "offline": offline,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    return {
        "dashboard": dashboard,
        "missing_rows": number_rows(missing_rows),
        "uncited_rows": number_rows(uncited_rows),
        "recon_rows": number_rows(recon_rows),
        "verify_rows": number_rows(verify_rows),
    }


def make_excel_bytes(result: Dict[str, Any]) -> bytes:
    if pd is None:
        raise RuntimeError("pandas/openpyxl not installed")
    tables = extract_tables(result)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame([tables["dashboard"]]).to_excel(writer, index=False, sheet_name="Dashboard")
        pd.DataFrame(tables["missing_rows"]).to_excel(writer, index=False, sheet_name="Missing")
        pd.DataFrame(tables["uncited_rows"]).to_excel(writer, index=False, sheet_name="Uncited")
        pd.DataFrame(tables["recon_rows"]).to_excel(writer, index=False, sheet_name="Reconciliation")
        pd.DataFrame(tables["verify_rows"]).to_excel(writer, index=False, sheet_name="Verification")
    return output.getvalue()


def make_word_bytes(result: Dict[str, Any]) -> bytes:
    if Document is None:
        raise RuntimeError("python-docx not installed")
    tables = extract_tables(result)

    doc = Document()
    doc.add_heading("Citation Crosschecker Report", level=1)
    doc.add_paragraph(f"Generated: {tables['dashboard']['timestamp']}")

    # Dashboard table
    doc.add_heading("Dashboard", level=2)
    dash = tables["dashboard"]
    t = doc.add_table(rows=1, cols=2)
    t.style = "Table Grid"
    t.rows[0].cells[0].text = "Metric"
    t.rows[0].cells[1].text = "Value"
    for k in [
        "in_text_citations_found", "reference_entries_found",
        "missing_in_references", "uncited_references", "match_rate_pct",
        "verify_enabled", "verified", "likely", "needs_review", "not_found", "offline"
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

    add_table("Missing in References", tables["missing_rows"], [("no", "No."), ("citation_in_text", "Citation in Text"), ("count_in_text", "Count")])
    add_table("Uncited References", tables["uncited_rows"], [("no", "No."), ("reference", "Reference"), ("note", "Note")])
    add_table("Reconciliation", tables["recon_rows"], [("no", "No."), ("status", "Status"), ("in_text", "In-text"), ("matched_reference", "Matched Reference")])
    add_table("Online Verification", tables["verify_rows"], [
        ("no", "No."), ("status", "Status"), ("source", "Source"), ("score", "Score"),
        ("doi", "DOI"), ("matched_year", "Year"), ("matched_authors", "Authors"), ("matched_title", "Title")
    ])

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def make_pdf_bytes(result: Dict[str, Any]) -> bytes:
    if canvas is None:
        raise RuntimeError("reportlab not installed")
    tables = extract_tables(result)

    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    width, height = A4

    x = 2 * cm
    y = height - 2 * cm

    def line(text: str, dy=14):
        nonlocal y
        c.drawString(x, y, (text or "")[:130])
        y -= dy
        if y < 2 * cm:
            c.showPage()
            y = height - 2 * cm

    c.setFont("Helvetica-Bold", 16)
    line("Citation Crosschecker Report", dy=20)

    c.setFont("Helvetica", 10)
    dash = tables["dashboard"]
    line(f"Generated: {dash['timestamp']}", dy=16)
    for k in ["in_text_citations_found", "reference_entries_found", "missing_in_references", "uncited_references", "match_rate_pct"]:
        line(f"{k}: {dash.get(k)}")
    if dash.get("verify_enabled"):
        line(f"Verified: {dash.get('verified')} | Likely: {dash.get('likely')} | Needs review: {dash.get('needs_review')} | Not found: {dash.get('not_found')}", dy=18)

    c.setFont("Helvetica-Bold", 12)
    line("Missing in References (first 50)", dy=16)
    c.setFont("Helvetica", 9)
    for r in tables["missing_rows"][:50]:
        line(f"{r.get('no')}. {r.get('citation_in_text')} (count={r.get('count_in_text')})")

    c.setFont("Helvetica-Bold", 12)
    line("Uncited References (first 30)", dy=16)
    c.setFont("Helvetica", 9)
    for r in tables["uncited_rows"][:30]:
        line(f"{r.get('no')}. {r.get('reference')}"[:130])

    c.setFont("Helvetica-Bold", 12)
    line("Online Verification (first 30)", dy=16)
    c.setFont("Helvetica", 9)
    for r in tables["verify_rows"][:30]:
        line(f"{r.get('no')}. {r.get('status')} | {r.get('doi') or ''}"[:130])

    c.save()
    return out.getvalue()


def filename_base(upload_name: str) -> str:
    name = (upload_name or "document").rsplit(".", 1)[0]
    safe = "".join(ch for ch in name if ch.isalnum() or ch in (" ", "_", "-")).strip()
    return safe or "document"


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
    throttle_s: float = Form(0.25),
    max_verify: int = Form(0),
):
    t0 = time.time()
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    try:
        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_norm,
            verify_online=bool(verify_online),
            use_crossref=bool(use_crossref),
            use_openalex=bool(use_openalex),
            throttle_s=float(throttle_s or 0.0),
            max_verify=int(max_verify or 0),
        )
        elapsed = round(time.time() - t0, 3)
        result["elapsed_seconds"] = elapsed
        result["_ui"] = extract_tables(result)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)[:400]}, status_code=500)


@app.post("/export/excel")
async def export_excel(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: bool = Form(False),
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
    throttle_s: float = Form(0.25),
    max_verify: int = Form(0),
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    result = run_crosscheck(
        file_bytes=file_bytes,
        filename=filename,
        style=style_norm,
        verify_online=bool(verify_online),
        use_crossref=bool(use_crossref),
        use_openalex=bool(use_openalex),
        throttle_s=float(throttle_s or 0.0),
        max_verify=int(max_verify or 0),
    )

    xlsx = make_excel_bytes(result)
    out_name = f"{filename_base(filename)}_citation_report.xlsx"

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
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
    throttle_s: float = Form(0.25),
    max_verify: int = Form(0),
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    result = run_crosscheck(
        file_bytes=file_bytes,
        filename=filename,
        style=style_norm,
        verify_online=bool(verify_online),
        use_crossref=bool(use_crossref),
        use_openalex=bool(use_openalex),
        throttle_s=float(throttle_s or 0.0),
        max_verify=int(max_verify or 0),
    )

    docx_bytes = make_word_bytes(result)
    out_name = f"{filename_base(filename)}_citation_report.docx"

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
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
    throttle_s: float = Form(0.25),
    max_verify: int = Form(0),
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    result = run_crosscheck(
        file_bytes=file_bytes,
        filename=filename,
        style=style_norm,
        verify_online=bool(verify_online),
        use_crossref=bool(use_crossref),
        use_openalex=bool(use_openalex),
        throttle_s=float(throttle_s or 0.0),
        max_verify=int(max_verify or 0),
    )

    pdf_bytes = make_pdf_bytes(result)
    out_name = f"{filename_base(filename)}_citation_report.pdf"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )
