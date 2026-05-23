# main.py — Citation Crosschecker with Async Queue System
# MAIN_BUILD = "commercial-2026-05-22-large-file-page-routing-v1.5.33"

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

# Optional lightweight preflight readers used only for queue routing.
# If a reader is unavailable or fails, the file is routed safely to the large queue.
try:
    from pypdf import PdfReader
except Exception:
    try:
        from PyPDF2 import PdfReader
    except Exception:
        PdfReader = None

try:
    from docx import Document as DocxDocument
except Exception:
    DocxDocument = None

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
verification_queue = None
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
        "/api/enrichment",
        "/new",
        "/analyse",
        "/results",
        "/features",
        "/pricing",
        "/contact",
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
    if (
        path.startswith("/online/")
        or path.startswith("/verify")
        or path.startswith("/api/")
        or path.startswith("/private-stats")
        or path.startswith("/result")
    ):
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
    # This protects CiteIntegrity pages from being embedded on other sites.
    # It does NOT block YouTube inside our own page, because YouTube is allowed below in CSP frame-src.
    response.headers["X-Frame-Options"] = "DENY"
    
    # 🛡️ MIME sniffing protection
    response.headers["X-Content-Type-Options"] = "nosniff"
    
    # 🛡️ XSS protection (legacy browsers)
    response.headers["X-XSS-Protection"] = "1; mode=block"
    
    # 🛡️ Referrer policy
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    
    # 🛡️ Content Security Policy
    # Allows the CiteIntegrity YouTube demo iframe while keeping the rest locked down.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "img-src 'self' data: https:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://www.youtube.com https://www.youtube-nocookie.com https://s.ytimg.com; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data: https:; "
        "connect-src 'self' https:; "
        "frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com; "
        "child-src 'self' https://www.youtube.com https://www.youtube-nocookie.com; "
        "media-src 'self' https: blob:; "
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


# --------------------------------------------------
# Large-document preflight routing
# --------------------------------------------------

NORMAL_DOCUMENT_QUEUE = "document_processing"
LARGE_DOCUMENT_QUEUE = "large_document_processing"

NORMAL_PDF_PAGE_LIMIT = int(os.environ.get("NORMAL_PDF_PAGE_LIMIT", "200"))
NORMAL_DOCX_WORD_LIMIT = int(os.environ.get("NORMAL_DOCX_WORD_LIMIT", "80000"))

NORMAL_FILE_REDIS_TTL = int(os.environ.get("NORMAL_FILE_REDIS_TTL", "3600"))
LARGE_FILE_REDIS_TTL = int(os.environ.get("LARGE_FILE_REDIS_TTL", "21600"))

NORMAL_DOCUMENT_JOB_TIMEOUT = int(os.environ.get("NORMAL_DOCUMENT_JOB_TIMEOUT", "3600"))
LARGE_DOCUMENT_JOB_TIMEOUT = int(os.environ.get("LARGE_DOCUMENT_JOB_TIMEOUT", "14400"))


def estimate_document_load(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """
    Lightweight preflight check before queueing.

    File size alone is not reliable. A 366-page PDF can be small in MB but still
    heavy to parse. This function routes based on page count for PDFs and word
    count for DOCX files.
    """
    filename_lower = (filename or "").lower().strip()
    file_size_mb = round(len(file_bytes or b"") / (1024 * 1024), 2)

    info: Dict[str, Any] = {
        "filename": filename,
        "file_size_mb": file_size_mb,
        "file_type": "unknown",
        "page_count": None,
        "word_count": None,
        "large_file": False,
        "queue_name": NORMAL_DOCUMENT_QUEUE,
        "route_reason": "Normal document route.",
    }

    # PDF: route mainly by page count.
    if filename_lower.endswith(".pdf"):
        info["file_type"] = "pdf"

        if PdfReader is None:
            info.update({
                "large_file": True,
                "queue_name": LARGE_DOCUMENT_QUEUE,
                "route_reason": "PDF page-count reader is unavailable, routed safely to the large-file queue.",
            })
            return info

        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            page_count = len(reader.pages)
            info["page_count"] = page_count

            if page_count > NORMAL_PDF_PAGE_LIMIT:
                info.update({
                    "large_file": True,
                    "queue_name": LARGE_DOCUMENT_QUEUE,
                    "route_reason": (
                        f"PDF has {page_count} pages, above the normal limit of "
                        f"{NORMAL_PDF_PAGE_LIMIT} pages."
                    ),
                })
            else:
                info["route_reason"] = (
                    f"PDF has {page_count} pages, within the normal limit of "
                    f"{NORMAL_PDF_PAGE_LIMIT} pages."
                )

            return info

        except Exception as e:
            info.update({
                "large_file": True,
                "queue_name": LARGE_DOCUMENT_QUEUE,
                "route_reason": f"PDF page-count check failed, routed safely to the large-file queue: {str(e)[:160]}",
            })
            return info

    # DOCX: route mainly by estimated word count.
    if filename_lower.endswith(".docx"):
        info["file_type"] = "docx"

        if DocxDocument is None:
            info.update({
                "large_file": True,
                "queue_name": LARGE_DOCUMENT_QUEUE,
                "route_reason": "DOCX reader is unavailable, routed safely to the large-file queue.",
            })
            return info

        try:
            doc = DocxDocument(io.BytesIO(file_bytes))
            text_parts: List[str] = []

            for paragraph in doc.paragraphs:
                if paragraph.text:
                    text_parts.append(paragraph.text)

            # Include tables because theses and dissertations often contain
            # important text, citations, or references inside tables.
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        if cell.text:
                            text_parts.append(cell.text)

            joined_text = "\n".join(text_parts)
            word_count = len(re.findall(r"\b\w+\b", joined_text))
            info["word_count"] = word_count

            if word_count > NORMAL_DOCX_WORD_LIMIT:
                info.update({
                    "large_file": True,
                    "queue_name": LARGE_DOCUMENT_QUEUE,
                    "route_reason": (
                        f"DOCX has about {word_count:,} words, above the normal "
                        f"limit of {NORMAL_DOCX_WORD_LIMIT:,} words."
                    ),
                })
            else:
                info["route_reason"] = (
                    f"DOCX has about {word_count:,} words, within the normal "
                    f"limit of {NORMAL_DOCX_WORD_LIMIT:,} words."
                )

            return info

        except Exception as e:
            info.update({
                "large_file": True,
                "queue_name": LARGE_DOCUMENT_QUEUE,
                "route_reason": f"DOCX preflight check failed, routed safely to the large-file queue: {str(e)[:160]}",
            })
            return info

    return info

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

def _norm_lookup_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _find_citation_for_reference(original_ref: str, matched_title: str, c2r_rows: list) -> str:
    """
    Robustly recover the in-text citation linked to a reference.
    This avoids exact-match failure between verification rows and reconciliation rows.
    """
    ref_norm = _norm_lookup_text(original_ref)
    title_norm = _norm_lookup_text(matched_title)

    for r in c2r_rows:
        matched_ref = (
            r.get("matched_reference", "")
            or r.get("reference", "")
            or r.get("ref", "")
            or ""
        )
        in_text = (
            r.get("in_text", "")
            or r.get("citation", "")
            or r.get("citation_in_text", "")
            or ""
        )

        if not matched_ref or not in_text:
            continue

        matched_norm = _norm_lookup_text(matched_ref)

        if ref_norm and (ref_norm == matched_norm or ref_norm[:120] in matched_norm or matched_norm[:120] in ref_norm):
            return in_text

        if title_norm and title_norm in matched_norm:
            return in_text

    return ""


def build_context_specific_recovery(result: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "missing_recovery": [],
        "verification_recovery": []
    }

    full_text = (
        result.get("main_text", "")
        or result.get("full_text", "")
        or result.get("data", {}).get("main_text", "")
        or ""
    )

    missing_items = result.get("missing_in_references", []) or []
    missing_suggestions = result.get("missing_citation_suggestions", {}) or {}

    # Missing citation recovery
    for item in missing_items:
        if isinstance(item, dict):
            citation_text = (
                item.get("citation_in_text", "")
                or item.get("citation", "")
                or item.get("in_text", "")
                or ""
            )
            count = item.get("count", 1)
        else:
            citation_text = str(item)
            count = 1

        if not citation_text:
            continue

        suggestions = missing_suggestions.get(citation_text, []) or []

        # Generate context-based suggestions if none already exist
        if not suggestions and full_text:
            try:
                context = extract_context(full_text, citation_text, window=250)
                if context:
                    suggestions = suggest_from_context(
                        context=context,
                        citation=citation_text,
                        top_k=3
                    )
            except Exception as e:
                print(f"[DEBUG] Missing recovery failed for {citation_text}: {e}")
                suggestions = []

        payload["missing_recovery"].append({
            "citation": citation_text,
            "count": count,
            "suggestions": suggestions,
            "message": "" if suggestions else "No evidence found."
        })

    # Verification recovery
    verify_rows = (result.get("online_verification") or {}).get("rows", []) or []
    c2r_rows = result.get("reconciliation_intext_to_reference", []) or []

    for row in verify_rows:
        status = row.get("status", "")
        if status not in {"needs_review", "not_found"}:
            continue

        original_ref = row.get("reference", "") or ""
        matched_title = row.get("matched_title", "") or ""

        citation_text = _find_citation_for_reference(
            original_ref=original_ref,
            matched_title=matched_title,
            c2r_rows=c2r_rows
        )

        suggestions = []

        if citation_text and full_text:
            try:
                context = extract_context(full_text, citation_text, window=250)
                if context:
                    suggestions = suggest_from_context(
                        context=context,
                        citation=citation_text,
                        top_k=3
                    )
            except Exception as e:
                print(f"[DEBUG] Verification recovery failed for {citation_text}: {e}")
                suggestions = []

        payload["verification_recovery"].append({
            "reference": original_ref,
            "status": status,
            "citation": citation_text,
            "suggestions": suggestions,
            "message": "" if suggestions else "No evidence found."
        })

    return payload

def load_job_record(job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        if job_id in _store:
            return _store[job_id]

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

            result = row["result"] or {}
            if isinstance(result, str):
                result = json.loads(result)

            verification = result.get("verification", {}) or {}
            online_verification = result.get("online_verification", {}) or {}
            rows = online_verification.get("rows", []) or []

            if rows and verification.get("state") != "completed":
                verification.update({
                    "state": "completed",
                    "progress": len(rows),
                    "total": len(rows),
                    "percentage": 100,
                    "results_count": len(rows),
                    "summary": online_verification.get("summary", {})
                })

            job_record = {
                "job_id": job_id,
                "status": row["status"],
                "result": result,
                "error": row["error"],
                "verification": {
                    "state": verification.get("state", "idle"),
                    "progress": verification.get("progress", 0),
                    "total": verification.get("total", 0),
                    "percentage": verification.get("percentage", 0),
                    "message": verification.get("message", ""),
                    "verification_job_id": verification.get("verification_job_id"),
                    "rq_job_id": verification.get("rq_job_id"),
                    "rq_status": verification.get("rq_status"),
                    "started_at": verification.get("started_at"),
                    "completed_at": verification.get("completed_at"),
                    "last_heartbeat": verification.get("last_heartbeat"),
                    "error": verification.get("error"),
                    "summary": online_verification.get("summary", {}),
                    "results_count": len(rows)
                }
            }

            with _lock:
                _store[job_id] = job_record

            return job_record

        except Exception as e:
            print(f"load_job_record DB error: {e}")
            import traceback
            traceback.print_exc()

    return None
def load_job_record_fresh(job_id: str) -> Optional[Dict[str, Any]]:
    """
    Fresh loader for polling and enrichment endpoints.

    PostgreSQL is the source of truth because the worker writes enrichment
    updates to jobs.result first, then refreshes Redis. Redis is used only as
    a fallback when PostgreSQL is unavailable.
    """

    result = None
    status = None
    error = None

    # 1. Read PostgreSQL first, so advanced-enrichment polling does not
    # accidentally use a stale Redis copy.
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

            status = row.get("status")
            error = row.get("error")
            result = row.get("result") or {}

            if isinstance(result, str):
                result = json.loads(result)

            # Refresh Redis with the source-of-truth result.
            if redis_conn:
                try:
                    redis_conn.setex(f"result:{job_id}", 3600, json.dumps(result))
                except Exception as e:
                    print(f"[FRESH LOAD] Redis refresh failed for {job_id}: {e}")

        except Exception as e:
            print(f"[FRESH LOAD] PostgreSQL read failed for {job_id}: {e}")

    # 2. Fallback to Redis only when PostgreSQL could not return a result.
    if result is None and redis_conn:
        try:
            cached = redis_conn.get(f"result:{job_id}")
            if cached:
                result = json.loads(cached)
                status = result.get("status") or "completed"
        except Exception as e:
            print(f"[FRESH LOAD] Redis read failed for {job_id}: {e}")

    if result is None:
        return None

    verification = result.get("verification", {}) or {}
    online_verification = result.get("online_verification", {}) or {}
    rows = online_verification.get("rows", []) or []

    if rows and verification.get("progress", 0) < len(rows):
        verification["progress"] = len(rows)
        verification["results_count"] = len(rows)

    if rows and not verification.get("total"):
        verification["total"] = len(rows)

    if verification.get("total"):
        verification["percentage"] = int(
            (verification.get("progress", 0) / max(verification.get("total", 1), 1)) * 100
        )

    result["verification"] = verification

    return {
        "job_id": job_id,
        "status": status,
        "result": result,
        "error": error,
        "verification": verification
    }
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
    """
    Update verification state in memory and PostgreSQL result JSON.

    The new results dashboard should be able to recover even if memory is lost,
    so verification state must be persisted in jobs.result.verification.
    """
    payload = {k: v for k, v in kwargs.items() if v is not None}

    with _lock:
        if job_id in _store:
            _store[job_id].setdefault("verification", {})
            _store[job_id]["verification"].update(payload)

            _store[job_id].setdefault("result", {})
            _store[job_id]["result"].setdefault("verification", {})
            _store[job_id]["result"]["verification"].update(payload)

    if not DATABASE_URL:
        return

    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        cursor = conn.cursor()

        cursor.execute("SELECT result FROM jobs WHERE job_id = %s", (job_id,))
        row = cursor.fetchone()

        if not row:
            cursor.close()
            conn.close()
            return

        result = row["result"] or {}
        if isinstance(result, str):
            result = json.loads(result)

        result.setdefault("verification", {})
        result["verification"].update(payload)

        cursor.execute(
            """
            UPDATE jobs
            SET result = %s::jsonb
            WHERE job_id = %s
            """,
            (json.dumps(result), job_id)
        )

        conn.commit()
        cursor.close()
        conn.close()

    except Exception as e:
        print(f"[VERIFY STATUS] PostgreSQL update failed for {job_id}: {e}")

def start_progress_sync(job_id: str, verification_job_id: str):
    def sync():
        print(f"[DEBUG] Sync thread started for job {job_id}, verification_job_id={verification_job_id}")
        
        last_progress = -1
        no_progress_count = 0
        max_no_progress = 600  # Increased to 600 (20 minutes) for large reference sets
        last_log_time = time.time()
        last_heartbeat = time.time()
        heartbeat_interval = 30  # Send heartbeat every 30 seconds
        
        while True:
            try:
                # Send heartbeat to prevent timeout and show job is alive
                if time.time() - last_heartbeat > heartbeat_interval:
                    print(f"[DEBUG] 💓 Heartbeat: Job {job_id} still processing (progress: {last_progress})")
                    last_heartbeat = time.time()
                    
                    # Update a timestamp in store to show job is alive
                    with _lock:
                        if job_id in _store:
                            _store[job_id]["verification"]["last_heartbeat"] = now()
                            # Also update the message to show it's still working
                            if _store[job_id]["verification"].get("total", 0) > 0:
                                current_progress = _store[job_id]["verification"].get("progress", 0)
                                total = _store[job_id]["verification"].get("total", 0)
                                if current_progress < total:
                                    _store[job_id]["verification"]["message"] = f"Still verifying: {current_progress}/{total} - This may take several minutes for large documents"
                
                # Get status from verify.py's job tracking
                status = get_verification_status(verification_job_id)
                
                # Debug log every 30 seconds (reduced frequency)
                if time.time() - last_log_time > 30:
                    print(f"[DEBUG] Sync status for {verification_job_id}: {status}")
                    last_log_time = time.time()
                
                if status:
                    current_progress = status.get("progress", 0)
                    total = status.get("total", 0)
                    status_state = status.get("status", "processing")
                    
                    # Only log every 5th progress update to reduce noise
                    if current_progress != last_progress:
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
                            
                            # Also update the message for frontend display with ETA for large sets
                            if total > 0:
                                if total > 100 and current_progress < total:
                                    # For large reference sets, show estimated time
                                    elapsed = time.time() - last_heartbeat + heartbeat_interval
                                    if current_progress > 0 and elapsed > 0:
                                        rate = current_progress / elapsed
                                        remaining = (total - current_progress) / rate if rate > 0 else 0
                                        _store[job_id]["verification"]["message"] = f"Verifying: {current_progress}/{total} (Est. remaining: {remaining/60:.1f} min)"
                                    else:
                                        _store[job_id]["verification"]["message"] = f"Verifying: {current_progress}/{total} - This may take several minutes"
                                else:
                                    _store[job_id]["verification"]["message"] = f"Verifying: {current_progress}/{total}"
                            else:
                                _store[job_id]["verification"]["message"] = "Starting verification..."
                            
                            print(f"[DEBUG] Updated _store for job {job_id}: progress={current_progress}, total={total}, state={status_state}")
                    
                    # Check for completion
                    if status_state == "completed":
                        print(f"[DEBUG] ✅ Verification job {verification_job_id} completed!")
                        verification_results = None
                        max_attempts = 30  # Increased attempts for large result sets
                        for attempt in range(max_attempts):
                            verification_results = get_verification_results(verification_job_id)
                            if verification_results:
                                print(f"[DEBUG] Retrieved {len(verification_results)} results on attempt {attempt + 1}")
                                break
                            print(f"[DEBUG] Waiting for results, attempt {attempt + 1}/{max_attempts}...")
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
                                    
                                    try:
                                        _store[job_id]["result"]["recovery"] = build_context_specific_recovery(
                                            _store[job_id]["result"]
                                        )
                                        print("[RECOVERY] Rows built")
                                    except Exception as e:
                                        print(f"[RECOVERY ERROR] {e}")
                                        _store[job_id]["result"]["recovery"] = {
                                            "missing_recovery": [],
                                            "verification_recovery": []
                                        }
                                    
                                    try:
                                        _store[job_id]["result"]["claim_support"] = build_claim_support_rows(
                                            _store[job_id]["result"]
                                        )
                                        print("[CLAIM SUPPORT] Rows:", len(_store[job_id]["result"].get("claim_support", [])))
                                        print("[CLAIM SUPPORT] Sample:", _store[job_id]["result"].get("claim_support", [])[:1])
                                    except Exception as e:
                                        print(f"[CLAIM SUPPORT ERROR] {e}")
                                        _store[job_id]["result"]["claim_support"] = []
                                    
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
                                    final_result = _store[job_id]["result"]
                                    final_result["verification_completed_at"] = now()
                                    
                                    cursor.execute("""
                                        UPDATE jobs
                                        SET result = %s::jsonb
                                        WHERE job_id = %s
                                    """, (json.dumps(final_result), job_id))
                                    conn.commit()
                                    cursor.close()
                                    conn.close()
                                    print(f"[DEBUG] Stored verification completion in PostgreSQL for job {job_id}")
                                except Exception as e:
                                    print(f"[DEBUG] Could not persist verification completion: {e}")
                        else:
                            print(f"[DEBUG] ⚠️ No results retrieved after {max_attempts} attempts")
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
                        print(f"[DEBUG] ❌ Verification job {verification_job_id} error: {error_msg}")
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
                    
                    # Check for stall (no progress for too long) - increased threshold for large sets
                    if no_progress_count > max_no_progress and current_progress < total:
                        print(f"[DEBUG] ⚠️ No progress for {max_no_progress * 2} seconds, but job may still be working on large references")
                        # Reset counter and continue instead of failing immediately
                        # Only fail if progress is 0 and we've been waiting over 30 minutes
                        if current_progress == 0 and no_progress_count > 900:  # 30 minutes
                            print(f"[DEBUG] ❌ No progress for 30 minutes, marking as error")
                            with _lock:
                                if job_id in _store:
                                    _store[job_id]["verification"]["state"] = "error"
                                    _store[job_id]["verification"]["message"] = "Verification stalled - no progress for 30 minutes"
                            
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
                                    """, ("Verification stalled - no progress for 30 minutes", now(), job_id))
                                    conn.commit()
                                    cursor.close()
                                    conn.close()
                                except Exception as e:
                                    print(f"[DEBUG] Could not persist stall error: {e}")
                            break
                        else:
                            # Reset counter and continue
                            no_progress_count = 0
                            print(f"[DEBUG] Resetting stall counter, still processing...")
                        
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
@app.get("/debug/verification-details/{job_id}")
async def debug_verification_details(job_id: str):
    """Debug endpoint to check verification details"""
    job = load_job_record(job_id)
    if not job:
        return {"error": "Job not found"}
    
    verification = job.get("verification", {})
    verification_job_id = verification.get("verification_job_id")
    
    result = {
        "job_id": job_id,
        "verification_job_id": verification_job_id,
        "verification_state": verification.get("state"),
        "verification_progress": verification.get("progress"),
        "verification_total": verification.get("total"),
        "verification_percentage": verification.get("percentage"),
        "started_at": verification.get("started_at"),
        "completed_at": verification.get("completed_at")
    }
    
    # Get the actual status from verify.py
    if verification_job_id:
        from verify import get_verification_status, get_verification_results
        verify_status = get_verification_status(verification_job_id)
        verify_results = get_verification_results(verification_job_id)
        
        result["actual_verify_status"] = verify_status
        result["has_verify_results"] = verify_results is not None
        result["verify_results_count"] = len(verify_results) if verify_results else 0
        
        # Also check if the verification is still in the jobs dictionary
        from verify import _jobs
        with verify._jobs_lock:
            result["verify_job_exists"] = verification_job_id in verify._jobs
            if verification_job_id in verify._jobs:
                job_obj = verify._jobs[verification_job_id]
                result["verify_job_details"] = {
                    "status": job_obj.status,
                    "progress": job_obj.progress,
                    "total": job_obj.total
                }
    
    return result

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
@app.get("/debug/check-verification/{job_id}")
async def debug_check_verification(job_id: str):
    """Debug endpoint to check verification data"""
    job = load_job_record(job_id)
    if not job:
        return {"error": "Job not found"}
    
    verification = job.get("verification", {})
    result = job.get("result", {})
    
    # Check verification results from verify.py
    from verify import get_verification_results
    verification_job_id = verification.get("verification_job_id")
    stored_results = get_verification_results(verification_job_id) if verification_job_id else None
    
    return {
        "job_id": job_id,
        "verification_job_id": verification_job_id,
        "verification_state": verification.get("state"),
        "verification_progress": verification.get("progress"),
        "verification_total": verification.get("total"),
        "verification_completed_at": verification.get("completed_at"),
        "has_online_verification_in_result": "online_verification" in result,
        "online_verification_rows": len(result.get("online_verification", {}).get("rows", [])),
        "stored_results_from_verify_py": len(stored_results) if stored_results else 0,
        "has_claim_support": "claim_support" in result,
        "claim_support_rows": len(result.get("claim_support", [])),
        "result_keys": list(result.keys())
    }    
@app.get("/debug/test-suggestions")
async def test_suggestions():
    """Test endpoint to see what suggestions look like"""
    from engine import generate_suggestions, parse_reference_author_year
    
    # Create test data
    test_citations = ["(Smith, 2019)", "(Wrong, 2020)"]
    test_references_raw = [
        "Smith, J. (2020). A test title. Journal of Testing, 10(2), 100-110.",
        "Johnson, A. (2019). Another title. Another Journal, 5(1), 20-30."
    ]
    
    test_refs = [parse_reference_author_year(r) for r in test_references_raw]
    test_refs = [r for r in test_refs if r is not None]
    test_ref_map = {r.key: r.reference_full for r in test_refs}
    
    suggestions = generate_suggestions(
        citations=test_citations,
        c2r=[],
        missing_rows=[],
        references=test_refs,
        ref_map=test_ref_map
    )
    
    return {
        "test_suggestions": suggestions,
        "citations_count": len(suggestions.get("citations", [])),
        "sample": suggestions.get("citations", [])[:2]
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

@app.get("/debug/db-status")
async def db_status():
    """Check database connection status"""
    status = {
        "postgresql_configured": DATABASE_URL is not None,
        "redis_configured": REDIS_URL is not None,
        "tables_exist": False,
        "stats_count": 0
    }
    
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            
            # Check if stats table exists
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'stats'
                )
            """)
            status["stats_table_exists"] = cursor.fetchone()[0]
            
            # Get total uploads
            cursor.execute("SELECT total_uploads FROM stats WHERE id = 1")
            row = cursor.fetchone()
            if row:
                status["stats_count"] = row[0]
            
            cursor.close()
            conn.close()
            status["database_connected"] = True
            
        except Exception as e:
            status["database_connected"] = False
            status["error"] = str(e)
    
    return status


@app.post("/debug/retry-stuck-verification/{job_id}")
async def retry_stuck_verification(job_id: str):
    """Force retry a stuck verification job"""
    job = load_job_record(job_id)
    if not job:
        return {"error": "Job not found"}
    
    verification = job.get("verification", {})
    if verification.get("state") != "running":
        return {"error": "Job is not running"}
    
    # Get the verification job ID
    verification_job_id = verification.get("verification_job_id")
    if not verification_job_id:
        return {"error": "No verification job ID found"}
    
    from verify import get_verification_status, _jobs
    
    # Check if the job is actually stuck
    status = get_verification_status(verification_job_id)
    if status and status.get("progress", 0) > 0:
        return {"error": "Job is making progress", "status": status}
    
    # Mark existing verification as failed
    update_verification_status(job_id, state="error", message="Stuck - retrying")
    
    # Restart verification
    refs = job.get("result", {}).get("references_raw", [])
    if not refs:
        return {"error": "No references to verify"}

    selected_style = _style_for_job_result(job.get("result", {}) or {})
    print(f"[VERIFY RETRY] Re-queueing job {job_id} with style={selected_style}")
    
    new_verification_job_id = f"verify:{job_id}:{uuid.uuid4().hex[:8]}"
    if not verification_queue:
        raise HTTPException(500, "Verification queue not initialized")
    
    update_verification_status(
        job_id,
        verification_job_id=new_verification_job_id,
        rq_job_id=new_verification_job_id,
        state="queued",
        total=len(refs),
        progress=0,
        percentage=0,
        started_at=now(),
        message="Verification re-queued"
    )
    
    verification_queue.enqueue(
        "worker.process_verification",
        job_id,
        selected_style,
        False,
        job_id=new_verification_job_id,
        job_timeout=10800,
        result_ttl=86400,
        failure_ttl=86400
    )
    
    return {
        "started": True,
        "success": True,
        "old_verification_job_id": verification_job_id,
        "new_verification_job_id": new_verification_job_id,
        "verification_job_id": new_verification_job_id,
        "job_id": job_id,
        "total_references": len(refs),
        "state": "queued",
        "message": "Verification re-queued successfully"
    }
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
    # Add queue lengths from Redis if available
    if redis_conn:
        try:
            normal_queue = Queue(NORMAL_DOCUMENT_QUEUE, connection=redis_conn)
            large_queue = Queue(LARGE_DOCUMENT_QUEUE, connection=redis_conn)
            verify_queue = Queue("verification", connection=redis_conn)
            deep_queue = Queue("deep_enrichment", connection=redis_conn)

            status["redis_queue_length"] = len(normal_queue)
            status["document_processing_queue_length"] = len(normal_queue)
            status["large_document_processing_queue_length"] = len(large_queue)
            status["verification_queue_length"] = len(verify_queue)
            status["deep_enrichment_queue_length"] = len(deep_queue)
        except Exception as e:
            status["redis_queue_length"] = 0
            status["queue_error"] = str(e)
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

@app.get("/new", response_class=HTMLResponse)
async def new_landing_page(request: Request):
    stats = stats_tracker.get_stats(detailed=False)
    total_stats = stats.get("total_stats", {})

    return templates.TemplateResponse(
        "new_index.html",
        {
            "request": request,
            "total_uploads": total_stats.get("total_uploads", 0),
            "total_references_checked": total_stats.get("total_references_checked", 0),
            "total_verifications": total_stats.get("total_verifications", 0),
            "success_rate": total_stats.get("success_rate", 0),
        }
    )

@app.get("/new/analyse", response_class=HTMLResponse)
async def new_analyse_page(request: Request):
    return templates.TemplateResponse("new_analyse.html", {"request": request})


@app.get("/analyse", response_class=HTMLResponse)
async def analyse_page(request: Request):
    return templates.TemplateResponse("new_analyse.html", {"request": request})


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    return templates.TemplateResponse("new_analyse.html", {"request": request})
@app.get("/results/{job_id}", response_class=HTMLResponse)
async def results_dashboard_page(request: Request, job_id: str, verify: int = 0):
    return templates.TemplateResponse(
        "new_results.html",
        {
            "request": request,
            "job_id": job_id,
            "auto_verify": "true" if verify == 1 else "false"
        }
    )


@app.get("/new/results/{job_id}", response_class=HTMLResponse)
async def new_results_dashboard_page(request: Request, job_id: str, verify: int = 0):
    return templates.TemplateResponse(
        "new_results.html",
        {
            "request": request,
            "job_id": job_id,
            "auto_verify": "true" if verify == 1 else "false"
        }
    )

# ============================================================
# STYLE PASSING HELPERS FOR ONLINE VERIFICATION
# ============================================================
def _main_style_token(style_value):
    """Return the canonical style family that should be sent to worker.process_verification."""
    s = str(style_value or "").strip().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[\s\-/]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")

    aliases = {
        "": "apa",
        "auto": "apa",
        "author_year": "author_year",
        "author_year_apa_harvard_chicago": "author_year",
        "apa": "apa",
        "apa7": "apa",
        "apa_7": "apa",
        "harvard": "harvard",
        "chicago": "chicago_author_date",
        "chicago_author_date": "chicago_author_date",
        "apa_harvard_chicago": "author_year",
        "apa_harvard_chicago_author_date": "author_year",
        "ieee": "numeric_square",
        "ieee_square": "numeric_square",
        "ieee_square_bracket": "numeric_square",
        "numeric_square": "numeric_square",
        "numeric_square_bracket": "numeric_square",
        "numeric_square_bracket_ieee_vancouver_nlm_elsevier_springer": "numeric_square",
        "vancouver": "numeric_square",
        "vancouver_square": "numeric_square",
        "vancouver_square_bracket": "numeric_square",
        "nlm": "numeric_square",
        "nlm_square": "numeric_square",
        "elsevier_square": "numeric_square",
        "elsevier_numbered_square_bracket": "numeric_square",
        "springer_square": "numeric_square",
        "springer_numbered_square_bracket": "numeric_square",
        "ama": "numeric_superscript",
        "ama_superscript": "numeric_superscript",
        "nature": "numeric_superscript",
        "nature_superscript": "numeric_superscript",
        "rsc": "numeric_superscript",
        "rsc_superscript": "numeric_superscript",
        "acs": "numeric_superscript",
        "acs_superscript": "numeric_superscript",
        "elsevier_superscript": "numeric_superscript",
        "numeric_superscript": "numeric_superscript",
        "numeric_superscript_ama_nature_rsc_acs_elsevier": "numeric_superscript",
        "vancouver_round": "numeric_round",
        "acs_round": "numeric_round",
        "numeric_round": "numeric_round",
        "numeric_round_bracket": "numeric_round",
        "numeric_round_bracket_vancouver_acs": "numeric_round",
    }

    if s in aliases:
        return aliases[s]

    # Also handle full human labels passed from the browser.
    if "numeric" in s and "square" in s:
        return "numeric_square"
    if "ieee" in s or "vancouver" in s or "nlm" in s or "springer" in s:
        if "round" in s:
            return "numeric_round"
        return "numeric_square"
    if "superscript" in s or "ama" in s or "nature" in s or "rsc" in s or "acs" in s:
        return "numeric_superscript"
    if "round" in s:
        return "numeric_round"
    if "author" in s or "apa" in s or "harvard" in s or "chicago" in s:
        return "author_year"

    return s or "apa"


def _style_for_job_result(result):
    """Recover the selected analysis style from the saved result object."""
    result = result or {}
    summary = result.get("summary") or {}
    candidates = [
        result.get("selected_style"),
        result.get("style"),
        result.get("style_family"),
        result.get("citation_style"),
        summary.get("selected_style"),
        summary.get("style"),
        summary.get("style_family"),
        summary.get("citation_style"),
    ]
    for value in candidates:
        if str(value or "").strip():
            return _main_style_token(value)
    return "apa"


def _recovery_style_hint(selected_style):
    selected_style = _main_style_token(selected_style)
    return "numeric" if selected_style.startswith("numeric_") else "apa"

# ============================================================
# ASYNC DOCUMENT CHECK (QUEUED)
# ============================================================

@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("auto"),
    enable_autofix: str = Form("false"),  # CHANGE: Use str instead of bool
    enable_online_verification: str = Form("false"),  # CHANGE: Use str instead of bool
    request: Request = None
):
    # Convert string to boolean
    autofix_enabled = enable_autofix.lower() == "true"
    online_verify_enabled = enable_online_verification.lower() == "true"
    
    print(f"📚 Received citation style: {style}")
    print(f"📋 Received enable_autofix string: {enable_autofix}")
    print(f"📋 Converted to bool: {autofix_enabled}")
    print(f"📋 Received enable_online_verification: {online_verify_enabled}")
    
    # =========================
    # 1. VALIDATION
    # =========================
    if not file.filename:
        return JSONResponse(
            status_code=400,
            content={"error": "No file provided", "message": "Please select a file to upload"}
        )
    
    filename_lower = (file.filename or "").lower().strip()
    is_docx = filename_lower.endswith(".docx")
    is_pdf = filename_lower.endswith(".pdf")

    if not (is_docx or is_pdf):
        return JSONResponse(
            status_code=400,
            content={
                "error": "Invalid file format",
                "message": "Only DOCX and PDF files are accepted",
                "instruction": (
                    "Please upload a .docx file for best accuracy or a text-based .pdf file. "
                    "Scanned or image-based PDFs may produce incomplete results."
                )
            }
        )

    if is_pdf:
        print(f"[PDF UPLOAD] Accepted PDF for cautious analysis: {file.filename}")
    
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

    # Preflight routing: use page count/word count, not only file size.
    load_info = estimate_document_load(data, file.filename)
    queue_name = load_info.get("queue_name", NORMAL_DOCUMENT_QUEUE)
    is_large_file = bool(load_info.get("large_file", False))
    file_ttl = LARGE_FILE_REDIS_TTL if is_large_file else NORMAL_FILE_REDIS_TTL
    job_timeout = LARGE_DOCUMENT_JOB_TIMEOUT if is_large_file else NORMAL_DOCUMENT_JOB_TIMEOUT

    print(
        f"[PREFLIGHT] {file.filename} -> queue={queue_name}, "
        f"large_file={is_large_file}, reason={load_info.get('route_reason')}"
    )

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

    redis_conn.setex(f"file:{job_id}", file_ttl, data)
    print(f"✅ File stored in Redis for job {job_id} with ttl={file_ttl}s")

    # =========================
    # 5. STORE JOB IN DATABASE
    # =========================
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()

            preflight_payload = {
                "preflight": load_info,
                "queue_name": queue_name,
                "large_file": is_large_file
            }

            cursor.execute("""
                INSERT INTO jobs (job_id, status, file_name, file_size_mb, result, created_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
            """, (job_id, "queued", file.filename, file_size_mb, json.dumps(preflight_payload)))

            conn.commit()
            cursor.close()
            conn.close()

            print(f"✅ Job {job_id} stored in PostgreSQL")

        except Exception as db_error:
            print(f"⚠️ Database error: {db_error}")

    # =========================
    # 6. ENQUEUE JOB
    # =========================
    if not redis_conn:
        return JSONResponse(
            status_code=500,
            content={"error": "Queue not initialized"}
        )

    try:
        target_queue = Queue(queue_name, connection=redis_conn)

        # 🔥 FIX: Use the autofix_enabled variable instead of hardcoded True
        target_queue.enqueue(
            "worker.process_document",
            job_id,
            file.filename,
            style,
            autofix_enabled,  # CHANGE: Use the variable, not hardcoded True
            job_timeout=job_timeout,
            result_ttl=86400,
            failure_ttl=86400
        )

        print(
            f"🔥 Job {job_id} queued successfully on {queue_name} "
            f"with autofix={autofix_enabled}, timeout={job_timeout}s"
        )

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
        "message": (
            "Large document detected and queued for temporary large-file processing."
            if is_large_file else
            "Document queued. Poll /job/{job_id} for status."
        ),
        "file_name": file.filename,
        "file_type": "pdf" if is_pdf else "docx",
        "file_size_mb": file_size_mb,
        "queue": queue_name,
        "large_file": is_large_file,
        "route_reason": load_info.get("route_reason"),
        "page_count": load_info.get("page_count"),
        "word_count": load_info.get("word_count"),
        "pdf_caution": (
            "PDF accepted for cautious analysis. Text-based PDFs work best. DOCX remains recommended for the most accurate citation analysis."
            if is_pdf else ""
        ),
        "autofix_enabled": autofix_enabled,  # Include for debugging
        "selected_style": _main_style_token(style)
    }


# ============================================================
# MANUAL VERIFICATION ENDPOINTS
# ============================================================

def _manual_safe_json_loads(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return value


def _manual_load_result(job_id: str) -> Dict[str, Any]:
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.get("result") or {}


def _manual_save_result(job_id: str, result: Dict[str, Any]):
    if DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE jobs SET result = %s::jsonb WHERE job_id = %s",
                (json.dumps(result), job_id)
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    if redis_conn:
        try:
            redis_conn.setex(f"result:{job_id}", 3600, json.dumps(result))
        except Exception as e:
            print(f"[MANUAL VERIFY] Redis refresh failed: {e}")


def _manual_norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _manual_reference_key(text: str) -> str:
    text = _manual_norm(text).lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:260]


def _manual_candidate_url(source: str, item: Dict[str, Any], doi: str = "") -> str:
    if doi:
        return f"https://doi.org/{doi.replace('https://doi.org/', '').strip()}"
    if source == "openalex":
        loc = item.get("primary_location") or {}
        return loc.get("landing_page_url") or item.get("id") or ""
    if source == "crossref":
        return item.get("URL") or ""
    if source == "datacite":
        attrs = item.get("attributes") or {}
        return attrs.get("url") or ""
    if source == "semantic_scholar":
        return item.get("url") or ""
    return ""


def _manual_candidate_from_source(source: str, item: Dict[str, Any], query: str) -> Optional[Dict[str, Any]]:
    title = ""
    year = ""
    authors = []
    doi = ""
    score = 0

    if source == "crossref":
        title_val = item.get("title") or []
        title = title_val[0] if isinstance(title_val, list) and title_val else str(title_val or "")
        for key in ("published-print", "published-online", "issued", "created"):
            parts = ((item.get(key) or {}).get("date-parts") or [])
            if parts and parts[0]:
                year = str(parts[0][0])
                break
        for au in item.get("author") or []:
            name = " ".join(x for x in [au.get("given"), au.get("family")] if x).strip()
            if name:
                authors.append(name)
        doi = str(item.get("DOI") or "").strip()
        score = item.get("score") or 0

    elif source == "openalex":
        title = item.get("title") or item.get("display_name") or ""
        year = str(item.get("publication_year") or "")
        for auth in item.get("authorships") or []:
            au = auth.get("author") or {}
            if au.get("display_name"):
                authors.append(au.get("display_name"))
        doi = str(item.get("doi") or "").replace("https://doi.org/", "").strip()
        score = item.get("relevance_score") or 0

    elif source == "datacite":
        attrs = item.get("attributes") or {}
        titles = attrs.get("titles") or []
        title = (titles[0] or {}).get("title", "") if titles else ""
        year = str(attrs.get("publicationYear") or "")
        for cr in attrs.get("creators") or []:
            if cr.get("name"):
                authors.append(cr.get("name"))
        doi = str(attrs.get("doi") or item.get("id") or "").strip()
        score = item.get("score") or 0

    elif source == "semantic_scholar":
        title = item.get("title") or ""
        year = str(item.get("year") or "")
        authors = [a.get("name") for a in (item.get("authors") or []) if a.get("name")]
        doi = str(((item.get("externalIds") or {}).get("DOI")) or "").strip()
        score = item.get("citationCount") or 0

    title = _manual_norm(title)
    if not title:
        return None
    return {
        "title": title,
        "year": year,
        "authors": authors[:8],
        "doi": doi,
        "url": _manual_candidate_url(source, item, doi),
        "source": source,
        "match_score": score,
        "query_used": query,
        "review_required": True,
    }


def _manual_get_json(url: str, timeout: int = 12) -> Dict[str, Any]:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "CiteIntegrity/1.0 manual-verification", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


@app.post("/api/manual-search")
async def api_manual_search(request: Request):
    payload = await request.json()
    job_id = str(payload.get("job_id") or "").strip()
    query = _manual_norm(payload.get("query") or "")
    sources = payload.get("sources") or ["openalex", "crossref", "semantic_scholar", "datacite"]
    rows_per_source = int(payload.get("rows_per_source") or 5)

    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")
    if len(query) < 3:
        raise HTTPException(status_code=400, detail="Manual search query is too short")

    # Confirms the job exists before running external searches.
    _manual_load_result(job_id)

    import urllib.parse
    candidates = []
    errors = []

    for source in sources:
        try:
            if source == "openalex":
                url = "https://api.openalex.org/works?" + urllib.parse.urlencode({"search": query, "per-page": str(rows_per_source)})
                data = _manual_get_json(url)
                items = data.get("results") or []
            elif source == "crossref":
                url = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query.bibliographic": query, "rows": str(rows_per_source)})
                data = _manual_get_json(url)
                items = ((data.get("message") or {}).get("items") or [])
            elif source == "datacite":
                url = "https://api.datacite.org/dois?" + urllib.parse.urlencode({"query": query, "page[size]": str(rows_per_source)})
                data = _manual_get_json(url)
                items = data.get("data") or []
            elif source == "semantic_scholar":
                url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode({"query": query, "limit": str(rows_per_source), "fields": "title,year,authors,externalIds,url,citationCount"})
                data = _manual_get_json(url)
                items = data.get("data") or []
            else:
                continue

            for item in items:
                cand = _manual_candidate_from_source(source, item, query)
                if cand:
                    candidates.append(cand)
        except Exception as e:
            errors.append({"source": source, "error": str(e)})

    seen = set()
    clean = []
    for c in candidates:
        key = _manual_reference_key((c.get("doi") or "") + " " + (c.get("title") or "") + " " + str(c.get("year") or ""))
        if key in seen:
            continue
        seen.add(key)
        clean.append(c)
        if len(clean) >= rows_per_source * max(1, len(sources)):
            break

    return {"ok": True, "query": query, "candidates": clean, "errors": errors}


@app.post("/api/manual-verify/decision")
async def api_manual_verify_decision(request: Request):
    payload = await request.json()
    job_id = str(payload.get("job_id") or "").strip()
    reference = _manual_norm(payload.get("reference") or "")
    decision = str(payload.get("decision") or "").strip().lower()
    candidate = payload.get("candidate") or {}
    note = _manual_norm(payload.get("note") or "")

    allowed = {"manual_verified", "manual_not_verified", "not_indexed_but_plausible", "keep_needs_review"}
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")
    if decision not in allowed:
        raise HTTPException(status_code=400, detail="Invalid manual verification decision")
    if not reference and not candidate:
        raise HTTPException(status_code=400, detail="Reference or candidate is required")

    result = _manual_load_result(job_id)
    ref_key = _manual_reference_key(reference or candidate.get("title") or candidate.get("doi") or "")
    stamp = datetime.utcnow().isoformat()
    label_map = {
        "manual_verified": "Verified",
        "manual_not_verified": "Not verified",
        "not_indexed_but_plausible": "Plausible",
        "keep_needs_review": "Needs review",
    }

    decision_payload = {
        "manual_decision": decision,
        "manual_decision_label": label_map.get(decision, decision.replace("_", " ").title()),
        "manual_verified_at": stamp,
        "manual_verification_note": note,
        "manual_candidate": candidate,
        "manual_reference_key": ref_key,
    }

    def maybe_apply(row):
        if not isinstance(row, dict):
            return False
        candidates = [
            row.get("reference"), row.get("original_reference"), row.get("matched_reference"),
            row.get("matched_title"), row.get("title"), row.get("source_title")
        ]
        row_key = _manual_reference_key(" ".join(str(x or "") for x in candidates))
        if ref_key and (ref_key in row_key or row_key in ref_key):
            row.update(decision_payload)
            row["review_required"] = decision != "manual_verified"
            return True
        return False

    touched = 0
    for row in ((result.get("online_verification") or {}).get("rows") or []):
        if maybe_apply(row):
            touched += 1

    recovery = result.get("recovery") or {}
    for section in ("verification_recovery", "missing_recovery"):
        for row in recovery.get(section) or []:
            if maybe_apply(row):
                touched += 1

    manual = result.setdefault("manual_verification", {})
    decisions = manual.setdefault("decisions", [])

    # Upsert by normalised reference key so repeated manual actions do not create
    # duplicate records for the same unresolved reference in the Manual Verification panel.
    decision_record = {
        "reference": reference,
        "decision": decision,
        "candidate": candidate,
        "note": note,
        "recorded_at": stamp,
        "manual_reference_key": ref_key,
    }

    replaced = False
    for i, existing in enumerate(list(decisions)):
        existing_key = existing.get("manual_reference_key") or _manual_reference_key(
            existing.get("reference") or (existing.get("candidate") or {}).get("title") or ""
        )
        if ref_key and existing_key == ref_key:
            decisions[i] = decision_record
            replaced = True
            break

    if not replaced:
        decisions.append(decision_record)

    decisions.sort(key=lambda d: _manual_reference_key(d.get("reference") or (d.get("candidate") or {}).get("title") or ""))
    manual["last_updated"] = stamp
    manual["decision_count"] = len(decisions)

    _manual_save_result(job_id, result)
    return {"ok": True, "message": f"Manual decision recorded: {decision_payload['manual_decision_label']}", "updated_rows": touched, "result": result}

# ============================================================
# RESULT CHECK ENDPOINT
# ============================================================

@app.get("/result/{job_id}")
async def get_result(job_id: str, fresh: int = 0):
    """Get job status and result.

    Use fresh=1 when the browser is polling for enrichment updates, so
    PostgreSQL is read directly instead of returning a possibly stale Redis value.
    """
    
    # Check Redis cache first unless a fresh PostgreSQL read is requested
    if redis_conn and not fresh:
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
    job = load_job_record_fresh(job_id)
    if not job:
        return {"status": "not_found"}

    return {
        "status": job.get("status", "unknown"),
        "result": job.get("result"),
        "verification": job.get("verification", {})
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
    selected_style = _style_for_job_result(result)
    print(f"[VERIFY ONLINE] Queuing verification for job {job_id} with selected_style={selected_style}")

    existing_rows = ((result.get("online_verification") or {}).get("rows") or [])

    if verification.get("state") == "completed" and existing_rows:
        return {
            "started": False,
            "message": "Verification already completed",
            "job_id": job_id,
            "completed": True,
            "total_references": len(existing_rows)
        }

    if verification.get("state") in {"queued", "running"}:
        return {
            "started": True,
            "success": True,
            "already_running": True,
            "message": "Verification already in progress",
            "job_id": job_id,
            "progress": verification.get("progress", 0),
            "total_references": verification.get("total", 0),
            "state": verification.get("state")
        }

    refs = result.get("references_raw", []) or []

    if not refs:
        repaired = recover_references_for_verification(
            result.get("main_text", ""),
            style_hint=_recovery_style_hint(selected_style)
        )

        if repaired:
            refs = repaired
            result["references_raw"] = repaired
            result.setdefault("summary", {})["reference_entries_found"] = len(repaired)

            with _lock:
                if job_id in _store:
                    _store[job_id].setdefault("result", {})
                    _store[job_id]["result"]["references_raw"] = repaired
                    _store[job_id]["result"].setdefault("summary", {})["reference_entries_found"] = len(repaired)

            if DATABASE_URL:
                try:
                    conn = psycopg2.connect(DATABASE_URL)
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        UPDATE jobs
                        SET result = %s::jsonb
                        WHERE job_id = %s
                        """,
                        (json.dumps(result), job_id)
                    )
                    conn.commit()
                    cursor.close()
                    conn.close()
                except Exception as e:
                    print(f"[VERIFY ONLINE] Could not persist repaired references: {e}")

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

    if not verification_queue:
        raise HTTPException(500, "Verification queue not initialized")
    
    new_verification_job_id = f"verify:{job_id}:{uuid.uuid4().hex[:8]}"
    
    update_verification_status(
        job_id,
        verification_job_id=new_verification_job_id,
        rq_job_id=new_verification_job_id,
        state="queued",
        total=len(refs),
        progress=0,
        percentage=0,
        started_at=now(),
        message="Verification re-queued"
    )
    
    verification_queue.enqueue(
        "worker.process_verification",
        job_id,
        selected_style,
        False,
        job_id=new_verification_job_id,
        job_timeout=10800,
        result_ttl=86400,
        failure_ttl=86400
    )
    
    return {
        "started": True,
        "success": True,
        "message": "Verification queued successfully",
        "job_id": job_id,
        "verification_job_id": new_verification_job_id,
        "total_references": len(refs),
        "state": "queued"
    }
# ============================================================
# STATUS POLLING
# ============================================================

@app.get("/online/status")
def online_status(job_id: str):
    try:
        job = load_job_record_fresh(job_id)

        if not job:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "Job not found",
                    "job_id": job_id,
                    "message": f"No job found with ID {job_id}",
                    "timestamp": now()
                }
            )

        result = job.get("result", {}) or {}
        verification = result.get("verification") or job.get("verification") or {}

        rq_job_id = verification.get("rq_job_id") or verification.get("verification_job_id")

        if rq_job_id and redis_conn and verification.get("state") in {"queued", "running", "finalising"}:
            try:
                from rq.job import Job

                rq_job = Job.fetch(rq_job_id, connection=redis_conn)
                rq_status = rq_job.get_status(refresh=True)

                verification["rq_status"] = rq_status

                if rq_status == "queued":
                    verification["state"] = "queued"
                    verification["message"] = "Verification job is queued and waiting for the worker"

                elif rq_status in {"started", "deferred"}:
                    verification["state"] = "running"
                    verification["message"] = "Verification running"

                elif rq_status == "finished":
                    fresh_job = load_job_record_fresh(job_id)
                    fresh_result = (fresh_job or {}).get("result", {}) or {}
                    fresh_verification = fresh_result.get("verification") or verification

                    final_tables_ready = (
                        fresh_verification.get("final_tables_ready") is True
                        or fresh_result.get("final_tables_ready") is True
                        or bool(fresh_result.get("verification_completed_at"))
                    )

                    if final_tables_ready:
                        result = fresh_result
                        verification = fresh_verification
                        verification["state"] = "completed"
                        verification["message"] = "Verification complete"
                    else:
                        verification["state"] = "finalising"
                        verification["message"] = "Verification rows are complete. Waiting for Recovery and Claim Support tables..."

                elif rq_status == "failed":
                    verification["state"] = "error"
                    verification["message"] = "Verification worker failed"
                    verification["error"] = str(rq_job.exc_info or "Unknown worker error")
                    verification["completed_at"] = now()

                update_verification_status(job_id, **verification)

            except Exception as e:
                verification["rq_status_error"] = str(e)

        online_verification = result.get("online_verification") or {}
        rows = online_verification.get("rows") or []

        progress = verification.get("progress", 0)
        total = verification.get("total", 0)

        if rows and progress < len(rows):
            progress = len(rows)

        if rows and not total:
            total = len(rows)

        percentage = verification.get("percentage", 0)

        if total:
            percentage = int((progress / max(total, 1)) * 100)

        result["verification"] = verification

        state = verification.get("state", "idle")

        final_tables_ready = (
            verification.get("final_tables_ready") is True
            or result.get("final_tables_ready") is True
            or bool(result.get("verification_completed_at"))
        )

        response = {
            "job_id": job_id,
            "online": {
                "state": state,
                "status": state,
                "progress": progress,
                "total": total,
                "percentage": percentage,
                "message": verification.get("message", ""),
                "verification_job_id": verification.get("verification_job_id"),
                "rq_job_id": verification.get("rq_job_id"),
                "rq_status": verification.get("rq_status"),
                "rq_status_error": verification.get("rq_status_error"),
                "error": verification.get("error"),
                "started_at": verification.get("started_at"),
                "completed_at": verification.get("completed_at"),
                "last_heartbeat": verification.get("last_heartbeat"),
                "results_count": verification.get("results_count", len(rows)),
                "has_results": len(rows) > 0,
                "final_tables_ready": final_tables_ready,
                "recovery_missing": len((result.get("recovery") or {}).get("missing_recovery") or []),
                "recovery_verify": len((result.get("recovery") or {}).get("verification_recovery") or []),
                "claim_support_rows": len(result.get("claim_support") or []),
                "c2r_rows": len(result.get("reconciliation_intext_to_reference") or []),
            }
        }

        if (
            rows
            or state in {"completed", "finalising"}
            or final_tables_ready
            or result.get("recovery")
            or result.get("claim_support")
        ):
            response["result"] = result

        return JSONResponse(content=response)

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "job_id": job_id,
                "online": {
                    "state": "error",
                    "status": "error",
                    "message": "Online status endpoint failed",
                    "error": str(e),
                    "progress": 0,
                    "total": 0,
                    "percentage": 0
                }
            }
        )
@app.post("/api/enrichment/start/{job_id}")
async def start_advanced_enrichment(job_id: str, request: Request):
    """
    Start advanced enrichment only when the user requests it.
    This queues deep recovery and claim-support enrichment without blocking the dashboard.
    """
    if not redis_conn:
        return JSONResponse(
            {"ok": False, "error": "Redis is not available."},
            status_code=500
        )

    if not DATABASE_URL:
        return JSONResponse(
            {"ok": False, "error": "Database is not available."},
            status_code=500
        )

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    scope = payload.get("scope", "weak_only")

    allowed_scopes = {
        "weak_only",
        "recovery_only",
        "claim_only",
        "citation_needed_only",
        "all_problem_rows",
    }

    if scope not in allowed_scopes:
        scope = "weak_only"

    job = load_job_record_fresh(job_id)

    if not job:
        return JSONResponse(
            {"ok": False, "error": "Job not found."},
            status_code=404
        )

    result = job.get("result") or {}

    enrichment = result.get("enrichment") or {}
    current_state = str(enrichment.get("state", "")).lower()

    if current_state in {"queued", "running"}:
        return {
            "ok": True,
            "message": "Advanced enrichment is already running.",
            "state": current_state,
            "rq_job_id": enrichment.get("rq_job_id"),
            "scope": enrichment.get("scope", scope),
        }

    deep_queue = Queue("deep_enrichment", connection=redis_conn)

    rq_job = deep_queue.enqueue(
        "worker.process_deep_enrichment",
        job_id,
        "apa",
        scope,
        job_timeout=10800,
        result_ttl=86400,
        failure_ttl=86400,
    )

    result["enrichment"] = {
        "state": "queued",
        "scope": scope,
        "rq_job_id": rq_job.id,
        "progress": 0,
        "total": None,
        "message": "Advanced enrichment queued.",
        "requested_at": datetime.utcnow().isoformat(),
        "deep_recovery_ready": False,
        "deep_claim_support_ready": False,
    }

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            UPDATE jobs
            SET result = %s::jsonb
            WHERE job_id = %s
            """,
            (json.dumps(result), job_id)
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    try:
        redis_conn.setex(f"result:{job_id}", 3600, json.dumps(result))
    except Exception as e:
        print(f"[ENRICHMENT START] Could not update Redis cache: {e}")

    return {
        "ok": True,
        "message": "Advanced enrichment queued.",
        "rq_job_id": rq_job.id,
        "scope": scope,
    }

@app.get("/debug/enrichment-counts/{job_id}")
async def debug_enrichment_counts(job_id: str):
    """Return counts that confirm whether advanced enrichment reached the UI payload."""
    job = load_job_record_fresh(job_id)
    if not job:
        return {"ok": False, "error": "Job not found"}

    result = job.get("result") or {}
    recovery = result.get("recovery") or {}
    missing_rows = recovery.get("missing_recovery") or []
    verification_rows = recovery.get("verification_recovery") or []
    claim_rows = result.get("claim_support") or []

    return {
        "ok": True,
        "job_id": job_id,
        "enrichment": result.get("enrichment") or {},
        "missing_recovery_rows": len(missing_rows),
        "verification_recovery_rows": len(verification_rows),
        "claim_support_rows": len(claim_rows),
        "missing_deep_source_count": sum(len(r.get("deep_suggestions") or r.get("suggestions") or []) for r in missing_rows),
        "verification_deep_source_count": sum(len(r.get("deep_suggestions") or r.get("suggestions") or []) for r in verification_rows),
        "claim_alternative_source_count": sum(len(r.get("alternative_sources") or r.get("deep_suggestions") or r.get("suggestions") or []) for r in claim_rows),
        "sample_missing_recovery": missing_rows[:1],
        "sample_verification_recovery": verification_rows[:1],
        "sample_claim_support": claim_rows[:1],
    }
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

def _dt_to_iso(value):
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _as_dict(row):
    """Convert psycopg2/SQLite rows to plain dictionaries."""
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return {}


def _safe_json(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return {}


def _safe_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _first_existing(result: Dict[str, Any], keys: List[str], default=None):
    for key in keys:
        if key in result and result.get(key) is not None:
            return result.get(key)
    return default


def _count_list(value) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        # Some engines return dict rows keyed by citation/reference.
        return len(value)
    return 0


def _extract_acii_score(result: Dict[str, Any]):
    acii = result.get("acii")
    if isinstance(acii, (int, float)):
        return round(float(acii), 2)
    if isinstance(acii, dict):
        for key in ["score", "acii_score", "overall_score", "total_score", "value"]:
            if key in acii:
                return round(_safe_float(acii.get(key)), 2)
    return None


def _extract_job_metrics(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract dashboard-safe metrics from a job result.

    This deliberately returns counts and status summaries only. It does not expose
    manuscript text, full references, student names or private thesis content.
    """
    result = result or {}

    refs = _first_existing(result, [
        "references_raw", "references", "reference_entries", "all_references"
    ], [])
    c2r = _first_existing(result, [
        "reconciliation_intext_to_reference", "citations_to_references", "c2r"
    ], [])
    r2c = _first_existing(result, [
        "reconciliation_reference_to_intext", "references_to_citations", "r2c"
    ], [])
    citations = _first_existing(result, [
        "citations_raw", "in_text_citations", "intext_citations", "citation_occurrences"
    ], [])
    missing = _first_existing(result, [
        "missing_in_references", "missing_citations", "missing_references"
    ], [])
    uncited = _first_existing(result, [
        "uncited_references", "uncited_reference_rows", "references_not_cited"
    ], [])
    claims = _first_existing(result, ["claim_support", "claim_support_rows"], [])
    suggestions = _first_existing(result, ["suggestions"], [])

    online = result.get("online_verification") or {}
    verification_rows = online.get("rows") or []
    verification_summary = online.get("summary") or {}

    verified = _safe_int(verification_summary.get("verified"))
    likely = _safe_int(verification_summary.get("likely"))
    needs_review = _safe_int(verification_summary.get("needs_review"))
    not_found = _safe_int(verification_summary.get("not_found"))
    offline = _safe_int(verification_summary.get("offline"))

    # If no summary exists, count statuses directly from verification rows.
    if verification_rows and not any([verified, likely, needs_review, not_found, offline]):
        for row in verification_rows:
            if not isinstance(row, dict):
                continue
            status = (row.get("status") or "offline").lower()
            if status == "verified":
                verified += 1
            elif status == "likely":
                likely += 1
            elif status == "needs_review":
                needs_review += 1
            elif status == "not_found":
                not_found += 1
            else:
                offline += 1

    references_count = _count_list(refs)
    citations_count = _count_list(citations)
    if citations_count == 0:
        citations_count = _count_list(c2r)

    recovery = result.get("recovery") or {}
    recovery_count = 0
    if isinstance(recovery, dict):
        recovery_count = _count_list(recovery.get("missing_recovery")) + _count_list(recovery.get("verification_recovery"))

    return {
        "references_count": references_count,
        "citations_count": citations_count,
        "missing_citations_count": _count_list(missing),
        "uncited_references_count": _count_list(uncited),
        "c2r_rows": _count_list(c2r),
        "r2c_rows": _count_list(r2c),
        "claim_rows": _count_list(claims),
        "suggestions_count": _count_list(suggestions),
        "recovery_rows": recovery_count,
        "verification_rows": _count_list(verification_rows),
        "verified": verified,
        "likely": likely,
        "needs_review": needs_review,
        "not_found": not_found,
        "offline": offline,
        "acii_score": _extract_acii_score(result),
    }


def _empty_dashboard_payload(days: int, message: str = "No data available yet.") -> Dict[str, Any]:
    return {
        "total_stats": {
            "total_uploads": 0,
            "total_processed": 0,
            "total_failed": 0,
            "success_rate": 0,
            "total_references_checked": 0,
            "total_intext_citations": 0,
            "total_missing_citations": 0,
            "total_uncited_references": 0,
            "total_verifications": 0,
            "average_processing_time": 0,
            "average_acii_score": None,
            "start_date": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
            "storage_backend": stats_tracker.db_type,
            "persistent_storage": stats_tracker.db_type == "postgresql",
            "storage_message": message,
        },
        "dashboard_metrics": {},
        "daily_stats": {},
        "recent_uploads": [],
        "system_info": {
            "current_time": datetime.now().isoformat(),
            "storage_backend": stats_tracker.db_type,
            "persistent_storage": stats_tracker.db_type == "postgresql",
            "active_jobs": 0,
            "total_jobs": 0,
            "queue_status": {},
            "server_busy": False,
        }
    }


def _build_postgres_dashboard_stats(days: int = 30) -> Dict[str, Any]:
    """
    Deployment-safe dashboard builder.

    IMPORTANT FIX:
    The earlier dashboard loaded jobs.result for thousands of jobs. Some result
    JSON objects contain full extracted manuscript text, references and tables,
    which can make /private-stats heavy enough to return HTTP 502 on Render.

    This version reads persistent PostgreSQL tables first and uses SQL-level JSONB
    counts only when possible. It never returns or transfers full manuscript text.
    Dashboard data remains persistent because it is read from PostgreSQL, not
    from in-memory variables or /tmp SQLite storage.
    """
    days = max(1, min(int(days or 30), 365))
    cutoff_date = (datetime.now() - timedelta(days=days - 1)).date()

    # Defaults keep the endpoint returning HTTP 200 even if optional metrics fail.
    stats_row = {}
    job_summary = {
        "total_jobs": 0,
        "completed_jobs": 0,
        "failed_jobs": 0,
        "running_jobs": 0,
        "queued_jobs": 0,
        "first_created_at": None,
        "last_updated_at": None,
    }
    daily_stats: Dict[str, Dict[str, Any]] = {}
    recent_uploads: List[Dict[str, Any]] = []
    advanced = {
        "references_count": 0,
        "citations_count": 0,
        "missing_citations_count": 0,
        "uncited_references_count": 0,
        "claim_rows": 0,
        "recovery_rows": 0,
        "suggestions_count": 0,
        "verification_rows": 0,
        "verified": 0,
        "likely": 0,
        "needs_review": 0,
        "not_found": 0,
        "offline": 0,
        "average_acii_score": None,
        "average_processing_time": 0,
        "error": None,
    }

    # ------------------------------------------------------------------
    # 1) Fast persistent totals from normal relational tables.
    # ------------------------------------------------------------------
    with psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, connect_timeout=10) as conn:
        with conn.cursor() as cursor:
            # Keep dashboard queries fast. If a query exceeds this, the endpoint
            # will fall back instead of causing a platform 502.
            cursor.execute("SET LOCAL statement_timeout = '8000ms'")

            cursor.execute("SELECT * FROM stats WHERE id = 1")
            stats_row = _as_dict(cursor.fetchone() or {})

            cursor.execute("""
                SELECT
                    COUNT(*) AS total_jobs,
                    COUNT(*) FILTER (
                        WHERE lower(coalesce(status, '')) IN ('completed','complete','done','success','finished')
                    ) AS completed_jobs,
                    COUNT(*) FILTER (
                        WHERE lower(coalesce(status, '')) IN ('failed','error','cancelled','canceled') OR error IS NOT NULL
                    ) AS failed_jobs,
                    COUNT(*) FILTER (
                        WHERE lower(coalesce(status, '')) IN ('queued','started','running','processing','deferred','scheduled')
                    ) AS running_jobs,
                    COUNT(*) FILTER (
                        WHERE lower(coalesce(status, '')) = 'queued'
                    ) AS queued_jobs,
                    MIN(created_at) AS first_created_at,
                    MAX(COALESCE(completed_at, started_at, created_at)) AS last_updated_at
                FROM jobs
            """)
            job_summary = _as_dict(cursor.fetchone() or job_summary)

            cursor.execute("""
                SELECT AVG(processing_time) AS avg_processing_time
                FROM uploads
                WHERE success = 1 AND processing_time IS NOT NULL AND processing_time > 0
            """)
            avg_row = _as_dict(cursor.fetchone() or {})
            advanced["average_processing_time"] = round(_safe_float(avg_row.get("avg_processing_time")), 2)

            cursor.execute("""
                SELECT date, uploads, processed, failed, references_count,
                       total_processing_time, processing_count
                FROM daily_stats
                WHERE date >= %s
                ORDER BY date DESC
            """, (cutoff_date,))
            for raw in cursor.fetchall() or []:
                row = _as_dict(raw)
                date_key = str(row.get("date"))[:10]
                pcount = _safe_int(row.get("processing_count"))
                ptime = _safe_float(row.get("total_processing_time"))
                daily_stats[date_key] = {
                    "uploads": _safe_int(row.get("uploads")),
                    "processed": _safe_int(row.get("processed")),
                    "failed": _safe_int(row.get("failed")),
                    "references": _safe_int(row.get("references_count")),
                    "citations": 0,
                    "missing_citations": 0,
                    "uncited_references": 0,
                    "verification_rows": 0,
                    "average_acii_score": None,
                    "avg_processing_time": round(ptime / pcount, 2) if pcount else 0,
                }

            cursor.execute("""
                SELECT timestamp, filename, file_size, references_count,
                       processing_time, success, error
                FROM uploads
                ORDER BY timestamp DESC
                LIMIT 50
            """)
            for raw in cursor.fetchall() or []:
                row = _as_dict(raw)
                recent_uploads.append({
                    "timestamp": _dt_to_iso(row.get("timestamp")),
                    "filename": row.get("filename") or "Untitled document",
                    "file_size_mb": round(_safe_float(row.get("file_size")) / (1024 * 1024), 2),
                    "references_count": _safe_int(row.get("references_count")),
                    "citations_count": 0,
                    "missing_citations_count": 0,
                    "uncited_references_count": 0,
                    "verification_rows": 0,
                    "acii_score": None,
                    "processing_time": _safe_float(row.get("processing_time")),
                    "status": "completed" if row.get("success") else "failed",
                    "success": bool(row.get("success")),
                    "error": row.get("error"),
                })

            # If the uploads table is empty but the jobs table exists, still show
            # recent activity without touching the large jobs.result JSON.
            if not recent_uploads:
                cursor.execute("""
                    SELECT job_id, status, file_name, file_size_mb,
                           processing_time, error, created_at
                    FROM jobs
                    ORDER BY created_at DESC
                    LIMIT 50
                """)
                for raw in cursor.fetchall() or []:
                    row = _as_dict(raw)
                    status = (row.get("status") or "unknown").lower()
                    recent_uploads.append({
                        "timestamp": _dt_to_iso(row.get("created_at")),
                        "filename": row.get("file_name") or "Untitled document",
                        "file_size_mb": _safe_float(row.get("file_size_mb")),
                        "references_count": 0,
                        "citations_count": 0,
                        "missing_citations_count": 0,
                        "uncited_references_count": 0,
                        "verification_rows": 0,
                        "acii_score": None,
                        "processing_time": _safe_float(row.get("processing_time")),
                        "status": status,
                        "success": status in {'completed','complete','done','success','finished'} and not row.get("error"),
                        "error": row.get("error"),
                    })

    # ------------------------------------------------------------------
    # 2) Optional SQL-level JSONB counts. This does NOT transfer jobs.result
    #    into Python, so it avoids the 502 problem caused by huge JSON payloads.
    # ------------------------------------------------------------------
    try:
        with psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, connect_timeout=10) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '6000ms'")
                cursor.execute("""
                    SELECT
                        COALESCE(SUM(CASE WHEN jsonb_typeof(result->'references_raw') = 'array'
                            THEN jsonb_array_length(result->'references_raw') ELSE 0 END), 0) AS references_count,
                        COALESCE(SUM(CASE WHEN jsonb_typeof(result->'reconciliation_intext_to_reference') = 'array'
                            THEN jsonb_array_length(result->'reconciliation_intext_to_reference') ELSE 0 END), 0) AS citations_count,
                        COALESCE(SUM(CASE WHEN jsonb_typeof(result->'missing_in_references') = 'array'
                            THEN jsonb_array_length(result->'missing_in_references') ELSE 0 END), 0) AS missing_citations_count,
                        COALESCE(SUM(CASE WHEN jsonb_typeof(result->'uncited_references') = 'array'
                            THEN jsonb_array_length(result->'uncited_references') ELSE 0 END), 0) AS uncited_references_count,
                        COALESCE(SUM(CASE WHEN jsonb_typeof(result->'claim_support') = 'array'
                            THEN jsonb_array_length(result->'claim_support') ELSE 0 END), 0) AS claim_rows,
                        COALESCE(SUM(CASE WHEN jsonb_typeof(result->'suggestions') = 'array'
                            THEN jsonb_array_length(result->'suggestions') ELSE 0 END), 0) AS suggestions_count,
                        COALESCE(SUM(CASE WHEN jsonb_typeof(result->'online_verification'->'rows') = 'array'
                            THEN jsonb_array_length(result->'online_verification'->'rows') ELSE 0 END), 0) AS verification_rows,
                        COALESCE(SUM(CASE WHEN (result #>> '{online_verification,summary,verified}') ~ '^[0-9]+$'
                            THEN (result #>> '{online_verification,summary,verified}')::int ELSE 0 END), 0) AS verified,
                        COALESCE(SUM(CASE WHEN (result #>> '{online_verification,summary,likely}') ~ '^[0-9]+$'
                            THEN (result #>> '{online_verification,summary,likely}')::int ELSE 0 END), 0) AS likely,
                        COALESCE(SUM(CASE WHEN (result #>> '{online_verification,summary,needs_review}') ~ '^[0-9]+$'
                            THEN (result #>> '{online_verification,summary,needs_review}')::int ELSE 0 END), 0) AS needs_review,
                        COALESCE(SUM(CASE WHEN (result #>> '{online_verification,summary,not_found}') ~ '^[0-9]+$'
                            THEN (result #>> '{online_verification,summary,not_found}')::int ELSE 0 END), 0) AS not_found,
                        COALESCE(SUM(CASE WHEN (result #>> '{online_verification,summary,offline}') ~ '^[0-9]+$'
                            THEN (result #>> '{online_verification,summary,offline}')::int ELSE 0 END), 0) AS offline,
                        AVG(
                            CASE
                                WHEN jsonb_typeof(result->'acii') = 'number' THEN (result->>'acii')::numeric
                                WHEN jsonb_typeof(result->'acii') = 'object' AND COALESCE(result->'acii'->>'score', '') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (result->'acii'->>'score')::numeric
                                WHEN jsonb_typeof(result->'acii') = 'object' AND COALESCE(result->'acii'->>'acii_score', '') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (result->'acii'->>'acii_score')::numeric
                                WHEN jsonb_typeof(result->'acii') = 'object' AND COALESCE(result->'acii'->>'overall_score', '') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (result->'acii'->>'overall_score')::numeric
                                ELSE NULL
                            END
                        ) AS average_acii_score
                    FROM jobs
                    WHERE result IS NOT NULL
                """)
                row = _as_dict(cursor.fetchone() or {})
                for key in [
                    "references_count", "citations_count", "missing_citations_count",
                    "uncited_references_count", "claim_rows", "suggestions_count",
                    "verification_rows", "verified", "likely", "needs_review", "not_found", "offline"
                ]:
                    advanced[key] = _safe_int(row.get(key))
                if row.get("average_acii_score") is not None:
                    advanced["average_acii_score"] = round(_safe_float(row.get("average_acii_score")), 2)
    except Exception as e:
        # Keep endpoint alive. The dashboard should show relational totals even
        # when optional advanced JSONB counts are temporarily too expensive.
        advanced["error"] = str(e)[:220]
        print(f"[STATS] Optional JSONB metric aggregation skipped: {advanced['error']}")

    total_jobs = _safe_int(job_summary.get("total_jobs"))
    total_uploads = max(_safe_int(stats_row.get("total_uploads")), total_jobs, len(recent_uploads))
    completed = max(_safe_int(stats_row.get("total_processed")), _safe_int(job_summary.get("completed_jobs")))
    failed = max(_safe_int(stats_row.get("total_failed")), _safe_int(job_summary.get("failed_jobs")))
    running = _safe_int(job_summary.get("running_jobs"))
    queued = _safe_int(job_summary.get("queued_jobs"))

    total_refs = max(_safe_int(stats_row.get("total_references_checked")), advanced["references_count"])
    total_verification_rows = max(_safe_int(stats_row.get("total_verifications")), advanced["verification_rows"])

    first_date = job_summary.get("first_created_at") or stats_row.get("updated_at") or datetime.now()
    last_updated = job_summary.get("last_updated_at") or stats_row.get("updated_at") or datetime.now()
    success_rate = round((completed / max(completed + failed, 1)) * 100, 2)

    storage_message = "Persistent PostgreSQL storage is active. Dashboard data will survive deployments and service restarts."
    if advanced.get("error"):
        storage_message += " Advanced JSONB metrics were skipped on this refresh to keep the dashboard fast."

    try:
        queue_status = get_queue_status()
    except Exception as e:
        queue_status = {"error": str(e)[:160]}

    try:
        server_busy = is_server_busy()
    except Exception:
        server_busy = False

    return {
        "total_stats": {
            "total_uploads": total_uploads,
            "total_processed": completed,
            "total_failed": failed,
            "success_rate": success_rate,
            "total_references_checked": total_refs,
            "total_intext_citations": advanced["citations_count"],
            "total_missing_citations": advanced["missing_citations_count"],
            "total_uncited_references": advanced["uncited_references_count"],
            "total_verifications": total_verification_rows,
            "average_processing_time": advanced["average_processing_time"],
            "average_acii_score": advanced["average_acii_score"],
            "start_date": _dt_to_iso(first_date),
            "last_updated": _dt_to_iso(last_updated),
            "storage_backend": "postgresql",
            "persistent_storage": True,
            "storage_message": storage_message,
            "advanced_metrics_error": advanced.get("error"),
        },
        "dashboard_metrics": {
            "completed_jobs": completed,
            "failed_jobs": failed,
            "running_jobs": running,
            "queued_jobs": queued,
            "total_jobs": total_jobs,
            "verified": advanced["verified"],
            "likely": advanced["likely"],
            "needs_review": advanced["needs_review"],
            "not_found": advanced["not_found"],
            "offline": advanced["offline"],
            "claim_rows": advanced["claim_rows"],
            "recovery_rows": advanced["recovery_rows"],
            "suggestions_count": advanced["suggestions_count"],
        },
        "daily_stats": daily_stats,
        "recent_uploads": recent_uploads,
        "system_info": {
            "current_time": datetime.now().isoformat(),
            "storage_backend": "postgresql",
            "persistent_storage": True,
            "active_jobs": running,
            "total_jobs": total_jobs,
            "queue_status": queue_status,
            "server_busy": server_busy,
        }
    }

def _build_sqlite_dashboard_stats(days: int = 30) -> Dict[str, Any]:
    """Local fallback. This is useful for development, but not deployment-safe."""
    stats = stats_tracker.get_stats(detailed=False, days=days)
    payload = _empty_dashboard_payload(
        days,
        message="SQLite /tmp storage is active. This is not deployment-safe. Set DATABASE_URL to a Render PostgreSQL database for persistent dashboard data."
    )
    payload["total_stats"].update(stats.get("total_stats", {}))
    payload["total_stats"].update({
        "storage_backend": "sqlite",
        "persistent_storage": False,
        "storage_message": "SQLite /tmp storage is active. Data can disappear after redeploys, restarts, or instance changes. Use PostgreSQL for production.",
    })
    try:
        import sqlite3
        with sqlite3.connect(stats_tracker.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT timestamp, filename, file_size, references_count,
                       processing_time, success, error
                FROM uploads
                ORDER BY timestamp DESC
                LIMIT 50
            """)
            recent = []
            for row in cursor.fetchall():
                recent.append({
                    "timestamp": row["timestamp"],
                    "filename": row["filename"],
                    "file_size_mb": round((_safe_float(row["file_size"]) / (1024 * 1024)), 2),
                    "references_count": _safe_int(row["references_count"]),
                    "citations_count": 0,
                    "missing_citations_count": 0,
                    "uncited_references_count": 0,
                    "verification_rows": 0,
                    "acii_score": None,
                    "processing_time": _safe_float(row["processing_time"]),
                    "status": "completed" if row["success"] else "failed",
                    "success": bool(row["success"]),
                    "error": row["error"],
                })
            payload["recent_uploads"] = recent
    except Exception as e:
        print(f"[STATS] SQLite detail fallback failed: {e}")
    return payload


@app.get("/private-stats")
def get_private_stats(
    credentials: HTTPBasicCredentials = Depends(security),
    detailed: bool = False,
    days: int = 30
):
    authenticate(credentials)
    days = max(1, min(int(days or 30), 365))

    try:
        if DATABASE_URL:
            return _build_postgres_dashboard_stats(days=days)
        return _build_sqlite_dashboard_stats(days=days)
    except Exception as e:
        print(f"[STATS] Dashboard stats error: {e}")
        fallback = _empty_dashboard_payload(days, message=f"Stats endpoint error: {str(e)[:180]}")
        fallback["system_info"]["error"] = str(e)
        return fallback

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
@app.get("/debug/recent-jobs")
async def debug_recent_jobs(limit: int = 10):
    """List recent jobs from PostgreSQL for debugging"""
    jobs_info = []
    
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT job_id, status, file_name, created_at, completed_at 
                FROM jobs 
                ORDER BY created_at DESC 
                LIMIT %s
            """, (limit,))
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            
            for row in rows:
                jobs_info.append({
                    "job_id": row["job_id"],
                    "status": row["status"],
                    "file_name": row["file_name"],
                    "created_at": str(row["created_at"]) if row["created_at"] else None,
                    "completed_at": str(row["completed_at"]) if row["completed_at"] else None
                })
        except Exception as e:
            print(f"PostgreSQL lookup error: {e}")
    
    # Also get in-memory jobs
    with _lock:
        memory_jobs = list(_store.keys())
    
    return {
        "recent_jobs_from_db": jobs_info,
        "jobs_in_memory": memory_jobs,
        "total_in_memory": len(memory_jobs),
        "total_in_db": len(jobs_info)
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
@app.get("/debug/verification-health/{job_id}")
async def verification_health(job_id: str):
    """Check if verification is making progress"""
    job = load_job_record(job_id)
    if not job:
        return {"error": "Job not found"}
    
    verification = job.get("verification", {})
    progress = verification.get("progress", 0)
    total = verification.get("total", 0)
    state = verification.get("state", "idle")
    last_heartbeat = verification.get("last_heartbeat")
    started_at = verification.get("started_at")
    
    # Calculate if stuck
    is_stuck = False
    if state == "running" and started_at:
        elapsed = (datetime.now() - datetime.fromisoformat(started_at)).total_seconds()
        if elapsed > 300 and progress == 0:  # 5 minutes with 0 progress
            is_stuck = True
    
    return {
        "job_id": job_id,
        "state": state,
        "progress": progress,
        "total": total,
        "percentage": (progress / total * 100) if total > 0 else 0,
        "started_at": started_at,
        "last_heartbeat": last_heartbeat,
        "elapsed_seconds": (datetime.now() - datetime.fromisoformat(started_at)).total_seconds() if started_at else 0,
        "is_stuck": is_stuck,
        "recommendation": "Job appears stuck" if is_stuck else "Job is progressing"
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
