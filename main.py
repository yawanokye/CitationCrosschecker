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

# Load environment variables from .env file (for local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✓ Loaded .env file")
except ImportError:
    print("! python-dotenv not installed, using system environment variables only")

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool


# -----------------------------
# Imports from your project
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

# How many references to verify per background batch
BATCH_SIZE_DEFAULT = int(os.getenv("VERIFY_BATCH_SIZE", "60"))

# Optional: throttle between batches to reduce burst pressure
BATCH_PAUSE_S = float(os.getenv("VERIFY_BATCH_PAUSE_S", "0.05"))


# -----------------------------
# In-memory store (simple + Render-friendly)
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
                "state": "idle",       # idle|running|done|error
                "progress": 0,
                "total": 0,
                "message": "",
                "started_at": "",
                "finished_at": "",
            },
            "file_bytes": b"",
            "file_name": "",
            "style": "",
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


_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


# -----------------------------
# Online verification worker (background)
# -----------------------------
def _run_online_batches(
    job_id: str,
    verify_mode: str,
    throttle_s: float,
    use_crossref: bool,
    use_openalex: bool,
    batch_size: int,
) -> None:
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
        job["online"]["message"] = "Starting..."
        job["online"]["started_at"] = _now_iso()

    try:
        result = job.get("result") or {}
        refs_raw = result.get("references_raw") or []
        
        # If no references found, try to get from job
        if not refs_raw:
            refs_raw = job.get("references_raw", [])
        
        total = len(refs_raw)

        with _store_lock:
            job["online"]["total"] = total
            job["online"]["message"] = f"Queued {total} references"

        all_rows: List[Dict[str, Any]] = []

        for start in range(0, total, max(1, int(batch_size or 60))):
            job = _get_job(job_id)
            if not job:
                return

            chunk = refs_raw[start:start + max(1, int(batch_size or 60))]
            if not chunk:
                continue

            rows = verify_references_batch(
                references=chunk,
                throttle_s=throttle_s,
                use_crossref=use_crossref,
                use_openalex=use_openalex,
            ) or []

            # normalize statuses
            for r in rows:
                r["status"] = _normalize_verify_status(r.get("status"))

            all_rows.extend(rows)

            # counts
            counts: Dict[str, int] = {"verified": 0, "likely": 0, "needs_review": 0, "not_found": 0, "offline": 0}
            for r in all_rows:
                st = _normalize_verify_status(r.get("status"))
                counts[st] = counts.get(st, 0) + 1

            with _store_lock:
                # Update result with verification data
                current_result = job.get("result") or {}
                current_result["online_verification"] = {
                    "summary": {**counts, "total": int(sum(counts.values()))},
                    "rows": all_rows,
                }
                job["result"] = current_result
                job["online"]["progress"] = min(start + len(chunk), total)
                job["online"]["message"] = f"Processed {job['online']['progress']} / {total}"

            time.sleep(BATCH_PAUSE_S)

        with _store_lock:
            job["online"]["state"] = "done"
            job["online"]["message"] = "Online verification completed"
            job["online"]["finished_at"] = _now_iso()

    except Exception as e:
        with _store_lock:
            job["online"]["state"] = "error"
            job["online"]["message"] = f"Online verification failed: {e}"
            job["online"]["finished_at"] = _now_iso()


# -----------------------------
# Export helpers
# -----------------------------
def _get_result_from_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload.")
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise HTTPException(400, "Missing job_id.")
    job = _get_job(job_id)
    if not job:
        raise HTTPException(404, "job_id not found or expired.")
    return job.get("result") or {}


def _export_csv_bytes(result: Dict[str, Any]) -> bytes:
    # Export: reconciliation_intext_to_reference + missing + uncited + verify rows
    out = io.StringIO()
    out.write("SECTION,STATUS,IN_TEXT,MATCHED_REFERENCE,FLAGS\n")
    for row in (result.get("reconciliation_intext_to_reference") or []):
        out.write(
            f"intext_to_reference,"
            f"{row.get('status','')},"
            f"{json.dumps(row.get('in_text',''))},"
            f"{json.dumps(row.get('matched_reference',''))},"
            f"{json.dumps(row.get('flags',''))}\n"
        )

    out.write("\nSECTION,CITATION_IN_TEXT,COUNT_IN_TEXT\n")
    for row in (result.get("missing_in_references") or []):
        out.write(f"missing,{json.dumps(row.get('citation_in_text',''))},{row.get('count_in_text',0)}\n")

    out.write("\nSECTION,UNCITED_REFERENCE\n")
    for ref in (result.get("uncited_references") or []):
        out.write(f"uncited,{json.dumps(ref)}\n")

    # Online verification (if any)
    ov = (result.get("online_verification") or {})
    rows = (ov.get("rows") or [])
    out.write("\nSECTION,VERIFY_STATUS,REFERENCE,FOUND_TITLE,FOUND_DOI,SCORE,SOURCE\n")
    for r in rows:
        out.write(
            f"verify,{r.get('status','')},"
            f"{json.dumps(r.get('reference',''))},"
            f"{json.dumps(r.get('matched_title',''))},"
            f"{json.dumps(r.get('doi',''))},"
            f"{r.get('score','')},"
            f"{json.dumps(r.get('source',''))}\n"
        )

    return out.getvalue().encode("utf-8", errors="ignore")


def _export_word_bytes(result: Dict[str, Any]) -> bytes:
    if not DOCX_OK:
        raise HTTPException(500, "python-docx not installed on server.")

    doc = DocxDocument()
    doc.add_heading("Citation Crosschecker Report", level=1)
    doc.add_paragraph(f"Generated: {_now_iso()}")
    doc.add_paragraph(f"Filename: {result.get('filename','')}")
    doc.add_paragraph(f"Style: {result.get('style','')}")

    s = result.get("summary") or {}
    doc.add_heading("Summary", level=2)
    for k in ["in_text_citations_found", "reference_entries_found", "missing_in_references", "uncited_references", "match_rate"]:
        doc.add_paragraph(f"{k}: {s.get(k)}")

    doc.add_heading("Missing in References", level=2)
    for row in (result.get("missing_in_references") or []):
        doc.add_paragraph(f"- {row.get('citation_in_text','')} (count={row.get('count_in_text',0)})")

    doc.add_heading("Uncited References", level=2)
    for ref in (result.get("uncited_references") or []):
        doc.add_paragraph(f"- {ref}")

    doc.add_heading("Reconciliation: In-text → Reference", level=2)
    for row in (result.get("reconciliation_intext_to_reference") or [])[:300]:
        doc.add_paragraph(f"[{row.get('status','')}] {row.get('in_text','')} → {row.get('matched_reference','')}")

    ov = (result.get("online_verification") or {})
    rows = (ov.get("rows") or [])
    if rows:
        doc.add_heading("Online Verification", level=2)
        for r in rows[:300]:
            doc.add_paragraph(f"[{r.get('status','')}] {r.get('reference','')} | DOI={r.get('doi','')} | src={r.get('source','')}")

    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()


# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title=APP_TITLE)

# Add CORS middleware to allow frontend requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For development only - restrict in production
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


@app.get("/test")
async def test():
    """Simple test endpoint to verify server is running."""
    return {"message": "Server is working", "status": "ok"}


@app.get("/favicon.ico")
async def favicon():
    """Handle favicon requests to avoid 404 errors."""
    favicon_path = os.path.join(static_dir, "favicon.ico")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path)
    return JSONResponse(status_code=204)  # No content


@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_mode: str = Form("all"),
    throttle_s: str = Form("0.12"),
    use_crossref: str = Form("true"),
    use_openalex: str = Form("true"),
):
    """
    Run initial citation check (no online verification).
    Returns job_id that can be used later for online verification.
    """
    if not ENGINE_OK:
        raise HTTPException(500, "engine.py import failed on server.")

    filename = _safe_filename(file.filename or "upload")
    file_bytes = _read_upload_bytes(file)

    style_s = (style or "apa").strip().lower()
    verify_mode_s = (verify_mode or "all").strip().lower()

    use_crossref_b = str(use_crossref).strip().lower() in {"1", "true", "yes", "y", "on"}
    use_openalex_b = str(use_openalex).strip().lower() in {"1", "true", "yes", "y", "on"}

    try:
        throttle_f = float(throttle_s or 0.12)
    except Exception:
        throttle_f = 0.12

    def _do_crosscheck() -> Dict[str, Any]:
        # Run the citation check (no online verification)
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
    
    # Store references_raw in the job for later verification
    job_id = _store_result(result)
    
    # Also store references_raw separately for easy access
    job = _get_job(job_id)
    if job and result.get("references_raw"):
        with _store_lock:
            job["references_raw"] = result.get("references_raw")

    wrapped = _wrap_output(result)
    wrapped["job_id"] = job_id
    wrapped["batch_size"] = BATCH_SIZE_DEFAULT
    return JSONResponse(wrapped)


@app.post("/verify-online")
async def verify_online(
    job_id: str = Form(...),
    verify_mode: str = Form("all"),
    throttle_s: str = Form("0.12"),
    use_crossref: str = Form("true"),
    use_openalex: str = Form("true"),
):
    """
    Run online verification on an existing job.
    Does NOT re-run the citation check.
    """
    if not VERIFY_OK:
        raise HTTPException(500, "verify.py not available on server.")

    job = _get_job(job_id)
    if not job:
        raise HTTPException(404, "job_id not found or expired.")

    verify_mode_s = (verify_mode or "all").strip().lower()
    use_crossref_b = str(use_crossref).strip().lower() in {"1", "true", "yes", "y", "on"}
    use_openalex_b = str(use_openalex).strip().lower() in {"1", "true", "yes", "y", "on"}

    try:
        throttle_f = float(throttle_s or 0.12)
    except Exception:
        throttle_f = 0.12

    # Check if job already has verification running
    if job["online"]["state"] == "running":
        return JSONResponse({
            "ok": True,
            "job_id": job_id,
            "message": "Verification already running",
            "online": job.get("online")
        })

    # Start background online verification
    t = threading.Thread(
        target=_run_online_batches,
        args=(job_id, verify_mode_s, throttle_f, use_crossref_b, use_openalex_b, BATCH_SIZE_DEFAULT),
        daemon=True,
    )
    t.start()

    return JSONResponse({
        "ok": True,
        "job_id": job_id,
        "message": "Online verification started",
        "online_started": True
    })


@app.post("/check")
async def check(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_mode: str = Form("all"),
    throttle_s: str = Form("0.12"),
    use_crossref: str = Form("true"),
    use_openalex: str = Form("true"),
):
    """Alias for /verify endpoint to maintain compatibility with frontend."""
    return await verify(
        file=file,
        style=style,
        verify_mode=verify_mode,
        throttle_s=throttle_s,
        use_crossref=use_crossref,
        use_openalex=use_openalex,
    )


@app.get("/online/status")
def online_status(job_id: str, include_result: int = 0):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(404, "job_id not found or expired.")
    
    result = job.get("result") or {}
    
    return {
        "ok": True,
        "ts": _now_iso(),
        "job_id": job_id,
        "online": job.get("online") or {},
        "online_verification": result.get("online_verification") or {"summary": {}, "rows": []},
        "result": result if include_result else {},
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
