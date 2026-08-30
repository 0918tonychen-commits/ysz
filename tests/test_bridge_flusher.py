"""Gateway behaviours that keep telemetry and diagnostics from going missing:
the idle cache flusher, and the reporting of packets the tracker discards.

``upload_telemetry`` only drains the SQLite backlog as a side effect of a new
uplink, so a backlog built while the backend was down would sit there forever if
the nodes also went quiet. ``Gateway._cache_flusher`` covers that gap.
"""

import sys
import types

import pytest

# bridge.py imports pyserial at module scope; the flusher never touches it, so a
# stub keeps this test independent of a real serial port (and of the package).
if "serial" not in sys.modules:
    stub = types.ModuleType("serial")
    stub.Serial = object
    stub.SerialException = type("SerialException", (Exception,), {})
    sys.modules["serial"] = stub

import bridge  # noqa: E402


class _NoopThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


def _prepare_run(monkeypatch):
    monkeypatch.setattr(bridge.gateway_cache, "configure", lambda *args: None)
    monkeypatch.setattr(bridge.gateway_cache, "cache_count", lambda: 0)
    monkeypatch.setattr(bridge.gateway_cache, "dead_letter_count", lambda: 0)
    monkeypatch.setattr(bridge.threading, "Thread", _NoopThread)


def test_serial_failure_propagates_for_service_restart(monkeypatch, capsys):
    _prepare_run(monkeypatch)

    def fail_to_open(*args, **kwargs):
        raise bridge.serial.SerialException("port unavailable")

    monkeypatch.setattr(bridge.serial, "Serial", fail_to_open)

    with pytest.raises(bridge.serial.SerialException, match="port unavailable"):
        bridge.Gateway().run()

    assert "CRITICAL: serial connection failed" in capsys.readouterr().out


def test_delivery_failure_propagates_for_service_restart(monkeypatch):
    _prepare_run(monkeypatch)

    class OneLineSerial:
        is_open = False
        in_waiting = 1

        def read(self, size):
            return b"packet\n"

    monkeypatch.setattr(bridge.serial, "Serial", lambda *args, **kwargs: OneLineSerial())
    gateway = bridge.Gateway()

    def fail_delivery(line):
        raise bridge.gateway_cache.TelemetryDeliveryError("queue and cache unavailable")

    monkeypatch.setattr(gateway, "_handle_line", fail_delivery)

    with pytest.raises(
        bridge.gateway_cache.TelemetryDeliveryError,
        match="queue and cache unavailable",
    ):
        gateway.run()


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


# --- the reader thread must never wait on the network -------------------------

def test_ack_does_not_block_the_reader_on_http(monkeypatch):
    """An ACK used to cost the reader a full HTTP timeout of serial silence."""
    gateway = bridge.Gateway()
    reported = []
    monkeypatch.setattr(
        bridge.gateway_cache,
        "report_command_ack",
        lambda *args, **kwargs: reported.append(args) or True,
    )

    gateway._handle_line("【ACK】from=s03, cmdId=C001, result=OK, rssi=-65, snr=6.1")

    # Queued, not sent: nothing touched the network on this thread.
    assert reported == []
    assert gateway.ack_queue.qsize() == 1

    gateway._drain_ack_queue(deliver=True)
    assert reported == [("s03", "C001", "OK")]
    assert gateway.ack_queue.empty()


def test_ack_queue_overflow_persists_instead_of_posting(monkeypatch):
    gateway = bridge.Gateway()
    saved = []
    monkeypatch.setattr(
        bridge.gateway_cache,
        "report_command_ack",
        lambda *a, **k: pytest.fail("the reader thread must not post"),
    )
    monkeypatch.setattr(
        bridge.gateway_cache,
        "save_command_ack_for_retry",
        lambda node, cmd_id, result, **kwargs: saved.append((node, cmd_id, result)),
    )
    for _ in range(bridge.ACK_QUEUE_SIZE):
        gateway.ack_queue.put_nowait({"node": "s03", "cmd_id": "C0", "result": "OK"})

    gateway._handle_line("【ACK】from=s03, cmdId=C999, result=OK")

    assert saved == [("s03", "C999", "OK")]


def test_poller_persists_queued_acks_on_shutdown(monkeypatch):
    gateway = bridge.Gateway()
    saved = []
    monkeypatch.setattr(
        bridge.gateway_cache, "flush_command_acks", lambda: 0
    )
    monkeypatch.setattr(
        bridge.gateway_cache, "fetch_pending_commands", lambda: []
    )
    monkeypatch.setattr(
        bridge.gateway_cache,
        "save_command_ack_for_retry",
        lambda node, cmd_id, result, **kwargs: saved.append((node, cmd_id, result)),
    )
    gateway.ack_queue.put_nowait({"node": "s03", "cmd_id": "C7", "result": "OK"})
    gateway.stop_event.set()

    gateway._command_poller()

    # An ACK still in memory at shutdown becomes durable rather than vanishing.
    assert saved == [("s03", "C7", "OK")]


# --- coordinated fail-fast ----------------------------------------------------

def test_uploader_declares_fatal_instead_of_swallowing(monkeypatch, capsys):
    """Both sinks gone is unrecoverable: stop, do not drop the rest silently."""
    gateway = bridge.Gateway()
    gateway.upload_queue.put(("s03", {"data": {"t": 1.0}, "meta": {}}, 1000.0))
    gateway.stop_event.set()

    def both_sinks_gone(*args, **kwargs):
        raise bridge.gateway_cache.TelemetryDeliveryError(
            "upload failed (HTTP 500) and telemetry could not be cached"
        )

    monkeypatch.setattr(bridge.gateway_cache, "upload_telemetry", both_sinks_gone)
    gateway._uploader()

    assert gateway.fatal_reason is not None
    assert "could not be cached" in gateway.fatal_reason
    assert "shutting down" in capsys.readouterr().out
    # The thread stayed alive long enough to finish the item it had taken, so
    # nothing is left waiting on a task_done that will never arrive.
    assert gateway.upload_queue.unfinished_tasks == 0


def test_recoverable_upload_error_is_not_fatal(monkeypatch):
    gateway = bridge.Gateway()
    gateway.upload_queue.put(("s03", {"data": {"t": 1.0}, "meta": {}}, 1000.0))
    gateway.stop_event.set()
    monkeypatch.setattr(
        bridge.gateway_cache,
        "upload_telemetry",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("transient")),
    )
    gateway._uploader()

    assert gateway.fatal_reason is None  # keep running; only the packet is lost


def test_first_fatal_reason_wins(capsys):
    gateway = bridge.Gateway()
    gateway._fail_fatally("serial connection failed: port gone")
    gateway._fail_fatally("something that broke while stopping")

    assert gateway.fatal_reason == "serial connection failed: port gone"
    assert capsys.readouterr().out.count("shutting down") == 1
    assert gateway.stop_event.is_set()


def test_shutdown_persists_events_the_uploader_never_took(monkeypatch):
    gateway = bridge.Gateway()
    saved = []
    monkeypatch.setattr(
        bridge.gateway_cache,
        "save_to_local_cache",
        lambda node, payload, **kwargs: saved.append((node, kwargs["recorded_at"])) or True,
    )
    for number in range(3):
        gateway.upload_queue.put(("s03", {"data": {"t": 1.0}, "meta": {}}, float(number)))

    assert gateway._drain_upload_queue_to_cache() == 3
    assert saved == [("s03", 0.0), ("s03", 1.0), ("s03", 2.0)]
    # join() can now return: every taken item was accounted for.
    assert gateway.upload_queue.unfinished_tasks == 0


def test_serial_write_failure_is_reported_not_just_logged(monkeypatch):
    """The backend already marked it 'sent'; silence would leave that lie."""
    gateway = bridge.Gateway()
    reported = []
    monkeypatch.setattr(
        bridge.gateway_cache,
        "report_dispatch_failure",
        lambda node, cmd_id, reason: reported.append((node, cmd_id, reason)),
    )

    class DeadPort:
        def write_line(self, line):
            raise bridge.serial.SerialException("port unavailable")

    gateway.serial_writer = DeadPort()
    gateway._dispatch_command(
        {"cmd_id": "Cabc", "node": "s03", "cmd": "REBOOT", "arg": ""}
    )

    assert reported == [("s03", "Cabc", "port unavailable")]


def test_dispatch_without_a_serial_port_is_reported(monkeypatch):
    gateway = bridge.Gateway()
    reported = []
    monkeypatch.setattr(
        bridge.gateway_cache,
        "report_dispatch_failure",
        lambda node, cmd_id, reason: reported.append((node, cmd_id, reason)),
    )
    gateway.serial_writer = None
    gateway._dispatch_command({"cmd_id": "Cabc", "node": "s03", "cmd": "PING"})

    assert reported == [("s03", "Cabc", "serial port not open")]


def test_successful_dispatch_reports_nothing(monkeypatch):
    gateway = bridge.Gateway()
    monkeypatch.setattr(
        bridge.gateway_cache,
        "report_dispatch_failure",
        lambda *a: pytest.fail("a delivered command must not be marked failed"),
    )
    written = []

    class Port:
        def write_line(self, line):
            written.append(line)

    gateway.serial_writer = Port()
    gateway._dispatch_command(
        {"cmd_id": "Cabc", "node": "s03", "cmd": "SET_LEVEL", "arg": "2"}
    )

    assert written == ["CMD Cabc s03 SET_LEVEL 2"]
