# worker.py - Place in your repository root
import os
import sys
import json
import time
import redis
import psycopg2
from rq import Worker, Queue, Connection
from engine import run_crosscheck, run_crosscheck_with_autofix
# Get connection strings
REDIS_URL = os.environ.get("REDIS_URL")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not REDIS_URL or not DATABASE_URL:
    print("ERROR: Missing REDIS_URL or DATABASE_URL")
    sys.exit(1)

# Connect to Redis
redis_conn = redis.from_url(REDIS_URL)


def process_document(job_id, filename, style="apa", enable_autofix=False):
    """Process a document - runs in background"""
    print(f"🔥 Processing job {job_id}: {filename}")
    print(f"📋 enable_autofix flag received: {enable_autofix}") 
    
    # 🔥 FORCE AUTOFIX TO TRUE (TEMPORARY FIX)
    # Remove this line after testing
    enable_autofix = True
    print(f"📋 FORCED enable_autofix to: {enable_autofix}")
    # =========================
    # LOAD FILE FROM REDIS
    # =========================
    file_content = redis_conn.get(f"file:{job_id}")
    
    print(f"📦 File size from Redis: {len(file_content) if file_content else 0}")
    
    if not file_content:
        raise Exception(f"❌ File not found in Redis for job {job_id}")

    try:
        # =========================
        # DB CONNECTION
        # =========================
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        
        cursor.execute(
            "UPDATE jobs SET status = 'processing', started_at = NOW() WHERE job_id = %s",
            (job_id,)
        )
        conn.commit()

        # =========================
        # 🔥 ALWAYS RUN AUTOFIX
        # =========================
        print("⚡ Running with AUTO-FIX FORCED ON")

        result = run_crosscheck_with_autofix(
            file_bytes=file_content,
            filename=filename,
            style=style,
            verify_online=False,
            enable_autofix=True   # 🔥 FORCE TRUE
        )

        # =========================
        # DEBUG LOGS
        # =========================
        print("🔍 AUTOFIX PRESENT:", "autofix" in result)
        print("🔍 AUTOFIX CONTENT:", result.get("autofix"))
        print("🔍 RESULT KEYS:", result.keys() if result else "NO RESULT")

        # =========================
        # 🔥 CRITICAL FIX: MAP TO FRONTEND
        # =========================
        # 🔥 HANDLE BOTH STRUCTURES
        autofix = result.get("autofix")
        
        if isinstance(autofix, dict):
            if "suggestions" in autofix:
                result["suggestions"] = autofix["suggestions"]
                print("✅ Suggestions mapped from autofix.suggestions")
            else:
                # autofix itself IS the suggestions object
                result["suggestions"] = autofix
                print("✅ Suggestions mapped directly from autofix")
        else:
            result["suggestions"] = {
                "citations": [],
                "missing": [],
                "unmatched": [],
                "references": []
            }
            print("⚠️ No suggestions structure found")
            

        # =========================
        # SAVE RESULT
        # =========================
        cursor.execute(
            "UPDATE jobs SET status = 'completed', result = %s, completed_at = NOW() WHERE job_id = %s",
            (json.dumps(result), job_id)
        )
        conn.commit()
        
        cursor.close()
        conn.close()
        
        # =========================
        # CACHE RESULT
        # =========================
        redis_conn.setex(f"result:{job_id}", 3600, json.dumps(result))
        redis_conn.delete(f"file:{job_id}")
        
        print(f"✅ Completed job {job_id}")
        return result
        
    except Exception as e:
        print(f"❌ Failed job {job_id}: {e}")
        
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE jobs SET status = 'failed', error = %s WHERE job_id = %s",
            (str(e), job_id)
        )
        conn.commit()
        cursor.close()
        conn.close()
        
        raise e

# Start the worker
if __name__ == "__main__":
    print("🚀 Starting worker...")
    print(f"📊 Redis: {REDIS_URL[:50]}...")
    print(f"💾 PostgreSQL: connected")
    
    with Connection(redis_conn):
        queue = Queue("document_processing", connection=redis_conn)
        worker = Worker(["document_processing"])
        print("✅ Worker ready, waiting for jobs...")
        worker.work(burst=False)
