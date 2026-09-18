# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pieces shared by the cloud imports (AWS, GCP, Azure): naming, slot series, safety checks and a small JSON client.

The GCP and Azure imports call the providers' REST APIs directly with this client, so the same code talks to a local
emulator (floci-gcp, floci-az) or, with `live`, to the real cloud with a bearer token from the provider's own
credential library. Only read calls are made.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from capacitylab.evidence.models import EvidenceItem
from capacitylab.evidence.normalize import redact_text

LOCAL_ENDPOINTS = ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")


class UnsafeCloudTarget(ValueError):
    """Raised when an import would reach a real cloud endpoint without being asked to."""


class CloudApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def check_target(live: bool, endpoint_url: str | None, cloud: str) -> None:
    if live and endpoint_url:
        raise UnsafeCloudTarget("--live reads a real account; do not also pass an emulator endpoint")
    if not live and not (endpoint_url or "").startswith(LOCAL_ENDPOINTS):
        raise UnsafeCloudTarget(f"refusing endpoint {endpoint_url!r}: without --live only a local {cloud} emulator "
                                "is allowed")


@dataclass
class CloudImport:
    items: list[EvidenceItem] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # parts that could not be read, with the reason


def tag_for(label: str | None, identifiers: list[str], fallback: str) -> tuple[str, str]:
    """Evidence id tag and display name: the label if given, otherwise a short hash of the identifiers."""
    if label and label.strip():
        clean = redact_text(label.strip())
        return re.sub(r"[^A-Z0-9]+", "-", clean.upper()).strip("-") or fallback, clean
    digest = hashlib.sha256("\n".join(sorted(identifiers)).encode()).hexdigest()[:10]
    return digest.upper(), f"{fallback.lower()} {digest}"


def slot_series(points: list[dict], slot_minutes: int = 15) -> dict:
    """Roll datapoints into fixed slots: the average of averages and the maximum of maxima per slot."""
    slots: dict[datetime, list[dict]] = defaultdict(list)
    for p in points:
        ts = p["Timestamp"].astimezone(UTC)
        slots[ts.replace(minute=ts.minute - ts.minute % slot_minutes, second=0, microsecond=0)].append(p)
    ordered = sorted(slots)
    return {
        "slot_minutes": slot_minutes,
        "slots": [s.strftime("%Y-%m-%d %H:%M") for s in ordered],
        "avg_by_slot": [round(sum(p["Average"] for p in slots[s]) / len(slots[s]), 2) for s in ordered],
        "max_by_slot": [round(max(p.get("Maximum", p["Average"]) for p in slots[s]), 2) for s in ordered],
    }


def parse_time(value: str) -> datetime:
    """RFC 3339 timestamps as the GCP and Azure APIs write them (nanoseconds are trimmed to microseconds)."""
    text = value.replace("Z", "+00:00")
    match = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", text)
    if match and match.group(2):
        text = match.group(1) + match.group(2)[:7] + match.group(3)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class JsonClient:
    """GET and POST JSON with an optional bearer token. Tests replace it with a recorded-response fake."""

    def __init__(self, token: Callable[[], str | None] | None = None, timeout_s: float = 20):
        self.token = token
        self.timeout_s = timeout_s

    def _call(self, method: str, url: str, params: dict | None = None, body: dict | None = None) -> dict:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        token = self.token() if self.token else None
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310 - checked target
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise CloudApiError(exc.code, detail) from exc
        return json.loads(raw) if raw else {}

    def get(self, url: str, params: dict | None = None) -> dict:
        return self._call("GET", url, params)

    def post(self, url: str, body: dict, params: dict | None = None) -> dict:
        return self._call("POST", url, params, body)

    def put(self, url: str, body: dict, params: dict | None = None) -> dict:
        return self._call("PUT", url, params, body)
