"""Publication-event verification for CiteIntegrity.

The verifier deliberately keeps bibliographic matching separate from publication
status. A DOI can be a strong metadata match and still have a retraction,
expression of concern, correction, or reinstatement in its history.

Crossref exposes Retraction Watch and publisher-supplied events in ``update-to``.
This module preserves the full event history and never treats a failed lookup as
evidence that a work is clear.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


PUBLICATION_STATUS_CHECK_ENABLED = os.getenv(
    "PUBLICATION_STATUS_CHECK_ENABLED", "true"
).strip().lower() not in {"0", "false", "no", "off"}
PUBLICATION_STATUS_TIMEOUT = float(os.getenv("PUBLICATION_STATUS_TIMEOUT", "8") or "8")
PUBLICATION_STATUS_CACHE_TTL = int(
    os.getenv("PUBLICATION_STATUS_CACHE_TTL", "21600") or "21600"
)
CROSSREF_API_BASE = os.getenv("CROSSREF_API_BASE", "https://api.crossref.org").rstrip("/")
CROSSREF_MAILTO = (
    os.getenv("CITATION_CROSSCHECKER_MAILTO")
    or os.getenv("CROSSREF_MAILTO")
    or ""
).strip()
_RETRACTION_WATCH_DB_ENV = os.getenv("RETRACTION_WATCH_DB_PATH", "").strip()
_BUNDLED_RETRACTION_WATCH_DB = Path(__file__).resolve().parent / "data" / "retraction_watch.sqlite3"
_LEGACY_RETRACTION_WATCH_DB = Path(__file__).resolve().parent / "retraction_watch.sqlite3"
RETRACTION_WATCH_DB_PATH = Path(
    _RETRACTION_WATCH_DB_ENV
    or str(
        _BUNDLED_RETRACTION_WATCH_DB
        if _BUNDLED_RETRACTION_WATCH_DB.is_file()
        else _LEGACY_RETRACTION_WATCH_DB
    )
)

_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
_NOTICE_TITLE_RE = re.compile(
    r"^\s*(?:retraction|withdrawal|expression\s+of\s+concern|editorial\s+expression\s+of\s+concern|"
    r"correction|corrigendum|erratum|reinstatement|notice\s+of\s+retraction)\s*[:\-]",
    re.I,
)

_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def normalise_doi(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^doi\s*:\s*", "", text, flags=re.I)
    match = _DOI_RE.search(text)
    if not match:
        return ""
    return match.group(0).rstrip(".,;:)]}").lower()


def extract_doi(text: Any) -> str:
    return normalise_doi(text)


def _first_title(message: Dict[str, Any]) -> str:
    title = message.get("title") or ""
    if isinstance(title, list):
        title = title[0] if title else ""
    return str(title or "").strip()


def _date_value(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("date-time"):
            return str(value["date-time"])
        parts = value.get("date-parts") or []
        if parts and isinstance(parts[0], list):
            nums = [str(x) for x in parts[0] if x is not None]
            if nums:
                return "-".join(nums)
    return str(value or "").strip()


def _event_type(value: Any, label: Any = "") -> str:
    raw = f"{value or ''} {label or ''}".strip().lower()
    raw = raw.replace("_", " ").replace("-", " ")
    if "reinstate" in raw:
        return "reinstatement"
    if "expression" in raw and "concern" in raw:
        return "expression_of_concern"
    if "retract" in raw:
        return "retraction"
    if "withdraw" in raw:
        return "withdrawal"
    if any(token in raw for token in ("correction", "corrigendum", "erratum")):
        return "correction"
    if "update" in raw or "supersed" in raw:
        return "other_update"
    return "other_update"


def _event_label(event_type: str) -> str:
    return {
        "retraction": "Retraction",
        "withdrawal": "Withdrawal",
        "expression_of_concern": "Expression of concern",
        "correction": "Correction",
        "reinstatement": "Reinstatement",
        "other_update": "Publication update",
    }.get(event_type, "Publication update")


def _normalise_event(item: Dict[str, Any], relation_key: str = "") -> Dict[str, Any]:
    event_type = _event_type(
        item.get("type") or item.get("subtype") or relation_key,
        item.get("label"),
    )
    doi = normalise_doi(item.get("DOI") or item.get("doi") or item.get("id"))
    url = str(item.get("URL") or item.get("url") or "").strip()
    if not url and doi:
        url = f"https://doi.org/{doi}"
    return {
        "type": event_type,
        "label": str(item.get("label") or _event_label(event_type)).strip(),
        "doi": doi,
        "date": _date_value(item.get("updated") or item.get("published") or item.get("created")),
        "source": str(item.get("source") or "crossref").strip().lower(),
        "url": url,
    }


def events_from_crossref_message(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return all publication events attached to a Crossref work."""
    if not isinstance(message, dict):
        return []

    events: List[Dict[str, Any]] = []
    for item in message.get("update-to") or []:
        if isinstance(item, dict):
            events.append(_normalise_event(item, "update-to"))

    relation = message.get("relation") or {}
    if isinstance(relation, dict):
        relation_map = {
            "is-retracted-by": "retraction",
            "retracts": "retraction",
            "is-corrected-by": "correction",
            "corrects": "correction",
            "is-updated-by": "other_update",
            "updates": "other_update",
            "is-superseded-by": "other_update",
        }
        for key, values in relation.items():
            if key not in relation_map:
                continue
            if not isinstance(values, list):
                values = [values]
            for raw in values:
                item = dict(raw) if isinstance(raw, dict) else {"id": raw}
                item.setdefault("type", relation_map[key])
                events.append(_normalise_event(item, key))

    deduped: List[Dict[str, Any]] = []
    seen = set()
    for event in events:
        key = (event["type"], event["doi"], event["date"], event["label"].lower())
        if key not in seen:
            seen.add(key)
            deduped.append(event)
    return deduped


def is_publication_notice(message: Dict[str, Any]) -> bool:
    title = _first_title(message)
    subtype = str(message.get("subtype") or message.get("type") or "").lower()
    return bool(_NOTICE_TITLE_RE.search(title)) or subtype in {
        "retraction", "withdrawal", "expression-of-concern", "correction",
        "corrigendum", "erratum", "reinstatement",
    }


def _event_sort_key(event: Dict[str, Any]) -> Tuple[str, int]:
    event_type = str(event.get("type") or "")
    order = {
        "expression_of_concern": 1,
        "correction": 2,
        "retraction": 3,
        "withdrawal": 3,
        "reinstatement": 4,
        "other_update": 5,
    }.get(event_type, 9)
    return (str(event.get("date") or "9999"), order)


def summarise_publication_events(
    events: Iterable[Dict[str, Any]], is_notice: bool = False
) -> Dict[str, Any]:
    ordered = sorted([dict(x) for x in events if isinstance(x, dict)], key=_event_sort_key)
    types = [str(x.get("type") or "other_update") for x in ordered]

    if is_notice:
        status = "publication_notice"
        severity = "information"
        reason = "This record is a publication notice. Citing the notice itself is legitimate."
    else:
        active_status = "clear"
        for event_type in types:
            if event_type in {"retraction", "withdrawal"}:
                active_status = "retracted" if event_type == "retraction" else "withdrawn"
            elif event_type == "reinstatement":
                active_status = "reinstated"
            elif event_type == "expression_of_concern" and active_status == "clear":
                active_status = "expression_of_concern"
            elif event_type == "correction" and active_status == "clear":
                active_status = "corrected"
        status = active_status
        severity = {
            "retracted": "critical",
            "withdrawn": "critical",
            "expression_of_concern": "warning",
            "corrected": "information",
            "reinstated": "information",
            "clear": "clear",
        }[status]
        reason = {
            "retracted": "A retraction event is active for this publication.",
            "withdrawn": "A withdrawal event is active for this publication.",
            "expression_of_concern": "An expression of concern is active for this publication.",
            "corrected": "A correction is recorded for this publication.",
            "reinstated": "This publication has a reinstatement after an earlier event and must not be shown as actively retracted.",
            "clear": "No Crossref publication event was returned for this DOI.",
        }[status]

    return {
        "publication_status": status,
        "publication_status_severity": severity,
        "publication_status_reason": reason,
        "publication_events": ordered,
        "publication_event_count": len(ordered),
        "publication_event_types": list(dict.fromkeys(types)),
        "work_role": "publication_notice" if is_notice else "research_work",
        "is_retracted": status in {"retracted", "withdrawn"},
        "is_withdrawn": status == "withdrawn",
        "expression_of_concern": status == "expression_of_concern",
        "is_corrected": "correction" in types,
        "is_reinstated": status == "reinstated",
    }


def _retraction_watch_lookup(doi: str) -> Dict[str, Any]:
    """Query the compact index built from Crossref's daily RW dataset."""
    doi = normalise_doi(doi)
    if not doi:
        return {"available": False, "events": [], "is_notice": False, "metadata": {}}
    if not RETRACTION_WATCH_DB_PATH.is_file():
        return {"available": False, "events": [], "is_notice": False, "metadata": {}}
    try:
        uri = f"file:{RETRACTION_WATCH_DB_PATH}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT event_type, label, notice_doi, event_date, source, record_id "
            "FROM events WHERE original_doi = ? ORDER BY event_date, record_id",
            (doi,),
        ).fetchall()
        is_notice = connection.execute(
            "SELECT 1 FROM notices WHERE notice_doi = ? LIMIT 1", (doi,)
        ).fetchone() is not None
        metadata = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM metadata").fetchall()
        }
        connection.close()
        events = [{
            "type": row["event_type"],
            "label": row["label"],
            "doi": row["notice_doi"],
            "date": row["event_date"],
            "source": row["source"],
            "url": f"https://doi.org/{row['notice_doi']}" if row["notice_doi"] else "",
            "record_id": row["record_id"],
        } for row in rows]
        return {
            "available": True,
            "events": events,
            "is_notice": is_notice,
            "metadata": metadata,
        }
    except Exception as exc:
        return {
            "available": False,
            "events": [],
            "is_notice": False,
            "metadata": {},
            "reason": f"Retraction Watch index lookup failed: {exc}",
        }


def _cache_get(doi: str) -> Optional[Dict[str, Any]]:
    with _CACHE_LOCK:
        item = _CACHE.get(doi)
        if not item:
            return None
        stored_at, value = item
        if time.time() - stored_at > PUBLICATION_STATUS_CACHE_TTL:
            _CACHE.pop(doi, None)
            return None
        return deepcopy(value)


def _cache_set(doi: str, value: Dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _CACHE[doi] = (time.time(), deepcopy(value))


def fetch_crossref_publication_status(
    doi: str,
    candidate_events: Optional[Iterable[Dict[str, Any]]] = None,
    candidate_is_notice: bool = False,
) -> Dict[str, Any]:
    doi = normalise_doi(doi)
    if not doi:
        return {
            "checked": False,
            "source": "crossref",
            "reason": "No DOI was available for publication-status verification.",
        }
    candidate_events = [dict(item) for item in (candidate_events or []) if isinstance(item, dict)]
    cached = _cache_get(doi)
    if cached is not None and not candidate_events and not candidate_is_notice:
        cached["cache_hit"] = True
        return cached

    headers = {
        "User-Agent": "CiteIntegrity/1.6 publication-integrity-check"
        + (f" (mailto:{CROSSREF_MAILTO})" if CROSSREF_MAILTO else "")
    }
    rw = _retraction_watch_lookup(doi)
    crossref_message: Dict[str, Any] = {}
    crossref_error = ""
    # The DOI-indexed Retraction Watch snapshot is the complete event source.
    # Avoid a second Crossref call when it is present. Publisher update-to data
    # already returned during bibliographic matching is supplied as candidate
    # events by the verifier.
    try:
        if rw.get("available"):
            raise LookupError("local_retraction_watch_index_available")
        url = f"{CROSSREF_API_BASE}/works/{quote(doi, safe='')}"
        if CROSSREF_MAILTO:
            url = f"{url}?{urlencode({'mailto': CROSSREF_MAILTO})}"
        request = Request(url, headers=headers)
        with urlopen(request, timeout=PUBLICATION_STATUS_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        crossref_message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(crossref_message, dict):
            raise ValueError("Crossref returned no work metadata")
    except LookupError as exc:
        if str(exc) != "local_retraction_watch_index_available":
            crossref_error = str(exc)
        crossref_message = {}
    except Exception as exc:
        crossref_message = {}
        crossref_error = str(exc)

    crossref_events = events_from_crossref_message(crossref_message)
    all_events = list(rw.get("events") or []) + candidate_events + crossref_events
    deduped_events = []
    seen_events = set()
    for event in all_events:
        key = (
            event.get("type"), event.get("doi"), event.get("date"),
            str(event.get("label") or "").lower(),
        )
        if key not in seen_events:
            seen_events.add(key)
            deduped_events.append(event)

    # A DOI used as a notice DOI is a legitimate publication notice even when
    # the notice later receives its own correction or update.
    notice_role = bool(rw.get("is_notice")) or bool(candidate_is_notice) or (
        bool(crossref_message) and is_publication_notice(crossref_message)
    )
    complete_source_checked = bool(rw.get("available"))
    if complete_source_checked or deduped_events:
        summary = summarise_publication_events(deduped_events, notice_role)
        result = {
            "checked": True,
            "source": "retraction_watch_crossref" if rw.get("available") else "crossref_update_to",
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "doi": doi,
            "data_version": (rw.get("metadata") or {}).get("dataset_date") or (rw.get("metadata") or {}).get("generated_at") or "",
            **summary,
        }
        _cache_set(doi, result)
        return result
    reason_parts = []
    if not rw.get("available"):
        reason_parts.append(rw.get("reason") or "Retraction Watch index is unavailable")
    if crossref_error:
        reason_parts.append(f"Crossref lookup failed: {crossref_error}")
    else:
        reason_parts.append("Crossref returned no event, which is insufficient without the Retraction Watch index")
    return {
        "checked": False,
        "source": "retraction_watch_crossref",
        "doi": doi,
        "reason": "; ".join(reason_parts),
    }


def enrich_verification_row(row: Dict[str, Any], reference: str = "") -> Dict[str, Any]:
    enriched = dict(row or {})
    enriched.setdefault("reference", reference)
    enriched["publication_status_check_enabled"] = bool(PUBLICATION_STATUS_CHECK_ENABLED)

    if not PUBLICATION_STATUS_CHECK_ENABLED:
        enriched.update({
            "publication_status_checked": False,
            "publication_status": "unchecked",
            "publication_status_source": "disabled",
            "publication_status_reason": "Publication-status verification is disabled by configuration.",
        })
        return enriched

    input_doi = extract_doi(reference) or normalise_doi(
        enriched.get("reference_doi") or enriched.get("input_doi")
    )
    matched_doi = normalise_doi(enriched.get("doi") or enriched.get("matched_doi"))
    match_status = str(enriched.get("status") or "").lower()
    lookup_doi = input_doi or (matched_doi if match_status in {"verified", "likely"} else "")

    if not lookup_doi:
        enriched.update({
            "publication_status_checked": False,
            "publication_status": "unchecked",
            "publication_status_source": "crossref",
            "publication_status_reason": "No confirmed DOI was available for publication-status verification.",
            "publication_events": [],
            "publication_event_count": 0,
            "publication_event_types": [],
        })
        return enriched

    status = fetch_crossref_publication_status(
        lookup_doi,
        candidate_events=enriched.get("candidate_publication_events") or [],
        candidate_is_notice=bool(enriched.get("candidate_is_publication_notice")),
    )
    enriched["publication_status_checked"] = bool(status.get("checked"))
    enriched["publication_status_source"] = status.get("source") or "crossref"
    enriched["publication_status_checked_at"] = status.get("checked_at")
    enriched["publication_status_data_version"] = status.get("data_version")
    enriched["publication_status_doi"] = lookup_doi
    if status.get("checked"):
        for key in (
            "publication_status", "publication_status_severity", "publication_status_reason",
            "publication_events", "publication_event_count", "publication_event_types",
            "work_role", "is_retracted", "is_withdrawn", "expression_of_concern",
            "is_corrected", "is_reinstated",
        ):
            enriched[key] = deepcopy(status.get(key))
    else:
        enriched.update({
            "publication_status": "unchecked",
            "publication_status_reason": status.get("reason") or "Publication status was not checked.",
            "publication_events": [],
            "publication_event_count": 0,
            "publication_event_types": [],
        })
    return enriched
