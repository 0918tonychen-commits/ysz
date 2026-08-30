import json
import sqlite3

import gateway_cache


class Response:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


def test_dead_letter_rows_do_not_evict_live_pending(tmp_path, monkeypatch):
    database = tmp_path / "cache.db"
    gateway_cache.configure("https://backend.invalid/update", str(database))
    monkeypatch.setattr(gateway_cache, "_max_rows", 3)
    monkeypatch.setattr(gateway_cache, "_max_dead_letter", 100)

    # Two poison rows quarantined with NEWER timestamps than the live ones.
    for number in (1, 2):
        gateway_cache.save_to_local_cache(
            "s05", {"data": {"t": number}, "meta": {}},
            event_id=f"dead-{number}", recorded_at=1000.0 + number,
        )
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE cache SET dead_letter=1")

    # Fill live pending exactly to the cap with OLDER timestamps.
    for number in range(3):
        gateway_cache.save_to_local_cache(
            "s05", {"data": {"t": number}, "meta": {}},
            event_id=f"live-{number}", recorded_at=float(number),
        )

    # cache_count only counts live rows; the cap was not spent on dead letters.
    assert gateway_cache.cache_count() == 3
    assert gateway_cache.dead_letter_count() == 2
    with sqlite3.connect(database) as conn:
        live_ids = {
            row[0]
            for row in conn.execute("SELECT event_id FROM cache WHERE dead_letter=0")
        }
    assert live_ids == {"live-0", "live-1", "live-2"}


def test_network_failure_is_cached_with_stable_identity(tmp_path, monkeypatch):
    database = tmp_path / "cache.db"
    gateway_cache.configure("https://backend.invalid/update", str(database), "secret")

    def fail(*args, **kwargs):
        raise gateway_cache.requests.Timeout("offline")

    monkeypatch.setattr(gateway_cache.requests, "post", fail)
    assert not gateway_cache.upload_telemetry(
        "s05",
        {"data": {"t": 25.0}, "meta": {"via": ["s02"]}},
        event_id="event-fixed-0001",
        recorded_at=1000.0,
    )
    assert gateway_cache.cache_count() == 1
    with sqlite3.connect(database) as conn:
        envelope = json.loads(conn.execute("SELECT payload FROM cache").fetchone()[0])
    assert envelope["event_id"] == "event-fixed-0001"
    assert envelope["recorded_at"] == 1000.0
    assert envelope["meta"]["via"] == ["s02"]


def test_500_remains_cached_and_429_then_recovery(tmp_path, monkeypatch):
    database = tmp_path / "cache.db"
    gateway_cache.configure("https://backend.invalid/update", str(database))
    gateway_cache.save_to_local_cache(
        "s05", {"data": {"t": 1}, "meta": {}}, event_id="event-fixed-0002"
    )
    statuses = iter([500, 429, 204])
    monkeypatch.setattr(
        gateway_cache.requests, "post", lambda *args, **kwargs: Response(next(statuses))
    )
    assert gateway_cache.flush_local_cache() == 0
    assert gateway_cache.cache_count() == 1
    assert gateway_cache.flush_local_cache() == 0
    assert gateway_cache.cache_count() == 1
    assert gateway_cache.flush_local_cache() == 1
    assert gateway_cache.cache_count() == 0


def test_fetch_pending_commands_returns_list(tmp_path, monkeypatch):
    database = tmp_path / "cache.db"
    gateway_cache.configure("https://backend.invalid/update", str(database), "secret")
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return Response(200, {"commands": [{"cmd_id": "C1", "node": "s03", "cmd": "PING", "arg": ""}]})

    monkeypatch.setattr(gateway_cache.requests, "get", fake_get)
    commands = gateway_cache.fetch_pending_commands()
    assert commands == [{"cmd_id": "C1", "node": "s03", "cmd": "PING", "arg": ""}]
    assert captured["url"] == "https://backend.invalid/api/commands/pending"
    assert captured["headers"] == {"X-API-Key": "secret"}


def test_fetch_pending_commands_swallows_network_errors(tmp_path, monkeypatch):
    database = tmp_path / "cache.db"
    gateway_cache.configure("https://backend.invalid/update", str(database))

    def fail(*args, **kwargs):
        raise gateway_cache.requests.Timeout("offline")

    monkeypatch.setattr(gateway_cache.requests, "get", fail)
    assert gateway_cache.fetch_pending_commands() == []


def test_report_command_ack_posts_to_ack_endpoint(tmp_path, monkeypatch):
    database = tmp_path / "cache.db"
    gateway_cache.configure("https://backend.invalid/update", str(database), "secret")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return Response(200)

    monkeypatch.setattr(gateway_cache.requests, "post", fake_post)
    assert gateway_cache.report_command_ack("s03", "C1", "OK", rssi=-65, snr=6.1)
    assert captured["url"] == "https://backend.invalid/api/commands/ack"
    assert captured["json"] == {"node": "s03", "cmd_id": "C1", "result": "OK", "rssi": -65, "snr": 6.1}


def test_failed_command_ack_is_persisted_and_retried(tmp_path, monkeypatch):
    database = tmp_path / "cache.db"
    gateway_cache.configure("https://backend.invalid/update", str(database), "secret")
    responses = iter([Response(500), Response(200)])
    monkeypatch.setattr(
        gateway_cache.requests, "post", lambda *args, **kwargs: next(responses)
    )

    assert not gateway_cache.report_command_ack("s03", "C1", "OK")
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE command_ack_cache SET next_attempt=0")
        assert conn.execute("SELECT COUNT(*) FROM command_ack_cache").fetchone()[0] == 1

    assert gateway_cache.flush_command_acks() == 1
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM command_ack_cache").fetchone()[0] == 0


def test_permanent_command_ack_error_is_not_retried(tmp_path, monkeypatch):
    database = tmp_path / "cache.db"
    gateway_cache.configure("https://backend.invalid/update", str(database), "secret")
    monkeypatch.setattr(
        gateway_cache.requests, "post", lambda *args, **kwargs: Response(404)
    )

    assert not gateway_cache.report_command_ack("s03", "C1", "OK")
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM command_ack_cache").fetchone()[0] == 0


def test_command_ack_retry_uses_backoff(tmp_path, monkeypatch):
    database = tmp_path / "cache.db"
    gateway_cache.configure("https://backend.invalid/update", str(database), "secret")
    monkeypatch.setattr(
        gateway_cache.requests, "post", lambda *args, **kwargs: Response(500)
    )

    gateway_cache.report_command_ack("s03", "C1", "OK")
    assert gateway_cache.flush_command_acks() == 0
    with sqlite3.connect(database) as conn:
        retries, next_attempt, created_at = conn.execute(
            "SELECT retries,next_attempt,created_at FROM command_ack_cache"
        ).fetchone()
    assert retries == 1
    assert next_attempt >= created_at + 4.9


def test_permanent_400_does_not_block_following_event(tmp_path, monkeypatch):
    database = tmp_path / "cache.db"
    gateway_cache.configure("https://backend.invalid/update", str(database))
    for number in (3, 4):
        gateway_cache.save_to_local_cache(
            "s05",
            {"data": {"t": number}, "meta": {}},
            event_id=f"event-fixed-000{number}",
            recorded_at=float(number),
        )
    statuses = iter([400, 201])
    monkeypatch.setattr(
        gateway_cache.requests, "post", lambda *args, **kwargs: Response(next(statuses))
    )
    assert gateway_cache.flush_local_cache() == 1
    assert gateway_cache.cache_count() == 0
    assert gateway_cache.dead_letter_count() == 1


def test_schema_init_is_skipped_per_database_but_redone_on_switch(tmp_path, monkeypatch):
    """The hot-path guard must not outlive the database it was taken for."""
    calls = []
    original = gateway_cache._init_schema
    monkeypatch.setattr(
        gateway_cache, "_init_schema", lambda: (calls.append(gateway_cache._local_db), original())[1]
    )

    first = tmp_path / "first.db"
    gateway_cache.configure("https://backend.invalid/update", str(first))
    for _ in range(5):
        gateway_cache.init_local_cache()
    assert calls == [str(first)]  # every cache op used to pay for this

    # configure() pointing elsewhere must still build the second schema.
    second = tmp_path / "second.db"
    gateway_cache.configure("https://backend.invalid/update", str(second))
    assert calls == [str(first), str(second)]
    assert gateway_cache.save_to_local_cache(
        "s05", {"data": {"t": 1.0}, "meta": {}}, event_id="switched-0001"
    )
    assert gateway_cache.cache_count() == 1


def test_legacy_database_is_migrated_and_the_backfill_is_idempotent(tmp_path):
    """The filtered backfill must still convert a pre-event_id database."""
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            """CREATE TABLE cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                timestamp REAL,
                retries INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.executemany(
            "INSERT INTO cache(node_id,payload,timestamp) VALUES (?,?,?)",
            [("s05", '{"data":{"t":1}}', 111.0), ("s06", '{"data":{"t":2}}', 222.0)],
        )

    gateway_cache.configure("https://backend.invalid/update", str(database))

    with sqlite3.connect(database) as conn:
        migrated = conn.execute(
            "SELECT node_id,event_id,recorded_at FROM cache ORDER BY id"
        ).fetchall()
    assert [row[1] for row in migrated] == ["legacy-1", "legacy-2"]
    assert [row[2] for row in migrated] == [111.0, 222.0]

    # Re-running must leave the already-migrated rows exactly as they are.
    gateway_cache._invalidate_schema_cache()
    gateway_cache.init_local_cache()
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT node_id,event_id,recorded_at FROM cache ORDER BY id"
        ).fetchall() == migrated
