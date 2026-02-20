# main.py
import io
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple, Optional

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


app = FastAPI(title="Citation Crosschecker", version="1.2.1")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def normalize_style(s: Optional[str]) -> str:
    s = (s or "").strip().lower()
    if s in ("apa", "apa7", "apa-7", "harvard", "apa/harvard", "author-year"):
        return "apa"
    if s == "ieee":
        return "ieee"
    if s in ("vancouver", "van", "numeric"):
        return "vancouver"
    return "apa"


def extract_tables(result: Dict[str, Any]) -> Dict[str, Any]:
    summary = (result.get("summary") or {}) if isinstance(result, dict) else {}

    missing = result.get("missing_in_references") or []
    uncited = result.get("uncited_references") or []
    c2r = result.get("reconciliation_intext_to_reference") or []
    r2c = result.get("reconciliation_reference_to_intext") or []

    def as_rows_missing(items):
        rows = []
        for x in items:
            if isinstance(x, dict):
                rows.append({
                    "no": "",
                    "citation_in_text": x.get("citation_in_text", ""),
                    "count_in_text": x.get("count_in_text", ""),
                })
            else:
                rows.append({"no": "", "citation_in_text": str(x), "count_in_text": ""})
        for i, r in enumerate(rows, start=1):
            r["no"] = i
        return rows

    def as_rows_uncited(items):
        rows = []
        for x in items:
            if isinstance(x, dict):
                rows.append({"no": "", "reference": x.get("reference", x.get("text", str(x)))})
            else:
                rows.append({"no": "", "reference": str(x)})
        for i, r in enumerate(rows, start=1):
            r["no"] = i
        return rows

    def as_rows_c2r(items):
        rows = []
        for x in items:
            if isinstance(x, dict):
                rows.append({
                    "no": "",
                    "status": x.get("status", ""),
                    "in_text": x.get("in_text", ""),
                    "matched_reference": x.get("matched_reference", ""),
                    "flags": x.get("flags", ""),
                })
            else:
                rows.append({"no": "", "status": "", "in_text": str(x), "matched_reference": "", "flags": ""})
        for i, r in enumerate(rows, start=1):
            r["no"] = i
        return rows

    def as_rows_r2c(items):
        rows = []
        for x in items:
            if isinstance(x, dict):
                cited_by = x.get("cited_by", [])
                if not isinstance(cited_by, list):
                    cited_by = []
                rows.append({
                    "no": "",
                    "times_cited": x.get("times_cited", 0),
                    "reference": x.get("reference", ""),
                    "cited_by": " | ".join(cited_by[:8]) + (" ..." if len(cited_by) > 8 else ""),
                })
            else:
                rows.append({"no": "", "times_cited": "", "reference": str(x), "cited_by": ""})
        for i, r in enumerate(rows, start=1):
            r["no"] = i
        return rows

    missing_rows = as_rows_missing(missing)
    uncited_rows = as_rows_uncited(uncited)
    c2r_rows = as_rows_c2r(c2r)
    r2c_rows = as_rows_r2c(r2c)

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

    ov = result.get("online_verification") or {}
    ov_summary = ov.get("summary") or {}
    ov_rows = ov.get("rows") or []

    verify_rows = []
    for x in ov_rows:
        if isinstance(x, dict):
            verify_rows.append({
                "no": "",
                "status": x.get("status", ""),
                "source": x.get("source", ""),
                "score": x.get("score", ""),
                "doi": x.get("doi", ""),
                "matched_year": x.get("matched_year", ""),
                "matched_authors": x.get("matched_authors", ""),
                "matched_title": x.get("matched_title", ""),
                "reference": x.get("reference", ""),
                "query_used": x.get("query_used", ""),
            })
        else:
            verify_rows.append({"no": "", "status": "", "source": "", "score": "", "doi": "", "matched_year": "",
                                "matched_authors": "", "matched_title": "", "reference": str(x), "query_used": ""})
    for i, r in enumerate(verify_rows, start=1):
        r["no"] = i

    return {
        "dashboard": dashboard,
        "missing_rows": missing_rows,
        "uncited_rows": uncited_rows,
        "c2r_rows": c2r_rows,
        "r2c_rows": r2c_rows,
        "verify_summary": ov_summary,
        "verify_rows": verify_rows,
    }


def filename_base(upload_name: str) -> str:
    name = (upload_name or "document").rsplit(".", 1)[0]
    safe = "".join(ch for ch in name if ch.isalnum() or ch in (" ", "_", "-")).strip()
    return safe or "document"


def make_csv_bytes(result: Dict[str, Any]) -> bytes:
    if pd is None:
        raise RuntimeError("pandas not installed. Add pandas to requirements.txt")
    t = extract_tables(result)
    df = pd.DataFrame(t["c2r_rows"])
    return df.to_csv(index=False).encode("utf-8")


def make_word_bytes(result: Dict[str, Any]) -> bytes:
    if Document is None:
        raise RuntimeError("python-docx not installed. Add python-docx to requirements.txt")

    t = extract_tables(result)
    doc = Document()

    doc.add_heading("Citation Crosschecker Report", level=1)
    doc.add_paragraph(f"Generated: {t['dashboard']['timestamp']}")

    doc.add_heading("Dashboard", level=2)
    dash = t["dashboard"]
    table = doc.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    hdr = table.rows[0].cells
    hdr[0].text = "Metric"
    hdr[1].text = "Value"
    for k in ["in_text_citations_found", "reference_entries_found", "missing_in_references", "uncited_references", "match_rate_pct"]:
        row = table.add_row().cells
        row[0].text = k
        row[1].text = str(dash.get(k, ""))

    def add_table(title: str, rows: List[Dict[str, Any]], cols: List[tuple]):
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
                cells[i].text = str(r.get(key, ""))

    add_table("Missing in References", t["missing_rows"], [("no","No."),("citation_in_text","Citation in Text"),("count_in_text","Count")])
    add_table("Uncited References", t["uncited_rows"], [("no","No."),("reference","Reference")])
    add_table("In-text → Reference", t["c2r_rows"], [("no","No."),("status","Status"),("in_text","In-text citation"),("matched_reference","Matched reference"),("flags","Flags")])
    add_table("Reference → In-text", t["r2c_rows"], [("no","No."),("times_cited","Times cited"),("reference","Reference"),("cited_by","Cited by (samples)")])

    doc.add_heading("Online Verification", level=2)
    vs = t.get("verify_summary") or {}
    if vs.get("total", 0) > 0:
        doc.add_paragraph(
            f"Verified: {vs.get('verified', 0)}, Likely: {vs.get('likely', 0)}, "
            f"Needs review: {vs.get('needs_review', 0)}, Not found: {vs.get('not_found', 0)}, "
            f"Offline: {vs.get('offline', 0)}"
        )
        add_table(
            "Verification Results",
            t["verify_rows"],
            [("no","No."),("status","Status"),("source","Source"),("score","Score"),("doi","DOI"),
             ("matched_year","Year"),("matched_authors","Authors"),("matched_title","Matched title"),
             ("reference","Reference"),("query_used","Query used")],
        )
    else:
        doc.add_paragraph("Not run or no results returned.")

    doc.add_paragraph("")
    doc.add_paragraph("© Prof Anokye M. Adam, University of Cape Coast.")
    doc.add_paragraph("Disclaimer: This checker can make mistakes. Always cross-check critical citations manually.")

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/check")
async def check(file: UploadFile = File(...), style: str = Form("apa")):
    t0 = time.time()
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    try:
        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_norm,
            verify_online=False,
        )
    except Exception as e:
        return JSONResponse({"error": f"Check failed: {type(e).__name__}: {e}"}, status_code=400)

    result["elapsed_seconds"] = round(time.time() - t0, 3)
    result["style"] = style_norm
    result["_ui"] = extract_tables(result)
    return JSONResponse(result)


@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_mode: str = Form("all"),
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
    throttle_s: float = Form(0.12),
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
            verify_online=True,
            verify_mode=(verify_mode or "all"),
            use_crossref=bool(use_crossref),
            use_openalex=bool(use_openalex),
            throttle_s=float(throttle_s or 0.0),
            max_verify=int(max_verify or 0),
        )
    except Exception as e:
        return JSONResponse({"error": f"Online verification failed: {type(e).__name__}: {e}"}, status_code=400)

    result["elapsed_seconds"] = round(time.time() - t0, 3)
    result["style"] = style_norm
    result["_ui"] = extract_tables(result)
    return JSONResponse(result)


@app.post("/export/csv")
async def export_csv(file: UploadFile = File(...), style: str = Form("apa")):
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
async def export_word(file: UploadFile = File(...), style: str = Form("apa")):
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
