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
_mirror_backend_url = ""
_api_key = ""
_site_bypass_token = ""
_local_db = "gateway_cache.db"
_max_rows = 5000
_max_dead_letter = 500
_flush_batch = 10
_flush_lock = threading.Lock()
_init_lock = threading.Lock()
_initialized_db: str | None = None

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
    backend_url: str,
    local_db: str = "gateway_cache.db",
    api_key: str = "",
    mirror_backend_url: str = "",
    site_bypass_token: str = "",
) -> None:
    global _backend_url, _mirror_backend_url, _local_db, _api_key, _site_bypass_token
    _backend_url = backend_url.rstrip("/")
    _mirror_backend_url = mirror_backend_url.rstrip("/")
    _local_db = local_db
    _api_key = api_key
    _site_bypass_token = site_bypass_token
    init_local_cache()


def _request_headers(*, mirror: bool = False) -> dict[str, str]:
    headers = {"X-API-Key": _api_key} if _api_key else {}
    if mirror and _site_bypass_token:
        headers["OAI-Sites-Authorization"] = f"Bearer {_site_bypass_token}"
    return headers


def init_local_cache() -> None:
    """Create and migrate the schema once per database path.

    Every cache operation calls this, so it has to be free on the hot path: it
    used to re-run the whole DDL and a full-table backfill on each call, ~21ms
    with the cache at its 5000-row cap — paid on the serial reader thread
    whenever the upload queue was full. The guard is keyed on ``_local_db`` so
    ``configure`` pointing at another database still re-runs it.
    """
    global _initialized_db
    if _initialized_db == _local_db:
        return
    with _init_lock:
        if _initialized_db == _local_db:  # another thread won the race
            return
        _init_schema()
        _initialized_db = _local_db


def _invalidate_schema_cache() -> None:
    """Re-run the schema check after a SQLite failure.

    Without this the guard above would turn a recoverable state — the database
    file deleted or replaced under a running gateway — into a permanent one.
    """
    global _initialized_db
    _initialized_db = None


def _init_schema() -> None:
    with _connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                node_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                recorded_at REAL NOT NULL,
                retries INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                dead_letter INTEGER NOT NULL DEFAULT 0
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
        if "dead_letter" not in columns:
            conn.execute(
                "ALTER TABLE cache ADD COLUMN dead_letter INTEGER NOT NULL DEFAULT 0"
            )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(cache)").fetchall()
        }
        timestamp_source = "timestamp" if "timestamp" in columns else "NULL"
        # Filtered, not blanket: without the WHERE this rewrote every row on
        # every call even when there was nothing left to backfill.
        conn.execute(
            "UPDATE cache SET event_id=COALESCE(event_id,'legacy-'||id), "
            f"recorded_at=COALESCE(recorded_at,{timestamp_source},strftime('%s','now')) "
            "WHERE event_id IS NULL OR recorded_at IS NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cache_event_id ON cache(event_id)"
        )


def cache_count() -> int:
    init_local_cache()
    with _connect() as conn:
        return int(
            conn.execute("SELECT COUNT(*) FROM cache WHERE dead_letter=0").fetchone()[0]
        )


def dead_letter_count() -> int:
    """Number of permanently rejected/corrupt events retained for diagnosis."""
    init_local_cache()
    with _connect() as conn:
        return int(
            conn.execute("SELECT COUNT(*) FROM cache WHERE dead_letter=1").fetchone()[0]
        )


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
            # Cap pending and quarantined rows separately: poison in the
            # dead-letter table must never count against, or evict, telemetry
            # that is still waiting to be delivered.
            live = conn.execute(
                "SELECT COUNT(*) FROM cache WHERE dead_letter=0"
            ).fetchone()[0]
            if live > _max_rows:
                lost = live - _max_rows
                conn.execute(
                    "DELETE FROM cache WHERE id IN "
                    "(SELECT id FROM cache WHERE dead_letter=0 ORDER BY recorded_at,id LIMIT ?)",
                    (lost,),
                )
                print(f"CRITICAL: cache limit exceeded; permanently dropped {lost} oldest pending events")
            dead = conn.execute(
                "SELECT COUNT(*) FROM cache WHERE dead_letter=1"
            ).fetchone()[0]
            if dead > _max_dead_letter:
                drop = dead - _max_dead_letter
                conn.execute(
                    "DELETE FROM cache WHERE id IN "
                    "(SELECT id FROM cache WHERE dead_letter=1 ORDER BY recorded_at,id LIMIT ?)",
                    (drop,),
                )
                print(f"WARNING: dead-letter limit exceeded; dropped {drop} oldest quarantined events")
        return True
    except sqlite3.Error as exc:
        _invalidate_schema_cache()
        print(f"CRITICAL: SQLite cache write failed: {exc}")
        return False


def _post(envelope: dict[str, Any], timeout: float) -> requests.Response:
    if not _backend_url:
        raise TelemetryDeliveryError("gateway cache backend URL is not configured")
    primary = requests.post(
        _backend_url, json=envelope, headers=_request_headers(), timeout=timeout
    )
    if not 200 <= primary.status_code < 300 or not _mirror_backend_url:
        return primary
    mirror = requests.post(
        _mirror_backend_url,
        json=envelope,
        headers=_request_headers(mirror=True),
        timeout=timeout,
    )
    if not 200 <= mirror.status_code < 300:
        # A mirror configuration/authentication problem must not make us drop
        # data that Render already accepted. The stable event_id makes retries
        # idempotent on both backends.
        raise requests.HTTPError(
            f"mirror backend rejected telemetry: HTTP {mirror.status_code}"
        )
    return primary


def flush_local_cache() -> int:
    if not _flush_lock.acquire(blocking=False):
        return 0
    sent = 0
    try:
        init_local_cache()
        with _connect() as conn:
            rows = conn.execute(
                """SELECT id,payload,retries FROM cache
                   WHERE dead_letter=0 ORDER BY recorded_at,id LIMIT ?""",
                (_flush_batch,),
            ).fetchall()
        for row_id, encoded, retries in rows:
            try:
                envelope = json.loads(encoded)
            except (TypeError, json.JSONDecodeError) as exc:
                print(f"CRITICAL: quarantining corrupt cached JSON row {row_id}: {exc}")
                with _connect() as conn:
                    conn.execute(
                        "UPDATE cache SET dead_letter=1,last_error=? WHERE id=?",
                        (f"invalid JSON: {exc}"[:500], row_id),
                    )
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
                # Per-packet, not per-batch: the console is how an operator sees
                # a specific mcount make it out, which a count would hide.
                print(
                    f"UPLOADED: node={envelope.get('node')} "
                    f"mcount={envelope.get('meta', {}).get('mcount', '?')}"
                )
            elif status in PERMANENT_STATUSES:
                print(
                    f"ERROR: quarantining permanently rejected event row "
                    f"{row_id}: HTTP {status}"
                )
                with _connect() as conn:
                    conn.execute(
                        "UPDATE cache SET dead_letter=1,last_error=? WHERE id=?",
                        (f"HTTP {status}", row_id),
                    )
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
