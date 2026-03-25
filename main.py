# main.py — Citation Crosschecker with Single Job ID System

import io
import os
import re
import uuid
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from engine import run_crosscheck
from verify import (
    submit_verification,
    get_verification_status,
    get_queue_status,
    is_server_busy
)
from acii import compute_acii


APP_TITLE = "CitationCrosschecker"

# ============================================================
# LIFESPAN MANAGER
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage background tasks on startup/shutdown"""
    print("🚀 Starting Citation Crosschecker...")
    print(f"📊 Single job tracking system initialized")
    yield
    print("👋 Shutting down...")

app = FastAPI(title=APP_TITLE, lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "static")),
    name="static"
)

# Store job data - SINGLE SOURCE OF TRUTH
_store: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


# --------------------------------------------------
# Utility Functions
# --------------------------------------------------

def now():
    return datetime.utcnow().isoformat()


def store_result(result):
    """Store result and return job ID"""
    job_id = uuid.uuid4().hex

    with _lock:
        _store[job_id] = {
            "result": result,
            "verification": {
                "state": "idle",           # idle, running, completed, error
                "progress": 0,
                "total": 0,
                "percentage": 0,
                "started_at": None,
                "completed_at": None,
                "results": None
            }
        }

    return job_id


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Get job by ID"""
    with _lock:
        return _store.get(job_id)


def update_verification_status(job_id: str, **kwargs):
    """Update verification status for a job"""
    with _lock:
        if job_id in _store:
            _store[job_id]["verification"].update(kwargs)


def _norm_text_citation(s: str) -> str:
    """Normalize citation text for duplicate detection"""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r'[^a-z0-9]', '', s)
    return s.strip()


def build_reference_to_intext(result):
    """Build mapping from references to in-text citations"""
    mapping = {}
    rows = result.get("reconciliation_intext_to_reference", [])
    
    citation_counter = defaultdict(int)
    citation_samples = defaultdict(list)
    seen_samples = defaultdict(set)
    
    for r in rows:
        ref = r.get("matched_reference")
        if not ref:
            continue
            
        in_text = r.get("in_text", "")
        in_text_norm = _norm_text_citation(in_text)
        
        citation_counter[ref] += 1
        
        if in_text_norm and in_text_norm not in seen_samples[ref]:
            if len(citation_samples[ref]) < 6:
                seen_samples[ref].add(in_text_norm)
                citation_samples[ref].append(in_text)
    
    for ref in citation_counter:
        mapping[ref] = {
            "reference": ref,
            "times_cited": citation_counter[ref],
            "cited_by": citation_samples.get(ref, [])
        }
    
    result_list = list(mapping.values())
    result_list.sort(key=lambda x: x["times_cited"], reverse=True)
    
    return result_list


# ============================================================
# QUEUE STATUS ENDPOINT
# ============================================================

@app.get("/queue/status")
async def queue_status():
    """Get current queue status"""
    status = get_queue_status()
    status["server_busy"] = is_server_busy()
    status["message"] = "Server is busy, please try later" if status["server_busy"] else "Server is ready"
    return status


# ============================================================
# INDEX
# ============================================================

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request}
    )


# ============================================================
# INITIAL DOCUMENT CHECK
# ============================================================

@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa")
):
    """Initial document check - extracts citations and references"""
    
    # Check if server is too busy
    if is_server_busy():
        queue_stats = get_queue_status()
        return JSONResponse(
            status_code=503,
            content={
                "error": "Server is busy",
                "message": "Please wait a moment and try again",
                "queue_size": queue_stats["queue_size"],
                "pending_jobs": queue_stats["pending_jobs"],
                "retry_after": 30
            }
        )
    
    data = await file.read()

    def run():
        return run_crosscheck(
            file_bytes=data,
            filename=file.filename,
            style=style,
            verify_online=False
        )

    result = await run_in_threadpool(run)

    # Build reference -> in-text mapping
    result["reconciliation_reference_to_intext"] = build_reference_to_intext(result)
    
    # Store result and get job ID
    job_id = store_result(result)

    return {
        "job_id": job_id,
        "data": result,
        "queue_status": get_queue_status()
    }


# ============================================================
# ONLINE VERIFICATION - SINGLE JOB ID
# ============================================================

@app.post("/verify-online")
async def verify_online(job_id: str = Form(...)):
    """Submit online verification - uses the same job_id"""
    
    job = get_job(job_id)

    if not job:
        raise HTTPException(404, "Job not found")

    # Check if already running
    if job["verification"]["state"] == "running":
        return {
            "started": False,
            "message": "Verification already in progress",
            "job_id": job_id,
            "progress": job["verification"].get("progress", 0),
            "total": job["verification"].get("total", 0)
        }
    
    # Check if already completed
    if job["verification"]["state"] == "completed":
        return {
            "started": False,
            "message": "Verification already completed",
            "job_id": job_id,
            "completed": True
        }
    
    # Get references to verify
    refs = job["result"].get("references_raw", [])
    
    if not refs:
        update_verification_status(job_id, state="completed", message="No references to verify")
        return {
            "started": False,
            "message": "No references to verify",
            "job_id": job_id
        }
    
    # Update job status
    update_verification_status(
        job_id,
        state="running",
        total=len(refs),
        progress=0,
        percentage=0,
        started_at=now()
    )
    
    # Start verification in background thread
    def run_verification():
        from verify import verify_references_batch
        
        try:
            results = verify_references_batch(refs, style="apa", job_id=job_id)
            
            # Process results
            summary = {
                "verified": 0,
                "likely": 0,
                "needs_review": 0,
                "not_found": 0,
                "offline": 0
            }
            
            for r in results:
                status = r.get("status", "offline")
                if status in summary:
                    summary[status] += 1
                else:
                    summary["offline"] += 1
            
            # Update job with results
            with _lock:
                if job_id in _store:
                    _store[job_id]["result"]["online_verification"] = {
                        "rows": results,
                        "summary": summary
                    }
                    
                    # Compute ACII
                    try:
                        _store[job_id]["result"]["acii"] = compute_acii(_store[job_id]["result"], results)
                    except Exception as e:
                        _store[job_id]["result"]["acii"] = {"error": str(e)}
                    
                    # Rebuild reference mapping
                    _store[job_id]["result"]["reconciliation_reference_to_intext"] = build_reference_to_intext(_store[job_id]["result"])
                    
                    # Deduplicate in-text citations
                    if "reconciliation_intext_to_reference" in _store[job_id]["result"]:
                        unique_cites = {}
                        for item in _store[job_id]["result"]["reconciliation_intext_to_reference"]:
                            cite_text = item.get("in_text", "")
                            cite_norm = _norm_text_citation(cite_text)
                            if cite_norm and cite_norm not in unique_cites:
                                unique_cites[cite_norm] = item
                        _store[job_id]["result"]["reconciliation_intext_to_reference"] = list(unique_cites.values())
                    
                    _store[job_id]["verification"]["state"] = "completed"
                    _store[job_id]["verification"]["completed_at"] = now()
                    _store[job_id]["verification"]["results"] = results
                    
        except Exception as e:
            with _lock:
                if job_id in _store:
                    _store[job_id]["verification"]["state"] = "error"
                    _store[job_id]["verification"]["message"] = str(e)
                    _store[job_id]["verification"]["completed_at"] = now()
    
    thread = threading.Thread(target=run_verification, daemon=True)
    thread.start()
    
    return {
        "started": True,
        "job_id": job_id,
        "total_references": len(refs),
        "message": "Verification started. Check /online/status for progress."
    }


# ============================================================
# STATUS POLLING - SINGLE SOURCE
# ============================================================

@app.get("/online/status")
def online_status(job_id: str):
    """Get verification status - single source of truth"""
    job = get_job(job_id)

    if not job:
        raise HTTPException(404, "Job not found")
    
    verification = job["verification"]
    
    # Build response
    response = {
        "online": {
            "state": verification["state"],
            "progress": verification["progress"],
            "total": verification["total"],
            "percentage": verification["percentage"],
            "started_at": verification["started_at"],
            "completed_at": verification["completed_at"]
        },
        "result": job["result"] if verification["state"] in ["completed", "error"] else None
    }
    
    # Add progress details
    if verification["total"] > 0:
        response["progress"] = {
            "current": verification["progress"],
            "total": verification["total"],
            "percentage": verification["percentage"],
            "status": verification["state"],
            "message": f"Processing: {verification['progress']}/{verification['total']} ({verification['percentage']}%)"
        }
    
    # Add queue status
    response["queue"] = get_queue_status()
    
    return response


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health():
    """Health check with system status"""
    queue_stats = get_queue_status()
    
    return {
        "status": "healthy" if not queue_stats.get("is_busy", False) else "degraded",
        "timestamp": now(),
        "queue": queue_stats,
        "server_busy": queue_stats.get("is_busy", False),
        "message": "Server is operational" if not queue_stats.get("is_busy", False) else "Server is busy, some requests may be queued"
    }


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status_code": exc.status_code,
            "timestamp": now()
        }
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": str(exc) if os.getenv("DEBUG") else "An unexpected error occurred",
            "timestamp": now()
        }
    )
