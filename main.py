# main.py — Citation Crosschecker with Async Queue System
# MAIN_BUILD = "commercial-2026-05-29-stripe-server-unlock-repair-v1.6.2"

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
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, BackgroundTasks, Depends, Body
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse, RedirectResponse
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

# Optional commercial dashboard, payment, access-control, manual-search, and certificate helpers.
# These imports are wrapped so the core analysis service can still start if a commercial module is missing.
try:
    from manual_scholar_search import manual_scholar_search
except Exception as e:
    print(f"⚠️ Manual scholar search helper not loaded: {e}")
    manual_scholar_search = None

try:
    from certificate_builder import build_citation_integrity_certificate, render_certificate_html
except Exception as e:
    print(f"⚠️ Certificate builder not loaded: {e}")
    build_citation_integrity_certificate = None
    render_certificate_html = None

try:
    from admin_dashboard import (
        router as admin_dashboard_router,
        init_commercial_dashboard_table,
        record_dashboard,
        record_dashboard_from_result,
    )
    ADMIN_DASHBOARD_AVAILABLE = True
except Exception as e:
    print(f"⚠️ Commercial dashboard helpers not loaded: {e}")
    admin_dashboard_router = None
    init_commercial_dashboard_table = None
    ADMIN_DASHBOARD_AVAILABLE = False

    def record_dashboard(*args, **kwargs):
        return None

    def record_dashboard_from_result(*args, **kwargs):
        return None

# Commercial access and Paystack payment helpers.
try:
    from entitlements import (
        build_plan_selection_payload,
        apply_entitlements_to_result,
    )
    from access_control import (
        init_commercial_tables,
        purchase_is_paid_for_job,
        validate_purchase_for_new_run,
        record_purchase_run,
    )
    from paystack_payments import (
        initialize_citeintegrity_payment,
        verify_and_activate_purchase,
        handle_paystack_webhook,
    )
    COMMERCIAL_FEATURES_AVAILABLE = True
except Exception as e:
    print(f"⚠️ Commercial/payment helpers not loaded: {e}")
    COMMERCIAL_FEATURES_AVAILABLE = False
    build_plan_selection_payload = None
    apply_entitlements_to_result = None
    init_commercial_tables = None
    purchase_is_paid_for_job = None
    validate_purchase_for_new_run = None
    record_purchase_run = None
    initialize_citeintegrity_payment = None
    verify_and_activate_purchase = None
    handle_paystack_webhook = None

# Optional Stripe and payment-routing helpers.
# Kept separate from the Paystack/access-control import block so that a missing
# Stripe package or key never disables existing Paystack payments.
try:
    from payment_router import choose_payment_provider
    from stripe_payments import (
        initialize_citeintegrity_stripe_payment,
        handle_stripe_webhook,
        verify_and_activate_stripe_session,
    )
    STRIPE_PAYMENT_FEATURES_AVAILABLE = True
except Exception as e:
    print(f"⚠️ Stripe/payment routing helpers not loaded: {e}")
    STRIPE_PAYMENT_FEATURES_AVAILABLE = False
    choose_payment_provider = None
    initialize_citeintegrity_stripe_payment = None
    handle_stripe_webhook = None
    verify_and_activate_stripe_session = None


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

# Initialise commercial/payment and admin-dashboard tables when the optional helpers are available.
if DATABASE_URL and COMMERCIAL_FEATURES_AVAILABLE and init_commercial_tables:
    try:
        init_commercial_tables(DATABASE_URL)
        print("✅ Commercial payment tables checked")
    except Exception as e:
        print(f"⚠️ Could not initialise commercial payment tables: {e}")

if DATABASE_URL and ADMIN_DASHBOARD_AVAILABLE and init_commercial_dashboard_table:
    try:
        init_commercial_dashboard_table()
        print("✅ Commercial admin dashboard table checked")
    except Exception as e:
        print(f"⚠️ Could not initialise commercial admin dashboard table: {e}")



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


if admin_dashboard_router is not None:
    try:
        app.include_router(admin_dashboard_router)
    except Exception as e:
        print(f"⚠️ Could not include commercial admin dashboard router: {e}")



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

    if path.startswith("/api/certificate/"):
        return await call_next(request)

    if path.startswith("/admin/commercial-dashboard") or path.startswith("/admin/api/commercial-dashboard"):
        return await call_next(request)

    
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
        "/terms",
        "/static",
        "/export-fixed-document",
        "/export-references",
        "/autofix-suggestions",
        "/fix-log",
        "/apply-autofix",
        "/queue/status",
        "/api/enrichment",
        "/api/certificate",
        "/webhooks/paystack",
        "/payment/paystack",
        "/api/paystack",
        "/webhooks/stripe",
        "/payment/stripe",
        "/api/payment",
        "/api/plans",
        "/api/manual-verify",
        "/api/manual-search",
        "/new",
        "/analyse",
        "/results",
        "/features",
        "/pricing",
        "/contact",
        "/about",
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
        "img-src 'self' data: https://i.ytimg.com https://img.youtube.com; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com; "
        "child-src 'self' https://www.youtube.com https://www.youtube-nocookie.com; "
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


# ============================================================
# PAYMENT ACCESS HELPERS
# ============================================================

def get_document_counts_from_result(result: Dict[str, Any]) -> Dict[str, int]:
    """Return reference and in-text citation counts from a job result."""
    result = result or {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}

    reference_count = (
        summary.get("reference_entries_found")
        or summary.get("references_count")
        or len(result.get("references_raw") or [])
        or len(result.get("references") or [])
        or 0
    )

    citation_count = (
        summary.get("in_text_citations_found")
        or summary.get("citations_count")
        or len(result.get("in_text_citations") or [])
        or len(result.get("citations") or [])
        or len(result.get("reconciliation_intext_to_reference") or [])
        or 0
    )

    try:
        reference_count = int(reference_count or 0)
    except Exception:
        reference_count = 0

    try:
        citation_count = int(citation_count or 0)
    except Exception:
        citation_count = 0

    return {
        "reference_count": reference_count,
        "citation_count": citation_count,
    }


def _purchase_tier_key(purchase: Dict[str, Any]) -> str:
    """Return the best available package/tier key from any payment helper schema."""
    purchase = purchase or {}
    return (
        purchase.get("document_tier")
        or purchase.get("tier_key")
        or purchase.get("package_key")
        or purchase.get("plan_key")
        or purchase.get("document_tier_key")
        or purchase.get("package")
        or "full_review"
    )


def _normalise_purchase_access(purchase: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a paid purchase row into the access payload expected by the results page."""
    purchase = purchase or {}
    return {
        "paid": True,
        "tier_key": _purchase_tier_key(purchase),
        "currency": purchase.get("currency") or "USD",
        "payment_provider": purchase.get("payment_provider") or purchase.get("provider") or "",
        "provider_reference": (
            purchase.get("provider_reference")
            or purchase.get("payment_reference")
            or purchase.get("reference")
            or purchase.get("stripe_session_id")
            or ""
        ),
        "purchase": purchase,
    }


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    try:
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = %s AND column_name = %s
            )
            """,
            (table_name, column_name),
        )
        row = cursor.fetchone()
        if isinstance(row, dict):
            return bool(row.get("exists"))
        return bool(row[0]) if row else False
    except Exception:
        return False


def _table_columns(cursor, table_name: str) -> set:
    """Return columns for a table. Used for backward-compatible payment repairs."""
    try:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s
            """,
            (table_name,),
        )
        return {
            (row.get("column_name") if isinstance(row, dict) else row[0])
            for row in (cursor.fetchall() or [])
        }
    except Exception:
        return set()


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a key from dict-like Stripe objects and normal Python objects."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return getattr(obj, key)
    except Exception:
        return default


def _as_dict(value: Any) -> Dict[str, Any]:
    """Best-effort conversion of Stripe metadata/customer objects into a dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value)
    except Exception:
        try:
            return json.loads(json.dumps(value))
        except Exception:
            return {}


def _session_payment_is_paid(session: Any) -> bool:
    payment_status = str(_obj_get(session, "payment_status", "") or "").lower()
    status = str(_obj_get(session, "status", "") or "").lower()
    return payment_status in {"paid", "no_payment_required"} or status in {"complete", "completed"}


def _lookup_purchase_by_reference(cursor, session_id: str, job_id: str = "") -> Optional[Dict[str, Any]]:
    """Find a Stripe purchase by session/reference or by preview job id."""
    purchase_cols = _table_columns(cursor, "purchases")
    if not purchase_cols:
        return None

    reference_columns = [
        "provider_reference",
        "payment_reference",
        "reference",
        "stripe_session_id",
        "checkout_session_id",
    ]

    clauses = []
    values = []
    for col in reference_columns:
        if col in purchase_cols and session_id:
            clauses.append(f"{col} = %s")
            values.append(session_id)

    if clauses:
        cursor.execute(
            f"""
            SELECT *
            FROM purchases
            WHERE {" OR ".join(clauses)}
            ORDER BY created_at DESC NULLS LAST
            LIMIT 1
            """,
            tuple(values),
        )
        found = cursor.fetchone()
        if found:
            return dict(found)

    if job_id and "preview_job_id" in purchase_cols:
        cursor.execute(
            """
            SELECT *
            FROM purchases
            WHERE preview_job_id = %s
              AND LOWER(COALESCE(payment_provider, '')) = 'stripe'
            ORDER BY created_at DESC NULLS LAST
            LIMIT 1
            """,
            (job_id,),
        )
        found = cursor.fetchone()
        if found:
            return dict(found)

    if job_id and "job_id" in purchase_cols:
        cursor.execute(
            """
            SELECT *
            FROM purchases
            WHERE job_id = %s
              AND LOWER(COALESCE(payment_provider, '')) = 'stripe'
            ORDER BY created_at DESC NULLS LAST
            LIMIT 1
            """,
            (job_id,),
        )
        found = cursor.fetchone()
        if found:
            return dict(found)

    return None


def _safe_payment_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or default))
    except Exception:
        return default


def _stripe_session_payload(session: Any, fallback_job_id: str = "") -> Dict[str, Any]:
    """Extract the fields CiteIntegrity needs from a verified Stripe Checkout Session."""
    metadata = _as_dict(_obj_get(session, "metadata", {}))
    customer_details = _as_dict(_obj_get(session, "customer_details", {}))

    session_id = str(_obj_get(session, "id", "") or "").strip()
    job_id = (
        str(fallback_job_id or "").strip()
        or str(metadata.get("preview_job_id") or "").strip()
        or str(metadata.get("job_id") or "").strip()
        or str(metadata.get("citeintegrity_job_id") or "").strip()
        or str(_obj_get(session, "client_reference_id", "") or "").strip()
    )

    email = (
        str(customer_details.get("email") or "").strip()
        or str(_obj_get(session, "customer_email", "") or "").strip()
        or str(metadata.get("email") or metadata.get("user_email") or "").strip()
    )

    tier_key = (
        metadata.get("tier_key")
        or metadata.get("document_tier")
        or metadata.get("package_key")
        or metadata.get("plan_key")
        or "full_review"
    )

    amount_minor = _safe_payment_int(_obj_get(session, "amount_total", 0), 0)
    currency = str(_obj_get(session, "currency", "") or metadata.get("currency") or "USD").upper()

    return {
        "session_id": session_id,
        "job_id": job_id,
        "user_email": email,
        "email": email,
        "document_tier": tier_key,
        "tier_key": tier_key,
        "package_key": tier_key,
        "plan_key": tier_key,
        "currency": currency,
        "amount_minor": amount_minor,
        "amount": amount_minor,
        "payment_provider": "stripe",
        "provider_reference": session_id,
        "payment_reference": session_id,
        "reference": session_id,
        "status": "paid" if _session_payment_is_paid(session) else "pending",
        "analyses_total": _safe_payment_int(metadata.get("analysis_runs") or metadata.get("analyses_total") or 2, 2),
        "analyses_used": _safe_payment_int(metadata.get("analyses_used") or 0, 0),
        "preview_job_id": job_id,
        "job_id": job_id,
        "preview_file_name": metadata.get("file_name") or metadata.get("preview_file_name") or "",
        "file_name": metadata.get("file_name") or metadata.get("preview_file_name") or "",
        "preview_reference_count": _safe_payment_int(metadata.get("reference_count") or metadata.get("preview_reference_count") or 0, 0),
        "preview_citation_count": _safe_payment_int(metadata.get("citation_count") or metadata.get("preview_citation_count") or 0, 0),
    }


def _write_purchase_and_run_for_paid_stripe_session(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Mark a verified Stripe Checkout Session as paid in the commercial tables and
    link it to the analysed preview job.

    This is intentionally schema-tolerant because older deployments may have
    slightly different payment columns.
    """
    if not (DATABASE_URL and payload.get("job_id")):
        return None

    job_id = payload["job_id"]
    session_id = payload.get("session_id") or payload.get("provider_reference") or ""

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        cursor = conn.cursor()

        # Prefer server-side counts from the completed job.
        job = load_job_record_fresh(job_id)
        if job and isinstance(job.get("result"), dict):
            counts = get_document_counts_from_result(job.get("result") or {})
            payload["preview_reference_count"] = counts["reference_count"] or payload.get("preview_reference_count") or 0
            payload["preview_citation_count"] = counts["citation_count"] or payload.get("preview_citation_count") or 0
            if not payload.get("preview_file_name"):
                payload["preview_file_name"] = job.get("file_name") or ""

        purchase_cols = _table_columns(cursor, "purchases")
        purchase = _lookup_purchase_by_reference(cursor, session_id, job_id) if purchase_cols else None
        purchase_id = (purchase or {}).get("id")

        allowed_purchase_values = {
            "status": "paid",
            "payment_provider": "stripe",
            "provider_reference": session_id,
            "payment_reference": session_id,
            "reference": session_id,
            "stripe_session_id": session_id,
            "checkout_session_id": session_id,
            "user_email": payload.get("user_email") or payload.get("email") or "",
            "email": payload.get("email") or payload.get("user_email") or "",
            "document_tier": _purchase_tier_key(payload),
            "tier_key": _purchase_tier_key(payload),
            "package_key": _purchase_tier_key(payload),
            "plan_key": _purchase_tier_key(payload),
            "currency": payload.get("currency") or "USD",
            "amount_minor": payload.get("amount_minor") or 0,
            "amount": payload.get("amount_minor") or 0,
            "analyses_total": payload.get("analyses_total") or 2,
            "analyses_used": payload.get("analyses_used") or 0,
            "preview_job_id": job_id,
            "job_id": job_id,
            "preview_file_name": payload.get("preview_file_name") or payload.get("file_name") or "",
            "file_name": payload.get("file_name") or payload.get("preview_file_name") or "",
            "preview_reference_count": payload.get("preview_reference_count") or 0,
            "preview_citation_count": payload.get("preview_citation_count") or 0,
        }

        if purchase_cols:
            if purchase_id:
                set_parts = []
                values = []
                for col, value in allowed_purchase_values.items():
                    if col in purchase_cols:
                        if col in {"analyses_total"}:
                            set_parts.append(f"{col} = COALESCE({col}, %s)")
                        elif col in {"analyses_used"}:
                            set_parts.append(f"{col} = COALESCE({col}, %s)")
                        else:
                            set_parts.append(f"{col} = %s")
                        values.append(value)

                if "updated_at" in purchase_cols:
                    set_parts.append("updated_at = CURRENT_TIMESTAMP")

                if set_parts:
                    values.append(purchase_id)
                    cursor.execute(
                        f"UPDATE purchases SET {', '.join(set_parts)} WHERE id = %s RETURNING *",
                        tuple(values),
                    )
                    purchase = dict(cursor.fetchone() or purchase)
            else:
                insert_cols = [col for col in allowed_purchase_values if col in purchase_cols]
                insert_values = [allowed_purchase_values[col] for col in insert_cols]
                if insert_cols:
                    placeholders = ", ".join(["%s"] * len(insert_cols))
                    returning = " RETURNING *" if "id" in purchase_cols else ""
                    cursor.execute(
                        f"""
                        INSERT INTO purchases ({", ".join(insert_cols)})
                        VALUES ({placeholders})
                        {returning}
                        """,
                        tuple(insert_values),
                    )
                    inserted = cursor.fetchone() if "id" in purchase_cols else None
                    purchase = dict(inserted or allowed_purchase_values)
                    purchase_id = purchase.get("id")

        purchase = dict(purchase or payload)
        if purchase_id:
            purchase["id"] = purchase_id

        # Ensure purchase_runs links this paid purchase to the analysed job.
        run_cols = _table_columns(cursor, "purchase_runs")
        if run_cols and purchase_id and "job_id" in run_cols and "purchase_id" in run_cols:
            cursor.execute(
                "SELECT * FROM purchase_runs WHERE job_id = %s ORDER BY created_at DESC NULLS LAST LIMIT 1",
                (job_id,),
            )
            existing_run = cursor.fetchone()

            run_values = {
                "purchase_id": purchase_id,
                "job_id": job_id,
                "file_name": payload.get("preview_file_name") or payload.get("file_name") or "",
                "reference_count": payload.get("preview_reference_count") or 0,
                "citation_count": payload.get("preview_citation_count") or 0,
            }

            if existing_run:
                set_parts = []
                values = []
                for col, value in run_values.items():
                    if col in run_cols and col != "job_id":
                        set_parts.append(f"{col} = %s")
                        values.append(value)
                if set_parts:
                    values.append(job_id)
                    cursor.execute(
                        f"UPDATE purchase_runs SET {', '.join(set_parts)} WHERE job_id = %s",
                        tuple(values),
                    )
            else:
                insert_cols = [col for col in run_values if col in run_cols]
                insert_values = [run_values[col] for col in insert_cols]
                placeholders = ", ".join(["%s"] * len(insert_cols))
                cursor.execute(
                    f"""
                    INSERT INTO purchase_runs ({", ".join(insert_cols)})
                    VALUES ({placeholders})
                    """,
                    tuple(insert_values),
                )

        conn.commit()
        cursor.close()
        conn.close()
        conn = None

        _mark_job_result_paid(job_id, purchase)
        print(f"[STRIPE_UNLOCK] Paid Stripe access linked for job_id={job_id}, session_id={session_id}")
        return purchase

    except Exception as e:
        if conn:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
        print(f"[STRIPE_UNLOCK_ERROR] job_id={job_id}, session_id={session_id}: {type(e).__name__}: {e}")

        # Even if the commercial tables fail, a verified Stripe session can still
        # mark the job result as paid so the user is not left locked out.
        fallback_purchase = dict(payload)
        fallback_purchase["status"] = "paid"
        _mark_job_result_paid(job_id, fallback_purchase)
        return fallback_purchase


def verify_stripe_session_and_unlock_job(session_id: str, fallback_job_id: str = "") -> Dict[str, Any]:
    """
    Direct Stripe verification fallback.

    The Stripe webhook remains preferred, but this makes the success redirect
    self-healing when the webhook is delayed or the Stripe helper does not attach
    the purchase to purchase_runs.
    """
    session_id = (session_id or "").strip()
    fallback_job_id = (fallback_job_id or "").strip()

    if not session_id:
        return {"ok": False, "activated": False, "message": "Missing Stripe session ID.", "job_id": fallback_job_id}

    try:
        import stripe  # type: ignore
    except Exception as e:
        return {
            "ok": False,
            "activated": False,
            "message": f"Stripe package is unavailable: {type(e).__name__}",
            "job_id": fallback_job_id,
        }

    stripe_secret_key = (
        os.environ.get("STRIPE_SECRET_KEY")
        or os.environ.get("STRIPE_API_KEY")
        or os.environ.get("STRIPE_SECRET")
        or ""
    ).strip()

    if not stripe_secret_key:
        return {
            "ok": False,
            "activated": False,
            "message": "Stripe secret key is not configured.",
            "job_id": fallback_job_id,
        }

    try:
        stripe.api_key = stripe_secret_key
        session = stripe.checkout.Session.retrieve(session_id)
        payload = _stripe_session_payload(session, fallback_job_id=fallback_job_id)

        # If metadata did not contain the job id, try to recover it from the database
        # using the session/reference created at initialization.
        if not payload.get("job_id") and DATABASE_URL:
            try:
                conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
                cursor = conn.cursor()
                existing = _lookup_purchase_by_reference(cursor, session_id, "")
                cursor.close()
                conn.close()
                if existing:
                    payload["job_id"] = (
                        existing.get("preview_job_id")
                        or existing.get("job_id")
                        or fallback_job_id
                        or ""
                    )
                    payload["preview_job_id"] = payload["job_id"]
                    payload["document_tier"] = _purchase_tier_key(existing)
                    payload["tier_key"] = _purchase_tier_key(existing)
                    payload["currency"] = existing.get("currency") or payload.get("currency") or "USD"
                    payload["user_email"] = existing.get("user_email") or existing.get("email") or payload.get("user_email") or ""
                    payload["email"] = payload["user_email"]
            except Exception as db_e:
                print(f"[STRIPE_UNLOCK_LOOKUP_ERROR] {type(db_e).__name__}: {db_e}")

        if not _session_payment_is_paid(session):
            return {
                "ok": True,
                "activated": False,
                "message": f"Stripe session is not paid yet: {_obj_get(session, 'payment_status', '')}",
                "job_id": payload.get("job_id") or fallback_job_id,
                "provider_reference": session_id,
            }

        if not payload.get("job_id"):
            return {
                "ok": True,
                "activated": False,
                "message": "Stripe payment is paid, but the preview job ID could not be recovered.",
                "job_id": fallback_job_id,
                "provider_reference": session_id,
            }

        purchase = _write_purchase_and_run_for_paid_stripe_session(payload) or payload
        return {
            "ok": True,
            "activated": True,
            "message": "Stripe payment verified and Full Review unlocked.",
            "job_id": payload.get("job_id"),
            "purchase": purchase,
            "provider_reference": session_id,
            "payment_provider": "stripe",
        }

    except Exception as e:
        print(f"[STRIPE_UNLOCK_VERIFY_ERROR] session_id={session_id}: {type(e).__name__}: {e}")
        return {
            "ok": False,
            "activated": False,
            "message": f"Stripe verification failed: {type(e).__name__}",
            "job_id": fallback_job_id,
            "provider_reference": session_id,
        }


def activate_paid_access_for_purchase(job_id: str, purchase: Dict[str, Any]) -> None:
    """Link an already-paid purchase to a job and refresh result access markers."""
    if not (DATABASE_URL and job_id and purchase):
        return

    purchase = dict(purchase or {})
    purchase.setdefault("preview_job_id", job_id)
    purchase.setdefault("job_id", job_id)
    purchase.setdefault("status", "paid")

    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        cursor = conn.cursor()
        run_cols = _table_columns(cursor, "purchase_runs")
        purchase_id = purchase.get("id")

        if run_cols and purchase_id and "purchase_id" in run_cols and "job_id" in run_cols:
            cursor.execute(
                "SELECT * FROM purchase_runs WHERE job_id = %s ORDER BY created_at DESC NULLS LAST LIMIT 1",
                (job_id,),
            )
            existing_run = cursor.fetchone()

            counts = get_document_counts_from_result((load_job_record_fresh(job_id) or {}).get("result") or {})
            file_name = purchase.get("preview_file_name") or purchase.get("file_name") or ""

            if existing_run:
                cursor.execute(
                    """
                    UPDATE purchase_runs
                    SET purchase_id = %s,
                        file_name = COALESCE(NULLIF(%s, ''), file_name),
                        reference_count = CASE WHEN %s > 0 THEN %s ELSE reference_count END,
                        citation_count = CASE WHEN %s > 0 THEN %s ELSE citation_count END
                    WHERE job_id = %s
                    """,
                    (
                        purchase_id,
                        file_name,
                        counts["reference_count"], counts["reference_count"],
                        counts["citation_count"], counts["citation_count"],
                        job_id,
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO purchase_runs (purchase_id, job_id, file_name, reference_count, citation_count)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (purchase_id, job_id, file_name, counts["reference_count"], counts["citation_count"]),
                )

        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"[ACCESS_ACTIVATE_PURCHASE_ERROR] job_id={job_id}: {type(e).__name__}: {e}")

    _mark_job_result_paid(job_id, purchase)




def _mark_job_result_paid(job_id: str, purchase: Dict[str, Any]) -> None:
    """
    Persist a server-side paid marker inside jobs.result and refresh Redis.
    This marker is written only after a paid purchase has been found server-side.
    """
    if not (DATABASE_URL and job_id and purchase):
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

        result = row.get("result") or {}
        if isinstance(result, str):
            result = json.loads(result)

        result.setdefault("access", {})
        result["access"].update({
            "paid": True,
            "tier_key": _purchase_tier_key(purchase),
            "currency": purchase.get("currency") or "USD",
            "payment_provider": purchase.get("payment_provider") or purchase.get("provider") or "stripe",
            "provider_reference": (
                purchase.get("provider_reference")
                or purchase.get("payment_reference")
                or purchase.get("reference")
                or purchase.get("stripe_session_id")
                or ""
            ),
        })

        cursor.execute(
            "UPDATE jobs SET result = %s::jsonb WHERE job_id = %s",
            (json.dumps(result), job_id),
        )
        conn.commit()
        cursor.close()
        conn.close()

        if redis_conn:
            try:
                redis_conn.setex(f"result:{job_id}", 3600, json.dumps(result))
            except Exception as redis_e:
                print(f"[ACCESS_REPAIR] Redis refresh failed for {job_id}: {redis_e}")

    except Exception as e:
        print(f"[ACCESS_REPAIR] Could not write paid marker for {job_id}: {type(e).__name__}: {e}")


def repair_paid_purchase_link_for_job(job_id: str) -> Dict[str, Any]:
    """
    Recover paid access when Stripe has marked a purchase as paid but purchase_runs
    was not linked to the analysed job.

    This is intentionally server-side: it does not trust ?paid=1 in the URL.
    It only repairs when the database already contains a paid/active purchase
    for the same preview_job_id, or when a paid marker was previously written
    by the Stripe success callback.
    """
    default = {"paid": False, "tier_key": "", "currency": "GHS", "purchase": None}

    if not (DATABASE_URL and job_id):
        return default

    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        cursor = conn.cursor()

        # First: if purchase_runs already links this job to a paid purchase, return it.
        cursor.execute(
            """
            SELECT p.*
            FROM purchases p
            JOIN purchase_runs pr ON pr.purchase_id = p.id
            WHERE pr.job_id = %s
              AND LOWER(COALESCE(p.status, '')) IN ('paid', 'active')
            ORDER BY p.created_at DESC
            LIMIT 1
            """,
            (job_id,),
        )
        purchase = cursor.fetchone()
        if purchase:
            purchase = dict(purchase)
            cursor.close()
            conn.close()
            _mark_job_result_paid(job_id, purchase)
            print(f"[ACCESS_REPAIR] Existing paid purchase_runs link found for job_id={job_id}")
            return _normalise_purchase_access(purchase)

        # Second: if purchases has preview_job_id, find the latest paid Stripe purchase for this job.
        has_preview_job_id = _column_exists(cursor, "purchases", "preview_job_id")
        purchase = None
        if has_preview_job_id:
            cursor.execute(
                """
                SELECT *
                FROM purchases
                WHERE preview_job_id = %s
                  AND LOWER(COALESCE(status, '')) IN ('paid', 'active')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (job_id,),
            )
            purchase = cursor.fetchone()

        if purchase:
            purchase = dict(purchase)
            purchase_id = purchase.get("id")
            cursor.execute(
                "SELECT * FROM purchase_runs WHERE job_id = %s FOR UPDATE",
                (job_id,),
            )
            existing_run = cursor.fetchone()
            if existing_run:
                cursor.execute(
                    """
                    UPDATE purchase_runs
                    SET purchase_id = %s,
                        file_name = COALESCE(NULLIF(%s, ''), file_name),
                        reference_count = CASE WHEN %s > 0 THEN %s ELSE reference_count END,
                        citation_count = CASE WHEN %s > 0 THEN %s ELSE citation_count END
                    WHERE job_id = %s
                    """,
                    (
                        purchase_id,
                        purchase.get("preview_file_name") or "",
                        int(purchase.get("preview_reference_count") or 0),
                        int(purchase.get("preview_reference_count") or 0),
                        int(purchase.get("preview_citation_count") or 0),
                        int(purchase.get("preview_citation_count") or 0),
                        job_id,
                    ),
                )
                action = "relinked"
            else:
                cursor.execute(
                    """
                    INSERT INTO purchase_runs
                        (purchase_id, job_id, file_name, reference_count, citation_count)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        purchase_id,
                        job_id,
                        purchase.get("preview_file_name") or "",
                        int(purchase.get("preview_reference_count") or 0),
                        int(purchase.get("preview_citation_count") or 0),
                    ),
                )
                action = "inserted"

            conn.commit()
            cursor.close()
            conn.close()
            _mark_job_result_paid(job_id, purchase)
            print(f"[ACCESS_REPAIR] Paid purchase {action} for job_id={job_id}, purchase_id={purchase_id}")
            return _normalise_purchase_access(purchase)

        # Third: server-side marker fallback. This only works if the Stripe success
        # callback already wrote jobs.result.access.paid=true after verifying Stripe.
        cursor.execute("SELECT result FROM jobs WHERE job_id = %s", (job_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        result = (row or {}).get("result") if row else {}
        if isinstance(result, str):
            result = json.loads(result)
        marker = (result or {}).get("access") or {}
        if marker.get("paid") is True and marker.get("payment_provider") == "stripe":
            print(f"[ACCESS_REPAIR] Using verified Stripe paid marker for job_id={job_id}")
            return {
                "paid": True,
                "tier_key": marker.get("tier_key", "") or "full_review",
                "currency": marker.get("currency", "USD") or "USD",
                "payment_provider": marker.get("payment_provider") or "stripe",
                "provider_reference": marker.get("provider_reference") or "",
                "purchase": marker,
            }

        return default

    except Exception as e:
        print(f"[ACCESS_REPAIR_ERROR] job_id={job_id}: {type(e).__name__}: {e}")
        return default


def get_access_for_job(job_id: str) -> Dict[str, Any]:
    """Return paid/free access metadata for a job, with Stripe repair fallback."""
    default = {
        "paid": False,
        "tier_key": "",
        "currency": "GHS",
        "purchase": None,
    }

    if not DATABASE_URL:
        return default

    # Normal Paystack/Stripe paid-access path.
    if COMMERCIAL_FEATURES_AVAILABLE and purchase_is_paid_for_job:
        try:
            access = purchase_is_paid_for_job(DATABASE_URL, job_id=job_id) or default
            if access.get("paid"):
                purchase = access.get("purchase") or {}
                return {
                    "paid": True,
                    "tier_key": access.get("tier_key", "") or _purchase_tier_key(purchase),
                    "currency": access.get("currency", "GHS") or "GHS",
                    "payment_provider": (
                        access.get("payment_provider")
                        or purchase.get("payment_provider")
                        or purchase.get("provider")
                        or ""
                    ),
                    "provider_reference": (
                        access.get("provider_reference")
                        or purchase.get("provider_reference")
                        or purchase.get("payment_reference")
                        or purchase.get("reference")
                        or ""
                    ),
                    "purchase": purchase,
                }
        except Exception as e:
            print(f"[ACCESS] Primary purchase lookup failed for {job_id}: {type(e).__name__}: {e}")

    # Stripe-specific recovery path for cases where Checkout succeeded but the
    # paid purchase was not attached to purchase_runs.
    repaired = repair_paid_purchase_link_for_job(job_id)
    if repaired.get("paid"):
        return repaired

    return default


def shape_result_for_access(job_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Apply Free Preview or paid Full Review entitlements before returning results."""
    access = get_access_for_job(job_id)

    if COMMERCIAL_FEATURES_AVAILABLE and apply_entitlements_to_result:
        try:
            shaped = apply_entitlements_to_result(
                result or {},
                tier_key=access.get("tier_key", ""),
                paid=access.get("paid", False),
                currency=access.get("currency", "GHS"),
            )
            shaped.setdefault("access", {})
            purchase = access.get("purchase") or {}
            shaped["access"].update({
                "paid": access.get("paid", False),
                "tier_key": access.get("tier_key", "") or _purchase_tier_key(purchase),
                "currency": access.get("currency", "GHS"),
                "payment_provider": access.get("payment_provider") or purchase.get("payment_provider") or purchase.get("provider") or "",
                "provider_reference": access.get("provider_reference") or purchase.get("provider_reference") or purchase.get("payment_reference") or purchase.get("reference") or "",
                "remaining_analyses": (
                    max(
                        int(purchase.get("analyses_total") or 0)
                        - int(purchase.get("analyses_used") or 0),
                        0,
                    )
                    if purchase else None
                ),
            })
            return shaped
        except Exception as e:
            print(f"[ACCESS] Could not apply entitlements for {job_id}: {e}")

    # Safe fallback: return raw result only if paid access is confirmed.
    return result or {} if access.get("paid") else (result or {})


def build_access_response(job_id: str) -> Dict[str, Any]:
    access = get_access_for_job(job_id)
    purchase = access.get("purchase") or {}
    return {
        "paid": access.get("paid", False),
        "tier_key": access.get("tier_key", "") or _purchase_tier_key(purchase),
        "currency": access.get("currency", "GHS"),
        "payment_provider": access.get("payment_provider") or purchase.get("payment_provider") or purchase.get("provider") or "",
        "provider_reference": access.get("provider_reference") or purchase.get("provider_reference") or purchase.get("payment_reference") or purchase.get("reference") or "",
        "analyses_total": purchase.get("analyses_total"),
        "analyses_used": purchase.get("analyses_used"),
        "remaining_analyses": (
            max(int(purchase.get("analyses_total") or 0) - int(purchase.get("analyses_used") or 0), 0)
            if purchase else None
        ),
    }


@app.get("/api/payment/access-check/{job_id}")
async def payment_access_check(job_id: str, _auth: Any = Depends(authenticate)):
    """Admin-only access diagnostic for Stripe/Paystack unlock issues."""
    access = build_access_response(job_id)
    payload: Dict[str, Any] = {
        "ok": True,
        "job_id": job_id,
        "access": access,
    }

    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, user_email, document_tier, currency, status,
                       payment_provider, provider_reference, analyses_total, analyses_used, created_at
                FROM purchases
                WHERE id IN (SELECT purchase_id FROM purchase_runs WHERE job_id = %s)
                ORDER BY created_at DESC
                LIMIT 5
                """,
                (job_id,),
            )
            payload["linked_purchases"] = [dict(r) for r in (cursor.fetchall() or [])]

            cursor.execute(
                """
                SELECT id, purchase_id, job_id, file_name, reference_count, citation_count, created_at
                FROM purchase_runs
                WHERE job_id = %s
                ORDER BY created_at DESC
                LIMIT 5
                """,
                (job_id,),
            )
            payload["purchase_runs"] = [dict(r) for r in (cursor.fetchall() or [])]
            cursor.close()
            conn.close()
        except Exception as e:
            payload["db_error"] = f"{type(e).__name__}: {str(e)}"

    return payload


@app.post("/api/payment/repair-access/{job_id}")
async def payment_repair_access(job_id: str, _auth: Any = Depends(authenticate)):
    """Admin-only endpoint to repair a paid Stripe purchase link for a job."""
    repaired = repair_paid_purchase_link_for_job(job_id)
    access = build_access_response(job_id)
    return {
        "ok": True,
        "job_id": job_id,
        "repaired": repaired,
        "access_after": access,
        "paid_after": bool(access.get("paid")),
    }

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

                                    try:
                                        record_dashboard_from_result(
                                            job_id,
                                            final_result,
                                            analysis_status="verification_completed",
                                        )
                                    except Exception as dash_error:
                                        print(f"⚠️ Could not update commercial dashboard after verification completion: {dash_error}")
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

@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return templates.TemplateResponse("terms.html", {
        "request": request
    })

@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    return templates.TemplateResponse("about.html", {
        "request": request
    })

@app.get("/contact", response_class=HTMLResponse)
async def contact_page(request: Request):
    return templates.TemplateResponse("contact.html", {
        "request": request
    })
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

    try:
        record_dashboard(
            job_id,
            file_name=file.filename,
            analysis_status="queued",
            reference_count=0,
            citation_count=0,
        )
    except Exception as e:
        print(f"⚠️ Could not record queued job in commercial dashboard: {e}")

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

    access = get_access_for_job(job_id)
    if not access.get("paid"):
        raise HTTPException(
            status_code=402,
            detail="Manual Search is available after Full Review payment."
        )

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

    access = get_access_for_job(job_id)
    if not access.get("paid"):
        raise HTTPException(
            status_code=402,
            detail="Manual verification decisions require Full Review payment."
        )

    purchase = access.get("purchase") or {}
    user_email = purchase.get("user_email") or purchase.get("email") or ""

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
        "manual_verified_by": user_email,
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

    try:
        record_dashboard_from_result(
            job_id,
            result,
            analysis_status="manual_verification_updated",
        )
    except Exception as e:
        print(f"⚠️ Could not update commercial dashboard after manual verification: {e}")

    safe_result = shape_result_for_access(job_id, result)

    return {
        "ok": True,
        "message": f"Manual decision recorded: {decision_payload['manual_decision_label']}",
        "updated_rows": touched,
        "manual_verification": result.get("manual_verification", {}),
        "result": safe_result,
    }

# ============================================================
# RESULT CHECK ENDPOINT
# ============================================================

@app.get("/result/{job_id}")
async def get_result(job_id: str, fresh: int = 0):
    """Get job status and result, shaped by Free Preview or paid Full Review access."""

    # Check Redis cache first unless a fresh PostgreSQL read is requested.
    if redis_conn and not fresh:
        cached = redis_conn.get(f"result:{job_id}")
        if cached:
            try:
                cached_result = json.loads(cached)
                try:
                    record_dashboard_from_result(
                        job_id,
                        cached_result,
                        analysis_status="completed",
                    )
                except Exception as e:
                    print(f"⚠️ Could not update commercial dashboard from Redis result: {e}")

                safe_result = shape_result_for_access(job_id, cached_result)
                return {
                    "status": "completed",
                    "data": safe_result,
                    "access": build_access_response(job_id),
                }
            except Exception as e:
                print(f"[RESULT] Redis result shaping failed for {job_id}: {e}")

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

                try:
                    record_dashboard_from_result(
                        job_id,
                        result,
                        analysis_status="completed",
                    )
                except Exception as e:
                    print(f"⚠️ Could not update commercial dashboard from PostgreSQL result: {e}")

                safe_result = shape_result_for_access(job_id, result)
                return {
                    "status": "completed",
                    "data": safe_result,
                    "access": build_access_response(job_id),
                }

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

    safe_result = shape_result_for_access(job_id, job.get("result") or {})

    return {
        "status": job.get("status", "unknown"),
        "result": safe_result,
        "verification": job.get("verification", {}),
        "access": build_access_response(job_id),
    }

# ============================================================
# PAYMENT AND PLAN ENDPOINTS
# Paystack handles Africa. Stripe handles all other billing countries.
# ============================================================

@app.get("/api/plans/recommend/{job_id}")
async def recommend_package_for_job(job_id: str, currency: str = "GHS"):
    if not (COMMERCIAL_FEATURES_AVAILABLE and build_plan_selection_payload):
        raise HTTPException(status_code=503, detail="Payment features are not available.")

    job = load_job_record_fresh(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    result = job.get("result") or {}
    counts = get_document_counts_from_result(result)

    return build_plan_selection_payload(
        counts["reference_count"],
        counts["citation_count"],
        selected_currency=currency,
    )



def _safe_int_payload(value, default: int = 0) -> int:
    try:
        return int(value or default)
    except Exception:
        return default


def _normalise_init_dashboard_amount(init: Dict[str, Any], provider: str) -> Dict[str, Any]:
    """Return amount values suitable for commercial dashboard records."""
    amount = init.get("amount") or init.get("charged_amount") or 0
    amount_minor = init.get("amount_minor") or init.get("amount_subunit") or 0

    try:
        amount = float(amount or 0)
    except Exception:
        amount = 0.0

    try:
        amount_minor = int(float(amount_minor or 0))
    except Exception:
        amount_minor = 0

    # Stripe returns amount in major units and amount_minor in minor units.
    # Paystack helper returns amount in major units and amount_subunit in minor units.
    if amount and not amount_minor:
        amount_minor = int(round(amount * 100))

    return {
        "amount": amount if amount else ((amount_minor / 100) if amount_minor else None),
        "amount_minor": amount_minor,
    }


@app.post("/api/payment/initialize")
async def payment_initialize(payload: dict = Body(...)):
    """
    Unified CiteIntegrity payment initializer.

    Frontend sends email, billing country, tier_key, and preview job_id.
    Africa is routed to Paystack. All other billing countries are routed to Stripe.
    """
    if not COMMERCIAL_FEATURES_AVAILABLE:
        raise HTTPException(status_code=503, detail="Payment features are not available.")

    if not choose_payment_provider:
        raise HTTPException(status_code=503, detail="Payment routing is not available.")

    user_email = (payload.get("email") or "").strip()
    tier_key = (payload.get("tier_key") or "").strip()
    country_code = (payload.get("country_code") or payload.get("billing_country") or "").strip().upper()
    selected_currency = (payload.get("currency") or "").strip().upper()
    job_id = (payload.get("job_id") or "").strip()
    file_name = (payload.get("file_name") or "").strip()

    reference_count = _safe_int_payload(payload.get("reference_count"), 0)
    citation_count = _safe_int_payload(payload.get("citation_count"), 0)

    if not user_email or "@" not in user_email:
        raise HTTPException(status_code=400, detail="A valid email is required.")

    if not tier_key:
        raise HTTPException(status_code=400, detail="A document package is required.")

    if not job_id:
        raise HTTPException(status_code=400, detail="A preview job ID is required.")

    if not country_code:
        raise HTTPException(status_code=400, detail="Billing country is required.")

    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not configured.")

    # Trust server-side document counts when the preview job is available.
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Preview job not found.")

    server_counts = get_document_counts_from_result(job.get("result") or {})
    reference_count = server_counts["reference_count"] or reference_count
    citation_count = server_counts["citation_count"] or citation_count

    provider = choose_payment_provider(country_code)

    if provider == "paystack":
        if not initialize_citeintegrity_payment:
            raise HTTPException(status_code=503, detail="Paystack payment is not available.")

        init = initialize_citeintegrity_payment(
            database_url=DATABASE_URL,
            user_email=user_email,
            tier_key=tier_key,
            reference_count=reference_count,
            citation_count=citation_count,
            selected_currency=selected_currency or "GHS",
            job_id=job_id,
            file_name=file_name,
            callback_path="/payment/paystack/callback",
        )

        if not init.get("ok"):
            return JSONResponse(init, status_code=402)

        checkout_url = init.get("authorization_url")
        payment_reference = init.get("reference") or init.get("payment_reference") or ""
        amount_info = _normalise_init_dashboard_amount(init, provider="paystack")

        try:
            record_dashboard(
                job_id,
                email=user_email,
                file_name=file_name,
                plan_key=tier_key,
                plan_name=tier_key,
                currency=init.get("currency") or init.get("charged_currency") or selected_currency or "GHS",
                amount=amount_info["amount"],
                amount_minor=amount_info["amount_minor"],
                payment_reference=payment_reference,
                payment_status="initialized",
                paid=False,
                reference_count=reference_count,
                citation_count=citation_count,
            )
        except Exception as e:
            print(f"⚠️ Could not update commercial dashboard after Paystack initialization: {e}")

        return {
            "ok": True,
            "provider": "paystack",
            "checkout_url": checkout_url,
            "authorization_url": checkout_url,
            "reference": payment_reference,
            "purchase_id": init.get("purchase_id"),
            "amount": init.get("amount"),
            "currency": init.get("currency"),
            "display_amount": init.get("display_amount"),
            "access_token": init.get("access_token"),
            "raw": init,
        }

    if not (STRIPE_PAYMENT_FEATURES_AVAILABLE and initialize_citeintegrity_stripe_payment):
        raise HTTPException(status_code=503, detail="Stripe payment is not available.")

    init = initialize_citeintegrity_stripe_payment(
        database_url=DATABASE_URL,
        user_email=user_email,
        tier_key=tier_key,
        reference_count=reference_count,
        citation_count=citation_count,
        selected_currency="USD",
        job_id=job_id,
        file_name=file_name,
    )

    if not init.get("ok"):
        return JSONResponse(init, status_code=402)

    amount_info = _normalise_init_dashboard_amount(init, provider="stripe")

    try:
        record_dashboard(
            job_id,
            email=user_email,
            file_name=file_name,
            plan_key=tier_key,
            plan_name=tier_key,
            currency=init.get("currency") or "USD",
            amount=amount_info["amount"],
            amount_minor=amount_info["amount_minor"],
            payment_reference=init.get("reference") or init.get("session_id"),
            payment_status="initialized",
            paid=False,
            reference_count=reference_count,
            citation_count=citation_count,
        )
    except Exception as e:
        print(f"⚠️ Could not update commercial dashboard after Stripe initialization: {e}")

    return init


@app.post("/api/paystack/initialize")
async def paystack_initialize(payload: dict = Body(...)):
    if not (COMMERCIAL_FEATURES_AVAILABLE and initialize_citeintegrity_payment):
        raise HTTPException(status_code=503, detail="Payment features are not available.")

    user_email = (payload.get("email") or "").strip()
    tier_key = (payload.get("tier_key") or "").strip()
    selected_currency = (payload.get("currency") or "GHS").strip().upper()
    job_id = (payload.get("job_id") or "").strip()
    file_name = (payload.get("file_name") or "").strip()

    reference_count = int(payload.get("reference_count") or 0)
    citation_count = int(payload.get("citation_count") or 0)

    if not user_email or "@" not in user_email:
        raise HTTPException(status_code=400, detail="A valid email is required.")

    if not tier_key:
        raise HTTPException(status_code=400, detail="A document package is required.")

    if not job_id:
        raise HTTPException(status_code=400, detail="A preview job ID is required.")

    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not configured.")

    # Trust server-side counts when the preview job is available.
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Preview job not found.")

    server_counts = get_document_counts_from_result(job.get("result") or {})
    reference_count = server_counts["reference_count"] or reference_count
    citation_count = server_counts["citation_count"] or citation_count

    init = initialize_citeintegrity_payment(
        database_url=DATABASE_URL,
        user_email=user_email,
        tier_key=tier_key,
        reference_count=reference_count,
        citation_count=citation_count,
        selected_currency=selected_currency,
        job_id=job_id,
        file_name=file_name,
        callback_path="/payment/paystack/callback",
    )

    if not init.get("ok"):
        raise HTTPException(
            status_code=402,
            detail=init.get("error", "Could not initialize payment."),
        )

    try:
        init_data = init.get("data") if isinstance(init.get("data"), dict) else {}
        payment_reference = (
            init.get("payment_reference")
            or init.get("reference")
            or init_data.get("reference")
            or ""
        )
        amount_minor = (
            init.get("amount_minor")
            or init.get("amount")
            or init_data.get("amount")
            or 0
        )
        try:
            amount_minor = int(float(amount_minor or 0))
        except Exception:
            amount_minor = 0

        plan_name = (
            init.get("plan_name")
            or init.get("tier_name")
            or init.get("package_name")
            or tier_key
        )

        record_dashboard(
            job_id,
            email=user_email,
            file_name=file_name,
            plan_key=tier_key,
            plan_name=plan_name,
            currency=selected_currency,
            amount=(amount_minor / 100) if amount_minor else None,
            amount_minor=amount_minor,
            payment_reference=payment_reference,
            payment_status="initialized",
            paid=False,
            reference_count=reference_count,
            citation_count=citation_count,
        )
    except Exception as e:
        print(f"⚠️ Could not update commercial dashboard after payment initialization: {e}")

    return init
# ============================================================
# CITATION INTEGRITY CERTIFICATE ENDPOINTS
# ============================================================

def _certificate_access_for_job(job_id: str) -> Dict[str, Any]:
    try:
        if "get_access_for_job" in globals():
            return get_access_for_job(job_id) or {}
    except Exception as e:
        print(f"[CERTIFICATE] get_access_for_job failed: {e}")

    try:
        return build_access_response(job_id) or {}
    except Exception as e:
        print(f"[CERTIFICATE] build_access_response failed: {e}")

    return {}


def _certificate_is_paid(access: Dict[str, Any]) -> bool:
    if not isinstance(access, dict):
        return False

    if access.get("paid") is True:
        return True

    nested = access.get("access")
    if isinstance(nested, dict) and nested.get("paid") is True:
        return True

    return False


def _save_certificate_result_to_db_and_cache(job_id: str, result: dict):
    if DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "UPDATE jobs SET result = %s::jsonb WHERE job_id = %s",
            (json.dumps(result), job_id),
        )
        conn.commit()
        cur.close()
        conn.close()

    if redis_conn:
        try:
            redis_conn.setex(f"result:{job_id}", 3600, json.dumps(result))
        except Exception as e:
            print(f"[CERTIFICATE] Redis refresh failed: {e}")


@app.get("/api/certificate/{job_id}")
async def get_citation_integrity_certificate(job_id: str):
    if build_citation_integrity_certificate is None or render_certificate_html is None:
        raise HTTPException(status_code=503, detail="Certificate features are not available.")

    access = _certificate_access_for_job(job_id)

    if not _certificate_is_paid(access):
        return JSONResponse(
            {
                "ok": False,
                "locked": True,
                "error": "Citation Integrity Certificate is available after Full Review payment.",
            },
            status_code=402,
        )

    job = load_job_record_fresh(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    result = job.get("result") or {}

    certificate = build_citation_integrity_certificate(
        result,
        job_id=job_id,
        access=access,
    )

    result["citation_integrity_certificate"] = certificate

    try:
        _save_certificate_result_to_db_and_cache(job_id, result)
    except Exception as e:
        print(f"[CERTIFICATE] Could not persist certificate: {e}")

    try:
        record_dashboard_from_result(
            job_id,
            result,
            analysis_status="certificate_generated",
            certificate_generated=True,
        )
    except Exception as e:
        print(f"⚠️ Could not update commercial dashboard after certificate generation: {e}")

    return {
        "ok": True,
        "certificate": certificate,
    }


@app.get("/api/certificate/{job_id}/download")
async def download_citation_integrity_certificate(job_id: str):
    if build_citation_integrity_certificate is None or render_certificate_html is None:
        raise HTTPException(status_code=503, detail="Certificate features are not available.")

    access = _certificate_access_for_job(job_id)

    if not _certificate_is_paid(access):
        return JSONResponse(
            {
                "ok": False,
                "locked": True,
                "error": "Citation Integrity Certificate is available after Full Review payment.",
            },
            status_code=402,
        )

    job = load_job_record_fresh(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    result = job.get("result") or {}

    certificate = result.get("citation_integrity_certificate") or build_citation_integrity_certificate(
        result,
        job_id=job_id,
        access=access,
    )

    html_doc = render_certificate_html(certificate)
    filename = f"CiteIntegrity_Certificate_{job_id[:8]}.doc"

    return Response(
        content=html_doc,
        media_type="application/msword",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )

@app.get("/payment/paystack/callback")
async def paystack_callback(request: Request, reference: str = "", trxref: str = ""):
    payment_reference = (reference or trxref or "").strip()

    if not payment_reference:
        return HTMLResponse(
            """
            <h2>Payment reference missing</h2>
            <p>Paystack did not return a valid payment reference. Please contact support.</p>
            """,
            status_code=400,
        )

    if not DATABASE_URL:
        return HTMLResponse(
            """
            <h2>Database unavailable</h2>
            <p>Payment was received, but CiteIntegrity could not access the database.</p>
            """,
            status_code=500,
        )

    try:
        result = verify_and_activate_purchase(
            database_url=DATABASE_URL,
            reference=payment_reference,
        )

        if not result.get("activated"):
            message = (
                result.get("message")
                or "Payment could not be confirmed."
            )

            return HTMLResponse(
                f"""
                <h2>Payment could not be confirmed</h2>
                <p>{message}</p>
                <p>Reference: {payment_reference}</p>
                """,
                status_code=400,
            )

        purchase = result.get("purchase") or {}
        preview_job_id = purchase.get("preview_job_id") or ""

        if preview_job_id:
            try:
                activate_paid_access_for_purchase(preview_job_id, purchase)
            except Exception as access_e:
                print(f"[PAYSTACK_CALLBACK_ACCESS_LINK_ERROR] {type(access_e).__name__}: {access_e}")

        try:
            amount_minor = (
                purchase.get("amount_minor")
                or purchase.get("amount")
                or result.get("amount")
                or 0
            )
            try:
                amount_minor = int(float(amount_minor or 0))
            except Exception:
                amount_minor = 0

            record_dashboard(
                preview_job_id or purchase.get("job_id") or purchase.get("preview_job_id"),
                email=(
                    purchase.get("user_email")
                    or purchase.get("email")
                    or result.get("email")
                ),
                file_name=purchase.get("file_name") or result.get("file_name"),
                plan_key=purchase.get("tier_key") or result.get("tier_key"),
                plan_name=(
                    purchase.get("plan_name")
                    or purchase.get("tier_name")
                    or result.get("plan_name")
                    or purchase.get("tier_key")
                ),
                currency=purchase.get("currency") or result.get("currency"),
                amount=(amount_minor / 100) if amount_minor else None,
                amount_minor=amount_minor,
                payment_reference=payment_reference,
                payment_status="paid",
                paid=True,
            )
        except Exception as e:
            print(f"⚠️ Could not update commercial dashboard after payment activation: {e}")

        if preview_job_id:
            return RedirectResponse(
                url=f"/new/results/{preview_job_id}?verify=1&paid=1",
                status_code=303,
            )

        return HTMLResponse(
            """
            <h2>Payment successful</h2>
            <p>Your CiteIntegrity review has been unlocked.</p>
            <p>Please return to your results page and refresh.</p>
            """
        )

    except Exception as e:
        print(f"[PAYSTACK CALLBACK ERROR] {type(e).__name__}: {e}")

        return HTMLResponse(
            f"""
            <h2>Payment received, but activation failed</h2>
            <p>Your payment may have been successful, but CiteIntegrity could not unlock the result automatically.</p>
            <p>Please contact support with this reference:</p>
            <p><strong>{payment_reference}</strong></p>
            <p>Error: {type(e).__name__}</p>
            """,
            status_code=500,
        )


@app.post("/webhooks/paystack")
async def paystack_webhook(request: Request):
    if not (COMMERCIAL_FEATURES_AVAILABLE and handle_paystack_webhook):
        return JSONResponse({"ok": False, "message": "Payment features are not available."}, status_code=503)

    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")

    result = handle_paystack_webhook(
        database_url=DATABASE_URL,
        raw_body=raw_body,
        signature=signature,
    )

    try:
        purchase = result.get("purchase") or {}
        if result.get("activated") or result.get("ok"):
            job_id = purchase.get("preview_job_id") or purchase.get("job_id")
            if job_id:
                try:
                    activate_paid_access_for_purchase(job_id, purchase)
                except Exception as access_e:
                    print(f"[PAYSTACK_WEBHOOK_ACCESS_LINK_ERROR] {type(access_e).__name__}: {access_e}")

                amount_minor = purchase.get("amount_minor") or purchase.get("amount") or 0
                try:
                    amount_minor = int(float(amount_minor or 0))
                except Exception:
                    amount_minor = 0

                record_dashboard(
                    job_id,
                    email=purchase.get("user_email") or purchase.get("email"),
                    file_name=purchase.get("file_name"),
                    plan_key=purchase.get("tier_key"),
                    plan_name=purchase.get("plan_name") or purchase.get("tier_name") or purchase.get("tier_key"),
                    currency=purchase.get("currency"),
                    amount=(amount_minor / 100) if amount_minor else None,
                    amount_minor=amount_minor,
                    payment_reference=purchase.get("payment_reference") or purchase.get("reference"),
                    payment_status="paid",
                    paid=True,
                )
    except Exception as e:
        print(f"⚠️ Could not update commercial dashboard from Paystack webhook: {e}")

    return JSONResponse(result, status_code=result.get("status_code", 200))



@app.post("/api/payment/stripe/confirm")
async def stripe_confirm_payment(payload: dict = Body(...)):
    """
    Public, server-verified Stripe confirmation endpoint.

    The browser may call this after returning from Stripe if the result page still
    shows Free Preview. It does not trust the browser's paid flag; it verifies the
    Stripe Checkout Session with the Stripe secret key before unlocking.
    """
    session_id = (payload.get("session_id") or payload.get("session") or "").strip()
    job_id = (payload.get("job_id") or "").strip()

    result = verify_stripe_session_and_unlock_job(
        session_id=session_id,
        fallback_job_id=job_id,
    )

    status_code = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status_code)


@app.get("/payment/stripe/success")
async def stripe_payment_success(session_id: str = "", job_id: str = ""):
    """
    Stripe success callback.

    Paystack unlocks through its callback by verifying the transaction and then
    marking the purchase as paid. Stripe should do the same here as a safe
    fallback, while the webhook remains the primary production confirmation.
    """
    requested_job_id = (job_id or "").strip()

    activation = {
        "ok": False,
        "activated": False,
        "message": "Payment received. Unlock is being processed.",
        "job_id": requested_job_id,
    }

    if not DATABASE_URL:
        print("[STRIPE_SUCCESS_PAGE] DATABASE_URL is not configured.")
    elif session_id and verify_and_activate_stripe_session:
        activation = verify_and_activate_stripe_session(
            database_url=DATABASE_URL,
            session_id=session_id,
            fallback_job_id=requested_job_id,
        ) or activation
    elif not session_id:
        print("[STRIPE_SUCCESS_PAGE] Missing session_id from Stripe success redirect.")
    else:
        print("[STRIPE_SUCCESS_PAGE] verify_and_activate_stripe_session helper is unavailable.")

    # Fallback: directly verify the Stripe Checkout Session and unlock the job.
    # This fixes the common case where Stripe payment succeeds but the helper or
    # webhook does not attach the paid purchase to purchase_runs before redirect.
    if session_id and not bool(activation.get("activated")):
        fallback_activation = verify_stripe_session_and_unlock_job(
            session_id=session_id,
            fallback_job_id=requested_job_id,
        )
        if fallback_activation.get("activated"):
            activation = fallback_activation

    purchase = activation.get("purchase") or {}
    final_job_id = (
        (activation.get("job_id") or "").strip()
        or requested_job_id
        or (purchase.get("preview_job_id") or "").strip()
        or (purchase.get("job_id") or "").strip()
    )

    access_after = {}
    if final_job_id:
        try:
            access_after = build_access_response(final_job_id)
            print(f"[STRIPE_SUCCESS_ACCESS] job_id={final_job_id}, access={access_after}")
        except Exception as e:
            print(f"[STRIPE_SUCCESS_ACCESS_ERROR] {type(e).__name__}: {e}")
            access_after = {}

    activated = bool(activation.get("activated") or access_after.get("paid"))

    # Server-side fallback marker for the results payload. This does not replace
    # the purchase/purchase_runs access check; it helps the frontend reflect the
    # paid state immediately after a verified Stripe success redirect.
    if activated and final_job_id and DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            cursor = conn.cursor()
            cursor.execute("SELECT result FROM jobs WHERE job_id = %s", (final_job_id,))
            row = cursor.fetchone()
            if row:
                stored_result = row.get("result") or {}
                if isinstance(stored_result, str):
                    stored_result = json.loads(stored_result)
                stored_result.setdefault("access", {})
                stored_result["access"].update({
                    "paid": True,
                    "tier_key": access_after.get("tier_key") or _purchase_tier_key(purchase),
                    "currency": access_after.get("currency") or purchase.get("currency") or "USD",
                    "payment_provider": "stripe",
                })
                cursor.execute(
                    "UPDATE jobs SET result = %s::jsonb WHERE job_id = %s",
                    (json.dumps(stored_result), final_job_id),
                )
                conn.commit()
                if redis_conn:
                    try:
                        redis_conn.setex(f"result:{final_job_id}", 3600, json.dumps(stored_result))
                    except Exception as redis_e:
                        print(f"[STRIPE_SUCCESS_CACHE] Redis paid marker refresh failed: {redis_e}")
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"[STRIPE_SUCCESS_MARKER_ERROR] {type(e).__name__}: {e}")

    # Keep route consistent with the current dashboard route.
    results_url = f"/new/results/{final_job_id}?verify=1&paid=1&fresh=1" if final_job_id else "/new/analyse"

    try:
        if activated and final_job_id:
            amount_minor = (
                purchase.get("amount_minor")
                or purchase.get("amount")
                or 0
            )
            try:
                amount_minor = int(float(amount_minor or 0))
            except Exception:
                amount_minor = 0

            record_dashboard(
                final_job_id,
                email=purchase.get("user_email") or purchase.get("email"),
                file_name=purchase.get("preview_file_name") or purchase.get("file_name"),
                plan_key=purchase.get("tier_key") or purchase.get("document_tier") or purchase.get("package_key"),
                plan_name=purchase.get("plan_name") or purchase.get("tier_name") or purchase.get("tier_key") or purchase.get("document_tier"),
                currency=purchase.get("currency") or "USD",
                amount=(amount_minor / 100) if amount_minor else None,
                amount_minor=amount_minor,
                payment_reference=activation.get("provider_reference") or purchase.get("provider_reference"),
                payment_status="paid",
                paid=True,
            )
    except Exception as e:
        print(f"⚠️ Could not update commercial dashboard after Stripe success activation: {e}")

    print(
        f"[STRIPE_SUCCESS_PAGE] session_id={session_id}, query_job_id={requested_job_id}, "
        f"activation_job_id={activation.get('job_id')}, activated={activated}, redirect={results_url}"
    )

    heading = "✅ Full Review unlocked" if activated else "✅ Payment received"
    message = (
        "Your CiteIntegrity Full Review has been unlocked. You will be redirected to your results page."
        if activated
        else "Your payment was received, but the unlock has not yet been confirmed. You will be redirected to your results page; refresh after a few seconds."
    )

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Payment Received | CiteIntegrity</title>
        <meta http-equiv="refresh" content="2;url={results_url}">
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #f8fafc;
                color: #0f172a;
                display: grid;
                place-items: center;
                min-height: 100vh;
                margin: 0;
            }}
            .card {{
                background: white;
                border: 1px solid #e2e8f0;
                border-radius: 22px;
                padding: 34px;
                max-width: 660px;
                text-align: center;
                box-shadow: 0 14px 40px rgba(15, 23, 42, 0.08);
            }}
            h1 {{ margin: 0 0 12px; color: #13855a; }}
            p {{ color: #64748b; line-height: 1.6; }}
            a {{
                display: inline-block;
                margin-top: 18px;
                background: #0f172a;
                color: white;
                padding: 12px 18px;
                border-radius: 999px;
                text-decoration: none;
                font-weight: 800;
            }}
            .small {{ font-size: 12px; color: #94a3b8; margin-top: 16px; word-break: break-all; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>{heading}</h1>
            <p>{message}</p>
            <p>If the results page does not update immediately, click the button below and refresh once.</p>
            <a href="{results_url}">Return to results</a>
            <p class="small">Session: {session_id}</p>
            <p class="small">Job: {final_job_id or 'not recovered'}</p>
        </div>
        <script>
            setTimeout(function () {{ window.location.href = "{results_url}"; }}, 2000);
        </script>
    </body>
    </html>
    """)


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    if not (STRIPE_PAYMENT_FEATURES_AVAILABLE and handle_stripe_webhook):
        return JSONResponse({"ok": False, "message": "Stripe payment features are not available."}, status_code=503)

    raw_body = await request.body()
    signature = request.headers.get("stripe-signature", "")

    result = handle_stripe_webhook(
        database_url=DATABASE_URL,
        raw_body=raw_body,
        signature=signature,
    )

    try:
        purchase = result.get("purchase") or {}
        if result.get("purchase_activated") or result.get("activated") or result.get("ok"):
            job_id = purchase.get("preview_job_id") or purchase.get("job_id")
            if job_id:
                amount_minor = result.get("amount_minor") or purchase.get("amount_minor") or 0
                amount = result.get("amount") or purchase.get("amount") or 0

                try:
                    amount_minor = int(float(amount_minor or 0))
                except Exception:
                    amount_minor = 0

                try:
                    amount = float(amount or 0)
                except Exception:
                    amount = 0.0

                if amount and not amount_minor:
                    amount_minor = int(round(amount * 100))

                try:
                    activate_paid_access_for_purchase(job_id, purchase)
                except Exception as access_e:
                    print(f"[STRIPE_WEBHOOK_ACCESS_LINK_ERROR] {type(access_e).__name__}: {access_e}")

                record_dashboard(
                    job_id,
                    email=purchase.get("user_email") or purchase.get("email"),
                    file_name=purchase.get("preview_file_name") or purchase.get("file_name"),
                    plan_key=purchase.get("tier_key") or purchase.get("document_tier") or purchase.get("package_key"),
                    plan_name=purchase.get("plan_name") or purchase.get("tier_name") or purchase.get("tier_key") or purchase.get("document_tier"),
                    currency=(result.get("currency") or purchase.get("currency") or "USD"),
                    amount=(amount_minor / 100) if amount_minor else (amount or None),
                    amount_minor=amount_minor,
                    payment_reference=(
                        result.get("reference")
                        or purchase.get("payment_reference")
                        or purchase.get("provider_reference")
                        or purchase.get("reference")
                    ),
                    payment_status="paid",
                    paid=True,
                )
    except Exception as e:
        print(f"⚠️ Could not update commercial dashboard from Stripe webhook: {e}")

    return JSONResponse(result, status_code=result.get("status_code", 200))


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


# ============================================================
# PRIVATE STATS: JSONB-AWARE DASHBOARD HELPERS
# ============================================================

def _dashboard_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _dashboard_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _normalise_job_result(result: Any) -> Dict[str, Any]:
    """Return the actual analysis result from jobs.result, regardless of wrapper shape."""
    if not result:
        return {}

    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            return {}

    if not isinstance(result, dict):
        return {}

    # Some API responses wrap the payload as {status: ..., data: {...}}.
    data = result.get("data")
    if isinstance(data, dict) and (
        data.get("summary")
        or data.get("online_verification")
        or data.get("references_raw")
        or data.get("acii")
    ):
        merged = dict(data)
        # Keep top-level access/status fields as fallbacks without overwriting data.
        for key in ("access", "status", "filename", "verification"):
            if key in result and key not in merged:
                merged[key] = result.get(key)
        return merged

    return result


def _dashboard_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _extract_result_metrics(result: Any) -> Dict[str, Any]:
    """Extract dashboard-safe counts from one completed analysis result."""
    result = _normalise_job_result(result)
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}

    online = result.get("online_verification") if isinstance(result.get("online_verification"), dict) else {}
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    acii = result.get("acii") if isinstance(result.get("acii"), dict) else {}

    references_raw = _dashboard_list(result.get("references_raw"))
    references = _dashboard_list(result.get("references"))
    citations = _dashboard_list(result.get("in_text_citations")) or _dashboard_list(result.get("citations"))
    c2r = _dashboard_list(result.get("reconciliation_intext_to_reference"))
    missing_items = _dashboard_list(result.get("missing_in_references"))
    uncited_items = _dashboard_list(result.get("uncited_references"))
    verification_rows = _dashboard_list(online.get("rows"))
    claim_support = result.get("claim_support")

    reference_count = (
        _dashboard_int(summary.get("reference_entries_found"))
        or _dashboard_int(summary.get("references_count"))
        or len(references_raw)
        or len(references)
    )

    citation_count = (
        _dashboard_int(summary.get("in_text_citations_found"))
        or _dashboard_int(summary.get("citations_count"))
        or len(citations)
        or len(c2r)
    )

    missing_count = (
        _dashboard_int(summary.get("missing_in_references"))
        or _dashboard_int(summary.get("missing_references"))
        or len(missing_items)
    )

    uncited_count = (
        _dashboard_int(summary.get("uncited_references"))
        or len(uncited_items)
    )

    verification_count = (
        len(verification_rows)
        or _dashboard_int(verification.get("results_count"))
        or _dashboard_int((online.get("summary") or {}).get("total") if isinstance(online.get("summary"), dict) else 0)
    )

    status_counts = {
        "verified": 0,
        "likely": 0,
        "needs_review": 0,
        "not_found": 0,
        "offline": 0,
    }
    for row in verification_rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "offline").strip().lower()
        status = status.replace("-", "_").replace(" ", "_")
        if status in status_counts:
            status_counts[status] += 1
        else:
            status_counts["offline"] += 1

    # If only the summary is available, use it as a fallback.
    online_summary = online.get("summary") if isinstance(online.get("summary"), dict) else {}
    for key in status_counts:
        if status_counts[key] == 0 and online_summary.get(key) is not None:
            status_counts[key] = _dashboard_int(online_summary.get(key))

    acii_score = None
    for candidate in (acii.get("ACII"), acii.get("score"), result.get("acii_score")):
        if candidate is not None and candidate != "":
            acii_score = round(_dashboard_float(candidate), 2)
            break

    if isinstance(claim_support, list):
        claim_rows = len(claim_support)
    elif isinstance(claim_support, dict):
        rows = claim_support.get("rows") or claim_support.get("items") or []
        claim_rows = len(rows) if isinstance(rows, list) else 0
    else:
        claim_rows = 0

    processing_time = (
        _dashboard_float(result.get("processing_time"))
        or _dashboard_float(result.get("processing_time_seconds"))
        or _dashboard_float(verification.get("processing_time_seconds"))
    )

    return {
        "references_count": reference_count,
        "citations_count": citation_count,
        "missing_citations_count": missing_count,
        "uncited_references_count": uncited_count,
        "verification_rows": verification_count,
        "acii_score": acii_score,
        "claim_rows": claim_rows,
        "processing_time": processing_time,
        "verified": status_counts["verified"],
        "likely": status_counts["likely"],
        "needs_review": status_counts["needs_review"],
        "not_found": status_counts["not_found"],
        "offline": status_counts["offline"],
    }


def _merge_total_stats_with_jsonb(stats: Dict[str, Any], rows: list, days: int) -> Dict[str, Any]:
    """Populate dashboard totals from jobs.result JSONB while preserving legacy counters."""
    total_stats = stats.setdefault("total_stats", {})
    metrics = {
        "verified": 0,
        "likely": 0,
        "needs_review": 0,
        "not_found": 0,
        "offline": 0,
        "claim_rows": 0,
    }

    totals = {
        "references": 0,
        "citations": 0,
        "missing": 0,
        "uncited": 0,
        "verification_rows": 0,
    }
    processing_times = []
    acii_scores = []

    processed = failed = uploads = 0
    for row in rows:
        uploads += 1
        status = str(row.get("status") or "").lower()
        if status in {"completed", "done", "success"}:
            processed += 1
        elif status in {"failed", "error"} or row.get("error"):
            failed += 1

        m = _extract_result_metrics(row.get("result"))
        totals["references"] += m["references_count"]
        totals["citations"] += m["citations_count"]
        totals["missing"] += m["missing_citations_count"]
        totals["uncited"] += m["uncited_references_count"]
        totals["verification_rows"] += m["verification_rows"]
        metrics["verified"] += m["verified"]
        metrics["likely"] += m["likely"]
        metrics["needs_review"] += m["needs_review"]
        metrics["not_found"] += m["not_found"]
        metrics["offline"] += m["offline"]
        metrics["claim_rows"] += m["claim_rows"]
        if m["processing_time"]:
            processing_times.append(m["processing_time"])
        if m["acii_score"] is not None:
            acii_scores.append(m["acii_score"])

    # Use JSONB rows for the selected reporting window, but fall back to legacy stats when rows are unavailable.
    if rows:
        total_stats["total_uploads"] = uploads
        total_stats["total_processed"] = processed
        total_stats["total_failed"] = failed
        total_stats["success_rate"] = round((processed / max(processed + failed, 1)) * 100, 2)

    total_stats["total_references_checked"] = totals["references"] or _dashboard_int(total_stats.get("total_references_checked"))
    total_stats["total_intext_citations"] = totals["citations"]
    total_stats["total_missing_citations"] = totals["missing"]
    total_stats["total_uncited_references"] = totals["uncited"]
    total_stats["total_verifications"] = totals["verification_rows"] or _dashboard_int(total_stats.get("total_verifications"))
    total_stats["average_processing_time"] = round(sum(processing_times) / len(processing_times), 2) if processing_times else _dashboard_float(total_stats.get("average_processing_time"))
    total_stats["average_acii_score"] = round(sum(acii_scores) / len(acii_scores), 2) if acii_scores else None
    total_stats["persistent_storage"] = bool(DATABASE_URL)
    total_stats["storage_backend"] = "postgresql" if DATABASE_URL else "sqlite"
    total_stats["storage_message"] = (
        f"Persistent PostgreSQL storage is active. Dashboard indicators are populated from jobs.result JSONB for the last {days} day(s)."
        if DATABASE_URL else
        "SQLite fallback is active. Set DATABASE_URL to preserve dashboard data across deployments."
    )
    total_stats["jsonb_metrics_loaded"] = bool(rows)
    total_stats["jsonb_jobs_scanned"] = len(rows)

    stats["dashboard_metrics"] = metrics
    return stats


def _build_daily_stats_from_jobs(rows: list) -> Dict[str, Any]:
    daily: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        ts = row.get("completed_at") or row.get("created_at") or row.get("started_at")
        if hasattr(ts, "strftime"):
            date_key = ts.strftime("%Y-%m-%d")
        else:
            date_key = str(ts or datetime.now().date())[:10]

        entry = daily.setdefault(date_key, {
            "uploads": 0,
            "processed": 0,
            "failed": 0,
            "references": 0,
            "citations": 0,
            "missing_citations": 0,
            "uncited_references": 0,
            "verification_rows": 0,
            "total_processing_time": 0.0,
            "processing_count": 0,
            "avg_processing_time": 0,
            "_acii_scores": [],
            "average_acii_score": None,
        })

        status = str(row.get("status") or "").lower()
        entry["uploads"] += 1
        if status in {"completed", "done", "success"}:
            entry["processed"] += 1
        elif status in {"failed", "error"} or row.get("error"):
            entry["failed"] += 1

        m = _extract_result_metrics(row.get("result"))
        entry["references"] += m["references_count"]
        entry["citations"] += m["citations_count"]
        entry["missing_citations"] += m["missing_citations_count"]
        entry["uncited_references"] += m["uncited_references_count"]
        entry["verification_rows"] += m["verification_rows"]
        if m["processing_time"]:
            entry["total_processing_time"] += m["processing_time"]
            entry["processing_count"] += 1
        if m["acii_score"] is not None:
            entry["_acii_scores"].append(m["acii_score"])

    for entry in daily.values():
        entry["avg_processing_time"] = round(entry["total_processing_time"] / entry["processing_count"], 2) if entry["processing_count"] else 0
        scores = entry.pop("_acii_scores", [])
        entry["average_acii_score"] = round(sum(scores) / len(scores), 2) if scores else None

    return dict(sorted(daily.items(), reverse=True))


def _build_recent_uploads_from_jobs(rows: list, limit: int = 50) -> list:
    recent = []
    sorted_rows = sorted(
        rows,
        key=lambda r: str(r.get("created_at") or r.get("completed_at") or ""),
        reverse=True,
    )
    for row in sorted_rows[:limit]:
        result = _normalise_job_result(row.get("result"))
        m = _extract_result_metrics(result)
        status = str(row.get("status") or "").lower()
        recent.append({
            "timestamp": row.get("created_at") or row.get("completed_at"),
            "filename": row.get("file_name") or result.get("filename") or "Untitled",
            "status": row.get("status") or "unknown",
            "success": status in {"completed", "done", "success"},
            "error": row.get("error"),
            "references_count": m["references_count"],
            "citations_count": m["citations_count"],
            "missing_citations_count": m["missing_citations_count"],
            "uncited_references_count": m["uncited_references_count"],
            "verification_rows": m["verification_rows"],
            "acii_score": m["acii_score"],
            "processing_time": row.get("processing_time") or m["processing_time"],
        })
    return recent


@app.get("/private-stats")
def get_private_stats(
    credentials: HTTPBasicCredentials = Depends(security),
    detailed: bool = False,
    days: int = 30
):
    authenticate(credentials)
    days = max(1, min(_dashboard_int(days, 30), 365))

    # Start with the existing legacy counters for backward compatibility.
    stats = stats_tracker.get_stats(detailed=detailed, days=days)
    job_rows = []

    if DATABASE_URL:
        try:
            with psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT job_id, status, file_name, result, error, processing_time,
                               created_at, started_at, completed_at
                        FROM jobs
                        WHERE created_at >= NOW() - (%s::int * INTERVAL '1 day')
                        ORDER BY created_at DESC
                        LIMIT 5000
                        """,
                        (days,),
                    )
                    job_rows = list(cursor.fetchall() or [])
        except Exception as e:
            print(f"[PRIVATE_STATS] JSONB job metrics failed: {type(e).__name__}: {e}")
            job_rows = []

    if job_rows:
        stats = _merge_total_stats_with_jsonb(stats, job_rows, days)
        stats["daily_stats"] = _build_daily_stats_from_jobs(job_rows)
        stats["recent_uploads"] = _build_recent_uploads_from_jobs(job_rows, limit=50)
    else:
        # Fallback to the older uploads/daily_stats tables when jobs.result is unavailable.
        recent_uploads = []
        daily_stats = {}
        if DATABASE_URL:
            try:
                with psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT timestamp, filename, references_count, processing_time, success
                            FROM uploads
                            ORDER BY timestamp DESC
                            LIMIT 50
                            """
                        )
                        recent_uploads = list(cursor.fetchall() or [])

                        cursor.execute(
                            """
                            SELECT date, uploads, processed, failed, references_count,
                                   total_processing_time, processing_count
                            FROM daily_stats
                            WHERE date >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
                            ORDER BY date DESC
                            """,
                            (days,),
                        )
                        for row in cursor.fetchall() or []:
                            date_str = row['date'].strftime('%Y-%m-%d') if hasattr(row['date'], 'strftime') else str(row['date'])
                            daily_stats[date_str] = {
                                "uploads": row['uploads'],
                                "processed": row['processed'],
                                "failed": row['failed'],
                                "references": row['references_count'],
                                "citations": 0,
                                "missing_citations": 0,
                                "uncited_references": 0,
                                "verification_rows": 0,
                                "total_processing_time": float(row['total_processing_time']) if row['total_processing_time'] else 0,
                                "processing_count": row['processing_count'],
                                "avg_processing_time": round(float(row['total_processing_time']) / row['processing_count'], 2) if row['processing_count'] > 0 else 0,
                                "average_acii_score": None,
                            }
            except Exception as e:
                print(f"[PRIVATE_STATS] legacy dashboard fallback failed: {type(e).__name__}: {e}")

        stats.setdefault("total_stats", {})["persistent_storage"] = bool(DATABASE_URL)
        stats["total_stats"]["storage_backend"] = "postgresql" if DATABASE_URL else "sqlite"
        stats["total_stats"]["storage_message"] = "Persistent storage is active, but JSONB job metrics could not be loaded on this refresh."
        stats["dashboard_metrics"] = {"verified": 0, "likely": 0, "needs_review": 0, "not_found": 0, "offline": 0, "claim_rows": 0}
        stats["recent_uploads"] = recent_uploads
        stats["daily_stats"] = daily_stats

    stats["system_info"] = {
        "current_time": datetime.now().isoformat(),
        "active_jobs": len([j for j in _store.values() if j.get("verification", {}).get("state") == "running"]),
        "total_jobs": len(_store),
        "queue_status": get_queue_status(),
        "server_busy": is_server_busy(),
    }

    return stats

@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    return templates.TemplateResponse("stats.html", {"request": request})
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
