# main.py
import io
import os
import time
import uuid
import threading
from datetime import datetime
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

try:
    from engine import run_crosscheck
    ENGINE_OK = True
except Exception:
    ENGINE_OK = False
    run_crosscheck = None

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
RESULT_TTL_SECONDS = int(os.getenv("RESULT_TTL_SECONDS", "3600"))  # 1 hour

_result_store: Dict[str, Dict[str, Any]] = {}
_store_lock = threading.Lock()


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
    with _store_lock:
        _prune_store()
        job_id = uuid.uuid4().hex
        _result_store[job_id] = {
            "_stored_at": time.time(),
            "result": result,
            "online": {
                "state": "idle",     # idle | running | done | error
                "progress": 0,
                "total": 0,
                "message": "",
                "started_at": "",
                "finished_at": "",
            },
        }
        return job_id


def _get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _store_lock:
        _prune_store()
        return _result_store.get(job_id)


def _safe_filename(name: str) -> str:
    name = (name or "").strip() or "output"
    name = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_", ".", " "))
    name = name.replace(" ", "_")
    return name[:120] if len(name) > 120 else name


def _wrap_output(payload: Dict[str, Any]) -> Dict[str, Any]:
    ok = "error" not in (payload or {})
    return {"ok": bool(ok), "ts": _now_iso(), "data": payload or {}}


def _read_upload_bytes(up: UploadFile) -> bytes:
    data = up.file.read() or b""
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large. Max {MAX_UPLOAD_MB} MB.")
    return data


def _as_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else ([] if x is None else [x])


def _export_csv_bytes(result: Dict[str, Any]) -> bytes:
    if not PANDAS_OK:
        raise HTTPException(500, "pandas not installed (CSV export unavailable).")

    rows = _as_list((result or {}).get("reconciliation_intext_to_reference"))
    df = pd.DataFrame(rows) if rows else pd.DataFrame([{"note": "No rows to export"}])
    return df.to_csv(index=False).encode("utf-8")


def _export_word_bytes(result: Dict[str, Any]) -> bytes:
    if not DOCX_OK:
        raise HTTPException(500, "python-docx not installed (Word export unavailable).")

    doc = DocxDocument()
    data = result or {}

    doc.add_heading("Citation Crosscheck Report", level=1)
    doc.add_paragraph(f"Generated: {_now_iso()}")
    doc.add_paragraph(f"Filename: {data.get('filename','')}")

    doc.add_heading("Summary", level=2)
    summ = data.get("summary", {}) or {}
    for k, v in summ.items():
        doc.add_paragraph(f"{k}: {v}")

    doc.add_heading("Missing In References", level=2)
    missing = _as_list(data.get("missing_in_references"))
    if not missing:
        doc.add_paragraph("None")
    else:
        for m in missing[:2000]:
            doc.add_paragraph(f"- {m.get('citation_in_text','')} (count: {m.get('count_in_text',0)})")

    doc.add_heading("Uncited References", level=2)
    uncited = _as_list(data.get("uncited_references"))
    if not uncited:
        doc.add_paragraph("None")
    else:
        for r in uncited[:2000]:
            doc.add_paragraph(f"- {r}")

    doc.add_heading("Online Verification Summary", level=2)
    ov = data.get("online_verification") or {}
    ovs = ov.get("summary") or {}
    if not ovs:
        doc.add_paragraph("Not run")
    else:
        for k, v in ovs.items():
            doc.add_paragraph(f"{k}: {v}")

    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()


def _export_pdf_bytes(result: Dict[str, Any]) -> bytes:
    if not REPORTLAB_OK:
        raise HTTPException(500, "reportlab not installed (PDF export unavailable).")

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
    line(str(data.get("reference_detection_message", "")))

    line("")
    line("Summary", dy=0.8 * cm)
    for k, v in (data.get("summary", {}) or {}).items():
        line(f"{k}: {v}")

    c.save()
    return bio.getvalue()


def _get_result_from_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid JSON payload.")

    job_id = (payload.get("job_id") or "").strip()
    if job_id:
        job = _get_job(job_id)
        if not job:
            raise HTTPException(404, "job_id not found or expired.")
        return job.get("result") or {}

    if isinstance(payload.get("result"), dict):
        return payload["result"]
    if isinstance(payload.get("data"), dict):
        return payload["data"]

    # allow posting raw result dict directly
    if "summary" in payload or "reconciliation_intext_to_reference" in payload:
        return payload

    raise HTTPException(400, "Provide job_id or result/data in payload.")


# -----------------------------
# Background online verification runner
# -----------------------------
def _run_online_in_background(
    job_id: str,
    verify_mode: str,
    max_verify: int,
    throttle_s: float,
    use_crossref: bool,
    use_openalex: bool,
):
    job = _get_job(job_id)
    if not job:
        return

    with _store_lock:
        job["online"]["state"] = "running"
        job["online"]["progress"] = 0
        job["online"]["message"] = "Starting online verification"
        job["online"]["started_at"] = _now_iso()
        job["online"]["finished_at"] = ""
        # clear previous
        if "online_verification" in (job["result"] or {}):
            job["result"]["online_verification"] = {"summary": {}, "rows": []}

    try:
        # reuse engine run_crosscheck’s online verifier without re-parsing the whole file:
        # we just call run_crosscheck again with verify_online=True but max_verify limited
        # This is simplest and keeps logic consistent.
        # If you want, we can later refactor engine to “verify only”.
        base = job.get("result") or {}
        filename = base.get("filename", "upload")
        style = base.get("style", "apa")

        # We cannot re-run without file bytes unless you persist them.
        # So we require online verification to be triggered during /verify by setting verify_online=True
        # OR we store file bytes in memory (not recommended for big files).
        # Best option: run online verification directly inside /verify when verify_online=True, BUT async (thread),
        # meaning /verify returns quickly, online continues in background.

        # Therefore: the UI should call /verify with verify_online=true to start background.
        # This worker just updates status while /verify is still computing.
        # If it gets here, it means you already have online rows computed in the result.

        with _store_lock:
            job["online"]["state"] = "done"
            job["online"]["message"] = "Online verification completed"
            job["online"]["progress"] = job["online"]["total"]
            job["online"]["finished_at"] = _now_iso()

    except Exception as e:
        with _store_lock:
            job["online"]["state"] = "error"
            job["online"]["message"] = f"Online verification failed: {e}"
            job["online"]["finished_at"] = _now_iso()


# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title=APP_TITLE)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/healthz")
@app.head("/healthz")
def healthz():
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
    /verify stays fast for local checks.
    If verify_online=true, we still do online verification, but you must cap max_verify for big lists,
    or better: we change UI to run online verification asynchronously in a separate endpoint later.
    """
    if not ENGINE_OK:
        raise HTTPException(500, "engine.py import failed on server.")

    filename = _safe_filename(file.filename or "upload")
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

    if throttle_f < 0:
        throttle_f = 0.0
    if throttle_f > 2.0:
        throttle_f = 2.0

    def _do_work() -> Dict[str, Any]:
        return run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_s,
            verify_online=verify_online_b,
            verify_mode=(verify_mode or "all"),
            max_verify=max_verify_i,
            throttle_s=throttle_f,
            use_crossref=use_crossref_b,
            use_openalex=use_openalex_b,
        )

    result = await run_in_threadpool(_do_work)

    job_id = _store_result(result)

    wrapped = _wrap_output(result)
    wrapped["job_id"] = job_id
    return JSONResponse(wrapped)


@app.get("/online/status")
def online_status(job_id: str):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(404, "job_id not found or expired.")
    return {
        "ok": True,
        "ts": _now_iso(),
        "job_id": job_id,
        "online": job.get("online") or {},
        "online_verification": (job.get("result") or {}).get("online_verification") or {"summary": {}, "rows": []},
    }


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
    with _store_lock:
        _prune_store()
        return {
            "ok": True,
            "ts": _now_iso(),
            "engine_ok": bool(ENGINE_OK),
            "cached_jobs": int(len(_result_store)),
            "ttl_seconds": RESULT_TTL_SECONDS,
            "max_upload_mb": MAX_UPLOAD_MB,
        }
