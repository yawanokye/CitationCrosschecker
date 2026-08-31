"""Dedicated worker for customer-requested paid deep enrichment."""

import os
import runpy


os.environ["WORKER_QUEUES"] = os.environ.get("DEEP_WORKER_QUEUES", "deep_enrichment")
os.environ.setdefault("SERVICE_ROLE", "deep_enrichment_worker")

# Keep nested network work conservative on the smaller launch worker.
os.environ.setdefault("DEEP_LOOKUPS_IN_VERIFY", "0")
os.environ.setdefault("RUN_REAL_CLAIM_CHECK_IN_VERIFY", "0")


if __name__ == "__main__":
    runpy.run_module("worker", run_name="__main__")
