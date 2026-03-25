# In main.py, update the verify_online endpoint to use the new job system

from verify import submit_verification_job, get_verification_status

@app.post("/verify-online")
async def verify_online(job_id: str = Form(...)):
    """Submit online verification job"""
    
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    if job["online"]["state"] == "running":
        return {"started": False, "message": "Verification already in progress"}

    refs = job["result"].get("references_raw", [])
    
    if not refs:
        job["online"]["state"] = "done"
        return {"started": False, "message": "No references to verify"}

    # Submit to verification (this runs in background)
    verification_job_id = submit_verification_job(refs, style="apa")
    
    job["online"]["state"] = "running"
    job["online"]["total"] = len(refs)
    job["online"]["progress"] = 0
    job["online"]["verification_job_id"] = verification_job_id
    
    return {"started": True, "job_id": job_id, "total_references": len(refs)}
