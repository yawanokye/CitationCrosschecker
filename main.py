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


app = FastAPI(title="Citation Crosschecker", version="1.1.0")

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

    missing_rows = []
    for x in missing:
        if isinstance(x, dict):
            missing_rows.append(
                {"no": "", "citation_in_text": x.get("citation_in_text", ""), "count_in_text": x.get("count_in_text", "")}
            )
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
            recon_rows.append(
                {
                    "no": "",
                    "in_text": x.get("in_text", ""),
                    "status": x.get("status", ""),
                    "matched_reference": x.get("matched_reference", ""),
                    "flags": ", ".join(x.get("flags", []) or []),
                }
            )
        else:
            recon_rows.append({"no": "", "in_text": str(x), "status": "", "matched_reference": "", "flags": ""})

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
                    "matched_first_author": x.get("matched_first_author", ""),
                    "matched_title": x.get("matched_title", ""),
                    "reference": x.get("reference", ""),
                    "query_used": x.get("query_used", ""),
                    "error": x.get("error", ""),
                }
            )
        else:
            verify_rows.append({"no": "", "status": "", "source": "", "score": "", "doi": "", "matched_year": "",
                                "matched_first_author": "", "matched_title": "", "reference": str(x),
                                "query_used": "", "error": ""})
    for i, r in enumerate(verify_rows, start=1):
        r["no"] = i

    return {
        "summary": summary,
        "missing_rows": missing_rows,
        "uncited_rows": uncited_rows,
        "recon_rows": recon_rows,
        "dashboard": dashboard,
        "verify_summary": ov_summary,
        "verify_rows": verify_rows,
    }


def make_csv_bytes(result: Dict[str, Any]) -> bytes:
    if pd is None:
        raise RuntimeError("pandas not installed. Add pandas to requirements.txt")

    t = extract_tables(result)

    # export multiple tables into one CSV with section column
    rows = []
    for r in t["recon_rows"]:
        rows.append({"section": "reconciliation", **r})
    for r in t["missing_rows"]:
        rows.append({"section": "missing_in_references", **r})
    for r in t["uncited_rows"]:
        rows.append({"section": "uncited_references", **r})
    for r in t["verify_rows"]:
        rows.append({"section": "online_verification", **r})

    df = pd.DataFrame(rows)
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
                cells[i].text = str(r.get(key, ""))

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
        "Reconciliation",
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
                ("matched_first_author", "Author"),
                ("matched_title", "Matched Title"),
                ("query_used", "Query Used"),
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
        "verify_summary": tables["verify_summary"],
        "verify_rows": tables["verify_rows"],
    }

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
    """
    IMPORTANT: returns FULL payload (same as /check) so the UI can render everything.
    """
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
        throttle_s=float(throttle_s or 0.0),
        max_verify=int(max_verify or 0),
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
        "verify_summary": tables["verify_summary"],
        "verify_rows": tables["verify_rows"],
    }

    return JSONResponse(result)


@app.post("/export/csv")
async def export_csv(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    include_online: bool = Form(True),
    verify_mode: str = Form("all"),
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
    throttle_s: float = Form(0.12),
    max_verify: int = Form(0),
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    # If include_online, run verify in one go (so export includes it)
    result = run_crosscheck(
        file_bytes=file_bytes,
        filename=filename,
        style=style_norm,
        verify_online=bool(include_online),
        verify_mode=verify_mode,
        use_crossref=bool(use_crossref),
        use_openalex=bool(use_openalex),
        throttle_s=float(throttle_s or 0.0),
        max_verify=int(max_verify or 0),
    )

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
    include_online: bool = Form(True),
    verify_mode: str = Form("all"),
    use_crossref: bool = Form(True),
    use_openalex: bool = Form(True),
    throttle_s: float = Form(0.12),
    max_verify: int = Form(0),
):
    file_bytes = await file.read()
    filename = file.filename or "uploaded"
    style_norm = normalize_style(style)

    result = run_crosscheck(
        file_bytes=file_bytes,
        filename=filename,
        style=style_norm,
        verify_online=bool(include_online),
        verify_mode=verify_mode,
        use_crossref=bool(use_crossref),
        use_openalex=bool(use_openalex),
        throttle_s=float(throttle_s or 0.0),
        max_verify=int(max_verify or 0),
    )

    docx_bytes = make_word_bytes(result)
    base = filename_base(filename)
    out_name = f"{base}_citation_report.docx"

    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )
