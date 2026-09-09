"""Gateway behaviours that keep telemetry and diagnostics from going missing.

Telemetry is write-ahead logged: the reader writes every packet to SQLite and
the uploader drains that outbox, so nothing lives only in memory. These cover
the reader's framing, the drop tally, ACK decoupling, and the fail-fast path.
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


@pytest.fixture(autouse=True)
def _isolated_outbox(tmp_path, monkeypatch):
    """Give every test its own outbox.

    _handle_line writes to SQLite now, so a test that parses a valid packet
    would otherwise leave rows in whichever database gateway_cache happens to be
    pointed at — the repository's own gateway_cache.db when this file runs
    alone. Pointing _local_db at tmp_path is enough: init_local_cache is keyed
    on that path, so the schema is built on first use and never shared.
    """
    monkeypatch.setattr(
        bridge.gateway_cache, "_local_db", str(tmp_path / "outbox.db")
    )


def _prepare_run(monkeypatch):
    monkeypatch.setattr(bridge.gateway_cache, "configure", lambda *args: None)
    monkeypatch.setattr(bridge.gateway_cache, "cache_count", lambda: 0)
    monkeypatch.setattr(bridge.gateway_cache, "dead_letter_count", lambda: 0)
    monkeypatch.setattr(bridge.threading, "Thread", _NoopThread)


def test_serial_open_failure_retries_until_stopped(monkeypatch, capsys):
    _prepare_run(monkeypatch)
    gateway = bridge.Gateway()
    attempts = []
    waits = []

    def fail_to_open(*args, **kwargs):
        attempts.append(True)
        raise bridge.serial.SerialException("port unavailable")

    def wait(delay):
        waits.append(delay)
        if len(waits) == 6:
            gateway.stop_event.set()
            return True
        return False

    monkeypatch.setattr(bridge.serial, "Serial", fail_to_open)
    monkeypatch.setattr(gateway.stop_event, "wait", wait)
    gateway.run()

    assert len(attempts) == 6
    assert waits == [3, 6, 12, 24, 30, 30]
    assert gateway.fatal_reason is None
    assert "retrying in 3s" in capsys.readouterr().out


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


def test_restart_with_boot_id_is_announced_and_kept(monkeypatch, capsys):
    gateway = bridge.Gateway()
    written = []
    monkeypatch.setattr(
        bridge.gateway_cache,
        "save_to_local_cache",
        lambda node, payload, **kwargs: written.append((node, payload)) or True,
    )
    gateway._handle_line("數據: s05_m40,t,25,boot,AAAA1111")
    gateway._handle_line("數據: s05_m1,t,25,boot,BBBB2222")

    out = capsys.readouterr().out
    assert "NODE RESTART: s05" in out and "BBBB2222" in out
    # The restart is the event under investigation: it must reach the outbox,
    # not be discarded as an out-of-order counter.
    assert len(written) == 2
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

def test_first_fatal_reason_wins(capsys):
    gateway = bridge.Gateway()
    gateway._fail_fatally("serial connection failed: port gone")
    gateway._fail_fatally("something that broke while stopping")

    assert gateway.fatal_reason == "serial connection failed: port gone"
    assert capsys.readouterr().out.count("shutting down") == 1
    assert gateway.stop_event.is_set()


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


def test_shutdown_cleanup_cannot_mask_the_real_cause(monkeypatch, capsys):
    """A failing drain must not replace the exception that ended the run."""
    _prepare_run(monkeypatch)

    gateway = bridge.Gateway()
    def fail_delivery():
        raise bridge.gateway_cache.TelemetryDeliveryError("outbox failed")

    monkeypatch.setattr(gateway, "_read_serial", fail_delivery)
    monkeypatch.setattr(
        gateway,
        "_drain_ack_queue",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("drain blew up")),
    )

    with pytest.raises(bridge.gateway_cache.TelemetryDeliveryError, match="outbox failed"):
        gateway.run()

    out = capsys.readouterr().out
    assert "could not persist queued ACKs on shutdown: drain blew up" in out


# --- the outbox is the only telemetry path ------------------------------------

def test_reader_persists_before_anything_can_lose_it(monkeypatch):
    """A packet is durable the moment it is parsed, not after a failed upload."""
    gateway = bridge.Gateway()
    written = []
    monkeypatch.setattr(
        bridge.gateway_cache,
        "save_to_local_cache",
        lambda node, payload, **kwargs: written.append((node, kwargs["recorded_at"])) or True,
    )

    gateway._handle_line("數據: s05_m1,t,25")

    assert len(written) == 1 and written[0][0] == "s05"
    # And the uploader is woken rather than left to its idle interval.
    assert gateway.outbox_pending.is_set()


def test_reader_fails_fast_when_the_outbox_refuses(monkeypatch):
    gateway = bridge.Gateway()
    monkeypatch.setattr(
        bridge.gateway_cache, "save_to_local_cache", lambda *a, **k: False
    )

    # Nowhere durable to put it: the reader must not carry on accepting packets.
    with pytest.raises(bridge.gateway_cache.TelemetryDeliveryError):
        gateway._handle_line("數據: s05_m1,t,25")


def test_uploader_retires_the_process_when_the_outbox_is_unusable(monkeypatch):
    gateway = bridge.Gateway()
    monkeypatch.setattr(bridge, "CACHE_FLUSH_INTERVAL", 0.001)
    monkeypatch.setattr(bridge, "OUTBOX_FAILURE_LIMIT", 3)
    attempts = []

    def broken_outbox():
        attempts.append(True)
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(bridge.gateway_cache, "flush_local_cache", broken_outbox)
    gateway._uploader()

    assert len(attempts) == 3
    assert "outbox unusable after 3 attempts" in (gateway.fatal_reason or "")
    assert gateway.stop_event.is_set()


def test_a_network_outage_is_not_an_outbox_failure(monkeypatch):
    """flush_local_cache keeps the row on network faults; that is not fatal."""
    gateway = bridge.Gateway()
    monkeypatch.setattr(bridge, "CACHE_FLUSH_INTERVAL", 0.001)
    calls = []

    def nothing_sent():
        calls.append(True)
        if len(calls) == 3:
            gateway.stop_event.set()
        return 0  # rows stayed in the outbox, no exception

    monkeypatch.setattr(bridge.gateway_cache, "flush_local_cache", nothing_sent)
    gateway._uploader()

    assert gateway.fatal_reason is None


# --- the command plane must survive its own failures --------------------------

def test_poller_survives_an_unexpected_failure(monkeypatch, capsys):
    """One bad cycle must not end commands and ACKs for the rest of the run."""
    gateway = bridge.Gateway()
    monkeypatch.setattr(bridge, "COMMAND_POLL_INTERVAL", 0.001)
    cycles = []

    def flaky():
        cycles.append(True)
        if len(cycles) == 1:
            raise RuntimeError("something unforeseen")
        if len(cycles) == 3:
            gateway.stop_event.set()
        return 0

    monkeypatch.setattr(bridge.gateway_cache, "flush_command_acks", flaky)
    monkeypatch.setattr(bridge.gateway_cache, "fetch_pending_commands", lambda: [])
    gateway._command_poller()

    # It kept going after the exception rather than dying silently.
    assert len(cycles) == 3
    assert "command plane cycle failed (1x): something unforeseen" in capsys.readouterr().out


def test_poller_failure_is_not_fatal(monkeypatch):
    """Telemetry does not run through here, so a broken command plane degrades."""
    gateway = bridge.Gateway()
    monkeypatch.setattr(bridge, "COMMAND_POLL_INTERVAL", 0.001)
    calls = []

    def always_broken():
        calls.append(True)
        if len(calls) == 5:
            gateway.stop_event.set()
        raise RuntimeError("command plane is down")

    monkeypatch.setattr(bridge.gateway_cache, "flush_command_acks", always_broken)
    monkeypatch.setattr(bridge.gateway_cache, "fetch_pending_commands", lambda: [])
    gateway._command_poller()

    assert gateway.fatal_reason is None
    assert not gateway.stop_event.is_set() or len(calls) == 5


def test_repeated_poller_failures_are_not_logged_every_cycle(monkeypatch, capsys):
    gateway = bridge.Gateway()
    monkeypatch.setattr(bridge, "COMMAND_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(bridge, "COMMAND_FAILURE_REPORT_EVERY", 5)
    calls = []

    def always_broken():
        calls.append(True)
        if len(calls) == 12:
            gateway.stop_event.set()
        raise RuntimeError("still down")

    monkeypatch.setattr(bridge.gateway_cache, "flush_command_acks", always_broken)
    monkeypatch.setattr(bridge.gateway_cache, "fetch_pending_commands", lambda: [])
    gateway._command_poller()

    # 12 failures, reported at the 1st, 5th and 10th — not twelve times.
    assert capsys.readouterr().out.count("command plane cycle failed") == 3


def test_a_failed_ack_delivery_is_persisted_not_dropped(monkeypatch, capsys):
    """It is already off the queue: the error handling must not lose it."""
    gateway = bridge.Gateway()
    saved = []
    monkeypatch.setattr(
        bridge.gateway_cache,
        "report_command_ack",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("resolver exploded")),
    )
    monkeypatch.setattr(
        bridge.gateway_cache,
        "save_command_ack_for_retry",
        lambda node, cmd_id, result, **kwargs: saved.append((node, cmd_id, result)),
    )
    gateway.ack_queue.put_nowait({"node": "s03", "cmd_id": "C7", "result": "OK"})

    gateway._drain_ack_queue(deliver=True)

    assert saved == [("s03", "C7", "OK")]
    assert "ACK C7 not delivered" in capsys.readouterr().out
    assert gateway.ack_queue.empty()


@pytest.mark.parametrize("failure_stage", ["open", "in_waiting", "read"])
def test_serial_reconnect_receives_and_persists_next_packet(monkeypatch, capsys, failure_stage):
    _prepare_run(monkeypatch)
    gateway = bridge.Gateway()
    waits = []
    opened = []
    saved = []

    class Port:
        is_open = True

        def __init__(self, broken=False):
            self.broken = broken

        @property
        def in_waiting(self):
            if self.broken and failure_stage == "in_waiting":
                raise bridge.serial.SerialException("ClearCommError failed")
            return 1

        def read(self, size):
            if self.broken:
                raise bridge.serial.SerialException("read failed")
            gateway.stop_event.set()
            return "數據: s03_m1,t,25,boot,ABCD1234\n".encode()

        def close(self):
            self.is_open = False

    def connect(*args, **kwargs):
        assert kwargs["write_timeout"] == 1.0
        first = not opened
        if first and failure_stage == "open":
            opened.append(None)
            raise bridge.serial.SerialException("port absent")
        port = Port(broken=first)
        opened.append(port)
        return port

    monkeypatch.setattr(bridge.serial, "Serial", connect)
    monkeypatch.setattr(gateway.stop_event, "wait", lambda delay: waits.append(delay) or False)
    monkeypatch.setattr(
        bridge.gateway_cache, "save_to_local_cache",
        lambda node, payload, **kwargs: saved.append((node, payload)) or True,
    )
    gateway.run()

    assert len(opened) == 2
    assert waits == [3]
    assert all(port is None or not port.is_open for port in opened)
    assert len(saved) == 1 and saved[0][0] == "s03"
    assert "Serial connection recovered on COM5" in capsys.readouterr().out
    assert gateway.fatal_reason is None


def test_reconnect_discards_fragment_and_preserves_boot_tracker(monkeypatch):
    _prepare_run(monkeypatch)
    gateway = bridge.Gateway()
    first_line = "數據: s03_m1,t,25,boot,AAAA1111\n".encode()
    new_boot = "數據: s03_m1,t,26,boot,BBBB2222\n".encode()
    saved = []
    errors = bridge.serial.SerialException

    class Port:
        is_open = True
        in_waiting = 1

        def __init__(self, items):
            self.items = iter(items)

        def read(self, size):
            item = next(self.items)
            if isinstance(item, Exception):
                raise item
            if item is None:
                gateway.stop_event.set()
                return b""
            return item

        def close(self):
            self.is_open = False

    ports = iter([
        Port([first_line + b"broken-fragment", errors("unplugged")]),
        Port([first_line + new_boot, None]),
    ])
    monkeypatch.setattr(bridge.serial, "Serial", lambda *a, **k: next(ports))
    monkeypatch.setattr(gateway.stop_event, "wait", lambda delay: False)
    monkeypatch.setattr(
        bridge.gateway_cache, "save_to_local_cache",
        lambda node, payload, **kwargs: saved.append(payload) or True,
    )
    gateway.run()

    assert [p["meta"]["boot_id"] for p in saved] == ["AAAA1111", "BBBB2222"]
    assert saved[1]["meta"]["rebooted"] == 1.0
    assert gateway.dropped == {"s03/duplicate": 1}


def test_disconnected_poller_delivers_acks_without_claiming_commands(monkeypatch):
    gateway = bridge.Gateway()
    reports = []
    gateway.ack_queue.put_nowait({"node": "s03", "cmd_id": "C8", "result": "OK"})
    monkeypatch.setattr(
        bridge.gateway_cache, "report_command_ack",
        lambda *a, **k: reports.append(a),
    )
    monkeypatch.setattr(bridge.gateway_cache, "flush_command_acks", lambda: 0)
    monkeypatch.setattr(
        bridge.gateway_cache, "fetch_pending_commands",
        lambda: pytest.fail("must not claim commands while offline"),
    )
    monkeypatch.setattr(gateway.stop_event, "wait", lambda delay: gateway.stop_event.set())
    gateway._command_poller()
    assert reports == [("s03", "C8", "OK")]


def test_write_failure_triggers_reconnect_without_replaying_command(monkeypatch):
    _prepare_run(monkeypatch)
    gateway = bridge.Gateway()
    writes = []
    reports = []
    opened = []

    class Port:
        is_open = True
        in_waiting = 1

        def write(self, data):
            writes.append(data)
            raise bridge.serial.SerialException("USB write failed")

        def read(self, size):
            # A downlink can run while a read is active (no lock starvation).
            if len(opened) == 1:
                gateway._dispatch_command({"cmd_id": "C9", "node": "s03", "cmd": "REBOOT"})
            else:
                gateway.stop_event.set()
            return b""

        def close(self):
            self.is_open = False

    def connect(*args, **kwargs):
        port = Port()
        opened.append(port)
        return port

    monkeypatch.setattr(bridge.serial, "Serial", connect)
    monkeypatch.setattr(gateway.stop_event, "wait", lambda delay: False)
    monkeypatch.setattr(
        bridge.gateway_cache, "report_dispatch_failure",
        lambda *args: reports.append(args),
    )
    gateway.run()
    assert len(opened) == 2
    assert writes == [b"CMD C9 s03 REBOOT\n"]
    assert reports == [("s03", "C9", "USB write failed")]


def test_serial_writer_rejects_partial_write_without_unbounded_flush():
    class PartialPort:
        def write(self, data):
            return len(data) - 1

        def flush(self):
            pytest.fail("unbounded flush must not be used")

    with pytest.raises(bridge.serial.SerialException, match="incomplete"):
        bridge.SerialWriter(PartialPort()).write_line("CMD C1 s03 PING")


def test_disconnect_while_fetching_reports_claimed_command_once(monkeypatch):
    gateway = bridge.Gateway()
    reports = []
    gateway.serial_writer = object()  # only used to observe connected state

    def fetch():
        gateway._close_serial()
        return [{"cmd_id": "C10", "node": "s03", "cmd": "REBOOT"}]

    monkeypatch.setattr(bridge.gateway_cache, "flush_command_acks", lambda: 0)
    monkeypatch.setattr(bridge.gateway_cache, "fetch_pending_commands", fetch)
    monkeypatch.setattr(
        bridge.gateway_cache, "report_dispatch_failure",
        lambda *args: reports.append(args),
    )
    monkeypatch.setattr(gateway.stop_event, "wait", lambda delay: gateway.stop_event.set())
    gateway._command_poller()
    assert reports == [("s03", "C10", "serial port not open")]


def test_ctrl_c_during_serial_retry_exits_cleanly(monkeypatch, capsys):
    _prepare_run(monkeypatch)
    gateway = bridge.Gateway()
    attempts = []

    def connect(*args, **kwargs):
        attempts.append(True)
        raise bridge.serial.SerialException("port absent")

    def interrupt(delay):
        raise KeyboardInterrupt

    monkeypatch.setattr(bridge.serial, "Serial", connect)
    monkeypatch.setattr(gateway.stop_event, "wait", interrupt)
    gateway.run()
    assert len(attempts) == 1
    assert gateway.fatal_reason is None
    assert "Gateway shutdown requested" in capsys.readouterr().out
