"""Flask backend for LoRa environmental telemetry."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from typing import Any, TypeVar

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
import psycopg
from psycopg.types.json import Jsonb
import requests

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

DATABASE_URL = os.environ.get("DATABASE_URL", "")
API_KEY = os.environ.get("LORA_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
OFFLINE_TIMEOUT = int(os.environ.get("OFFLINE_TIMEOUT_SECONDS", "180"))
ALERT_COOLDOWN = 600
RETENTION_SECONDS = 7 * 86400
HOURLY_RETENTION_SECONDS = 365 * 86400

NODE_RE = re.compile(r"^s\d{2,}$")
SENSOR_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
EVENT_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
META_NUMERIC = {
    "mcount", "rssi", "snr", "hop_rssi", "hop_snr", "loss", "level", "fallback",
    "rebooted",
}
META_ALLOWED = META_NUMERIC | {"via", "boot_id"}
ALERT_RULES = {
    "co2": {"max": 1000, "label": "CO₂", "unit": "ppm"},
    "pm25": {"max": 35, "label": "PM2.5", "unit": "µg/m³"},
    "pm10": {"max": 150, "label": "PM10", "unit": "µg/m³"},
    "temperature": {"max": 40, "label": "溫度", "unit": "°C"},
}
COMMAND_ALLOWLIST = {
    "PING",
    "SET_TARGET",
    "SET_BACKUP",
    "SET_LEVEL",
    "SET_INTERVAL",
    "PROMOTE",
    "DEMOTE",
    "REBOOT",
    "DUMP",
}
CMD_ARG_RE = re.compile(r"^[A-Za-z0-9_]{0,16}$")
COMMAND_TIMEOUT_SECONDS = 300
COMMAND_PENDING_TTL_SECONDS = 1800

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")
if not API_KEY:
    print("WARNING: LORA_API_KEY is unset; protected endpoints reject all requests")

T = TypeVar("T")

_db_lock = threading.RLock()
_db_conn: psycopg.Connection[Any] | None = None
_alert_lock = threading.Lock()
_alert_cooldowns: dict[str, float] = {}
_monitor_started = False


def _connection() -> psycopg.Connection[Any]:
    global _db_conn
    if _db_conn is None or _db_conn.closed:
        _db_conn = psycopg.connect(DATABASE_URL)
    return _db_conn


@contextmanager
def db_transaction():
    with _db_lock:
        conn = _connection()
        with conn.transaction():
            yield conn


def _discard_connection() -> None:
    global _db_conn
    with _db_lock:
        if _db_conn and not _db_conn.closed:
            _db_conn.close()
        _db_conn = None


def db_run(operation: Callable[[psycopg.Cursor[Any]], T]) -> T:
    """Run ``operation`` in a transaction, retrying once on a dropped connection.

    Neon hangs up on idle connections, so the first statement after a quiet
    period can fail even though the database is healthy. A failed transaction
    committed nothing, so replaying it on a fresh connection is safe.
    """
    for attempt in range(2):
        try:
            with db_transaction() as conn:
                with conn.cursor() as cursor:
                    return operation(cursor)
        except psycopg.OperationalError:
            _discard_connection()
            if attempt:
                raise
    raise AssertionError("unreachable")


def db_fetch(query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    def fetch(cursor: psycopg.Cursor[Any]) -> list[tuple[Any, ...]]:
        cursor.execute(query, params)
        return cursor.fetchall()

    return db_run(fetch)


def db_execute(query: str, params: tuple[Any, ...] = ()) -> None:
    db_run(lambda cursor: cursor.execute(query, params))


def db_claim(query: str, params: tuple[Any, ...] = ()) -> bool:
    """Run an ``... RETURNING`` statement; True when it matched a row."""

    def claim(cursor: psycopg.Cursor[Any]) -> bool:
        cursor.execute(query, params)
        return cursor.fetchone() is not None

    return db_run(claim)


def init_db() -> None:
    def create(cursor: psycopg.Cursor[Any]) -> None:
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS telemetry_events (
                event_id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL,
                recorded_at DOUBLE PRECISION NOT NULL,
                received_at DOUBLE PRECISION NOT NULL,
                data JSONB NOT NULL,
                meta JSONB NOT NULL
            )"""
        )
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS readings (
                id BIGSERIAL PRIMARY KEY,
                event_id TEXT,
                node_id TEXT NOT NULL,
                sensor TEXT NOT NULL,
                value DOUBLE PRECISION NOT NULL,
                recorded_at DOUBLE PRECISION NOT NULL
            )"""
        )
        cursor.execute("ALTER TABLE readings ADD COLUMN IF NOT EXISTS event_id TEXT")
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_event_sensor "
            "ON readings(event_id,sensor) WHERE event_id IS NOT NULL"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_node_time "
            "ON telemetry_events(node_id,recorded_at)"
        )
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS readings_hourly (
                node_id TEXT NOT NULL,
                sensor TEXT NOT NULL,
                bucket_ts DOUBLE PRECISION NOT NULL,
                sum_value DOUBLE PRECISION NOT NULL,
                min_value DOUBLE PRECISION NOT NULL,
                max_value DOUBLE PRECISION NOT NULL,
                sample_count BIGINT NOT NULL,
                PRIMARY KEY(node_id,sensor,bucket_ts)
            )"""
        )
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS alert_state (
                alert_key TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                last_sent DOUBLE PRECISION NOT NULL
            )"""
        )
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS commands (
                cmd_id TEXT PRIMARY KEY,
                node TEXT NOT NULL,
                cmd TEXT NOT NULL,
                arg TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                result TEXT,
                created_at DOUBLE PRECISION NOT NULL,
                sent_at DOUBLE PRECISION,
                acked_at DOUBLE PRECISION
            )"""
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_commands_status "
            "ON commands(status,created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_commands_node "
            "ON commands(node,created_at)"
        )

    db_run(create)


def _authorized() -> bool:
    supplied = request.headers.get("X-API-Key", "")
    if not supplied:
        auth = request.headers.get("Authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
    return bool(API_KEY and supplied and hmac.compare_digest(API_KEY, supplied))


def _validate_payload(body: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(body, dict):
        return None, "JSON body must be an object"
    event_id, node = body.get("event_id"), body.get("node")
    if not isinstance(event_id, str) or not EVENT_RE.fullmatch(event_id):
        return None, "invalid event_id"
    if not isinstance(node, str) or not NODE_RE.fullmatch(node):
        return None, "invalid node"
    data, meta = body.get("data"), body.get("meta", {})
    if not isinstance(data, dict) or not data or len(data) > 64:
        return None, "data must be a non-empty object with at most 64 sensors"
    if not isinstance(meta, dict) or set(meta) - META_ALLOWED:
        return None, "meta contains unsupported fields"
    clean_data: dict[str, float] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not SENSOR_RE.fullmatch(key):
            return None, f"invalid sensor key: {key!r}"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, f"sensor {key} must be numeric"
        number = float(value)
        if not math.isfinite(number):
            return None, f"sensor {key} must be finite"
        clean_data[key] = round(number, 4)
    clean_meta: dict[str, Any] = {}
    for key, value in meta.items():
        if key == "via":
            if (
                not isinstance(value, list)
                or len(value) > 16
                or any(not isinstance(v, str) or not NODE_RE.fullmatch(v) for v in value)
            ):
                return None, "meta.via must be a list of valid node IDs"
            clean_meta[key] = list(dict.fromkeys(value))
        elif key == "boot_id":
            if not isinstance(value, str) or len(value) > 64:
                return None, "invalid boot_id"
            clean_meta[key] = value
        elif isinstance(value, list):
            # Multi-hop relays report one rssi/snr per hop under the same key.
            if (
                not value
                or len(value) > 16
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value)
            ):
                return None, f"meta.{key} must be a list of numbers"
            numbers = [float(v) for v in value]
            if not all(math.isfinite(v) for v in numbers):
                return None, f"meta.{key} must be finite"
            clean_meta[key] = numbers
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, f"meta.{key} must be numeric"
            number = float(value)
            if not math.isfinite(number):
                return None, f"meta.{key} must be finite"
            clean_meta[key] = number
    try:
        recorded_at = float(body["recorded_at"])
    except (KeyError, TypeError, ValueError):
        return None, "invalid recorded_at"
    now = time.time()
    if not math.isfinite(recorded_at) or recorded_at < now - 366 * 86400 or recorded_at > now + 300:
        return None, "recorded_at outside allowed range"
    return {
        "event_id": event_id,
        "node": node,
        "recorded_at": recorded_at,
        "data": clean_data,
        "meta": clean_meta,
    }, None


def send_discord_alert(title: str, message: str, color: int = 0xFF003C) -> None:
    if not DISCORD_WEBHOOK_URL:
        print(f"ALERT: {title}: {message}")
        return

    def send() -> None:
        try:
            requests.post(
                DISCORD_WEBHOOK_URL,
                json={"embeds": [{"title": title, "description": message, "color": color}]},
                timeout=5,
            )
        except requests.RequestException as exc:
            print(f"WARNING: Discord alert failed: {exc}")

    threading.Thread(target=send, daemon=True).start()


def check_threshold_alerts(node: str, data: dict[str, float], recorded_at: float) -> None:
    now = time.time()
    # A backlog upload is not a live excursion. Reporting one as current is
    # wrong on its own, but it also spends the cooldown slot below, which would
    # then suppress a genuine excursion arriving minutes later. Same freshness
    # test the offline/online transition uses.
    if not _is_fresh_sample(recorded_at, now, now):
        return
    for sensor, rule in ALERT_RULES.items():
        if sensor not in data or data[sensor] <= rule["max"]:
            continue
        key = f"threshold:{node}:{sensor}"
        claimed = db_claim(
            """INSERT INTO alert_state(alert_key,state,last_sent)
               VALUES (%s,'active',%s)
               ON CONFLICT(alert_key) DO UPDATE SET last_sent=EXCLUDED.last_sent
               WHERE alert_state.last_sent < %s
               RETURNING alert_key""",
            (key, now, now - ALERT_COOLDOWN),
        )
        if claimed:
            send_discord_alert(
                f"數值超標｜{node.upper()}",
                f"{rule['label']} {data[sensor]} {rule['unit']}，上限 {rule['max']}",
            )


def _latest_rows() -> list[tuple[Any, ...]]:
    return db_fetch(
        """SELECT DISTINCT ON (node_id) node_id,data,meta,recorded_at,received_at
           FROM telemetry_events ORDER BY node_id,recorded_at DESC,received_at DESC"""
    )


def check_offline_alerts() -> None:
    now = time.time()
    for node, _data, _meta, recorded, received in _latest_rows():
        if _is_fresh_sample(recorded, received, now):
            continue
        claimed = db_claim(
            """INSERT INTO alert_state(alert_key,state,last_sent)
               VALUES (%s,'offline',%s)
               ON CONFLICT(alert_key) DO UPDATE
               SET state='offline',last_sent=EXCLUDED.last_sent
               WHERE alert_state.state <> 'offline'
               RETURNING alert_key""",
            (f"offline:{node}", now),
        )
        if claimed:
            send_discord_alert(
                f"節點離線｜{node.upper()}",
                f"已超過 {OFFLINE_TIMEOUT} 秒沒有回報",
                0xFFA500,
            )

def check_command_timeouts() -> None:
    """Expire downlink commands that were dispatched but never ACKed, and stale
    pending ones that were never claimed (e.g. the gateway was offline), so an
    old REBOOT/SET_TARGET can't fire long after the operator gave up on it."""
    now = time.time()
    sent_cutoff = now - COMMAND_TIMEOUT_SECONDS
    pending_cutoff = now - COMMAND_PENDING_TTL_SECONDS

    def sweep(cursor: psycopg.Cursor[Any]) -> None:
        cursor.execute(
            "UPDATE commands SET status='timeout' WHERE status='sent' AND sent_at < %s",
            (sent_cutoff,),
        )
        cursor.execute(
            "UPDATE commands SET status='expired' WHERE status='pending' AND created_at < %s",
            (pending_cutoff,),
        )

    db_run(sweep)


_last_maintenance = 0.0


def _is_fresh_sample(recorded_at: float, received_at: float, now: float) -> bool:
    """A backlog upload is not proof that the originating node is online."""
    return (
        now - received_at <= OFFLINE_TIMEOUT
        and now - recorded_at <= OFFLINE_TIMEOUT
    )


def database_maintenance() -> None:
    """Atomically downsample expired readings, then enforce both retention limits."""
    global _last_maintenance
    now = time.time()
    if now - _last_maintenance < 300:
        return
    raw_cutoff = now - RETENTION_SECONDS
    hourly_cutoff = now - HOURLY_RETENTION_SECONDS
    def maintain(cursor: psycopg.Cursor[Any]) -> None:
        cursor.execute(
            """WITH expired AS (
                   DELETE FROM readings WHERE recorded_at < %s
                   RETURNING node_id,sensor,value,recorded_at
               )
               INSERT INTO readings_hourly
                   (node_id,sensor,bucket_ts,sum_value,min_value,max_value,sample_count)
               SELECT node_id,sensor,FLOOR(recorded_at/3600)*3600,
                      SUM(value),MIN(value),MAX(value),COUNT(*)
               FROM expired
               GROUP BY node_id,sensor,FLOOR(recorded_at/3600)*3600
               ON CONFLICT(node_id,sensor,bucket_ts) DO UPDATE SET
                   sum_value=readings_hourly.sum_value+EXCLUDED.sum_value,
                   min_value=LEAST(readings_hourly.min_value,EXCLUDED.min_value),
                   max_value=GREATEST(readings_hourly.max_value,EXCLUDED.max_value),
                   sample_count=readings_hourly.sample_count+EXCLUDED.sample_count""",
            (raw_cutoff,),
        )
        cursor.execute(
            "DELETE FROM telemetry_events WHERE recorded_at < %s", (raw_cutoff,)
        )
        cursor.execute(
            "DELETE FROM readings_hourly WHERE bucket_ts < %s", (hourly_cutoff,)
        )

    db_run(maintain)
    _last_maintenance = now


def _offline_monitor() -> None:
    while True:
        try:
            check_offline_alerts()
            database_maintenance()
            check_command_timeouts()
        except Exception as exc:
            print(f"WARNING: offline monitor failed: {exc}")
        time.sleep(min(30, max(5, OFFLINE_TIMEOUT // 3)))


def start_offline_monitor() -> None:
    global _monitor_started
    with _alert_lock:
        if _monitor_started:
            return
        _monitor_started = True
    threading.Thread(target=_offline_monitor, name="offline-monitor", daemon=True).start()


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/update")
def update():
    if not _authorized():
        return jsonify(status="error", message="unauthorized"), 401
    payload, error = _validate_payload(request.get_json(silent=True))
    if error:
        return jsonify(status="error", message=error), 400
    assert payload is not None
    now = time.time()

    def store(cursor: psycopg.Cursor[Any]) -> bool:
        cursor.execute(
            """INSERT INTO telemetry_events
               (event_id,node_id,recorded_at,received_at,data,meta)
               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id) DO NOTHING
               RETURNING event_id""",
            (
                payload["event_id"],
                payload["node"],
                payload["recorded_at"],
                now,
                Jsonb(payload["data"]),
                Jsonb(payload["meta"]),
            ),
        )
        is_new = cursor.fetchone() is not None
        if is_new:
            cursor.executemany(
                """INSERT INTO readings
                   (event_id,node_id,sensor,value,recorded_at)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                [
                    (
                        payload["event_id"],
                        payload["node"],
                        key,
                        value,
                        payload["recorded_at"],
                    )
                    for key, value in payload["data"].items()
                ],
            )
        else:
            # A retry after a lost HTTP response is not a new reading, but it
            # proves the node/gateway path is alive now.
            cursor.execute(
                """UPDATE telemetry_events SET received_at=GREATEST(received_at,%s)
                   WHERE event_id=%s""",
                (now, payload["event_id"]),
            )
        return is_new

    inserted = db_run(store)
    was_offline = False
    if _is_fresh_sample(payload["recorded_at"], now, now):
        was_offline = db_claim(
            """UPDATE alert_state SET state='online',last_sent=%s
               WHERE alert_key=%s AND state='offline' RETURNING alert_key""",
            (now, f"offline:{payload['node']}"),
        )
    if was_offline:
        send_discord_alert(
            f"節點恢復連線｜{payload['node'].upper()}",
            "節點已重新開始回報",
            0x39FF14,
        )
    if inserted:
        check_threshold_alerts(
            payload["node"], payload["data"], payload["recorded_at"]
        )
    return jsonify(status="success", duplicate=not inserted), 200


@app.get("/api/all_data")
def all_data():
    now = time.time()
    nodes: dict[str, Any] = {}
    for node, data, meta, recorded, received in _latest_rows():
        nodes[node] = {
            "data": data,
            "meta": meta,
            "recorded_at": recorded,
            "last_seen": received,
            "online": _is_fresh_sample(recorded, received, now),
        }
    return jsonify(
        status="online" if any(item["online"] for item in nodes.values()) else "offline",
        offline_timeout=OFFLINE_TIMEOUT,
        nodes=nodes,
    )


@app.get("/healthz")
def healthz():
    """Deployment health probe: verifies the process and PostgreSQL connection."""
    try:
        row = db_fetch("SELECT 1")
    except psycopg.Error:
        return jsonify(status="error", database="unavailable"), 503
    return jsonify(status="ok", database="ok" if row == [(1,)] else "unexpected"), 200


def _history(node: str, cutoff: float) -> dict[str, Any]:
    if not NODE_RE.fullmatch(node):
        return {"labels": []}
    rows = db_fetch(
        """SELECT recorded_at,data FROM telemetry_events
           WHERE node_id=%s AND recorded_at >= %s
           ORDER BY recorded_at,event_id""",
        (node, cutoff),
    )
    sensors = sorted({key for _ts, data in rows for key in data})
    result: dict[str, Any] = {
        "labels": [
            datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime(
                "%m/%d %H:%M:%S"
            )
            for ts, _data in rows
        ]
    }
    for sensor in sensors:
        result[sensor] = [data.get(sensor) for _ts, data in rows]
    return result


@app.get("/api/history_range/<node>")
def history_range(node: str):
    days = max(1, min(request.args.get("days", 7, type=int), 7))
    return jsonify(_history(node, time.time() - days * 86400))


@app.get("/api/history_hourly/<node>")
def history_hourly(node: str):
    if not NODE_RE.fullmatch(node):
        return jsonify(labels=[])
    days = max(1, min(request.args.get("days", 30, type=int), 365))
    rows = db_fetch(
        """SELECT sensor,bucket_ts,sum_value/sample_count,min_value,max_value
           FROM readings_hourly WHERE node_id=%s AND bucket_ts >= %s
           ORDER BY bucket_ts,sensor""",
        (node, time.time() - days * 86400),
    )
    timestamps = sorted({row[1] for row in rows})
    values = {(sensor, ts): (avg, low, high) for sensor, ts, avg, low, high in rows}
    result: dict[str, Any] = {
        "labels": [
            datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime("%m/%d %H:00")
            for ts in timestamps
        ]
    }
    for sensor in sorted({row[0] for row in rows}):
        result[sensor] = [values.get((sensor, ts), (None, None, None))[0] for ts in timestamps]
        result[f"{sensor}_min"] = [values.get((sensor, ts), (None, None, None))[1] for ts in timestamps]
        result[f"{sensor}_max"] = [values.get((sensor, ts), (None, None, None))[2] for ts in timestamps]
    return jsonify(result)


@app.get("/api/test-alert")
def test_alert():
    if not _authorized():
        return jsonify(status="error", message="unauthorized"), 401
    send_discord_alert("測試通知", "LoRa 警報通知設定成功", 0x0088FF)
    return jsonify(status="success"), 200


def _command_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    cmd_id, node, cmd, arg, status, result, created_at, sent_at, acked_at = row
    return {
        "cmd_id": cmd_id,
        "node": node,
        "cmd": cmd,
        "arg": arg,
        "status": status,
        "result": result,
        "created_at": created_at,
        "sent_at": sent_at,
        "acked_at": acked_at,
    }


@app.post("/api/commands")
def create_command():
    """Operator-facing: queue a downlink command for a node."""
    if not _authorized():
        return jsonify(status="error", message="unauthorized"), 401
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(status="error", message="JSON body must be an object"), 400
    node, cmd, arg = body.get("node"), body.get("cmd"), body.get("arg", "")
    if not isinstance(node, str) or not NODE_RE.fullmatch(node):
        return jsonify(status="error", message="invalid node"), 400
    if not isinstance(cmd, str) or cmd not in COMMAND_ALLOWLIST:
        return jsonify(status="error", message="invalid cmd"), 400
    if not isinstance(arg, str) or not CMD_ARG_RE.fullmatch(arg):
        return jsonify(status="error", message="invalid arg"), 400
    cmd_id = "C" + secrets.token_hex(4)
    db_execute(
        """INSERT INTO commands(cmd_id,node,cmd,arg,status,created_at)
           VALUES (%s,%s,%s,%s,'pending',%s)""",
        (cmd_id, node, cmd, arg, time.time()),
    )
    return jsonify(status="success", cmd_id=cmd_id), 201


@app.get("/api/commands/pending")
def pending_commands():
    """Gateway-facing: claim queued commands for delivery over serial."""
    if not _authorized():
        return jsonify(status="error", message="unauthorized"), 401
    now = time.time()

    def claim(cursor: psycopg.Cursor[Any]) -> list[tuple[Any, ...]]:
        # Expire stale pending in the same transaction so a command that
        # aged past its TTL between sweeps is never dispatched here.
        cursor.execute(
            "UPDATE commands SET status='expired' WHERE status='pending' AND created_at < %s",
            (now - COMMAND_PENDING_TTL_SECONDS,),
        )
        cursor.execute(
            """WITH claimed AS (
                   SELECT cmd_id FROM commands
                   WHERE status='pending' ORDER BY created_at LIMIT 10
               )
               UPDATE commands SET status='sent',sent_at=%s
               WHERE cmd_id IN (SELECT cmd_id FROM claimed)
               RETURNING cmd_id,node,cmd,arg""",
            (now,),
        )
        return cursor.fetchall()

    rows = db_run(claim)
    commands = [
        {"cmd_id": cmd_id, "node": node, "cmd": cmd, "arg": arg}
        for cmd_id, node, cmd, arg in rows
    ]
    return jsonify(commands=commands), 200


@app.post("/api/commands/ack")
def command_ack():
    """Gateway-facing: report that a node executed a command and replied."""
    if not _authorized():
        return jsonify(status="error", message="unauthorized"), 401
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(status="error", message="JSON body must be an object"), 400
    cmd_id, node, result = body.get("cmd_id"), body.get("node"), body.get("result")
    if not isinstance(cmd_id, str) or not cmd_id or len(cmd_id) > 32:
        return jsonify(status="error", message="invalid cmd_id"), 400
    if not isinstance(node, str) or not NODE_RE.fullmatch(node):
        return jsonify(status="error", message="invalid node"), 400
    if not isinstance(result, str) or not result or len(result) > 64:
        return jsonify(status="error", message="invalid result"), 400
    updated = db_claim(
        """UPDATE commands SET status='acked',result=%s,acked_at=%s
           WHERE cmd_id=%s AND node=%s RETURNING cmd_id""",
        (result, time.time(), cmd_id, node),
    )
    if not updated:
        return jsonify(status="error", message="unknown cmd_id"), 404
    send_discord_alert(
        f"指令已執行｜{node.upper()}",
        f"cmd_id={cmd_id} 結果={result}",
        0xFF003C if result.startswith("ERR") else 0x39FF14,  # PONG/OK* 皆為成功,ERR* 才紅
    )
    return jsonify(status="success"), 200


@app.post("/api/commands/dispatch_failed")
def command_dispatch_failed():
    """Gateway-facing: a claimed command never made it onto the serial link.

    /api/commands/pending marks rows 'sent' when it hands them over, so a
    gateway that then fails to write is holding a command the operator is
    watching succeed. Delivery stays at-most-once — the command is not requeued,
    because REBOOT and the PROMOTE/DEMOTE pair are not safe to repeat without
    cmd_id de-duplication on the node. This only stops the lie.
    """
    if not _authorized():
        return jsonify(status="error", message="unauthorized"), 401
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(status="error", message="JSON body must be an object"), 400
    cmd_id, node, reason = body.get("cmd_id"), body.get("node"), body.get("reason", "")
    if not isinstance(cmd_id, str) or not cmd_id or len(cmd_id) > 32:
        return jsonify(status="error", message="invalid cmd_id"), 400
    if not isinstance(node, str) or not NODE_RE.fullmatch(node):
        return jsonify(status="error", message="invalid node"), 400
    if not isinstance(reason, str) or len(reason) > 200:
        return jsonify(status="error", message="invalid reason"), 400
    # Only a command still believed to be in flight: an ACK that raced us in
    # means it did reach the node, and that answer is the truthful one.
    updated = db_claim(
        """UPDATE commands SET status='dispatch_failed',result=%s
           WHERE cmd_id=%s AND node=%s AND status='sent' RETURNING cmd_id""",
        (f"ERR_DISPATCH: {reason}"[:200], cmd_id, node),
    )
    if not updated:
        return jsonify(status="error", message="unknown or already settled cmd_id"), 404
    send_discord_alert(
        f"指令未送出｜{node.upper()}",
        f"cmd_id={cmd_id} 原因={reason}",
    )
    return jsonify(status="success"), 200


@app.get("/api/commands")
def list_commands():
    node = request.args.get("node")
    if node is not None and not NODE_RE.fullmatch(node):
        return jsonify(commands=[]), 200
    if node:
        rows = db_fetch(
            """SELECT cmd_id,node,cmd,arg,status,result,created_at,sent_at,acked_at
               FROM commands WHERE node=%s ORDER BY created_at DESC LIMIT 20""",
            (node,),
        )
    else:
        rows = db_fetch(
            """SELECT cmd_id,node,cmd,arg,status,result,created_at,sent_at,acked_at
               FROM commands ORDER BY created_at DESC LIMIT 50"""
        )
    return jsonify(commands=[_command_row_to_dict(row) for row in rows]), 200


@app.get("/api/commands/<cmd_id>")
def command_status(cmd_id: str):
    rows = db_fetch(
        """SELECT cmd_id,node,cmd,arg,status,result,created_at,sent_at,acked_at
           FROM commands WHERE cmd_id=%s""",
        (cmd_id,),
    )
    if not rows:
        return jsonify(status="error", message="unknown cmd_id"), 404
    return jsonify(_command_row_to_dict(rows[0])), 200


init_db()
start_offline_monitor()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
