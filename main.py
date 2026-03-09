# main.py (FULL FILE) — Citation Crosschecker (Render-safe)
# - /verify runs parsing + reconciliation fast and returns job_id
# - /verify-online runs ONLY online verification on existing job
# - /online/status lets UI poll progress + partial rows (dashboard updates live)
# - /export/csv and /export/word export the stored results using job_id

import io
import os
import time
import uuid
import json
import re
import threading
import requests
from datetime import datetime
from typing import Any, Dict, Optional, List

try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✓ Loaded .env file")
except ImportError:
    print("! python-dotenv not installed")

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool


# -----------------------------
# Imports from project
# -----------------------------

try:
    from engine import run_crosscheck
    ENGINE_OK = True
    print("✓ Engine imported successfully")
except Exception as e:
    ENGINE_OK = False
    run_crosscheck = None
    print(f"✗ Engine import failed: {e}")

try:
    from verify import verify_references_batch
    VERIFY_OK = True
    print("✓ Verify module imported successfully")
except Exception as e:
    VERIFY_OK = False
    verify_references_batch = None
    print(f"! Verify module not available: {e}")

# -----------------------------
# NEW: ACII Import
# -----------------------------

try:
    from acii import compute_acii
    ACII_OK = True
    print("✓ ACII module loaded")
except Exception as e:
    ACII_OK = False
    compute_acii = None
    print(f"! ACII module not available: {e}")


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


# -----------------------------
# Config
# -----------------------------

APP_TITLE = "CitationCrosschecker"

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "40"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

RESULT_TTL_SECONDS = int(os.getenv("RESULT_TTL_SECONDS", "3600"))

BATCH_SIZE_DEFAULT = int(os.getenv("VERIFY_BATCH_SIZE", "60"))
BATCH_PAUSE_S = float(os.getenv("VERIFY_BATCH_PAUSE_S", "0.05"))


# -----------------------------
# In-memory store
# -----------------------------

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
                "state": "idle",
                "progress": 0,
                "total": 0,
                "message": "",
                "started_at": "",
                "finished_at": "",
            },
            "references_raw": result.get("references_raw", []),
            "file_name": result.get("filename", ""),
            "style": result.get("style", ""),
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


# -----------------------------
# Online verification worker
# -----------------------------

def _run_online_batches(
    job_id: str,
    verify_mode: str,
    throttle_s: float,
    use_crossref: bool,
    use_openalex: bool,
    batch_size: int,
):

    job = _get_job(job_id)

    if not job:
        return

    if not VERIFY_OK:

        with _store_lock:
            job["online"]["state"] = "error"
            job["online"]["message"] = "verify.py not available on server."
            job["online"]["finished_at"] = _now_iso()

        return

    with _store_lock:
        job["online"]["state"] = "running"
        job["online"]["progress"] = 0
        job["online"]["started_at"] = _now_iso()

    refs_raw = job.get("references_raw") or []
    style = job.get("style") or "apa"

    total = len(refs_raw)

    all_rows: List[Dict[str, Any]] = []

    for start in range(0, total, batch_size):

        chunk = refs_raw[start:start + batch_size]

        rows = verify_references_batch(
            references=chunk,
            style=style,
            throttle_s=throttle_s,
            use_crossref=use_crossref,
            use_openalex=use_openalex,
        ) or []

        all_rows.extend(rows)

        with _store_lock:

            current_result = job.get("result") or {}

            current_result["online_verification"] = {
                "summary": {"total": len(all_rows)},
                "rows": all_rows,
            }

            job["result"] = current_result
            job["online"]["progress"] = min(start + len(chunk), total)

        time.sleep(BATCH_PAUSE_S)

    # ---------------------------------------
    # NEW: Compute ACII after verification
    # ---------------------------------------

    try:

        if ACII_OK:

            engine_result = job.get("result") or {}

            acii_metrics = compute_acii(engine_result, all_rows)

            engine_result["acii"] = acii_metrics

            job["result"] = engine_result

            print("✓ ACII computed")

    except Exception as e:

        print(f"ACII computation failed: {e}")

    with _store_lock:
        job["online"]["state"] = "done"
        job["online"]["finished_at"] = _now_iso()


# -----------------------------
# FastAPI app
# -----------------------------

app = FastAPI(title=APP_TITLE)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
):

    if not ENGINE_OK:
        raise HTTPException(500, "engine.py import failed")

    filename = _safe_filename(file.filename or "upload")

    file_bytes = await file.read()

    result = await run_in_threadpool(
        run_crosscheck,
        file_bytes=file_bytes,
        filename=filename,
        style=style,
        verify_online=False,
    )

    job_id = _store_result(result)

    wrapped = _wrap_output(result)

    wrapped["job_id"] = job_id

    return JSONResponse(wrapped)


@app.post("/verify-online")
async def verify_online(
    job_id: str = Form(...),
):

    job = _get_job(job_id)

    if not job:
        raise HTTPException(404, "job_id not found")

    t = threading.Thread(
        target=_run_online_batches,
        args=(job_id, "all", 0.12, True, True, BATCH_SIZE_DEFAULT),
        daemon=True,
    )

    t.start()

    return {"ok": True, "job_id": job_id}


@app.get("/online/status")
def online_status(job_id: str, include_result: int = 0):

    job = _get_job(job_id)

    if not job:
        raise HTTPException(404, "job_id not found")

    result = job.get("result") or {}

    return {
        "ok": True,
        "ts": _now_iso(),
        "job_id": job_id,
        "online": job.get("online"),
        "online_verification": result.get("online_verification"),
        "acii": result.get("acii"),   # ACII exposed to dashboard
        "result": result if include_result else {},
    }
