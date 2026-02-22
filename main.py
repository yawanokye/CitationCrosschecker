# main.py (FULL) - add background online verification batching
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
    from verify import verify_references_batch
    VERIFY_OK = True
except Exception:
    VERIFY_OK = False
    verify_references_batch = None

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


APP_TITLE = "CitationCrosschecker"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "40"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
RESULT_TTL_SECONDS = int(os.getenv("RESULT_TTL_SECONDS", "3600"))

BATCH_SIZE_DEFAULT = int(os.getenv("VERIFY_BATCH_SIZE", "60"))

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
                "state": "idle",    # idle|running|done|error
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


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    allowed = {"verified", "likely", "needs_review", "not_found", "offline"}
    return st if st in allowed else "needs_review"


def _export_csv_bytes(result: Dict[str, Any]) -> bytes:
    if not PANDAS_OK:
        raise HTTPException(500, "pandas not installed (CSV export unavailable).")
    rows = result.get("reconciliation_intext_to_reference") or []
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

    doc.add_heading("Online Verification Summary", level=2)
    ov = data.get("online_verification") or {}
    ovs = ov.get("summary") or {}
    if not ovs:
        doc.add_paragraph("Not run")
    else:
        for k, v in ovs.items():
            doc.add_paragraph(f"{k}: {v}")

    doc.add_heading("Online Verification Rows (sample)", level=2)
    rows = (ov.get("rows") or [])[:200]
    if not rows:
        doc.add_paragraph("No rows")
    else:
        for r in rows:
            doc.add_paragraph(f"- {r.get('status','')} | {r.get('source','')} | {r.get('doi','')} | {r.get('matched_title','')}")

    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()


def _get_result_from_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid JSON payload.")
    job_id = (payload.get("job_id") or "").strip()
    if not job_id:
        raise HTTPException(400, "Provide job_id.")
    job = _get_job(job_id)
    if not job:
        raise HTTPException(404, "job_id not found or expired.")
    return job.get("result") or {}


def _online_select_refs(result: Dict[str, Any], verify_mode: str) -> List[str]:
    refs = result.get("reconciliation_reference_to_intext") or []
    # refs in r2c are dicts, raw reference is in key 'reference'
    all_refs = [r.get("reference", "") for r in refs if (r.get("reference") or "").strip()]

    if (verify_mode or "all").strip().lower() == "uncited_only":
        uncited = set(result.get("uncited_references") or [])
        sel = [r for r in all_refs if r in uncited]
        return sel if sel else all_refs

    return all_refs


def _run_online_batches(job_id: str, verify_mode: str, throttle_s: float, use_crossref: bool, use_openalex: bool, batch_size: int):
    job = _get_job(job_id)
    if not job:
        return

    if not VERIFY_OK:
        with _store_lock:
            job["online"] = {
                "state": "error",
                "progress": 0,
                "total": 0,
                "message": "verify.py not available on server",
                "started_at": _now_iso(),
                "finished_at": _now_iso(),
            }
        return

    with _store_lock:
        job["online"]["state"] = "running"
        job["online"]["message"] = "Preparing references"
        job["online"]["started_at"] = _now_iso()
        job["online"]["finished_at"] = ""

    result = job.get("result") or {}
    refs = _online_select_refs(result, verify_mode=verify_mode)

    total = len(refs)
    with _store_lock:
        job["online"]["total"] = total
        job["online"]["progress"] = 0
        job["online"]["message"] = f"Running {total} checks in batches of {batch_size}"

        # reset online_verification in stored result
        result["online_verification"] = {"summary": {}, "rows": []}
        result["verify_mode_used"] = verify_mode or "all"

    all_rows: List[Dict[str, Any]] = []
    counts = {"verified": 0, "likely": 0, "needs_review": 0, "not_found": 0, "offline": 0}

    try:
      for start in range(0, total, batch_size):
        chunk = refs[start:start + batch_size]

        rows = verify_references_batch(
            references=chunk,
            max_to_check=len(chunk),
            throttle_s=float(throttle_s or 0.0),
            use_crossref=bool(use_crossref),
            use_openalex=bool(use_openalex),
        ) or []

        for r in rows:
            r["status"] = _normalize_verify_status(r.get("status"))
            st = r["status"]
            if st not in counts:
                st = "needs_review"
                r["status"] = st
            counts[st] += 1
        all_rows.extend(rows)

        with _store_lock:
            job["online"]["progress"] = min(start + len(chunk), total)
            job["online"]["message"] = f"Processed {job['online']['progress']} / {total}"

        # small pause between batches to reduce burst load
        time.sleep(0.05)

      with _store_lock:
        result["online_verification"] = {
            "summary": {**counts, "total": int(sum(counts.values()))},
            "rows": all_rows,
        }
        job["online"]["state"] = "done"
        job["online"]["message"] = "Online verification completed"
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: str = Form("false"),  # if true: we start background job, not inline
    verify_mode: str = Form("all"),
    max_verify: str = Form("0"),         # ignored for background, kept for UI compatibility
    throttle_s: str = Form("0.12"),
    use_crossref: str = Form("true"),
    use_openalex: str = Form("true"),
):
    if not ENGINE_OK:
        raise HTTPException(500, "engine.py import failed on server.")

    filename = _safe_filename(file.filename or "upload")
    file_bytes = _read_upload_bytes(file)

    style_s = (style or "apa").strip().lower()
    verify_online_b = str(verify_online).strip().lower() in {"1", "true", "yes", "y", "on"}
    verify_mode_s = (verify_mode or "all").strip().lower()

    use_crossref_b = str(use_crossref).strip().lower() in {"1", "true", "yes", "y", "on"}
    use_openalex_b = str(use_openalex).strip().lower() in {"1", "true", "yes", "y", "on"}

    try:
        throttle_f = float(throttle_s or 0.12)
    except Exception:
        throttle_f = 0.12

    def _do_crosscheck() -> Dict[str, Any]:
        # IMPORTANT: verify_online is forced OFF here so /verify returns fast and never 502
        return run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_s,
            verify_online=False,
            verify_mode=verify_mode_s,
            max_verify=0,
            throttle_s=throttle_f,
            use_crossref=use_crossref_b,
            use_openalex=use_openalex_b,
        )

    result = await run_in_threadpool(_do_crosscheck)
    job_id = _store_result(result)

    # Start online verification in background if requested
    if verify_online_b:
        t = threading.Thread(
            target=_run_online_batches,
            args=(job_id, verify_mode_s, throttle_f, use_crossref_b, use_openalex_b, BATCH_SIZE_DEFAULT),
            daemon=True,
        )
        t.start()

    wrapped = _wrap_output(result)
    wrapped["job_id"] = job_id
    wrapped["online_started"] = bool(verify_online_b)
    wrapped["batch_size"] = BATCH_SIZE_DEFAULT
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
    fn = _safe_filename(result.get("filename") or "crosscheck") + ".csv"
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
    fn = _safe_filename(result.get("filename") or "crosscheck") + ".docx"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )
