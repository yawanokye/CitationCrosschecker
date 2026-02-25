# main.py (FULL FILE) — Citation Crosschecker (Render-safe)
# - /verify runs parsing + reconciliation fast and returns job_id
# - If verify_online=true, starts background online verification in batches (default 60)
# - /online/status lets UI poll progress + partial rows (dashboard updates live)
# - /export/csv and /export/word export the stored results using job_id
# - Optional AI Assist (DeepSeek) runs in background and patches reconciliation results

import io
import os
import time
import uuid
import json
import re
import threading
import requests
from datetime import datetime
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool


# -----------------------------
# Imports from your project
# -----------------------------
try:
    from engine import run_crosscheck
    ENGINE_OK = True
except Exception:
    ENGINE_OK = False
    run_crosscheck = None

try:
    # Optional: used for AI-assisted re-reconciliation (safe if missing)
    from engine import (
        extract_author_year_citations,
        extract_numeric_citations,
        parse_reference_author_year,
        parse_reference_numeric,
        reconcile_author_year,
        reconcile_numeric,
        read_docx_split_main_and_refs,
        read_pdf_text,
    )
    ENGINE_AI_OK = True
except Exception:
    ENGINE_AI_OK = False

try:
    from verify import verify_references_batch
    VERIFY_OK = True
except Exception:
    VERIFY_OK = False
    verify_references_batch = None

try:
    import pandas as pd
    PANDAS_OK = True
except Exception:
    PANDAS_OK = False
    pd = None

try:
    from docx import Document as DocxDocument
    DOCX_OK = True
except Exception:
    DOCX_OK = False
    DocxDocument = None


# -----------------------------
# Config
# -----------------------------
APP_TITLE = "CitationCrosschecker"

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "40"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

RESULT_TTL_SECONDS = int(os.getenv("RESULT_TTL_SECONDS", "3600"))

# How many references to verify per background batch
BATCH_SIZE_DEFAULT = int(os.getenv("VERIFY_BATCH_SIZE", "60"))

# Optional: throttle between batches to reduce burst pressure
BATCH_PAUSE_S = float(os.getenv("VERIFY_BATCH_PAUSE_S", "0.05"))


# -----------------------------
# In-memory store (simple + Render-friendly)
# -----------------------------
_result_store: Dict[str, Dict[str, Any]] = {}
_store_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _prune_store() -> None:
    now = time.time()
    dead = []
    for k, v in _result_store.items():
        if now - float(v.get("_stored_at", now)) > RESULT_TTL_SECONDS:
            dead.append(k)
    for k in dead:
        _result_store.pop(k, None)


def _store_result(result: Dict[str, Any]) -> str:
    with _store_lock:
        _prune_store()
        job_id = uuid.uuid4().hex
        _result_store[job_id] = {
            "_stored_at": time.time(),
            "result": result,
            "online": {
                "state": "idle",       # idle|running|done|error
                "progress": 0,
                "total": 0,
                "message": "",
                "started_at": "",
                "finished_at": "",
            },
            "ai": {
                "state": "idle",       # idle|running|done|error|skipped
                "message": "",
                "started_at": "",
                "finished_at": "",
                "added_citations": 0,
            },
            # stored only when AI assist is requested (kept small / optional)
            "file_bytes": b"",
            "file_name": "",
            "style": "",
        }
        return job_id


def _get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _store_lock:
        _prune_store()
        return _result_store.get(job_id)


def _safe_filename(name: str) -> str:
    name = (name or "").strip() or "output"
    name = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_", ".", " "))
    name = name.replace(" ", "_")
    return name[:120] if len(name) > 120 else name


def _wrap_output(payload: Dict[str, Any]) -> Dict[str, Any]:
    ok = "error" not in (payload or {})
    return {"ok": bool(ok), "ts": _now_iso(), "data": payload or {}}


def _read_upload_bytes(up: UploadFile) -> bytes:
    data = up.file.read() or b""
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large. Max {MAX_UPLOAD_MB} MB.")
    return data


_ALLOWED_VERIFY_STATUSES = {"verified", "likely", "needs_review", "not_found", "offline"}


def _normalize_verify_status(s: str) -> str:
    st = (s or "").strip().lower().replace(" ", "_")
    if st not in _ALLOWED_VERIFY_STATUSES:
        st = "needs_review"
    return st


# -----------------------------
# DeepSeek AI Assist (optional, server-side only)
# -----------------------------
DEEPSEEK_API_KEY = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
DEEPSEEK_URL = (os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com/v1/chat/completions").strip()

# keep it strict to avoid hallucinations
_AI_YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20\d{2})([a-z])?\b", re.I)

# Discourse / non-author words we should ignore if AI returns them as authors
_AI_NON_AUTHOR = {
    "however", "similarly", "regretably", "regrettably", "traditionally", "therefore", "moreover",
    "furthermore", "consequently", "notably", "generally", "specifically", "overall", "in", "on",
}


def _deepseek_chat(messages: List[Dict[str, str]], timeout_s: float = 25.0) -> str:
    if not DEEPSEEK_API_KEY:
        return ""
    try:
        r = requests.post(
            DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": (os.getenv("DEEPSEEK_MODEL") or "deepseek-chat").strip(),
                "messages": messages,
                "temperature": 0,
            },
            timeout=timeout_s,
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except Exception:
        return ""


def _deepseek_extract_citations(snippets: List[str], style_hint: str) -> List[Dict[str, Any]]:
    """Extract citations from snippets. Returns list of dicts:
    - APA: {raw, author, year}
    - numeric: {raw, nums:[...]}
    """
    if not DEEPSEEK_API_KEY or not snippets:
        return []

    # hard cap to keep requests small and fast
    snippets = [s for s in snippets if s and s.strip()][:120]
    if not snippets:
        return []

    system = (
        "You extract in-text citations from academic writing. "
        "Return ONLY valid JSON. Do not add commentary."
    )

    if style_hint == "numeric":
        user = {
            "role": "user",
            "content": (
                "From the snippets, extract numeric in-text citations. "
                "Return JSON array of objects with keys: raw, nums. "
                "nums is an array of strings like [\"1\",\"2\",\"3\"]. "
                "Only include citations explicitly present.\n\nSNIPPETS:\n"
                + json.dumps(snippets)
            ),
        }
    else:
        user = {
            "role": "user",
            "content": (
                "From the snippets, extract author-year in-text citations. "
                "Return JSON array of objects with keys: raw, author, year. "
                "author should be the first author surname or an org key. "
                "Only include citations explicitly present.\n\nSNIPPETS:\n"
                + json.dumps(snippets)
            ),
        }

    content = _deepseek_chat(
        messages=[{"role": "system", "content": system}, user],
        timeout_s=28.0,
    )
    if not content:
        return []

    # try to locate JSON array
    m = re.search(r"\[[\s\S]*\]", content)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except Exception:
        return []

    out: List[Dict[str, Any]] = []

    if style_hint == "numeric":
        for it in items if isinstance(items, list) else []:
            raw = str((it or {}).get("raw", "")).strip()
            nums = (it or {}).get("nums", [])
            if not raw or not isinstance(nums, list):
                continue
            nums2 = []
            for n in nums:
                s = str(n).strip()
                if re.fullmatch(r"\d{1,4}", s):
                    nums2.append(s)
            if nums2:
                out.append({"raw": raw, "nums": nums2})
        return out

    # APA/Harvard
    for it in items if isinstance(items, list) else []:
        raw = str((it or {}).get("raw", "")).strip()
        author = str((it or {}).get("author", "")).strip()
        year = str((it or {}).get("year", "")).strip()

        if not raw or not author or not _AI_YEAR_RE.search(year):
            continue

        a = author.strip().lower()
        if a in _AI_NON_AUTHOR:
            continue

        out.append({"raw": raw, "author": author, "year": _AI_YEAR_RE.search(year).group(0)})
    return out


def _select_ai_snippets(text: str, style_hint: str) -> List[str]:
    """Pick likely-problematic sentences/clauses to send to AI."""
    t = text or ""
    if not t:
        return []
    # coarse sentence split
    parts = re.split(r"(?<=[\.\!\?])\s+", t)
    keep: List[str] = []
    for s in parts:
        s0 = (s or "").strip()
        if not s0 or len(s0) < 25:
            continue
        if len(s0) > 600:
            s0 = s0[:600]

        if style_hint == "numeric":
            if re.search(r"\[\s*\d{1,4}(?:\s*[-–,]\s*\d{1,4})*\s*\]", s0) or re.search(r"\(\s*\d{1,4}(?:\s*[,\\-–]\s*\d{1,4})+\s*\)", s0):
                keep.append(s0)
        else:
            # focus on sentences with years and typical messy patterns
            if _AI_YEAR_RE.search(s0) and (
                "&" in s0
                or "et al" in s0.lower()
                or "for instance" in s0.lower()
                or "e.g" in s0.lower()
                or "(" in s0
            ):
                keep.append(s0)

        if len(keep) >= 120:
            break
    return keep


# -----------------------------
# Export helpers
# -----------------------------
def _get_result_from_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload.")
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise HTTPException(400, "Missing job_id.")
    job = _get_job(job_id)
    if not job:
        raise HTTPException(404, "job_id not found or expired.")
    return job.get("result") or {}


def _export_csv_bytes(result: Dict[str, Any]) -> bytes:
    # Export: reconciliation_intext_to_reference + missing + uncited + verify rows
    out = io.StringIO()
    out.write("SECTION,STATUS,IN_TEXT,MATCHED_REFERENCE,FLAGS\n")
    for row in (result.get("reconciliation_intext_to_reference") or []):
        out.write(
            f"intext_to_reference,"
            f"{row.get('status','')},"
            f"{json.dumps(row.get('in_text',''))},"
            f"{json.dumps(row.get('matched_reference',''))},"
            f"{json.dumps(row.get('flags',''))}\n"
        )

    out.write("\nSECTION,CITATION_IN_TEXT,COUNT_IN_TEXT\n")
    for row in (result.get("missing_in_references") or []):
        out.write(f"missing,{json.dumps(row.get('citation_in_text',''))},{row.get('count_in_text',0)}\n")

    out.write("\nSECTION,UNCITED_REFERENCE\n")
    for ref in (result.get("uncited_references") or []):
        out.write(f"uncited,{json.dumps(ref)}\n")

    # Online verification (if any)
    ov = (result.get("online_verification") or {})
    rows = (ov.get("rows") or [])
    out.write("\nSECTION,VERIFY_STATUS,REFERENCE,FOUND_TITLE,FOUND_DOI,SCORE,SOURCE\n")
    for r in rows:
        out.write(
            f"verify,{r.get('status','')},"
            f"{json.dumps(r.get('reference_full',''))},"
            f"{json.dumps(r.get('found_title',''))},"
            f"{json.dumps(r.get('found_doi',''))},"
            f"{r.get('score','')},"
            f"{json.dumps(r.get('source',''))}\n"
        )

    return out.getvalue().encode("utf-8", errors="ignore")


def _export_word_bytes(result: Dict[str, Any]) -> bytes:
    if not DOCX_OK:
        raise HTTPException(500, "python-docx not installed on server.")

    doc = DocxDocument()
    doc.add_heading("Citation Crosschecker Report", level=1)
    doc.add_paragraph(f"Generated: {_now_iso()}")
    doc.add_paragraph(f"Filename: {result.get('filename','')}")
    doc.add_paragraph(f"Style: {result.get('style','')}")

    s = result.get("summary") or {}
    doc.add_heading("Summary", level=2)
    for k in ["in_text_citations_found", "reference_entries_found", "missing_in_references", "uncited_references", "match_rate"]:
        doc.add_paragraph(f"{k}: {s.get(k)}")

    ai = result.get("ai_assist") or {}
    if ai.get("enabled"):
        doc.add_paragraph(f"AI Assist: enabled (added_citations={ai.get('added_citations',0)}, snippets_sent={ai.get('snippets_sent',0)})")

    doc.add_heading("Missing in References", level=2)
    for row in (result.get("missing_in_references") or []):
        doc.add_paragraph(f"- {row.get('citation_in_text','')} (count={row.get('count_in_text',0)})")

    doc.add_heading("Uncited References", level=2)
    for ref in (result.get("uncited_references") or []):
        doc.add_paragraph(f"- {ref}")

    doc.add_heading("Reconciliation: In-text → Reference", level=2)
    for row in (result.get("reconciliation_intext_to_reference") or [])[:300]:
        doc.add_paragraph(f"[{row.get('status','')}] {row.get('in_text','')} → {row.get('matched_reference','')}")

    ov = (result.get("online_verification") or {})
    rows = (ov.get("rows") or [])
    if rows:
        doc.add_heading("Online Verification", level=2)
        for r in rows[:300]:
            doc.add_paragraph(f"[{r.get('status','')}] {r.get('reference_full','')} | DOI={r.get('found_doi','')} | src={r.get('source','')}")

    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()


# -----------------------------
# Online verification worker (background)
# -----------------------------
def _run_online_batches(
    job_id: str,
    verify_mode: str,
    throttle_s: float,
    use_crossref: bool,
    use_openalex: bool,
    batch_size: int,
) -> None:
    job = _get_job(job_id)
    if not job:
        return

    if not VERIFY_OK:
        with _store_lock:
            job["online"]["state"] = "error"
            job["online"]["message"] = "verify.py not available on server."
            job["online"]["finished_at"] = _now_iso()
        return

    with _store_lock:
        job["online"]["state"] = "running"
        job["online"]["progress"] = 0
        job["online"]["message"] = "Starting..."
        job["online"]["started_at"] = _now_iso()

    try:
        result = job.get("result") or {}
        refs_raw = result.get("references_raw") or []
        total = len(refs_raw)

        with _store_lock:
            job["online"]["total"] = total
            job["online"]["message"] = f"Queued {total} references"

        all_rows: List[Dict[str, Any]] = []

        for start in range(0, total, max(1, int(batch_size or 60))):
            job = _get_job(job_id)
            if not job:
                return

            chunk = refs_raw[start:start + max(1, int(batch_size or 60))]
            if not chunk:
                continue

            rows = verify_references_batch(
                references=chunk,
                style=(result.get("style") or "apa"),
                verify_mode=verify_mode,
                throttle_s=throttle_s,
                use_crossref=use_crossref,
                use_openalex=use_openalex,
            ) or []

            # normalize statuses
            for r in rows:
                r["status"] = _normalize_verify_status(r.get("status"))

            all_rows.extend(rows)

            # counts
            counts: Dict[str, int] = {"verified": 0, "likely": 0, "needs_review": 0, "not_found": 0, "offline": 0}
            for r in all_rows:
                st = _normalize_verify_status(r.get("status"))
                counts[st] = counts.get(st, 0) + 1

            with _store_lock:
                result = job.get("result") or {}
                result["online_verification"] = {
                    "summary": {**counts, "total": int(sum(counts.values()))},
                    "rows": all_rows,
                }
                job["online"]["progress"] = min(start + len(chunk), total)
                job["online"]["message"] = f"Processed {job['online']['progress']} / {total}"

            time.sleep(BATCH_PAUSE_S)

        with _store_lock:
            job["online"]["state"] = "done"
            job["online"]["message"] = "Online verification completed"
            job["online"]["finished_at"] = _now_iso()

    except Exception as e:
        with _store_lock:
            job["online"]["state"] = "error"
            job["online"]["message"] = f"Online verification failed: {e}"
            job["online"]["finished_at"] = _now_iso()


# -----------------------------
# AI assist worker (DeepSeek) — runs in background and patches stored result
# -----------------------------
def _run_ai_assist(job_id: str) -> None:
    job = _get_job(job_id)
    if not job:
        return

    if not DEEPSEEK_API_KEY:
        with _store_lock:
            job["ai"]["state"] = "skipped"
            job["ai"]["message"] = "AI assist skipped: DEEPSEEK_API_KEY not set"
            job["ai"]["finished_at"] = _now_iso()
        return

    if not ENGINE_AI_OK:
        with _store_lock:
            job["ai"]["state"] = "skipped"
            job["ai"]["message"] = "AI assist skipped: engine helpers not available"
            job["ai"]["finished_at"] = _now_iso()
        return

    fb = job.get("file_bytes") or b""
    fname = job.get("file_name") or "upload"
    style_s = (job.get("style") or "apa").strip().lower()
    style_hint = "numeric" if ("ieee" in style_s or "vancouver" in style_s or "numeric" in style_s) else "apa"

    if not fb:
        with _store_lock:
            job["ai"]["state"] = "error"
            job["ai"]["message"] = "AI assist failed: missing file bytes"
            job["ai"]["finished_at"] = _now_iso()
        return

    try:
        # 1) extract main text (avoid refs where possible)
        if fname.lower().endswith(".docx"):
            main_text, _ref_lines, _msg = read_docx_split_main_and_refs(fb)
        elif fname.lower().endswith(".pdf"):
            full_text = read_pdf_text(fb)
            # crude split: stop at first References heading if present
            m = re.search(r"^\s*(references|bibliography|works\s+cited)\b.*$", full_text, flags=re.I | re.M)
            main_text = full_text[: m.start()] if m else full_text
        else:
            main_text = fb.decode("utf-8", errors="ignore")

        if len(main_text) > 350_000:
            half = 175_000
            main_text = main_text[:half] + "\n... [TRUNCATED] ...\n" + main_text[-half:]

        # 2) choose snippets and ask AI
        snippets = _select_ai_snippets(main_text, style_hint=style_hint)
        ai_items = _deepseek_extract_citations(snippets, style_hint=style_hint)

        # 3) rebuild reconciliation using AI-added citations
        base_result = (job.get("result") or {})
        references_raw = base_result.get("references_raw") or []

        if style_hint == "numeric":
            # base citations: bracketed first
            cites = extract_numeric_citations(main_text, bracketed=True)
            if "vancouver" in style_s and len(cites) < 3:
                cites = extract_numeric_citations(main_text, bracketed=False)

            # add AI nums
            for it in ai_items:
                for n in it.get("nums", []):
                    if re.fullmatch(r"\d{1,4}", str(n)):
                        cites.append(str(n))

            refs = [parse_reference_numeric(r) for r in references_raw]
            refs = [r for r in refs if r is not None]

            c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_numeric(cites, refs)
            ref_count = len(refs)

        else:
            cites = extract_author_year_citations(main_text)

            # add AI citations as raw strings like "Author, YEAR"
            for it in ai_items:
                a = (it.get("author") or "").strip()
                y = (it.get("year") or "").strip()
                if a and _AI_YEAR_RE.search(y):
                    cites.append(f"{a}, {_AI_YEAR_RE.search(y).group(0)}")

            refs = [parse_reference_author_year(r) for r in references_raw]
            refs = [r for r in refs if r is not None]

            c2r, r2c, missing_rows, uncited_refs, intext_count = reconcile_author_year(cites, refs)
            ref_count = len(refs)

        # recompute match rate (same rule as engine)
        missing_unique = int(len(missing_rows or []))
        match_rate = 0.0
        if intext_count > 0:
            match_rate = 100.0 * max(0.0, float(intext_count - missing_unique)) / float(intext_count)

        # patch result in store
        base_result["ai_assist"] = {
            "enabled": True,
            "added_citations": int(len(ai_items)),
            "snippets_sent": int(len(snippets)),
        }
        base_result["summary"] = {
            **(base_result.get("summary") or {}),
            "in_text_citations_found": int(intext_count),
            "reference_entries_found": int(ref_count),
            "missing_in_references": int(missing_unique),
            "uncited_references": int(len(uncited_refs)),
            "match_rate": float(round(match_rate, 1)),
        }
        base_result["missing_in_references"] = missing_rows
        base_result["uncited_references"] = uncited_refs
        base_result["reconciliation_intext_to_reference"] = c2r
        base_result["reconciliation_reference_to_intext"] = r2c

        with _store_lock:
            job["result"] = base_result
            job["ai"]["state"] = "done"
            job["ai"]["message"] = "AI assist completed"
            job["ai"]["finished_at"] = _now_iso()
            job["ai"]["added_citations"] = int(len(ai_items))

    except Exception as e:
        with _store_lock:
            job["ai"]["state"] = "error"
            job["ai"]["message"] = f"AI assist failed: {e}"
            job["ai"]["finished_at"] = _now_iso()


# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title=APP_TITLE)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    style: str = Form("apa"),
    verify_online: str = Form("false"),  # if true: we start background job, not inline
    verify_mode: str = Form("all"),
    max_verify: str = Form("0"),         # kept for UI compatibility
    throttle_s: str = Form("0.12"),
    use_crossref: str = Form("true"),
    use_openalex: str = Form("true"),
    ai_assist: str = Form("false"),
):
    if not ENGINE_OK:
        raise HTTPException(500, "engine.py import failed on server.")

    filename = _safe_filename(file.filename or "upload")
    file_bytes = _read_upload_bytes(file)

    style_s = (style or "apa").strip().lower()
    verify_online_b = str(verify_online).strip().lower() in {"1", "true", "yes", "y", "on"}
    verify_mode_s = (verify_mode or "all").strip().lower()

    use_crossref_b = str(use_crossref).strip().lower() in {"1", "true", "yes", "y", "on"}
    use_openalex_b = str(use_openalex).strip().lower() in {"1", "true", "yes", "y", "on"}
    ai_assist_b = str(ai_assist).strip().lower() in {"1", "true", "yes", "y", "on"}

    try:
        throttle_f = float(throttle_s or 0.12)
    except Exception:
        throttle_f = 0.12

    def _do_crosscheck() -> Dict[str, Any]:
        # Always run offline here so /verify returns fast and avoids Render router timeouts.
        return run_crosscheck(
            file_bytes=file_bytes,
            filename=filename,
            style=style_s,
            verify_online=False,
            verify_mode=verify_mode_s,
            max_verify=0,
            throttle_s=throttle_f,
            use_crossref=use_crossref_b,
            use_openalex=use_openalex_b,
        )

    result = await run_in_threadpool(_do_crosscheck)
    job_id = _store_result(result)

    # Attach file bytes for optional AI assist (server-side only)
    if ai_assist_b:
        job = _get_job(job_id)
        if job is not None:
            # avoid huge memory spikes
            if len(file_bytes) <= 8 * 1024 * 1024:
                with _store_lock:
                    job["file_bytes"] = file_bytes
                    job["file_name"] = filename
                    job["style"] = style_s
                    job["ai"]["state"] = "running"
                    job["ai"]["message"] = "AI assist running"
                    job["ai"]["started_at"] = _now_iso()
                threading.Thread(target=_run_ai_assist, args=(job_id,), daemon=True).start()
            else:
                with _store_lock:
                    job["ai"]["state"] = "skipped"
                    job["ai"]["message"] = "AI assist skipped: file too large"
                    job["ai"]["finished_at"] = _now_iso()

    # Start background online verification if requested
    if verify_online_b:
        t = threading.Thread(
            target=_run_online_batches,
            args=(job_id, verify_mode_s, throttle_f, use_crossref_b, use_openalex_b, BATCH_SIZE_DEFAULT),
            daemon=True,
        )
        t.start()

    wrapped = _wrap_output(result)
    wrapped["job_id"] = job_id
    wrapped["online_started"] = bool(verify_online_b)
    wrapped["ai_started"] = bool(ai_assist_b)
    wrapped["batch_size"] = BATCH_SIZE_DEFAULT
    return JSONResponse(wrapped)


@app.get("/online/status")
def online_status(job_id: str, include_result: int = 0):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(404, "job_id not found or expired.")
    return {
        "ok": True,
        "ts": _now_iso(),
        "job_id": job_id,
        "online": job.get("online") or {},
        "ai": job.get("ai") or {},
        "online_verification": (job.get("result") or {}).get("online_verification") or {"summary": {}, "rows": []},
        "result": (job.get("result") or {}) if include_result else {},
    }


@app.post("/export/csv")
async def export_csv(payload: Dict[str, Any]):
    result = _get_result_from_request(payload)

    def _do() -> bytes:
        return _export_csv_bytes(result)

    data = await run_in_threadpool(_do)
    fn = _safe_filename(result.get("filename") or "crosscheck") + ".csv"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


@app.post("/export/word")
async def export_word(payload: Dict[str, Any]):
    result = _get_result_from_request(payload)

    def _do() -> bytes:
        return _export_word_bytes(result)

    data = await run_in_threadpool(_do)
    fn = _safe_filename(result.get("filename") or "crosscheck") + ".docx"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )
