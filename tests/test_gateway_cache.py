import json
import sqlite3

import gateway_cache


class Response:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


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
    gateway_cache.report_command_ack("s03", "C1", "OK", rssi=-65, snr=6.1)
    assert captured["url"] == "https://backend.invalid/api/commands/ack"
    assert captured["json"] == {"node": "s03", "cmd_id": "C1", "result": "OK", "rssi": -65, "snr": 6.1}


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
