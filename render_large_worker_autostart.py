
"""
render_large_worker_autostart.py

Automatic pay-as-you-use large-file worker launcher for CiteIntegrity.
Call trigger_large_worker_if_needed() after enqueueing a job to
large_document_processing.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict

import requests

LARGE_WORKER_LOCK_KEY = "citeintegrity:large-worker:autostart-lock"
LARGE_WORKER_LAST_JOB_KEY = "citeintegrity:large-worker:last-render-job"


def _env_flag(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def trigger_large_worker_if_needed(redis_conn=None, reason: str = "") -> Dict[str, Any]:
    """
    Start a Render One-Off Job for large_document_processing if one was not
    recently started. Uses a Redis lock to avoid duplicate expensive jobs.
    """
    if not _env_flag("LARGE_WORKER_AUTOSTART_ENABLED", "false"):
        return {"started": False, "reason": "LARGE_WORKER_AUTOSTART_ENABLED is not true"}

    api_key = os.getenv("RENDER_API_KEY", "").strip()
    service_id = os.getenv("RENDER_LARGE_WORKER_BASE_SERVICE_ID", "").strip()
    if not api_key or not service_id:
        return {"started": False, "reason": "Missing RENDER_API_KEY or RENDER_LARGE_WORKER_BASE_SERVICE_ID"}

    lock_ttl = int(os.getenv("LARGE_WORKER_LOCK_TTL_SECONDS", "7200") or "7200")

    if redis_conn is not None:
        try:
            lock_value = f"{int(time.time())}:{reason or 'large-job'}"
            got_lock = redis_conn.set(LARGE_WORKER_LOCK_KEY, lock_value, nx=True, ex=lock_ttl)
            if not got_lock:
                return {"started": False, "reason": "Large worker autostart lock already exists"}
        except Exception as e:
            print(f"[LARGE WORKER AUTOSTART] Redis lock failed; continuing cautiously: {e}")

    start_command = os.getenv("RENDER_LARGE_WORKER_COMMAND", "python large_worker_oneoff_burst.py").strip()
    plan_id = os.getenv("RENDER_LARGE_WORKER_PLAN_ID", "plan-srv-011").strip()

    payload = {"startCommand": start_command}
    if plan_id:
        payload["planId"] = plan_id

    url = f"https://api.render.com/v1/services/{service_id}/jobs"

    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=15,
        )

        if resp.status_code not in {200, 201}:
            if redis_conn is not None:
                try:
                    redis_conn.delete(LARGE_WORKER_LOCK_KEY)
                except Exception:
                    pass
            return {
                "started": False,
                "status_code": resp.status_code,
                "reason": resp.text[:500],
                "payload": payload,
            }

        data = resp.json()
        if redis_conn is not None:
            try:
                redis_conn.setex(
                    LARGE_WORKER_LAST_JOB_KEY,
                    86400,
                    json.dumps({"render_job": data, "payload": payload, "reason": reason}),
                )
            except Exception:
                pass

        return {"started": True, "render_job": data, "payload": payload, "reason": reason}

    except Exception as e:
        if redis_conn is not None:
            try:
                redis_conn.delete(LARGE_WORKER_LOCK_KEY)
            except Exception:
                pass
        return {"started": False, "reason": str(e), "payload": payload}


def clear_large_worker_autostart_lock(redis_conn=None) -> bool:
    if redis_conn is None:
        return False
    try:
        redis_conn.delete(LARGE_WORKER_LOCK_KEY)
        return True
    except Exception:
        return False
