from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from engine import run_crosscheck

app = FastAPI(title="Citation Crosschecker API", version="1.0.0")

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/check")
async def check(
    file: UploadFile = File(...),
    style: str = Form("apa"),
):
    data = await file.read()

    if not (file.filename or "").lower().endswith((".docx", ".pdf")):
        raise HTTPException(status_code=400, detail="Upload a DOCX or PDF")

    return run_crosscheck(data, file.filename or "upload", style=style)
