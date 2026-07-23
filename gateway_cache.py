"""Thread-safe SQLite store-and-forward telemetry delivery."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

import requests

_backend_url = ""
_api_key = ""
_local_db = "gateway_cache.db"
_max_rows = 5000
_flush_batch = 10
_flush_lock = threading.Lock()

TEMPORARY_STATUSES = {408, 425, 429}
PERMANENT_STATUSES = {400, 401, 403, 404, 415, 422}


class TelemetryDeliveryError(RuntimeError):
    pass


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_local_db, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def configure(
    backend_url: str, local_db: str = "gateway_cache.db", api_key: str = ""
) -> None:
    global _backend_url, _local_db, _api_key
    _backend_url = backend_url.rstrip("/")
    _local_db = local_db
    _api_key = api_key
    init_local_cache()


def init_local_cache() -> None:
    with _connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                node_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                recorded_at REAL NOT NULL,
                retries INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )"""
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(cache)").fetchall()
        }
        if "event_id" not in columns:
            conn.execute("ALTER TABLE cache ADD COLUMN event_id TEXT")
        if "recorded_at" not in columns:
            conn.execute("ALTER TABLE cache ADD COLUMN recorded_at REAL")
        if "last_error" not in columns:
            conn.execute("ALTER TABLE cache ADD COLUMN last_error TEXT")
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(cache)").fetchall()
        }
        timestamp_source = "timestamp" if "timestamp" in columns else "NULL"
        conn.execute(
            "UPDATE cache SET event_id=COALESCE(event_id,'legacy-'||id), "
            f"recorded_at=COALESCE(recorded_at,{timestamp_source},strftime('%s','now'))"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cache_event_id ON cache(event_id)"
        )


def cache_count() -> int:
    init_local_cache()
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0])


def _envelope(
    node_id: str,
    payload: dict[str, Any],
    recorded_at: float | None,
    event_id: str | None,
) -> dict[str, Any]:
    if "data" in payload:
        data = payload.get("data", {})
        meta = payload.get("meta", {})
    else:  # compatibility with the old gateway call
        data, meta = payload, {}
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "node": node_id,
        "recorded_at": recorded_at if recorded_at is not None else time.time(),
        "data": data,
        "meta": meta,
    }


def save_to_local_cache(
    node_id: str,
    payload: dict[str, Any],
    *,
    recorded_at: float | None = None,
    event_id: str | None = None,
    last_error: str | None = None,
) -> bool:
    envelope = _envelope(node_id, payload, recorded_at, event_id)
    try:
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        print(f"CRITICAL: telemetry JSON serialization failed: {exc}")
        return False
    try:
        init_local_cache()
        with _connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO cache
                   (event_id,node_id,payload,recorded_at,last_error)
                   VALUES (?,?,?,?,?)""",
                (
                    envelope["event_id"],
                    node_id,
                    encoded,
                    envelope["recorded_at"],
                    last_error,
                ),
            )
            total = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            if total > _max_rows:
                lost = total - _max_rows
                conn.execute(
                    "DELETE FROM cache WHERE id IN "
                    "(SELECT id FROM cache ORDER BY recorded_at,id LIMIT ?)",
                    (lost,),
                )
                print(f"CRITICAL: cache limit exceeded; permanently dropped {lost} oldest events")
        return True
    except sqlite3.Error as exc:
        print(f"CRITICAL: SQLite cache write failed: {exc}")
        return False


def _post(envelope: dict[str, Any], timeout: float) -> requests.Response:
    if not _backend_url:
        raise TelemetryDeliveryError("gateway cache backend URL is not configured")
    headers = {"X-API-Key": _api_key} if _api_key else {}
    return requests.post(_backend_url, json=envelope, headers=headers, timeout=timeout)


def flush_local_cache() -> int:
    if not _flush_lock.acquire(blocking=False):
        return 0
    sent = 0
    try:
        init_local_cache()
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id,payload,retries FROM cache ORDER BY recorded_at,id LIMIT ?",
                (_flush_batch,),
            ).fetchall()
        for row_id, encoded, retries in rows:
            try:
                envelope = json.loads(encoded)
            except (TypeError, json.JSONDecodeError) as exc:
                print(f"CRITICAL: dropping corrupt cached JSON row {row_id}: {exc}")
                with _connect() as conn:
                    conn.execute("DELETE FROM cache WHERE id=?", (row_id,))
                continue
            try:
                response = _post(envelope, 2.0)
                status = response.status_code
            except requests.RequestException as exc:
                with _connect() as conn:
                    conn.execute(
                        "UPDATE cache SET retries=retries+1,last_error=? WHERE id=?",
                        (str(exc)[:500], row_id),
                    )
                break
            if 200 <= status < 300:
                with _connect() as conn:
                    conn.execute("DELETE FROM cache WHERE id=?", (row_id,))
                sent += 1
            elif status in PERMANENT_STATUSES:
                print(f"ERROR: permanently rejected cached event row {row_id}: HTTP {status}")
                with _connect() as conn:
                    conn.execute("DELETE FROM cache WHERE id=?", (row_id,))
            else:
                with _connect() as conn:
                    conn.execute(
                        "UPDATE cache SET retries=?,last_error=? WHERE id=?",
                        (retries + 1, f"HTTP {status}", row_id),
                    )
                if status in TEMPORARY_STATUSES or status >= 500:
                    break
        return sent
    finally:
        _flush_lock.release()


def upload_telemetry(
    node_id: str,
    payload: dict[str, Any],
    *,
    recorded_at: float | None = None,
    event_id: str | None = None,
) -> bool:
    envelope = _envelope(node_id, payload, recorded_at, event_id)
    flush_local_cache()
    try:
        response = _post(envelope, 3.0)
        if 200 <= response.status_code < 300:
            return True
        error = f"HTTP {response.status_code}"
    except requests.RequestException as exc:
        error = str(exc)
    if not save_to_local_cache(
        node_id,
        payload,
        recorded_at=envelope["recorded_at"],
        event_id=envelope["event_id"],
        last_error=error,
    ):
        raise TelemetryDeliveryError(
            f"upload failed ({error}) and telemetry could not be cached"
        )
    return False
