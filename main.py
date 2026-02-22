# main.py
import io
import json
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Heavy work should not block the event loop
from starlette.concurrency import run_in_threadpool

# Your core parser / crosschecker
try:
    from engine import run_crosscheck
    ENGINE_OK = True
except Exception:
    ENGINE_OK = False
    run_crosscheck = None

# Optional exports
try:
    import pandas as pd
    PANDAS_OK = True
except Exception:
    PANDAS_OK = False
    pd = None

try:
    from docx import Document as DocxDocument
    DOCX_OK = True
except Exception:
    DOCX_OK = False
    DocxDocument = None

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import cm
    REPORTLAB_OK = True
except Exception:
    REPORTLAB_OK = False
    A4 = None
    canvas = None
    cm = None


APP_TITLE = "CitationCrosschecker"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "40"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Keep results in memory for exports so /export doesn't re-run /verify
# Note: this resets on deploy/restart, which is fine.
RESULT_TTL_SECONDS = int(os.getenv("RESULT_TTL_SECONDS", "3600"))  # 1 hour
_result_store: Dict[str, Dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _prune_store() -> None:
    now = time.time()
    dead = []
    for k, v in _result_store.items():
        if now - float(v.get("_stored_at", now)) > RESULT_TTL_SECONDS:
            dead.append(k)
    for k in dead:
        _result_store.pop(k, None)


def _store_result(result: Dict[str, Any]) -> str:
    _prune_store()
    job_id = uuid.uuid4().hex
    _result_store[job_id] = {
        "_stored_at": time.time(),
        "result": result,
    }
    return job_id


def _get_stored(job_id: str) -> Optional[Dict[str, Any]]:
    _prune_store()
    item = _result_store.get(job_id)
    return item.get("result") if item else None


def _safe_filename(name: str) -> str:
    name = (name or "").strip() or "output"
    name = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_", ".", " "))
    name = name.replace(" ", "_")
    return name[:120] if len(name) > 120 else name


def _wrap_output(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Always wrap output consistently so frontend never breaks.
    """
    ok = "error" not in (payload or {})
    return {
        "ok": bool(ok),
        "ts": _now_iso(),
        "data": payload or {},
    }


def _read_upload_bytes(up: UploadFile) -> bytes:
    """
    Enforce a hard upload size limit to avoid memory blowups on Standard 2GB.
    """
    data = up.file.read()
    if data is None:
        return b""
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max allowed is {MAX_UPLOAD_MB} MB.",
        )
    return data


def _as_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else ([] if x is None else [x])


def _flatten_rows_for_csv(result: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Prepare multiple CSV sheets (as named row lists).
    """
    data = result or {}
    sheets: Dict[str, List[Dict[str, Any]]] = {}

    sheets["summary"] = [data.get("summary", {})]

    sheets["missing_in_references"] = _as_list(data.get("missing_in_references"))
    sheets["uncited_references"] = [{"reference": r} for r in _as_list(data.get("uncited_references"))]

    sheets["intext_to_reference"] = _as_list(data.get("reconciliation_intext_to_reference"))
    sheets["reference_to_intext"] = _as_list(data.get("reconciliation_reference_to_intext"))

    ov = (data.get("online_verification") or {})
    sheets["online_verification_summary"] = [ov.get("summary", {})]
    sheets["online_verification_rows"] = _as_list(ov.get("rows"))

    # optional debug
    if data.get("debug"):
        sheets["debug"] = [data.get("debug")]

    return sheets


def _export_csv_bytes(result: Dict[str, Any]) -> bytes:
    """
    Single CSV file: we export the most useful table (intext_to_reference).
    If you want multi-sheet, use Excel export instead.
    """
    if not PANDAS_OK:
        raise HTTPException(status_code=500, detail="pandas not installed (CSV export unavailable).")

    rows = _as_list((result or {}).get("reconciliation_intext_to_reference"))
    df = pd.DataFrame(rows) if rows else pd.DataFrame([{"note": "No rows"}])
    out = df.to_csv(index=False).encode("utf-8")
    return out


def _export_word_bytes(result: Dict[str, Any]) -> bytes:
    if not DOCX_OK:
        raise HTTPException(status_code=500, detail="python-docx not installed (Word export unavailable).")

    doc = DocxDocument()

    data = result or {}
    doc.add_heading("Citation Crosscheck Report", level=1)
    doc.add_paragraph(f"Generated: {_now_iso()}")

    # Summary
    doc.add_heading("Summary", level=2)
    summ = data.get("summary", {}) or {}
    for k, v in summ.items():
        doc.add_paragraph(f"{k}: {v}")

    # Detection notes
    doc.add_heading("Detection Notes", level=2)
    doc.add_paragraph(_safe_filename(str(data.get("filename", ""))) or "")
    doc.add_paragraph(str(data.get("reference_detection_message", "")) or "")

    # Missing
    doc.add_heading("Missing In References", level=2)
    missing = _as_list(data.get("missing_in_references"))
    if not missing:
        doc.add_paragraph("None")
    else:
        for m in missing[:2000]:
            doc.add_paragraph(f"- {m.get('citation_in_text','')}  (count: {m.get('count_in_text',0)})")

    # Uncited
    doc.add_heading("Uncited References", level=2)
    uncited = _as_list(data.get("uncited_references"))
    if not uncited:
        doc.add_paragraph("None")
    else:
        for r in uncited[:2000]:
            doc.add_paragraph(f"- {r}")

    # Reconciliation (trim)
    doc.add_heading("In-text → Reference (Top Rows)", level=2)
    c2r = _as_list(data.get("reconciliation_intext_to_reference"))
    if not c2r:
        doc.add_paragraph("No rows")
    else:
        for row in c2r[:500]:
            doc.add_paragraph(f"In-text: {row.get('in_text','')}")
            doc.add_paragraph(f"Status: {row.get('status','')}")
            mr = row.get("matched_reference", "")
            if mr:
                doc.add_paragraph(f"Matched: {mr}")
            flags = row.get("flags", "")
            if flags:
                doc.add_paragraph(f"Flags: {flags}")
            doc.add_paragraph("")

    # Online verification summary
    ov = data.get("online_verification") or {}
    doc.add_heading("Online Verification", level=2)
    ovs = ov.get("summary") or {}
    if ovs:
        for k, v in ovs.items():
            doc.add_paragraph(f"{k}: {v}")
    rows = _as_list(ov.get("rows"))
    if rows:
        doc.add_paragraph("")
        doc.add_paragraph("Top verified/likely rows:")
        shown = 0
        for r in rows:
            st = (r.get("status") or "").lower()
            if st in {"verified", "likely"}:
                doc.add_paragraph(f"- {r.get('reference','')}")
                doc.add_paragraph(f"  status={r.get('status','')}, source={r.get('source','')}, score={r.get('score',0)}")
                if r.get("doi"):
                    doc.add_paragraph(f"  doi={r.get('doi')}")
                if r.get("matched_title"):
                    doc.add_paragraph(f"  matched_title={r.get('matched_title')}")
                doc.add_paragraph("")
                shown += 1
                if shown >= 200:
                    break

    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()


def _export_pdf_bytes(result: Dict[str, Any]) -> bytes:
    if not REPORTLAB_OK:
        raise HTTPException(status_code=500, detail="reportlab not installed (PDF export unavailable).")

    data = result or {}
    bio = io.BytesIO()
    c = canvas.Canvas(bio, pagesize=A4)
    width, height = A4

    x = 2 * cm
    y = height - 2 * cm

    def line(txt: str, dy: float = 0.6 * cm):
        nonlocal y
        txt = (txt or "").replace("\t", " ").strip()
        if len(txt) > 140:
            txt = txt[:140] + "..."
        c.drawString(x, y, txt)
        y -= dy
        if y < 2 * cm:
            c.showPage()
            y = height - 2 * cm

    line("Citation Crosscheck Report", dy=0.9 * cm)
    line(f"Generated: {_now_iso()}")
    line(f"Filename: {data.get('filename','')}")
    line(f"Detection: {data.get('reference_detection_message','')}")
    line("")

    summ = data.get("summary", {}) or {}
    line("Summary", dy=0.8 * cm)
    for k, v in summ.items():
        line(f"{k}: {v}")

    line("")
    line("Missing In References (top 50)", dy=0.8 * cm)
    missing = _as_list(data.get("missing_in_references"))[:50]
    if not missing:
        line("None")
    else:
        for m in missing:
            line(f"- {m.get('citation_in_text','')} (count {m.get('count_in_text',0)})")

    line("")
    line("Uncited References (top 30)", dy=0.8 * cm)
    uncited = _as_list(data.get("uncited_references"))[:30]
    if not uncited:
        line("None")
    else:
        for r in uncited:
            line(f"- {r}")

    c.save()
    return bio.getvalue()


# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title=APP_TITLE)

# Static + templates
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/healthz")
@app.head("/healthz")
def healthz():
    # Must be ultra-fast for Render health checks
    return {"ok": True, "ts": _now_iso()}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: str = Form("false"),
    verify_mode: str = Form("all"),
    max_verify: str = Form("0"),
    throttle_s: str = Form("0.12"),
    use_crossref: str = Form("true"),
    use_openalex: str = Form("true"),
):
    """
    Heavy endpoint. We run the core work in a threadpool so the event loop stays responsive.
    Gunicorn timeout must still be increased in start.sh.
    """
    if not ENGINE_OK:
        raise HTTPException(status_code=500, detail="engine.py import failed on server.")

    vb = _safe_filename(file.filename or "upload")
    file_bytes = _read_upload_bytes(file)

    style_s = (style or "apa").strip().lower()
    verify_online_b = str(verify_online).strip().lower() in {"1", "true", "yes", "y", "on"}
    use_crossref_b = str(use_crossref).strip().lower() in {"1", "true", "yes", "y", "on"}
    use_openalex_b = str(use_openalex).strip().lower() in {"1", "true", "yes", "y", "on"}

    try:
        max_verify_i = int(float(max_verify or 0))
    except Exception:
        max_verify_i = 0

    try:
        throttle_f = float(throttle_s or 0.12)
    except Exception:
        throttle_f = 0.12

    # Keep throttle reasonable
    if throttle_f < 0:
        throttle_f = 0.0
    if throttle_f > 2.0:
        throttle_f = 2.0

    def _do_work() -> Dict[str, Any]:
        # engine.run_crosscheck is pure python CPU/IO heavy
        return run_crosscheck(
            file_bytes=file_bytes,
            filename=vb,
            style=style_s,
            verify_online=verify_online_b,
            verify_mode=(verify_mode or "all"),
            max_verify=max_verify_i,
            throttle_s=throttle_f,
            use_crossref=use_crossref_b,
            use_openalex=use_openalex_b,
        )

    result = await run_in_threadpool(_do_work)

    # Store for exports so export endpoints don't re-run work
    job_id = _store_result(result)

    wrapped = _wrap_output(result)
    wrapped["job_id"] = job_id
    return JSONResponse(wrapped)


@app.post("/check")
async def check_payload(payload: Dict[str, Any]):
    """
    If your frontend uses /check for text-only checks, keep it light.
    This endpoint just validates and stores the payload as a "job" so exports can work.
    """
    try:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}
        job_id = _store_result(data)
        out = _wrap_output(data)
        out["job_id"] = job_id
        return JSONResponse(out)
    except Exception as e:
        return JSONResponse(_wrap_output({"error": str(e)}), status_code=400)


def _get_result_from_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Accept either:
    - {"job_id": "..."}  (server-stored result)
    - {"result": {...}}  (client-supplied)
    - {"data": {...}}    (client-supplied wrapper)
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    job_id = (payload.get("job_id") or "").strip()
    if job_id:
        stored = _get_stored(job_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="job_id not found or expired.")
        return stored

    if isinstance(payload.get("result"), dict):
        return payload["result"]

    if isinstance(payload.get("data"), dict):
        return payload["data"]

    # if user posted the raw result dict directly
    if "summary" in payload or "reconciliation_intext_to_reference" in payload:
        return payload

    raise HTTPException(status_code=400, detail="Provide job_id or result/data in payload.")


@app.post("/export/csv")
async def export_csv(payload: Dict[str, Any]):
    result = _get_result_from_request(payload)

    def _do() -> bytes:
        return _export_csv_bytes(result)

    data = await run_in_threadpool(_do)
    fn = _safe_filename(payload.get("filename") or result.get("filename") or "crosscheck") + ".csv"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


@app.post("/export/word")
async def export_word(payload: Dict[str, Any]):
    result = _get_result_from_request(payload)

    def _do() -> bytes:
        return _export_word_bytes(result)

    data = await run_in_threadpool(_do)
    fn = _safe_filename(payload.get("filename") or result.get("filename") or "crosscheck") + ".docx"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


@app.post("/export/pdf")
async def export_pdf(payload: Dict[str, Any]):
    result = _get_result_from_request(payload)

    def _do() -> bytes:
        return _export_pdf_bytes(result)

    data = await run_in_threadpool(_do)
    fn = _safe_filename(payload.get("filename") or result.get("filename") or "crosscheck") + ".pdf"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


@app.get("/status")
def status():
    """
    Lightweight endpoint to see server is alive and how many jobs are cached.
    """
    _prune_store()
    return {
        "ok": True,
        "ts": _now_iso(),
        "engine_ok": bool(ENGINE_OK),
        "cached_jobs": int(len(_result_store)),
        "ttl_seconds": RESULT_TTL_SECONDS,
        "max_upload_mb": MAX_UPLOAD_MB,
    }
