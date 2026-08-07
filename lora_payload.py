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
ACK_FIELD_RE = re.compile(r"(\w+)\s*=\s*([^,]+)")
# boot_id is the one non-numeric meta field: a random value minted at every cold
# boot, which is what separates a genuine restart from a dropped packet.
BOOT_ID_RE = re.compile(r"^[0-9A-Fa-f]{4,32}$")
BOOT_INLINE_RE = re.compile(
    r"^boot(?:_id)?\s*[:=]\s*([0-9A-Fa-f]{4,32})$", re.IGNORECASE
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
    "r_in",
    "loss",
    "level",
    "fallback",
    "via",
    "boot",
    "boot_id",
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
    "r_in": "hop_rssi",
    "boot": "boot_id",
}
NUMERIC_META_KEYS = {
    "mcount",
    "rssi",
    "snr",
    "hop_rssi",
    "hop_snr",
    "loss",
    "level",
    "fallback",
}


@dataclass
class MCountTracker:
    """Tracks ordering and loss.

    A node's mcount restarts at 1 on every reset, so a restart looks exactly
    like a backwards jump. Two things rescue it: an explicit ``boot_id``, which
    is authoritative, and — for firmware that does not send one — a long quiet
    period followed by a near-zero counter.
    """

    reboot_grace_seconds: float = 300.0
    last: dict[str, int] = field(default_factory=dict)
    last_seen: dict[str, float] = field(default_factory=dict)
    received: dict[str, int] = field(default_factory=dict)
    lost: dict[str, int] = field(default_factory=dict)
    boot: dict[str, str] = field(default_factory=dict)

    def rebooted(self, node: str, boot_id: str | None) -> bool:
        """True when boot_id proves the node restarted since the last packet."""
        if boot_id is None:
            return False
        previous = self.boot.get(node)
        return previous is not None and previous != boot_id

    def classify(
        self,
        node: str,
        mcount: int,
        now: float | None = None,
        boot_id: str | None = None,
    ) -> ParseStatus:
        now = time.monotonic() if now is None else now
        # A new boot_id explains any counter jump, in either direction, with no
        # dependence on how long the node was away.
        if self.rebooted(node, boot_id):
            return "valid"
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

    def commit(
        self,
        node: str,
        mcount: int,
        now: float | None = None,
        boot_id: str | None = None,
    ) -> float:
        now = time.monotonic() if now is None else now
        previous = self.last.get(node)
        # Packets the node never sent because it was rebooting are not losses;
        # counting them would bury the real loss rate under restart noise.
        restarted = self.rebooted(node, boot_id) or (
            previous is not None and mcount < previous
        )
        if not restarted and previous is not None and mcount > previous + 1:
            self.lost[node] = self.lost.get(node, 0) + mcount - previous - 1
        self.received[node] = self.received.get(node, 0) + 1
        self.lost.setdefault(node, 0)
        self.last[node] = mcount
        self.last_seen[node] = now
        if boot_id is not None:
            self.boot[node] = boot_id
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

    # ``boot,A3F19C2B`` or ``boot=A3F19C2B``; the first one wins so a relay
    # cannot overwrite the originating node's value.
    for index, token in enumerate(tokens):
        inline = BOOT_INLINE_RE.fullmatch(token)
        if inline:
            meta["boot_id"] = inline.group(1).upper()
            break
        if META_ALIASES.get(token.lower(), token.lower()) == "boot_id":
            if index + 1 < len(tokens) and BOOT_ID_RE.fullmatch(tokens[index + 1]):
                meta["boot_id"] = tokens[index + 1].upper()
                break

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
            if canonical == "mcount":
                meta[canonical] = int(number)
                continue
            # A relayed packet gains one rssi/snr pair per hop with the same
            # key name; keep every hop instead of letting the last one win.
            existing = meta.get(canonical)
            if canonical in meta:
                if isinstance(existing, list):
                    existing.append(number)
                else:
                    meta[canonical] = [existing, number]
            else:
                meta[canonical] = number

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
        if META_ALIASES.get(key, key) == "boot_id":
            index += 2  # skip the hex value; it would pass KEY_RE as a sensor
            continue
        if KEY_RE.fullmatch(key) and key not in META_KEYS and NUMBER_RE.fullmatch(value):
            data[SENSOR_ALIASES.get(key, key)] = float(value)
            index += 2
        else:
            index += 1

    # Fallback for formats the token loop above didn't already capture; must
    # not override it, or a later single regex match would discard the
    # per-hop list built above for a relayed packet.
    rssi_match = RSSI_RE.search(raw)
    snr_match = SNR_RE.search(raw)
    if rssi_match and "rssi" not in meta:
        meta["rssi"] = int(rssi_match.group(1))
    if snr_match and "snr" not in meta:
        meta["snr"] = float(snr_match.group(1))

    # The source suffix is authoritative even if a legacy msg/mcount pair differs.
    meta["mcount"] = mcount

    if not data:
        return node, None, "incomplete"

    boot_id = meta.get("boot_id")
    if tracker is not None:
        status = tracker.classify(node, mcount, boot_id=boot_id)
        if status != "valid":
            return node, None, status
        # Numeric, not bool: the backend's meta validation only accepts numbers.
        meta["rebooted"] = 1.0 if tracker.rebooted(node, boot_id) else 0.0
        meta["loss"] = tracker.commit(node, mcount, boot_id=boot_id)
    else:
        meta["loss"] = 0.0

    return node, {"data": data, "meta": meta}, "valid"


def parse_ack_line(line: str) -> dict[str, Any] | None:
    """Parse an L1 gateway ACK line: ``from=s03, cmdId=C001, result=OK, rssi=-65, snr=6.1``.

    Unlike sensor uplinks this is a plain ``key=value`` list, not the
    positional comma format ``parse_payload`` expects.
    """
    fields = dict(ACK_FIELD_RE.findall(line))
    node = fields.get("from", "").strip().lower()
    cmd_id = fields.get("cmdId", "").strip()
    result = fields.get("result", "").strip()
    if not node or not NODE_RE.fullmatch(node) or not cmd_id or not result:
        return None
    ack: dict[str, Any] = {"node": node, "cmd_id": cmd_id, "result": result}
    rssi = fields.get("rssi", "").strip()
    if rssi and NUMBER_RE.fullmatch(rssi):
        ack["rssi"] = int(float(rssi))
    snr = fields.get("snr", "").strip()
    if snr and NUMBER_RE.fullmatch(snr):
        ack["snr"] = float(snr)
    return ack
