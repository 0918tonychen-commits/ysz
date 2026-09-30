"""Exercise the deployed HTTP path with one real telemetry event.

The check intentionally uses only the Python standard library so it can run
from a host machine or from CI without adding another dependency.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid


BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:10000").rstrip("/")
API_KEY = os.environ.get("LORA_API_KEY", "ysz-local-development-only")


def request(
    path: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    authenticated: bool = False,
) -> tuple[int, dict[str, object]]:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if authenticated:
        headers["X-API-Key"] = API_KEY
    outbound = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(outbound, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.load(exc)
        finally:
            exc.close()
        return exc.code, payload


def assert_status(
    actual: tuple[int, dict[str, object]], expected: int, label: str
) -> dict[str, object]:
    status, payload = actual
    if status != expected:
        raise RuntimeError(f"{label}: expected HTTP {expected}, got {status}: {payload}")
    return payload


def main() -> None:
    health = assert_status(request("/healthz"), 200, "health check")
    if health.get("database") != "ok":
        raise RuntimeError(f"health check returned an unexpected body: {health}")

    event = {
        "event_id": f"smoke-{uuid.uuid4()}",
        "node": "s99",
        "recorded_at": time.time(),
        "data": {"temperature": 25.5, "humidity": 60.0, "co2": 450.0},
        "meta": {"mcount": 1, "via": [], "rssi": -70, "snr": 7.5, "loss": 0},
    }

    assert_status(request("/update", method="POST", body=event), 401, "authentication")
    created = assert_status(
        request("/update", method="POST", body=event, authenticated=True),
        200,
        "telemetry insert",
    )
    if created.get("duplicate") is not False:
        raise RuntimeError(f"first insert was not reported as new: {created}")

    duplicate = assert_status(
        request("/update", method="POST", body=event, authenticated=True),
        200,
        "telemetry de-duplication",
    )
    if duplicate.get("duplicate") is not True:
        raise RuntimeError(f"second insert was not reported as duplicate: {duplicate}")

    latest = assert_status(request("/api/all_data"), 200, "latest telemetry")
    nodes = latest.get("nodes")
    if not isinstance(nodes, dict) or "s99" not in nodes:
        raise RuntimeError(f"inserted node is missing from latest telemetry: {latest}")

    print("PASS: health, authentication, insert, de-duplication, and readback")


if __name__ == "__main__":
    main()
