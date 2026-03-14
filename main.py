# main.py — FULL FILE (Citation Crosschecker Render Optimized - FIXED VERSION)

import io
import os
import re
import uuid
import threading
from datetime import datetime
from typing import Any, Dict, List
from collections import defaultdict

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
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

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "static")),
    name="static"
)

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
# Helper function to normalize text for comparison
# --------------------------------------------------

def _norm_text_citation(s: str) -> str:
    """Normalize citation text for duplicate detection"""
    if not s:
        return ""
    s = s.lower()
    # Remove page numbers, punctuation, and extra spaces
    s = re.sub(r'[^a-z0-9]', '', s)
    return s.strip()


# --------------------------------------------------
# Reference -> citation mapping (FIXED - PROPER COUNTING)
# --------------------------------------------------


def build_reference_to_intext(result):
    """
    Build mapping from references to in-text citations.
    Counts EACH citation occurrence, but prevents duplicate ENTRIES.
    """
    mapping = {}
    rows = result.get("reconciliation_intext_to_reference", [])
    
    # Track ALL citations to count them properly
    # But track unique combinations for display
    citation_counter = defaultdict(int)
    citation_samples = defaultdict(list)
    seen_samples = defaultdict(set)
    
    for r in rows:
        ref = r.get("matched_reference")
        if not ref:
            continue
            
        # Get the citation text
        in_text = r.get("in_text", "")
        
        # Create a normalized version for sample deduplication only
        in_text_norm = _norm_text_citation(in_text)
        
        # COUNT EVERY CITATION (this is for the times_cited number)
        citation_counter[ref] += 1
        
        # For display samples, only keep unique ones (limited to 6)
        if in_text_norm and in_text_norm not in seen_samples[ref]:
            if len(citation_samples[ref]) < 6:
                seen_samples[ref].add(in_text_norm)
                citation_samples[ref].append(in_text)
    
    # Build the result
    for ref in citation_counter:
        mapping[ref] = {
            "reference": ref,
            "times_cited": citation_counter[ref],  # This counts ALL occurrences
            "cited_by": citation_samples.get(ref, [])  # This shows unique samples
        }
    
    # Convert to list and sort by times_cited (most cited first)
    result_list = list(mapping.values())
    result_list.sort(key=lambda x: x["times_cited"], reverse=True)
    
    return result_list

# --------------------------------------------------
# INDEX
# --------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request}
    )


# --------------------------------------------------
# INITIAL DOCUMENT CHECK
# --------------------------------------------------

@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa")
):
    data = await file.read()

    def run():
        return run_crosscheck(
            file_bytes=data,
            filename=file.filename,
            style=style,
            verify_online=False
        )

    result = await run_in_threadpool(run)

    # Build reference -> in-text mapping (deduplicated)
    result["reconciliation_reference_to_intext"] = build_reference_to_intext(result)
    
    # Also ensure in-text citations are unique in the forward mapping
    # This is handled by the reconciliation logic in engine.py, but we'll add
    # a post-processing step to be safe
    if "reconciliation_intext_to_reference" in result:
        # Deduplicate in-text to reference mapping
        unique_cites = {}
        for item in result["reconciliation_intext_to_reference"]:
            cite_text = item.get("in_text", "")
            cite_norm = _norm_text_citation(cite_text)
            if cite_norm and cite_norm not in unique_cites:
                unique_cites[cite_norm] = item
        
        result["reconciliation_intext_to_reference"] = list(unique_cites.values())

    job_id = store_result(result)

    return {
        "job_id": job_id,
        "data": result
    }


# --------------------------------------------------
# ONLINE VERIFICATION
# --------------------------------------------------

@app.post("/verify-online")
async def verify_online(job_id: str = Form(...)):
    job = get_job(job_id)

    if not job:
        raise HTTPException(404, "Job not found")

    refs = job["result"].get("references_raw", [])

    job["online"]["state"] = "running"
    job["online"]["total"] = len(refs)

    def worker():
        try:
            rows = verify_references_batch(refs)

            summary = {
                "verified": 0,
                "likely": 0,
                "needs_review": 0,
                "not_found": 0,
                "offline": 0
            }

            for r in rows:
                status = r.get("status", "offline")
                if status in summary:
                    summary[status] += 1
                else:
                    summary["offline"] += 1

            result = job["result"]

            # Attach verification results
            result["online_verification"] = {
                "rows": rows,
                "summary": summary
            }

            # --------------------------------------------------
            # Compute ACII
            # --------------------------------------------------
            try:
                result["acii"] = compute_acii(result, rows)
            except Exception as e:
                result["acii"] = {
                    "error": str(e)
                }

            # --------------------------------------------------
            # rebuild reference mapping (deduplicated)
            # --------------------------------------------------
            result["reconciliation_reference_to_intext"] = build_reference_to_intext(result)
            
            # Also deduplicate in-text to reference mapping
            if "reconciliation_intext_to_reference" in result:
                unique_cites = {}
                for item in result["reconciliation_intext_to_reference"]:
                    cite_text = item.get("in_text", "")
                    cite_norm = _norm_text_citation(cite_text)
                    if cite_norm and cite_norm not in unique_cites:
                        unique_cites[cite_norm] = item
                
                result["reconciliation_intext_to_reference"] = list(unique_cites.values())

            job["online"]["state"] = "done"
            
        except Exception as e:
            job["online"]["state"] = "error"
            job["online"]["message"] = str(e)

    threading.Thread(target=worker, daemon=True).start()

    return {"started": True}


# --------------------------------------------------
# STATUS POLLING
# --------------------------------------------------

@app.get("/online/status")
def online_status(job_id: str):
    job = get_job(job_id)

    if not job:
        raise HTTPException(404, "Job not found")

    return {
        "online": job["online"],
        "result": job["result"]
    }


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "timestamp": now()
    }


