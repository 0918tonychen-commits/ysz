"""LoRa payload parsing and per-node MCOUNT tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
from typing import Any, Literal

ParseStatus = Literal["valid", "duplicate", "out_of_order", "incomplete", "invalid"]

NODE_RE = re.compile(r"^s\d{2,}$", re.IGNORECASE)
SOURCE_RE = re.compile(r"^(s\d{2,})_m(\d+)$", re.IGNORECASE)
VIA_RE = re.compile(r"(?:^|_)via_([sl]\d{2,})(?=_|$)", re.IGNORECASE)
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
NUMBER_RE = re.compile(r"^-?(?:\d+(?:\.\d+)?|\.\d+)$")
RSSI_RE = re.compile(r"(?:^|,)\s*rssi\s*[:=,]\s*(-?\d+)(?=\s*,|$)", re.IGNORECASE)
SNR_RE = re.compile(
    r"(?:^|,)\s*snr\s*[:=,]\s*(-?(?:\d+(?:\.\d+)?|\.\d+))(?=\s*,|$)",
    re.IGNORECASE,
)
INLINE_PAIR_RE = re.compile(
    r"^([a-z][a-z0-9_]*)\s*[:=]\s*(-?(?:\d+(?:\.\d+)?|\.\d+))$",
    re.IGNORECASE,
)

META_KEYS = {
    "mcount",
    "msg",
    "rssi",
    "gw_rssi",
    "snr",
    "gw_snr",
    "hop_rssi",
    "hop_snr",
    "loss",
    "level",
    "via",
}
SENSOR_ALIASES = {
    "t": "temperature",
    "temp": "temperature",
    "h": "humidity",
    "hum": "humidity",
    "c": "co2",
    "v": "voltage",
    "volt": "voltage",
}
META_ALIASES = {
    "msg": "mcount",
    "gw_rssi": "rssi",
    "gw_snr": "snr",
}
NUMERIC_META_KEYS = {
    "mcount",
    "rssi",
    "snr",
    "hop_rssi",
    "hop_snr",
    "loss",
    "level",
}


@dataclass
class MCountTracker:
    """Tracks ordering and loss. A long quiet period permits a reboot-to-zero."""

    reboot_grace_seconds: float = 300.0
    last: dict[str, int] = field(default_factory=dict)
    last_seen: dict[str, float] = field(default_factory=dict)
    received: dict[str, int] = field(default_factory=dict)
    lost: dict[str, int] = field(default_factory=dict)

    def classify(self, node: str, mcount: int, now: float | None = None) -> ParseStatus:
        now = time.monotonic() if now is None else now
        previous = self.last.get(node)
        if previous is None:
            return "valid"
        if mcount == previous:
            return "duplicate"
        if mcount < previous:
            quiet = now - self.last_seen.get(node, now)
            if mcount <= 2 and quiet >= self.reboot_grace_seconds:
                return "valid"
            return "out_of_order"
        return "valid"

    def commit(self, node: str, mcount: int, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        previous = self.last.get(node)
        if previous is not None and mcount > previous + 1:
            self.lost[node] = self.lost.get(node, 0) + mcount - previous - 1
        self.received[node] = self.received.get(node, 0) + 1
        self.lost.setdefault(node, 0)
        self.last[node] = mcount
        self.last_seen[node] = now
        total = self.received[node] + self.lost[node]
        return round(self.lost[node] * 100.0 / total, 1) if total else 0.0


DEFAULT_TRACKER = MCountTracker()


def parse_payload(
    raw: str, tracker: MCountTracker | None = DEFAULT_TRACKER
) -> tuple[str | None, dict[str, Any] | None, ParseStatus]:
    """Return ``(node_id, {"data": ..., "meta": ...}, status)``."""
    if not isinstance(raw, str) or not raw.strip():
        return None, None, "invalid"

    tokens = [part.strip() for part in raw.strip().split(",")]
    source_index = None
    source_match = None
    for index, token in enumerate(tokens):
        match = SOURCE_RE.fullmatch(token)
        if match:
            source_index, source_match = index, match
            break
    if source_match is None:
        return None, None, "invalid"

    node = source_match.group(1).lower()
    mcount = int(source_match.group(2))
    data: dict[str, float] = {}
    meta: dict[str, Any] = {"mcount": mcount, "via": []}

    for token in tokens:
        for via in VIA_RE.findall(token):
            route = via.lower()
            if route != node and route not in meta["via"]:
                meta["via"].append(route)

    # Accept both ``key,value`` and ``key:value`` forms used by old firmware.
    for index, token in enumerate(tokens):
        key = token.lower()
        value: str | None = None
        inline = INLINE_PAIR_RE.fullmatch(token)
        if inline:
            key, value = inline.group(1).lower(), inline.group(2)
        elif index + 1 < len(tokens) and NUMBER_RE.fullmatch(tokens[index + 1]):
            value = tokens[index + 1]
        canonical = META_ALIASES.get(key, key)
        if canonical in NUMERIC_META_KEYS and value is not None:
            number = float(value)
            meta[canonical] = int(number) if canonical == "mcount" else number

    # Also support an explicit ``via,s02`` pair.
    for index, token in enumerate(tokens[:-1]):
        if token.lower() == "via" and NODE_RE.fullmatch(tokens[index + 1]):
            route = tokens[index + 1].lower()
            if route != node and route not in meta["via"]:
                meta["via"].append(route)

    index = source_index + 1
    while index + 1 < len(tokens):
        key = tokens[index].lower()
        value = tokens[index + 1]
        if key in {"rssi", "snr"} or key.startswith("via_"):
            index += 1
            continue
        if KEY_RE.fullmatch(key) and key not in META_KEYS and NUMBER_RE.fullmatch(value):
            data[SENSOR_ALIASES.get(key, key)] = float(value)
            index += 2
        else:
            index += 1

    rssi_match = RSSI_RE.search(raw)
    snr_match = SNR_RE.search(raw)
    if rssi_match:
        meta["rssi"] = int(rssi_match.group(1))
    if snr_match:
        meta["snr"] = float(snr_match.group(1))

    # The source suffix is authoritative even if a legacy msg/mcount pair differs.
    meta["mcount"] = mcount

    if not data:
        return node, None, "incomplete"

    if tracker is not None:
        status = tracker.classify(node, mcount)
        if status != "valid":
            return node, None, status
        meta["loss"] = tracker.commit(node, mcount)
    else:
        meta["loss"] = 0.0

    return node, {"data": data, "meta": meta}, "valid"
