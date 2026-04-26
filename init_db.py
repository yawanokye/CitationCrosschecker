# init_db.py
import os
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL")

def init_database():
    if not DATABASE_URL:
        print("❌ No DATABASE_URL found in environment")
        return False
    
    try:
        print(f"🔌 Connecting to database...")
        conn = psycopg2.connect(DATABASE_URL)
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
        
        # Create jobs table
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
        
        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_uploads_timestamp ON uploads(timestamp)")
        
        # Insert initial stats if not exists
        cursor.execute("""
            INSERT INTO stats (id, total_uploads, total_processed, total_failed, total_references_checked)
            VALUES (1, 0, 0, 0, 0)
            ON CONFLICT (id) DO NOTHING
        """)
        
        conn.commit()
        
        # Verify tables
        cursor.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        tables = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        print("✅ Database tables verified/created successfully")
        print(f"📊 Tables found: {[t[0] for t in tables]}")
        
        return True
        
    except Exception as e:
        print(f"❌ Database initialization error: {e}")
        return False

if __name__ == "__main__":
    print("=" * 50)
    print("Database Initialization Script")
    print("=" * 50)
    success = init_database()
    if success:
        print("\n✅ Database is ready!")
    else:
        print("\n❌ Database initialization failed. Check your DATABASE_URL.")
