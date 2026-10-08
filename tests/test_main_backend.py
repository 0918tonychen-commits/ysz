"""Flask telemetry, database, alert, and health endpoint tests.

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


# --- uplink-only surface -----------------------------------------------------

def test_downlink_command_endpoints_are_removed():
    with main.app.test_client() as client:
        assert client.post("/api/commands", json={}).status_code == 404
        assert client.get("/api/commands").status_code == 404
        assert client.get("/api/commands/pending").status_code == 404
        assert client.post("/api/commands/ack", json={}).status_code == 404


# --- database reconnect (Neon drops idle connections) -----------------------









# --- happy paths driven through the fake DB ---------------------------------



















# --- the new pending-TTL sweep ----------------------------------------------



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




def test_write_gives_up_after_second_failure(monkeypatch):
    cursor = FakeCursor()
    monkeypatch.setattr(main, "db_transaction", _flaky_transaction(cursor, 2))
    monkeypatch.setattr(main, "_discard_connection", lambda: None)
    with pytest.raises(psycopg.OperationalError):
        main.db_execute("UPDATE telemetry_events SET received_at=received_at")
    assert cursor.executed == []


def test_failed_transaction_discards_the_connection(monkeypatch):
    cursor = FakeCursor()
    discarded = []
    monkeypatch.setattr(main, "db_transaction", _flaky_transaction(cursor, 1))
    monkeypatch.setattr(main, "_discard_connection", lambda: discarded.append(True))
    main.db_execute("UPDATE telemetry_events SET received_at=received_at")
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


# --- a silenced alert path must be visible from outside ----------------------

def test_healthz_reports_an_unset_webhook(monkeypatch):
    """The failure this exists to catch: alerts firing into the log forever
    while every external signal says the deployment is healthy."""
    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(main, "db_fetch", lambda *a, **k: [(1,)])
    with main.app.test_client() as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "database": "ok",
        "discord": "unset",
    }


def test_healthz_reports_a_configured_webhook_without_leaking_it(monkeypatch):
    secret = "https://discord.com/api/webhooks/1/super-secret-token"
    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", secret)
    monkeypatch.setattr(main, "db_fetch", lambda *a, **k: [(1,)])
    with main.app.test_client() as client:
        response = client.get("/healthz")

    assert response.get_json()["discord"] == "configured"
    assert "super-secret-token" not in response.get_data(as_text=True)


def test_healthz_still_reports_discord_when_the_database_is_down(monkeypatch):
    def boom(*_args, **_kwargs):
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(main, "db_fetch", boom)
    with main.app.test_client() as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.get_json()["discord"] == "unset"
