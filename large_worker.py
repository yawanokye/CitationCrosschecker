"""
Temporary large-file worker entrypoint for CiteIntegrity.

Run command:
    python large_worker.py

Purpose:
    Forces this process to listen only to the large_document_processing queue.
    This is useful for Render One-Off Jobs or temporary high-memory workers.
"""

import os
import runpy


# ---------------------------------------------------------
# Force this process to behave as the large-file worker
# ---------------------------------------------------------
os.environ["WORKER_QUEUES"] = "large_document_processing"
os.environ["SERVICE_ROLE"] = "large_document_worker"


# ---------------------------------------------------------
# Safer defaults for large-file processing
# These only apply if the variable is not already set in Render.
# ---------------------------------------------------------
os.environ.setdefault("VERIFY_PARALLEL_MODE", "1")
os.environ.setdefault("VERIFY_PARALLEL_WORKERS", "1")
os.environ.setdefault("VERIFY_INNER_THREADS", "1")

os.environ.setdefault("DEEP_LOOKUPS_IN_VERIFY", "0")
os.environ.setdefault("RUN_REAL_CLAIM_CHECK_IN_VERIFY", "0")
os.environ.setdefault("ENQUEUE_DEEP_ENRICHMENT_AFTER_VERIFY", "0")

os.environ.setdefault("VERIFY_USE_CACHE", "0")
os.environ.setdefault("CACHE_RESULTS_IN_REDIS", "0")
os.environ.setdefault("DELETE_FILE_AFTER_PROCESSING", "1")

os.environ.setdefault("MAX_LARGE_FILE_MB", "80")
os.environ.setdefault("MAX_LARGE_PDF_PAGES", "500")


# ---------------------------------------------------------
# Run your existing worker.py exactly as if you typed:
# python worker.py
# ---------------------------------------------------------
if __name__ == "__main__":
    runpy.run_module("worker", run_name="__main__")
