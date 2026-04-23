import psycopg2

# Your Render PostgreSQL connection string (copy from Render dashboard)
DATABASE_URL = "postgresql://citeintegrity:tey5nazFNiI0dj7D60FXlFkVwyPlXYjN@dpg-d7kiapvavr4c73bk9h60-a/citeintegrity"

try:
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    
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
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
    
    conn.commit()
    print("✅ Table 'jobs' created successfully!")
    
    cursor.close()
    conn.close()
    
except Exception as e:
    print(f"❌ Error: {e}")
