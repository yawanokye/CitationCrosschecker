# main.py — Citation Crosschecker with Single Job ID System

import io
import os
import re
import uuid
import threading
import time
import json
import pickle
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets

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

# ===============================
# COUNTER SETUP
# ===============================

COUNTER_FILE = "verify_count.txt"

def increment_counter():
    if not os.path.exists(COUNTER_FILE):
        with open(COUNTER_FILE, "w") as f:
            f.write("0")

    with open(COUNTER_FILE, "r+") as f:
        count = int(f.read().strip() or 0)
        count += 1
        f.seek(0)
        f.write(str(count))
        f.truncate()

# ===============================
# ENHANCED STATS TRACKING SYSTEM
# ===============================

STATS_FILE = "upload_stats.json"
DETAILED_STATS_FILE = "detailed_uploads.pkl"

class UploadStats:
    """Track detailed upload and processing statistics"""
    
    def __init__(self):
        self.stats_file = STATS_FILE
        self.detailed_file = DETAILED_STATS_FILE
        self._load_stats()
    
    def _load_stats(self):
        """Load existing stats from files"""
        # Load main stats
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, 'r') as f:
                    self.stats = json.load(f)
            except:
                self.stats = self._init_stats()
        else:
            self.stats = self._init_stats()
        
        # Load detailed upload history
        if os.path.exists(self.detailed_file):
            try:
                with open(self.detailed_file, 'rb') as f:
                    self.detailed_uploads = pickle.load(f)
            except:
                self.detailed_uploads = []
        else:
            self.detailed_uploads = []
    
    def _init_stats(self):
        """Initialize stats structure"""
        return {
            "total_uploads": 0,
            "total_processed": 0,
            "total_failed": 0,
            "total_verifications": 0,
            "total_references_checked": 0,
            "total_unique_users": 0,
            "daily_stats": {},
            "monthly_stats": {},
            "hourly_stats": {},
            "average_processing_time": 0,
            "processing_times": [],
            "start_date": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat()
        }
    
    def _save_stats(self):
        """Save stats to file"""
        self.stats["last_updated"] = datetime.now().isoformat()
        with open(self.stats_file, 'w') as f:
            json.dump(self.stats, f, indent=2)
    
    def _save_detailed_uploads(self):
        """Save detailed upload history"""
        # Keep only last 1000 entries to prevent file bloat
        if len(self.detailed_uploads) > 1000:
            self.detailed_uploads = self.detailed_uploads[-1000:]
        with open(self.detailed_file, 'wb') as f:
            pickle.dump(self.detailed_uploads, f)
    
    def add_upload(self, filename: str, file_size: int, references_count: int, 
                   processing_time: float = None, success: bool = True, 
                   ip_address: str = None, error: str = None):
        """Record a new upload"""
        
        now = datetime.now()
        date_key = now.strftime("%Y-%m-%d")
        month_key = now.strftime("%Y-%m")
        hour_key = now.strftime("%Y-%m-%d %H:00")
        
        # Update main counters
        self.stats["total_uploads"] += 1
        if success:
            self.stats["total_processed"] += 1
        else:
            self.stats["total_failed"] += 1
        
        self.stats["total_references_checked"] += references_count
        
        # Update daily stats
        if date_key not in self.stats["daily_stats"]:
            self.stats["daily_stats"][date_key] = {
                "uploads": 0,
                "processed": 0,
                "failed": 0,
                "references": 0,
                "processing_times": []
            }
        self.stats["daily_stats"][date_key]["uploads"] += 1
        if success:
            self.stats["daily_stats"][date_key]["processed"] += 1
        else:
            self.stats["daily_stats"][date_key]["failed"] += 1
        self.stats["daily_stats"][date_key]["references"] += references_count
        if processing_time:
            self.stats["daily_stats"][date_key]["processing_times"].append(processing_time)
        
        # Update monthly stats
        if month_key not in self.stats["monthly_stats"]:
            self.stats["monthly_stats"][month_key] = {
                "uploads": 0,
                "processed": 0,
                "failed": 0,
                "references": 0
            }
        self.stats["monthly_stats"][month_key]["uploads"] += 1
        if success:
            self.stats["monthly_stats"][month_key]["processed"] += 1
        else:
            self.stats["monthly_stats"][month_key]["failed"] += 1
        self.stats["monthly_stats"][month_key]["references"] += references_count
        
        # Update hourly stats
        if hour_key not in self.stats["hourly_stats"]:
            self.stats["hourly_stats"][hour_key] = {
                "uploads": 0,
                "processed": 0,
                "failed": 0
            }
        self.stats["hourly_stats"][hour_key]["uploads"] += 1
        if success:
            self.stats["hourly_stats"][hour_key]["processed"] += 1
        else:
            self.stats["hourly_stats"][hour_key]["failed"] += 1
        
        # Update average processing time
        if processing_time:
            self.stats["processing_times"].append(processing_time)
            # Keep only last 100
            if len(self.stats["processing_times"]) > 100:
                self.stats["processing_times"] = self.stats["processing_times"][-100:]
            self.stats["average_processing_time"] = sum(self.stats["processing_times"]) / len(self.stats["processing_times"])
        
        # Add to detailed uploads
        upload_record = {
            "timestamp": now.isoformat(),
            "filename": filename,
            "file_size": file_size,
            "references_count": references_count,
            "processing_time": processing_time,
            "success": success,
            "ip_address": ip_address,
            "error": error
        }
        self.detailed_uploads.append(upload_record)
        
        # Save stats
        self._save_stats()
        self._save_detailed_uploads()
    
    def add_verification(self, job_id: str, references_count: int, success: bool = True):
        """Record a verification run"""
        self.stats["total_verifications"] += 1
        self._save_stats()
    
    def get_stats(self, detailed: bool = False, days: int = None) -> Dict:
        """Get statistics"""
        
        now = datetime.now()
        
        # Prepare response
        response = {
            "total_stats": {
                "total_uploads": self.stats["total_uploads"],
                "total_processed": self.stats["total_processed"],
                "total_failed": self.stats["total_failed"],
                "success_rate": round((self.stats["total_processed"] / max(self.stats["total_uploads"], 1)) * 100, 2),
                "total_references_checked": self.stats["total_references_checked"],
                "total_verifications": self.stats["total_verifications"],
                "average_processing_time": round(self.stats["average_processing_time"], 2) if self.stats["average_processing_time"] else 0,
                "start_date": self.stats["start_date"],
                "last_updated": self.stats["last_updated"]
            }
        }
        
        # Add daily stats for last N days
        if days:
            daily_stats = {}
            for i in range(days):
                date_key = (now - timedelta(days=i)).strftime("%Y-%m-%d")
                if date_key in self.stats["daily_stats"]:
                    daily_stats[date_key] = self.stats["daily_stats"][date_key].copy()
                    # Calculate average processing time for the day
                    if daily_stats[date_key]["processing_times"]:
                        daily_stats[date_key]["avg_processing_time"] = round(
                            sum(daily_stats[date_key]["processing_times"]) / len(daily_stats[date_key]["processing_times"]), 2
                        )
                    else:
                        daily_stats[date_key]["avg_processing_time"] = 0
                    del daily_stats[date_key]["processing_times"]  # Clean up for response
            response["daily_stats"] = daily_stats
        
        # Add monthly stats for last 6 months
        monthly_stats = {}
        months_to_show = 6
        for i in range(months_to_show):
            month_key = (now - timedelta(days=30*i)).strftime("%Y-%m")
            if month_key in self.stats["monthly_stats"]:
                monthly_stats[month_key] = self.stats["monthly_stats"][month_key]
        response["monthly_stats"] = monthly_stats
        
        # Add recent uploads if detailed
        if detailed:
            response["recent_uploads"] = self.detailed_uploads[-20:]  # Last 20 uploads
        
        return response
    
    def clear_stats(self, keep_last_days: int = 30):
        """Clear old stats (keep last N days)"""
        
        cutoff_date = datetime.now() - timedelta(days=keep_last_days)
        
        # Clear daily stats
        daily_keys_to_remove = []
        for date_key in self.stats["daily_stats"]:
            try:
                date_obj = datetime.strptime(date_key, "%Y-%m-%d")
                if date_obj < cutoff_date:
                    daily_keys_to_remove.append(date_key)
            except:
                pass
        
        for key in daily_keys_to_remove:
            del self.stats["daily_stats"][key]
        
        # Clear old detailed uploads
        self.detailed_uploads = [u for u in self.detailed_uploads 
                                  if datetime.fromisoformat(u["timestamp"]) > cutoff_date]
        
        self._save_stats()
        self._save_detailed_uploads()


# Initialize stats tracker
stats_tracker = UploadStats()

# ===============================
# AUTH SETUP
# ===============================

security = HTTPBasic()

USERNAME = "admin"
PASSWORD = "Ano77kye7509#"  # change this

def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, USERNAME)
    correct_password = secrets.compare_digest(credentials.password, PASSWORD)

    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="Secure Area"'},
        )


APP_TITLE = "CitationCrosschecker"

# ============================================================
# LIFESPAN MANAGER
# ============================================================

@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Manage background tasks on startup/shutdown"""
    print("🚀 Starting Citation Crosschecker...")
    print(f"📊 Single job tracking system initialized")
    print(f"📈 Stats tracker loaded: {stats_tracker.stats['total_uploads']} total uploads")
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
# PRIVACY POLICY
# ============================================================

@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    """Privacy policy page"""
    return templates.TemplateResponse("privacy.html", {"request": request})

# ============================================================
# INITIAL DOCUMENT CHECK
# ============================================================

@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    request: Request = None
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
    
    start_time = time.time()
    data = await file.read()
    file_size = len(data)
    
    try:
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
        
        # Get references count
        references_count = len(result.get("references_raw", []))
        
        # Calculate processing time
        processing_time = time.time() - start_time
        
        # Get client IP if available
        client_ip = None
        if request and hasattr(request, "client"):
            client_ip = request.client.host if request.client else None
        
        # Record stats
        stats_tracker.add_upload(
            filename=file.filename,
            file_size=file_size,
            references_count=references_count,
            processing_time=processing_time,
            success=True,
            ip_address=client_ip
        )
        
        increment_counter()
        # Store result and get job ID
        job_id = store_result(result)

        return {
            "job_id": job_id,
            "data": result,
            "queue_status": get_queue_status()
        }
        
    except Exception as e:
        # Record failed upload
        processing_time = time.time() - start_time
        stats_tracker.add_upload(
            filename=file.filename,
            file_size=file_size,
            references_count=0,
            processing_time=processing_time,
            success=False,
            error=str(e)
        )
        raise


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
    
    # Record verification stats
    stats_tracker.add_verification(job_id, len(refs), success=True)
    
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
# ENHANCED PRIVATE STATS ENDPOINTS
# ============================================================

@app.get("/private-stats")
def get_private_stats(
    credentials: HTTPBasicCredentials = Depends(security),
    detailed: bool = False,
    days: int = 30
):
    """Get comprehensive upload statistics (protected endpoint)"""
    
    # Authenticate
    authenticate(credentials)
    
    # Get stats
    stats = stats_tracker.get_stats(detailed=detailed, days=days)
    
    # Add additional system info
    stats["system_info"] = {
        "current_time": datetime.now().isoformat(),
        "active_jobs": len([j for j in _store.values() if j["verification"]["state"] == "running"]),
        "total_jobs": len(_store),
        "queue_status": get_queue_status(),
        "server_busy": is_server_busy()
    }
    
    return stats


@app.get("/private-stats/count")
def get_simple_count(credentials: HTTPBasicCredentials = Depends(security)):
    """Simple manuscript count (protected endpoint) - backward compatible"""
    
    authenticate(credentials)
    
    try:
        with open(COUNTER_FILE) as f:
            count = int(f.read())
    except:
        count = 0
    
    return {"manuscripts_checked": count}


@app.get("/private-stats/clear")
def clear_old_stats(
    credentials: HTTPBasicCredentials = Depends(security),
    keep_days: int = 30
):
    """Clear stats older than specified days (protected endpoint)"""
    
    authenticate(credentials)
    
    try:
        stats_tracker.clear_stats(keep_last_days=keep_days)
        return {
            "success": True,
            "message": f"Cleared stats older than {keep_days} days",
            "kept_days": keep_days
        }
    except Exception as e:
        raise HTTPException(500, f"Error clearing stats: {str(e)}")


@app.get("/private-stats/export")
def export_stats(
    credentials: HTTPBasicCredentials = Depends(security),
    format: str = "json"
):
    """Export stats in JSON or CSV format (protected endpoint)"""
    
    authenticate(credentials)
    
    stats = stats_tracker.get_stats(detailed=True, days=365)
    
    if format == "csv":
        # Create CSV from detailed uploads
        import csv
        output = io.StringIO()
        
        if stats.get("recent_uploads"):
            writer = csv.DictWriter(output, fieldnames=stats["recent_uploads"][0].keys())
            writer.writeheader()
            writer.writerows(stats["recent_uploads"])
            
            return Response(
                content=output.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=upload_stats.csv"}
            )
    
    return stats


@app.get("/private-stats/performance")
def get_performance_stats(
    credentials: HTTPBasicCredentials = Depends(security)
):
    """Get performance metrics (protected endpoint)"""
    
    authenticate(credentials)
    
    stats = stats_tracker.get_stats(detailed=False)
    
    # Calculate additional metrics
    days_online = max((datetime.now() - datetime.fromisoformat(stats["total_stats"]["start_date"])).days, 1)
    
    performance = {
        "average_processing_time": stats["total_stats"]["average_processing_time"],
        "success_rate": stats["total_stats"]["success_rate"],
        "total_references_per_upload": round(
            stats["total_stats"]["total_references_checked"] / max(stats["total_stats"]["total_uploads"], 1), 2
        ),
        "uploads_per_day": round(stats["total_stats"]["total_uploads"] / days_online, 2),
        "references_per_day": round(stats["total_stats"]["total_references_checked"] / days_online, 2)
    }
    
    return performance


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
# STATISTICS WEB PAGE
# ============================================================

@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    """Statistics dashboard page - requires authentication"""
    # The actual stats data will be loaded via JavaScript
    # that calls the /private-stats API with authentication
    return templates.TemplateResponse(
        "stats.html",
        {"request": request}
    )
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
