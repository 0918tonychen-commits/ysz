"""The gateway's idle cache flusher.

``upload_telemetry`` only drains the SQLite backlog as a side effect of a new
uplink, so a backlog built while the backend was down would sit there forever if
the nodes also went quiet. ``Gateway._cache_flusher`` covers that gap.
"""

import sys
import types

# bridge.py imports pyserial at module scope; the flusher never touches it, so a
# stub keeps this test independent of a real serial port (and of the package).
if "serial" not in sys.modules:
    stub = types.ModuleType("serial")
    stub.Serial = object
    stub.SerialException = type("SerialException", (Exception,), {})
    sys.modules["serial"] = stub

import bridge  # noqa: E402


def test_flusher_drains_backlog_while_no_packets_arrive(monkeypatch):
    gateway = bridge.Gateway()
    calls = []

    def fake_flush():
        calls.append(True)
        if len(calls) == 2:
            gateway.stop_event.set()
        return 3

    monkeypatch.setattr(bridge, "CACHE_FLUSH_INTERVAL", 0.01)
    monkeypatch.setattr(bridge.gateway_cache, "flush_local_cache", fake_flush)
    gateway._cache_flusher()

    # It kept flushing on its own, with no uplink to trigger upload_telemetry.
    assert len(calls) == 2
    assert gateway.stop_event.is_set()


def test_flusher_survives_a_failing_flush(monkeypatch):
    gateway = bridge.Gateway()
    calls = []

    def fake_flush():
        calls.append(True)
        if len(calls) == 2:
            gateway.stop_event.set()
            return 0
        raise RuntimeError("database is locked")

    monkeypatch.setattr(bridge, "CACHE_FLUSH_INTERVAL", 0.01)
    monkeypatch.setattr(bridge.gateway_cache, "flush_local_cache", fake_flush)
    gateway._cache_flusher()  # must not propagate; the thread has to stay alive

    assert len(calls) == 2


def test_flusher_stops_promptly_on_shutdown(monkeypatch):
    gateway = bridge.Gateway()
    calls = []

    monkeypatch.setattr(bridge, "CACHE_FLUSH_INTERVAL", 0.01)
    monkeypatch.setattr(
        bridge.gateway_cache, "flush_local_cache", lambda: calls.append(True)
    )
    gateway.stop_event.set()
    gateway._cache_flusher()

    assert calls == []
