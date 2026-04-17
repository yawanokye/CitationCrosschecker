# ============================================================
# pipeline.py — Unified Async Processing Pipeline
# Supports BOTH:
#   1. Formatter (raw_reference text)
#   2. Full document analysis (file_bytes)
# ============================================================

from typing import Dict, Any, Union
from concurrent.futures import ThreadPoolExecutor

# Core modules
from engine import run_crosscheck
from verify import run_verification
from formatter import process_references
from claim_checker import build_claim_support_rows
from acii import compute_acii


# ============================================================
# 🟢 1. FORMATTER PIPELINE (FAST)
# ============================================================

def run_formatter_pipeline(raw_reference: str, style: str) -> Dict[str, Any]:
    """
    Lightweight pipeline for reference formatting + repair.
    Used by Reference Formatter page.
    """
    try:
        formatted = process_references(
            raw_reference=raw_reference,
            style=style,
            variant="generic",
            source_type="journal"
        )

        return {
            "type": "formatter",
            "formatted": formatted.get("formatted", ""),
            "warnings": formatted.get("warnings", []),
            "repair_results": formatted.get("repair_results", [])
        }

    except Exception as e:
        return {
            "type": "formatter",
            "formatted": "",
            "warnings": [f"Formatter error: {str(e)}"],
            "repair_results": []
        }


# ============================================================
# 🔵 2. FULL DOCUMENT PIPELINE (ADVANCED)
# ============================================================

def run_full_pipeline(file_bytes: bytes, style: str, run_verify: bool = False) -> Dict[str, Any]:
    """
    Full academic integrity pipeline (ASYNC-READY):
    Engine → Formatter → Claims → ACII
    Verification is handled separately (async)
    """
    result = {}

    try:
        # =========================
        # STEP 1 — ENGINE (CPU HEAVY)
        # =========================
        engine_result = run_crosscheck(file_bytes)
        result["engine"] = engine_result

        # =========================
        # STEP 2 — FORMATTER (PARALLEL SAFE)
        # =========================
        with ThreadPoolExecutor(max_workers=2) as pool:

            future_format = pool.submit(
                process_references,
                raw_reference=engine_result.get("references_text", ""),
                style=style,
                variant="generic",
                source_type="journal"
            )

            formatted = future_format.result()

        result["formatted"] = formatted

        # =========================
        # STEP 3 — PLACEHOLDER VERIFICATION (IMPORTANT)
        # =========================
        result["online_verification"] = {
            "rows": [],
            "summary": {
                "status": "pending",
                "message": "Verification running in background"
            }
        }

        # =========================
        # STEP 4 — CLAIM SUPPORT (WITHOUT VERIFICATION)
        # =========================
        claims = build_claim_support_rows({
            **engine_result,
            "online_verification": result["online_verification"]
        })
        result["claims"] = claims

        # =========================
        # STEP 5 — ACII (WITHOUT VERIFY)
        # =========================
        acii_score = compute_acii(
            engine_result,
            []   # no verification yet
        )
        result["acii"] = acii_score

        return {
            "type": "full",
            "result": result
        }

    except Exception as e:
        return {
            "type": "full",
            "error": str(e),
            "result": result
        }


# ============================================================
# 🧠 3. SMART ROUTER (AUTO-DETECT INPUT)
# ============================================================

def run_pipeline(input_data: Union[str, bytes], style: str) -> Dict[str, Any]:
    """
    Unified entry point.

    Automatically detects:
    - TEXT → formatter pipeline
    - FILE → full pipeline
    """

    # TEXT INPUT → FORMATTER
    if isinstance(input_data, str):
        return run_formatter_pipeline(input_data, style)

    # FILE INPUT → FULL ANALYSIS
    if isinstance(input_data, (bytes, bytearray)):
        return run_full_pipeline(input_data, style)

    return {
        "error": "Unsupported input type"
    }
