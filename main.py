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

try:
    import pandas as pd
except Exception:
    pd = None

try:
    from docx import Document
except Exception:
    Document = None

app = FastAPI(title="Citation Crosschecker", version="1.0.0")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


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

    missing_rows = []
    for x in missing:
        if isinstance(x, dict):
            missing_rows.append({"no": "", "citation_in_text": x.get("citation_in_text", ""), "count_in_text": x.get("count_in_text", "")})
        else:
            missing_rows.append({"no": "", "citation_in_text": str(x), "count_in_text": ""})

    uncited_rows = []
    for x in uncited:
        if isinstance(x, dict):
            uncited_rows.append({"no": "", "reference": x.get("reference", x.get("text", str(x))), "note": x.get("note", "")})
        else:
            uncited_rows.append({"no": "", "reference": str(x), "note": ""})

    recon_rows = []
    for x in recon:
        if isinstance(x, dict):
            recon_rows.append({
                "no": "",
                "status": x.get("status", ""),
                "in_text": x.get("in_text", ""),
                "matched_reference": x.get("matched_reference", ""),
                "flags": x.get("flags", ""),
            })
        else:
            recon_rows.append({"no": "", "status": "", "in_text": str(x), "matched_reference": "", "flags": ""})

    for i, r in enumerate(missing_rows, start=1):
        r["no"] = i
    for i, r in enumerate(uncited_rows, start=1):
        r["no"] = i
    for i, r in enumerate(recon_rows, start=1):
        r["no"] = i

    itc = int(summary.get("in_text_citations_found", 0) or 0)
    refn = int(summary.get("reference_entries_found", 0) or 0)
    miss = int(summary.get("missing_in_references", 0) or 0)
    unct = int(summary.get("uncited_references", 0) or 0)
    match_rate = round(((itc - miss) / itc) * 100.0, 1) if itc else 0.0

    dashboard = {
        "in_text_citations_found": itc,
        "reference_entries_found": refn,
        "missing_in_references": miss,
        "uncited_references": unct,
        "match_rate_pct": match_rate,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    ov = result.get("online_verification") or {"summary": {}, "rows": []}
    if isinstance(ov, list):
        ov = {"summary": {}, "rows": ov}

    vs = ov.get("summary", {}) or {}
    vr = ov.get("rows", []) or []

    verify_rows = []
    for i, x in enumerate(vr, start=1):
        if isinstance(x, dict):
            verify_rows.append({
                "no": i,
                "status": x.get("status", ""),
                "source": x.get("source", ""),
                "score": x.get("score", ""),
                "doi": x.get("doi", ""),
                "matched_year": x.get("matched_year", ""),
                "matched_first_author": x.get("matched_first_author", ""),
                "matched_title": x.get("matched_title", ""),
                "reference": x.get("reference", ""),
                "query_used": x.get("query_used", ""),
                "error": x.get("error", ""),
            })

    return {
        "dashboard": dashboard,
        "missing_rows": missing_rows,
        "uncited_rows": uncited_rows,
        "recon_rows": recon_rows,
        "verify_summary": vs,
        "verify_rows": verify_rows,
        "online_verification": {"summary": vs, "rows": vr},
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
    doc.add_paragraph(f"Generated: {t['dashboard']['timestamp']}")

    doc.add_heading("Dashboard", level=2)
    dash = t["dashboard"]
    tb = doc.add_table(rows=1, cols=2)
    tb.style = "Table Grid"
    tb.rows[0].cells[0].text = "Metric"
    tb.rows[0].cells[1].text = "Value"
    for k in ["in_text_citations_found", "reference_entries_found", "missing_in_references", "uncited_references", "match_rate_pct"]:
        row = tb.add_row().cells
        row[0].text = k
        row[1].text = str(dash.get(k, ""))

    def add_table(title: str, rows: List[Dict[str, Any]], cols: List[Tuple[str, str]]):
        doc.add_heading(title, level=2)
        if not rows:
            doc.add_paragraph("None.")
            return
        tbb = doc.add_table(rows=1, cols=len(cols))
        tbb.style = "Table Grid"
        for i, (_, label) in enumerate(cols):
            tbb.rows[0].cells[i].text = label
        for r in rows:
            rr = tbb.add_row().cells
            for i, (key, _) in enumerate(cols):
                rr[i].text = str(r.get(key, ""))

    add_table("Missing in References", t["missing_rows"], [("no", "No."), ("citation_in_text", "Citation in Text"), ("count_in_text", "Count")])
    add_table("Uncited References", t["uncited_rows"], [("no", "No."), ("reference", "Reference"), ("note", "Note")])
    add_table("Reconciliation", t["recon_rows"], [("no", "No."), ("status", "Status"), ("in_text", "In-text"), ("matched_reference", "Matched Reference"), ("flags", "Flags")])

    doc.add_heading("Online Verification", level=2)
    vs = t.get("verify_summary") or {}
    doc.add_paragraph(
        f"Verified: {vs.get('verified', 0)}, Likely: {vs.get('likely', 0)}, "
        f"Needs review: {vs.get('needs_review', 0)}, Not found: {vs.get('not_found', 0)}, Offline: {vs.get('offline', 0)}"
    )
    add_table(
        "Verification Results",
        t["verify_rows"],
        [("no", "No."), ("status", "Status"), ("source", "Source"), ("score", "Score"), ("doi", "DOI"), ("matched_year", "Year"),
         ("matched_first_author", "Author"), ("matched_title", "Matched Title"), ("reference", "Reference")],
    )

    doc.add_paragraph("")
    doc.add_paragraph("Copyright © Prof Anokye M. Adam, University of Cape Coast.")
    doc.add_paragraph("Disclaimer: This checker can make mistakes. Always cross-check results before final decisions.")

    out = io.BytesIO()
    doc.save(out)
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
async def check(file: UploadFile = File(...), style: str = Form("apa")):
    t0 = time.time()
    try:
        file_bytes = await file.read()
        filename = file.filename or "uploaded"
        style_norm = normalize_style(style)

        result = run_crosscheck(file_bytes=file_bytes, filename=filename, style=style_norm, verify_online=False)
        result["elapsed_seconds"] = round(time.time() - t0, 3)
        result["style"] = style_norm

        t = extract_tables(result)
        result["_ui"] = {
            "dashboard": t["dashboard"],
            "missing_rows": t["missing_rows"],
            "uncited_rows": t["uncited_rows"],
            "recon_rows": t["recon_rows"],
            "verify_summary": t["verify_summary"],
            "verify_rows": t["verify_rows"],
        }
        return JSONResponse(result)

    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {str(e)}"}, status_code=500)


@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_mode: str = Form("all"),
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
    use_semantic: bool = Form(True),
    throttle_s: float = Form(0.08),
    max_verify: int = Form(0),
):
    t0 = time.time()
    try:
        file_bytes = await file.read()
        filename = file.filename or "uploaded"
        style_norm = normalize_style(style)

        result = run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_norm,
            verify_online=True,
            verify_mode=verify_mode,
            throttle_s=float(throttle_s or 0.0),
            max_verify=int(max_verify or 0),
            use_crossref=bool(use_crossref),
            use_openalex=bool(use_openalex),
            use_semantic=bool(use_semantic),
        )
        result["elapsed_seconds"] = round(time.time() - t0, 3)
        result["style"] = style_norm

        t = extract_tables(result)
        payload = {
            "filename": filename,
            "style": style_norm,
            "elapsed_seconds": result["elapsed_seconds"],
            "online_verification": t["online_verification"],
            "_ui": {
                "dashboard": t["dashboard"],
                "missing_rows": t["missing_rows"],
                "uncited_rows": t["uncited_rows"],
                "recon_rows": t["recon_rows"],
                "verify_summary": t["verify_summary"],
                "verify_rows": t["verify_rows"],
            },
        }
        return JSONResponse(payload)

    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {str(e)}"}, status_code=500)


@app.post("/export/csv")
async def export_csv(file: UploadFile = File(...), style: str = Form("apa")):
    try:
        file_bytes = await file.read()
        filename = file.filename or "uploaded"
        style_norm = normalize_style(style)

        result = run_crosscheck(file_bytes=file_bytes, filename=filename, style=style_norm, verify_online=False)
        csv_bytes = make_csv_bytes(result)

        out_name = f"{filename_base(filename)}_citation_report.csv"
        return StreamingResponse(io.BytesIO(csv_bytes), media_type="text/csv",
                                headers={"Content-Disposition": f'attachment; filename="{out_name}"'})
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {str(e)}"}, status_code=500)


@app.post("/export/word")
async def export_word(file: UploadFile = File(...), style: str = Form("apa")):
    try:
        file_bytes = await file.read()
        filename = file.filename or "uploaded"
        style_norm = normalize_style(style)

        result = run_crosscheck(file_bytes=file_bytes, filename=filename, style=style_norm, verify_online=False)
        docx_bytes = make_word_bytes(result)

        out_name = f"{filename_base(filename)}_citation_report.docx"
        return StreamingResponse(io.BytesIO(docx_bytes),
                                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                headers={"Content-Disposition": f'attachment; filename="{out_name}"'})
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {str(e)}"}, status_code=500)
