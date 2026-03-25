# main.py — Citation Crosschecker with Queue Management and Progress Tracking

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
    is_server_busy_check,
    verify_references_batch
)
from acii import compute_acii


APP_TITLE = "CitationCrosschecker"

# ============================================================
# LIFESPAN MANAGER for background tasks
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage background tasks on startup/shutdown"""
    print("🚀 Starting Citation Crosschecker...")
    print(f"📊 Queue system initialized")
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

_store: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()

# Store active verification jobs for progress tracking
_verification_tasks: Dict[str, Dict[str, Any]] = {}
_tasks_lock = threading.Lock()


# --------------------------------------------------
# Utility Functions
# --------------------------------------------------

def now():
    return datetime.utcnow().isoformat()


def store_result(result):
    job_id = uuid.uuid4().hex

    with _lock:
        _store[job_id] = {
            "result": result,
            "online": {
                "state": "idle",
                "progress": 0,
                "total": 0,
                "started_at": None,
                "completed_at": None
            }
        }

    return job_id


def get_job(job_id):
    with _lock:
        return _store.get(job_id)


def _norm_text_citation(s: str) -> str:
    """Normalize citation text for duplicate detection"""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r'[^a-z0-9]', '', s)
    return s.strip()


def build_reference_to_intext(result):
    """
    Build mapping from references to in-text citations.
    Counts EACH citation occurrence, but prevents duplicate ENTRIES.
    """
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
    """Get current queue status for monitoring"""
    status = get_queue_status()
    
    # Add human-readable busy status
    status["server_busy"] = is_server_busy_check()
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
# INITIAL DOCUMENT CHECK (with queue integration)
# ============================================================

@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    background_tasks: BackgroundTasks = None
):
    """Initial document check - extracts citations and references"""
    
    # Check if server is too busy
    if is_server_busy_check():
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
# ONLINE VERIFICATION (QUEUE-BASED WITH PROGRESS)
# ============================================================

@app.post("/verify-online")
async def verify_online(job_id: str = Form(...)):
    """Submit online verification job to queue"""
    
    # Check if server is too busy
    if is_server_busy_check():
        queue_stats = get_queue_status()
        return JSONResponse(
            status_code=503,
            content={
                "error": "Server is busy",
                "message": "Verification queue is full. Please try again later.",
                "queue_size": queue_stats["queue_size"],
                "pending_jobs": queue_stats["pending_jobs"],
                "retry_after": 30
            }
        )
    
    job = get_job(job_id)

    if not job:
        raise HTTPException(404, "Job not found")

    # Check if already running
    if job["online"]["state"] == "running":
        return {
            "started": False,
            "message": "Verification already in progress",
            "job_id": job_id,
            "progress": job["online"].get("progress", 0),
            "total": job["online"].get("total", 0)
        }
    
    # Check if already completed
    if job["online"]["state"] == "done":
        return {
            "started": False,
            "message": "Verification already completed",
            "job_id": job_id,
            "completed": True
        }
    
    # Get references to verify
    refs = job["result"].get("references_raw", [])
    
    if not refs:
        job["online"]["state"] = "done"
        job["online"]["message"] = "No references to verify"
        return {
            "started": False,
            "message": "No references to verify",
            "job_id": job_id
        }
    
    # Submit to verification queue
    verification_job_id = submit_verification(refs, style="apa")
    
    # Store verification job ID for tracking
    with _tasks_lock:
        _verification_tasks[job_id] = {
            "verification_job_id": verification_job_id,
            "started_at": now(),
            "refs_count": len(refs)
        }
    
    # Update job status
    job["online"]["state"] = "running"
    job["online"]["total"] = len(refs)
    job["online"]["progress"] = 0
    job["online"]["started_at"] = now()
    job["online"]["verification_job_id"] = verification_job_id
    
    return {
        "started": True,
        "job_id": job_id,
        "verification_job_id": verification_job_id,
        "total_references": len(refs),
        "message": "Verification started. Check /online/status for progress."
    }


# ============================================================
# STATUS POLLING (WITH PROGRESS)
# ============================================================

@app.get("/online/status")
def online_status(job_id: str):
    """Get verification status with progress tracking"""
    job = get_job(job_id)

    if not job:
        raise HTTPException(404, "Job not found")
    
    # Get verification task progress
    verification_progress = None
    with _tasks_lock:
        task = _verification_tasks.get(job_id)
        if task:
            verification_job_id = task.get("verification_job_id")
            if verification_job_id:
                verification_status = get_verification_status(verification_job_id)
                if verification_status:
                    verification_progress = verification_status
                    
                    # Update job with progress
                    job["online"]["progress"] = verification_status.get("progress", 0)
                    job["online"]["percentage"] = verification_status.get("percentage", 0)
                    
                    # If verification is complete, process results
                    if verification_status["status"] == "completed":
                        results = verification_status.get("results", [])
                        
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
                        
                        # Attach verification results
                        job["result"]["online_verification"] = {
                            "rows": results,
                            "summary": summary
                        }
                        
                        # Compute ACII
                        try:
                            job["result"]["acii"] = compute_acii(job["result"], results)
                        except Exception as e:
                            job["result"]["acii"] = {"error": str(e)}
                        
                        # Rebuild reference mapping
                        job["result"]["reconciliation_reference_to_intext"] = build_reference_to_intext(job["result"])
                        
                        # Deduplicate in-text citations
                        if "reconciliation_intext_to_reference" in job["result"]:
                            unique_cites = {}
                            for item in job["result"]["reconciliation_intext_to_reference"]:
                                cite_text = item.get("in_text", "")
                                cite_norm = _norm_text_citation(cite_text)
                                if cite_norm and cite_norm not in unique_cites:
                                    unique_cites[cite_norm] = item
                            job["result"]["reconciliation_intext_to_reference"] = list(unique_cites.values())
                        
                        job["online"]["state"] = "done"
                        job["online"]["completed_at"] = now()
                        
                        # Clean up task
                        del _verification_tasks[job_id]
                    
                    elif verification_status["status"] == "failed":
                        job["online"]["state"] = "error"
                        job["online"]["message"] = verification_status.get("error", "Verification failed")
                        job["online"]["completed_at"] = now()
                        del _verification_tasks[job_id]
    
    # Build response
    response = {
        "online": job["online"],
        "result": job["result"] if job["online"]["state"] in ["done", "error"] else None
    }
    
    # Add progress details if available
    if verification_progress:
        response["progress"] = {
            "current": verification_progress.get("progress", 0),
            "total": verification_progress.get("total", 0),
            "percentage": verification_progress.get("percentage", 0),
            "status": verification_progress.get("status", "pending"),
            "estimated_remaining": _estimate_remaining_time(verification_progress)
        }
    
    # Add queue status
    response["queue"] = get_queue_status()
    
    return response


def _estimate_remaining_time(progress: Dict) -> Optional[str]:
    """Estimate remaining time based on progress"""
    if not progress:
        return None
    
    progress_pct = progress.get("percentage", 0)
    if progress_pct <= 0 or progress_pct >= 100:
        return None
    
    started_at = progress.get("started_at")
    if not started_at:
        return None
    
    try:
        start_time = datetime.fromisoformat(started_at)
        elapsed = (datetime.utcnow() - start_time).total_seconds()
        
        if elapsed < 5:
            return "Just started"
        
        if progress_pct > 0:
            estimated_total = elapsed / (progress_pct / 100)
            remaining = estimated_total - elapsed
            
            if remaining < 60:
                return f"{int(remaining)} seconds remaining"
            elif remaining < 3600:
                return f"{int(remaining / 60)} minutes remaining"
            else:
                return f"{int(remaining / 3600)} hours remaining"
    except:
        pass
    
    return "Processing..."


# ============================================================
# HEALTH CHECK (with queue status)
# ============================================================

@app.get("/health")
def health():
    """Health check with system status"""
    queue_stats = get_queue_status()
    
    return {
        "status": "healthy" if not queue_stats["is_busy"] else "degraded",
        "timestamp": now(),
        "queue": queue_stats,
        "server_busy": queue_stats["is_busy"],
        "message": "Server is operational" if not queue_stats["is_busy"] else "Server is busy, some requests may be queued"
    }


# ============================================================
# QUEUE MONITORING (Admin)
# ============================================================

@app.get("/admin/queue")
async def admin_queue():
    """Admin endpoint to monitor queue (can be protected later)"""
    return {
        "queue_status": get_queue_status(),
        "active_tasks": len(_verification_tasks),
        "stored_jobs": len(_store)
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
