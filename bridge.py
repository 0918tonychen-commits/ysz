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
QUEUE_SIZE = int(os.environ.get("UPLOAD_QUEUE_SIZE", "256"))
ACK_QUEUE_SIZE = int(os.environ.get("ACK_QUEUE_SIZE", "128"))
SHUTDOWN_DRAIN_SECONDS = float(os.environ.get("SHUTDOWN_DRAIN_SECONDS", "10"))
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
        # non-main thread leaves the process status at 0, and killing the
        # uploader strands upload_queue.join() on task_done calls that will
        # never come. See _fail_fatally.
        self.fatal_reason: str | None = None
        self._fatal_lock = threading.Lock()
        self.upload_queue: queue.Queue[tuple[str, dict[str, Any], float]] = queue.Queue(
            maxsize=QUEUE_SIZE
        )
        # Acknowledgements take the same decoupled route as telemetry: the
        # reader thread must never wait on the network. See _queue_ack.
        self.ack_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=ACK_QUEUE_SIZE)
        self.tracker = MCountTracker()
        self.serial_port: serial.Serial | None = None
        self.serial_writer: SerialWriter | None = None
        # Dropped packets used to vanish without a trace, which hid exactly the
        # restart behaviour we need to see. Count them and report periodically.
        self.dropped: dict[str, int] = {}
        self._last_drop_report = 0.0

    def enqueue(self, node: str, payload: dict[str, Any], recorded_at: float) -> None:
        try:
            self.upload_queue.put_nowait((node, payload, recorded_at))
        except queue.Full:
            if not gateway_cache.save_to_local_cache(
                node, payload, recorded_at=recorded_at, last_error="upload queue full"
            ):
                raise gateway_cache.TelemetryDeliveryError(
                    "upload queue full and SQLite fallback failed"
                )

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
        while not self.stop_event.is_set() or not self.upload_queue.empty():
            try:
                node, payload, recorded_at = self.upload_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                uploaded = gateway_cache.upload_telemetry(
                    node, payload, recorded_at=recorded_at
                )
                mcount = payload.get("meta", {}).get("mcount", "?")
                if uploaded:
                    print(f"UPLOADED: node={node} mcount={mcount}")
                else:
                    print(f"CACHED: node={node} mcount={mcount} waiting for retry")
            except gateway_cache.TelemetryDeliveryError as exc:
                # Neither the backend nor SQLite will take it. Carrying on would
                # drop every packet from here to the end of the run, silently.
                self._fail_fatally(str(exc))
            except Exception as exc:
                print(f"CRITICAL: telemetry delivery failed: {exc}")
            finally:
                self.upload_queue.task_done()

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
            gateway_cache.save_command_ack_for_retry(
                ack["node"],
                ack["cmd_id"],
                ack["result"],
                rssi=ack.get("rssi"),
                snr=ack.get("snr"),
            )

    def _drain_upload_queue_to_cache(self) -> int:
        """Persist queued events the uploader did not get to before shutdown."""
        stranded = 0
        while True:
            try:
                node, payload, recorded_at = self.upload_queue.get_nowait()
            except queue.Empty:
                return stranded
            try:
                if gateway_cache.save_to_local_cache(
                    node,
                    payload,
                    recorded_at=recorded_at,
                    last_error="gateway shut down before upload",
                ):
                    stranded += 1
                else:
                    print(f"CRITICAL: lost queued event from {node} on shutdown")
            finally:
                self.upload_queue.task_done()

    def _drain_ack_queue(self, *, deliver: bool) -> None:
        """Report queued ACKs, or persist them when shutting down."""
        while True:
            try:
                ack = self.ack_queue.get_nowait()
            except queue.Empty:
                return
            if deliver:
                gateway_cache.report_command_ack(
                    ack["node"],
                    ack["cmd_id"],
                    ack["result"],
                    rssi=ack.get("rssi"),
                    snr=ack.get("snr"),
                )
            else:
                gateway_cache.save_command_ack_for_retry(
                    ack["node"],
                    ack["cmd_id"],
                    ack["result"],
                    rssi=ack.get("rssi"),
                    snr=ack.get("snr"),
                )

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

    def _cache_flusher(self) -> None:
        """Drain the SQLite backlog even while no packets are arriving.

        ``upload_telemetry`` only flushes as a side effect of a new uplink, so a
        backlog built up while the backend was down would otherwise sit there
        until the next packet — which may never come if the nodes went quiet too.
        """
        while not self.stop_event.is_set():
            self.stop_event.wait(CACHE_FLUSH_INTERVAL)
            if self.stop_event.is_set():
                return
            try:
                sent = gateway_cache.flush_local_cache()
            except Exception as exc:
                print(f"WARNING: idle cache flush failed: {exc}")
                continue
            if sent:
                print(f"Flushed {sent} cached events from the backlog")

    def _command_poller(self) -> None:
        while not self.stop_event.is_set():
            self._drain_ack_queue(deliver=True)
            gateway_cache.flush_command_acks()
            for command in gateway_cache.fetch_pending_commands():
                self._dispatch_command(command)
            self.stop_event.wait(COMMAND_POLL_INTERVAL)
        # Shutting down: persist rather than block on the network, so anything
        # still in flight is picked up from SQLite on the next run.
        self._drain_ack_queue(deliver=False)

    def _dispatch_command(self, command: dict[str, Any]) -> None:
        if self.serial_writer is None:
            return
        cmd_id, node, cmd, arg = (
            command.get("cmd_id"),
            command.get("node"),
            command.get("cmd"),
            command.get("arg") or "",
        )
        if not cmd_id or not node or not cmd:
            print(f"WARNING: skipping malformed pending command: {command}")
            return
        line = f"CMD {cmd_id} {node} {cmd}"
        if arg:
            line += f" {arg}"
        try:
            self.serial_writer.write_line(line)
            print(f"Dispatched command to serial: {line}")
        except (ValueError, serial.SerialException) as exc:
            print(f"WARNING: failed to dispatch command {cmd_id}: {exc}")

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
        uploader = threading.Thread(target=self._uploader, name="uploader", daemon=False)
        uploader.start()
        flusher = threading.Thread(
            target=self._cache_flusher, name="cache-flusher", daemon=True
        )
        flusher.start()
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
            # Bounded: upload_queue.join() waits on task_done forever, so a dead
            # uploader used to hang shutdown outright. Give it a window, then
            # put whatever is left somewhere durable instead of losing it.
            uploader.join(timeout=SHUTDOWN_DRAIN_SECONDS)
            if uploader.is_alive():
                print(
                    f"WARNING: uploader still working after {SHUTDOWN_DRAIN_SECONDS}s"
                )
            stranded = self._drain_upload_queue_to_cache()
            if stranded:
                print(f"Persisted {stranded} events that had not been uploaded yet")
            if self.serial_port and self.serial_port.is_open:
                self.serial_port.close()


def main() -> None:
    gateway = Gateway()
    gateway.run()
    if gateway.fatal_reason:
        # Non-zero so a service manager restarts us. A clean Ctrl-C, and a
        # serial port that simply went away, are now told apart by this alone.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
