# main.py — Citation Crosschecker with Async Queue System
# MAIN_BUILD = "DEMO_MAIN-web-safe-queue-worker-health-2026-06-01-v1.5.44"

import io
import asyncio
import base64
import contextvars
import hashlib
import hmac
import os
import re
import uuid
import threading
import time
import json
import html
import secrets
import sqlite3
import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus

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
from rq import Queue, Worker
from rq.registry import StartedJobRegistry, FailedJobRegistry, DeferredJobRegistry

# Optional automatic high-memory Render One-Off Job trigger for large files.
# If the helper file is not deployed or environment is incomplete, normal queueing still works.
try:
    from render_large_worker_autostart import trigger_large_worker_if_needed
except Exception as e:
    print(f"⚠️ Large worker autostart helper not loaded: {e}")
    def trigger_large_worker_if_needed(redis_conn=None, reason: str = ""):
        return {"started": False, "reason": f"helper unavailable: {e}"}

# FastAPI and web frameworks
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
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
from citation_suggester import extract_context, suggest_from_context, suggest_for_unverified
from claim_checker import build_claim_support_rows
from reference_formatter import (
    format_verified_reference_list,
    format_reference,
    export_references_to_docx,
    export_references_to_html,
    DOCX_AVAILABLE
)
from academic_voice import analyse_academic_voice, rewrite_selected_passage
from correction_plan import build_correction_plan, compare_revision_results
from evidence_resolution import (
    assess_candidate_context_fit,
    build_claim_fingerprint,
    build_document_topic_profile,
)
from source_risk import assess_source_risks
from payment_control import get_access_mode, set_access_mode, open_access_payload
from document_correction_pack import build_annotated_document, build_tracked_changes_document
from citation_coach import build_citation_coach
from privacy_lifecycle import (
    CONTENT_TTL_SECONDS,
    DELETE_AFTER_PACKAGE_DOWNLOAD,
    attach_privacy_status,
    build_report_package,
    purge_result_content,
    redis_content_keys,
)


# Optional certificate helpers for training/demo mode. These are wrapped so the
# demonstration service can still start even if the certificate module is not yet deployed.
try:
    from certificate_builder import (
        build_citation_integrity_certificate,
        render_certificate_html,
        render_certificate_pdf_bytes,
    )
    CERTIFICATE_FEATURES_AVAILABLE = True
except Exception as e:
    print(f"⚠️ Certificate builder not loaded: {e}")
    build_citation_integrity_certificate = None
    render_certificate_html = None
    render_certificate_pdf_bytes = None
    CERTIFICATE_FEATURES_AVAILABLE = False


# ===============================
# DATABASE SETUP - PostgreSQL (with SQLite fallback)
# ===============================

# Get database URL from environment (Render sets this)
DATABASE_URL = os.environ.get("DATABASE_URL")

# Get Redis URL from environment (Render sets this)
REDIS_URL = os.environ.get("REDIS_URL")

# ===============================
# TRAINING / DEMONSTRATION MODE
# ===============================
# Production is the safe default. Demonstration access must be explicitly enabled.
DEMO_UNLOCK_ALL_FEATURES = os.environ.get("DEMO_UNLOCK_ALL_FEATURES", "false").strip().lower() in {"1", "true", "yes", "on"}
DEMO_ACCESS_EMAIL = os.environ.get("DEMO_ACCESS_EMAIL", "demo@citeintegrity.org")

# Keep the web service responsive. Heavy recovery, claim-support and large-text
# citation-needed generation must not run inside homepage/result polling requests.
WEB_SAFE_RESULT_PREPARE = os.environ.get("WEB_SAFE_RESULT_PREPARE", "true").strip().lower() not in {"0", "false", "no"}
RUN_RECOVERY_SUGGESTIONS_IN_WEB = os.environ.get("RUN_RECOVERY_SUGGESTIONS_IN_WEB", "false").strip().lower() in {"1", "true", "yes", "on"}
RUN_CLAIM_SUPPORT_IN_WEB = os.environ.get("RUN_CLAIM_SUPPORT_IN_WEB", "false").strip().lower() in {"1", "true", "yes", "on"}
WEB_CITATION_NEEDED_MAX_CHARS = int(os.environ.get("WEB_CITATION_NEEDED_MAX_CHARS", "150000"))

# ===============================
# CONTACT EMAIL FORWARDING SETUP
# ===============================

CONTACT_SUPPORT_EMAIL = os.environ.get("CONTACT_SUPPORT_EMAIL", "support@citeintegrity.org")

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", CONTACT_SUPPORT_EMAIL).strip()
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "CiteIntegrity Contact Form").strip()
CONTACT_REQUIRE_EMAIL_SENT = os.environ.get("CONTACT_REQUIRE_EMAIL_SENT", "true").strip().lower() not in {"0", "false", "no"}


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
                            SELECT AVG(processing_time) AS avg_processing_time
                            FROM uploads
                            WHERE success = 1 AND processing_time IS NOT NULL
                        """)
                        avg_row = cursor.fetchone()
                        if isinstance(avg_row, dict):
                            avg_value = avg_row.get("avg_processing_time")
                        else:
                            avg_value = avg_row[0] if avg_row else None
                        avg_processing_time = round(float(avg_value), 2) if avg_value else 0
                        
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

USERNAME = os.environ.get("DEVELOPER_USERNAME", "admin")
PASSWORD = os.environ.get("DEVELOPER_PASSWORD", "Ano77kye7509#")

def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, USERNAME)
    correct_password = secrets.compare_digest(credentials.password, PASSWORD)

    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="Secure Area"'},
        )


def developer_request_is_authorized(request: Request) -> bool:
    """Allow developers to test the full product while public maintenance mode is active."""
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(authorization.split(" ", 1)[1], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(username, USERNAME) and secrets.compare_digest(password, PASSWORD)

APP_TITLE = "CitationCrosschecker"
RELEASE_VERSION = os.environ.get("RELEASE_VERSION", "1.9.0").strip()
RELEASE_SLOT = os.environ.get("RELEASE_SLOT", "blue").strip().lower()
DEVELOPER_SESSION_COOKIE = "citeintegrity_developer_session"
DEVELOPER_ACCESS_LEVELS = {"full_access", "full_review"}
DEVELOPER_ACCESS_LEVEL = contextvars.ContextVar("developer_access_level", default="")
DEVELOPER_SESSION_TTL_SECONDS = max(900, int(os.environ.get("DEVELOPER_SESSION_TTL_SECONDS", "43200")))
DEVELOPER_SESSION_SECRET = os.environ.get("DEVELOPER_SESSION_SECRET", "").strip() or hashlib.sha256(
    f"{PASSWORD}:citeintegrity-developer-session".encode("utf-8")
).hexdigest()


def create_developer_session_token(access_level: str = "full_access") -> str:
    access_level = access_level if access_level in DEVELOPER_ACCESS_LEVELS else "full_access"
    issued_at = str(int(time.time()))
    payload = f"{issued_at}:{USERNAME}:{access_level}"
    signature = hmac.new(DEVELOPER_SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{issued_at}.{access_level}.{signature}"


def developer_session_access_level(request: Request) -> str:
    token = request.cookies.get(DEVELOPER_SESSION_COOKIE, "")
    try:
        issued_at_text, access_level, supplied_signature = token.split(".", 2)
        issued_at = int(issued_at_text)
    except (TypeError, ValueError):
        return ""
    if access_level not in DEVELOPER_ACCESS_LEVELS:
        return ""
    age = int(time.time()) - issued_at
    if age < 0 or age > DEVELOPER_SESSION_TTL_SECONDS:
        return ""
    payload = f"{issued_at}:{USERNAME}:{access_level}"
    expected_signature = hmac.new(DEVELOPER_SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return access_level if secrets.compare_digest(supplied_signature, expected_signature) else ""


def developer_session_is_authorized(request: Request) -> bool:
    return bool(developer_session_access_level(request))


# ===============================
# LIFESPAN MANAGER
# ===============================

def cleanup_expired_manuscript_content(limit: int = 200) -> Dict[str, int]:
    """Best-effort expiry pass. Safe to run at startup and before new uploads."""
    if not DATABASE_URL:
        return {"checked": 0, "deleted": 0}
    cutoff = datetime.utcnow() - timedelta(seconds=CONTENT_TTL_SECONDS)
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    cursor = conn.cursor()
    checked = deleted = 0
    try:
        cursor.execute(
            """
            SELECT job_id, result FROM jobs
            WHERE created_at < %s
              AND status IN ('completed', 'failed')
              AND COALESCE((result->'privacy'->>'content_deleted')::boolean, false) = false
            ORDER BY created_at ASC LIMIT %s
            """,
            (cutoff, int(limit)),
        )
        for row in cursor.fetchall():
            checked += 1
            result = row.get("result") or {}
            if isinstance(result, str):
                result = json.loads(result)
            try:
                _snapshot_feature_usage(result)
            except Exception as snapshot_error:
                print(f"[PRIVACY] Count-only expiry snapshot failed safely for {row['job_id']}: {snapshot_error}")
            minimal = purge_result_content(result, reason="automatic_expiry")
            cursor.execute("UPDATE jobs SET result = %s::jsonb WHERE job_id = %s", (json.dumps(minimal), row["job_id"]))
            if redis_conn:
                redis_conn.delete(*list(redis_content_keys(row["job_id"])))
            deleted += 1
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"[PRIVACY] Expiry cleanup failed: {exc}")
    finally:
        cursor.close()
        conn.close()
    return {"checked": checked, "deleted": deleted}


async def _privacy_cleanup_loop():
    interval = max(300, int(os.environ.get("CONTENT_CLEANUP_INTERVAL_SECONDS", "3600")))
    while True:
        await asyncio.sleep(interval)
        await run_in_threadpool(cleanup_expired_manuscript_content)

@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    print("🚀 Starting Citation Crosschecker...")
    stats = stats_tracker.get_stats(detailed=False)
    print(f"📈 Stats tracker loaded: {stats['total_stats']['total_uploads']} total uploads")
    cleanup = cleanup_expired_manuscript_content()
    print(f"🧹 Temporary-content cleanup: {cleanup}")
    cleanup_task = asyncio.create_task(_privacy_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
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
        "/terms",
		"/about",
		"/static",
        "/export-fixed-document",
        "/export-references",
        "/autofix-suggestions",
        "/fix-log",
        "/apply-autofix",
        "/queue/status",
        "/api/enrichment",
        "/api/certificate",
        "/api/citation-needed",
        "/api/demo",
        "/new",
        "/analyse",
        "/results",
        "/features",
        "/pricing",
        "/contact",
        "/api/contact",
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


@app.middleware("http")
async def maintenance_gate(request: Request, call_next):
    """Block public use during upgrades without restricting authenticated developers."""
    path = request.url.path
    if path == "/health" or path.startswith("/developer/") or path.startswith("/api/developer/access"):
        return await call_next(request)
    basic_authorized = developer_request_is_authorized(request)
    session_level = developer_session_access_level(request)
    if basic_authorized or session_level:
        context_token = DEVELOPER_ACCESS_LEVEL.set(session_level or "full_access")
        try:
            return await call_next(request)
        finally:
            DEVELOPER_ACCESS_LEVEL.reset(context_token)

    state = get_access_mode(DATABASE_URL, redis_conn)
    if state.get("mode") != "maintenance":
        return await call_next(request)

    message = html.escape(str(state.get("maintenance_message") or "CiteIntegrity is undergoing scheduled maintenance while an upgrade is tested."))
    check_at = str(state.get("maintenance_check_at") or "")
    retry_after = "3600"
    if path.startswith(("/api/", "/verify", "/result", "/analyse")) or request.method not in {"GET", "HEAD"}:
        return JSONResponse(
            status_code=503,
            content={
                "maintenance": True,
                "message": html.unescape(message),
                "check_back_at": check_at or None,
                "automatic_reopening": False,
            },
            headers={"Retry-After": retry_after, "Cache-Control": "no-store"},
        )

    check_markup = (
        f'<p class="check">Please check again on or after <time id="checkTime" datetime="{html.escape(check_at)}">{html.escape(check_at)}</time>.</p>'
        if check_at else '<p class="check">Please check again later.</p>'
    )
    localize_script = (
        "<script>const t=document.getElementById('checkTime');if(t){const d=new Date(t.dateTime);"
        "if(!Number.isNaN(d.getTime()))t.textContent=d.toLocaleString(undefined,{dateStyle:'full',timeStyle:'short'});}</script>"
        if check_at else ""
    )
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CiteIntegrity maintenance</title><style>
body{{margin:0;background:#f3f7f5;color:#172033;font-family:Arial,sans-serif;display:grid;min-height:100vh;place-items:center}}
main{{width:min(620px,calc(100% - 40px));background:#fff;border-radius:18px;padding:38px;box-shadow:0 14px 45px #15251d18;border-top:6px solid #0f7a4f}}
.eyebrow{{color:#0f7a4f;font-weight:800;letter-spacing:.08em;text-transform:uppercase;font-size:.78rem}}h1{{font-size:2rem;margin:.55rem 0 1rem}}p{{line-height:1.6}}.check{{background:#edf8f2;border-radius:10px;padding:14px;font-weight:700}}.privacy{{font-size:.9rem;color:#52605a}}a{{color:#0f6946}}
</style></head><body><main><div class="eyebrow">Scheduled upgrade</div><h1>CiteIntegrity is temporarily unavailable</h1>
<p>{message}</p>{check_markup}<p>The check-back time is an estimate. Public access will resume after the developer completes testing and reopens the service.</p>
<p class="privacy">Uploaded manuscripts and reports continue to follow the configured temporary-storage and deletion schedule.</p>
<p><a href="/developer/access">Developer testing access</a></p></main>{localize_script}</body></html>""",
        status_code=503,
        headers={"Retry-After": retry_after, "Cache-Control": "no-store"},
    )
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

        # Do not run live Crossref/OpenAlex recovery from the web process by default.
        # This was causing long queue/result polling delays and API rate-limit loops.
        # Deep source recovery should run in the background worker/enrichment queue.
        if not suggestions and full_text and RUN_RECOVERY_SUGGESTIONS_IN_WEB:
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

        recovery_context = ""
        if full_text:
            try:
                recovery_context = extract_context(full_text, citation_text, window=350) or ""
            except Exception:
                recovery_context = ""

        source_payload = _context_specific_possible_source_payload(
            text=recovery_context or citation_text,
            citation=citation_text,
            reference="",
            row_type="missing_recovery",
        )

        recovery_row = {
            "citation": citation_text,
            "count": count,
            "suggestions": suggestions,
            "message": "" if suggestions else "No evidence found.",
        }
        payload["missing_recovery"].append(_attach_context_source_payload(recovery_row, source_payload))

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

        recovery_context = ""
        if citation_text and full_text:
            try:
                recovery_context = extract_context(full_text, citation_text, window=350) or ""
            except Exception:
                recovery_context = ""

        source_payload = _context_specific_possible_source_payload(
            text=recovery_context or matched_title or original_ref,
            citation=citation_text,
            reference=original_ref,
            row_type="verification_recovery",
        )

        recovery_row = {
            "reference": original_ref,
            "status": status,
            "citation": citation_text,
            "matched_title": matched_title,
            "suggestions": suggestions,
            "message": "" if suggestions else "No evidence found.",
        }
        payload["verification_recovery"].append(_attach_context_source_payload(recovery_row, source_payload))

    return payload


def _ensure_recovery_possible_sources(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure every recovery-like row in result has context-specific possible-source fields.
    Safe to call repeatedly and safe for older saved jobs.
    """
    if not isinstance(result, dict):
        return result

    recovery = result.get("recovery") or {}
    if isinstance(recovery, dict):
        for key, row_type in [
            ("missing_recovery", "missing_recovery"),
            ("verification_recovery", "verification_recovery"),
        ]:
            rows = recovery.get(key) or []
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if row.get("possible_source_guidance"):
                        continue
                    context_text = row.get("context_snippet") or row.get("claim") or row.get("citation") or row.get("matched_title") or row.get("reference") or ""
                    payload = _context_specific_possible_source_payload(
                        text=context_text,
                        citation=row.get("citation", ""),
                        reference=row.get("reference", ""),
                        row_type=row_type,
                    )
                    _attach_context_source_payload(row, payload)

    for key in ["advanced_enrichment", "advanced_recovery", "recovery_rows", "verification_recovery"]:
        rows = result.get(key)
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("possible_source_guidance"):
                    continue
                context_text = row.get("context_snippet") or row.get("claim") or row.get("citation") or row.get("matched_title") or row.get("reference") or ""
                payload = _context_specific_possible_source_payload(
                    text=context_text,
                    citation=row.get("citation", ""),
                    reference=row.get("reference", ""),
                    row_type="recovery",
                )
                _attach_context_source_payload(row, payload)

    return result



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
                                        _ensure_recovery_possible_sources(_store[job_id]["result"])
                                        print("[RECOVERY] Rows built with context-specific possible-source guidance")
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



def _extract_claim_key_terms(claim: str, max_terms: int = 10) -> List[str]:
    """Extract readable key terms from a citation-needed claim for focused searches."""
    text = re.sub(r"\s+", " ", str(claim or "")).strip()
    stop = {
        "the", "and", "for", "with", "that", "this", "from", "into", "were", "was",
        "are", "been", "being", "have", "has", "had", "can", "may", "might", "will",
        "would", "should", "could", "study", "research", "result", "results", "finding",
        "findings", "therefore", "however", "also", "more", "most", "than", "then"
    }
    raw_terms = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", text)
    terms = []
    seen = set()
    for term in raw_terms:
        low = term.lower()
        if low in stop or low in seen:
            continue
        seen.add(low)
        terms.append(term)
        if len(terms) >= max_terms:
            break
    return terms


def _suggested_source_type_for_claim(claim: str) -> str:
    """Recommend a source type based on the nature of the uncited claim."""
    c = str(claim or "").lower()
    if re.search(r"\b(percent|percentage|rate|increase|decrease|prevalence|average|mean|median|ratio|statistic|data|survey|sample|respondents)\b", c):
        return "Empirical study, dataset, official statistics, survey report, or methods paper"
    if re.search(r"\b(policy|regulation|law|act|standard|guideline|framework|compliance|authority|ministry|commission)\b", c):
        return "Policy document, legal instrument, institutional guideline, official report, or regulatory source"
    if re.search(r"\b(theory|model|framework|concept|construct|definition|dimension|relationship|mechanism)\b", c):
        return "Theory paper, conceptual article, textbook, or validated scale/instrument source"
    if re.search(r"\b(ghana|africa|country|national|regional|local|municipal|district|institutional)\b", c):
        return "Country-specific empirical study, official national report, or institutional publication"
    return "Peer-reviewed empirical article, authoritative book, official report, or credible scholarly source"


def _citation_needed_recommendation(claim: str) -> str:
    """Give a context-specific citation action rather than a generic search instruction."""
    source_type = _suggested_source_type_for_claim(claim)
    terms = _extract_claim_key_terms(claim, max_terms=6)
    term_text = ", ".join(terms) if terms else "the main claim keywords"
    return (
        f"Add a source that directly supports this claim. Best source type: {source_type}. "
        f"Use focused terms such as: {term_text}. If no direct source exists, soften or qualify the claim."
    )


def _build_citation_needed_search_query(claim: str) -> str:
    """Build a short robust search query to avoid broken or overly long search links."""
    terms = _extract_claim_key_terms(claim, max_terms=8)
    if terms:
        return " ".join(terms)[:180]
    return re.sub(r"\s+", " ", str(claim or "")).strip()[:180]

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

def _rq_job_age_seconds(job) -> Optional[int]:
    try:
        ts = getattr(job, "enqueued_at", None) or getattr(job, "created_at", None)
        if not ts:
            return None
        if ts.tzinfo is not None:
            age = datetime.now(ts.tzinfo) - ts
        else:
            age = datetime.utcnow() - ts
        return max(0, int(age.total_seconds()))
    except Exception:
        return None


def _rq_queue_diagnostics(queue_name: str) -> Dict[str, Any]:
    """Small, safe queue diagnostic used by /queue/status."""
    info: Dict[str, Any] = {
        "name": queue_name,
        "waiting": 0,
        "started": 0,
        "failed": 0,
        "deferred": 0,
        "oldest_waiting_seconds": None,
        "sample_job_ids": [],
        "error": None,
    }
    if not redis_conn:
        info["error"] = "Redis not configured"
        return info

    try:
        q = Queue(queue_name, connection=redis_conn)
        info["waiting"] = q.count

        try:
            jobs = q.get_jobs(offset=0, length=5)
            info["sample_job_ids"] = [j.id for j in jobs]
            ages = [_rq_job_age_seconds(j) for j in jobs]
            ages = [a for a in ages if a is not None]
            info["oldest_waiting_seconds"] = max(ages) if ages else None
        except Exception:
            pass

        try:
            info["started"] = StartedJobRegistry(queue_name, connection=redis_conn).count
        except Exception:
            pass
        try:
            info["failed"] = FailedJobRegistry(queue_name, connection=redis_conn).count
        except Exception:
            pass
        try:
            info["deferred"] = DeferredJobRegistry(queue_name, connection=redis_conn).count
        except Exception:
            pass

    except Exception as e:
        info["error"] = str(e)

    return info


def _rq_worker_diagnostics() -> List[Dict[str, Any]]:
    """Return active RQ workers and the queues they listen to."""
    out: List[Dict[str, Any]] = []
    if not redis_conn:
        return out
    try:
        for w in Worker.all(connection=redis_conn):
            queues = []
            try:
                queues = [q.name for q in getattr(w, "queues", [])]
            except Exception:
                queues = []
            try:
                state = w.get_state()
            except Exception:
                state = ""
            out.append({
                "name": getattr(w, "name", ""),
                "state": state,
                "queues": queues,
                "last_heartbeat": str(getattr(w, "last_heartbeat", "") or ""),
                "current_job_id": getattr(w, "get_current_job_id", lambda: None)(),
            })
    except Exception as e:
        out.append({"error": str(e)})
    return out


# ============================================================
# QUEUE STATUS ENDPOINT
# ============================================================

@app.get("/queue/status")
async def queue_status():
    """Queue and worker health endpoint.

    This endpoint now shows whether queues have active workers. A queue can have
    waiting jobs forever if no RQ worker is listening to that queue.
    """
    status = get_queue_status()
    status["server_busy"] = is_server_busy()
    status["message"] = "Server is busy, please try later" if status["server_busy"] else "Server is ready"

    queue_names = [
        NORMAL_DOCUMENT_QUEUE,
        LARGE_DOCUMENT_QUEUE,
        "verification",
        "deep_enrichment",
    ]

    if redis_conn:
        try:
            queue_details = {name: _rq_queue_diagnostics(name) for name in queue_names}
            workers = _rq_worker_diagnostics()

            listened_queues = set()
            for w in workers:
                for q in w.get("queues", []) or []:
                    listened_queues.add(q)

            status["queues"] = queue_details
            status["workers"] = workers
            status["listened_queues"] = sorted(listened_queues)
            status["queue_has_worker"] = {name: name in listened_queues for name in queue_names}

            status["redis_queue_length"] = queue_details.get(NORMAL_DOCUMENT_QUEUE, {}).get("waiting", 0)
            status["document_processing_queue_length"] = queue_details.get(NORMAL_DOCUMENT_QUEUE, {}).get("waiting", 0)
            status["large_document_processing_queue_length"] = queue_details.get(LARGE_DOCUMENT_QUEUE, {}).get("waiting", 0)
            status["verification_queue_length"] = queue_details.get("verification", {}).get("waiting", 0)
            status["deep_enrichment_queue_length"] = queue_details.get("deep_enrichment", {}).get("waiting", 0)

            status["queue_warning"] = ""
            unserved = [
                name for name in queue_names
                if queue_details.get(name, {}).get("waiting", 0) > 0 and name not in listened_queues
            ]
            if unserved:
                status["queue_warning"] = (
                    "Jobs are waiting in queue(s) with no active worker listening: "
                    + ", ".join(unserved)
                )

        except Exception as e:
            status["redis_queue_length"] = 0
            status["queue_error"] = str(e)
    else:
        status["redis_queue_length"] = 0
        status["queue_error"] = "Redis not configured"

    return status

# ============================================================
# INDEX
# ============================================================

_INDEX_HTML_CACHE: Optional[str] = None

def _get_cached_index_html() -> str:
    """Serve the homepage without touching queues, database, or heavy template work."""
    global _INDEX_HTML_CACHE
    if _INDEX_HTML_CACHE is None:
        index_path = Path(templates_dir) / "index.html"
        try:
            _INDEX_HTML_CACHE = index_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[HOME] Could not load cached index.html: {e}")
            _INDEX_HTML_CACHE = "<!doctype html><html><body>CiteIntegrity is starting. Please refresh.</body></html>"
    return _INDEX_HTML_CACHE

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return HTMLResponse(_get_cached_index_html())

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
async def terms_page(request: Request):
    return templates.TemplateResponse("about.html", {
        "request": request
    })
# ============================================================
# CONTACT PAGE AND EMAIL FORWARDING
# ============================================================

@app.get("/contact", response_class=HTMLResponse)
async def contact_page(request: Request):
    return templates.TemplateResponse("contact.html", {
        "request": request
    })


def _contact_recipients() -> List[str]:
    """Return support recipients from CONTACT_SUPPORT_EMAIL. Comma-separated values are supported."""
    recipients = []
    for item in str(CONTACT_SUPPORT_EMAIL or "").split(","):
        item = item.strip()
        if item and "@" in item:
            recipients.append(item)
    return recipients or ["support@citeintegrity.org"]


def send_contact_email_notification(data: Dict[str, Any]) -> bool:
    """
    Forward contact form messages to CiteIntegrity support using SMTP.

    Required Render environment variables:
    SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM_EMAIL.
    CONTACT_SUPPORT_EMAIL controls the recipient, default: support@citeintegrity.org.
    """
    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD:
        print("⚠️ SMTP not configured. Contact email notification was not sent.")
        return False

    try:
        category = str(data.get("category") or "Contact message").strip()
        subject = str(data.get("subject") or "New contact message").strip()
        sender_name = str(data.get("name") or "").strip()
        sender_email = str(data.get("email") or "").strip()

        msg = EmailMessage()
        msg["Subject"] = f"[CiteIntegrity Contact] {category}: {subject}"[:240]
        msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
        msg["To"] = ", ".join(_contact_recipients())

        if sender_email and "@" in sender_email:
            msg["Reply-To"] = sender_email

        body = f"""
New CiteIntegrity contact form message

Name: {sender_name}
Email: {sender_email}
Organisation: {data.get("organisation", "")}
Phone: {data.get("phone", "")}
Category: {category}
Reference / Job ID: {data.get("reference", "")}

Subject:
{subject}

Message:
{data.get("message", "")}

Source: {data.get("source", "contact_page")}
        """.strip()

        msg.set_content(body)

        context = ssl.create_default_context()

        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=20) as server:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)

        print(f"📧 Contact email forwarded to {msg['To']}")
        return True

    except Exception as e:
        print(f"❌ Contact email forwarding failed: {e}")
        return False


def _init_contact_messages_table():
    if not DATABASE_URL:
        return False

    try:
        with psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS contact_messages (
                        id SERIAL PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        name TEXT NOT NULL,
                        email TEXT NOT NULL,
                        organisation TEXT,
                        phone TEXT,
                        category TEXT NOT NULL,
                        reference TEXT,
                        subject TEXT NOT NULL,
                        message TEXT NOT NULL,
                        source TEXT,
                        ip_address TEXT,
                        user_agent TEXT,
                        status TEXT DEFAULT 'new',
                        email_sent BOOLEAN DEFAULT FALSE
                    )
                """)
                cursor.execute("""
                    ALTER TABLE contact_messages
                    ADD COLUMN IF NOT EXISTS email_sent BOOLEAN DEFAULT FALSE
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_contact_messages_created_at
                    ON contact_messages(created_at)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_contact_messages_category
                    ON contact_messages(category)
                """)
                conn.commit()

        return True

    except Exception as e:
        print(f"⚠️ Could not initialise contact_messages table: {e}")
        return False


@app.get("/api/contact")
async def contact_api_health():
    """Health check for the CiteIntegrity contact endpoint.

    Open https://citeintegrity.org/api/contact in a browser after deployment.
    If this returns ok=true, the active main.py has the contact API route.
    """
    smtp_ready = bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD)
    return {
        "ok": True,
        "endpoint": "/api/contact",
        "post_active": True,
        "email_forwarding_configured": smtp_ready,
        "support_email": CONTACT_SUPPORT_EMAIL,
        "smtp_host_configured": bool(SMTP_HOST),
        "smtp_username_configured": bool(SMTP_USERNAME),
        "smtp_password_configured": bool(SMTP_PASSWORD),
        "message": (
            "Contact endpoint is active. Submit the contact form using POST. "
            "Email forwarding will work when SMTP settings are configured."
        ),
    }


@app.options("/api/contact")
async def contact_api_options():
    """Allow simple endpoint checks without triggering the contact form."""
    return Response(status_code=204)


async def _read_contact_payload(request: Request) -> Dict[str, Any]:
    """Accept either JSON payloads or standard HTML form submissions."""
    content_type = (request.headers.get("content-type") or "").lower()

    if "application/json" in content_type:
        try:
            payload = await request.json()
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass

    # Try normal form parsing first.
    try:
        form = await request.form()
        if form:
            return {key: form.get(key) for key in form.keys()}
    except Exception:
        pass

    # Fallback for x-www-form-urlencoded payloads when multipart support is unavailable.
    try:
        from urllib.parse import parse_qs
        body = (await request.body()).decode("utf-8", errors="ignore")
        parsed = parse_qs(body)
        if parsed:
            return {key: values[-1] if values else "" for key, values in parsed.items()}
    except Exception:
        pass

    raise HTTPException(status_code=400, detail="Invalid contact form payload")

@app.post("/api/contact")
async def submit_contact_message(request: Request):
    payload = await _read_contact_payload(request)

    allowed_categories = {
        "General enquiry",
        "Technical support",
        "Billing or payment issue",
        "Request refund",
        "Institutional or university access",
        "Partnership enquiry",
        "Data or privacy request",
        "Report a problem",
        "Other",
    }

    # Honeypot spam check. Return a neutral success message without sending email.
    if str(payload.get("website") or "").strip():
        return {"ok": True, "message": "Received"}

    name = str(payload.get("name") or "").strip()[:180]
    email = str(payload.get("email") or "").strip()[:220]
    organisation = str(payload.get("organisation") or "").strip()[:220]
    phone = str(payload.get("phone") or "").strip()[:80]
    category = str(payload.get("category") or "").strip()[:120]
    reference = str(payload.get("reference") or "").strip()[:180]
    subject = str(payload.get("subject") or "").strip()[:240]
    message = str(payload.get("message") or "").strip()[:8000]
    source = str(payload.get("source") or "contact_page").strip()[:120]
    consent = bool(payload.get("consent"))

    if not name or not email or not category or not subject or not message or not consent:
        raise HTTPException(status_code=400, detail="Missing required contact fields")

    if category not in allowed_categories:
        category = "Other"

    if "@" not in email or "." not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    ip_address = request.client.host if request.client else ""
    user_agent = request.headers.get("user-agent", "")[:500]

    contact_data = {
        "name": name,
        "email": email,
        "organisation": organisation,
        "phone": phone,
        "category": category,
        "reference": reference,
        "subject": subject,
        "message": message,
        "source": source,
    }

    email_sent = send_contact_email_notification(contact_data)

    if CONTACT_REQUIRE_EMAIL_SENT and not email_sent:
        raise HTTPException(
            status_code=503,
            detail=(
                "The contact endpoint is active, but the email notification could not be sent. "
                "Please check SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD and SMTP_FROM_EMAIL."
            )
        )

    saved = False
    saved_id = None

    if DATABASE_URL:
        try:
            _init_contact_messages_table()

            with psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO contact_messages
                        (name, email, organisation, phone, category, reference, subject, message, source, ip_address, user_agent, email_sent)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            name,
                            email,
                            organisation,
                            phone,
                            category,
                            reference,
                            subject,
                            message,
                            source,
                            ip_address,
                            user_agent,
                            email_sent,
                        ),
                    )

                    row = cursor.fetchone()
                    conn.commit()
                    saved = True
                    saved_id = row.get("id") if isinstance(row, dict) else None

                    print(
                        f"📩 Contact message saved: "
                        f"#{saved_id or 'unknown'} - {category} - {email} - email_sent={email_sent}"
                    )

        except Exception as e:
            print(f"❌ Contact message save failed: {e}")

    return {
        "ok": True,
        "saved": saved,
        "email_sent": email_sent,
        "support_email": CONTACT_SUPPORT_EMAIL,
        "message": "Your message has been received. CiteIntegrity support will respond through the email address provided."
    }



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
            "auto_verify": "true" if verify == 1 else "false",
            "demo_mode": "true" if DEMO_UNLOCK_ALL_FEATURES else "false",
        }
    )


@app.get("/new/results/{job_id}", response_class=HTMLResponse)
async def new_results_dashboard_page(request: Request, job_id: str, verify: int = 0):
    return templates.TemplateResponse(
        "new_results.html",
        {
            "request": request,
            "job_id": job_id,
            "auto_verify": "true" if verify == 1 else "false",
            "demo_mode": "true" if DEMO_UNLOCK_ALL_FEATURES else "false",
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
    enable_academic_voice: str = Form("false"),
    request: Request = None
):
    # Convert string to boolean
    autofix_enabled = enable_autofix.lower() == "true"
    online_verify_enabled = enable_online_verification.lower() == "true"
    academic_voice_enabled = enable_academic_voice.lower() == "true"
    
    print(f"📚 Received citation style: {style}")
    print(f"📋 Received enable_autofix string: {enable_autofix}")
    print(f"📋 Converted to bool: {autofix_enabled}")
    print(f"📋 Received enable_online_verification: {online_verify_enabled}")
    print(f"📋 Academic Voice and Writing Signals enabled: {academic_voice_enabled}")
    
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
                "large_file": is_large_file,
                "analysis_options": {
                    "academic_voice_enabled": academic_voice_enabled,
                },
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

    large_worker_autostart = {
        "started": False,
        "reason": "not a large-file route",
        "queue_name": queue_name,
    }

    try:
        target_queue = Queue(queue_name, connection=redis_conn)

        # 🔥 FIX: Use the autofix_enabled variable instead of hardcoded True
        target_queue.enqueue(
            "worker.process_document",
            job_id,
            file.filename,
            style,
            autofix_enabled,  # CHANGE: Use the variable, not hardcoded True
            academic_voice_enabled,
            job_timeout=job_timeout,
            result_ttl=86400,
            failure_ttl=86400
        )

        print(
            f"🔥 Job {job_id} queued successfully on {queue_name} "
            f"with autofix={autofix_enabled}, timeout={job_timeout}s"
        )

        # Automatic pay-as-you-use large-file processing.
        # When a file is routed to large_document_processing, start a temporary
        # high-memory Render One-Off Job that drains the large queue and exits.
        if queue_name == LARGE_DOCUMENT_QUEUE:
            try:
                large_worker_autostart = trigger_large_worker_if_needed(
                    redis_conn,
                    reason=f"large upload {job_id}"
                )
                print(f"[LARGE WORKER AUTOSTART] {large_worker_autostart}")
            except Exception as autostart_error:
                large_worker_autostart = {
                    "started": False,
                    "reason": f"autostart exception: {autostart_error}",
                    "queue_name": queue_name,
                }
                print(f"[LARGE WORKER AUTOSTART ERROR] {autostart_error}")

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
        "academic_voice_enabled": academic_voice_enabled,
        "selected_style": _main_style_token(style),
        "large_worker_autostart": large_worker_autostart,
        "large_worker_autostart_enabled": os.environ.get("LARGE_WORKER_AUTOSTART_ENABLED", "false"),
    }




@app.get("/admin/large-worker/autostart-status")
async def large_worker_autostart_status(_auth: Any = Depends(authenticate)):
    """Admin diagnostic: shows whether automatic large-worker launch is configured. API key is masked."""
    api_key = os.environ.get("RENDER_API_KEY", "")
    return {
        "enabled": os.environ.get("LARGE_WORKER_AUTOSTART_ENABLED", "false"),
        "base_service_id": os.environ.get("RENDER_LARGE_WORKER_BASE_SERVICE_ID", ""),
        "plan_id": os.environ.get("RENDER_LARGE_WORKER_PLAN_ID", ""),
        "command": os.environ.get("RENDER_LARGE_WORKER_COMMAND", ""),
        "has_api_key": bool(api_key),
        "api_key_prefix": (api_key[:6] + "..." if api_key else ""),
        "lock_key": "citeintegrity:large-worker:autostart-lock",
        "lock_exists": bool(redis_conn.get("citeintegrity:large-worker:autostart-lock")) if redis_conn else False,
        "last_render_job": (redis_conn.get("citeintegrity:large-worker:last-render-job") or b"").decode("utf-8", errors="ignore")[:1000] if redis_conn else "",
    }

@app.api_route("/admin/large-worker/clear-autostart-lock", methods=["GET", "POST"])
async def clear_large_worker_autostart_lock_endpoint(_auth: Any = Depends(authenticate)):
    """Admin diagnostic: clears the autostart lock if a previous API call failed or a one-off job ended."""
    if not redis_conn:
        return {"ok": False, "message": "Redis not connected"}
    redis_conn.delete("citeintegrity:large-worker:autostart-lock")
    return {"ok": True, "message": "Large-worker autostart lock cleared"}


@app.get("/developer/access", response_class=HTMLResponse)
async def developer_access_page(request: Request, _auth: Any = Depends(authenticate)):
    state = get_access_mode(DATABASE_URL, redis_conn)
    mode = state.get("mode", "payment_required")
    expiry_text = html.escape(str(state.get("expires_at") or "Not applicable"))
    checked_payment = "checked" if mode == "payment_required" else ""
    checked_open = "checked" if mode == "open_access" else ""
    checked_maintenance = "checked" if mode == "maintenance" else ""
    maintenance_check_text = html.escape(str(state.get("maintenance_check_at") or "Not applicable"))
    maintenance_message_text = html.escape(str(state.get("maintenance_message") or "CiteIntegrity is undergoing scheduled maintenance while an upgrade is tested."))
    ai_status_text = "Configured" if os.environ.get("OPENAI_API_KEY", "").strip() else "Not configured"
    response = HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>CiteIntegrity Developer Access</title>
<style>body{{font-family:Arial,sans-serif;background:#f4f7f6;color:#172033;margin:0}}main{{max-width:760px;margin:50px auto;background:white;padding:32px;border-radius:16px;box-shadow:0 8px 30px #0001}}h1{{margin-top:0}}label{{display:block;border:1px solid #dbe4e0;padding:18px;border-radius:10px;margin:12px 0}}button,.button{{display:inline-block;background:#0f7a4f;color:white;border:0;border-radius:8px;padding:12px 18px;font-weight:700;cursor:pointer;text-decoration:none}}.button.secondary{{background:#172033}}.testing{{background:#edf8f2;border:1px solid #b9dfca;padding:18px;border-radius:12px;margin:18px 0}}.warning{{background:#fff7ed;border-left:4px solid #f59e0b;padding:12px}}code{{background:#eef2f1;padding:2px 5px}}</style></head><body><main>
<h1>Developer Access Control</h1><p>Current mode: <strong id="currentMode">{html.escape(mode.replace('_',' ').title())}</strong><br>Open-access expiry: <strong id="expiryTime">{expiry_text}</strong><br>Maintenance check-back time: <strong id="maintenanceCheck">{maintenance_check_text}</strong></p>
<p><strong>Deployment under test:</strong> release {html.escape(RELEASE_VERSION)}, slot {html.escape(RELEASE_SLOT)}. Confirm this identity before promoting a preview deployment.</p>
<p><strong>Optional AI academic rewriting:</strong> {ai_status_text}. Citation checking and scholarly source search remain available without an AI key.</p>
<div class="testing"><strong>Choose the developer testing level for this browser.</strong><p><strong>Full Access</strong> opens the whole product testing experience. <strong>Full Review Unlocked</strong> opens paid manuscript review outputs without opening access to public users.</p><a class="button" href="/developer/testing?level=full_access">Open Full Access</a> <a class="button" href="/developer/testing?level=full_review">Open Full Review Unlocked</a> <a class="button secondary" href="/developer/logout">End Developer Session</a></div>
<form id="accessForm">
<label><input type="radio" name="mode" value="payment_required" {checked_payment}> <strong>Payment-controlled access</strong><br>Free preview is limited. Full Review requires a successful payment or valid entitlement.</label>
<label><input type="radio" name="mode" value="open_access" {checked_open}> <strong>Temporarily open Full Review for all users</strong><br>All completed analyses receive Full Review access without payment until the selected period expires.</label>
<label><input type="radio" name="mode" value="maintenance" {checked_maintenance}> <strong>Block public usage for maintenance</strong><br>Public users see a maintenance notice and check-back time. Authenticated developers retain full access for upgrade testing.</label>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:12px 0"><label style="margin:0"><strong>Duration or check-back period</strong><br><input type="number" name="duration_value" min="1" max="365" value="1" style="width:90%;padding:10px;margin-top:8px"></label><label style="margin:0"><strong>Period</strong><br><select name="duration_unit" style="width:95%;padding:10px;margin-top:8px"><option value="hours">Hours</option><option value="days">Days</option><option value="weeks">Weeks</option></select></label></div>
<label><strong>Maintenance notice</strong><br><textarea name="maintenance_message" rows="3" maxlength="500" style="width:95%;padding:10px;margin-top:8px">{maintenance_message_text}</textarea></label>
<p class="warning">Open access is a global commercial setting. It does not disable document privacy or deletion controls.</p>
<button type="submit">Save access mode</button> <span id="status"></span>
</form><script>
document.getElementById('accessForm').addEventListener('submit', async (event) => {{
 event.preventDefault(); const form=new FormData(event.target); const mode=form.get('mode'); const duration_value=Number(form.get('duration_value')); const duration_unit=form.get('duration_unit'); const maintenance_message=form.get('maintenance_message');
 if(mode==='open_access' && !confirm('Open Full Review access to every user without payment?')) return;
 if(mode==='maintenance' && !confirm('Block all public usage and show the maintenance notice? Developer authentication will still allow testing.')) return;
 const response=await fetch('/api/developer/access',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{mode,duration_value,duration_unit,maintenance_message}})}});
 const data=await response.json(); document.getElementById('status').textContent=response.ok?'Saved.':(data.detail||'Failed.');
 if(response.ok) {{ document.getElementById('currentMode').textContent=data.mode.replaceAll('_',' '); document.getElementById('expiryTime').textContent=data.expires_at||'Not applicable'; document.getElementById('maintenanceCheck').textContent=data.maintenance_check_at||'Not applicable'; }}
}});
</script></main></body></html>""")
    return response


@app.get("/developer/testing")
async def developer_testing(level: str = "full_access", _auth: Any = Depends(authenticate)):
    level = level if level in DEVELOPER_ACCESS_LEVELS else "full_access"
    response = RedirectResponse(url=f"/?developer_testing={level}", status_code=303)
    response.set_cookie(
        DEVELOPER_SESSION_COOKIE,
        create_developer_session_token(level),
        max_age=DEVELOPER_SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.get("/developer/logout")
async def developer_logout():
    response = HTMLResponse("""<!doctype html><html><head><meta charset="utf-8"><title>Developer session ended</title></head><body style="font-family:Arial,sans-serif;padding:50px"><h1>Developer session ended</h1><p>This browser no longer bypasses maintenance mode.</p><p><a href="/developer/access">Sign in again</a></p></body></html>""")
    response.delete_cookie(DEVELOPER_SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


@app.get("/api/developer/access")
async def developer_access_status(_auth: Any = Depends(authenticate)):
    return get_access_mode(DATABASE_URL, redis_conn)


@app.post("/api/developer/access")
async def developer_access_update(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    authenticate(credentials)
    payload = await request.json()
    try:
        return set_access_mode(str(payload.get("mode") or ""), credentials.username, DATABASE_URL, redis_conn, payload.get("duration_value"), str(payload.get("duration_unit") or "hours"), str(payload.get("maintenance_message") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


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


def _bump_feature_usage(result: Dict[str, Any], **increments: int) -> Dict[str, Any]:
    """Record count-only product telemetry inside the temporary job result.

    The values are deliberately limited to feature names and integer counters.
    No manuscript passage, citation, candidate title, DOI, author or search query
    is stored here, so the counters may safely remain after content deletion and
    feed the password-protected aggregate statistics dashboard.
    """
    usage = result.setdefault("feature_usage", {})
    if not isinstance(usage, dict):
        usage = {}
        result["feature_usage"] = usage
    usage["schema_version"] = 1
    for key, amount in increments.items():
        clean_key = re.sub(r"[^a-z0-9_]+", "_", str(key or "").strip().lower()).strip("_")
        if not clean_key:
            continue
        usage[clean_key] = max(0, _safe_int(usage.get(clean_key))) + max(0, _safe_int(amount))
    usage["last_activity_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    return usage


def _snapshot_feature_usage(result: Dict[str, Any]) -> Dict[str, Any]:
    """Freeze current count-only outcomes before detailed job content is purged."""
    metrics = _extract_feature_metrics(result)
    usage = result.setdefault("feature_usage", {})
    if not isinstance(usage, dict):
        usage = {}
        result["feature_usage"] = usage
    for key in (
        "evidence_workspace_analyses", "evidence_issues_generated",
        "evidence_issues_pending", "evidence_issues_resolved",
        "tracked_approvals_current", "academic_voice_enabled_analyses",
        "academic_voice_completed_analyses", "academic_voice_signals",
        "academic_voice_approved_revisions",
    ):
        usage[key] = _safe_int(metrics.get(key))
    usage["schema_version"] = 1
    usage["snapshot_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    return usage


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



def _manual_evidence_records(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    manual = result.setdefault("manual_verification", {})
    records = manual.setdefault("evidence", [])
    return records if isinstance(records, list) else []


def _manual_evidence_matches_key(record: Dict[str, Any], ref_key: str) -> bool:
    if not isinstance(record, dict) or not ref_key:
        return False
    record_key = record.get("manual_reference_key") or _manual_reference_key(record.get("reference") or "")
    return bool(record_key and (record_key == ref_key or record_key in ref_key or ref_key in record_key))


def _manual_evidence_for_key(result: Dict[str, Any], ref_key: str) -> List[Dict[str, Any]]:
    return [r for r in _manual_evidence_records(result) if _manual_evidence_matches_key(r, ref_key)]


def _manual_add_evidence_record(result: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    records = _manual_evidence_records(result)
    key = record.get("manual_reference_key") or _manual_reference_key(record.get("reference") or "")
    url = str(record.get("evidence_url") or "").strip()
    source = str(record.get("evidence_source") or "").strip()
    evidence_type = str(record.get("evidence_type") or "").strip()

    # Upsert by reference key + source + URL so repeated clicks do not inflate evidence counts.
    for existing in records:
        if (
            existing.get("manual_reference_key") == key
            and str(existing.get("evidence_url") or "").strip() == url
            and str(existing.get("evidence_source") or "").strip().lower() == source.lower()
            and str(existing.get("evidence_type") or "").strip().lower() == evidence_type.lower()
        ):
            existing.update({k: v for k, v in record.items() if v not in (None, "")})
            return existing

    records.append(record)
    return record




def _manual_attach_evidence_to_verified_decisions(result: Dict[str, Any], ref_key: str, record: Dict[str, Any]) -> int:
    """Attach a newly recorded evidence link to any existing manual_verified decision.

    This makes the certificate robust when a user clicks an evidence link shortly
    before or after marking the reference as manually verified. The evidence is
    stored once in manual_verification.evidence and also attached to the matching
    verified decision so certificate generation can include it reliably.
    """
    if not isinstance(result, dict) or not ref_key or not isinstance(record, dict):
        return 0
    manual = result.setdefault("manual_verification", {})
    decisions = manual.setdefault("decisions", [])
    attached_count = 0
    record_url = str(record.get("evidence_url") or record.get("url") or "").strip()

    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        if str(decision.get("decision") or "").lower() != "manual_verified":
            continue
        decision_key = decision.get("manual_reference_key") or _manual_reference_key(
            decision.get("reference") or (decision.get("candidate") or {}).get("title") or ""
        )
        if not decision_key or not (decision_key == ref_key or decision_key in ref_key or ref_key in decision_key):
            continue

        evidence_records = decision.setdefault("evidence_records", [])
        if not isinstance(evidence_records, list):
            evidence_records = []
            decision["evidence_records"] = evidence_records

        duplicate = False
        for existing in evidence_records:
            if not isinstance(existing, dict):
                continue
            if record_url and str(existing.get("evidence_url") or existing.get("url") or "").strip() == record_url:
                duplicate = True
                break
        if not duplicate:
            evidence_records.append(record)
        decision["manual_evidence_recorded"] = True
        attached_count += 1
    return attached_count

def _manual_build_summary(result: Dict[str, Any]) -> Dict[str, int]:
    manual = result.setdefault("manual_verification", {})
    decisions = manual.get("decisions") or []
    evidence = manual.get("evidence") or []

    out = {
        "manual_verified": 0,
        "manual_verified_with_evidence": 0,
        "manual_verified_without_evidence": 0,
        "manual_not_verified": 0,
        "not_indexed_but_plausible": 0,
        "keep_needs_review": 0,
        "manual_evidence_links_recorded": len(evidence),
        "google_scholar_evidence_links_recorded": 0,
        "unique_manual_decisions": len(decisions),
        "total_manual_decisions": len(decisions),
    }

    for ev in evidence:
        source = str((ev or {}).get("evidence_source") or "").lower()
        url = str((ev or {}).get("evidence_url") or "").lower()
        if "google scholar" in source or "scholar.google" in url:
            out["google_scholar_evidence_links_recorded"] += 1

    for d in decisions:
        decision = str((d or {}).get("decision") or "").lower()
        ref_key = (d or {}).get("manual_reference_key") or _manual_reference_key((d or {}).get("reference") or "")
        has_ev = bool((d or {}).get("evidence_records") or _manual_evidence_for_key(result, ref_key))

        if decision == "manual_verified":
            out["manual_verified"] += 1
            if has_ev:
                out["manual_verified_with_evidence"] += 1
            else:
                out["manual_verified_without_evidence"] += 1
        elif decision == "manual_not_verified":
            out["manual_not_verified"] += 1
        elif decision == "not_indexed_but_plausible":
            out["not_indexed_but_plausible"] += 1
        elif decision == "keep_needs_review":
            out["keep_needs_review"] += 1

    return out


@app.post("/api/manual-verify/evidence")
async def api_manual_verify_evidence(request: Request):
    payload = await request.json()
    job_id = str(payload.get("job_id") or "").strip()
    reference = _manual_norm(payload.get("reference") or "")
    evidence_url = str(payload.get("evidence_url") or "").strip()
    evidence_source = _manual_norm(payload.get("evidence_source") or payload.get("source") or "Manual search")
    evidence_type = _manual_norm(payload.get("evidence_type") or "search_result")

    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")
    if not reference:
        raise HTTPException(status_code=400, detail="reference is required")
    if not evidence_url:
        raise HTTPException(status_code=400, detail="evidence_url is required")

    result = _manual_load_result(job_id)
    ref_key = _manual_reference_key(reference)
    stamp = datetime.utcnow().isoformat()

    record = _manual_add_evidence_record(result, {
        "reference": reference,
        "manual_reference_key": ref_key,
        "evidence_source": evidence_source,
        "evidence_type": evidence_type,
        "evidence_url": evidence_url,
        "opened_at": stamp,
        "recorded_at": stamp,
        "recorded_by": DEMO_ACCESS_EMAIL if DEMO_UNLOCK_ALL_FEATURES else "user",
    })

    _manual_attach_evidence_to_verified_decisions(result, ref_key, record)

    result["manual_verification_summary"] = _manual_build_summary(result)
    result["certificate_state"] = {
        "requires_regeneration": True,
        "manual_verification_updated_after_certificate": True,
        "last_manual_verification_at": stamp,
        "last_certificate_generated_at": ((result.get("citation_integrity_certificate") or {}).get("generated_at") or ""),
        "reason": "Manual verification evidence was recorded. Generate an updated certificate to include it.",
    }
    result.pop("citation_integrity_certificate", None)
    _manual_save_result(job_id, result)

    return {
        "ok": True,
        "message": "Manual verification evidence recorded.",
        "evidence": record,
        "manual_verification_summary": result.get("manual_verification_summary"),
        "certificate_update_required": True,
        "certificate_state": result.get("certificate_state"),
    }


@app.post("/api/manual-verify/decision")
async def api_manual_verify_decision(request: Request):
    payload = await request.json()
    job_id = str(payload.get("job_id") or "").strip()
    reference = _manual_norm(payload.get("reference") or "")
    decision = str(payload.get("decision") or "").strip().lower()
    candidate = payload.get("candidate") or {}
    note = _manual_norm(payload.get("note") or "")
    incoming_evidence = payload.get("evidence") or payload.get("evidence_record") or {}
    incoming_evidence_records = payload.get("evidence_records") or []
    if isinstance(incoming_evidence, dict) and incoming_evidence:
        incoming_evidence_records = list(incoming_evidence_records or []) + [incoming_evidence]

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

    # Evidence links clicked immediately before the manual decision are sent with
    # the decision payload as a fallback. Upsert them before evidence is matched
    # to the manual_verified decision record.
    for ev in incoming_evidence_records if isinstance(incoming_evidence_records, list) else []:
        if not isinstance(ev, dict):
            continue
        ev_url = str(ev.get("evidence_url") or ev.get("url") or "").strip()
        if not ev_url:
            continue
        ev_record = _manual_add_evidence_record(result, {
            "reference": reference,
            "manual_reference_key": ref_key,
            "evidence_source": _manual_norm(ev.get("evidence_source") or ev.get("source") or "Manual search"),
            "evidence_type": _manual_norm(ev.get("evidence_type") or ev.get("type") or "search_result"),
            "evidence_url": ev_url,
            "opened_at": ev.get("opened_at") or stamp,
            "recorded_at": ev.get("recorded_at") or stamp,
            "recorded_by": DEMO_ACCESS_EMAIL if DEMO_UNLOCK_ALL_FEATURES else "user",
        })
        _manual_attach_evidence_to_verified_decisions(result, ref_key, ev_record)

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

    # If the user selected a candidate with a DOI/source URL, store that as evidence too.
    candidate_url = str(candidate.get("url") or candidate.get("source_url") or "").strip() if isinstance(candidate, dict) else ""
    candidate_doi = str(candidate.get("doi") or "").strip() if isinstance(candidate, dict) else ""
    candidate_source = str(candidate.get("source") or "Manual search candidate").strip() if isinstance(candidate, dict) else "Manual search candidate"
    if candidate_doi or candidate_url:
        evidence_url = candidate_url or f"https://doi.org/{candidate_doi.replace('https://doi.org/', '').strip()}"
        candidate_evidence_record = _manual_add_evidence_record(result, {
            "reference": reference,
            "manual_reference_key": ref_key,
            "evidence_source": candidate_source,
            "evidence_type": "source_record" if candidate_doi else "candidate_record",
            "evidence_url": evidence_url,
            "opened_at": stamp,
            "recorded_at": stamp,
            "recorded_by": DEMO_ACCESS_EMAIL if DEMO_UNLOCK_ALL_FEATURES else "user",
        })
        _manual_attach_evidence_to_verified_decisions(result, ref_key, candidate_evidence_record)

    # Attach any recorded evidence to the decision itself so the certificate can show
    # whether the manual decision was user-attested with evidence or without evidence.
    matching_evidence = _manual_evidence_for_key(result, ref_key)
    decision_record["evidence_records"] = matching_evidence
    decision_record["manual_evidence_recorded"] = bool(matching_evidence)

    # Re-upsert the enriched decision record after evidence was attached.
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

    result["manual_verification_summary"] = _manual_build_summary(result)
    result["certificate_state"] = {
        "requires_regeneration": True,
        "manual_verification_updated_after_certificate": True,
        "last_manual_verification_at": stamp,
        "last_certificate_generated_at": ((result.get("citation_integrity_certificate") or {}).get("generated_at") or ""),
        "reason": "Manual verification changed after the last certificate. Generate an updated certificate.",
    }
    result.pop("citation_integrity_certificate", None)

    _manual_save_result(job_id, result)
    return {
        "ok": True,
        "message": f"Manual decision recorded: {decision_payload['manual_decision_label']}. Certificate update required.",
        "updated_rows": touched,
        "manual_verification_summary": result.get("manual_verification_summary"),
        "certificate_update_required": True,
        "certificate_state": result.get("certificate_state"),
        "result": result,
    }


# ============================================================
# TRAINING / DEMO FULL-REVIEW UNLOCK HELPERS
# ============================================================

def _demo_full_access_payload(job_id: str = "") -> Dict[str, Any]:
    """Return a paid-style access payload without exposing payment details."""
    return {
        "paid": True,
        "demo_unlocked": True,
        "training_mode": True,
        "source": "training_demo",
        "user_email": DEMO_ACCESS_EMAIL,
        "message": "Training/demo mode: all Full Review features are enabled.",
        "package": {
            "name": "Training Demonstration Full Review",
            "document_tier_name": "Training Demonstration",
            "tier_key": "training_demo_full_review",
            "runs_total": None,
            "runs_used": None,
        },
        "purchase": {
            "payment_provider": "training_demo",
            "status": "demo_unlocked",
            "preview_job_id": job_id,
            "email": DEMO_ACCESS_EMAIL,
        },
    }


def _demo_is_locked_payload(value: Any) -> bool:
    return isinstance(value, dict) and value.get("locked") is True


def _demo_extract_document_title(result: Dict[str, Any]) -> str:
    """Best-effort title extraction for certificate display."""
    if not isinstance(result, dict):
        return "Untitled document"

    candidates = [
        result.get("document_title"),
        result.get("title"),
        result.get("manuscript_title"),
        result.get("file_title"),
        (result.get("metadata") or {}).get("title") if isinstance(result.get("metadata"), dict) else None,
        result.get("filename"),
        result.get("file_name"),
    ]

    for value in candidates:
        value = str(value or "").strip()
        if value:
            value = re.sub(r"\.(docx|pdf|doc|txt)$", "", value, flags=re.I).strip()
            value = value.replace("_", " ").replace("-", " ")
            value = re.sub(r"\s+", " ", value).strip()
            if value:
                return value[:220]

    return "Untitled document"


def _demo_main_text(result: Dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return (
        str(result.get("main_text") or "")
        or str(result.get("full_text") or "")
        or str(data.get("main_text") or "")
        or str(data.get("full_text") or "")
        or ""
    )


def _demo_sentence_has_citation(sentence: str) -> bool:
    if not sentence:
        return False
    # Author-year, numeric square, numeric superscript-ish, and common DOI/URL evidence.
    patterns = [
        r"\([A-Z][A-Za-z'’\-]+(?:\s+et\s+al\.)?,\s*\d{4}[a-z]?\)",
        r"\[[0-9,\-\s]+\]",
        r"\bdoi\b|https?://",
    ]
    return any(re.search(p, sentence) for p in patterns)



def _citation_needed_is_results_statistics_sentence(sentence: str) -> bool:
    """
    Suppress Citation Needed false positives from the author's own work.

    The detector should not flag methods, data-processing decisions, sampling,
    questionnaire administration, regression/ANOVA/correlation output, descriptive
    statistics, table narration, response-rate statements, chapter summaries,
    conclusions, recommendations, or reported findings from the current study.

    It should focus on external literature/background claims that lack citation.
    """
    s = re.sub(r"\s+", " ", str(sentence or "")).strip()
    if not s:
        return False

    low = s.lower()

    # 1. Methods / methodology / design / sampling / data collection statements.
    method_patterns = [
        r"\bthis study\s+(adopts?|employs?|uses?|used|relies|relied|focuses|examines|investigates|seeks|aims)\b",
        r"\bthe study\s+(adopts?|employs?|uses?|used|relies|relied|focuses|examines|investigates|seeks|aims)\b",
        r"\bquantitative\s+(research\s+)?approach\b",
        r"\bcross[-\s]?sectional\s+survey\s+design\b",
        r"\bresearch\s+design\b",
        r"\bstudy\s+area\b",
        r"\bstudy\s+population\b",
        r"\bsampling\s+(procedure|procedures|technique|techniques|method|methods)\b",
        r"\bsample\s+size\b",
        r"\byamane\b",
        r"\bstratified\s+sampling\b",
        r"\bsimple\s+random\s+sampling\b",
        r"\bproportionate\s+allocation\b",
        r"\bquestionnaires?\s+(were\s+)?(distributed|administered|returned|retrieved|completed)\b",
        r"\badditional\s+questionnaires?\s+were\s+distributed\b",
        r"\bnon[-\s]?response\b",
        r"\bresponse\s+rate\b",
        r"\bdata\s+(were\s+)?(collected|coded|cleaned|entered|analysed|analyzed|processed)\b",
        r"\bdata\s+processing\s+and\s+analysis\b",
        r"\binferential\s+analysis\s+was\s+then\s+conducted\b",
        r"\bmultiple\s+linear\s+regression\s+because\b",
        r"\bregression\s+was\s+(used|preferred|conducted)\b",
        r"\bcronbach[’']?s?\s+alpha\b",
        r"\breliability\s+(test|analysis|statistics)\b",
        r"\bvalidity\b",
        r"\bpilot\s+test\b",
        r"\bethical\s+considerations?\b",
        r"\binformed\s+consent\b",
        r"\bbefore\s+estimating\s+the\s+model\b",
        r"\bnormality\s+of\s+residuals\b",
        r"\bmulticollinearity\b",
        r"\bhomoscedasticity\b",
        r"\bcontrol\s+variables?\s+include\b",
        r"\bthe\s+regression\s+model\s+specified\b",
    ]
    if any(re.search(p, low, flags=re.I) for p in method_patterns):
        return True

    # 2. Results / own empirical findings / table narration.
    result_patterns = [
        r"\bchapter\s+four\b",
        r"\bresults\s+and\s+discussion\b",
        r"\bresults?\s+(show|shows|showed|revealed|indicated|confirm|confirmed|suggest|suggested)\b",
        r"\bfindings?\s+(show|shows|showed|revealed|indicated|confirm|confirmed|suggest|suggested)\b",
        r"\bthe\s+findings\s+revealed\b",
        r"\bthe\s+model\s+explained\b",
        r"\bmodel\s+summary\b",
        r"\bregression\s+analysis\b",
        r"\bmultiple\s+regression\s+results?\b",
        r"\bthe\s+regression\s+analysis\s+(confirmed|indicates?|showed|revealed)\b",
        r"\banova\s+results?\b",
        r"\bcorrelation\s+analysis\b",
        r"\bdescriptive\s+analysis\b",
        r"\bdescriptive\s+statistics\b",
        r"\btable\s+\d+\s+(shows?|presents?|reports?|summarises?|summarizes?)\b",
        r"\bfigure\s+\d+\s+(shows?|presents?|illustrates?)\b",
        r"\bhighest[-\s]?rated\s+challenge\b",
        r"\blowest[-\s]?rated\b",
        r"\branked\s+(first|second|third|fourth|fifth|\d+)\b",
        r"\bmean\s+and\s+standard\s+deviation\b",
        r"\bm\s*=\s*\d",
        r"\bsd\s*=\s*\d",
        r"\bβ\s*=\s*[-+]?\d",
        r"\bbeta\s*=\s*[-+]?\d",
        r"\bp\s*[<=>]\s*0?\.\d+",
        r"\br\s*[- ]?square\b",
        r"\br²\b",
        r"\bf\s*\(.*?\)\s*=\s*\d",
        r"\bt\s*\(.*?\)\s*=\s*\d",
        r"\bcoefficient\b.*\bp\s*[<=>]",
        r"\bstatistically\s+significant\s+(effect|relationship|association|influence)\b",
        r"\bpositive\s+and\s+statistically\s+significant\b",
        r"\bnegative\s+and\s+statistically\s+significant\b",
    ]
    if any(re.search(p, low, flags=re.I) for p in result_patterns):
        return True

    # 3. Chapter 5 own summary/conclusion/recommendation language.
    if re.search(r"\b(the\s+study\s+concludes|the\s+study\s+recommends|it\s+recommends|summary\s+of\s+findings|chapter\s+summary)\b", low):
        return True

    # 4. Statistical/numeric reporting plus own-results verbs.
    has_stat = bool(re.search(
        r"\b\d+(?:\.\d+)?\s*%|\bpercent\b|\bmean\b|\bmedian\b|\bsd\b|\bstandard\s+deviation\b|\bcoefficient\b|\bp[-\s]?value\b|\bsig\.?\b|\brespondents?\b|\bquestionnaires?\b",
        low,
        flags=re.I,
    ))
    has_own_reporting = bool(re.search(
        r"\b(this\s+study|the\s+study|results?|findings?|analysis|regression|correlation|anova|table\s+\d+|model)\b",
        low,
        flags=re.I,
    ))
    if has_stat and has_own_reporting:
        return True

    return False


def _normalise_context_text_for_source(*parts: Any) -> str:
    """Join claim/citation/reference/context text for source guidance."""
    joined = " ".join(str(p or "") for p in parts if p is not None)
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined[:1200]


def _citation_needed_possible_source_payload(claim: str) -> Dict[str, Any]:
    """
    Build possible-source guidance for a Citation Needed claim.

    This does not verify the source. It gives the user a defensible direction
    for what kind of evidence should be searched for and later verified.
    """
    claim_text = str(claim or "")
    source_type = _suggested_source_type_for_claim(claim_text)
    key_terms = _extract_claim_key_terms(claim_text, max_terms=8)
    search_query = _build_citation_needed_search_query(claim_text)

    low = claim_text.lower()
    examples: List[str] = []

    if re.search(r"\b(percent|percentage|rate|increase|decrease|prevalence|average|mean|median|ratio|statistic|data|survey|sample|respondents)\b", low):
        examples = [
            "peer-reviewed empirical article",
            "official statistics or dataset",
            "survey report",
            "methodology or measurement source",
        ]
    elif re.search(r"\b(policy|regulation|law|act|standard|guideline|framework|compliance|authority|ministry|commission)\b", low):
        examples = [
            "government policy document",
            "law, regulation, or official guideline",
            "institutional report",
            "regulatory authority publication",
        ]
    elif re.search(r"\b(theory|model|framework|concept|construct|definition|dimension|relationship|mechanism)\b", low):
        examples = [
            "theory paper",
            "conceptual article",
            "validated measurement scale",
            "authoritative textbook",
        ]
    elif re.search(r"\b(ghana|africa|country|national|regional|local|municipal|district|institutional|university|public sector)\b", low):
        examples = [
            "country-specific empirical study",
            "official national report",
            "institutional publication",
            "government or development-agency report",
        ]
    else:
        examples = [
            "peer-reviewed article",
            "authoritative book",
            "official report",
            "credible scholarly source",
        ]

    guidance = (
        f"Look for a source that directly supports the claim. Possible source type: {source_type}. "
        f"Use focused terms such as: {', '.join(key_terms) if key_terms else search_query}. "
        "If no direct evidence exists, soften or qualify the claim rather than forcing a weak citation."
    )

    return {
        "possible_source": source_type,
        "possible_source_type": source_type,
        "possible_source_guidance": guidance,
        "possible_source_examples": examples,
        "key_terms": key_terms,
        "suggested_search_query": search_query,
        "suggested_sources": [
            {"label": "Google Scholar", "type": "search", "query": search_query},
            {"label": "Google", "type": "search", "query": search_query},
            {"label": "Crossref", "type": "metadata_search", "query": search_query},
        ],
    }


def _context_specific_possible_source_payload(
    text: str,
    citation: str = "",
    reference: str = "",
    row_type: str = "recovery",
) -> Dict[str, Any]:
    """
    Context-specific possible-source guidance for recovery rows.

    Used for:
    - missing citation recovery
    - verification recovery
    - advanced recovery rows

    It does not claim that a source is correct. It tells the user what kind of
    source is most defensible and what focused query should be used.
    """
    context_text = _normalise_context_text_for_source(text, citation, reference)
    source_payload = _citation_needed_possible_source_payload(context_text)

    if row_type == "missing_recovery":
        action = (
            "Find the full bibliographic source that supports the in-text citation and add it to the reference list. "
            "If the citation is not supported by the surrounding claim, replace it with a better source."
        )
    elif row_type == "verification_recovery":
        action = (
            "Use the possible source guidance to manually verify the reference title, authors, year, DOI or publisher record. "
            "If a stronger source is found, update the reference entry."
        )
    else:
        action = (
            "Use the possible source guidance to locate a source that directly supports the claim or reference context."
        )

    source_payload.update({
        "recovery_type": row_type,
        "context_snippet": context_text[:500],
        "citation": citation,
        "reference": reference,
        "recommended_action": action,
        "possible_source_note": (
            "Possible source guidance is a search direction only. The user should still open and verify the actual source before accepting it."
        ),
    })
    return source_payload


def _attach_context_source_payload(row: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Attach possible-source fields to a recovery or citation-needed row."""
    if not isinstance(row, dict):
        return row
    payload = payload if isinstance(payload, dict) else {}

    row["possible_source"] = payload.get("possible_source") or payload.get("possible_source_type") or ""
    row["possible_source_type"] = payload.get("possible_source_type") or row.get("possible_source") or ""
    row["possible_source_guidance"] = payload.get("possible_source_guidance") or ""
    row["possible_source_examples"] = payload.get("possible_source_examples") or []
    row["key_terms"] = payload.get("key_terms") or []
    row["suggested_search_query"] = payload.get("suggested_search_query") or ""
    row["suggested_sources"] = payload.get("suggested_sources") or []
    row["context_snippet"] = payload.get("context_snippet") or row.get("context_snippet") or ""
    row["recommended_action"] = payload.get("recommended_action") or row.get("recommended_action") or ""
    row["possible_source_note"] = payload.get("possible_source_note") or row.get("possible_source_note") or ""
    return row


def _ensure_citation_needed_possible_sources(result: Dict[str, Any]) -> Dict[str, Any]:
    """Backfill possible-source fields for existing Citation Needed rows."""
    if not isinstance(result, dict):
        return result
    rows = result.get("citation_needed_claims") or result.get("citation_needed") or result.get("claims_needing_citation")
    if not isinstance(rows, list):
        return result
    filtered_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        claim = row.get("claim") or row.get("text") or row.get("sentence") or row.get("statement") or ""
        if not claim:
            continue
        if not _citation_needed_row_should_keep(row):
            continue
        payload = _citation_needed_possible_source_payload(str(claim))
        if not row.get("possible_source_guidance"):
            _attach_context_source_payload(row, payload)
        row.setdefault("recommendation", _citation_needed_recommendation(str(claim)))
        row.setdefault("context_snippet", str(claim)[:500])
        filtered_rows.append(row)
    result["citation_needed_claims"] = filtered_rows
    if isinstance(result.get("summary"), dict):
        result["summary"]["citation_needed_claims"] = len(filtered_rows)
    return result




def _citation_needed_external_claim_marker(sentence: str) -> bool:
    """True when a sentence looks like an external/background/literature claim."""
    low = str(sentence or "").lower()
    external_patterns = [
        r"\bempirical\s+(evidence|studies|research)\s+(shows?|indicates?|suggests?|reveals?)\b",
        r"\bprior\s+(studies|research|evidence)\s+(shows?|indicates?|suggests?|reveals?)\b",
        r"\brecent\s+(studies|research|literature|scholarship)\s+(shows?|indicates?|suggests?|reveals?|emphasizes?|highlights?)\b",
        r"\bmodern\s+literature\s+(shows?|indicates?|suggests?|emphasizes?|highlights?)\b",
        r"\bthe\s+literature\s+(suggests?|shows?|indicates?|reveals?|emphasizes?|highlights?)\b",
        r"\bliterature\s+(suggests?|shows?|indicates?|reveals?|emphasizes?|highlights?)\b",
        r"\bresearch\s+(shows?|indicates?|suggests?|reveals?|demonstrates?)\b",
        r"\bstudies\s+(show|indicate|suggest|reveal|demonstrate)\b",
        r"\bhas\s+been\s+(linked|associated|shown|reported|identified)\b",
        r"\bare\s+widely\s+(recognised|recognized|accepted|used|regarded)\b",
        r"\bpublic\s+procurement\s+accounts\s+for\b",
        r"\bthere\s+is\s+(also\s+)?(a\s+)?lack\s+of\s+empirical\s+evidence\b",
        r"\bgap\s+in\s+the\s+(literature|evidence)\b",
        r"\bexisting\s+(literature|studies|evidence)\b",
        r"\bprevious\s+(studies|research)\b",
        r"\bvalue\s+for\s+money\s+refers\b",
        r"\be[-\s]?procurement\s+is\s+(viewed|considered|defined|expected)\b",
    ]
    return any(re.search(p, low, flags=re.I) for p in external_patterns)


def _citation_needed_row_should_keep(row: Dict[str, Any]) -> bool:
    """Filter existing/stale Citation Needed rows so old false positives do not remain."""
    if not isinstance(row, dict):
        return False
    claim = str(row.get("claim") or row.get("text") or row.get("sentence") or row.get("statement") or "")
    if not claim.strip():
        return False
    if _demo_sentence_has_citation(claim):
        return False
    if _citation_needed_is_results_statistics_sentence(claim):
        return False
    # Keep only external/background/literature claims or broad statistical claims.
    if _citation_needed_external_claim_marker(claim):
        return True
    if re.search(r"\b\d+(?:\.\d+)?\s*%|\bpercent\b|\bprevalence\b|\bglobal\b|\bnational\b", claim, flags=re.I):
        return True
    return False


def _demo_paragraph_has_citation(paragraph: str) -> bool:
    return _demo_sentence_has_citation(paragraph or "")


def _make_search_source(title: str, query: str, source_type: str = "search") -> Dict[str, Any]:
    q = re.sub(r"\s+", " ", str(query or "")).strip()[:180]
    return {
        "title": title,
        "suggested": title,
        "year": "Review required",
        "authors": "Search result",
        "relevance": "context-specific search",
        "confidence": "review",
        "source_type": source_type,
        "query": q,
        "url": "https://scholar.google.com/scholar?q=" + quote_plus(q) if q else "",
        "reason": "Generated from the extracted claim/context; open and verify before using.",
    }


def _context_terms_for_query(text: str, authors: str = "", citation: str = "") -> str:
    raw = _normalise_context_text_for_source(text, authors, citation)
    terms = _extract_claim_key_terms(raw, max_terms=10)
    if authors:
        for a in re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", str(authors))[:3]:
            if a not in terms:
                terms.insert(0, a)
    return " ".join(terms[:10])[:180] or raw[:180]


def _demo_enrich_problem_rows_in_place(result: Dict[str, Any], scope: str = "weak_only") -> Dict[str, Any]:
    """
    Preserve the old structure: advanced enrichment updates the relevant tabs.

    - Claim Support rows get alternative_sources where status is weak/unclear/insufficient/source_needs_review.
    - Recovery rows get suggestions/deep_suggestions using claim/citation context and author/reference text.
    - Citation Needed rows get possible-source guidance and alternative search sources in the Citation Needed tab.

    No separate Advanced Enrichment results table is created.
    """
    if not isinstance(result, dict):
        return result

    full_text = _demo_main_text(result) or result.get("main_text", "") or result.get("full_text", "") or ""

    weak_statuses = {
        "source_needs_review", "insufficient_evidence", "weak", "unclear", "weak_or_unclear",
        "needs_review", "not_found", "no_evidence_found", "related_evidence", "offline"
    }

    def enrich_claim_row(row: Dict[str, Any]) -> None:
        status = str(row.get("support_status") or row.get("status") or "").lower().replace(" ", "_")
        if scope not in {"weak_only", "claim_only", "all_problem_rows"}:
            return
        if status not in weak_statuses and scope == "weak_only":
            return
        claim = str(row.get("claim") or row.get("claim_extracted") or row.get("extracted_claim") or row.get("context") or "")
        citation = str(row.get("citation") or "")
        authors = str(row.get("matched_authors") or row.get("authors") or row.get("author") or "")
        context = claim
        if citation and full_text:
            try:
                context = extract_context(full_text, citation, window=420) or claim
            except Exception:
                context = claim
        source_payload = _context_specific_possible_source_payload(context or claim, citation=citation, reference=authors, row_type="claim_support")
        _attach_context_source_payload(row, source_payload)
        query = _context_terms_for_query(context or claim, authors=authors, citation=citation)
        alt = row.get("alternative_sources") or row.get("deep_suggestions") or row.get("suggestions") or []
        if not isinstance(alt, list):
            alt = []
        if not alt:
            alt = [
                _make_search_source("Search Google Scholar for a stronger supporting source", query, "google_scholar"),
                _make_search_source("Search Crossref/OpenAlex by claim keywords", query, "metadata_search"),
            ]
        row["alternative_sources"] = alt
        row["enrichment_note"] = "Advanced enrichment updated this Claim Support row in place using the extracted claim/context."

    def enrich_recovery_row(row: Dict[str, Any], row_type: str) -> None:
        if scope not in {"weak_only", "recovery_only", "all_problem_rows"}:
            return
        citation = str(row.get("citation") or "")
        reference = str(row.get("reference") or row.get("matched_title") or "")
        base = str(row.get("context_snippet") or citation or reference or "")
        context = base
        if citation and full_text:
            try:
                context = extract_context(full_text, citation, window=420) or base
            except Exception:
                context = base
        source_payload = _context_specific_possible_source_payload(context or base, citation=citation, reference=reference, row_type=row_type)
        _attach_context_source_payload(row, source_payload)
        existing = row.get("deep_suggestions") or row.get("suggestions") or []
        if not isinstance(existing, list):
            existing = []
        if not existing:
            query = _context_terms_for_query(context or base, authors=reference, citation=citation)
            existing = [
                _make_search_source("Search Google Scholar using citation context and authors", query, "google_scholar"),
                _make_search_source("Search Crossref/OpenAlex using title/authors/year", query, "metadata_search"),
            ]
        row["suggestions"] = existing
        row["deep_suggestions"] = existing
        row["enrichment_note"] = "Advanced enrichment updated this Recovery row in place using citation context and author/reference text."

    def enrich_citation_needed_row(row: Dict[str, Any]) -> None:
        if scope not in {"weak_only", "citation_needed_only", "all_problem_rows"}:
            return
        claim = str(row.get("claim") or row.get("text") or row.get("sentence") or row.get("statement") or "")
        if not claim:
            return
        source_payload = _citation_needed_possible_source_payload(claim)
        _attach_context_source_payload(row, source_payload)
        query = row.get("suggested_search_query") or _context_terms_for_query(claim)
        alt = row.get("alternative_sources") or []
        if not isinstance(alt, list):
            alt = []
        if not alt:
            alt = [
                _make_search_source("Search Google Scholar for a source supporting this claim", query, "google_scholar"),
                _make_search_source("Search Crossref/OpenAlex for a scholarly source", query, "metadata_search"),
            ]
        row["alternative_sources"] = alt
        row["enrichment_note"] = "Advanced enrichment updated this Citation Needed row in place using the claim text."

    for row in result.get("claim_support") or []:
        if isinstance(row, dict):
            enrich_claim_row(row)

    recovery = result.get("recovery") or {}
    if isinstance(recovery, dict):
        for row in recovery.get("missing_recovery") or []:
            if isinstance(row, dict):
                enrich_recovery_row(row, "missing_recovery")
        for row in recovery.get("verification_recovery") or []:
            if isinstance(row, dict):
                enrich_recovery_row(row, "verification_recovery")

    filtered_cn = []
    for row in result.get("citation_needed_claims") or []:
        if isinstance(row, dict) and _citation_needed_row_should_keep(row):
            enrich_citation_needed_row(row)
            filtered_cn.append(row)
    result["citation_needed_claims"] = filtered_cn

    result.setdefault("summary", {})
    if isinstance(result["summary"], dict):
        result["summary"]["citation_needed_claims"] = len(result.get("citation_needed_claims") or [])

    return result

def _demo_generate_citation_needed_claims(result: Dict[str, Any], limit: int = 60) -> List[Dict[str, Any]]:
    """
    Conservative detector for external claims that may need citations.

    It deliberately suppresses the author's own methods/results/statistics and
    focuses on uncited background/literature/empirical-evidence claims.
    """
    text = _demo_main_text(result)
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p and p.strip()]
    if not paragraphs:
        paragraphs = [text]

    claim_markers = re.compile(
        r"\b(empirical\s+evidence|empirical\s+studies|prior\s+studies|recent\s+research|modern\s+literature|"
        r"literature\s+suggests|literature\s+shows|research\s+shows|studies\s+show|previous\s+studies|"
        r"has\s+been\s+linked|has\s+been\s+associated|widely\s+recognised|widely\s+recognized|"
        r"lack\s+of\s+empirical\s+evidence|gap\s+in\s+the\s+literature|public\s+procurement\s+accounts\s+for|"
        r"increase|decrease|prevalence|majority|minority|global|national|countrywide)\b",
        re.I,
    )

    rows: List[Dict[str, Any]] = []
    seen = set()
    sent_idx = 0

    for paragraph in paragraphs:
        paragraph_clean = re.sub(r"\s+", " ", paragraph).strip()
        if not paragraph_clean:
            continue
        paragraph_has_citation = _demo_paragraph_has_citation(paragraph_clean)
        sentences = re.split(r"(?<=[.!?])\s+", paragraph_clean)

        for sentence in sentences:
            clean = re.sub(r"\s+", " ", sentence or "").strip()
            sent_idx += 1
            if len(clean) < 70 or len(clean) > 420:
                continue
            if _demo_sentence_has_citation(clean):
                continue
            if _citation_needed_is_results_statistics_sentence(clean):
                continue
            # If the paragraph already has a citation, do not separately flag a
            # neighbouring sentence unless it is a clear external-gap claim.
            if paragraph_has_citation and not re.search(r"\b(lack\s+of\s+empirical\s+evidence|gap\s+in\s+the\s+literature)\b", clean, re.I):
                continue
            if not claim_markers.search(clean) and not _citation_needed_external_claim_marker(clean):
                continue

            key = clean[:160].lower()
            if key in seen:
                continue
            seen.add(key)

            priority = "high" if re.search(r"\d+(?:\.\d+)?\s*%|prevalence|global|national", clean, re.I) else "medium"
            focused_query = _build_citation_needed_search_query(clean)
            possible_source = _citation_needed_possible_source_payload(clean)
            row = {
                "id": f"CN-{len(rows)+1:03d}",
                "claim": clean,
                "priority": priority,
                "reason": "External factual, literature, empirical-evidence or background claim without a detected supporting citation.",
                "recommendation": _citation_needed_recommendation(clean),
                "context_snippet": clean,
                "key_terms": _extract_claim_key_terms(clean),
                "suggested_source_type": possible_source.get("possible_source_type"),
                "possible_source": possible_source.get("possible_source"),
                "possible_source_type": possible_source.get("possible_source_type"),
                "possible_source_guidance": possible_source.get("possible_source_guidance"),
                "possible_source_examples": possible_source.get("possible_source_examples"),
                "suggested_search_query": focused_query,
                "suggested_sources": possible_source.get("suggested_sources"),
                "status": "unresolved",
                "source": "training_demo_detector",
                "sentence_index": sent_idx,
            }
            rows.append(row)
            if len(rows) >= limit:
                return rows

    return rows


def _demo_save_result(job_id: str, result: Dict[str, Any]) -> None:
    """Persist updated demo result to PostgreSQL, Redis, and memory where available."""
    if not job_id or not isinstance(result, dict):
        return

    with _lock:
        if job_id in _store:
            _store[job_id].setdefault("result", {})
            _store[job_id]["result"] = result

    if DATABASE_URL:
        try:
            with psycopg2.connect(DATABASE_URL) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE jobs SET result = %s::jsonb WHERE job_id = %s",
                        (json.dumps(result), job_id),
                    )
                    conn.commit()
        except Exception as e:
            print(f"[DEMO UNLOCK] Could not persist result for {job_id}: {e}")

    if redis_conn:
        try:
            redis_conn.setex(f"result:{job_id}", 3600, json.dumps(result))
        except Exception as e:
            print(f"[DEMO UNLOCK] Could not refresh Redis for {job_id}: {e}")


def _demo_prepare_full_review_result(job_id: str, result: Dict[str, Any], *, persist: bool = False) -> Dict[str, Any]:
    """Open all Full Review sections for the no-payment training/demonstration build."""
    if not isinstance(result, dict):
        return result

    changed = False

    if DEMO_UNLOCK_ALL_FEATURES:
        result["access"] = _demo_full_access_payload(job_id)
        result["payment_required"] = False
        result["locked"] = False
        result["demo_unlocked"] = True
        changed = True

    title = _demo_extract_document_title(result)
    if result.get("document_title") != title:
        result["document_title"] = title
        changed = True

    # Remove locked placeholders that commercial builds sometimes put in the result.
    for key in ("recovery", "claim_support", "citation_needed_claims"):
        if _demo_is_locked_payload(result.get(key)):
            if key == "recovery":
                result[key] = {"missing_recovery": [], "verification_recovery": []}
            else:
                result[key] = []
            changed = True

    # Recovery and claim support are expected by the dashboard and certificate,
    # but they must not be built with live lookups inside result polling/homepage web workers.
    # The worker/deep-enrichment queue should populate these sections.
    if not isinstance(result.get("recovery"), dict):
        if WEB_SAFE_RESULT_PREPARE and not RUN_RECOVERY_SUGGESTIONS_IN_WEB:
            result["recovery"] = {"missing_recovery": [], "verification_recovery": []}
            result.setdefault("deferred_sections", {})["recovery"] = "Deferred to background enrichment to keep the web service responsive."
        else:
            try:
                result["recovery"] = build_context_specific_recovery(result)
            except Exception as e:
                print(f"[DEMO UNLOCK] Recovery build failed: {e}")
                result["recovery"] = {"missing_recovery": [], "verification_recovery": []}
        changed = True

    if not isinstance(result.get("claim_support"), list):
        if WEB_SAFE_RESULT_PREPARE and not RUN_CLAIM_SUPPORT_IN_WEB:
            result["claim_support"] = []
            result.setdefault("deferred_sections", {})["claim_support"] = "Deferred to background enrichment to keep the web service responsive."
        else:
            try:
                result["claim_support"] = build_claim_support_rows(result)
            except Exception as e:
                print(f"[DEMO UNLOCK] Claim support build failed: {e}")
                result["claim_support"] = []
        changed = True

    if not isinstance(result.get("citation_needed_claims"), list):
        main_text_for_citation_needed = _demo_main_text(result)
        if WEB_SAFE_RESULT_PREPARE and len(main_text_for_citation_needed or "") > WEB_CITATION_NEEDED_MAX_CHARS:
            result["citation_needed_claims"] = []
            result.setdefault("deferred_sections", {})["citation_needed_claims"] = "Large-text citation-needed detection deferred to background enrichment."
        else:
            result["citation_needed_claims"] = _demo_generate_citation_needed_claims(result)
        changed = True

    try:
        _ensure_citation_needed_possible_sources(result)
        _ensure_recovery_possible_sources(result)
    except Exception as e:
        print(f"[DEMO ACCESS] context-specific possible-source guidance skipped: {e}")

    result.setdefault("summary", {})
    if isinstance(result["summary"], dict):
        result["summary"]["citation_needed_claims"] = len(result.get("citation_needed_claims") or [])
        result["summary"]["claim_support_rows"] = len(result.get("claim_support") or [])
        result["summary"]["document_title"] = result.get("document_title")
        changed = True

    if persist and changed:
        _demo_save_result(job_id, result)

    return result


def _demo_complete_enrichment(job_id: str, result: Dict[str, Any], scope: str = "weak_only", reason: str = "local_demo") -> Dict[str, Any]:
    """Local fallback for advanced enrichment during demonstrations."""
    result = _demo_prepare_full_review_result(job_id, result, persist=False)
    try:
        _demo_enrich_problem_rows_in_place(result, scope=scope)
    except Exception as e:
        print(f"[DEMO ENRICHMENT] In-place context-specific source enrichment skipped: {e}")
    result["enrichment"] = {
        "state": "completed",
        "scope": scope,
        "progress": 100,
        "total": 100,
        "message": "Advanced enrichment completed in training/demo mode.",
        "requested_at": datetime.utcnow().isoformat(),
        "completed_at": datetime.utcnow().isoformat(),
        "deep_recovery_ready": True,
        "deep_claim_support_ready": True,
        "citation_needed_ready": True,
        "mode": reason,
    }
    _demo_save_result(job_id, result)
    return {
        "ok": True,
        "demo_unlocked": True,
        "state": "completed",
        "message": "Advanced enrichment completed in training/demo mode.",
        "scope": scope,
        "result": result,
    }


def _demo_certificate_fallback(result: Dict[str, Any], job_id: str = "") -> Dict[str, Any]:
    """Fallback certificate if certificate_builder.py is not present."""
    result = _demo_prepare_full_review_result(job_id, result, persist=False)
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    refs = result.get("references_raw") or result.get("references") or []
    cites = result.get("in_text_citations") or result.get("citations") or result.get("reconciliation_intext_to_reference") or []
    verify_rows = ((result.get("online_verification") or {}).get("rows") or []) if isinstance(result.get("online_verification"), dict) else []
    verified = sum(1 for r in verify_rows if isinstance(r, dict) and str(r.get("status") or "").lower() == "verified")
    certificate_id = "CI-DEMO-" + (job_id[:8].upper() if job_id else datetime.utcnow().strftime("%Y%m%d"))
    generated_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    return {
        "brand_name": "CiteIntegrity",
        "certificate_title": "CiteIntegrity Submission-Readiness Certificate",
        "certificate_id": certificate_id,
        "generated_at": generated_at,
        "generated_at_display": generated_at.replace("T", " ").replace("Z", " UTC"),
        "job_id": job_id,
        "document_title": result.get("document_title") or _demo_extract_document_title(result),
        "package": "Training Demonstration Full Review",
        "total_in_text_citations": summary.get("in_text_citations_found") or len(cites),
        "total_references": summary.get("reference_entries_found") or len(refs),
        "acii_score": ((result.get("acii") or {}).get("ACII") if isinstance(result.get("acii"), dict) else None) or ((result.get("acii") or {}).get("score") if isinstance(result.get("acii"), dict) else None),
        "acii_rating": "Demo review",
        "clearance_status": "Review Recorded",
        "verified_stamp": "REVIEW RECORDED",
        "summary": {
            "automatically_verified_references": verified,
            "user_attested_manual_verification_with_evidence": 0,
            "user_attested_manual_verification_without_evidence": 0,
            "citation_needed_claims": len(result.get("citation_needed_claims") or []),
            "claim_support_issues": 0,
        },
        "risk_counts": {"critical": 0, "moderate": 0, "minor": 0},
        "clearance_requirements": ["Review certificate metrics and conduct final human review before submission."],
        "coverage_note": "Training/demo certificate generated without payment gating.",
        "validity_note": "This certificate summarises CiteIntegrity review outputs and does not replace academic supervision, journal peer review, or independent source verification.",
    }


def _demo_build_certificate(result: Dict[str, Any], job_id: str = "") -> Dict[str, Any]:
    result = _demo_prepare_full_review_result(job_id, result, persist=False)
    access = _demo_full_access_payload(job_id)
    document_title = result.get("document_title") or _demo_extract_document_title(result)
    if build_citation_integrity_certificate is not None:
        try:
            cert = build_citation_integrity_certificate(
                result,
                job_id=job_id,
                access=access,
                package_label="Training Demonstration Full Review",
                document_title=document_title,
            )
        except TypeError:
            cert = build_citation_integrity_certificate(
                result,
                job_id=job_id,
                access=access,
                package_label="Training Demonstration Full Review",
            )
    else:
        cert = _demo_certificate_fallback(result, job_id)

    cert["document_title"] = document_title
    cert["demo_unlocked"] = True
    cert["package"] = "Training Demonstration Full Review"
    cert["review_type"] = "Full Review"
    # Do not show package/run usage on the demo certificate. It is not a payment record.
    cert.pop("analysis_run", None)
    return cert

# ============================================================
# RESULT CHECK ENDPOINT
# ============================================================

def _apply_developer_testing_access(result: Dict[str, Any], request: Optional[Request] = None) -> Dict[str, Any]:
    """Unlock paid-style outputs only for the current authenticated developer request."""
    access_level = developer_session_access_level(request) if request is not None else DEVELOPER_ACCESS_LEVEL.get()
    if not access_level and request is not None and developer_request_is_authorized(request):
        access_level = "full_access"
    if access_level not in DEVELOPER_ACCESS_LEVELS or not isinstance(result, dict):
        return result
    result = _demo_prepare_full_review_result("", result, persist=False)
    level_name = "Full Access" if access_level == "full_access" else "Full Review Unlocked"
    result["access"] = {
        "paid": True,
        "developer_unlocked": True,
        "developer_access_level": access_level,
        "source": "developer_session",
        "message": f"Developer testing: {level_name}.",
        "package": {
            "name": f"Developer Testing - {level_name}",
            "document_tier_name": level_name,
            "tier_key": f"developer_{access_level}",
            "is_paid": True,
            "analysis_runs": None,
            "validity_days": None,
        },
    }
    result["payment_required"] = False
    result["locked"] = False
    result["developer_full_access"] = access_level == "full_access"
    return result


def _prepare_student_result(result: Dict[str, Any], request: Optional[Request] = None) -> Dict[str, Any]:
    """Attach explainable, lightweight student guidance to a completed result."""
    result = result or {}
    if result.get("result_deleted"):
        return attach_privacy_status(result)
    analysis_options = result.get("analysis_options") if isinstance(result.get("analysis_options"), dict) else {}
    voice_settings = result.get("academic_voice_settings") if isinstance(result.get("academic_voice_settings"), dict) else {}
    if "enabled" not in voice_settings:
        voice_settings["enabled"] = bool(analysis_options.get("academic_voice_enabled", False))
    voice_settings.update({
        "available": True,
        "default_enabled": False,
        "module_name": "Academic Voice and Writing Signals",
        "authorship_inference": False,
    })
    result["academic_voice_settings"] = voice_settings
    if voice_settings.get("enabled") is True and not result.get("academic_voice_review"):
        manuscript_text = (
            result.get("main_text") or result.get("full_text") or
            result.get("document_text") or result.get("text") or ""
        )
        if manuscript_text:
            result["academic_voice_review"] = analyse_academic_voice(manuscript_text)
    result["source_risk_review"] = assess_source_risks(result)
    result["citation_improvement_coach"] = build_citation_coach(result)
    result["correction_plan"] = build_correction_plan(result)
    result["evidence_resolution_workspace"] = result["correction_plan"].get("evidence_resolution_workspace") or {}
    access_control = get_access_mode(DATABASE_URL, redis_conn)
    result["global_access_control"] = access_control
    if access_control.get("mode") == "open_access":
        result["access"] = open_access_payload()
        result["payment_required"] = False
    result = _apply_developer_testing_access(result, request)
    return attach_privacy_status(result)


def _purge_job_content(job_id: str, reason: str = "user_requested") -> Dict[str, Any]:
    """Remove manuscript content while retaining minimal transaction/job metadata."""
    job = load_job_record_fresh(job_id)
    if not job:
        return {"ok": False, "message": "Job not found"}
    result_for_purge = job.get("result") or {}
    try:
        _snapshot_feature_usage(result_for_purge)
    except Exception as snapshot_error:
        print(f"[PRIVACY] Count-only feature snapshot failed safely for {job_id}: {snapshot_error}")
    minimal = purge_result_content(result_for_purge, reason=reason)
    if DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE jobs SET result = %s::jsonb WHERE job_id = %s",
                (json.dumps(minimal), job_id),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    if redis_conn:
        try:
            redis_conn.delete(*list(redis_content_keys(job_id)))
        except Exception as exc:
            print(f"[PRIVACY] Redis deletion failed for {job_id}: {exc}")
    with _lock:
        if job_id in _store:
            _store[job_id]["result"] = minimal
            _store[job_id].pop("fixed_document", None)
    return {"ok": True, "job_id": job_id, "privacy": minimal.get("privacy")}

@app.get("/result/{job_id}")
async def get_result(request: Request, job_id: str, fresh: int = 0):
    """Get job status and result.

    Use fresh=1 when the browser is polling for enrichment updates, so
    PostgreSQL is read directly instead of returning a possibly stale Redis value.
    """
    
    # Check Redis cache first unless a fresh PostgreSQL read is requested
    if redis_conn and not fresh:
        cached = redis_conn.get(f"result:{job_id}")
        if cached:
            try:
                cached_result = json.loads(cached)
                cached_result = _demo_prepare_full_review_result(job_id, cached_result, persist=False)
                cached_result = _prepare_student_result(cached_result, request)
                return {"status": "completed", "data": cached_result}
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
                result = _demo_prepare_full_review_result(job_id, result, persist=True)
                result = _prepare_student_result(result, request)
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


@app.get("/api/privacy/{job_id}")
async def get_privacy_status(job_id: str):
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = attach_privacy_status(job.get("result") or {})
    return {"job_id": job_id, "privacy": result.get("privacy")}


@app.delete("/api/privacy/{job_id}")
async def delete_job_content(job_id: str):
    outcome = _purge_job_content(job_id, reason="user_requested")
    if not outcome.get("ok"):
        raise HTTPException(status_code=404, detail=outcome.get("message"))
    return outcome


@app.get("/api/report-package/{job_id}")
async def download_complete_report_package(job_id: str, delete_after: int = 1):
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = _prepare_student_result(job.get("result") or {})
    if result.get("result_deleted"):
        raise HTTPException(status_code=410, detail="Manuscript content and detailed results have already been deleted.")
    original_bytes = redis_conn.get(f"original:{job_id}") if redis_conn else None
    file_name = str(job.get("file_name") or result.get("file_name") or "manuscript.docx")
    annotated = build_annotated_document(original_bytes, result.get("correction_plan") or {}, file_name)
    tracked, _change_manifest = build_tracked_changes_document(original_bytes, result.get("correction_plan") or {}, file_name)
    package = build_report_package(job_id, result, extra_files={
        "CiteIntegrity_Annotated_Manuscript.docx": annotated,
        "CiteIntegrity_Track_Changes.docx": tracked,
    })
    should_delete = bool(delete_after) and DELETE_AFTER_PACKAGE_DOWNLOAD
    background = BackgroundTask(_purge_job_content, job_id, "download_completed") if should_delete else None
    return Response(
        content=package,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="CiteIntegrity_Report_{job_id[:8]}.zip"',
            "Cache-Control": "no-store, max-age=0",
            "X-Content-Deletion": "after-download" if should_delete else "scheduled-expiry",
        },
        background=background,
    )


@app.get("/api/academic-voice/{job_id}")
async def get_academic_voice_review(job_id: str):
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = _prepare_student_result(job.get("result") or {})
    if result.get("result_deleted"):
        raise HTTPException(status_code=410, detail="Manuscript content has been deleted.")
    settings = result.get("academic_voice_settings") or {}
    if settings.get("enabled") is not True:
        return {
            "feature": "Academic Voice and Writing Signals",
            "enabled": False,
            "settings": settings,
            "signals": [],
            "message": "This optional module is off for this analysis. Enable it to review explainable writing patterns.",
        }
    review = result.get("academic_voice_review") or {
        "feature": "Academic Voice and Writing Signals",
        "signals": [],
        "message": "No extractable manuscript text was available for this review.",
    }
    review["enabled"] = True
    review["settings"] = settings
    return review


@app.post("/api/academic-voice/{job_id}/settings")
async def update_academic_voice_settings(job_id: str, request: Request):
    """Enable or disable the optional writing-signal review for one analysis."""
    payload = await request.json()
    enabled = payload.get("enabled") is True
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = job.get("result") or {}
    if result.get("result_deleted"):
        raise HTTPException(status_code=410, detail="Manuscript content has been deleted.")
    settings = result.setdefault("academic_voice_settings", {})
    was_enabled = settings.get("enabled") is True
    settings.update({
        "enabled": enabled,
        "available": True,
        "default_enabled": False,
        "module_name": "Academic Voice and Writing Signals",
        "authorship_inference": False,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    if enabled:
        manuscript_text = str(
            result.get("main_text") or result.get("full_text") or
            result.get("document_text") or result.get("text") or ""
        )
        if not manuscript_text.strip():
            raise HTTPException(status_code=422, detail="No extractable manuscript text is available for writing-signal review.")
        result["academic_voice_review"] = analyse_academic_voice(manuscript_text)
    if enabled and not was_enabled:
        _bump_feature_usage(result, academic_voice_enable_events=1)
    elif not enabled and was_enabled:
        _bump_feature_usage(result, academic_voice_disable_events=1)
    result["correction_plan"] = build_correction_plan(result)
    result["evidence_resolution_workspace"] = result["correction_plan"].get("evidence_resolution_workspace") or {}
    _manual_save_result(job_id, result)
    return {
        "ok": True,
        "settings": settings,
        "academic_voice_review": result.get("academic_voice_review") if enabled else {},
        "correction_plan": result["correction_plan"],
    }


@app.post("/api/corrections/{job_id}/decision")
async def save_correction_decision(job_id: str, request: Request):
    payload = await request.json()
    item_id = str(payload.get("item_id") or "").strip()
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in {"accepted", "rejected", "ignored", "resolved", "pending"}:
        raise HTTPException(status_code=400, detail="Decision must be accepted, rejected, ignored, resolved, or pending.")
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = job.get("result") or {}
    plan = build_correction_plan(result)
    selected_item = next((item for item in plan.get("items") or [] if item.get("id") == item_id), None)
    if not selected_item:
        raise HTTPException(status_code=404, detail="Correction item not found")
    action = str(payload.get("action") or "").strip().lower()
    approved_source = payload.get("approved_source") if isinstance(payload.get("approved_source"), dict) else {}
    proposed_replacement = str(payload.get("proposed_replacement") or selected_item.get("proposed_replacement") or "").strip()
    secondary_replacement = str(payload.get("secondary_replacement") or "").strip()
    original_text = str(payload.get("original_text") or selected_item.get("original_text") or selected_item.get("evidence") or "").strip()
    operation = "replace"
    category = selected_item.get("category")
    if decision == "accepted":
        if category in {"citation_needed", "missing_reference"} and action in {"insert_citation", "add_reference"}:
            if not approved_source.get("url") or not approved_source.get("title"):
                raise HTTPException(status_code=400, detail="Select and open a scholarly source before approving this correction.")
            if approved_source.get("opened_by_user") is not True:
                raise HTTPException(status_code=400, detail="Open the candidate source before approving it.")
            if category == "citation_needed" and approved_source.get("context_fit_confirmed") is not True:
                raise HTTPException(status_code=400, detail="Confirm that the source supports the exact claim and fits the manuscript topic.")
            if category == "missing_reference" and approved_source.get("identity_confirmed") is not True:
                raise HTTPException(status_code=400, detail="Confirm that the candidate is the publication intended by the in-text citation.")
            if not proposed_replacement:
                raise HTTPException(status_code=400, detail="The approved citation or reference text is missing.")
            if action == "insert_citation":
                secondary_replacement = secondary_replacement or str(approved_source.get("formatted_reference") or "").strip()
                operation = "insert_after_and_append_reference" if secondary_replacement else "insert_after"
            else:
                operation = "append_reference"
        elif category == "claim_support" and action == "add_supporting_citation":
            if not approved_source.get("url") or not approved_source.get("title"):
                raise HTTPException(status_code=400, detail="Open and verify a scholarly source before adding it to the claim.")
            if approved_source.get("opened_by_user") is not True or approved_source.get("context_fit_confirmed") is not True:
                raise HTTPException(status_code=400, detail="Open the source and confirm that it supports the exact claim and fits the manuscript topic before approval.")
            if not original_text or not proposed_replacement:
                raise HTTPException(status_code=400, detail="The claim text or approved citation text is missing.")
            secondary_replacement = secondary_replacement or str(approved_source.get("formatted_reference") or "").strip()
            operation = "insert_after_and_append_reference" if secondary_replacement else "insert_after"
        elif category == "claim_support" and action == "revise_claim":
            if not original_text or not proposed_replacement or original_text == proposed_replacement:
                raise HTTPException(status_code=400, detail="Enter revised claim wording before approval.")
            operation = "replace"
        elif category in {"source_verification", "reference_incomplete", "reference_identity_conflict"} and action == "replace_reference":
            if not approved_source.get("url") or not approved_source.get("title"):
                raise HTTPException(status_code=400, detail="Open and verify a complete scholarly source before replacing this reference.")
            if approved_source.get("opened_by_user") is not True or approved_source.get("identity_confirmed") is not True:
                raise HTTPException(status_code=400, detail="Open the candidate and confirm that it is the same publication before replacing the reference.")
            if not original_text or not proposed_replacement:
                raise HTTPException(status_code=400, detail="The original or completed reference text is missing.")
            operation = "replace"
        elif category == "uncited_reference" and action == "delete_reference":
            operation = "delete"
        elif category == "uncited_reference" and action == "cite_reference":
            if not original_text or not proposed_replacement:
                raise HTTPException(status_code=400, detail="Select the claim and citation text before approving where to cite this reference.")
            operation = "insert_after"
        elif proposed_replacement:
            action = action or "replace_text"
            operation = "replace"
    decisions = result.setdefault("correction_decisions", {})
    decisions[item_id] = {
        "decision": decision,
        "note": str(payload.get("note") or "")[:1000],
        "action": action,
        "approved_source": approved_source,
        "proposed_replacement": proposed_replacement,
        "secondary_replacement": secondary_replacement,
        "original_text": original_text,
        "track_operation": operation,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    usage_increments = {"correction_decision_events": 1}
    if decision == "accepted":
        usage_increments["tracked_approval_events"] = 1
        if approved_source:
            usage_increments["source_approval_events"] = 1
    _bump_feature_usage(result, **usage_increments)
    result["correction_plan"] = build_correction_plan(result)
    _manual_save_result(job_id, result)
    return {"ok": True, "item_id": item_id, "decision": decision, "correction_plan": result["correction_plan"]}


@app.post("/api/corrections/{job_id}/approve-reference-formatting")
async def approve_all_reference_formatting(job_id: str):
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = job.get("result") or {}
    plan = build_correction_plan(result)
    decisions = result.setdefault("correction_decisions", {})
    approved = 0
    timestamp = datetime.utcnow().isoformat() + "Z"
    for item in plan.get("items") or []:
        if item.get("category") != "reference_style" or item.get("confidence") != "high" or not item.get("proposed_replacement") or item.get("decision") != "pending":
            continue
        decisions[item["id"]] = {
            "decision": "accepted", "note": "User approved all detected reference-style corrections.",
            "action": "format_reference", "approved_source": {},
            "proposed_replacement": item["proposed_replacement"], "secondary_replacement": "",
            "original_text": item.get("original_text") or item.get("evidence") or "",
            "track_operation": "replace", "updated_at": timestamp,
        }
        approved += 1
    result["correction_plan"] = build_correction_plan(result)
    if approved:
        _bump_feature_usage(
            result,
            correction_decision_events=approved,
            tracked_approval_events=approved,
            reference_style_approval_events=approved,
        )
    _manual_save_result(job_id, result)
    return {"ok": True, "approved_count": approved, "correction_plan": result["correction_plan"]}


def _candidate_citation_text(candidate: Dict[str, Any]) -> str:
    authors = candidate.get("authors") or []
    first = str(authors[0] if isinstance(authors, list) and authors else authors or "Source").strip()
    surname = first.split(",", 1)[0].split()[-1] if first else "Source"
    year = str(candidate.get("year") or "n.d.")
    return f"({surname} et al., {year})" if isinstance(authors, list) and len(authors) > 2 else f"({surname}, {year})"


def _candidate_reference_text(candidate: Dict[str, Any]) -> str:
    authors = candidate.get("authors") or []
    author_text = ", ".join(str(a) for a in authors) if isinstance(authors, list) else str(authors or "")
    year = candidate.get("year") or "n.d."
    title = candidate.get("title") or ""
    doi = candidate.get("doi") or ""
    url = candidate.get("url") or (f"https://doi.org/{doi}" if doi else "")
    return f"{author_text} ({year}). {title}. {url}".strip()


def _reference_style_for_result(result: Dict[str, Any]) -> str:
    """Keep the manuscript's exact author-year style for inserted references."""
    summary = result.get("summary") or {}
    values = [
        result.get("selected_style"), result.get("style"), result.get("citation_style"),
        summary.get("selected_style"), summary.get("style"), summary.get("citation_style"),
    ]
    raw = " ".join(str(value or "").lower() for value in values)
    if "apa6" in raw or "apa 6" in raw:
        return "apa6"
    if "harvard" in raw:
        return "harvard"
    if any(token in raw for token in ("numeric", "ieee", "vancouver", "ama", "nature")):
        return _style_for_job_result(result)
    return "apa7"


def _numeric_reference_number(result: Dict[str, Any], evidence: str = "") -> int:
    match = re.match(r"\s*[\[(]?\s*(\d{1,4})", str(evidence or ""))
    if match:
        return int(match.group(1))
    references = result.get("references_raw") or result.get("references") or result.get("reference_list") or []
    return len(references) + 1 if isinstance(references, list) else 1


def _numeric_marker(number: int, style: str) -> str:
    if style == "numeric_round":
        return f"({number})"
    if style == "numeric_superscript":
        return str(number).translate(str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹"))
    return f"[{number}]"


def _style_aware_candidate_citation(candidate: Dict[str, Any], result: Dict[str, Any], evidence: str = "") -> str:
    style = _reference_style_for_result(result)
    if style.startswith("numeric_"):
        return _numeric_marker(_numeric_reference_number(result, evidence), style)
    return _candidate_citation_text(candidate)


def _style_aware_candidate_text(candidate: Dict[str, Any], result: Dict[str, Any], evidence: str = "") -> str:
    style = _reference_style_for_result(result)
    if style.startswith("numeric_"):
        marker = _numeric_marker(_numeric_reference_number(result, evidence), style)
        return f"{marker} {_candidate_reference_text(candidate)}".strip()
    ref = {
        "authors": candidate.get("authors") or [],
        "year": candidate.get("year") or "",
        "title": candidate.get("title") or "",
        "source": candidate.get("journal") or "",
        "volume": candidate.get("volume") or "",
        "issue": candidate.get("issue") or "",
        "pages": candidate.get("pages") or "",
        "doi": candidate.get("doi") or "",
        "publisher": candidate.get("publisher") or "",
        "type": candidate.get("publication_type") or "article",
    }
    formatted = format_reference(ref, style=style)
    # Word Track Changes receives plain text. Markdown italics markers must not
    # appear in the manuscript.
    return re.sub(r"\s+", " ", re.sub(r"\*", "", formatted)).strip()


def _reference_citation_context(manuscript_text: str, reference: str, window: int = 600) -> str:
    """Recover claim context from an author-year reference without using AI."""
    text = str(manuscript_text or "")
    ref = str(reference or "")
    if not text or not ref:
        return ""
    year_match = re.search(r"\b(?:19|20)\d{2}[a-z]?\b", ref, re.I)
    author_part = re.split(r"\s*[,(]", ref, maxsplit=1)[0].strip()
    surname = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]{2,}", author_part)
    surname = surname[-1] if surname else ""
    year = year_match.group(0) if year_match else ""
    if not surname:
        return ""
    patterns = []
    if year:
        patterns.extend([
            rf"\b{re.escape(surname)}\b[^\n.]{{0,100}}\b{re.escape(year)}\b",
            rf"\b{re.escape(surname)}\b\s*\(\s*{re.escape(year)}\s*\)",
        ])
    patterns.append(rf"\b{re.escape(surname)}\b")
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            start = max(0, match.start() - window)
            end = min(len(text), match.end() + window)
            candidate = text[start:end]
            # Skip the full reference-list entry when possible.
            if ref[:100].lower() in candidate.lower() and candidate.lower().count(surname.lower()) == 1:
                continue
            return re.sub(r"\s+", " ", candidate).strip()
    return ""


def _claim_context_window(manuscript_text: str, claim: str, window: int = 900) -> str:
    """Return the claim plus nearby prose for topic-aware source discovery."""
    text = str(manuscript_text or "")
    needle = re.sub(r"\s+", " ", str(claim or "")).strip()
    if not text or not needle:
        return needle
    position = text.casefold().find(needle.casefold())
    if position < 0:
        probe = needle[:120]
        position = text.casefold().find(probe.casefold()) if probe else -1
    if position < 0:
        return needle
    start = max(0, position - window)
    end = min(len(text), position + len(needle) + window)
    nearby = re.sub(r"\s+", " ", text[start:end]).strip()
    return re.sub(r"\s+", " ", f"{needle} {nearby}").strip()[:2400]


@app.post("/api/corrections/{job_id}/sources/{item_id}")
async def find_correction_sources(job_id: str, item_id: str):
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = job.get("result") or {}
    plan = build_correction_plan(result)
    item = next((row for row in plan.get("items") or [] if row.get("id") == item_id), None)
    allowed_categories = {"citation_needed", "missing_reference", "source_verification", "claim_support", "reference_incomplete", "reference_identity_conflict"}
    if not item or item.get("category") not in allowed_categories:
        raise HTTPException(status_code=400, detail="Source discovery is available for citation-needed, missing-reference, claim-support, incomplete-reference and unverified-reference fixes.")
    evidence = str(item.get("evidence") or "")
    manuscript_text = str(result.get("main_text") or result.get("full_text") or result.get("document_text") or result.get("text") or "")
    category = item.get("category")
    context = extract_context(manuscript_text, evidence, window=600) if manuscript_text and evidence else ""
    if category in {"claim_support", "citation_needed"}:
        context = _claim_context_window(manuscript_text, evidence, window=900) or context
    if category in {"source_verification", "reference_incomplete", "reference_identity_conflict"}:
        context = _reference_citation_context(manuscript_text, evidence, window=600) or context
    context = context or str((item.get("supporting_metadata") or {}).get("claim") or "") or evidence
    topic_profile = build_document_topic_profile(manuscript_text, result)
    claim_fingerprint: Dict[str, Any] = {}
    discovery_context = context
    if category in {"claim_support", "citation_needed"}:
        location = item.get("location") if isinstance(item.get("location"), dict) else {}
        claim_fingerprint = build_claim_fingerprint(
            evidence,
            context,
            topic_profile,
            str(location.get("section") or ""),
        )
        discovery_context = str(claim_fingerprint.get("search_query") or context)
    try:
        if category in {"source_verification", "reference_incomplete", "reference_identity_conflict"}:
            candidates = await run_in_threadpool(suggest_for_unverified, evidence, 5, "apa", True)
            # If strict metadata recovery finds nothing, use manuscript context
            # as a review-only discovery fallback. It must not silently replace
            # the original reference.
            if not candidates:
                candidates = await run_in_threadpool(
                    suggest_from_context, discovery_context, evidence, 5,
                    use_citation_hint=True, min_relevance=70,
                )
        else:
            discovery_limit = 12 if category in {"claim_support", "citation_needed"} else 5
            discovery_min_relevance = 45 if category in {"claim_support", "citation_needed"} else 70
            candidates = await run_in_threadpool(
                suggest_from_context,
                discovery_context,
                evidence if category == "missing_reference" else "",
                discovery_limit,
                use_citation_hint=category == "missing_reference",
                min_relevance=discovery_min_relevance,
                strict_citation_identity=category == "missing_reference",
            )
    except Exception as exc:
        print(f"[SOURCE DISCOVERY] {job_id} {item_id} failed: {type(exc).__name__}: {exc}")
        try:
            _bump_feature_usage(
                result,
                source_search_requests=1,
                context_aware_search_requests=1 if category in {"claim_support", "citation_needed"} else 0,
                reference_identity_search_requests=1 if category not in {"claim_support", "citation_needed"} else 0,
                source_search_failures=1,
            )
            _manual_save_result(job_id, result)
        except Exception as telemetry_error:
            print(f"[SOURCE DISCOVERY] Could not persist count-only failure telemetry: {telemetry_error}")
        raise HTTPException(status_code=502, detail=f"Scholarly source lookup failed safely: {type(exc).__name__}. Try again or use the manual evidence links.")
    candidates = [candidate for candidate in (candidates or []) if isinstance(candidate, dict)]
    context_checked_candidates = []
    withheld_count = 0
    for candidate in candidates:
        if category in {"claim_support", "citation_needed"}:
            candidate["context_fit"] = assess_candidate_context_fit(
                evidence,
                context,
                candidate,
                topic_profile,
                claim_fingerprint,
            )
            if (candidate.get("context_fit") or {}).get("passes_context_gate") is not True:
                withheld_count += 1
                continue
            candidate["approval_confirmation_type"] = "claim_support_and_topic_fit"
        elif category in {"source_verification", "reference_incomplete", "reference_identity_conflict", "missing_reference"}:
            match_basis = candidate.get("match_basis") if isinstance(candidate.get("match_basis"), dict) else {}
            candidate["identity_fit"] = {
                "status": "exact_or_strong_identity_candidate" if (
                    candidate.get("doi_match") is True or
                    int(match_basis.get("reference_title_similarity") or 0) >= 82 or
                    int(match_basis.get("reference_fit_score") or 0) >= 82
                ) else "identity_requires_manual_confirmation",
                "title_similarity": match_basis.get("reference_title_similarity"),
                "doi_match": bool(candidate.get("doi_match") or match_basis.get("reference_doi_match")),
                "warning": "Confirm that this is the same publication, not merely a source on a similar topic.",
            }
            candidate["approval_confirmation_type"] = "same_publication_identity"
        candidate["citation_text"] = _style_aware_candidate_citation(candidate, result, evidence)
        candidate["formatted_reference"] = _style_aware_candidate_text(candidate, result, evidence)
        candidate["reference_style"] = _reference_style_for_result(result)
        candidate["context_used"] = context[:700]
        candidate["approval_warning"] = "Open and verify the source. Context ranking is a discovery aid, not proof that the source supports the claim."
        context_checked_candidates.append(candidate)
    candidates = context_checked_candidates[:5]
    result.setdefault("correction_source_candidates", {})[item_id] = candidates
    _bump_feature_usage(
        result,
        source_search_requests=1,
        context_aware_search_requests=1 if category in {"claim_support", "citation_needed"} else 0,
        reference_identity_search_requests=1 if category not in {"claim_support", "citation_needed"} else 0,
        candidate_sources_returned=len(candidates),
        candidates_withheld_by_context=withheld_count,
        searches_with_candidates=1 if candidates else 0,
    )
    result["correction_plan"] = build_correction_plan(result)
    _manual_save_result(job_id, result)
    return {
        "ok": True,
        "item_id": item_id,
        "candidates": candidates,
        "withheld_candidate_count": withheld_count,
        "claim_fingerprint": claim_fingerprint if category in {"claim_support", "citation_needed"} else {},
        "message": "Open and verify a source before approving it. Candidates based only on general keywords are withheld.",
    }


@app.get("/api/ai/status")
async def ai_configuration_status():
    configured = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    return {
        "configured": configured,
        "model": os.environ.get("AI_REWRITE_MODEL", "gpt-5.6-luna") if configured else None,
        "message": "Optional academic rewriting is configured." if configured else "Add OPENAI_API_KEY to the server environment to enable optional academic rewriting.",
    }


@app.post("/api/academic-voice/rewrite")
async def rewrite_academic_voice(request: Request):
    payload = await request.json()
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="The analysis job ID is required.")
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job_result = job.get("result") or {}
    if (job_result.get("academic_voice_settings") or {}).get("enabled") is not True:
        raise HTTPException(status_code=409, detail="Academic Voice and Writing Signals is off for this analysis. Enable it before requesting a revision.")
    passage = str(payload.get("passage") or "")
    if not passage.strip():
        raise HTTPException(status_code=400, detail="Select a passage to revise.")
    try:
        revision = await run_in_threadpool(
            rewrite_selected_passage,
            passage,
            str(payload.get("context") or ""),
            str(payload.get("discipline") or ""),
            str(payload.get("spelling") or "British English"),
            None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    _bump_feature_usage(job_result, academic_voice_rewrite_requests=1)
    _manual_save_result(job_id, job_result)
    return {
        "revision": revision,
        "notice": "Review every change. CiteIntegrity does not guarantee AI-detector outcomes and never replaces responsible authorship.",
    }


@app.post("/api/academic-voice/{job_id}/approve")
async def approve_academic_voice_revision(job_id: str, request: Request):
    payload = await request.json()
    original = str(payload.get("original_text") or "").strip()
    revised = str(payload.get("proposed_replacement") or "").strip()
    if not original or not revised:
        raise HTTPException(status_code=400, detail="Both the original passage and revised passage are required.")
    if original == revised:
        raise HTTPException(status_code=400, detail="The revision is identical to the original passage.")
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = job.get("result") or {}
    if (result.get("academic_voice_settings") or {}).get("enabled") is not True:
        raise HTTPException(status_code=409, detail="Academic Voice and Writing Signals is off for this analysis.")
    import hashlib
    revision_id = "voice-revision-" + hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    revisions = result.setdefault("academic_voice_revisions", {})
    revisions[revision_id] = {
        "original_text": original,
        "proposed_replacement": revised,
        "reason": str(payload.get("reason") or "Approved AI-assisted academic voice revision")[:500],
        "model": str(payload.get("model") or "")[:120],
        "confidence": str(payload.get("confidence") or "reviewed")[:40],
        "approved_at": datetime.utcnow().isoformat() + "Z",
    }
    _bump_feature_usage(
        result,
        academic_voice_approval_events=1,
        correction_decision_events=1,
        tracked_approval_events=1,
    )
    result["correction_plan"] = build_correction_plan(result)
    _manual_save_result(job_id, result)
    return {"ok": True, "revision_id": revision_id, "correction_plan": result["correction_plan"], "academic_voice_revisions": revisions}


@app.post("/api/revision-compare")
async def compare_revision(request: Request):
    payload = await request.json()
    original_id = str(payload.get("original_job_id") or "")
    revised_id = str(payload.get("revised_job_id") or "")
    original = load_job_record_fresh(original_id) if original_id else None
    revised = load_job_record_fresh(revised_id) if revised_id else None
    if not original or not revised:
        raise HTTPException(status_code=404, detail="Both completed analysis job IDs are required.")
    return compare_revision_results(original.get("result") or {}, revised.get("result") or {})

@app.get("/job/{job_id}")
def get_job_endpoint(job_id: str, include_result: int = 0):
    """Lightweight job status endpoint.

    By default this no longer returns the full result JSON, preventing large
    completed jobs from blocking other homepage/result requests.
    Use ?include_result=1 only when the full result is required.
    """
    job = load_job_record_fresh(job_id)
    if not job:
        return {"status": "not_found"}

    payload = {
        "status": job.get("status", "unknown"),
        "verification": job.get("verification", {})
    }
    if include_result:
        payload["result"] = job.get("result")
    return payload
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
        result = _demo_prepare_full_review_result(job_id, result, persist=False)
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
        if final_tables_ready:
            # Rebuild after Claim Support is finalised so mapping-incomplete and
            # weak rows receive their direct correction actions immediately.
            result["correction_plan"] = build_correction_plan(result)

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
    Start advanced enrichment. In the training/demo build, this endpoint is
    always open. If Redis or the deep worker is unavailable, it completes a
    local enrichment pass so the demonstration does not show a closed feature.
    """
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

    if current_state in {"queued", "running"} and redis_conn:
        return {
            "ok": True,
            "demo_unlocked": DEMO_UNLOCK_ALL_FEATURES,
            "message": "Advanced enrichment is already running.",
            "state": current_state,
            "rq_job_id": enrichment.get("rq_job_id"),
            "scope": enrichment.get("scope", scope),
        }

    # Demo fallback keeps the feature open when Redis/DB/worker is unavailable.
    if DEMO_UNLOCK_ALL_FEATURES and (not redis_conn or not DATABASE_URL):
        return _demo_complete_enrichment(job_id, result, scope, reason="local_demo_no_queue")

    try:
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

        result = _demo_prepare_full_review_result(job_id, result, persist=False)
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
            "citation_needed_ready": bool(result.get("citation_needed_claims")),
            "demo_unlocked": DEMO_UNLOCK_ALL_FEATURES,
        }

        _demo_save_result(job_id, result)

        return {
            "ok": True,
            "demo_unlocked": DEMO_UNLOCK_ALL_FEATURES,
            "message": "Advanced enrichment queued.",
            "rq_job_id": rq_job.id,
            "scope": scope,
        }

    except Exception as e:
        print(f"[ENRICHMENT START] Queue failed; using local demo fallback: {e}")
        if DEMO_UNLOCK_ALL_FEATURES:
            return _demo_complete_enrichment(job_id, result, scope, reason="local_demo_queue_fallback")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
# CERTIFICATE AND CITATION-NEEDED ENDPOINTS - TRAINING/DEMO OPEN ACCESS
# ============================================================

@app.get("/api/demo/access/{job_id}")
async def demo_access_status(job_id: str):
    return {
        "ok": True,
        "job_id": job_id,
        "access": _demo_full_access_payload(job_id),
        "message": "Training/demo mode is enabled; all Full Review features are open.",
    }


@app.get("/api/citation-needed/{job_id}")
async def get_citation_needed_claims(job_id: str):
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    result = _demo_prepare_full_review_result(job_id, job.get("result") or {}, persist=True)
    claims = result.get("citation_needed_claims") or []
    return {
        "ok": True,
        "job_id": job_id,
        "demo_unlocked": DEMO_UNLOCK_ALL_FEATURES,
        "claims": claims,
        "count": len(claims),
    }


@app.post("/api/citation-needed/start/{job_id}")
async def start_citation_needed_claims(job_id: str):
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    result = job.get("result") or {}
    result["citation_needed_claims"] = _demo_generate_citation_needed_claims(result)
    result = _demo_prepare_full_review_result(job_id, result, persist=True)
    return {
        "ok": True,
        "job_id": job_id,
        "demo_unlocked": DEMO_UNLOCK_ALL_FEATURES,
        "message": "Citation-needed claims generated in training/demo mode.",
        "claims": result.get("citation_needed_claims") or [],
        "count": len(result.get("citation_needed_claims") or []),
    }


@app.get("/api/certificate/{job_id}")
async def get_citation_integrity_certificate(job_id: str):
    """Generate or regenerate a demo certificate only when the user asks for it."""
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    result = _demo_prepare_full_review_result(job_id, job.get("result") or {}, persist=False)
    result["manual_verification_summary"] = _manual_build_summary(result)
    certificate = _demo_build_certificate(result, job_id=job_id)
    result["citation_integrity_certificate"] = certificate
    result["certificate_state"] = {
        "requires_regeneration": False,
        "manual_verification_updated_after_certificate": False,
        "last_manual_verification_at": (result.get("manual_verification") or {}).get("last_updated", ""),
        "last_certificate_generated_at": certificate.get("generated_at") or certificate.get("timestamp") or "",
        "reason": "",
    }
    _demo_save_result(job_id, result)

    return {
        "ok": True,
        "demo_unlocked": DEMO_UNLOCK_ALL_FEATURES,
        "certificate": certificate,
        "certificate_state": result.get("certificate_state"),
    }


@app.get("/api/certificate/{job_id}/download")
async def download_citation_integrity_certificate(job_id: str, format: str = "pdf"):
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    result = _demo_prepare_full_review_result(job_id, job.get("result") or {}, persist=False)
    result["manual_verification_summary"] = _manual_build_summary(result)
    # Download the generated certificate if it is current; otherwise rebuild so the
    # PDF always includes the latest manual verification evidence.
    state = result.get("certificate_state") or {}
    if result.get("citation_integrity_certificate") and not state.get("requires_regeneration"):
        certificate = result.get("citation_integrity_certificate")
    else:
        certificate = _demo_build_certificate(result, job_id=job_id)
        result["citation_integrity_certificate"] = certificate
        result["certificate_state"] = {
            "requires_regeneration": False,
            "manual_verification_updated_after_certificate": False,
            "last_manual_verification_at": (result.get("manual_verification") or {}).get("last_updated", ""),
            "last_certificate_generated_at": certificate.get("generated_at") or certificate.get("timestamp") or "",
            "reason": "",
        }
        _demo_save_result(job_id, result)

    safe_job = re.sub(r"[^A-Za-z0-9_-]+", "", job_id[:12] or "certificate")
    generated_stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")

    if str(format or "pdf").lower() == "html":
        if render_certificate_html is not None:
            html_doc = render_certificate_html(certificate)
        else:
            html_doc = "<html><body><h1>CiteIntegrity Certificate</h1><pre>" + html.escape(json.dumps(certificate, indent=2)) + "</pre></body></html>"
        return Response(
            content=html_doc,
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="Demo_CiteIntegrity_Certificate_{safe_job}_{generated_stamp}.html"', "Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )

    if render_certificate_pdf_bytes is None:
        # Graceful fallback if reportlab/certificate PDF helper is not yet deployed.
        if render_certificate_html is not None:
            html_doc = render_certificate_html(certificate)
        else:
            html_doc = "<html><body><h1>CiteIntegrity Certificate</h1><pre>" + html.escape(json.dumps(certificate, indent=2)) + "</pre></body></html>"
        return Response(
            content=html_doc,
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="Demo_CiteIntegrity_Certificate_{safe_job}_{generated_stamp}.html"', "Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )

    try:
        pdf_bytes = render_certificate_pdf_bytes(certificate)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Certificate PDF generation failed: {e}")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Demo_CiteIntegrity_Certificate_{safe_job}_{generated_stamp}.pdf"', "Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )

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
        # ACII is stored as {"ACII": 77.69, ...} in recent results.
        # Keep older aliases too for backward compatibility.
        for key in ["ACII", "score", "acii_score", "overall_score", "total_score", "value"]:
            if key in acii:
                return round(_safe_float(acii.get(key)), 2)
    return None


FEATURE_METRIC_KEYS = (
    "evidence_workspace_analyses",
    "evidence_issues_generated",
    "evidence_issues_pending",
    "evidence_issues_resolved",
    "tracked_approvals_current",
    "source_search_requests",
    "context_aware_search_requests",
    "reference_identity_search_requests",
    "source_search_failures",
    "searches_with_candidates",
    "candidate_sources_returned",
    "candidates_withheld_by_context",
    "correction_decision_events",
    "source_approval_events",
    "reference_style_approval_events",
    "academic_voice_enabled_analyses",
    "academic_voice_completed_analyses",
    "academic_voice_signals",
    "academic_voice_rewrite_requests",
    "academic_voice_approved_revisions",
)


def _empty_feature_metrics() -> Dict[str, int]:
    return {key: 0 for key in FEATURE_METRIC_KEYS}


def _extract_feature_metrics(result: Dict[str, Any]) -> Dict[str, int]:
    """Extract count-only metrics for the post-1.8.8 product features."""
    result = result or {}
    metrics = _empty_feature_metrics()
    usage = result.get("feature_usage") if isinstance(result.get("feature_usage"), dict) else {}
    plan = result.get("correction_plan") if isinstance(result.get("correction_plan"), dict) else {}
    workspace = result.get("evidence_resolution_workspace")
    if not isinstance(workspace, dict):
        workspace = plan.get("evidence_resolution_workspace") if isinstance(plan.get("evidence_resolution_workspace"), dict) else {}
    counts = workspace.get("counts") if isinstance(workspace.get("counts"), dict) else {}
    if workspace:
        metrics["evidence_workspace_analyses"] = 1
    else:
        metrics["evidence_workspace_analyses"] = _safe_int(usage.get("evidence_workspace_analyses"))
    metrics["evidence_issues_generated"] = _safe_int(
        counts.get("total") if workspace else usage.get("evidence_issues_generated")
    )
    metrics["evidence_issues_pending"] = _safe_int(
        counts.get("pending") if workspace else usage.get("evidence_issues_pending")
    )
    metrics["evidence_issues_resolved"] = _safe_int(
        counts.get("resolved") if workspace else usage.get("evidence_issues_resolved")
    )

    decisions = result.get("correction_decisions") if isinstance(result.get("correction_decisions"), dict) else {}
    metrics["tracked_approvals_current"] = (
        sum(
            1 for decision in decisions.values()
            if isinstance(decision, dict)
            and str(decision.get("decision") or "").lower() == "accepted"
            and str(decision.get("track_operation") or "").lower() in {
                "replace", "delete", "insert_after", "append_reference",
                "insert_after_and_append_reference",
            }
        )
        if decisions else _safe_int(usage.get("tracked_approvals_current"))
    )

    for key in (
        "source_search_requests", "context_aware_search_requests",
        "reference_identity_search_requests", "source_search_failures",
        "searches_with_candidates", "candidate_sources_returned",
        "candidates_withheld_by_context", "correction_decision_events",
        "source_approval_events", "reference_style_approval_events",
        "academic_voice_rewrite_requests",
    ):
        metrics[key] = _safe_int(usage.get(key))

    settings = result.get("academic_voice_settings") if isinstance(result.get("academic_voice_settings"), dict) else {}
    review = result.get("academic_voice_review") if isinstance(result.get("academic_voice_review"), dict) else {}
    signals = review.get("signals") if isinstance(review.get("signals"), list) else []
    revisions = result.get("academic_voice_revisions") if isinstance(result.get("academic_voice_revisions"), dict) else {}
    metrics["academic_voice_enabled_analyses"] = (
        1 if settings.get("enabled") is True
        else _safe_int(usage.get("academic_voice_enabled_analyses"))
    )
    metrics["academic_voice_completed_analyses"] = (
        1 if review else _safe_int(usage.get("academic_voice_completed_analyses"))
    )
    metrics["academic_voice_signals"] = (
        len(signals) if review else _safe_int(usage.get("academic_voice_signals"))
    )
    metrics["academic_voice_approved_revisions"] = (
        len(revisions) if revisions else _safe_int(usage.get("academic_voice_approved_revisions"))
    )
    return metrics


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
        **_extract_feature_metrics(result),
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
        "dashboard_metrics": {
            **_empty_feature_metrics(),
            "completed_jobs": 0,
            "failed_jobs": 0,
            "running_jobs": 0,
            "queued_jobs": 0,
            "total_jobs": 0,
            "verified": 0,
            "likely": 0,
            "needs_review": 0,
            "not_found": 0,
            "offline": 0,
            "claim_rows": 0,
            "recovery_rows": 0,
            "suggestions_count": 0,
        },
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
    Dashboard builder populated from persistent PostgreSQL data.

    Dashboard-only update:
    - Keeps payment routes and payment logic untouched.
    - Uses jobs.result JSONB to populate citation, verification, ACII and claim metrics.
    - Uses SQL-level JSONB expressions, not full manuscript JSON transfer, to avoid
      exposing or loading full document text into the dashboard response.
    - Falls back gracefully to relational upload totals when optional JSONB metrics
      are unavailable.
    """
    days = max(1, min(int(days or 30), 365))
    cutoff_dt = datetime.now() - timedelta(days=days - 1)
    cutoff_date = cutoff_dt.date()

    stats_row: Dict[str, Any] = {}
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
    advanced.update(_empty_feature_metrics())

    completed_statuses = "('completed','complete','done','success','finished')"
    failed_statuses = "('failed','error','cancelled','canceled')"
    active_statuses = "('queued','started','running','processing','deferred','scheduled')"

    # SQL snippets are repeated in aggregate, daily and recent queries. They use
    # summary counts first, then fall back to JSON array lengths.
    references_expr = """
        CASE
            WHEN COALESCE(result #>> '{summary,reference_entries_found}', '') ~ '^[0-9]+$'
                THEN (result #>> '{summary,reference_entries_found}')::int
            WHEN jsonb_typeof(result->'references_raw') = 'array'
                THEN jsonb_array_length(result->'references_raw')
            WHEN jsonb_typeof(result->'references') = 'array'
                THEN jsonb_array_length(result->'references')
            ELSE 0
        END
    """

    citations_expr = """
        CASE
            WHEN COALESCE(result #>> '{summary,in_text_citations_found}', '') ~ '^[0-9]+$'
                THEN (result #>> '{summary,in_text_citations_found}')::int
            WHEN jsonb_typeof(result->'in_text_citations') = 'array'
                THEN jsonb_array_length(result->'in_text_citations')
            WHEN jsonb_typeof(result->'citations') = 'array'
                THEN jsonb_array_length(result->'citations')
            WHEN jsonb_typeof(result->'reconciliation_intext_to_reference') = 'array'
                THEN jsonb_array_length(result->'reconciliation_intext_to_reference')
            ELSE 0
        END
    """

    missing_expr = """
        CASE
            WHEN COALESCE(result #>> '{summary,missing_in_references}', '') ~ '^[0-9]+$'
                THEN (result #>> '{summary,missing_in_references}')::int
            WHEN jsonb_typeof(result->'missing_in_references') = 'array'
                THEN jsonb_array_length(result->'missing_in_references')
            WHEN jsonb_typeof(result->'missing_citations') = 'array'
                THEN jsonb_array_length(result->'missing_citations')
            ELSE 0
        END
    """

    uncited_expr = """
        CASE
            WHEN jsonb_typeof(result->'uncited_references') = 'array'
                THEN jsonb_array_length(result->'uncited_references')
            WHEN jsonb_typeof(result->'uncited_reference_rows') = 'array'
                THEN jsonb_array_length(result->'uncited_reference_rows')
            ELSE 0
        END
    """

    verification_rows_expr = """
        CASE
            WHEN COALESCE(result #>> '{online_verification,summary,total}', '') ~ '^[0-9]+$'
                THEN (result #>> '{online_verification,summary,total}')::int
            WHEN COALESCE(result #>> '{verification,total}', '') ~ '^[0-9]+$'
                THEN (result #>> '{verification,total}')::int
            WHEN jsonb_typeof(result->'online_verification'->'rows') = 'array'
                THEN jsonb_array_length(result->'online_verification'->'rows')
            ELSE 0
        END
    """

    verified_expr = """
        CASE WHEN COALESCE(result #>> '{online_verification,summary,verified}', '') ~ '^[0-9]+$'
            THEN (result #>> '{online_verification,summary,verified}')::int ELSE 0 END
    """
    likely_expr = """
        CASE WHEN COALESCE(result #>> '{online_verification,summary,likely}', '') ~ '^[0-9]+$'
            THEN (result #>> '{online_verification,summary,likely}')::int ELSE 0 END
    """
    needs_review_expr = """
        CASE WHEN COALESCE(result #>> '{online_verification,summary,needs_review}', '') ~ '^[0-9]+$'
            THEN (result #>> '{online_verification,summary,needs_review}')::int ELSE 0 END
    """
    not_found_expr = """
        CASE WHEN COALESCE(result #>> '{online_verification,summary,not_found}', '') ~ '^[0-9]+$'
            THEN (result #>> '{online_verification,summary,not_found}')::int ELSE 0 END
    """
    offline_expr = """
        CASE WHEN COALESCE(result #>> '{online_verification,summary,offline}', '') ~ '^[0-9]+$'
            THEN (result #>> '{online_verification,summary,offline}')::int ELSE 0 END
    """

    claim_expr = """
        CASE WHEN jsonb_typeof(result->'claim_support') = 'array'
            THEN jsonb_array_length(result->'claim_support') ELSE 0 END
    """

    recovery_expr = """
        (
            CASE WHEN jsonb_typeof(result->'recovery'->'missing_recovery') = 'array'
                THEN jsonb_array_length(result->'recovery'->'missing_recovery') ELSE 0 END
            +
            CASE WHEN jsonb_typeof(result->'recovery'->'verification_recovery') = 'array'
                THEN jsonb_array_length(result->'recovery'->'verification_recovery') ELSE 0 END
        )
    """

    suggestions_expr = """
        CASE
            WHEN jsonb_typeof(result->'suggestions') = 'array'
                THEN jsonb_array_length(result->'suggestions')
            WHEN jsonb_typeof(result->'suggestions'->'citations') = 'array'
                THEN jsonb_array_length(result->'suggestions'->'citations')
            ELSE 0
        END
    """

    acii_expr = """
        CASE
            WHEN jsonb_typeof(result->'acii') = 'number' THEN (result->>'acii')::numeric
            WHEN COALESCE(result #>> '{acii,ACII}', '') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (result #>> '{acii,ACII}')::numeric
            WHEN COALESCE(result #>> '{acii,score}', '') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (result #>> '{acii,score}')::numeric
            WHEN COALESCE(result #>> '{acii,acii_score}', '') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (result #>> '{acii,acii_score}')::numeric
            WHEN COALESCE(result #>> '{acii,overall_score}', '') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (result #>> '{acii,overall_score}')::numeric
            ELSE NULL
        END
    """

    def feature_usage_expr(key: str) -> str:
        path = "{feature_usage," + key + "}"
        return f"""
            CASE WHEN COALESCE(result #>> '{path}', '') ~ '^[0-9]+$'
                THEN (result #>> '{path}')::int ELSE 0 END
        """

    evidence_total_expr = """
        CASE
            WHEN COALESCE(result #>> '{evidence_resolution_workspace,counts,total}', result #>> '{correction_plan,evidence_resolution_workspace,counts,total}', result #>> '{feature_usage,evidence_issues_generated}', '') ~ '^[0-9]+$'
                THEN COALESCE(result #>> '{evidence_resolution_workspace,counts,total}', result #>> '{correction_plan,evidence_resolution_workspace,counts,total}', result #>> '{feature_usage,evidence_issues_generated}')::int
            ELSE 0
        END
    """
    evidence_pending_expr = """
        CASE
            WHEN COALESCE(result #>> '{evidence_resolution_workspace,counts,pending}', result #>> '{correction_plan,evidence_resolution_workspace,counts,pending}', result #>> '{feature_usage,evidence_issues_pending}', '') ~ '^[0-9]+$'
                THEN COALESCE(result #>> '{evidence_resolution_workspace,counts,pending}', result #>> '{correction_plan,evidence_resolution_workspace,counts,pending}', result #>> '{feature_usage,evidence_issues_pending}')::int
            ELSE 0
        END
    """
    evidence_resolved_expr = """
        CASE
            WHEN COALESCE(result #>> '{evidence_resolution_workspace,counts,resolved}', result #>> '{correction_plan,evidence_resolution_workspace,counts,resolved}', result #>> '{feature_usage,evidence_issues_resolved}', '') ~ '^[0-9]+$'
                THEN COALESCE(result #>> '{evidence_resolution_workspace,counts,resolved}', result #>> '{correction_plan,evidence_resolution_workspace,counts,resolved}', result #>> '{feature_usage,evidence_issues_resolved}')::int
            ELSE 0
        END
    """
    evidence_analysis_expr = """
        CASE WHEN jsonb_typeof(result->'evidence_resolution_workspace') = 'object'
               OR jsonb_typeof(result->'correction_plan'->'evidence_resolution_workspace') = 'object'
            THEN 1
            WHEN COALESCE(result #>> '{feature_usage,evidence_workspace_analyses}', '') ~ '^[0-9]+$'
                THEN (result #>> '{feature_usage,evidence_workspace_analyses}')::int
            ELSE 0 END
    """
    tracked_approvals_expr = """
        CASE WHEN jsonb_typeof(result->'correction_decisions') = 'object' THEN
            (
                SELECT COUNT(*)
                FROM jsonb_each(result->'correction_decisions') AS decision_entry(key, value)
                WHERE lower(COALESCE(value->>'decision', '')) = 'accepted'
                  AND lower(COALESCE(value->>'track_operation', '')) IN (
                      'replace', 'delete', 'insert_after', 'append_reference',
                      'insert_after_and_append_reference'
                  )
            )
        WHEN COALESCE(result #>> '{feature_usage,tracked_approvals_current}', '') ~ '^[0-9]+$'
            THEN (result #>> '{feature_usage,tracked_approvals_current}')::int
        ELSE 0 END
    """
    academic_voice_enabled_expr = """
        CASE
            WHEN lower(COALESCE(result #>> '{academic_voice_settings,enabled}', '')) = 'true' THEN 1
            WHEN lower(COALESCE(result #>> '{academic_voice_settings,enabled}', '')) = 'false' THEN 0
            WHEN COALESCE(result #>> '{feature_usage,academic_voice_enabled_analyses}', '') ~ '^[0-9]+$'
                THEN (result #>> '{feature_usage,academic_voice_enabled_analyses}')::int
            ELSE 0
        END
    """
    academic_voice_completed_expr = """
        CASE WHEN jsonb_typeof(result->'academic_voice_review') = 'object' THEN 1
            WHEN COALESCE(result #>> '{feature_usage,academic_voice_completed_analyses}', '') ~ '^[0-9]+$'
                THEN (result #>> '{feature_usage,academic_voice_completed_analyses}')::int
            ELSE 0 END
    """
    academic_voice_signals_expr = """
        CASE WHEN jsonb_typeof(result->'academic_voice_review'->'signals') = 'array'
            THEN jsonb_array_length(result->'academic_voice_review'->'signals')
            WHEN COALESCE(result #>> '{feature_usage,academic_voice_signals}', '') ~ '^[0-9]+$'
                THEN (result #>> '{feature_usage,academic_voice_signals}')::int
            ELSE 0 END
    """
    academic_voice_revisions_expr = """
        CASE WHEN jsonb_typeof(result->'academic_voice_revisions') = 'object'
            THEN (
                SELECT COUNT(*)
                FROM jsonb_object_keys(result->'academic_voice_revisions') AS revision_key
            )
            WHEN COALESCE(result #>> '{feature_usage,academic_voice_approved_revisions}', '') ~ '^[0-9]+$'
                THEN (result #>> '{feature_usage,academic_voice_approved_revisions}')::int
            ELSE 0 END
    """

    # ------------------------------------------------------------------
    # 1) Relational totals and basic recent uploads.
    # ------------------------------------------------------------------
    with psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, connect_timeout=10) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '10000ms'")

            cursor.execute("SELECT * FROM stats WHERE id = 1")
            stats_row = _as_dict(cursor.fetchone() or {})

            cursor.execute(f"""
                SELECT
                    COUNT(*) AS total_jobs,
                    COUNT(*) FILTER (WHERE lower(coalesce(status, '')) IN {completed_statuses}) AS completed_jobs,
                    COUNT(*) FILTER (WHERE lower(coalesce(status, '')) IN {failed_statuses} OR error IS NOT NULL) AS failed_jobs,
                    COUNT(*) FILTER (WHERE lower(coalesce(status, '')) IN {active_statuses}) AS running_jobs,
                    COUNT(*) FILTER (WHERE lower(coalesce(status, '')) = 'queued') AS queued_jobs,
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

            # Seed daily data from daily_stats so upload/processed counts remain stable.
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

    # ------------------------------------------------------------------
    # 2) Dashboard indicators from jobs.result JSONB for the selected window.
    # ------------------------------------------------------------------
    try:
        with psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, connect_timeout=10) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '20000ms'")

                cursor.execute(f"""
                    SELECT
                        COALESCE(SUM({references_expr}), 0) AS references_count,
                        COALESCE(SUM({citations_expr}), 0) AS citations_count,
                        COALESCE(SUM({missing_expr}), 0) AS missing_citations_count,
                        COALESCE(SUM({uncited_expr}), 0) AS uncited_references_count,
                        COALESCE(SUM({claim_expr}), 0) AS claim_rows,
                        COALESCE(SUM({recovery_expr}), 0) AS recovery_rows,
                        COALESCE(SUM({suggestions_expr}), 0) AS suggestions_count,
                        COALESCE(SUM({verification_rows_expr}), 0) AS verification_rows,
                        COALESCE(SUM({verified_expr}), 0) AS verified,
                        COALESCE(SUM({likely_expr}), 0) AS likely,
                        COALESCE(SUM({needs_review_expr}), 0) AS needs_review,
                        COALESCE(SUM({not_found_expr}), 0) AS not_found,
                        COALESCE(SUM({offline_expr}), 0) AS offline,
                        COALESCE(SUM({evidence_analysis_expr}), 0) AS evidence_workspace_analyses,
                        COALESCE(SUM({evidence_total_expr}), 0) AS evidence_issues_generated,
                        COALESCE(SUM({evidence_pending_expr}), 0) AS evidence_issues_pending,
                        COALESCE(SUM({evidence_resolved_expr}), 0) AS evidence_issues_resolved,
                        COALESCE(SUM({tracked_approvals_expr}), 0) AS tracked_approvals_current,
                        COALESCE(SUM({feature_usage_expr('source_search_requests')}), 0) AS source_search_requests,
                        COALESCE(SUM({feature_usage_expr('context_aware_search_requests')}), 0) AS context_aware_search_requests,
                        COALESCE(SUM({feature_usage_expr('reference_identity_search_requests')}), 0) AS reference_identity_search_requests,
                        COALESCE(SUM({feature_usage_expr('source_search_failures')}), 0) AS source_search_failures,
                        COALESCE(SUM({feature_usage_expr('searches_with_candidates')}), 0) AS searches_with_candidates,
                        COALESCE(SUM({feature_usage_expr('candidate_sources_returned')}), 0) AS candidate_sources_returned,
                        COALESCE(SUM({feature_usage_expr('candidates_withheld_by_context')}), 0) AS candidates_withheld_by_context,
                        COALESCE(SUM({feature_usage_expr('correction_decision_events')}), 0) AS correction_decision_events,
                        COALESCE(SUM({feature_usage_expr('source_approval_events')}), 0) AS source_approval_events,
                        COALESCE(SUM({feature_usage_expr('reference_style_approval_events')}), 0) AS reference_style_approval_events,
                        COALESCE(SUM({academic_voice_enabled_expr}), 0) AS academic_voice_enabled_analyses,
                        COALESCE(SUM({academic_voice_completed_expr}), 0) AS academic_voice_completed_analyses,
                        COALESCE(SUM({academic_voice_signals_expr}), 0) AS academic_voice_signals,
                        COALESCE(SUM({feature_usage_expr('academic_voice_rewrite_requests')}), 0) AS academic_voice_rewrite_requests,
                        COALESCE(SUM({academic_voice_revisions_expr}), 0) AS academic_voice_approved_revisions,
                        AVG({acii_expr}) AS average_acii_score
                    FROM jobs
                    WHERE result IS NOT NULL
                      AND created_at >= %s
                """, (cutoff_dt,))

                row = _as_dict(cursor.fetchone() or {})
                for key in [
                    "references_count", "citations_count", "missing_citations_count",
                    "uncited_references_count", "claim_rows", "recovery_rows", "suggestions_count",
                    "verification_rows", "verified", "likely", "needs_review", "not_found", "offline"
                ] + list(FEATURE_METRIC_KEYS):
                    advanced[key] = _safe_int(row.get(key))

                if row.get("average_acii_score") is not None:
                    advanced["average_acii_score"] = round(_safe_float(row.get("average_acii_score")), 2)

                # Daily JSONB metrics. This overlays citation-integrity counts onto
                # the existing daily upload table rather than replacing it.
                cursor.execute(f"""
                    SELECT
                        created_at::date AS day,
                        COUNT(*) AS uploads,
                        COUNT(*) FILTER (WHERE lower(coalesce(status, '')) IN {completed_statuses}) AS processed,
                        COUNT(*) FILTER (WHERE lower(coalesce(status, '')) IN {failed_statuses} OR error IS NOT NULL) AS failed,
                        COALESCE(SUM({references_expr}), 0) AS references,
                        COALESCE(SUM({citations_expr}), 0) AS citations,
                        COALESCE(SUM({missing_expr}), 0) AS missing_citations,
                        COALESCE(SUM({uncited_expr}), 0) AS uncited_references,
                        COALESCE(SUM({verification_rows_expr}), 0) AS verification_rows,
                        AVG({acii_expr}) AS average_acii_score,
                        AVG(NULLIF(processing_time, 0)) AS avg_processing_time
                    FROM jobs
                    WHERE created_at >= %s
                    GROUP BY created_at::date
                    ORDER BY day DESC
                """, (cutoff_dt,))

                for raw in cursor.fetchall() or []:
                    row = _as_dict(raw)
                    date_key = str(row.get("day"))[:10]
                    existing = daily_stats.get(date_key, {})
                    avg_time = row.get("avg_processing_time")
                    daily_stats[date_key] = {
                        "uploads": max(_safe_int(existing.get("uploads")), _safe_int(row.get("uploads"))),
                        "processed": max(_safe_int(existing.get("processed")), _safe_int(row.get("processed"))),
                        "failed": max(_safe_int(existing.get("failed")), _safe_int(row.get("failed"))),
                        "references": max(_safe_int(existing.get("references")), _safe_int(row.get("references"))),
                        "citations": _safe_int(row.get("citations")),
                        "missing_citations": _safe_int(row.get("missing_citations")),
                        "uncited_references": _safe_int(row.get("uncited_references")),
                        "verification_rows": _safe_int(row.get("verification_rows")),
                        "average_acii_score": round(_safe_float(row.get("average_acii_score")), 2) if row.get("average_acii_score") is not None else None,
                        "avg_processing_time": round(_safe_float(avg_time), 2) if avg_time is not None else _safe_float(existing.get("avg_processing_time")),
                    }

                # Recent documents enriched from jobs.result, not uploads, because
                # the uploads table may contain only basic counts and many zeros.
                cursor.execute(f"""
                    SELECT
                        job_id,
                        created_at AS timestamp,
                        file_name AS filename,
                        file_size_mb,
                        status,
                        error,
                        processing_time,
                        {references_expr} AS references_count,
                        {citations_expr} AS citations_count,
                        {missing_expr} AS missing_citations_count,
                        {uncited_expr} AS uncited_references_count,
                        {verification_rows_expr} AS verification_rows,
                        {acii_expr} AS acii_score
                    FROM jobs
                    WHERE created_at >= %s
                    ORDER BY created_at DESC
                    LIMIT 50
                """, (cutoff_dt,))

                recent_uploads = []
                for raw in cursor.fetchall() or []:
                    row = _as_dict(raw)
                    status = (row.get("status") or "unknown").lower()
                    recent_uploads.append({
                        "job_id": row.get("job_id"),
                        "timestamp": _dt_to_iso(row.get("timestamp")),
                        "filename": row.get("filename") or "Untitled document",
                        "file_size_mb": _safe_float(row.get("file_size_mb")),
                        "references_count": _safe_int(row.get("references_count")),
                        "citations_count": _safe_int(row.get("citations_count")),
                        "missing_citations_count": _safe_int(row.get("missing_citations_count")),
                        "uncited_references_count": _safe_int(row.get("uncited_references_count")),
                        "verification_rows": _safe_int(row.get("verification_rows")),
                        "acii_score": round(_safe_float(row.get("acii_score")), 2) if row.get("acii_score") is not None else None,
                        "processing_time": _safe_float(row.get("processing_time")),
                        "status": status,
                        "success": status in {'completed', 'complete', 'done', 'success', 'finished'} and not row.get("error"),
                        "error": row.get("error"),
                    })

    except Exception as e:
        advanced["error"] = str(e)[:220]
        print(f"[STATS] JSONB dashboard metric aggregation failed: {advanced['error']}")

        # If enriched job rows fail, still show recent uploads from the uploads table.
        try:
            with psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, connect_timeout=10) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT timestamp, filename, file_size, references_count,
                               processing_time, success, error
                        FROM uploads
                        ORDER BY timestamp DESC
                        LIMIT 50
                    """)
                    recent_uploads = []
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
        except Exception as inner:
            print(f"[STATS] fallback recent uploads failed: {inner}")

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

    storage_message = (
        "Persistent PostgreSQL storage is active. Dashboard indicators are populated from jobs.result JSONB. "
        "Data will survive deployments and service restarts."
    )
    if advanced.get("error"):
        storage_message += f" Some optional JSONB metrics could not be refreshed: {advanced.get('error')}"

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
            **{key: advanced[key] for key in FEATURE_METRIC_KEYS},
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
    # Development jobs may live only in the in-process store. Aggregate only
    # count fields from those results; never expose their text in /private-stats.
    local_feature_totals = _empty_feature_metrics()
    try:
        with _lock:
            local_jobs = list(_store.values())
        for local_job in local_jobs:
            result = local_job.get("result") if isinstance(local_job, dict) else {}
            if not isinstance(result, dict):
                continue
            feature_metrics = _extract_feature_metrics(result)
            for key in FEATURE_METRIC_KEYS:
                local_feature_totals[key] += _safe_int(feature_metrics.get(key))
        payload["dashboard_metrics"].update(local_feature_totals)
    except Exception as e:
        print(f"[STATS] SQLite count-only feature aggregation failed: {e}")
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
        "postgresql_connected": DATABASE_URL is not None,
        "release_version": RELEASE_VERSION,
        "release_slot": RELEASE_SLOT,
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
        content={"error": exc.detail, "status_code": exc.status_code, "timestamp": now()},
        # Preserve authentication challenges and any other endpoint-specific
        # headers. Without WWW-Authenticate, browsers show raw 401 JSON instead
        # of opening the developer sign-in prompt.
        headers=exc.headers,
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
