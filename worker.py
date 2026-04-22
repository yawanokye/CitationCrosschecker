# worker.py - Place in your repository root
import os
import sys
import json
import time
import redis
import psycopg2
from rq import Worker, Queue, Connection

# Get connection strings
REDIS_URL = os.environ.get("REDIS_URL")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not REDIS_URL or not DATABASE_URL:
    print("ERROR: Missing REDIS_URL or DATABASE_URL")
    sys.exit(1)

# Connect to Redis
redis_conn = redis.from_url(REDIS_URL)

# Import your engine (adjust path if needed)
try:
    from engine import run_crosscheck
    print("✅ Engine imported successfully")
except ImportError as e:
    print(f"ERROR: Cannot import engine - {e}")
    sys.exit(1)

def process_document(job_id: str, file_content: bytes, filename: str, style: str = "apa"):
    """Process a document - runs in background"""
    print(f"Processing job {job_id}: {filename}")
    
    try:
        # Connect to PostgreSQL
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        
        # Update status
        cursor.execute(
            "UPDATE jobs SET status = 'processing', started_at = NOW() WHERE job_id = %s",
            (job_id,)
        )
        conn.commit()
        
        # Process the document
        result = run_crosscheck(
            file_bytes=file_content,
            filename=filename,
            style=style,
            verify_online=False
        )
        
        # Store result
        cursor.execute(
            "UPDATE jobs SET status = 'completed', result = %s, completed_at = NOW() WHERE job_id = %s",
            (json.dumps(result), job_id)
        )
        conn.commit()
        
        cursor.close()
        conn.close()
        
        # Cache in Redis
        redis_conn.setex(f"result:{job_id}", 3600, json.dumps(result))
        redis_conn.delete(f"file:{job_id}")
        
        print(f"✅ Completed job {job_id}")
        return result
        
    except Exception as e:
        print(f"❌ Failed job {job_id}: {e}")
        # Update status to failed
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
        worker.work()
