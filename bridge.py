"""Serial-to-HTTP LoRa gateway."""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any

import serial
from dotenv import load_dotenv

import gateway_cache
from lora_payload import MCountTracker, parse_ack_line, parse_payload

load_dotenv()

COM_PORT = os.environ.get("LORA_COM_PORT", "COM3")
BAUD_RATE = int(os.environ.get("LORA_BAUD_RATE", "115200"))
BACKEND_URL = os.environ.get("BACKEND_URL", "https://ysz.onrender.com/update")
LOCAL_DB = os.environ.get("GATEWAY_CACHE_DB", "gateway_cache.db")
API_KEY = os.environ.get("LORA_API_KEY", "")
ACK_QUEUE_SIZE = int(os.environ.get("ACK_QUEUE_SIZE", "128"))
SHUTDOWN_DRAIN_SECONDS = float(os.environ.get("SHUTDOWN_DRAIN_SECONDS", "10"))
OUTBOX_FAILURE_LIMIT = int(os.environ.get("OUTBOX_FAILURE_LIMIT", "5"))
# At the default 5s poll this is roughly one line every five minutes.
COMMAND_FAILURE_REPORT_EVERY = 60
MAX_SERIAL_LINE = 8192
COMMAND_POLL_INTERVAL = float(os.environ.get("COMMAND_POLL_INTERVAL_SECONDS", "5"))
CACHE_FLUSH_INTERVAL = float(os.environ.get("CACHE_FLUSH_INTERVAL_SECONDS", "30"))
DROP_REPORT_INTERVAL = float(os.environ.get("DROP_REPORT_INTERVAL_SECONDS", "300"))


class SerialWriter:
    def __init__(self, serial_port: serial.Serial) -> None:
        self._serial = serial_port
        self._lock = threading.Lock()

    def write_line(self, command: str) -> None:
        if not command or not command.strip():
            raise ValueError("empty serial command is not allowed")
        with self._lock:
            self._serial.write((command.rstrip("\r\n") + "\n").encode("utf-8"))
            self._serial.flush()


class Gateway:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        # Any thread may declare the gateway unrecoverable; run() reads this to
        # pick the exit code. A worker cannot just re-raise: an exception on a
        # non-main thread leaves the process status at 0. The reader re-raises
        # instead, which is fail-fast on the main thread. See _fail_fatally.
        self.fatal_reason: str | None = None
        self._fatal_lock = threading.Lock()
        # Raised whenever the reader adds to the outbox, so the uploader starts
        # draining immediately instead of waiting out its idle interval.
        self.outbox_pending = threading.Event()
        # Acknowledgements are the one thing still buffered in memory: they are
        # small, and losing one costs a status, not a measurement. See _queue_ack.
        self.ack_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=ACK_QUEUE_SIZE)
        self.tracker = MCountTracker()
        self.serial_port: serial.Serial | None = None
        self.serial_writer: SerialWriter | None = None
        # Dropped packets used to vanish without a trace, which hid exactly the
        # restart behaviour we need to see. Count them and report periodically.
        self.dropped: dict[str, int] = {}
        self._last_drop_report = 0.0

    def enqueue(self, node: str, payload: dict[str, Any], recorded_at: float) -> None:
        """Write the packet to the durable outbox before anything else sees it.

        Telemetry used to sit in an in-memory queue until an upload attempt
        failed, so a power cut took everything not yet tried — hundreds of
        packets, with no record that they had ever existed. SQLite is now the
        first stop, not the fallback, and delivery is a separate concern.
        """
        if not gateway_cache.save_to_local_cache(
            node, payload, recorded_at=recorded_at
        ):
            raise gateway_cache.TelemetryDeliveryError(
                "telemetry could not be written to the outbox"
            )
        self.outbox_pending.set()

    def _fail_fatally(self, reason: str) -> None:
        """Declare an unrecoverable condition and begin an orderly shutdown.

        Safe to call from any thread. The first reason wins, so the cause is
        reported rather than whatever failed downstream of it while stopping.
        """
        with self._fatal_lock:
            first = self.fatal_reason is None
            if first:
                self.fatal_reason = reason
        if first:
            print(f"CRITICAL: {reason}; shutting down for a restart")
        self.stop_event.set()

    def _uploader(self) -> None:
        """Drain the outbox, woken by new packets and by an idle timer.

        This is the only delivery path. It replaces the upload queue and the
        separate idle flusher, which existed because the queue could not drain a
        backlog nobody was pushing into. Nothing here can lose a packet: a row
        stays in SQLite until the backend has taken it or permanently refused it.
        """
        failures = 0
        while not self.stop_event.is_set():
            self.outbox_pending.clear()
            try:
                while gateway_cache.flush_local_cache() and not self.stop_event.is_set():
                    pass
                failures = 0
            except Exception as exc:
                # Network faults never reach here — flush_local_cache keeps the
                # row and retries. Escaping means the outbox itself is unusable,
                # and the reader will not notice until its next write, which at
                # a packet every few tens of minutes is far too long to sit here
                # accepting data we have no way to ship.
                failures += 1
                print(f"WARNING: outbox flush failed ({failures}): {exc}")
                if failures >= OUTBOX_FAILURE_LIMIT:
                    self._fail_fatally(f"outbox unusable after {failures} attempts: {exc}")
            self.outbox_pending.wait(CACHE_FLUSH_INTERVAL)

    def _handle_line(self, line: str) -> None:
        if "【ACK】" in line:
            ack = parse_ack_line(line)
            if ack:
                self._queue_ack(ack)
            else:
                print(f"WARNING: malformed ACK line: {line[:200]}")
            return
        if "數據:" not in line:
            return
        raw = line.split("數據:", 1)[1].strip()
        node, payload, status = parse_payload(raw, self.tracker)
        if status == "valid" and node and payload:
            if payload["meta"].get("rebooted"):
                print(
                    f"NODE RESTART: {node} boot_id={payload['meta'].get('boot_id')} "
                    f"mcount={payload['meta'].get('mcount')}"
                )
            self.enqueue(node, payload, time.time())
            return
        if status in {"duplicate", "out_of_order"}:
            self._record_drop(node, status, raw)
            return
        print(f"WARNING: ignored LoRa payload ({status}): {raw[:200]}")

    def _queue_ack(self, ack: dict[str, Any]) -> None:
        """Hand an ACK to the poller thread instead of reporting it inline.

        ``report_command_ack`` makes a synchronous HTTP request. On this thread
        that is seconds of serial silence — a connect plus read timeout, and DNS
        on top of it, which no timeout covers. Nothing calls ``read()`` mean-
        while, so the driver's receive buffer overflows and drops bytes before
        Python ever sees them: invisible to the drop tally, unlike every other
        loss this gateway records.
        """
        try:
            self.ack_queue.put_nowait(ack)
        except queue.Full:
            # Still no network on this thread: a bounded local write instead.
            self._persist_ack(ack)

    def _persist_ack(self, ack: dict[str, Any]) -> None:
        gateway_cache.save_command_ack_for_retry(
            ack["node"],
            ack["cmd_id"],
            ack["result"],
            rssi=ack.get("rssi"),
            snr=ack.get("snr"),
        )

    def _drain_ack_queue(self, *, deliver: bool) -> None:
        """Report queued ACKs, or persist them when shutting down.

        An ACK leaves the queue the moment it is taken, so a failure past that
        point has to put it somewhere durable — otherwise the error handling is
        itself what loses it.
        """
        while True:
            try:
                ack = self.ack_queue.get_nowait()
            except queue.Empty:
                return
            try:
                if deliver:
                    gateway_cache.report_command_ack(
                        ack["node"],
                        ack["cmd_id"],
                        ack["result"],
                        rssi=ack.get("rssi"),
                        snr=ack.get("snr"),
                    )
                else:
                    self._persist_ack(ack)
            except Exception as exc:
                print(f"WARNING: ACK {ack.get('cmd_id')} not delivered: {exc}")
                try:
                    self._persist_ack(ack)
                except Exception as inner:
                    print(f"CRITICAL: lost ACK {ack.get('cmd_id')}: {inner}")

    def _consume(self, buffer: bytearray, chunk: bytes) -> bytearray:
        """Append ``chunk`` and dispatch every line it completes.

        The length cap bounds a single runaway line, so it is applied per line
        and to the trailing fragment. Applying it to the whole buffer instead
        threw away the completed packets queued in front of the garbage: one
        read carrying a long unterminated run plus good lines discarded the lot.
        """
        buffer.extend(chunk)
        while b"\n" in buffer:
            raw_line, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            if len(raw_line) > MAX_SERIAL_LINE:
                # Never hand this to _handle_line: parse_payload splits on every
                # comma and de-duplicates via routes in O(n^2), on this thread.
                print("WARNING: oversized serial line discarded")
                continue
            self._handle_line(raw_line.decode("utf-8", errors="ignore").strip())
        if len(buffer) > MAX_SERIAL_LINE:
            print("WARNING: oversized serial line fragment discarded")
            buffer.clear()
        return buffer

    def _record_drop(self, node: str | None, status: str, raw: str) -> None:
        """Tally a discarded packet and summarise it on a slow cadence.

        An out-of-order burst is usually a node that restarted without sending a
        boot_id, so the first one is printed in full: that sample is the whole
        reason the drop is worth knowing about.
        """
        key = f"{node or '?'}/{status}"
        first = key not in self.dropped
        self.dropped[key] = self.dropped.get(key, 0) + 1
        if first:
            print(f"WARNING: dropped LoRa payload ({status}) from {node}: {raw[:200]}")
        now = time.monotonic()
        if now - self._last_drop_report < DROP_REPORT_INTERVAL:
            return
        self._last_drop_report = now
        summary = ", ".join(f"{k}={v}" for k, v in sorted(self.dropped.items()))
        print(f"WARNING: dropped packets so far: {summary}")

    def _command_poller(self) -> None:
        """Run the whole command plane: ACK delivery and retries, command
        polling and dispatch.

        One escaping exception used to end this thread outright, and with it
        every command and every acknowledgement for the rest of the run —
        nothing in the log, nothing in /healthz, and the dashboard still showing
        commands as 'sent'. Failures are contained per cycle instead.

        They are deliberately not fatal. Telemetry is the gateway's job and does
        not pass through here, so a broken command plane is a degraded gateway,
        not a dead one.
        """
        failures = 0
        while not self.stop_event.is_set():
            try:
                self._drain_ack_queue(deliver=True)
                gateway_cache.flush_command_acks()
                for command in gateway_cache.fetch_pending_commands():
                    self._dispatch_command(command)
                failures = 0
            except Exception as exc:
                failures += 1
                # First one in full, then on a slow cadence: this cycle runs
                # every few seconds and a stuck fault would bury the log.
                if failures == 1 or failures % COMMAND_FAILURE_REPORT_EVERY == 0:
                    print(
                        f"WARNING: command plane cycle failed ({failures}x): {exc}"
                    )
            self.stop_event.wait(COMMAND_POLL_INTERVAL)
        # Shutting down: persist rather than block on the network, so anything
        # still in flight is picked up from SQLite on the next run.
        try:
            self._drain_ack_queue(deliver=False)
        except Exception as exc:
            print(f"CRITICAL: could not persist queued ACKs: {exc}")

    def _dispatch_command(self, command: dict[str, Any]) -> None:
        """Write one claimed command to serial, or say plainly that it did not.

        /api/commands/pending marks a command 'sent' when it hands it over, so
        every exit from here that is not a successful write leaves the operator
        watching a command that was never transmitted. Delivery stays
        at-most-once — nothing is requeued, since REBOOT and PROMOTE/DEMOTE are
        not safe to repeat until the node de-duplicates on cmd_id.
        """
        cmd_id, node, cmd, arg = (
            command.get("cmd_id"),
            command.get("node"),
            command.get("cmd"),
            command.get("arg") or "",
        )
        if not cmd_id or not node or not cmd:
            # Nothing to report against: without a cmd_id there is no row to fix.
            print(f"WARNING: skipping malformed pending command: {command}")
            return
        if self.serial_writer is None:
            self._report_dispatch_failure(node, cmd_id, "serial port not open")
            return
        line = f"CMD {cmd_id} {node} {cmd}"
        if arg:
            line += f" {arg}"
        try:
            self.serial_writer.write_line(line)
        except (ValueError, serial.SerialException) as exc:
            self._report_dispatch_failure(node, cmd_id, str(exc))
            return
        print(f"Dispatched command to serial: {line}")

    def _report_dispatch_failure(self, node: str, cmd_id: str, reason: str) -> None:
        print(f"WARNING: failed to dispatch command {cmd_id}: {reason}")
        gateway_cache.report_dispatch_failure(node, cmd_id, reason)

    def run(self) -> None:
        gateway_cache.configure(BACKEND_URL, LOCAL_DB, API_KEY)
        pending = gateway_cache.cache_count()
        quarantined = gateway_cache.dead_letter_count()
        print(
            f"Gateway cache ready: {pending} pending, "
            f"{quarantined} quarantined"
        )
        if not API_KEY:
            print("WARNING: LORA_API_KEY is empty; protected backend uploads will fail")
        # Daemon is safe now that the outbox is durable: killing the uploader
        # mid-flight loses an HTTP attempt, not a packet — the row stays pending
        # and goes out on the next run. A non-daemon thread would let a socket
        # with no deadline hold the process open past any join() we do here.
        uploader = threading.Thread(target=self._uploader, name="uploader", daemon=True)
        uploader.start()
        poller: threading.Thread | None = None
        try:
            self.serial_port = serial.Serial(COM_PORT, BAUD_RATE, timeout=0.25)
            self.serial_writer = SerialWriter(self.serial_port)
            print(f"LoRa gateway listening on {COM_PORT} at {BAUD_RATE} baud")
            poller = threading.Thread(
                target=self._command_poller, name="command-poller", daemon=True
            )
            poller.start()
            buffer = bytearray()
            while not self.stop_event.is_set():
                chunk = self.serial_port.read(self.serial_port.in_waiting or 1)
                if not chunk:
                    continue
                buffer = self._consume(buffer, chunk)
            if buffer:
                self._handle_line(buffer.decode("utf-8", errors="ignore").strip())
        except serial.SerialException as exc:
            # On this thread re-raising is the fail-fast: the traceback exits
            # non-zero by itself. _fail_fatally exists for the threads where it
            # cannot — see _uploader. TelemetryDeliveryError from enqueue is
            # deliberately left to propagate the same way.
            print(f"CRITICAL: serial connection failed: {exc}")
            raise
        except KeyboardInterrupt:
            print("Gateway shutdown requested")
        finally:
            self.stop_event.set()
            self.outbox_pending.set()  # wake the uploader out of its idle wait
            uploader.join(timeout=SHUTDOWN_DRAIN_SECONDS)
            if uploader.is_alive():
                print(f"WARNING: uploader still working after {SHUTDOWN_DRAIN_SECONDS}s")
            # Cleanup runs on the way out of a failure, so it must not be able
            # to replace the exception that caused it — losing the real cause,
            # and skipping the steps after whichever one raised.
            try:
                # The poller persists its own queued ACKs on the way out, but it
                # is a daemon: it can be killed before it gets there. Join it,
                # then do the same thing from here so the outcome does not
                # depend on a daemon winning a race with interpreter shutdown.
                if poller is not None:
                    poller.join(timeout=SHUTDOWN_DRAIN_SECONDS)
                self._drain_ack_queue(deliver=False)
            except Exception as exc:
                print(f"CRITICAL: could not persist queued ACKs on shutdown: {exc}")
            try:
                if self.serial_port and self.serial_port.is_open:
                    self.serial_port.close()
            except Exception as exc:
                print(f"WARNING: closing the serial port failed: {exc}")


def main() -> None:
    gateway = Gateway()
    gateway.run()
    if gateway.fatal_reason:
        # Non-zero so a service manager restarts us. A clean Ctrl-C, and a
        # serial port that simply went away, are now told apart by this alone.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
