import psycopg2
from psycopg2.extras import RealDictCursor
import os

# Your Render PostgreSQL connection string
DATABASE_URL = "postgresql://citeintegrity:tey5nazFNiI0dj7D60FXlFkVwyPlXYjN@dpg-d7kiapvavr4c73bk9h60-a/citeintegrity"

# Alternative: Read from environment variable (better practice)
# DATABASE_URL = os.environ.get("DATABASE_URL")

try:
    print("🔌 Connecting to PostgreSQL...")
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    print("✅ Connected successfully!")
    
    # ========== CREATE JOBS TABLE ==========
    print("📋 Creating 'jobs' table...")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT CHECK(status IN ('queued', 'processing', 'completed', 'failed')),
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
    
    # Create indexes for faster queries
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)")
    print("✅ 'jobs' table created with indexes")
    
    # ========== CREATE STATS TABLES ==========
    print("📊 Creating stats tables...")
    
    # Stats table
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
    
    # Insert initial stats if not exists
    cursor.execute("""
        INSERT INTO stats (id, total_uploads, total_processed, total_failed, total_references_checked)
        VALUES (1, 0, 0, 0, 0)
        ON CONFLICT (id) DO NOTHING
    """)
    
    # Uploads table for tracking
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
    
    # Daily stats table
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
    
    conn.commit()
    print("✅ All tables created successfully!")
    
    # ========== VERIFY TABLES EXIST ==========
    print("\n📋 Verifying tables:")
    cursor.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public'
        ORDER BY table_name
    """)
    tables = cursor.fetchall()
    for table in tables:
        print(f"   - {table[0]}")
    
    cursor.close()
    conn.close()
    
    print("\n✅ Database setup complete! Your app is ready to use.")
    
except Exception as e:
    print(f"❌ Error: {e}")
    
    # Provide helpful troubleshooting tips
    print("\n🔧 Troubleshooting tips:")
    print("   1. Check if your database is active in Render Dashboard")
    print("   2. Verify the connection string is correct")
    print("   3. Make sure your IP is allowed (if using external connection)")
