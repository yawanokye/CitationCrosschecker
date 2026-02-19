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


app = FastAPI(title="Citation Crosschecker", version="1.0.0")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# -----------------------------
# Helpers
# -----------------------------
def normalize_style(s: str) -> str:
    s = (s or "").strip().lower()
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
    r2c = safe_get(result, "reconciliation_reference_to_intext", []) or []

    missing_rows = []
    for x in missing:
        if isinstance(x, dict):
            missing_rows.append(
                {
                    "no": "",
                    "citation_in_text": x.get("citation_in_text", ""),
                    "count_in_text": x.get("count_in_text", ""),
                }
            )
        else:
            missing_rows.append({"no": "", "citation_in_text": str(x), "count_in_text": ""})

    uncited_rows = []
    for x in uncited:
        if isinstance(x, dict):
            uncited_rows.append(
                {
                    "no": "",
                    "reference": x.get("reference", x.get("text", str(x))),
                    "note": x.get("note", ""),
                }
            )
        else:
            uncited_rows.append({"no": "", "reference": str(x), "note": ""})

    recon_rows = []
    for x in recon:
        if isinstance(x, dict):
            recon_rows.append(
                {
                    "no": "",
                    "in_text": x.get("in_text", ""),
                    "status": x.get("status", ""),
                    "matched_reference": x.get("matched_reference", ""),
                    "flags": x.get("flags", []) or [],
                }
            )
        else:
            recon_rows.append({"no": "", "in_text": str(x), "status": "", "matched_reference": "", "flags": []})

    r2c_rows = []
    for x in r2c:
        if isinstance(x, dict):
            r2c_rows.append(
                {
                    "no": "",
                    "reference": x.get("reference", ""),
                    "times_cited": x.get("times_cited", 0),
                    "cited_by": x.get("cited_by", []),
                }
            )

    for i, r in enumerate(missing_rows, start=1):
        r["no"] = i
    for i, r in enumerate(uncited_rows, start=1):
        r["no"] = i
    for i, r in enumerate(recon_rows, start=1):
        r["no"] = i
    for i, r in enumerate(r2c_rows, start=1):
        r["no"] = i

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

    ov = safe_get(result, "online_verification", {}) or {}
    ov_summary = safe_get(ov, "summary", {}) or {}
    ov_rows = safe_get(ov, "rows", []) or []

    verify_rows = []
    for x in ov_rows:
        if isinstance(x, dict):
            verify_rows.append(
                {
                    "no": "",
                    "status": x.get("status", ""),
                    "source": x.get("source", ""),
                    "score": x.get("score", ""),
                    "doi": x.get("doi", ""),
                    "matched_year": x.get("matched_year", ""),
                    "matched_title": x.get("matched_title", ""),
                    "matched_authors": x.get("matched_authors", x.get("matched_first_author", "")),
                    "reference": x.get("reference", ""),
                    "query_used": x.get("query_used", ""),
                    "error": x.get("error", ""),
                }
            )
        else:
            verify_rows.append(
                {
                    "no": "",
                    "status": "",
                    "source": "",
                    "score": "",
                    "doi": "",
                    "matched_year": "",
                    "matched_title": "",
                    "matched_authors": "",
                    "reference": str(x),
                    "query_used": "",
                    "error": "",
                }
            )
    for i, r in enumerate(verify_rows, start=1):
        r["no"] = i

    return {
        "summary": summary,
        "missing_rows": missing_rows,
        "uncited_rows": uncited_rows,
        "recon_rows": recon_rows,
        "r2c_rows": r2c_rows,
        "dashboard": dashboard,
        "verify_summary": ov_summary,
        "verify_rows": verify_rows,
    }


def make_csv_bytes(result: Dict[str, Any]) -> bytes:
    if pd is None:
        raise RuntimeError("pandas not installed. Add pandas to requirements.txt")
    t = extract_tables(result)
    df = pd.DataFrame(t["recon_rows"])
    return df.to_csv(index=False).encode("utf-8")


def make_word_bytes(result: Dict[str, Any]) -> bytes:
    if Document is None:
        raise RuntimeError("python-docx not installed. Add python-docx to requirements.txt")

    t = extract_tables(result)
    doc = Document()

    doc.add_heading("Citation Crosschecker Report", level=1)
    meta = doc.add_paragraph()
    meta.add_run(f"Generated: {t['dashboard']['timestamp']}\n")

    doc.add_heading("Dashboard", level=2)
    dash = t["dashboard"]
    table = doc.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    hdr = table.rows[0].cells
    hdr[0].text = "Metric"
    hdr[1].text = "Value"
    for k in [
        "in_text_citations_found",
        "reference_entries_found",
        "missing_in_references",
        "uncited_references",
        "match_rate_pct",
    ]:
        row = table.add_row().cells
        row[0].text = k
        row[1].text = str(dash.get(k, ""))

    def add_table(title: str, rows: List[Dict[str, Any]], cols: List[Tuple[str, str]]):
        doc.add_heading(title, level=2)
        if not rows:
            doc.add_paragraph("None.")
            return
        tb = doc.add_table(rows=1, cols=len(cols))
        tb.style = "Table Grid"
        h = tb.rows[0].cells
        for i, (_, label) in enumerate(cols):
            h[i].text = label
        for r in rows:
            cells = tb.add_row().cells
            for i, (key, _) in enumerate(cols):
                v = r.get(key, "")
                if isinstance(v, list):
                    v = " | ".join(str(x) for x in v[:10])
                cells[i].text = str(v)

    add_table(
        "Missing in References",
        t["missing_rows"],
        [("no", "No."), ("citation_in_text", "Citation in Text"), ("count_in_text", "Count")],
    )
    add_table(
        "Uncited References",
        t["uncited_rows"],
        [("no", "No."), ("reference", "Reference"), ("note", "Note")],
    )
    add_table(
        "In-text → Reference",
        t["recon_rows"],
        [("no", "No."), ("status", "Status"), ("in_text", "In-text"), ("matched_reference", "Matched Reference"), ("flags", "Flags")],
    )

    doc.add_heading("Online Verification", level=2)
    vs = t.get("verify_summary") or {}
    if vs.get("enabled"):
        p = doc.add_paragraph()
        p.add_run(
            f"Verified: {vs.get('verified', 0)}, Likely: {vs.get('likely', 0)}, "
            f"Needs review: {vs.get('needs_review', 0)}, Not found: {vs.get('not_found', 0)}, "
            f"Offline: {vs.get('offline', 0)}"
        )
        add_table(
            "Verification Results",
            t["verify_rows"],
            [
                ("no", "No."),
                ("status", "Status"),
                ("source", "Source"),
                ("score", "Score"),
                ("doi", "DOI"),
                ("matched_year", "Year"),
                ("matched_authors", "Matched authors"),
                ("matched_title", "Matched title"),
                ("reference", "Reference"),
            ],
        )
    else:
        doc.add_paragraph("Not run.")

    doc.add_paragraph("")
    doc.add_paragraph("Copyright © Prof Anokye M. Adam, University of Cape Coast.")
    doc.add_paragraph("Disclaimer: This checker can make mistakes. Always cross-check results before final decisions.")

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def make_pdf_bytes(result: Dict[str, Any]) -> bytes:
    if canvas is None:
        raise RuntimeError("reportlab not installed. Add reportlab to requirements.txt")

    t = extract_tables(result)
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    width, height = A4

    x = 2 * cm
    y = height - 2 * cm

    def line(text: str, dy=14):
        nonlocal y
        c.drawString(x, y, (text or "")[:1200])
        y -= dy
        if y < 2 * cm:
            c.showPage()
            y = height - 2 * cm

    c.setFont("Helvetica-Bold", 16)
    line("Citation Crosschecker Report", dy=20)

    c.setFont("Helvetica", 10)
    line(f"Generated: {t['dashboard']['timestamp']}", dy=16)
    dash = t["dashboard"]
    line(f"In-text citations found: {dash['in_text_citations_found']}")
    line(f"Reference entries found: {dash['reference_entries_found']}")
    line(f"Missing in references: {dash['missing_in_references']}")
    line(f"Uncited references: {dash['uncited_references']}")
    line(f"Match rate (%): {dash['match_rate_pct']}", dy=18)

    c.setFont("Helvetica-Bold", 12)
    line("Missing in References", dy=16)
    c.setFont("Helvetica", 10)
    if not t["missing_rows"]:
        line("None.")
    else:
        for r in t["missing_rows"][:80]:
            line(f"{r.get('no','')}. {r.get('citation_in_text','')} (count: {r.get('count_in_text','')})")

    c.setFont("Helvetica-Bold", 12)
    line("Uncited References", dy=16)
    c.setFont("Helvetica", 10)
    if not t["uncited_rows"]:
        line("None.")
    else:
        for r in t["uncited_rows"][:80]:
            line(f"{r.get('no','')}. {(r.get('reference','') or '')[:160]}")

    c.setFont("Helvetica-Bold", 12)
    line("Reconciliation (first 60)", dy=16)
    c.setFont("Helvetica", 9)
    for r in t["recon_rows"][:60]:
        flags = r.get("flags") or []
        flags_txt = f" | flags: {', '.join(flags)}" if flags else ""
        line(f"{r.get('no','')}. {r.get('status','')} | {(r.get('in_text','') or '')[:120]}{flags_txt}")
        mr = (r.get("matched_reference", "") or "")[:150]
        if mr:
            line(f"   -> {mr}", dy=12)

    c.setFont("Helvetica", 9)
    line("")
    line("Copyright © Prof Anokye M. Adam, University of Cape Coast.")
    line("Disclaimer: This checker can make mistakes. Always cross-check results before final decisions.")

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

    result = run_crosscheck(
        file_bytes=file_bytes,
        filename=filename,
        style=style_norm,
        verify_online=False,
    )

    elapsed = round(time.time() - t0, 3)
    result["style"] = style_norm
    result["elapsed_seconds"] = elapsed

    tables = extract_tables(result)
    result["_ui"] = {
        "dashboard": tables["dashboard"],
        "missing_rows": tables["missing_rows"],
        "uncited_rows": tables["uncited_rows"],
        "recon_rows": tables["recon_rows"],
    }

    return JSONResponse(result)


@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: bool = Form(True),
    verify_mode: str = Form("all"),
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
    use_semantic_scholar: bool = Form(True),
    throttle_s: float = Form(0.12),
    max_verify: int = Form(0),
):
    t0 = time.time()
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    result = run_crosscheck(
        file_bytes=file_bytes,
        filename=filename,
        style=style_norm,
        verify_online=True,
        verify_mode=verify_mode,
        use_crossref=bool(use_crossref),
        use_openalex=bool(use_openalex),
        use_semantic_scholar=bool(use_semantic_scholar),
        throttle_s=float(throttle_s or 0.0),
        max_verify=int(max_verify or 0),
    )

    elapsed = round(time.time() - t0, 3)
    result["elapsed_seconds"] = elapsed

    tables = extract_tables(result)
    result["_ui"] = {
        "dashboard": tables["dashboard"],
        "missing_rows": tables["missing_rows"],
        "uncited_rows": tables["uncited_rows"],
        "recon_rows": tables["recon_rows"],
        "verify_summary": tables.get("verify_summary", {}) or {},
        "verify_rows": tables.get("verify_rows", []) or [],
    }

    return JSONResponse(result)


@app.post("/export/csv")
async def export_csv(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    result = run_crosscheck(file_bytes=file_bytes, filename=filename, style=style_norm, verify_online=False)
    csv_bytes = make_csv_bytes(result)

    base = filename_base(filename)
    out_name = f"{base}_citation_report.csv"

    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
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

    result = run_crosscheck(file_bytes=file_bytes, filename=filename, style=style_norm, verify_online=False)
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

    result = run_crosscheck(file_bytes=file_bytes, filename=filename, style=style_norm, verify_online=False)
    pdf_bytes = make_pdf_bytes(result)

    base = filename_base(filename)
    out_name = f"{base}_citation_report.pdf"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )
