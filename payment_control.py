"""Persistent global payment-access control for the developer console."""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict


VALID_MODES = {"payment_required", "open_access", "maintenance"}
SETTING_KEY = "global_access_mode"
CACHE_KEY = "citeintegrity:settings:global_access_mode"
EXPIRY_KEY = "global_access_expires_at"
MAINTENANCE_CHECK_KEY = "maintenance_check_at"
MAINTENANCE_MESSAGE_KEY = "maintenance_message"


def default_mode() -> str:
    value = os.getenv("GLOBAL_ACCESS_MODE", "payment_required").strip().lower()
    return value if value in VALID_MODES else "payment_required"


def ensure_settings_table(database_url: str | None) -> None:
    if not database_url:
        return
    import psycopg2
    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS service_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_by TEXT
                )
            """)


def get_access_mode(database_url: str | None = None, redis_conn: Any = None) -> Dict[str, Any]:
    if database_url:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            ensure_settings_table(database_url)
            with psycopg2.connect(database_url, cursor_factory=RealDictCursor) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT setting_key, setting_value, updated_at, updated_by FROM service_settings WHERE setting_key IN (%s, %s, %s, %s)", (SETTING_KEY, EXPIRY_KEY, MAINTENANCE_CHECK_KEY, MAINTENANCE_MESSAGE_KEY))
                    setting_rows = cursor.fetchall()
                    values = {r["setting_key"]: r for r in setting_rows}
                    row = values.get(SETTING_KEY)
                    if row and row["setting_value"] in VALID_MODES:
                        mode = row["setting_value"]
                        expiry_row = values.get(EXPIRY_KEY)
                        expires_at = expiry_row.get("setting_value") if expiry_row else None
                        expired = False
                        if mode == "open_access" and expires_at:
                            try:
                                expiry_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                                expired = expiry_dt <= datetime.now(timezone.utc)
                            except Exception:
                                expired = True
                        if mode == "open_access" and (not expires_at or expired):
                            cursor.execute("UPDATE service_settings SET setting_value='payment_required', updated_at=NOW(), updated_by='automatic_expiry' WHERE setting_key=%s", (SETTING_KEY,))
                            conn.commit()
                            mode = "payment_required"
                            if redis_conn:
                                redis_conn.delete(CACHE_KEY)
                            return {"mode": mode, "source": "automatic_expiry", "open_access_expired": True, "expired_at": expires_at}
                        if redis_conn:
                            redis_conn.setex(CACHE_KEY, 60, mode)
                        remaining_seconds = None
                        if mode == "open_access" and expires_at:
                            remaining_seconds = max(0, int((datetime.fromisoformat(expires_at.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()))
                        maintenance_check_at = (values.get(MAINTENANCE_CHECK_KEY) or {}).get("setting_value") or None
                        maintenance_message = (values.get(MAINTENANCE_MESSAGE_KEY) or {}).get("setting_value") or "CiteIntegrity is undergoing scheduled maintenance while an upgrade is tested."
                        return {"mode": mode, "source": "saved_setting", "updated_at": row.get("updated_at"), "updated_by": row.get("updated_by"), "expires_at": expires_at, "remaining_seconds": remaining_seconds, "maintenance_check_at": maintenance_check_at, "maintenance_message": maintenance_message}
        except Exception as exc:
            return {"mode": default_mode(), "source": "environment_fallback", "warning": str(exc)[:180]}
    return {"mode": default_mode(), "source": "environment"}


def set_access_mode(mode: str, updated_by: str, database_url: str | None = None, redis_conn: Any = None, duration_value: int | None = None, duration_unit: str = "hours", maintenance_message: str = "") -> Dict[str, Any]:
    mode = str(mode or "").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError("Mode must be payment_required, open_access, or maintenance.")
    if not database_url:
        raise RuntimeError("Persistent developer settings require DATABASE_URL.")
    expires_at = None
    check_at = None
    if mode in {"open_access", "maintenance"}:
        try:
            duration_value = int(duration_value or 0)
        except Exception:
            duration_value = 0
        duration_unit = str(duration_unit or "hours").lower()
        if duration_value < 1 or duration_value > 365:
            raise ValueError("Duration must be between 1 and 365 units.")
        if duration_unit not in {"hours", "days", "weeks"}:
            raise ValueError("Duration unit must be hours, days, or weeks.")
        delta = {"hours": timedelta(hours=duration_value), "days": timedelta(days=duration_value), "weeks": timedelta(weeks=duration_value)}[duration_unit]
        if mode == "open_access":
            expires_at = datetime.now(timezone.utc) + delta
        else:
            check_at = datetime.now(timezone.utc) + delta
    import psycopg2
    ensure_settings_table(database_url)
    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO service_settings(setting_key, setting_value, updated_at, updated_by)
                VALUES (%s, %s, NOW(), %s)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value=EXCLUDED.setting_value,
                    updated_at=NOW(), updated_by=EXCLUDED.updated_by
            """, (SETTING_KEY, mode, updated_by))
            cursor.execute("""
                INSERT INTO service_settings(setting_key, setting_value, updated_at, updated_by)
                VALUES (%s, %s, NOW(), %s)
                ON CONFLICT(setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value, updated_at=NOW(), updated_by=EXCLUDED.updated_by
            """, (EXPIRY_KEY, expires_at.isoformat() if expires_at else "", updated_by))
            for key, value in (
                (MAINTENANCE_CHECK_KEY, check_at.isoformat() if check_at else ""),
                (MAINTENANCE_MESSAGE_KEY, (maintenance_message or "CiteIntegrity is undergoing scheduled maintenance while an upgrade is tested.")[:500] if mode == "maintenance" else ""),
            ):
                cursor.execute("""
                    INSERT INTO service_settings(setting_key, setting_value, updated_at, updated_by)
                    VALUES (%s, %s, NOW(), %s)
                    ON CONFLICT(setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value, updated_at=NOW(), updated_by=EXCLUDED.updated_by
                """, (key, value, updated_by))
    if redis_conn:
        redis_conn.setex(CACHE_KEY, 60, mode)
    return {"mode": mode, "updated_by": updated_by, "updated_at": datetime.now(timezone.utc).isoformat(), "expires_at": expires_at.isoformat() if expires_at else None, "maintenance_check_at": check_at.isoformat() if check_at else None, "maintenance_message": maintenance_message if mode == "maintenance" else None, "duration_value": duration_value if mode in {"open_access", "maintenance"} else None, "duration_unit": duration_unit if mode in {"open_access", "maintenance"} else None, "persisted": True}


def open_access_payload() -> Dict[str, Any]:
    return {
        "paid": True,
        "global_open_access": True,
        "source": "developer_control",
        "message": "Full Review access is temporarily open to all users by developer setting.",
        "package": {
            "name": "Developer Open Access",
            "document_tier_name": "Full Review, open access",
            "is_paid": True,
            "analysis_runs": 2,
            "validity_days": 90,
        },
    }
