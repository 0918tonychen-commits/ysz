"""Gateway behaviours that keep telemetry and diagnostics from going missing:
the idle cache flusher, and the reporting of packets the tracker discards.

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


# --- discarded packets must leave a trace ------------------------------------

def test_out_of_order_packet_is_logged_not_silently_dropped(capsys):
    gateway = bridge.Gateway()
    gateway._handle_line("數據: s05_m40,t,25")
    gateway._handle_line("數據: s05_m1,t,25")

    out = capsys.readouterr().out
    assert "dropped LoRa payload (out_of_order)" in out
    assert gateway.dropped["s05/out_of_order"] == 1


def test_repeated_drops_print_one_sample_then_only_tally(capsys):
    gateway = bridge.Gateway()
    gateway._handle_line("數據: s05_m40,t,25")
    for _ in range(5):
        gateway._handle_line("數據: s05_m1,t,25")

    out = capsys.readouterr().out
    # One full sample is worth printing; five identical ones are just noise.
    assert out.count("dropped LoRa payload") == 1
    assert gateway.dropped["s05/out_of_order"] == 5


def test_restart_with_boot_id_is_announced_and_kept(capsys):
    gateway = bridge.Gateway()
    gateway._handle_line("數據: s05_m40,t,25,boot,AAAA1111")
    gateway._handle_line("數據: s05_m1,t,25,boot,BBBB2222")

    out = capsys.readouterr().out
    assert "NODE RESTART: s05" in out and "BBBB2222" in out
    # The restart is the event under investigation: it must reach the backend,
    # not be discarded as an out-of-order counter.
    assert gateway.upload_queue.qsize() == 2
    assert gateway.dropped == {}


def test_oversized_burst_keeps_the_complete_lines(capsys):
    gateway = bridge.Gateway()
    handled = []
    gateway._handle_line = handled.append

    # One read carrying far more than MAX_SERIAL_LINE, but only whole lines.
    burst = b"".join(
        f"數據: s10_m{n}, t, 25.0, h, 60.0\n".encode("utf-8") for n in range(1, 300)
    )
    assert len(burst) > bridge.MAX_SERIAL_LINE
    leftover = gateway._consume(bytearray(), burst)

    # The cap is about one runaway line, not about the size of a healthy burst.
    assert len(handled) == 299
    assert leftover == bytearray()
    assert "oversized" not in capsys.readouterr().out


def test_oversized_line_is_dropped_without_taking_its_neighbours(capsys):
    gateway = bridge.Gateway()
    handled = []
    gateway._handle_line = handled.append

    garbage = b"x" * (bridge.MAX_SERIAL_LINE + 1)
    chunk = (
        "數據: s10_m1, t, 25.0\n".encode("utf-8")
        + garbage
        + "\n數據: s10_m2, t, 26.0\n".encode("utf-8")
    )
    leftover = gateway._consume(bytearray(), chunk)

    assert handled == ["數據: s10_m1, t, 25.0", "數據: s10_m2, t, 26.0"]
    assert leftover == bytearray()
    assert "oversized serial line discarded" in capsys.readouterr().out


def test_oversized_fragment_is_cleared_across_reads(capsys):
    gateway = bridge.Gateway()
    handled = []
    gateway._handle_line = handled.append

    # A run with no newline in sight, split over several reads, must not grow
    # without bound — but must also not be handed on once it finally terminates.
    buffer = bytearray()
    for _ in range(3):
        buffer = gateway._consume(buffer, b"x" * 4096)
    assert len(buffer) <= bridge.MAX_SERIAL_LINE
    assert "oversized serial line fragment discarded" in capsys.readouterr().out

    buffer = gateway._consume(buffer, "tail\n數據: s10_m9, t, 25.0\n".encode("utf-8"))
    assert handled[-1] == "數據: s10_m9, t, 25.0"


def test_uploader_reports_success(monkeypatch, capsys):
    gateway = bridge.Gateway()
    payload = {"data": {"temperature": 25.0}, "meta": {"mcount": 7}}
    gateway.upload_queue.put(("s03", payload, 1000.0))
    gateway.stop_event.set()
    monkeypatch.setattr(
        bridge.gateway_cache, "upload_telemetry", lambda *args, **kwargs: True
    )

    gateway._uploader()

    assert "UPLOADED: node=s03 mcount=7" in capsys.readouterr().out


def test_uploader_reports_sqlite_fallback(monkeypatch, capsys):
    gateway = bridge.Gateway()
    payload = {"data": {"temperature": 25.0}, "meta": {"mcount": 8}}
    gateway.upload_queue.put(("s03", payload, 1000.0))
    gateway.stop_event.set()
    monkeypatch.setattr(
        bridge.gateway_cache, "upload_telemetry", lambda *args, **kwargs: False
    )

    gateway._uploader()

    assert "CACHED: node=s03 mcount=8 waiting for retry" in capsys.readouterr().out
