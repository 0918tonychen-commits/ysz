"""Flask command-control-plane tests.

`main` connects to PostgreSQL and starts a monitor thread at import time, and
the README promises the suite needs no Neon. So we stub ``psycopg.connect``
before importing ``main`` (init_db then only issues DDL, never fetches), and
each test swaps ``main.db_transaction`` / ``main.db_fetch`` for an in-memory
fake to drive the endpoint logic without a real database.
"""

import contextlib
from functools import partial
import os
import time

# Must be set before importing main (it validates these at import time).
os.environ.setdefault("DATABASE_URL", "postgresql://fake/db")
os.environ.setdefault("LORA_API_KEY", "test-key-123")
os.environ["DISCORD_WEBHOOK_URL"] = ""  # never hit a real webhook from tests

import psycopg
import pytest


class _ImportStubCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        pass

    def executemany(self, *args, **kwargs):
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _ImportStubConn:
    closed = False

    def cursor(self):
        return _ImportStubCursor()

    @contextlib.contextmanager
    def transaction(self):
        yield self

    def close(self):
        pass


psycopg.connect = lambda *args, **kwargs: _ImportStubConn()

import main  # noqa: E402

API_KEY = "test-key-123"
AUTH = {"X-API-Key": API_KEY}


class FakeCursor:
    def __init__(self, fetchone=None, fetchall=()):
        self._fetchone = fetchone
        self._fetchall = list(fetchall)
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=()):
        self.executed.append((query, params))

    def executemany(self, query, seq):
        self.executed.append((query, list(seq)))

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def fake_transaction(cursor):
    @contextlib.contextmanager
    def _tx():
        yield FakeConn(cursor)

    return _tx


def sql_of(cursor):
    return " ".join(query for query, _ in cursor.executed)


# --- auth + input validation (these return before any DB access) ------------

def test_create_command_requires_auth():
    with main.app.test_client() as client:
        response = client.post("/api/commands", json={"node": "s03", "cmd": "PING"})
    assert response.status_code == 401


def test_create_command_rejects_bad_node():
    with main.app.test_client() as client:
        response = client.post(
            "/api/commands", json={"node": "bad", "cmd": "PING"}, headers=AUTH
        )
    assert response.status_code == 400
    assert response.get_json()["message"] == "invalid node"


def test_create_command_rejects_cmd_outside_allowlist():
    with main.app.test_client() as client:
        response = client.post(
            "/api/commands", json={"node": "s03", "cmd": "DROP_TABLE"}, headers=AUTH
        )
    assert response.status_code == 400
    assert response.get_json()["message"] == "invalid cmd"


def test_create_command_rejects_arg_with_separators():
    # A comma/space in arg would corrupt the comma-delimited LoRa command frame.
    with main.app.test_client() as client:
        response = client.post(
            "/api/commands",
            json={"node": "s03", "cmd": "SET_TARGET", "arg": "s05,evil"},
            headers=AUTH,
        )
    assert response.status_code == 400
    assert response.get_json()["message"] == "invalid arg"


# --- happy paths driven through the fake DB ---------------------------------

def test_create_command_returns_cmd_id(monkeypatch):
    cursor = FakeCursor()
    monkeypatch.setattr(main, "db_transaction", fake_transaction(cursor))
    with main.app.test_client() as client:
        response = client.post(
            "/api/commands",
            json={"node": "s03", "cmd": "SET_TARGET", "arg": "s05"},
            headers=AUTH,
        )
    assert response.status_code == 201
    assert response.get_json()["cmd_id"].startswith("C")


def test_pending_commands_expires_stale_then_claims(monkeypatch):
    cursor = FakeCursor(fetchall=[("C1", "s03", "PING", "")])
    monkeypatch.setattr(main, "db_transaction", fake_transaction(cursor))
    with main.app.test_client() as client:
        response = client.get("/api/commands/pending", headers=AUTH)
    assert response.status_code == 200
    assert response.get_json()["commands"] == [
        {"cmd_id": "C1", "node": "s03", "cmd": "PING", "arg": ""}
    ]
    # defense-in-depth: the claim endpoint expires stale pending in the same txn
    assert "status='expired'" in sql_of(cursor)


def test_pending_commands_requires_auth():
    with main.app.test_client() as client:
        response = client.get("/api/commands/pending")
    assert response.status_code == 401


def test_command_ack_unknown_returns_404(monkeypatch):
    cursor = FakeCursor(fetchone=None)  # UPDATE ... RETURNING matched no row
    monkeypatch.setattr(main, "db_transaction", fake_transaction(cursor))
    with main.app.test_client() as client:
        response = client.post(
            "/api/commands/ack",
            json={"node": "s03", "cmd_id": "Cdead", "result": "OK"},
            headers=AUTH,
        )
    assert response.status_code == 404


def test_command_ack_known_returns_success(monkeypatch):
    cursor = FakeCursor(fetchone=("Cabc",))
    monkeypatch.setattr(main, "db_transaction", fake_transaction(cursor))
    monkeypatch.setattr(main, "send_discord_alert", lambda *a, **k: None)
    with main.app.test_client() as client:
        response = client.post(
            "/api/commands/ack",
            json={"node": "s03", "cmd_id": "Cabc", "result": "OK"},
            headers=AUTH,
        )
    assert response.status_code == 200
    query, params = cursor.executed[0]
    assert "AND node=%s" in query
    assert params[-1] == "s03"


def test_command_ack_rejects_bad_node(monkeypatch):
    with main.app.test_client() as client:
        response = client.post(
            "/api/commands/ack",
            json={"node": "nope", "cmd_id": "Cabc", "result": "OK"},
            headers=AUTH,
        )
    assert response.status_code == 400


def test_command_status_unknown_returns_404(monkeypatch):
    monkeypatch.setattr(main, "db_fetch", lambda *a, **k: [])
    with main.app.test_client() as client:
        response = client.get("/api/commands/Cnope")
    assert response.status_code == 404


def test_list_commands_by_node_maps_rows(monkeypatch):
    row = ("C1", "s03", "PING", "", "acked", "PONG", 1.0, 2.0, 3.0)
    monkeypatch.setattr(main, "db_fetch", lambda *a, **k: [row])
    with main.app.test_client() as client:
        response = client.get("/api/commands?node=s03")
    assert response.status_code == 200
    body = response.get_json()["commands"][0]
    assert body["cmd_id"] == "C1"
    assert body["status"] == "acked"
    assert body["result"] == "PONG"


def test_list_commands_rejects_bad_node():
    with main.app.test_client() as client:
        response = client.get("/api/commands?node=bad")
    assert response.status_code == 200
    assert response.get_json()["commands"] == []


# --- the new pending-TTL sweep ----------------------------------------------

def test_check_command_timeouts_expires_sent_and_pending(monkeypatch):
    cursor = FakeCursor()
    monkeypatch.setattr(main, "db_transaction", fake_transaction(cursor))
    main.check_command_timeouts()
    sql = sql_of(cursor)
    assert "status='timeout'" in sql and "status='sent'" in sql
    assert "status='expired'" in sql and "status='pending'" in sql


# --- write-path reconnect (Neon drops idle connections) ---------------------

def _flaky_transaction(cursor, failures):
    """A db_transaction whose first ``failures`` uses raise OperationalError."""
    remaining = {"count": failures}

    @contextlib.contextmanager
    def _tx():
        if remaining["count"]:
            remaining["count"] -= 1
            raise psycopg.OperationalError("connection closed by server")
        yield FakeConn(cursor)

    return _tx


def test_write_retries_once_after_dropped_connection(monkeypatch):
    cursor = FakeCursor(fetchone=("Cabc",))
    monkeypatch.setattr(main, "db_transaction", _flaky_transaction(cursor, 1))
    monkeypatch.setattr(main, "_discard_connection", lambda: None)
    monkeypatch.setattr(main, "send_discord_alert", lambda *a, **k: None)
    with main.app.test_client() as client:
        response = client.post(
            "/api/commands/ack",
            json={"node": "s03", "cmd_id": "Cabc", "result": "OK"},
            headers=AUTH,
        )
    # Previously the first statement after an idle period surfaced as a 500.
    assert response.status_code == 200
    assert len(cursor.executed) == 1  # the retry ran the statement exactly once


def test_write_gives_up_after_second_failure(monkeypatch):
    cursor = FakeCursor()
    monkeypatch.setattr(main, "db_transaction", _flaky_transaction(cursor, 2))
    monkeypatch.setattr(main, "_discard_connection", lambda: None)
    with pytest.raises(psycopg.OperationalError):
        main.db_execute("UPDATE commands SET status='x'")
    assert cursor.executed == []


def test_failed_transaction_discards_the_connection(monkeypatch):
    cursor = FakeCursor()
    discarded = []
    monkeypatch.setattr(main, "db_transaction", _flaky_transaction(cursor, 1))
    monkeypatch.setattr(main, "_discard_connection", lambda: discarded.append(True))
    main.db_execute("UPDATE commands SET status='x'")
    assert discarded == [True]


def test_backlog_sample_is_not_considered_online():
    now = 10_000.0
    assert not main._is_fresh_sample(
        now - main.OFFLINE_TIMEOUT - 1,
        now,
        now,
    )


def test_recent_sample_and_delivery_are_online():
    now = 10_000.0
    assert main._is_fresh_sample(now - 1, now - 1, now)


# --- dispatch_failed: a claimed command that never reached the radio ----------

def test_dispatch_failed_requires_auth():
    with main.app.test_client() as client:
        response = client.post(
            "/api/commands/dispatch_failed", json={"node": "s03", "cmd_id": "Cabc"}
        )
    assert response.status_code == 401


def test_dispatch_failed_marks_only_a_command_still_in_flight(monkeypatch):
    cursor = FakeCursor(fetchone=("Cabc",))
    monkeypatch.setattr(main, "db_transaction", fake_transaction(cursor))
    monkeypatch.setattr(main, "send_discord_alert", lambda *a, **k: None)
    with main.app.test_client() as client:
        response = client.post(
            "/api/commands/dispatch_failed",
            json={"node": "s03", "cmd_id": "Cabc", "reason": "port unavailable"},
            headers=AUTH,
        )
    assert response.status_code == 200
    query, params = cursor.executed[0]
    assert "status='dispatch_failed'" in query
    # An ACK that raced us in wins: it proves the node did get the command.
    assert "AND status='sent'" in query
    assert params[0] == "ERR_DISPATCH: port unavailable"


def test_dispatch_failed_on_already_settled_command_returns_404(monkeypatch):
    cursor = FakeCursor(fetchone=None)
    monkeypatch.setattr(main, "db_transaction", fake_transaction(cursor))
    with main.app.test_client() as client:
        response = client.post(
            "/api/commands/dispatch_failed",
            json={"node": "s03", "cmd_id": "Cabc", "reason": "port unavailable"},
            headers=AUTH,
        )
    assert response.status_code == 404


def test_dispatch_failed_rejects_oversized_reason(monkeypatch):
    with main.app.test_client() as client:
        response = client.post(
            "/api/commands/dispatch_failed",
            json={"node": "s03", "cmd_id": "Cabc", "reason": "x" * 201},
            headers=AUTH,
        )
    assert response.status_code == 400


# --- threshold alerts must describe now, not a backlog replay ----------------

def test_stale_backlog_reading_does_not_raise_a_current_alert(monkeypatch):
    """Flushing a 3-day backlog must not report old excursions as happening now."""
    sent = []
    monkeypatch.setattr(main, "send_discord_alert", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(
        main, "db_claim", lambda *a, **k: pytest.fail("must not touch alert_state")
    )
    stale = time.time() - 3 * 86400

    main.check_threshold_alerts("s03", {"co2": 5000.0}, stale)

    assert sent == []


def test_fresh_reading_still_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(main, "send_discord_alert", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(main, "db_claim", lambda *a, **k: True)

    main.check_threshold_alerts("s03", {"co2": 5000.0}, time.time())

    assert len(sent) == 1
    assert "S03" in sent[0][0]


# --- an alert Discord never accepted must not silence the next one -----------

class _FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def _capture_posts(monkeypatch, responses):
    """Serve ``responses`` (a status code or an exception per attempt)."""
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(json)
        outcome = responses[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(main.requests, "post", post)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", "https://discord.invalid/hook")
    return calls


def test_delivery_reports_success_on_a_2xx(monkeypatch):
    calls = _capture_posts(monkeypatch, [_FakeResponse(204)])

    assert main._deliver_discord("t", "m", 0) is True
    assert len(calls) == 1


def test_delivery_retries_once_after_a_network_error(monkeypatch):
    calls = _capture_posts(
        monkeypatch, [main.requests.RequestException("boom"), _FakeResponse(204)]
    )

    assert main._deliver_discord("t", "m", 0) is True
    assert len(calls) == 2


def test_delivery_gives_up_and_reports_failure(monkeypatch):
    calls = _capture_posts(monkeypatch, [_FakeResponse(500), _FakeResponse(502)])

    assert main._deliver_discord("t", "m", 0) is False
    assert len(calls) == 2


def test_a_revoked_webhook_is_not_retried(monkeypatch):
    """404/401 fails identically the second time; retrying just delays the log."""
    calls = _capture_posts(monkeypatch, [_FakeResponse(404)])

    assert main._deliver_discord("t", "m", 0) is False
    assert len(calls) == 1


def test_rate_limit_waits_the_retry_after_it_was_given(monkeypatch):
    _capture_posts(
        monkeypatch,
        [_FakeResponse(429, {"Retry-After": "3"}), _FakeResponse(204)],
    )
    slept = []
    monkeypatch.setattr(main.time, "sleep", slept.append)

    assert main._deliver_discord("t", "m", 0) is True
    assert slept == [3.0]


def test_absurd_retry_after_falls_back_to_the_default_delay(monkeypatch):
    response = _FakeResponse(429, {"Retry-After": "6000"})

    assert main._discord_retry_delay(response, 2.0) == 2.0


def test_failed_delivery_releases_the_threshold_cooldown(monkeypatch):
    """The whole point: a dropped alert must not also eat the 10-minute slot."""
    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", "https://discord.invalid/hook")
    monkeypatch.setattr(main, "_deliver_discord", lambda *a: False)
    released = []
    monkeypatch.setattr(
        main, "db_claim", lambda query, params: released.append((query, params))
    )
    threads = []
    monkeypatch.setattr(
        main.threading, "Thread", lambda target, daemon: _InlineThread(target, threads)
    )

    main.send_discord_alert(
        "t", "m", on_failure=partial(main._release_threshold_slot, "k", 1.0)
    )

    assert len(released) == 1
    query, params = released[0]
    assert query.startswith("DELETE FROM alert_state")
    assert params == ("k", 1.0)


def test_delivered_alert_keeps_the_cooldown(monkeypatch):
    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", "https://discord.invalid/hook")
    monkeypatch.setattr(main, "_deliver_discord", lambda *a: True)
    monkeypatch.setattr(
        main, "db_claim", lambda *a, **k: pytest.fail("must not roll back")
    )
    threads = []
    monkeypatch.setattr(
        main.threading, "Thread", lambda target, daemon: _InlineThread(target, threads)
    )

    main.send_discord_alert("t", "m", on_failure=lambda: pytest.fail("delivered"))


def test_unconfigured_webhook_logs_instead_of_rolling_back(monkeypatch):
    """With no webhook the log line is the delivery; rolling back would make
    every pass re-alert into the same log."""
    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", "")

    main.send_discord_alert("t", "m", on_failure=lambda: pytest.fail("no rollback"))


class _InlineThread:
    """Run the alert body synchronously so the test can assert on its effects."""

    def __init__(self, target, started):
        self._target = target
        self._started = started

    def start(self):
        self._started.append(self)
        self._target()
