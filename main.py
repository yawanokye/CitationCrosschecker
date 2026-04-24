# main.py — Citation Crosschecker with Async Queue System

import io
import os
import re
import uuid
import threading
import time
import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

# Database libraries
import psycopg2
from psycopg2.extras import RealDictCursor

# Queue libraries
import redis
from rq import Queue

# FastAPI and web frameworks
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from engine import run_crosscheck, run_crosscheck_with_autofix, recover_references_for_verification

# Your custom modules
from engine import run_crosscheck, run_crosscheck_with_autofix
from verify import (
    submit_verification,
    get_verification_status,
    get_queue_status,
    is_server_busy,
    get_verification_results,
    clear_verification_results
)
from acii import compute_acii
from citation_suggester import extract_context, suggest_from_context
from claim_checker import build_claim_support_rows
from reference_formatter import (
    format_verified_reference_list,
    export_references_to_docx,
    export_references_to_html,
    DOCX_AVAILABLE
)


# ===============================
# DATABASE SETUP - PostgreSQL (with SQLite fallback)
# ===============================

# Get database URL from environment (Render sets this)
DATABASE_URL = os.environ.get("DATABASE_URL")

# Get Redis URL from environment (Render sets this)
REDIS_URL = os.environ.get("REDIS_URL")

# Initialize Redis connection and task queue
redis_conn = None
task_queue = None
if REDIS_URL:
    try:
        redis_conn = redis.from_url(REDIS_URL)

        task_queue = Queue("document_processing", connection=redis_conn)
        verification_queue = Queue("verification", connection=redis_conn)

        print("✅ Redis connected and both queues initialized")

    except Exception as e:
        print(f"⚠️ Failed to connect to Redis: {e}")
else:
    print("⚠️ REDIS_URL not set - queue disabled")


class StatsTracker:
    """Stats tracker that works with PostgreSQL (preferred) or SQLite (fallback)"""
    
    def __init__(self):
        self.db_type = "postgresql" if DATABASE_URL else "sqlite"
        self.db_path = '/tmp/citation_stats.db'
        self._lock = threading.Lock()
        
        print(f"📁 Using {self.db_type.upper()} database")
        
        if self.db_type == "postgresql":
            self._init_postgresql()
        else:
            self._init_sqlite()
    
    def _get_postgres_connection(self):
        """Get PostgreSQL connection"""
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    
    def _init_postgresql(self):
        """Initialize PostgreSQL tables"""
        try:
            with self._get_postgres_connection() as conn:
                with conn.cursor() as cursor:
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
                    
                    # Create uploads table
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS uploads (
                            id SERIAL PRIMARY KEY,
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
                            date DATE PRIMARY KEY,
                            uploads INTEGER DEFAULT 0,
                            processed INTEGER DEFAULT 0,
                            failed INTEGER DEFAULT 0,
                            references_count INTEGER DEFAULT 0,
                            total_processing_time REAL DEFAULT 0,
                            processing_count INTEGER DEFAULT 0
                        )
                    """)
                    
                    # Create jobs table for queue
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS jobs (
                            job_id TEXT PRIMARY KEY,
                            status TEXT,
                            queue_position INTEGER,
                            worker_id TEXT,
                            file_name TEXT,
                            file_size_mb REAL,
                            result JSONB,
                            error TEXT,
                            processing_time REAL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            started_at TIMESTAMP,
                            completed_at TIMESTAMP
                        )
                    """)
                    
                    # Create indexes for jobs table
                    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
                    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
                    
                    # Insert initial stats if not exists
                    cursor.execute("""
                        INSERT INTO stats (id, total_uploads, total_processed, total_failed, total_references_checked)
                        VALUES (1, 0, 0, 0, 0)
                        ON CONFLICT (id) DO NOTHING
                    """)
                    
                    conn.commit()
                    print("✅ PostgreSQL database initialized")
                    
                    # Get existing stats
                    cursor.execute("SELECT total_uploads FROM stats WHERE id = 1")
                    row = cursor.fetchone()
                    if row:
                        print(f"📊 Existing stats: {row['total_uploads']} total uploads")
                        
        except Exception as e:
            print(f"❌ Failed to initialize PostgreSQL: {e}")
            print("⚠️ Falling back to SQLite")
            self.db_type = "sqlite"
            self._init_sqlite()
    
    def _init_sqlite(self):
        """Initialize SQLite tables (fallback)"""
        import sqlite3
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
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
                
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        status TEXT,
                        queue_position INTEGER,
                        worker_id TEXT,
                        file_name TEXT,
                        file_size_mb REAL,
                        result TEXT,
                        error TEXT,
                        processing_time REAL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        started_at TIMESTAMP,
                        completed_at TIMESTAMP
                    )
                """)
                
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
                
                cursor.execute("""
                    INSERT OR IGNORE INTO stats (id, total_uploads, total_processed, total_failed, total_references_checked)
                    VALUES (1, 0, 0, 0, 0)
                """)
                
                conn.commit()
                print("✅ SQLite database initialized")
                
                cursor.execute("SELECT total_uploads FROM stats WHERE id = 1")
                row = cursor.fetchone()
                if row:
                    print(f"📊 Existing stats: {row[0]} total uploads")
                    
        except Exception as e:
            print(f"❌ Failed to initialize SQLite: {e}")
    
    def add_upload(self, filename: str, file_size: int, references_count: int, 
                   processing_time: float = None, success: bool = True, 
                   ip_address: str = None, error: str = None):
        """Record an upload in the database"""
        try:
            if self.db_type == "postgresql":
                with self._get_postgres_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("""
                            INSERT INTO uploads 
                            (filename, file_size, references_count, processing_time, success, ip_address, error)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (filename, file_size, references_count, processing_time, 
                              1 if success else 0, ip_address, error))
                        
                        if success:
                            cursor.execute("""
                                UPDATE stats 
                                SET total_uploads = total_uploads + 1,
                                    total_processed = total_processed + 1,
                                    total_references_checked = total_references_checked + %s,
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
                        
                        today = datetime.now().date()
                        cursor.execute("""
                            INSERT INTO daily_stats (date, uploads, processed, failed, references_count)
                            VALUES (%s, 1, %s, %s, %s)
                            ON CONFLICT (date) DO UPDATE SET
                                uploads = daily_stats.uploads + 1,
                                processed = daily_stats.processed + %s,
                                failed = daily_stats.failed + %s,
                                references_count = daily_stats.references_count + %s
                        """, (today, 1 if success else 0, 0 if success else 1, 
                              references_count if success else 0,
                              1 if success else 0, 0 if success else 1, 
                              references_count if success else 0))
                        
                        if processing_time and success:
                            cursor.execute("""
                                UPDATE daily_stats 
                                SET total_processing_time = total_processing_time + %s,
                                    processing_count = processing_count + 1
                                WHERE date = %s
                            """, (processing_time, today))
                        
                        conn.commit()
                        
                        cursor.execute("SELECT total_uploads FROM stats WHERE id = 1")
                        row = cursor.fetchone()
                        total = row['total_uploads'] if row else 0
                        print(f"📊 Recorded: {filename} - {references_count} refs (Total: {total})")
            else:
                self._add_upload_sqlite(filename, file_size, references_count, 
                                         processing_time, success, ip_address, error)
            return True
        except Exception as e:
            print(f"❌ Database error in add_upload: {e}")
            return False
    
    def _add_upload_sqlite(self, filename, file_size, references_count, 
                           processing_time, success, ip_address, error):
        """SQLite version of add_upload"""
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO uploads 
                (filename, file_size, references_count, processing_time, success, ip_address, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (filename, file_size, references_count, processing_time, 
                  1 if success else 0, ip_address, error))
            
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
            
            if processing_time and success:
                cursor.execute("""
                    UPDATE daily_stats 
                    SET total_processing_time = total_processing_time + ?,
                        processing_count = processing_count + 1
                    WHERE date = ?
                """, (processing_time, today))
            
            conn.commit()
    
    def add_verification(self, job_id: str, references_count: int, success: bool = True):
        """Record a verification event"""
        try:
            if self.db_type == "postgresql":
                with self._get_postgres_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("""
                            UPDATE stats 
                            SET total_verifications = total_verifications + 1,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = 1
                        """)
                        conn.commit()
            else:
                import sqlite3
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE stats 
                        SET total_verifications = total_verifications + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = 1
                    """)
                    conn.commit()
            print(f"📊 Recorded verification for job {job_id}")
            return True
        except Exception as e:
            print(f"❌ Database error in add_verification: {e}")
            return False
    
    def get_stats(self, detailed: bool = False, days: int = 30):
        """Get statistics from database"""
        try:
            if self.db_type == "postgresql":
                with self._get_postgres_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT * FROM stats WHERE id = 1")
                        row = cursor.fetchone()
                        
                        if row:
                            total_uploads = row['total_uploads']
                            total_processed = row['total_processed']
                            total_failed = row['total_failed']
                            total_verifications = row['total_verifications']
                            total_references = row['total_references_checked']
                            updated_at = row['updated_at']
                        else:
                            total_uploads = total_processed = total_failed = total_verifications = total_references = 0
                            updated_at = datetime.now()
                        
                        success_rate = round((total_processed / max(total_uploads, 1)) * 100, 2)
                        
                        cursor.execute("""
                            SELECT AVG(processing_time) 
                            FROM uploads 
                            WHERE success = 1 AND processing_time IS NOT NULL
                        """)
                        avg_row = cursor.fetchone()
                        avg_processing_time = round(avg_row[0], 2) if avg_row and avg_row[0] else 0
                        
                        return {
                            "total_stats": {
                                "total_uploads": total_uploads,
                                "total_processed": total_processed,
                                "total_failed": total_failed,
                                "success_rate": success_rate,
                                "total_references_checked": total_references,
                                "total_verifications": total_verifications,
                                "average_processing_time": avg_processing_time,
                                "start_date": datetime.now().isoformat(),
                                "last_updated": str(updated_at)
                            }
                        }
            else:
                import sqlite3
                with sqlite3.connect(self.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    
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
                    
                    cursor.execute("""
                        SELECT AVG(processing_time) 
                        FROM uploads 
                        WHERE success = 1 AND processing_time IS NOT NULL
                    """)
                    avg_row = cursor.fetchone()
                    avg_processing_time = round(avg_row[0], 2) if avg_row and avg_row[0] else 0
                    
                    return {
                        "total_stats": {
                            "total_uploads": total_uploads,
                            "total_processed": total_processed,
                            "total_failed": total_failed,
                            "success_rate": success_rate,
                            "total_references_checked": total_references,
                            "total_verifications": total_verifications,
                            "average_processing_time": avg_processing_time,
                            "start_date": datetime.now().isoformat(),
                            "last_updated": str(updated_at)
                        }
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
        """Clear statistics older than keep_last_days"""
        try:
            if self.db_type == "postgresql":
                with self._get_postgres_connection() as conn:
                    with conn.cursor() as cursor:
                        cutoff_date = datetime.now().date() - timedelta(days=keep_last_days)
                        
                        cursor.execute("DELETE FROM uploads WHERE date(timestamp) < %s", (cutoff_date,))
                        cursor.execute("DELETE FROM daily_stats WHERE date < %s", (cutoff_date,))
                        
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
            else:
                import sqlite3
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    cutoff_date = (datetime.now() - timedelta(days=keep_last_days)).strftime("%Y-%m-%d")
                    
                    cursor.execute("DELETE FROM uploads WHERE date(timestamp) < ?", (cutoff_date,))
                    cursor.execute("DELETE FROM daily_stats WHERE date < ?", (cutoff_date,))
                    
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


# Create the stats tracker instance
stats_tracker = StatsTracker()
print(f"✅ Using {stats_tracker.db_type.upper()} database for persistent statistics")


# ===============================
# COUNTER SETUP
# ===============================

def increment_counter():
    try:
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
PASSWORD = "Ano77kye7509#"

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


# ===============================
# LIFESPAN MANAGER
# ===============================

@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    print("🚀 Starting Citation Crosschecker...")
    stats = stats_tracker.get_stats(detailed=False)
    print(f"📈 Stats tracker loaded: {stats['total_stats']['total_uploads']} total uploads")
    yield
    print("👋 Shutting down...")

app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan
)


# --- GLOBAL PROTECTION CONTROLS ---
processing = False

BLOCKED_PATHS = [
    "/wp-admin",
    "/wordpress",
    "/wp-login",
    "/xmlrpc.php",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/debug"
]

BAD_AGENTS = [
    "bot", "crawler", "scanner", "spider",
    "curl", "wget", "python-requests",
    "httpclient", "scrapy", "libwww"
]

# =========================
# SECURITY MIDDLEWARE (1st)
# =========================
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path.lower()
    ua = request.headers.get("user-agent", "").lower()
    
    # ✅ ALLOW LIST - Critical endpoints that must work
    ALLOWED_PATHS = [
        "/",
        "/verify",
        "/result",
        "/online/status",
        "/verify-online",
        "/stats",
        "/health",
        "/privacy",
        "/static",
        "/export-fixed-document",
        "/export-references",
        "/autofix-suggestions",
        "/fix-log",
        "/apply-autofix",
        "/queue/status",
        "/private-stats"
    ]
    
    # Check if path is allowed (exact match or starts with allowed path)
    for allowed in ALLOWED_PATHS:
        if path == allowed or path.startswith(allowed + "/"):
            return await call_next(request)
    
    # 🔒 Block sensitive endpoints
    for blocked in BLOCKED_PATHS:
        if path.startswith(blocked):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
    
    # 🤖 Block bots (commented out but keeping structure)
    # if any(b in ua for b in BAD_AGENTS):
    #     return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    
    return await call_next(request)

# =========================
# REDIRECT MIDDLEWARE (2nd)
# =========================
@app.middleware("http")
async def redirect_with_message(request: Request, call_next):
    host = request.headers.get("host", "")
    path = request.url.path
    
    # Skip redirect for API endpoints
    if path.startswith("/online/") or path.startswith("/verify") or path.startswith("/private-stats") or path.startswith("/result"):
        return await call_next(request)
    
    if "citationcrosschecker.onrender.com" in host:
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Moved Permanently</title>
            <meta http-equiv="refresh" content="2;url=https://citeintegrity.org{request.url.path}">
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    text-align: center;
                    padding-top: 80px;
                    background: #f9f9f9;
                }}
                .box {{
                    background: white;
                    padding: 30px;
                    border-radius: 10px;
                    display: inline-block;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }}
                a {{
                    color: #6c2bd9;
                    text-decoration: none;
                    font-weight: bold;
                }}
            </style>
        </head>
        <body>
            <div class="box">
                <h2>Moved Permanently</h2>
                <p>This service is now available at:</p>
                <p><a href="https://citeintegrity.org">citeintegrity.org</a></p>
                <p>You will be redirected automatically...</p>
            </div>
        </body>
        </html>
        """, status_code=301)
    
    return await call_next(request)

from fastapi import Request
from fastapi.responses import RedirectResponse

@app.middleware("http")
async def force_single_domain(request: Request, call_next):
    host = request.headers.get("host", "")

    if "citationcrosschecker-1.onrender.com" in host:
        return RedirectResponse(
            url=f"https://citationcrosschecker.onrender.com{request.url.path}",
            status_code=301
        )

    return await call_next(request)
# =========================
# SECURITY HEADERS (3rd)
# =========================
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    
    # 🔐 HSTS (force HTTPS)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    
    # 🛡️ Clickjacking protection
    response.headers["X-Frame-Options"] = "DENY"
    
    # 🛡️ MIME sniffing protection
    response.headers["X-Content-Type-Options"] = "nosniff"
    
    # 🛡️ XSS protection (legacy browsers)
    response.headers["X-XSS-Protection"] = "1; mode=block"
    
    # 🛡️ Referrer policy
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    
    # 🛡️ Content Security Policy (safe default)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    
    # 🛡️ Permissions policy
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), payment=()"
    )
    
    # 🛡️ Prevent caching of sensitive responses
    response.headers["Cache-Control"] = "no-store"
    
    return response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

templates_dir = os.path.join(BASE_DIR, "templates")
if not os.path.exists(templates_dir):
    os.makedirs(templates_dir)

templates = Jinja2Templates(directory=templates_dir)

static_dir = os.path.join(BASE_DIR, "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

_store: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()

# --------------------------------------------------
# Utility Functions
# --------------------------------------------------

def now():
    return datetime.utcnow().isoformat()

def format_time(seconds):
    """Format seconds into human readable time"""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"

def _norm_text_citation(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r'[^a-z0-9]', '', s)
    return s.strip()

def build_reference_to_intext(result):
    mapping = {}
    
    all_references = result.get("references_raw", [])
    reconciliation_rows = result.get("reconciliation_intext_to_reference", [])
    
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
    
    for ref in all_references:
        if ref and ref.strip():
            mapping[ref] = {
                "reference": ref,
                "times_cited": citation_counter.get(ref, 0),
                "cited_by": citation_samples.get(ref, [])
            }
    
    for ref in citation_counter:
        if ref not in mapping:
            mapping[ref] = {
                "reference": ref,
                "times_cited": citation_counter[ref],
                "cited_by": citation_samples.get(ref, [])
            }
    
    result_list = list(mapping.values())
    result_list.sort(key=lambda x: (x["times_cited"] == 0, -x["times_cited"]))
    
    return result_list

def _compute_verification_summary(rows: List[Dict[str, Any]]) -> Dict[str, int]:
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

def build_context_specific_recovery(result: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "missing_recovery": [],
        "verification_recovery": []
    }

    full_text = result.get("main_text", "") or result.get("full_text", "")

    # Top section: Missing citation recovery
    missing_items = result.get("missing_in_references", []) or []
    missing_suggestions = result.get("missing_citation_suggestions", {}) or {}

    for item in missing_items:
        if isinstance(item, dict):
            citation_text = item.get("citation_in_text", "") or item.get("citation", "")
            count = item.get("count", 1)
        else:
            citation_text = str(item)
            count = 1

        suggestions = missing_suggestions.get(citation_text, [])

        if citation_text:
            payload["missing_recovery"].append({
                "citation": citation_text,
                "count": count,
                "suggestions": suggestions,
                "message": "" if suggestions else "No evidence found."
            })

    # Bottom section: needs_review / not_found
    verify_rows = (result.get("online_verification") or {}).get("rows", []) or []
    c2r_rows = result.get("reconciliation_intext_to_reference", []) or []

    # Build lookup: matched reference -> in-text citation
    ref_to_citation = {}
    for r in c2r_rows:
        matched_ref = r.get("matched_reference", "") or ""
        in_text = r.get("in_text", "") or r.get("citation", "") or r.get("citation_in_text", "") or ""
        if matched_ref and in_text and matched_ref not in ref_to_citation:
            ref_to_citation[matched_ref] = in_text

    for row in verify_rows:
        status = row.get("status", "")
        if status not in {"needs_review", "not_found"}:
            continue

        original_ref = row.get("reference", "") or ""
        matched_title = row.get("matched_title", "") or ""

        citation_text = ref_to_citation.get(original_ref, "") or ref_to_citation.get(matched_title, "")

        suggestions = []
        if citation_text and full_text:
            context = extract_context(full_text, citation_text, window=200)
            if context:
                suggestions = suggest_from_context(
                    context=context,
                    citation=citation_text,
                    top_k=3
                )

        payload["verification_recovery"].append({
            "reference": original_ref,
            "status": status,
            "citation": citation_text,
            "suggestions": suggestions,
            "message": "" if suggestions else "No evidence found."
        })

    return payload
def load_job_record(job_id: str) -> Optional[Dict[str, Any]]:
    # prefer in-memory because it contains verification state
    with _lock:
        if job_id in _store:
            return _store[job_id]

    # fallback to PostgreSQL
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status, result, error FROM jobs WHERE job_id = %s",
                (job_id,)
            )
            row = cursor.fetchone()
            cursor.close()
            conn.close()

            if not row:
                return None

            result = row["result"]
            if isinstance(result, str):
                result = json.loads(result)

            # Check if there's a verification job ID in the result
            verification_job_id = result.get("verification_job_id")
            verification_state = "idle"
            verification_progress = 0
            verification_total = 0
            verification_percentage = 0
            
            # If verification_job_id exists, get its status from verify.py
            if verification_job_id:
                try:
                    from verify import get_verification_status
                    verify_status = get_verification_status(verification_job_id)
                    if verify_status:
                        verification_state = verify_status.get("status", "idle")
                        verification_progress = verify_status.get("progress", 0)
                        verification_total = verify_status.get("total", 0)
                        verification_percentage = verify_status.get("percentage", 0)
                        print(f"[DEBUG] Loaded verification state for {verification_job_id}: {verification_progress}/{verification_total}")
                except Exception as e:
                    print(f"[DEBUG] Could not get verification status: {e}")
            
            # Check if online verification results exist
            online_verification = result.get("online_verification", {})
            if online_verification.get("rows"):
                verification_state = "completed"
                verification_total = len(online_verification.get("rows", []))
                verification_progress = verification_total
                verification_percentage = 100
            
            # Build the job record
            job_record = {
                "job_id": job_id,
                "status": row["status"],
                "result": result or {},
                "error": row["error"],
                "verification": {
                    "state": verification_state,
                    "progress": verification_progress,
                    "total": verification_total,
                    "percentage": verification_percentage,
                    "message": "",
                    "verification_job_id": verification_job_id,
                    "started_at": result.get("verification_started_at"),
                    "completed_at": result.get("verification_completed_at"),
                    "summary": online_verification.get("summary", {}),
                    "results_count": len(online_verification.get("rows", []))
                }
            }
            
            # Store in memory for future requests
            with _lock:
                _store[job_id] = job_record
            
            print(f"[DEBUG] Loaded job {job_id} from PostgreSQL with verification state: {verification_state} ({verification_progress}/{verification_total})")
            
            return job_record
            
        except Exception as e:
            print(f"load_job_record DB error: {e}")
            import traceback
            traceback.print_exc()

    return None

def store_result(result):
    job_id = uuid.uuid4().hex

    with _lock:
        _store[job_id] = {
            "result": result,
            "autofix_applied": False,
            "fixed_document": None,
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
    with _lock:
        return _store.get(job_id)

def update_verification_status(job_id: str, **kwargs):
    with _lock:
        if job_id in _store:
            _store[job_id]["verification"].update(kwargs)

def start_progress_sync(job_id: str, verification_job_id: str):
    def sync():
        print(f"[DEBUG] Sync thread started for job {job_id}, verification_job_id={verification_job_id}")
        
        last_progress = -1
        no_progress_count = 0
        max_no_progress = 300
        last_log_time = time.time()
        
        while True:
            try:
                # Get status from verify.py's job tracking
                status = get_verification_status(verification_job_id)
                
                # Debug log every 10 seconds
                if time.time() - last_log_time > 10:
                    print(f"[DEBUG] Sync status for {verification_job_id}: {status}")
                    last_log_time = time.time()
                
                if status:
                    current_progress = status.get("progress", 0)
                    total = status.get("total", 0)
                    status_state = status.get("status", "processing")
                    
                    print(f"[DEBUG] Progress update: {current_progress}/{total} (state: {status_state})")
                    
                    if current_progress == last_progress:
                        no_progress_count += 1
                    else:
                        no_progress_count = 0
                        last_progress = current_progress
                    
                    with _lock:
                        if job_id in _store:
                            # Update verification state for frontend polling
                            _store[job_id]["verification"]["progress"] = current_progress
                            _store[job_id]["verification"]["percentage"] = status.get("percentage", 0)
                            _store[job_id]["verification"]["state"] = status_state
                            _store[job_id]["verification"]["total"] = total
                            
                            # Also update the message for frontend display
                            if total > 0:
                                _store[job_id]["verification"]["message"] = f"Verifying: {current_progress}/{total}"
                            else:
                                _store[job_id]["verification"]["message"] = "Starting verification..."
                            
                            print(f"[DEBUG] Updated _store for job {job_id}: progress={current_progress}, total={total}, state={status_state}")
                    
                    # Check for completion
                    if status_state == "completed":
                        print(f"[DEBUG] Verification job {verification_job_id} completed!")
                        verification_results = None
                        max_attempts = 20
                        for attempt in range(max_attempts):
                            verification_results = get_verification_results(verification_job_id)
                            if verification_results:
                                print(f"[DEBUG] Retrieved {len(verification_results)} results on attempt {attempt + 1}")
                                break
                            time.sleep(2)
                        
                        if verification_results:
                            summary = _compute_verification_summary(verification_results)
                            
                            with _lock:
                                if job_id in _store:
                                    _store[job_id]["result"]["online_verification"] = {
                                        "rows": verification_results,
                                        "summary": summary
                                    }
                                    
                                    try:
                                        _store[job_id]["result"]["acii"] = compute_acii(
                                            _store[job_id]["result"], 
                                            verification_results
                                        )
                                    except Exception as e:
                                        print(f"[DEBUG] ACII computation error: {e}")
                                    
                                    try:
                                        _store[job_id]["result"]["reconciliation_reference_to_intext"] = build_reference_to_intext(_store[job_id]["result"])
                                    except Exception as e:
                                        print(f"[DEBUG] Error rebuilding reference mapping: {e}")
                                    
                                    _store[job_id]["result"]["recovery"] = build_context_specific_recovery(_store[job_id]["result"])
                                    _store[job_id]["result"]["claim_support"] = build_claim_support_rows(_store[job_id]["result"])
                                    _store[job_id]["verification"]["results"] = verification_results
                                    _store[job_id]["verification"]["results_count"] = len(verification_results)
                                    _store[job_id]["verification"]["summary"] = summary
                                    _store[job_id]["verification"]["state"] = "completed"
                                    _store[job_id]["verification"]["completed_at"] = now()
                            
                            # 🔥 Store completion info in PostgreSQL
                            if DATABASE_URL:
                                try:
                                    conn = psycopg2.connect(DATABASE_URL)
                                    cursor = conn.cursor()
                                    cursor.execute("""
                                        UPDATE jobs
                                        SET result = result || jsonb_build_object(
                                            'verification_completed_at', %s,
                                            'online_verification', %s
                                        )
                                        WHERE job_id = %s
                                    """, (now(), json.dumps({"rows": verification_results, "summary": summary}), job_id))
                                    conn.commit()
                                    cursor.close()
                                    conn.close()
                                    print(f"[DEBUG] Stored verification completion in PostgreSQL for job {job_id}")
                                except Exception as e:
                                    print(f"[DEBUG] Could not persist verification completion: {e}")
                        else:
                            with _lock:
                                if job_id in _store:
                                    _store[job_id]["verification"]["state"] = "error"
                                    _store[job_id]["verification"]["message"] = "No results retrieved after completion"
                            
                            # 🔥 Store error state in PostgreSQL
                            if DATABASE_URL:
                                try:
                                    conn = psycopg2.connect(DATABASE_URL)
                                    cursor = conn.cursor()
                                    cursor.execute("""
                                        UPDATE jobs
                                        SET result = result || jsonb_build_object(
                                            'verification_error', %s,
                                            'verification_completed_at', %s
                                        )
                                        WHERE job_id = %s
                                    """, ("No results retrieved after completion", now(), job_id))
                                    conn.commit()
                                    cursor.close()
                                    conn.close()
                                except Exception as e:
                                    print(f"[DEBUG] Could not persist error state: {e}")
                        
                        break
                    
                    # Check for error
                    elif status_state == "error":
                        error_msg = status.get("error", "Unknown error")
                        with _lock:
                            if job_id in _store:
                                _store[job_id]["verification"]["state"] = "error"
                                _store[job_id]["verification"]["message"] = error_msg
                        
                        # 🔥 Store error state in PostgreSQL
                        if DATABASE_URL:
                            try:
                                conn = psycopg2.connect(DATABASE_URL)
                                cursor = conn.cursor()
                                cursor.execute("""
                                    UPDATE jobs
                                    SET result = result || jsonb_build_object(
                                        'verification_error', %s,
                                        'verification_completed_at', %s
                                    )
                                    WHERE job_id = %s
                                """, (error_msg, now(), job_id))
                                conn.commit()
                                cursor.close()
                                conn.close()
                            except Exception as e:
                                print(f"[DEBUG] Could not persist error state: {e}")
                        break
                    
                    # Check for stall (no progress for too long)
                    if no_progress_count > max_no_progress:
                        print(f"[DEBUG] No progress for {max_no_progress * 2} seconds, marking as error")
                        with _lock:
                            if job_id in _store:
                                _store[job_id]["verification"]["state"] = "error"
                                _store[job_id]["verification"]["message"] = "Verification stalled - no progress"
                        
                        # 🔥 Store stall error in PostgreSQL
                        if DATABASE_URL:
                            try:
                                conn = psycopg2.connect(DATABASE_URL)
                                cursor = conn.cursor()
                                cursor.execute("""
                                    UPDATE jobs
                                    SET result = result || jsonb_build_object(
                                        'verification_error', %s,
                                        'verification_completed_at', %s
                                    )
                                    WHERE job_id = %s
                                """, ("Verification stalled - no progress", now(), job_id))
                                conn.commit()
                                cursor.close()
                                conn.close()
                            except Exception as e:
                                print(f"[DEBUG] Could not persist stall error: {e}")
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
# DOCUMENT FIXING FUNCTIONS
# ============================================================

def apply_autofix_to_document(original_text: str, autofix_suggestions: Dict) -> str:
    """Apply auto-fix suggestions to document text"""
    if not autofix_suggestions or not autofix_suggestions.get("citations"):
        return original_text
    
    fixed_text = original_text
    
    citations_to_fix = sorted(
        autofix_suggestions.get("citations", []),
        key=lambda x: len(x.get("original", "")),
        reverse=True
    )
    
    for fix in citations_to_fix:
        original = fix.get("original", "")
        suggested = fix.get("suggested", "")
        if original and suggested and original != suggested:
            pattern = r'\b' + re.escape(original) + r'\b'
            new_text = re.sub(pattern, suggested, fixed_text)
            if new_text != fixed_text:
                fixed_text = new_text
    
    return fixed_text

def generate_fixed_document_content(job_data: Dict, autofix_suggestions: Dict) -> str:
    """Generate the fixed document content as a string"""
    result = job_data.get("result", {})
    original_text = result.get("main_text", "")
    
    if not original_text:
        original_text = result.get("data", {}).get("main_text", "")
    
    if not original_text:
        print("[DEBUG] No main_text found in result")
        return ""
    
    fixed_text = apply_autofix_to_document(original_text, autofix_suggestions)
    
    fix_log = []
    for fix in autofix_suggestions.get("citations", []):
        if fix.get("confidence", 0) >= 0.85:
            fix_log.append(f"[AUTO-FIXED] {fix.get('original')} -> {fix.get('suggested')} ({fix.get('type')})")
    
    if fix_log:
        header = "\n".join([
            "<!--",
            "CiteIntegrity Auto-Fix Log",
            f"Generated: {datetime.now().isoformat()}",
            "-" * 40,
        ] + fix_log + ["-->", ""])
        fixed_text = header + fixed_text
    
    return fixed_text

# ============================================================
# DEBUG ENDPOINTS
# ============================================================

@app.get("/debug/job/{job_id}")
async def debug_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return {"error": "Job not found"}
    
    verification = job.get("verification", {})
    result = job.get("result", {})
    
    return {
        "job_id": job_id,
        "autofix_applied": job.get("autofix_applied", False),
        "has_fixed_document": job.get("fixed_document") is not None,
        "has_main_text": "main_text" in result or ("data" in result and "main_text" in result.get("data", {})),
        "verification": {
            "state": verification.get("state"),
            "progress": verification.get("progress"),
            "total": verification.get("total"),
            "percentage": verification.get("percentage"),
        },
        "has_result": bool(result),
        "has_online_verification": "online_verification" in result,
        "references_count": len(result.get("references_raw", [])),
        "has_autofix_suggestions": "autofix" in result
    }

@app.get("/debug/autofix-data/{job_id}")
async def debug_autofix_data(job_id: str):
    # First check PostgreSQL
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            cursor = conn.cursor()
            cursor.execute("SELECT result FROM jobs WHERE job_id = %s", (job_id,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if row and row["result"]:
                result = row["result"]
                if isinstance(result, str):
                    result = json.loads(result)
                return {
                    "source": "postgresql",
                    "has_autofix": "autofix" in result,
                    "autofix_keys": list(result.get("autofix", {}).keys()) if "autofix" in result else [],
                    "has_main_text": "main_text" in result,
                    "main_text_length": len(result.get("main_text", "")),
                    "references_count": len(result.get("references_raw", []))
                }
        except Exception as e:
            print(f"PostgreSQL lookup error: {e}")
    
    # Fallback to in-memory
    job = get_job(job_id)
    if not job:
        return {"error": "Job not found"}
    
    result = job.get("result", {})
    return {
        "source": "memory",
        "has_autofix": "autofix" in result,
        "autofix_keys": list(result.get("autofix", {}).keys()) if "autofix" in result else [],
        "has_main_text": "main_text" in result,
        "main_text_length": len(result.get("main_text", "")),
        "references_count": len(result.get("references_raw", []))
    }

@app.get("/debug/job-progress/{job_id}")
async def job_progress(job_id: str):
    # First check PostgreSQL
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status, created_at, started_at, completed_at, processing_time, error 
                FROM jobs WHERE job_id = %s
            """, (job_id,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if row:
                elapsed_seconds = 0
                if row["started_at"]:
                    elapsed_seconds = (datetime.now() - row["started_at"]).total_seconds()
                
                return {
                    "job_id": job_id,
                    "source": "postgresql",
                    "state": row["status"],
                    "created_at": str(row["created_at"]) if row["created_at"] else None,
                    "started_at": str(row["started_at"]) if row["started_at"] else None,
                    "completed_at": str(row["completed_at"]) if row["completed_at"] else None,
                    "elapsed_seconds": round(elapsed_seconds, 1),
                    "elapsed_formatted": format_time(elapsed_seconds),
                    "processing_time": row["processing_time"],
                    "error": row["error"]
                }
        except Exception as e:
            print(f"PostgreSQL lookup error: {e}")
    
    # Fallback to in-memory
    job = get_job(job_id)
    if not job:
        return {"error": "Job not found"}
    
    verification = job.get("verification", {})
    
    elapsed_seconds = 0
    if verification.get("started_at"):
        started = datetime.fromisoformat(verification["started_at"])
        elapsed_seconds = (datetime.utcnow() - started).total_seconds()
    
    progress = verification.get("progress", 0)
    total = verification.get("total", 0)
    estimated_remaining = 0
    if progress > 0 and elapsed_seconds > 0:
        rate = progress / elapsed_seconds
        estimated_remaining = (total - progress) / rate if rate > 0 else 0
    
    return {
        "job_id": job_id,
        "source": "memory",
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
        "has_results": verification.get("results_count", 0) > 0
    }

@app.get("/debug/verify-status/{verification_job_id}")
async def debug_verify_status(verification_job_id: str):
    from verify import get_verification_status, get_verification_results
    
    status = get_verification_status(verification_job_id)
    results = get_verification_results(verification_job_id)
    
    return {
        "verification_job_id": verification_job_id,
        "status": status,
        "results_count": len(results) if results else 0,
        "has_results": results is not None,
        "sample_result": results[0] if results and len(results) > 0 else None
    }

@app.get("/debug/all-jobs")
async def debug_all_jobs():
    jobs_info = {}
    
    # Get jobs from PostgreSQL
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT job_id, status, file_name, created_at, completed_at FROM jobs ORDER BY created_at DESC LIMIT 50"
            )
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            
            for row in rows:
                jobs_info[row["job_id"]] = {
                    "source": "postgresql",
                    "status": row["status"],
                    "file_name": row["file_name"],
                    "created_at": str(row["created_at"]) if row["created_at"] else None,
                    "completed_at": str(row["completed_at"]) if row["completed_at"] else None
                }
        except Exception as e:
            print(f"PostgreSQL lookup error: {e}")
    
    # Also get in-memory jobs
    with _lock:
        for job_id, job_data in _store.items():
            if job_id not in jobs_info:
                jobs_info[job_id] = {
                    "source": "memory",
                    "verification_state": job_data.get("verification", {}).get("state"),
                    "verification_progress": job_data.get("verification", {}).get("progress"),
                    "has_results": bool(job_data.get("result")),
                    "autofix_applied": job_data.get("autofix_applied", False)
                }
    
    return {
        "total_jobs": len(jobs_info),
        "jobs": jobs_info
    }

@app.get("/debug/sync-status/{job_id}")
async def debug_sync_status(job_id: str):
    job = get_job(job_id)
    if not job:
        return {"error": "Job not found"}
    
    verification = job.get("verification", {})
    return {
        "job_id": job_id,
        "verification_job_id": verification.get("verification_job_id"),
        "state": verification.get("state"),
        "progress": verification.get("progress"),
        "total": verification.get("total"),
        "percentage": verification.get("percentage"),
        "started_at": verification.get("started_at"),
        "completed_at": verification.get("completed_at"),
        "sync_thread_running": verification.get("state") == "running" and verification.get("total", 0) > 0
    }
    
@app.post("/debug/retry-verification/{job_id}")
async def debug_retry_verification(job_id: str):
    refs = []
    
    # Try to get references from PostgreSQL first
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            cursor = conn.cursor()
            cursor.execute("SELECT result FROM jobs WHERE job_id = %s", (job_id,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if row and row["result"]:
                result = row["result"]
                if isinstance(result, str):
                    result = json.loads(result)
                refs = result.get("references_raw", [])
        except Exception as e:
            print(f"PostgreSQL lookup error: {e}")
    
    # Fallback to in-memory
    if not refs:
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
            
            # Update in-memory store
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
                    
                    _store[job_id]["verification"]["state"] = "completed"
                    _store[job_id]["verification"]["completed_at"] = now()
                    _store[job_id]["verification"]["results"] = final_results
                    _store[job_id]["verification"]["progress"] = len(refs)
                    _store[job_id]["verification"]["percentage"] = 100
                    _store[job_id]["verification"]["results_count"] = len(final_results)
                    _store[job_id]["verification"]["summary"] = summary
            
            # Also update PostgreSQL if possible
            if DATABASE_URL:
                try:
                    conn = psycopg2.connect(DATABASE_URL)
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE jobs 
                        SET result = result || jsonb_build_object('online_verification', %s::jsonb)
                        WHERE job_id = %s
                    """, (json.dumps({"rows": final_results, "summary": summary}), job_id))
                    conn.commit()
                    cursor.close()
                    conn.close()
                except Exception as db_error:
                    print(f"PostgreSQL update error: {db_error}")
            
            return {
                "success": True,
                "job_id": job_id,
                "summary": summary,
                "results_count": len(final_results),
                "message": f"Verification completed for {len(final_results)} references"
            }
        else:
            return {"error": "No results returned from verification"}
        
    except Exception as e:
        return {"error": str(e)}

@app.get("/debug/verification-data/{job_id}")
async def debug_verification_data(job_id: str):
    """Debug endpoint to check verification data structure"""
    job = get_job(job_id)
    if not job:
        return {"error": "Job not found"}
    
    result = job.get("result", {})
    online_verification = result.get("online_verification", {})
    rows = online_verification.get("rows", [])
    
    sample = []
    for i, row in enumerate(rows[:3]):
        sample.append({
            "index": i,
            "has_suggested_references": "suggested_references" in row,
            "suggested_references_count": len(row.get("suggested_references", [])),
            "status": row.get("status"),
            "reference_preview": row.get("reference", "")[:100] if row.get("reference") else ""
        })
    
    verification_job_id = job.get("verification", {}).get("verification_job_id")
    raw_verification_results = None
    if verification_job_id:
        raw_verification_results = get_verification_results(verification_job_id)
        if raw_verification_results and len(raw_verification_results) > 0:
            raw_sample = []
            for i, row in enumerate(raw_verification_results[:3]):
                raw_sample.append({
                    "index": i,
                    "has_suggested_references": "suggested_references" in row,
                    "suggested_references_count": len(row.get("suggested_references", [])),
                })
    
    return {
        "job_id": job_id,
        "verification_job_id": verification_job_id,
        "total_rows": len(rows),
        "sample": sample,
        "raw_verification_sample": raw_sample if verification_job_id else None,
        "full_first_row": rows[0] if rows else None
    }
@app.get("/debug/test-verification/{job_id}")
async def test_verification(job_id: str):
    """Test endpoint to check verification status"""
    job = get_job(job_id)
    if not job:
        return {"error": "Job not found"}
    
    verification = job.get("verification", {})
    result = job.get("result", {})
    
    # Get the verification job ID
    verification_job_id = verification.get("verification_job_id")
    
    # Get status from verify.py
    from verify import get_verification_status, get_verification_results
    
    verify_status = None
    if verification_job_id:
        verify_status = get_verification_status(verification_job_id)
    
    return {
        "job_id": job_id,
        "verification_job_id": verification_job_id,
        "frontend_state": verification.get("state"),
        "frontend_progress": verification.get("progress"),
        "frontend_total": verification.get("total"),
        "backend_verify_status": verify_status,
        "has_verification_results": bool(get_verification_results(verification_job_id)) if verification_job_id else False,
        "references_count": len(result.get("references_raw", []))
    }
@app.get("/debug/jobs")
async def debug_jobs():
    """List all jobs in _store"""
    with _lock:
        jobs_info = {}
        for job_id, job_data in _store.items():
            verification = job_data.get("verification", {})
            jobs_info[job_id] = {
                "verification_state": verification.get("state"),
                "verification_progress": verification.get("progress"),
                "verification_total": verification.get("total"),
                "verification_job_id": verification.get("verification_job_id"),
                "has_result": bool(job_data.get("result"))
            }
    return {
        "total_jobs": len(jobs_info),
        "jobs": jobs_info
    }
# ============================================================
# QUEUE STATUS ENDPOINT
# ============================================================

@app.get("/queue/status")
async def queue_status():
    status = get_queue_status()
    status["server_busy"] = is_server_busy()
    status["message"] = "Server is busy, please try later" if status["server_busy"] else "Server is ready"
    # Add queue length from Redis if available
    if task_queue:
        status["redis_queue_length"] = len(task_queue)
    else:
        status["redis_queue_length"] = 0
    return status

# ============================================================
# INDEX
# ============================================================

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# ============================================================
# PRIVACY POLICY
# ============================================================

@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request})

# ============================================================
# ASYNC DOCUMENT CHECK (QUEUED)
# ============================================================

@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    enable_autofix: bool = Form(False),
    enable_online_verification: bool = Form(False),
    request: Request = None
):
    # =========================
    # 1. VALIDATION
    # =========================
    if not file.filename:
        return JSONResponse(
            status_code=400,
            content={"error": "No file provided", "message": "Please select a file to upload"}
        )
    
    if not file.filename.lower().endswith('.docx'):
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid file format", "message": "Only DOCX files are accepted"}
        )
    
    if is_server_busy():
        return JSONResponse(
            status_code=503,
            content={
                "error": "Server is busy",
                "message": "Please wait a moment and try again",
                "retry_after": 30
            }
        )

    # =========================
    # 2. READ FILE
    # =========================
    data = await file.read()
    file_size = len(data)
    file_size_mb = round(file_size / (1024 * 1024), 2)

    # =========================
    # 3. GENERATE JOB ID
    # =========================
    job_id = str(uuid.uuid4())

    # =========================
    # 4. STORE FILE IN REDIS
    # =========================
    if not redis_conn:
        return JSONResponse(
            status_code=500,
            content={"error": "Queue system unavailable", "message": "Redis not connected"}
        )

    redis_conn.setex(f"file:{job_id}", 3600, data)
    print(f"✅ File stored in Redis for job {job_id}")

    # =========================
    # 5. STORE JOB IN DATABASE
    # =========================
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO jobs (job_id, status, file_name, file_size_mb, created_at)
                VALUES (%s, %s, %s, %s, NOW())
            """, (job_id, "queued", file.filename, file_size_mb))

            conn.commit()
            cursor.close()
            conn.close()

            print(f"✅ Job {job_id} stored in PostgreSQL")

        except Exception as db_error:
            print(f"⚠️ Database error: {db_error}")

    # =========================
    # 6. ENQUEUE JOB (🔥 FIXED)
    # =========================
    if not task_queue:
        return JSONResponse(
            status_code=500,
            content={"error": "Queue not initialized"}
        )

    try:
        task_queue.enqueue(
            "worker.process_document",   # 🔥 must match module.function
            job_id,                      # 🔥 positional args ONLY
            file.filename,
            style,
            job_timeout=3600
        )

        print(f"🔥 Job {job_id} queued successfully")

    except Exception as q_error:
        print(f"❌ Queue error: {q_error}")
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to queue job", "message": str(q_error)}
        )

    # =========================
    # 7. RECORD STATS
    # =========================
    try:
        stats_tracker.add_upload(
            filename=file.filename,
            file_size=file_size,
            references_count=0,
            processing_time=0,
            success=True,
            ip_address=request.client.host if request and request.client else None
        )
    except Exception as stats_error:
        print(f"⚠️ Stats error: {stats_error}")

    # =========================
    # 8. RETURN RESPONSE
    # =========================
    return {
        "job_id": job_id,
        "status": "queued",
        "message": "Document queued. Poll /job/{job_id} for status.",
        "file_name": file.filename,
        "file_size_mb": file_size_mb
    }
# ============================================================
# RESULT CHECK ENDPOINT
# ============================================================

@app.get("/result/{job_id}")
async def get_result(job_id: str):
    """Get job status and result"""
    
    # Check Redis cache first
    if redis_conn:
        cached = redis_conn.get(f"result:{job_id}")
        if cached:
            try:
                return {"status": "completed", "data": json.loads(cached)}
            except:
                pass
    
    # Check PostgreSQL
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status, result, error FROM jobs WHERE job_id = %s",
                (job_id,)
            )
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if not row:
                return {"status": "not_found", "error": "Job not found"}
            
            if row["status"] == "completed":
                result = row["result"]
                if isinstance(result, str):
                    result = json.loads(result)
                return {"status": "completed", "data": result}
            elif row["status"] == "processing":
                return {"status": "processing", "message": "Processing in background"}
            elif row["status"] == "queued":
                return {"status": "queued", "message": "Waiting in queue"}
            elif row["status"] == "failed":
                return {"status": "failed", "error": row["error"]}
            
        except Exception as e:
            print(f"Database error: {e}")
            return {"status": "error", "error": str(e)}
    
    return {"status": "pending", "message": "Job not found"}

@app.get("/job/{job_id}")
def get_job_endpoint(job_id: str):
    job = load_job_record(job_id)
    if not job:
        return {"status": "not_found"}
    return {
        "status": job.get("status", "unknown"),
        "result": job.get("result")
    }
# ============================================================
# AUTO-FIX ENDPOINTS
# ============================================================

@app.post("/apply-autofix")
async def apply_autofix(job_id: str = Form(...)):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    
    result = job.get("result", {})
    autofix_data = result.get("autofix", {})
    
    if not autofix_data or not autofix_data.get("suggestions"):
        raise HTTPException(400, "No auto-fix suggestions available for this document")
    
    fixed_content = generate_fixed_document_content(job, autofix_data.get("suggestions", {}))
    
    if not fixed_content:
        raise HTTPException(500, "Failed to generate fixed document - no main_text found")
    
    with _lock:
        if job_id in _store:
            _store[job_id]["fixed_document"] = fixed_content
            _store[job_id]["autofix_applied"] = True
    
    applied_fixes = []
    for fix in autofix_data.get("suggestions", {}).get("citations", []):
        if fix.get("confidence", 0) >= 0.85:
            applied_fixes.append({
                "original": fix.get("original"),
                "suggested": fix.get("suggested"),
                "type": fix.get("type")
            })
    
    return {
        "success": True,
        "job_id": job_id,
        "fixes_applied_count": len(applied_fixes),
        "fixes": applied_fixes,
        "message": f"Applied {len(applied_fixes)} auto-fixes to the document"
    }

@app.get("/autofix-suggestions/{job_id}")
async def get_autofix_suggestions(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    
    result = job.get("result", {})
    autofix_data = result.get("autofix", {})
    
    if not autofix_data:
        return {
            "available": False,
            "message": "Auto-fix was not enabled for this document. Please re-upload with auto-fix enabled."
        }
    
    suggestions = autofix_data.get("suggestions", {})
    summary = autofix_data.get("summary", {})
    
    return {
        "available": True,
        "enabled": autofix_data.get("enabled", False),
        "summary": summary,
        "citations": suggestions.get("citations", []),
        "references": suggestions.get("references", []),
        "statistics": suggestions.get("statistics", {}),
        "auto_fixable_count": suggestions.get("auto_fixable_count", 0),
        "review_needed_count": suggestions.get("review_needed_count", 0)
    }

@app.get("/fix-log/{job_id}")
async def get_fix_log(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    
    result = job.get("result", {})
    autofix_data = result.get("autofix", {})
    
    fixes_applied = []
    
    for fix in autofix_data.get("suggestions", {}).get("citations", []):
        if fix.get("confidence", 0) >= 0.85:
            fixes_applied.append({
                "original": fix.get("original"),
                "suggested": fix.get("suggested"),
                "type": fix.get("type"),
                "confidence": fix.get("confidence"),
                "reason": fix.get("reason")
            })
    
    return {
        "job_id": job_id,
        "autofix_applied": job.get("autofix_applied", False),
        "fixes": fixes_applied,
        "total_fixes": len(fixes_applied),
        "generated_at": datetime.now().isoformat()
    }

# ============================================================
# ONLINE VERIFICATION
# ============================================================

@app.post("/verify-online")
async def verify_online(job_id: str = Form(...)):
    job = load_job_record(job_id)

    if not job:
        raise HTTPException(404, "Job not found")

    verification = job.get("verification", {}) or {}
    result = job.get("result", {}) or {}

    # if an old bad attempt marked it completed but no rows exist, reopen it
    existing_rows = ((result.get("online_verification") or {}).get("rows") or [])
    if verification.get("state") == "completed" and not existing_rows:
        update_verification_status(
            job_id,
            state="idle",
            progress=0,
            total=0,
            percentage=0,
            message=""
        )
        verification["state"] = "idle"

    if verification.get("state") == "running":
        return {
            "started": False,
            "message": "Verification already in progress",
            "job_id": job_id,
            "progress": verification.get("progress", 0),
            "total": verification.get("total", 0)
        }

    if verification.get("state") == "completed" and existing_rows:
        return {
            "started": False,
            "message": "Verification already completed",
            "job_id": job_id,
            "completed": True
        }

    refs = result.get("references_raw", []) or []

    # repair references if engine returned none
    if not refs:
        repaired = recover_references_for_verification(
            result.get("main_text", ""),
            style_hint="apa"
        )
        if repaired:
            refs = repaired
            result["references_raw"] = repaired
            result.setdefault("summary", {})["reference_entries_found"] = len(repaired)

            with _lock:
                if job_id in _store:
                    _store[job_id]["result"]["references_raw"] = repaired
                    _store[job_id]["result"].setdefault("summary", {})["reference_entries_found"] = len(repaired)

            if DATABASE_URL:
                try:
                    conn = psycopg2.connect(DATABASE_URL)
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE jobs
                        SET result = result || %s::jsonb
                        WHERE job_id = %s
                    """, (
                        json.dumps({
                            "references_raw": repaired,
                            "summary": {
                                **(result.get("summary") or {}),
                                "reference_entries_found": len(repaired)
                            }
                        }),
                        job_id
                    ))
                    conn.commit()
                    cursor.close()
                    conn.close()
                except Exception as e:
                    print(f"[DEBUG] Could not persist repaired references: {e}")

    if not refs:
        update_verification_status(
            job_id,
            state="idle",
            progress=0,
            total=0,
            percentage=0,
            message=result.get("reference_detection_message", "No references extracted")
        )
        return {
            "started": False,
            "message": result.get("reference_detection_message", "No references extracted"),
            "job_id": job_id,
            "reason": "no_references"
        }

    update_verification_status(
        job_id,
        state="running",
        total=len(refs),
        progress=0,
        percentage=0,
        started_at=now(),
        message="Verification started"
    )

    verification_job_id = submit_verification(refs, style="apa", enrich_metadata=False)
    update_verification_status(job_id, verification_job_id=verification_job_id)

    # 🔥 Store verification metadata in PostgreSQL
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE jobs
                SET result = result || jsonb_build_object(
                    'verification_job_id', %s,
                    'verification_started_at', %s
                )
                WHERE job_id = %s
            """, (verification_job_id, now(), job_id))
            conn.commit()
            cursor.close()
            conn.close()
            print(f"[DEBUG] Stored verification_job_id {verification_job_id} in PostgreSQL for job {job_id}")
        except Exception as e:
            print(f"[DEBUG] Could not persist verification_job_id: {e}")

    stats_tracker.add_verification(job_id, len(refs), success=True)
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
    print(f"[DEBUG] /online/status called with job_id: {job_id}")
    
    # Use load_job_record which checks both memory and PostgreSQL
    job = load_job_record(job_id)
    
    print(f"[DEBUG] load_job_record returned: {job is not None}")
    
    if not job:
        raise HTTPException(404, "Job not found")
    
    # If job was loaded from PostgreSQL, store it in memory for future requests
    if job_id not in _store:
        with _lock:
            _store[job_id] = job
        print(f"[DEBUG] Loaded job {job_id} into memory from PostgreSQL")
    
    verification = job.get("verification", {})
    result = job.get("result", {})
    
    print(f"[DEBUG] Job found. Verification state: {verification.get('state')}, verification_job_id: {verification.get('verification_job_id')}")
    
    # Get the actual verification job ID from the store
    verification_job_id = verification.get("verification_job_id")
    
    if verification_job_id:
        # Get status from verify.py using the correct verification job ID
        from verify import get_verification_status
        verify_status = get_verification_status(verification_job_id)
        
        print(f"[DEBUG] verify_status from get_verification_status: {verify_status}")
        
        if verify_status:
            # Update the verification dict with the real progress
            verification["state"] = verify_status.get("status", "processing")
            verification["progress"] = verify_status.get("progress", 0)
            verification["total"] = verify_status.get("total", 0)
            verification["percentage"] = verify_status.get("percentage", 0)
            print(f"[DEBUG] Updated verification: progress={verification['progress']}/{verification['total']}, state={verification['state']}")
    else:
        # Try to find verification_job_id in the result (from database)
        verification_job_id_in_result = result.get("verification_job_id")
        if verification_job_id_in_result:
            verification["verification_job_id"] = verification_job_id_in_result
            verification_job_id = verification_job_id_in_result
            print(f"[DEBUG] Found verification_job_id in result: {verification_job_id}")
            
            from verify import get_verification_status
            verify_status = get_verification_status(verification_job_id)
            if verify_status:
                verification["state"] = verify_status.get("status", "processing")
                verification["progress"] = verify_status.get("progress", 0)
                verification["total"] = verify_status.get("total", 0)
                verification["percentage"] = verify_status.get("percentage", 0)
                print(f"[DEBUG] Updated verification from result: progress={verification['progress']}/{verification['total']}")
        else:
            print(f"[DEBUG] No verification_job_id found for job {job_id}")
    
    elapsed_seconds = 0
    remaining_seconds = None
    
    if verification.get("started_at") and verification.get("state") == "running":
        started = datetime.fromisoformat(verification["started_at"])
        elapsed_seconds = (datetime.utcnow() - started).total_seconds()
        
        progress = verification.get("progress", 0)
        total = verification.get("total", 0)
        if progress > 0 and elapsed_seconds > 0:
            rate = progress / elapsed_seconds
            remaining_seconds = (total - progress) / rate if rate > 0 else 0
    
    response = {
        "online": {
            "state": verification.get("state", "idle"),
            "progress": verification.get("progress", 0),
            "total": verification.get("total", 0),
            "percentage": verification.get("percentage", 0),
            "started_at": verification.get("started_at"),
            "completed_at": verification.get("completed_at"),
            "summary": verification.get("summary", {}),
            "results_count": verification.get("results_count", 0),
            "elapsed_seconds": round(elapsed_seconds, 1),
            "elapsed_formatted": format_time(elapsed_seconds),
            "remaining_seconds": round(remaining_seconds, 1) if remaining_seconds else None,
            "remaining_formatted": format_time(remaining_seconds) if remaining_seconds else None,
            "verification_job_id": verification_job_id
        },
        "result": result if verification.get("state") in ["completed", "error"] else None
    }
    
    if verification.get("state") == "completed" and "online_verification" in result:
        response["online_verification"] = {
            "rows": result["online_verification"]["rows"],
            "summary": result["online_verification"]["summary"]
        }
    
    if verification.get("total", 0) > 0:
        time_msg = f"Processing: {verification.get('progress', 0)}/{verification.get('total', 0)} ({verification.get('percentage', 0)}%)"
        if remaining_seconds:
            time_msg += f" - Est. remaining: {format_time(remaining_seconds)}"
        response["progress"] = {
            "current": verification.get("progress", 0),
            "total": verification.get("total", 0),
            "percentage": verification.get("percentage", 0),
            "status": verification.get("state", "idle"),
            "message": time_msg,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "remaining_seconds": round(remaining_seconds, 1) if remaining_seconds else None
        }
    
    response["queue"] = get_queue_status()
    
    return response
# ============================================================
# DOCUMENT EXPORT
# ============================================================

@app.get("/export-fixed-document/{job_id}")
async def export_fixed_document(job_id: str, format: str = "txt"):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    
    fixed_document = job.get("fixed_document")
    
    if not fixed_document:
        result = job.get("result", {})
        autofix_data = result.get("autofix", {})
        
        if autofix_data and autofix_data.get("suggestions"):
            fixed_document = generate_fixed_document_content(job, autofix_data.get("suggestions", {}))
            if fixed_document:
                with _lock:
                    if job_id in _store:
                        _store[job_id]["fixed_document"] = fixed_document
                        _store[job_id]["autofix_applied"] = True
    
    if not fixed_document:
        raise HTTPException(400, "No fixed document available. Please apply auto-fix first.")
    
    original_filename = job.get("result", {}).get("filename", "document")
    base_name = os.path.splitext(original_filename)[0]
    
    return Response(
        content=fixed_document,
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={base_name}_fixed.txt"}
    )

# ============================================================
# STATISTICS WEB PAGE
# ============================================================

@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    return templates.TemplateResponse("stats.html", {"request": request})

# ============================================================
# PRIVATE STATS ENDPOINTS
# ============================================================

@app.get("/private-stats")
def get_private_stats(
    credentials: HTTPBasicCredentials = Depends(security),
    detailed: bool = False,
    days: int = 30
):
    authenticate(credentials)
    stats = stats_tracker.get_stats(detailed=detailed, days=days)
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
    authenticate(credentials)
    stats = stats_tracker.get_stats(detailed=False)
    return {"manuscripts_checked": stats['total_stats']['total_uploads']}

@app.get("/private-stats/clear")
def clear_old_stats(
    credentials: HTTPBasicCredentials = Depends(security),
    keep_days: int = 30
):
    authenticate(credentials)
    try:
        stats_tracker.clear_stats(keep_last_days=keep_days)
        return {"success": True, "message": f"Cleared stats older than {keep_days} days", "kept_days": keep_days}
    except Exception as e:
        raise HTTPException(500, f"Error clearing stats: {str(e)}")

@app.get("/private-stats/export")
def export_stats(
    credentials: HTTPBasicCredentials = Depends(security),
    format: str = "json"
):
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
# EXPORT REFERENCES ENDPOINT
# ============================================================

@app.post("/export-references")
async def export_references(
    job_id: str = Form(...),
    style: str = Form("apa7"),
    format_type: str = Form("docx")
):
    """
    Export verified references to DOCX or HTML.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    
    result = job.get("result", {})
    online_verification = result.get("online_verification", {})
    verification_rows = online_verification.get("rows", [])
    
    if not verification_rows:
        raise HTTPException(400, "No verification results available. Run online verification first.")
    
    formatted_refs = format_verified_reference_list(verification_rows, style)
    
    if not formatted_refs:
        raise HTTPException(400, "No verified references found to export.")
    
    if format_type == "docx":
        if not DOCX_AVAILABLE:
            raise HTTPException(500, "DOCX export not available. Please install python-docx.")
        
        docx_buffer = export_references_to_docx(formatted_refs, style)
        
        if not docx_buffer:
            raise HTTPException(500, "Failed to generate DOCX file.")
        
        filename = f"citeintegrity_references_{job_id[:8]}_{style}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
        
        return Response(
            content=docx_buffer.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    
    elif format_type == "html":
        html_content = export_references_to_html(formatted_refs, style)
        
        filename = f"citeintegrity_references_{job_id[:8]}_{style}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        
        return Response(
            content=html_content,
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    
    else:
        raise HTTPException(400, "Invalid format. Use 'docx' or 'html'.")

# ============================================================
# DEBUG STATS ENDPOINT
# ============================================================

@app.get("/debug/stats-info")
def debug_stats_info(credentials: HTTPBasicCredentials = Depends(security)):
    authenticate(credentials)
    stats = stats_tracker.get_stats(detailed=True)
    return {
        "storage": "sqlite",
        "database_path": '/tmp/citation_stats.db',
        "database_exists": os.path.exists('/tmp/citation_stats.db'),
        "database_size": os.path.getsize('/tmp/citation_stats.db') if os.path.exists('/tmp/citation_stats.db') else 0,
        "stats": stats
    }

# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health():
    queue_stats = get_queue_status()
    return {
        "status": "healthy" if not queue_stats.get("is_busy", False) else "degraded",
        "timestamp": now(),
        "queue": queue_stats,
        "server_busy": queue_stats.get("is_busy", False),
        "redis_connected": redis_conn is not None,
        "postgresql_connected": DATABASE_URL is not None
    }

# ============================================================
# ERROR HANDLERS
# ============================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "status_code": exc.status_code, "timestamp": now()}
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
