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
    is_server_busy,
    get_verification_results,
    clear_verification_results
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

# Ensure templates directory exists
templates_dir = os.path.join(BASE_DIR, "templates")
if not os.path.exists(templates_dir):
    os.makedirs(templates_dir)

templates = Jinja2Templates(directory=templates_dir)

# Ensure static directory exists
static_dir = os.path.join(BASE_DIR, "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount(
    "/static",
    StaticFiles(directory=static_dir),
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
                "results": None,
                "verification_job_id": None,  # Store the verification job ID for syncing
                "summary": None,  # Store verification summary
                "results_count": 0  # Store count of results
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


def start_progress_sync(job_id: str, verification_job_id: str):
    """Background thread to sync progress and results from verify.py to main store"""
    def sync():
        print(f"[DEBUG] ========================================")
        print(f"[DEBUG] Sync thread started for job {job_id}")
        print(f"[DEBUG] Verification job ID: {verification_job_id}")
        print(f"[DEBUG] ========================================")
        
        while True:
            status = get_verification_status(verification_job_id)
            if status:
                print(f"[DEBUG] Sync status: {status.get('status')} - Progress: {status.get('progress')}/{status.get('total')} ({status.get('percentage')}%)")
                
                with _lock:
                    if job_id in _store:
                        # Update progress
                        _store[job_id]["verification"]["progress"] = status.get("progress", 0)
                        _store[job_id]["verification"]["percentage"] = status.get("percentage", 0)
                        _store[job_id]["verification"]["state"] = status.get("status", "running")
                        _store[job_id]["verification"]["total"] = status.get("total", 0)
                        
                        # When complete, get the actual results
                        if status.get("status") == "completed":
                            print(f"[DEBUG] Job {verification_job_id} marked as completed!")
                            print(f"[DEBUG] Attempting to retrieve verification results...")
                            
                            # Try multiple times to get results
                            verification_results = None
                            max_attempts = 10
                            for attempt in range(max_attempts):
                                verification_results = get_verification_results(verification_job_id)
                                if verification_results:
                                    print(f"[DEBUG] Retrieved {len(verification_results)} results on attempt {attempt + 1}")
                                    break
                                if attempt < max_attempts - 1:
                                    print(f"[DEBUG] No results yet, attempt {attempt + 1}/{max_attempts}, waiting 2 seconds...")
                                    time.sleep(2)
                            
                            if verification_results:
                                print(f"[DEBUG] Successfully retrieved {len(verification_results)} verification results")
                                
                                # Store in main result
                                summary = _compute_verification_summary(verification_results)
                                print(f"[DEBUG] Summary: {summary}")
                                
                                _store[job_id]["result"]["online_verification"] = {
                                    "rows": verification_results,
                                    "summary": summary
                                }
                                
                                # Update ACII with verification results
                                try:
                                    _store[job_id]["result"]["acii"] = compute_acii(
                                        _store[job_id]["result"], 
                                        verification_results
                                    )
                                    print(f"[DEBUG] ACII updated: {_store[job_id]['result']['acii'].get('ACII', 'N/A')}")
                                except Exception as e:
                                    print(f"[DEBUG] ACII computation error: {e}")
                                
                                # Rebuild reference mapping with verification data
                                try:
                                    _store[job_id]["result"]["reconciliation_reference_to_intext"] = build_reference_to_intext(_store[job_id]["result"])
                                    print(f"[DEBUG] Reference mapping rebuilt")
                                except Exception as e:
                                    print(f"[DEBUG] Error rebuilding reference mapping: {e}")
                                
                                _store[job_id]["verification"]["results"] = verification_results
                                _store[job_id]["verification"]["results_count"] = len(verification_results)
                                _store[job_id]["verification"]["summary"] = summary
                            else:
                                print(f"[DEBUG] WARNING: No verification results found after {max_attempts} attempts!")
                                _store[job_id]["verification"]["state"] = "error"
                                _store[job_id]["verification"]["message"] = "No results retrieved after completion"
                            
                            _store[job_id]["verification"]["state"] = "completed"
                            _store[job_id]["verification"]["completed_at"] = now()
                            print(f"[DEBUG] Job {job_id} marked as completed with {_store[job_id]['verification'].get('results_count', 0)} results")
                            break
                            
                        elif status.get("status") == "error":
                            print(f"[DEBUG] Job {verification_job_id} errored: {status.get('error', 'Unknown error')}")
                            with _lock:
                                if job_id in _store:
                                    _store[job_id]["verification"]["state"] = "error"
                                    _store[job_id]["verification"]["message"] = status.get("error", "Unknown error")
                            break
            else:
                print(f"[DEBUG] No status found for verification job {verification_job_id}, waiting...")
            
            time.sleep(2)
        
        print(f"[DEBUG] Sync thread exiting for job {job_id}")
    
    thread = threading.Thread(target=sync, daemon=True)
    thread.start()
    return thread


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


def _compute_verification_summary(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """Helper to compute verification summary"""
    summary = {
        "verified": 0,
        "likely": 0,
        "needs_review": 0,
        "not_found": 0,
        "offline": 0,
        "total": len(rows)
    }
    
    for r in rows:
        if r:
            status = r.get("status", "offline")
            if status in summary:
                summary[status] += 1
            else:
                summary["offline"] += 1
    
    return summary


# ============================================================
# DEBUG ENDPOINTS
# ============================================================

@app.get("/debug/job/{job_id}")
async def debug_job(job_id: str):
    """Debug endpoint to check job status"""
    job = get_job(job_id)
    if not job:
        return {"error": "Job not found"}
    
    verification = job.get("verification", {})
    result = job.get("result", {})
    
    return {
        "job_id": job_id,
        "verification": {
            "state": verification.get("state"),
            "progress": verification.get("progress"),
            "total": verification.get("total"),
            "percentage": verification.get("percentage"),
            "started_at": verification.get("started_at"),
            "completed_at": verification.get("completed_at"),
            "verification_job_id": verification.get("verification_job_id"),
            "results_count": verification.get("results_count", 0),
            "summary": verification.get("summary", {})
        },
        "has_result": bool(result),
        "has_online_verification": "online_verification" in result,
        "online_verification_rows": len(result.get("online_verification", {}).get("rows", [])),
        "references_count": len(result.get("references_raw", [])),
        "acii_score": result.get("acii", {}).get("ACII", "N/A")
    }


@app.get("/debug/verify-status/{verification_job_id}")
async def debug_verify_status(verification_job_id: str):
    """Debug endpoint to check verify.py job status"""
    from verify import get_verification_status, get_verification_results
    
    status = get_verification_status(verification_job_id)
    results = get_verification_results(verification_job_id)
    
    return {
        "verification_job_id": verification_job_id,
        "status": status,
        "results_count": len(results) if results else 0,
        "has_results": results is not None
    }


@app.get("/debug/all-jobs")
async def debug_all_jobs():
    """List all jobs in the system"""
    with _lock:
        jobs = {}
        for job_id, job_data in _store.items():
            jobs[job_id] = {
                "verification_state": job_data.get("verification", {}).get("state"),
                "verification_progress": job_data.get("verification", {}).get("progress"),
                "verification_total": job_data.get("verification", {}).get("total"),
                "has_results": bool(job_data.get("result")),
                "created_at": job_data.get("created_at", "N/A")
            }
        return {"total_jobs": len(jobs), "jobs": jobs}


@app.post("/debug/retry-verification/{job_id}")
async def debug_retry_verification(job_id: str):
    """Manually trigger verification for debugging"""
    job = get_job(job_id)
    if not job:
        return {"error": "Job not found"}
    
    refs = job["result"].get("references_raw", [])
    if not refs:
        return {"error": "No references to verify"}
    
    # Force verification
    from verify import verify_references_batch, get_verification_results
    
    try:
        # Create a temporary job ID for this debug run
        temp_job_id = uuid.uuid4().hex
        results = verify_references_batch(refs, style="apa", job_id=temp_job_id)
        
        # Get the stored results
        stored_results = get_verification_results(temp_job_id)
        final_results = stored_results if stored_results else results
        
        if final_results:
            # Process results
            summary = _compute_verification_summary(final_results)
            
            # Update job with results
            with _lock:
                if job_id in _store:
                    _store[job_id]["result"]["online_verification"] = {
                        "rows": final_results,
                        "summary": summary
                    }
                    
                    # Compute ACII
                    try:
                        _store[job_id]["result"]["acii"] = compute_acii(_store[job_id]["result"], final_results)
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
                    _store[job_id]["verification"]["results"] = final_results
                    _store[job_id]["verification"]["progress"] = len(refs)
                    _store[job_id]["verification"]["percentage"] = 100
                    _store[job_id]["verification"]["results_count"] = len(final_results)
                    _store[job_id]["verification"]["summary"] = summary
            
            return {
                "success": True,
                "summary": summary,
                "results_count": len(final_results)
            }
        else:
            return {"error": "No results returned"}
        
    except Exception as e:
        return {"error": str(e)}


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
    
    print(f"[DEBUG] Starting verification for job {job_id} with {len(refs)} references")
    
    # Update job status
    update_verification_status(
        job_id,
        state="running",
        total=len(refs),
        progress=0,
        percentage=0,
        started_at=now()
    )
    
    # Submit to verification queue
    verification_job_id = submit_verification(refs, style="apa")
    
    # Store verification job ID for tracking
    update_verification_status(job_id, verification_job_id=verification_job_id)
    
    # Start progress sync thread
    start_progress_sync(job_id, verification_job_id)
    
    return {
        "started": True,
        "job_id": job_id,
        "verification_job_id": verification_job_id,
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
    result = job.get("result", {})
    
    # Build response
    response = {
        "online": {
            "state": verification["state"],
            "progress": verification["progress"],
            "total": verification["total"],
            "percentage": verification["percentage"],
            "started_at": verification["started_at"],
            "completed_at": verification["completed_at"],
            "summary": verification.get("summary", {}),
            "results_count": verification.get("results_count", 0)
        },
        "result": result if verification["state"] in ["completed", "error"] else None
    }
    
    # If verification is complete, include online_verification data
    if verification["state"] == "completed" and "online_verification" in result:
        response["online_verification"] = {
            "rows": result["online_verification"]["rows"],
            "summary": result["online_verification"]["summary"]
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


# Ensure app is exported for Gunicorn
app = app
