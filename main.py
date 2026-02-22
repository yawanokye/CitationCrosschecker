# main.py
__version__ = "1.2.3"

import io
import time
import traceback
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from engine import run_crosscheck

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# -----------------------
# Verification limits
# -----------------------
VERIFY_DEFAULT_CAP = 60
VERIFY_HARD_CAP = 200


# -----------------------
# Utilities
# -----------------------
def build_ui_payload(result: dict) -> dict:
    summary = result.get("summary", {})
    missing = result.get("missing_in_references", [])
    uncited = result.get("uncited_references", [])
    c2r = result.get("reconciliation_intext_to_reference", [])
    r2c = result.get("reconciliation_reference_to_intext", [])
    verify = result.get("online_verification", {})

    total = summary.get("in_text_citations_found", 0)
    matched = total - summary.get("missing_in_references", 0)
    match_rate = round((matched / total) * 100, 1) if total else 0

    return {
        "dashboard": {
            "in_text_citations_found": summary.get("in_text_citations_found", 0),
            "reference_entries_found": summary.get("reference_entries_found", 0),
            "missing_in_references": summary.get("missing_in_references", 0),
            "uncited_references": summary.get("uncited_references", 0),
            "match_rate_pct": match_rate,
        },
        "missing_rows": [
            {"no": i + 1, **row} for i, row in enumerate(missing)
        ],
        "uncited_rows": [
            {"no": i + 1, "reference": r} for i, r in enumerate(uncited)
        ],
        "c2r_rows": [
            {"no": i + 1, **row} for i, row in enumerate(c2r)
        ],
        "r2c_rows": [
            {
                "no": i + 1,
                "reference": row.get("reference", ""),
                "times_cited": row.get("times_cited", 0),
                "cited_by": ", ".join(row.get("cited_by", [])[:5])
            }
            for i, row in enumerate(r2c)
        ],
        "verify_rows": [
            {"no": i + 1, **row}
            for i, row in enumerate(verify.get("rows", []))
        ],
    }


def safe_cap(max_verify: Optional[int]) -> int:
    if not max_verify or max_verify <= 0:
        return VERIFY_DEFAULT_CAP
    return min(max_verify, VERIFY_HARD_CAP)


# -----------------------
# Routes
# -----------------------
@app.get("/")
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/check")
async def check_document(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    try:
        start = time.time()
        content = await file.read()

        result = run_crosscheck(
            file_bytes=content,
            filename=file.filename,
            style=style,
            verify_online=False,
        )

        result["elapsed_seconds"] = round(time.time() - start, 2)
        result["_ui"] = build_ui_payload(result)

        return JSONResponse(result)

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            {"error": f"Check failed: {str(e)}"},
            status_code=500
        )


@app.post("/verify")
async def verify_document(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_mode: str = Form("all"),
    use_crossref: str = Form("true"),
    use_openalex: str = Form("true"),
    throttle_s: float = Form(0.12),
    max_verify: int = Form(0),
):
    try:
        start = time.time()
        content = await file.read()

        mv = safe_cap(max_verify)

        result = run_crosscheck(
            file_bytes=content,
            filename=file.filename,
            style=style,
            verify_online=True,
            verify_mode=verify_mode,
            max_verify=mv,
            throttle_s=throttle_s,
            use_crossref=(use_crossref.lower() == "true"),
            use_openalex=(use_openalex.lower() == "true"),
        )

        result["elapsed_seconds"] = round(time.time() - start, 2)
        result["_ui"] = build_ui_payload(result)

        return JSONResponse(result)

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            {"error": f"Online verification failed: {str(e)}"},
            status_code=500
        )


# -----------------------
# Export endpoints
# -----------------------
@app.post("/export/csv")
async def export_csv(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    try:
        content = await file.read()
        result = run_crosscheck(
            file_bytes=content,
            filename=file.filename,
            style=style,
            verify_online=False,
        )

        output = io.StringIO()
        output.write("Type,Content,Times Cited\n")

        for row in result.get("reconciliation_reference_to_intext", []):
            ref = row.get("reference", "").replace('"', '""')
            times = row.get("times_cited", 0)
            output.write(f'Reference,"{ref}",{times}\n')

        mem = io.BytesIO()
        mem.write(output.getvalue().encode("utf-8"))
        mem.seek(0)

        return FileResponse(
            mem,
            media_type="text/csv",
            filename="citation_report.csv"
        )

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            {"error": f"CSV export failed: {str(e)}"},
            status_code=500
        )


@app.post("/export/word")
async def export_word(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    try:
        from docx import Document

        content = await file.read()
        result = run_crosscheck(
            file_bytes=content,
            filename=file.filename,
            style=style,
            verify_online=False,
        )

        doc = Document()
        doc.add_heading("Citation Crosschecker Report", level=1)

        summary = result.get("summary", {})
        doc.add_paragraph(f"In-text citations: {summary.get('in_text_citations_found', 0)}")
        doc.add_paragraph(f"Reference entries: {summary.get('reference_entries_found', 0)}")
        doc.add_paragraph(f"Missing: {summary.get('missing_in_references', 0)}")
        doc.add_paragraph(f"Uncited: {summary.get('uncited_references', 0)}")

        mem = io.BytesIO()
        doc.save(mem)
        mem.seek(0)

        return FileResponse(
            mem,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="citation_report.docx"
        )

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            {"error": f"Word export failed: {str(e)}"},
            status_code=500
        )
