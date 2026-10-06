"""Account for every extracted reference before preview sampling or completion."""

import re
from collections import defaultdict, deque

from reference_review import reference_resolution_counts


def _reference_key(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    superscript = re.match(r"^([⁰¹²³⁴⁵⁶⁷⁸⁹]+)\s+", text)
    if superscript:
        number = superscript.group(1).translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789"))
        text = number + ". " + text[superscript.end():]
    # Verification normalises Word's numeric markers. Preserve duplicate entries
    # as separate occurrences even when their normalised text is identical.
    return re.sub(r"^\s*(?:\[\d+\][.)]?|\(\d+\)[.]?|\d+[.)])\s*", "", text)


def _as_count(value):
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError):
        return 0


def _ordered_results(references, rows):
    """Match results to input occurrences, never to a filtered row's position."""
    valid = [row for row in rows if isinstance(row, dict) and row.get("status")]
    keyed = defaultdict(deque)
    for row in valid:
        key = _reference_key(row.get("original_reference") or row.get("reference"))
        if key:
            keyed[key].append(row)
    positional = len(valid) == len(references) and not keyed
    return [
        dict(keyed[key].popleft()) if keyed[key] else
        dict(valid[i]) if positional else None
        for i, key in enumerate(map(_reference_key, references))
    ]


def ensure_reference_outcomes(references, rows, start_index=0):
    """Keep missing provider results visible as failures, with no match claim."""
    outcomes = _ordered_results(references, rows or [])
    for i, (reference, row) in enumerate(zip(references, outcomes)):
        if row is None:
            row = {
                "status": "offline", "source": "worker_missing_result",
                "verification_attempted": False, "missing_verification_result": True,
                "publication_status": "unchecked", "publication_status_checked": False,
                "message": "No usable verification result was saved for this reference. Retry verification.",
                "score": 0,
            }
        row.update(reference=reference, original_reference=reference,
                   reference_index=start_index + i + 1)
        outcomes[i] = row
    return outcomes


def verification_coverage(result):
    """Full counts, calculated on saved rows before paid/free display limits."""
    references = [str(ref).strip() for ref in result.get("references_raw") or [] if str(ref or "").strip()]
    online = result.get("online_verification") or {}
    rows = online.get("rows") or [] if isinstance(online, dict) else online if isinstance(online, list) else []
    meta = result.get("verification") or {}
    summary = result.get("summary") or {}
    expected = max(len(references), _as_count(meta.get("total")),
                   _as_count(summary.get("reference_entries_found")))
    ordered = _ordered_results(references, rows) if references else [
        row for row in rows if isinstance(row, dict) and row.get("status")]
    outcomes = [row for row in ordered if row is not None]
    if not expected:
        expected = len(outcomes)
    processed = [row for row in outcomes if row.get("verification_attempted") is not False]
    counts = reference_resolution_counts(outcomes)
    pending = max(0, expected - len(processed))
    return {
        "expected": expected, "extracted": len(references), "result_rows": len(outcomes),
        "processed": len(processed), "pending": pending,
        "matched": counts["verified"] + counts["metadata_differences"],
        "complete": expected > 0 and pending == 0,
        "extraction_count_mismatch": bool(references and len(references) < expected),
        "outcomes": counts,
    }


def reconcile_verification_meta(result):
    """Do not infer completion from the mere presence of a partial result."""
    coverage = verification_coverage(result)
    meta = dict(result.get("verification") or {})
    meta.update(total=coverage["expected"], progress=coverage["processed"],
                results_count=coverage["result_rows"],
                percentage=int(100 * coverage["processed"] / coverage["expected"]) if coverage["expected"] else 0)
    if meta.get("state") == "completed" and coverage["expected"] and not coverage["complete"]:
        meta.update(state="incomplete", message=(
            f"Verification incomplete: {coverage['processed']}/{coverage['expected']} references processed. "
            "Retry verification for missing results."))
    result["verification"] = meta
    result["verification_coverage"] = coverage
    return meta
