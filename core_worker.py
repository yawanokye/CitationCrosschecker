"""Cost-optimised always-on worker for uploads and verification."""

import os
import runpy


os.environ["WORKER_QUEUES"] = os.environ.get(
    "CORE_WORKER_QUEUES",
    "document_processing,verification,large_document_processing",
)
os.environ.setdefault("SERVICE_ROLE", "core_worker")


if __name__ == "__main__":
    runpy.run_module("worker", run_name="__main__")
