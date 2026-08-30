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
        self.reader_failed = threading.Event()
        self.upload_queue: queue.Queue[tuple[str, dict[str, Any], float]] = queue.Queue(
            maxsize=QUEUE_SIZE
        )
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
            except Exception as exc:
                print(f"CRITICAL: telemetry delivery failed: {exc}")
            finally:
                self.upload_queue.task_done()

    def _handle_line(self, line: str) -> None:
        if "【ACK】" in line:
            ack = parse_ack_line(line)
            if ack:
                gateway_cache.report_command_ack(
                    ack["node"],
                    ack["cmd_id"],
                    ack["result"],
                    rssi=ack.get("rssi"),
                    snr=ack.get("snr"),
                )
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
            gateway_cache.flush_command_acks()
            for command in gateway_cache.fetch_pending_commands():
                self._dispatch_command(command)
            self.stop_event.wait(COMMAND_POLL_INTERVAL)

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
                try:
                    chunk = self.serial_port.read(self.serial_port.in_waiting or 1)
                except serial.SerialException as exc:
                    self.reader_failed.set()
                    print(f"CRITICAL: serial reader stopped: {exc}")
                    break
                if not chunk:
                    continue
                buffer.extend(chunk)
                if len(buffer) > MAX_SERIAL_LINE:
                    print("WARNING: oversized serial line discarded")
                    buffer.clear()
                    continue
                while b"\n" in buffer:
                    raw_line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    self._handle_line(raw_line.decode("utf-8", errors="ignore").strip())
            if buffer:
                self._handle_line(buffer.decode("utf-8", errors="ignore").strip())
        except serial.SerialException as exc:
            self.reader_failed.set()
            print(f"CRITICAL: unable to open serial port: {exc}")
        except KeyboardInterrupt:
            print("Gateway shutdown requested")
        finally:
            self.stop_event.set()
            self.upload_queue.join()
            uploader.join(timeout=5)
            if self.serial_port and self.serial_port.is_open:
                self.serial_port.close()


def main() -> None:
    Gateway().run()


if __name__ == "__main__":
    main()
