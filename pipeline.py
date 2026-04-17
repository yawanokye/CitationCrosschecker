from typing import Dict, Any, Union
from concurrent.futures import ThreadPoolExecutor

from engine import run_crosscheck
from formatter import process_references
from claim_checker import build_claim_support_rows
from acii import compute_acii


def run_formatter_pipeline(raw_reference: str, style: str) -> Dict[str, Any]:
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


def run_full_pipeline(file_bytes: bytes, style: str) -> Dict[str, Any]:
    """
    Fast document analysis only.
    Online verification runs separately in the async verification system.
    """
    result = {}

    try:
        engine_result = run_crosscheck(file_bytes)
        result["engine"] = engine_result

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

        result["online_verification"] = {
            "rows": [],
            "summary": {
                "status": "pending",
                "message": "Verification pending"
            }
        }

        result["claims"] = build_claim_support_rows({
            **engine_result,
            "online_verification": result["online_verification"]
        })

        result["acii"] = compute_acii(engine_result, [])

        result["meta"] = {
            "verification_pending": True,
            "claims_provisional": True,
            "acii_provisional": True
        }

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


def run_pipeline(input_data: Union[str, bytes], style: str) -> Dict[str, Any]:
    if isinstance(input_data, str):
        return run_formatter_pipeline(input_data, style)

    if isinstance(input_data, (bytes, bytearray)):
        return run_full_pipeline(input_data, style)

    return {"error": "Unsupported input type"}
