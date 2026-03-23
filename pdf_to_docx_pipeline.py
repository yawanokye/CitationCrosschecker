# pdf_to_docx_pipeline.py

import io
import re
from typing import Tuple
from pdf2docx import Converter
from docx import Document


# ---------------------------------------------------------
# STEP 1: Convert PDF → DOCX (in memory or temp file)
# ---------------------------------------------------------
def convert_pdf_to_docx_bytes(pdf_bytes: bytes) -> bytes:
    temp_pdf = "temp_input.pdf"
    temp_docx = "temp_output.docx"

    # save pdf
    with open(temp_pdf, "wb") as f:
        f.write(pdf_bytes)

    # convert
    cv = Converter(temp_pdf)
    cv.convert(temp_docx, start=0, end=None)
    cv.close()

    # read docx back
    with open(temp_docx, "rb") as f:
        docx_bytes = f.read()

    return docx_bytes


# ---------------------------------------------------------
# STEP 2: Clean extracted DOCX text
# ---------------------------------------------------------
def clean_text(text: str) -> str:
    if not text:
        return ""

    # normalize spaces
    text = text.replace("\r", " ").replace("\n", " ")

    # fix broken words from PDF
    text = re.sub(r"-\s+", "", text)

    # normalize multiple spaces
    text = re.sub(r"\s+", " ", text)

    # 🔥 split merged references: ") Author,"
    text = re.sub(r"\)\s+([A-Z][a-z]+,)", r")\n\1", text)

    return text.strip()


# ---------------------------------------------------------
# STEP 3: Extract clean text from DOCX
# ---------------------------------------------------------
def extract_clean_docx_text(docx_bytes: bytes) -> str:
    doc = Document(io.BytesIO(docx_bytes))
    full_text = []

    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            full_text.append(t)

    text = "\n".join(full_text)
    return clean_text(text)


# ---------------------------------------------------------
# STEP 4: Split into main text + references
# ---------------------------------------------------------
def split_main_and_references(text: str) -> Tuple[str, list]:
    lines = text.split("\n")

    ref_index = -1
    for i, line in enumerate(lines):
        if re.search(r"^\s*references?\s*$", line, re.I):
            ref_index = i
            break

    if ref_index == -1:
        return text, []

    main = "\n".join(lines[:ref_index])
    refs = lines[ref_index + 1:]

    return main.strip(), refs


# ---------------------------------------------------------
# STEP 5: FULL PIPELINE
# ---------------------------------------------------------
def process_pdf(pdf_bytes: bytes):
    # convert
    docx_bytes = convert_pdf_to_docx_bytes(pdf_bytes)

    # extract clean text
    text = extract_clean_docx_text(docx_bytes)

    # split
    main_text, references = split_main_and_references(text)

    return {
        "main_text": main_text,
        "references": references
    }
