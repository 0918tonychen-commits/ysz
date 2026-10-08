"""Serial-to-HTTP LoRa gateway."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import serial
from dotenv import load_dotenv

import gateway_cache
from lora_payload import MCountTracker, parse_payload

load_dotenv()

COM_PORT = os.environ.get("LORA_COM_PORT", "COM5")
BAUD_RATE = int(os.environ.get("LORA_BAUD_RATE", "115200"))
BACKEND_URL = os.environ.get("BACKEND_URL", "https://ysz.onrender.com/update")
MIRROR_BACKEND_URL = os.environ.get("MIRROR_BACKEND_URL", "")
LOCAL_DB = os.environ.get("GATEWAY_CACHE_DB", "gateway_cache.db")
API_KEY = os.environ.get("LORA_API_KEY", "")
SITE_BYPASS_TOKEN = os.environ.get("SITES_BYPASS_TOKEN", "")
SHUTDOWN_DRAIN_SECONDS = float(os.environ.get("SHUTDOWN_DRAIN_SECONDS", "10"))
OUTBOX_FAILURE_LIMIT = int(os.environ.get("OUTBOX_FAILURE_LIMIT", "5"))
MAX_SERIAL_LINE = 8192
CACHE_FLUSH_INTERVAL = float(os.environ.get("CACHE_FLUSH_INTERVAL_SECONDS", "30"))
DROP_REPORT_INTERVAL = float(os.environ.get("DROP_REPORT_INTERVAL_SECONDS", "300"))
SERIAL_RECONNECT_SECONDS = 3.0
SERIAL_RECONNECT_MAX_SECONDS = 30.0


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
        self.tracker = MCountTracker()
        self.serial_port: serial.Serial | None = None
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

    def _close_serial(self) -> None:
        port = self.serial_port
        self.serial_port = None
        try:
            if port is not None and port.is_open:
                port.close()
        except Exception as exc:
            print(f"WARNING: closing the serial port failed: {exc}")

    def _read_serial(self) -> None:
        """Reconnect the receiver without restarting delivery or node tracking."""
        buffer = bytearray()
        retry_delay = SERIAL_RECONNECT_SECONDS
        failures = 0
        while not self.stop_event.is_set():
            try:
                if self.serial_port is None:
                    self.serial_port = serial.Serial(
                        COM_PORT, BAUD_RATE, timeout=0.25, write_timeout=1.0
                    )
                    print(f"LoRa gateway listening on {COM_PORT} at {BAUD_RATE} baud")
                port = self.serial_port
                chunk = port.read(port.in_waiting or 1)
            except (serial.SerialException, OSError) as exc:
                self._close_serial()
                if buffer:
                    print("WARNING: discarded incomplete serial line after disconnect")
                    buffer.clear()
                failures += 1
                print(
                    f"WARNING: serial connection failed on {COM_PORT} ({failures}x): "
                    f"{exc}; retrying in {retry_delay:g}s"
                )
                if self.stop_event.wait(retry_delay):
                    break
                retry_delay = min(retry_delay * 2, SERIAL_RECONNECT_MAX_SECONDS)
                continue
            if chunk:
                if failures:
                    print(f"Serial connection recovered on {COM_PORT}; receiving data")
                failures = 0
                retry_delay = SERIAL_RECONNECT_SECONDS
                # Keep durable-cache/parse errors outside the serial retry
                # handler: failure to preserve telemetry is still fatal.
                buffer = self._consume(buffer, chunk)

    def run(self) -> None:
        gateway_cache.configure(
            BACKEND_URL,
            LOCAL_DB,
            API_KEY,
            MIRROR_BACKEND_URL,
            SITE_BYPASS_TOKEN,
        )
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
        try:
            print("Uplink-only gateway mode")
            self._read_serial()
        except KeyboardInterrupt:
            print("Gateway shutdown requested")
        finally:
            self.stop_event.set()
            self.outbox_pending.set()  # wake the uploader out of its idle wait
            uploader.join(timeout=SHUTDOWN_DRAIN_SECONDS)
            if uploader.is_alive():
                print(f"WARNING: uploader still working after {SHUTDOWN_DRAIN_SECONDS}s")
            self._close_serial()


def main() -> None:
    gateway = Gateway()
    gateway.run()
    if gateway.fatal_reason:
        # Durable-outbox failures remain fatal; transient serial faults retry.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
