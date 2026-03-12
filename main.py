# main.py — FULL FILE (Citation Crosschecker Render Optimized)

import io
import os
import time
import uuid
import json
import threading
from datetime import datetime
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from engine import run_crosscheck
from verify import verify_references_batch
from acii import compute_acii

APP_TITLE = "CitationCrosschecker"

app = FastAPI(title=APP_TITLE)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

_store: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()

# --------------------------------------------------
# Utility
# --------------------------------------------------

def now():
    return datetime.utcnow().isoformat()

def store_result(result):

    job_id = uuid.uuid4().hex

    with _lock:
        _store[job_id] = {
            "result": result,
            "online": {
                "state": "idle",
                "progress": 0,
                "total": 0
            }
        }

    return job_id


def get_job(job_id):

    with _lock:
        return _store.get(job_id)


# --------------------------------------------------
# NEW: reference -> citation mapping
# --------------------------------------------------

def build_reference_to_intext(result):

    mapping = {}

    rows = result.get("reconciliation_intext_to_reference", [])

    for r in rows:

        ref = r.get("matched_reference")

        if not ref:
            continue

        mapping.setdefault(ref, {
            "reference": ref,
            "times_cited": 0,
            "cited_by": []
        })

        mapping[ref]["times_cited"] += 1
        mapping[ref]["cited_by"].append(r.get("in_text"))

    return list(mapping.values())


# --------------------------------------------------
# INDEX
# --------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# --------------------------------------------------
# INITIAL VERIFY
# --------------------------------------------------

@app.post("/verify")
async def verify(file: UploadFile = File(...), style: str = Form("apa")):

    data = await file.read()

    def run():
        return run_crosscheck(
            file_bytes=data,
            filename=file.filename,
            style=style,
            verify_online=False
        )

    result = await run_in_threadpool(run)

    # Build missing interface structures
    result["reconciliation_reference_to_intext"] = build_reference_to_intext(result)

    job_id = store_result(result)

    return {
        "job_id": job_id,
        "data": result
    }


# --------------------------------------------------
# ONLINE VERIFY
# --------------------------------------------------

@app.post("/verify-online")
async def verify_online(job_id: str = Form(...)):

    job = get_job(job_id)

    if not job:
        raise HTTPException(404)

    refs = job["result"].get("references_raw", [])

    job["online"]["state"] = "running"
    job["online"]["total"] = len(refs)

    def worker():

        rows = verify_references_batch(refs)

        summary = {
            "verified": 0,
            "likely": 0,
            "needs_review": 0,
            "not_found": 0,
            "offline": 0
        }

        for r in rows:
            summary[r["status"]] += 1

        result = job["result"]

        result["online_verification"] = {
            "rows": rows,
            "summary": summary
        }

        # ACII
        result["acii"] = compute_acii(result, rows)

        # rebuild mapping
        result["reconciliation_reference_to_intext"] = build_reference_to_intext(result)

        job["online"]["state"] = "done"

    threading.Thread(target=worker).start()

    return {"started": True}


# --------------------------------------------------
# STATUS
# --------------------------------------------------

@app.get("/online/status")
def online_status(job_id: str):

    job = get_job(job_id)

    if not job:
        raise HTTPException(404)

    return {
        "online": job["online"],
        "result": job["result"]
    }
