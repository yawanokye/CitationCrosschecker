# main.py — Citation Crosschecker with Single Job ID System

import io
import os
import re
import uuid
import threading
import time
import json
import sqlite3
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
# DATABASE SETUP - SQLite (Works with Python 3.14)
# ===============================

# Use /tmp for SQLite database on Render (writable)
DB_PATH = '/tmp/citation_stats.db'
print(f"📁 SQLite database path: {DB_PATH}")

class SQLiteStats:
    """Stats tracker using SQLite - PERSISTENT across restarts"""
    
    def __init__(self):
        self.db_path = DB_PATH
        self._lock = threading.Lock()
        self._init_database()
    
    def _get_connection(self):
        """Get database connection"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_database(self):
        """Create tables if they don't exist"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                # Create stats table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS stats (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        total_uploads INTEGER DEFAULT 0,
                        total_processed INTEGER DEFAULT 0,
                        total_failed INTEGER DEFAULT 0,
                        total_verifications INTEGER DEFAULT 0,
                        total_references_checked INTEGER DEFAULT 0,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Create uploads table for detailed records
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS uploads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        filename TEXT,
                        file_size INTEGER,
                        references_count INTEGER,
                        processing_time REAL,
                        success INTEGER,
                        ip_address TEXT,
                        error TEXT
                    )
                """)
                
                # Create daily_stats table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS daily_stats (
                        date TEXT PRIMARY KEY,
                        uploads INTEGER DEFAULT 0,
                        processed INTEGER DEFAULT 0,
                        failed INTEGER DEFAULT 0,
                        references_count INTEGER DEFAULT 0,
                        total_processing_time REAL DEFAULT 0,
                        processing_count INTEGER DEFAULT 0
                    )
                """)
                
                # Insert initial stats if not exists
                cursor.execute("""
                    INSERT OR IGNORE INTO stats (id, total_uploads, total_processed, total_failed, total_references_checked)
                    VALUES (1, 0, 0, 0, 0)
                """)
                
                conn.commit()
                print("✅ SQLite database initialized")
                
                # Show existing stats if any
                cursor.execute("SELECT total_uploads FROM stats WHERE id = 1")
                row = cursor.fetchone()
                if row:
                    print(f"📊 Existing stats: {row[0]} total uploads")
                    
        except Exception as e:
            print(f"❌ Failed to initialize database: {e}")
    
    def add_upload(self, filename: str, file_size: int, references_count: int, 
                   processing_time: float = None, success: bool = True, 
                   ip_address: str = None, error: str = None):
        """Record a new upload"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                # Insert upload record
                cursor.execute("""
                    INSERT INTO uploads 
                    (filename, file_size, references_count, processing_time, success, ip_address, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (filename, file_size, references_count, processing_time, 1 if success else 0, ip_address, error))
                
                # Update main stats
                if success:
                    cursor.execute("""
                        UPDATE stats 
                        SET total_uploads = total_uploads + 1,
                            total_processed = total_processed + 1,
                            total_references_checked = total_references_checked + ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = 1
                    """, (references_count,))
                else:
                    cursor.execute("""
                        UPDATE stats 
                        SET total_uploads = total_uploads + 1,
                            total_failed = total_failed + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = 1
                    """)
                
                # Update daily stats
                today = datetime.now().strftime("%Y-%m-%d")
                cursor.execute("""
                    INSERT INTO daily_stats (date, uploads, processed, failed, references_count)
                    VALUES (?, 1, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        uploads = uploads + 1,
                        processed = processed + ?,
                        failed = failed + ?,
                        references_count = references_count + ?
                """, (today, 1 if success else 0, 0 if success else 1, references_count if success else 0,
                      1 if success else 0, 0 if success else 1, references_count if success else 0))
                
                # Update processing time
                if processing_time and success:
                    cursor.execute("""
                        UPDATE daily_stats 
                        SET total_processing_time = total_processing_time + ?,
                            processing_count = processing_count + 1
                        WHERE date = ?
                    """, (processing_time, today))
                
                conn.commit()
                
                # Get updated total for logging
                cursor.execute("SELECT total_uploads FROM stats WHERE id = 1")
                total = cursor.fetchone()[0]
                print(f"📊 Recorded in SQLite: {filename} - {references_count} refs (Total: {total})")
                return True
        except Exception as e:
            print(f"❌ Database error in add_upload: {e}")
            return False
    
    def add_verification(self, job_id: str, references_count: int, success: bool = True):
        """Record a verification run"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE stats 
                    SET total_verifications = total_verifications + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """)
                conn.commit()
        except Exception as e:
            print(f"❌ Database error in add_verification: {e}")
    
    def get_stats(self, detailed: bool = False, days: int = 30):
        """Get statistics"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                # Get main stats
                cursor.execute("SELECT * FROM stats WHERE id = 1")
                row = cursor.fetchone()
                
                if row:
                    total_uploads = row[1]
                    total_processed = row[2]
                    total_failed = row[3]
                    total_verifications = row[4]
                    total_references = row[5]
                    updated_at = row[6]
                else:
                    total_uploads = total_processed = total_failed = total_verifications = total_references = 0
                    updated_at = datetime.now()
                
                success_rate = round((total_processed / max(total_uploads, 1)) * 100, 2)
                
                # Get average processing time
                cursor.execute("""
                    SELECT AVG(processing_time) 
                    FROM uploads 
                    WHERE success = 1 AND processing_time IS NOT NULL
                """)
                avg_time = cursor.fetchone()[0]
                avg_processing_time = round(avg_time, 2) if avg_time else 0
                
                # Get daily stats
                daily_stats = {}
                if days:
                    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                    cursor.execute("""
                        SELECT date, uploads, processed, failed, references_count,
                               CASE WHEN processing_count > 0 
                                    THEN total_processing_time / processing_count 
                                    ELSE 0 END as avg_time
                        FROM daily_stats
                        WHERE date >= ?
                        ORDER BY date DESC
                    """, (cutoff_date,))
                    
                    for row in cursor.fetchall():
                        daily_stats[row[0]] = {
                            "uploads": row[1],
                            "processed": row[2],
                            "failed": row[3],
                            "references": row[4],
                            "avg_processing_time": round(row[5], 2) if row[5] else 0
                        }
                
                # Get recent uploads
                recent_uploads = []
                if detailed:
                    cursor.execute("""
                        SELECT timestamp, filename, references_count, processing_time, success, error
                        FROM uploads
                        ORDER BY timestamp DESC
                        LIMIT 20
                    """)
                    for row in cursor.fetchall():
                        recent_uploads.append({
                            "timestamp": row[0],
                            "filename": row[1],
                            "references_count": row[2],
                            "processing_time": row[3],
                            "success": bool(row[4]),
                            "error": row[5]
                        })
                
                # Get start date
                cursor.execute("SELECT MIN(timestamp) FROM uploads")
                start_date_row = cursor.fetchone()
                start_date = start_date_row[0] if start_date_row[0] else datetime.now().isoformat()
                
                return {
                    "total_stats": {
                        "total_uploads": total_uploads,
                        "total_processed": total_processed,
                        "total_failed": total_failed,
                        "success_rate": success_rate,
                        "total_references_checked": total_references,
                        "total_verifications": total_verifications,
                        "average_processing_time": avg_processing_time,
                        "start_date": start_date,
                        "last_updated": updated_at if isinstance(updated_at, str) else str(updated_at)
                    },
                    "daily_stats": daily_stats,
                    "recent_uploads": recent_uploads if detailed else None
                }
        except Exception as e:
            print(f"❌ Database error in get_stats: {e}")
            return {
                "total_stats": {
                    "total_uploads": 0,
                    "total_processed": 0,
                    "total_failed": 0,
                    "success_rate": 0,
                    "total_references_checked": 0,
                    "total_verifications": 0,
                    "average_processing_time": 0,
                    "start_date": datetime.now().isoformat(),
                    "last_updated": datetime.now().isoformat()
                }
            }
    
    def clear_stats(self, keep_last_days: int = 30):
        """Clear old stats"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cutoff_date = (datetime.now() - timedelta(days=keep_last_days)).strftime("%Y-%m-%d")
                
                # Delete old uploads
                cursor.execute("DELETE FROM uploads WHERE date(timestamp) < ?", (cutoff_date,))
                
                # Delete old daily stats
                cursor.execute("DELETE FROM daily_stats WHERE date < ?", (cutoff_date,))
                
                # Recalculate totals
                cursor.execute("""
                    UPDATE stats 
                    SET total_uploads = (SELECT COUNT(*) FROM uploads),
                        total_processed = (SELECT COUNT(*) FROM uploads WHERE success = 1),
                        total_failed = (SELECT COUNT(*) FROM uploads WHERE success = 0),
                        total_references_checked = (SELECT COALESCE(SUM(references_count), 0) FROM uploads),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """)
                
                conn.commit()
                print(f"✅ Cleared stats older than {keep_last_days} days")
        except Exception as e:
            print(f"❌ Database error in clear_stats: {e}")

# Initialize stats tracker
stats_tracker = SQLiteStats()
print(f"✅ Using SQLite database for persistent statistics")

# ===============================
# COUNTER SETUP
# ===============================

def increment_counter():
    """Increment counter - now handled by SQLite"""
    try:
        # Just get current total from database
        stats = stats_tracker.get_stats(detailed=False)
        return stats['total_stats']['total_uploads']
    except Exception as e:
        print(f"⚠️ Error getting counter: {e}")
        return 0

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
    stats = stats_tracker.get_stats(detailed=False)
    print(f"📈 Stats tracker loaded: {stats['total_stats']['total_uploads']} total uploads")
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
    """Build mapping from references to in-text citations - INCLUDES uncited references"""
    mapping = {}
    
    # Get all references from the result
    all_references = result.get("references_raw", [])
    reconciliation_rows = result.get("reconciliation_intext_to_reference", [])
    
    # First, build citation counts for matched references
    citation_counter = defaultdict(int)
    citation_samples = defaultdict(list)
    seen_samples = defaultdict(set)
    
    for r in reconciliation_rows:
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
    
    # Create mapping for ALL references (including uncited ones)
    for ref in all_references:
        if ref and ref.strip():
            mapping[ref] = {
                "reference": ref,
                "times_cited": citation_counter.get(ref, 0),
                "cited_by": citation_samples.get(ref, [])
            }
    
    # Also include any matched references that might not be in all_references
    for ref in citation_counter:
        if ref not in mapping:
            mapping[ref] = {
                "reference": ref,
                "times_cited": citation_counter[ref],
                "cited_by": citation_samples.get(ref, [])
            }
    
    # Sort by times_cited (descending), with uncited references at the end
    result_list = list(mapping.values())
    result_list.sort(key=lambda x: (x["times_cited"] == 0, -x["times_cited"]))
    
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
                "state": "idle",
                "progress": 0,
                "total": 0,
                "percentage": 0,
                "started_at": None,
                "completed_at": None,
                "results": None,
                "verification_job_id": None,
                "summary": None,
                "results_count": 0
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
    """Background thread to sync progress and results - NO TIME LIMIT"""
    def sync():
        print(f"[DEBUG] ========================================")
        print(f"[DEBUG] Sync thread started for job {job_id}")
        print(f"[DEBUG] Verification job ID: {verification_job_id}")
        print(f"[DEBUG] ========================================")
        
        last_progress = -1
        no_progress_count = 0
        max_no_progress = 300  # 10 minutes without progress (300 * 2 seconds = 600 seconds)
        
        while True:
            try:
                status = get_verification_status(verification_job_id)
                
                if status:
                    current_progress = status.get("progress", 0)
                    total = status.get("total", 0)
                    
                    # Check for stalled progress (no movement for a long time)
                    if current_progress == last_progress:
                        no_progress_count += 1
                        if no_progress_count > max_no_progress and current_progress < total:
                            print(f"[DEBUG] WARNING: No progress for {no_progress_count * 2} seconds. Job may be stalled but continuing...")
                    else:
                        if no_progress_count > 0:
                            print(f"[DEBUG] Progress resumed after {no_progress_count * 2} seconds")
                        no_progress_count = 0
                        last_progress = current_progress
                    
                    print(f"[DEBUG] Sync status: {status.get('status')} - Progress: {current_progress}/{total} ({status.get('percentage')}%)")
                    
                    with _lock:
                        if job_id in _store:
                            # Update progress
                            _store[job_id]["verification"]["progress"] = current_progress
                            _store[job_id]["verification"]["percentage"] = status.get("percentage", 0)
                            _store[job_id]["verification"]["state"] = status.get("status", "running")
                            _store[job_id]["verification"]["total"] = total
                            
                            # When complete, get the actual results
                            if status.get("status") == "completed":
                                print(f"[DEBUG] Job {verification_job_id} marked as completed!")
                                print(f"[DEBUG] Attempting to retrieve verification results...")
                                
                                # Try multiple times to get results
                                verification_results = None
                                max_attempts = 20  # More attempts for large jobs
                                for attempt in range(max_attempts):
                                    verification_results = get_verification_results(verification_job_id)
                                    if verification_results:
                                        print(f"[DEBUG] Retrieved {len(verification_results)} results on attempt {attempt + 1}")
                                        break
                                    if attempt < max_attempts - 1:
                                        if attempt % 5 == 0:
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
                
            except Exception as e:
                print(f"[DEBUG] Error in sync thread: {e}")
                import traceback
                traceback.print_exc()
            
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


@app.get("/debug/job-progress/{job_id}")
async def job_progress(job_id: str):
    """Check progress of a specific job with time estimation"""
    job = get_job(job_id)
    if not job:
        return {"error": "Job not found"}
    
    verification = job.get("verification", {})
    
    # Calculate elapsed time
    elapsed_seconds = 0
    if verification.get("started_at"):
        started = datetime.fromisoformat(verification["started_at"])
        elapsed_seconds = (datetime.utcnow() - started).total_seconds()
    
    # Estimate remaining time
    progress = verification.get("progress", 0)
    total = verification.get("total", 0)
    estimated_remaining = 0
    if progress > 0 and elapsed_seconds > 0:
        rate = progress / elapsed_seconds
        estimated_remaining = (total - progress) / rate if rate > 0 else 0
    
    return {
        "job_id": job_id,
        "state": verification.get("state"),
        "progress": progress,
        "total": total,
        "percentage": verification.get("percentage"),
        "started_at": verification.get("started_at"),
        "completed_at": verification.get("completed_at"),
        "elapsed_seconds": round(elapsed_seconds, 1),
        "elapsed_formatted": format_time(elapsed_seconds),
        "estimated_remaining_seconds": round(estimated_remaining, 1),
        "estimated_remaining_formatted": format_time(estimated_remaining),
        "has_results": verification.get("results_count", 0) > 0,
        "verification_job_id": verification.get("verification_job_id")
    }


def format_time(seconds):
    """Format seconds into human readable time"""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


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
    
    from verify import verify_references_batch, get_verification_results
    
    try:
        temp_job_id = uuid.uuid4().hex
        results = verify_references_batch(refs, style="apa", job_id=temp_job_id)
        
        stored_results = get_verification_results(temp_job_id)
        final_results = stored_results if stored_results else results
        
        if final_results:
            summary = _compute_verification_summary(final_results)
            
            with _lock:
                if job_id in _store:
                    _store[job_id]["result"]["online_verification"] = {
                        "rows": final_results,
                        "summary": summary
                    }
                    
                    try:
                        _store[job_id]["result"]["acii"] = compute_acii(_store[job_id]["result"], final_results)
                    except Exception as e:
                        _store[job_id]["result"]["acii"] = {"error": str(e)}
                    
                    _store[job_id]["result"]["reconciliation_reference_to_intext"] = build_reference_to_intext(_store[job_id]["result"])
                    
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
    
    # STEP 1: FILE TYPE VALIDATION - REJECT PDF FILES
    if file.filename and file.filename.lower().endswith('.pdf'):
        return JSONResponse(
            status_code=400,
            content={
                "error": "PDF files are not supported",
                "message": "Please convert PDF to DOCX first: Open blank Word → File → Open → Select PDF → Click OK → Save as .docx",
                "instruction": "DOCX is the recommended format. Convert your PDF to Word before uploading."
            }
        )
    
    # Check if file is DOCX - if not, reject
    if not (file.filename and file.filename.lower().endswith('.docx')):
        return JSONResponse(
            status_code=400,
            content={
                "error": "Invalid file format",
                "message": "Only DOCX files are accepted. Please convert your PDF to DOCX format.",
                "instruction": "Open blank Word → File → Open → Select PDF → Click OK → Save as .docx"
            }
        )
    
    # STEP 2: CHECK SERVER LOAD
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
    
    # STEP 3: PROCESS THE FILE
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
            error=str(e),
            ip_address=request.client.host if request and request.client else None
        )
        raise


# ============================================================
# ONLINE VERIFICATION
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
    
    print(f"[DEBUG] ========================================")
    print(f"[DEBUG] Starting verification for job {job_id} with {len(refs)} references")
    print(f"[DEBUG] Estimated time: ~{len(refs) * 4} seconds ({len(refs) * 4 / 60:.1f} minutes)")
    print(f"[DEBUG] ========================================")
    
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
        "estimated_time_seconds": len(refs) * 4,
        "estimated_time_formatted": format_time(len(refs) * 4),
        "message": "Verification started. Check /online/status for progress."
    }


# ============================================================
# STATUS POLLING
# ============================================================

@app.get("/online/status")
def online_status(job_id: str):
    """Get verification status - single source of truth"""
    job = get_job(job_id)

    if not job:
        raise HTTPException(404, "Job not found")
    
    verification = job["verification"]
    result = job.get("result", {})
    
    # Calculate elapsed and estimated remaining time
    elapsed_seconds = 0
    remaining_seconds = None
    
    if verification.get("started_at") and verification["state"] == "running":
        started = datetime.fromisoformat(verification["started_at"])
        elapsed_seconds = (datetime.utcnow() - started).total_seconds()
        
        progress = verification.get("progress", 0)
        total = verification.get("total", 0)
        if progress > 0 and elapsed_seconds > 0:
            rate = progress / elapsed_seconds
            remaining_seconds = (total - progress) / rate if rate > 0 else 0
    
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
            "results_count": verification.get("results_count", 0),
            "elapsed_seconds": round(elapsed_seconds, 1),
            "elapsed_formatted": format_time(elapsed_seconds),
            "remaining_seconds": round(remaining_seconds, 1) if remaining_seconds else None,
            "remaining_formatted": format_time(remaining_seconds) if remaining_seconds else None
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
        time_msg = f"Processing: {verification['progress']}/{verification['total']} ({verification['percentage']}%)"
        if remaining_seconds:
            time_msg += f" - Est. remaining: {format_time(remaining_seconds)}"
        response["progress"] = {
            "current": verification["progress"],
            "total": verification["total"],
            "percentage": verification["percentage"],
            "status": verification["state"],
            "message": time_msg,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "remaining_seconds": round(remaining_seconds, 1) if remaining_seconds else None
        }
    
    # Add queue status
    response["queue"] = get_queue_status()
    
    return response


# ============================================================
# STATISTICS WEB PAGE
# ============================================================

@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    """Statistics dashboard page"""
    return templates.TemplateResponse(
        "stats.html",
        {"request": request}
    )


# ============================================================
# PRIVATE STATS ENDPOINTS
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
    """Simple manuscript count (protected endpoint)"""
    
    authenticate(credentials)
    
    stats = stats_tracker.get_stats(detailed=False)
    return {"manuscripts_checked": stats['total_stats']['total_uploads']}


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
# DEBUG STATS ENDPOINT
# ============================================================

@app.get("/debug/stats-info")
def debug_stats_info(credentials: HTTPBasicCredentials = Depends(security)):
    """Debug endpoint to check stats"""
    authenticate(credentials)
    
    stats = stats_tracker.get_stats(detailed=True)
    
    return {
        "storage": "sqlite",
        "database_path": DB_PATH,
        "database_exists": os.path.exists(DB_PATH),
        "database_size": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
        "stats": stats
    }


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
