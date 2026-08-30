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
_max_dead_letter = 500
_flush_batch = 10
_flush_lock = threading.Lock()
_ack_flush_lock = threading.Lock()
_init_lock = threading.Lock()
_initialized_db: str | None = None

TEMPORARY_STATUSES = {408, 425, 429}
PERMANENT_STATUSES = {400, 401, 403, 404, 415, 422}
ACK_PERMANENT_STATUSES = {400, 404, 415, 422}


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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS command_ack_cache (
                cmd_id TEXT PRIMARY KEY,
                node TEXT NOT NULL,
                result TEXT NOT NULL,
                rssi INTEGER,
                snr REAL,
                created_at REAL NOT NULL,
                last_error TEXT,
                retries INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0
            )"""
        )
        ack_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(command_ack_cache)").fetchall()
        }
        if "retries" not in ack_columns:
            conn.execute(
                "ALTER TABLE command_ack_cache ADD COLUMN retries INTEGER NOT NULL DEFAULT 0"
            )
        if "next_attempt" not in ack_columns:
            conn.execute(
                "ALTER TABLE command_ack_cache ADD COLUMN next_attempt REAL NOT NULL DEFAULT 0"
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
    headers = {"X-API-Key": _api_key} if _api_key else {}
    return requests.post(_backend_url, json=envelope, headers=headers, timeout=timeout)


def _api_base() -> str:
    if _backend_url.endswith("/update"):
        return _backend_url[: -len("/update")]
    return _backend_url


def fetch_pending_commands(timeout: float = 3.0) -> list[dict[str, Any]]:
    """Claim queued downlink commands from the backend for delivery over serial."""
    if not _backend_url:
        return []
    headers = {"X-API-Key": _api_key} if _api_key else {}
    try:
        response = requests.get(
            f"{_api_base()}/api/commands/pending", headers=headers, timeout=timeout
        )
        if response.status_code == 200:
            commands = response.json().get("commands", [])
            if isinstance(commands, list):
                return commands
    except (requests.RequestException, ValueError) as exc:
        print(f"WARNING: command poll failed: {exc}")
    return []


def report_command_ack(
    node: str,
    cmd_id: str,
    result: str,
    *,
    rssi: int | None = None,
    snr: float | None = None,
    timeout: float = 3.0,
) -> bool:
    if not _backend_url:
        _save_command_ack(node, cmd_id, result, rssi, snr, "backend not configured")
        return False
    headers = {"X-API-Key": _api_key} if _api_key else {}
    body: dict[str, Any] = {"node": node, "cmd_id": cmd_id, "result": result}
    if rssi is not None:
        body["rssi"] = rssi
    if snr is not None:
        body["snr"] = snr
    try:
        response = requests.post(
            f"{_api_base()}/api/commands/ack", json=body, headers=headers, timeout=timeout
        )
        if 200 <= response.status_code < 300:
            _delete_command_ack(cmd_id)
            return True
        error = f"HTTP {response.status_code}"
        if response.status_code in ACK_PERMANENT_STATUSES:
            print(f"ERROR: permanently rejected command ACK {cmd_id}: {error}")
            _delete_command_ack(cmd_id)
            return False
    except requests.RequestException as exc:
        error = str(exc)
    print(f"WARNING: command ACK report failed: {error}")
    _save_command_ack(node, cmd_id, result, rssi, snr, error)
    return False


def save_command_ack_for_retry(
    node: str,
    cmd_id: str,
    result: str,
    *,
    rssi: int | None = None,
    snr: float | None = None,
) -> None:
    """Persist an ACK for the background poller without spending an attempt.

    For callers that must not block on the network — the serial reader — this
    makes the acknowledgement durable now and leaves delivery to
    ``flush_command_acks``, due immediately rather than after a retry backoff.
    """
    _save_command_ack(
        node, cmd_id, result, rssi, snr, "queued for delivery", count_attempt=False
    )


def _save_command_ack(
    node: str,
    cmd_id: str,
    result: str,
    rssi: int | None,
    snr: float | None,
    error: str,
    *,
    count_attempt: bool = True,
) -> None:
    try:
        init_local_cache()
        with _connect() as conn:
            row = conn.execute(
                "SELECT retries FROM command_ack_cache WHERE cmd_id=?", (cmd_id,)
            ).fetchone()
            previous = int(row[0]) if row else 0
            # A queued-but-never-tried ACK has not failed at anything, so it
            # neither burns a retry nor waits out a backoff it did not earn.
            retries = previous + 1 if count_attempt else previous
            next_attempt = (
                time.time() + min(300.0, 5.0 * (2 ** min(retries - 1, 6)))
                if count_attempt
                else 0.0  # due now, not "now + 0": never race the flush clock
            )
            conn.execute(
                """INSERT INTO command_ack_cache
                   (cmd_id,node,result,rssi,snr,created_at,last_error,retries,next_attempt)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cmd_id) DO UPDATE SET
                     node=excluded.node,result=excluded.result,rssi=excluded.rssi,
                     snr=excluded.snr,last_error=excluded.last_error,
                     retries=excluded.retries,next_attempt=excluded.next_attempt""",
                (
                    cmd_id, node, result, rssi, snr, time.time(), error[:500],
                    retries, next_attempt,
                ),
            )
            conn.execute(
                """DELETE FROM command_ack_cache WHERE cmd_id IN
                   (SELECT cmd_id FROM command_ack_cache
                    ORDER BY created_at DESC LIMIT -1 OFFSET 500)"""
            )
    except sqlite3.Error as exc:
        _invalidate_schema_cache()
        print(f"CRITICAL: command ACK cache write failed: {exc}")


def _delete_command_ack(cmd_id: str) -> None:
    try:
        init_local_cache()
        with _connect() as conn:
            conn.execute("DELETE FROM command_ack_cache WHERE cmd_id=?", (cmd_id,))
    except sqlite3.Error as exc:
        _invalidate_schema_cache()
        print(f"WARNING: command ACK cache cleanup failed: {exc}")


def flush_command_acks() -> int:
    """Retry persisted node acknowledgements; keep failures for the next poll."""
    if not _ack_flush_lock.acquire(blocking=False):
        return 0
    try:
        try:
            init_local_cache()
            with _connect() as conn:
                rows = conn.execute(
                    """SELECT node,cmd_id,result,rssi,snr FROM command_ack_cache
                       WHERE next_attempt <= ? ORDER BY next_attempt,created_at LIMIT 10""",
                    (time.time(),),
                ).fetchall()
        except sqlite3.Error as exc:
            _invalidate_schema_cache()
            print(f"WARNING: command ACK cache read failed: {exc}")
            return 0
        sent = 0
        for node, cmd_id, result, rssi, snr in rows:
            if report_command_ack(node, cmd_id, result, rssi=rssi, snr=snr):
                sent += 1
        return sent
    finally:
        _ack_flush_lock.release()


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


def upload_telemetry(
    node_id: str,
    payload: dict[str, Any],
    *,
    recorded_at: float | None = None,
    event_id: str | None = None,
) -> bool:
    envelope = _envelope(node_id, payload, recorded_at, event_id)
    # Draining the backlog here is opportunistic, so it is best-effort: it runs
    # outside the delivery attempt below, and anything it raises — a SQLite
    # error from the backlog rows, which says nothing about this event — used to
    # cost this event its one chance at both the network and the cache.
    try:
        flush_local_cache()
    except Exception as exc:
        print(f"WARNING: backlog flush before upload failed: {exc}")
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
