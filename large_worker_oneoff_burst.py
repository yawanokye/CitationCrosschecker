
"""
large_worker_oneoff_burst.py

Burst-mode large-file worker for Render One-Off Jobs.
It drains large_document_processing and exits when the queue is empty.
"""

import os
import sys

import redis
from rq import Connection, Queue, Worker

# Import worker.py so queued jobs like worker.process_document are resolvable.
import worker  # noqa: F401

REDIS_URL = os.getenv("REDIS_URL", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if not REDIS_URL:
    print("ERROR: REDIS_URL is not set.")
    sys.exit(1)

if not DATABASE_URL:
    print("ERROR: DATABASE_URL is not set.")
    sys.exit(1)

queue_env = os.getenv("ONEOFF_QUEUES", "large_document_processing")
queue_names = [q.strip() for q in queue_env.split(",") if q.strip()]

redis_conn = redis.from_url(REDIS_URL)

print("🚀 CiteIntegrity automatic large-file One-Off Worker")
print(f"📌 Queues: {queue_names}")
print("⚡ Burst mode: true — this worker exits when the queue is empty.")

with Connection(redis_conn):
    queues = [Queue(name, connection=redis_conn) for name in queue_names]
    for q in queues:
        print(f"📌 Queue {q.name}: {q.count} waiting job(s)")
    Worker(queues, connection=redis_conn).work(burst=True)
