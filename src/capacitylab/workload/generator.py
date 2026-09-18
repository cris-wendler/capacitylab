# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seeded synthetic workload generator.

Produces per-slot, per-tenant statement rates. Identical inputs always produce identical output
(string seeds use SHA-512 internally, so results do not depend on PYTHONHASHSEED).
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import Literal

from pydantic import BaseModel, Field


class FingerprintSpec(BaseModel):
    id: str
    label: str
    kind: Literal["read", "write", "batch", "other"]
    cpu_ms_per_exec: float
    base_qps_by_tenant: dict[str, float]
    slo_id: str | None = None
    campaign_sensitive: bool = False
    release_sensitive: bool = False
    contends_with_batch: bool = False
    lock_wait_ms_during_batch: float = 0.0
    tables: list[str] = Field(default_factory=list)


class WorkloadSpec(BaseModel):
    seed: int
    noise_pct: float = 0.05
    diurnal: dict[int, float]  # hour of day -> multiplier on base rates (linear interpolation between hours)
    fingerprints: list[FingerprintSpec]

    def fingerprint(self, fid: str) -> FingerprintSpec:
        for f in self.fingerprints:
            if f.id == fid:
                return f
        raise KeyError(fid)


class Surge(BaseModel):
    """A multiplicative change applied to selected rates inside a slot range [start_slot, end_slot)."""

    start_slot: int
    end_slot: int
    multiplier: float
    tenant: str | None = None  # None applies to every tenant
    flag: Literal["campaign_sensitive", "release_sensitive"]


def _diurnal_factor(diurnal: dict[int, float], minute_of_day: int) -> float:
    hours = sorted(diurnal)
    h = minute_of_day / 60.0
    lower = max((x for x in hours if x <= h), default=hours[0])
    upper = min((x for x in hours if x >= h), default=hours[-1])
    if upper == lower:
        return diurnal[lower]
    frac = (h - lower) / (upper - lower)
    return diurnal[lower] + frac * (diurnal[upper] - diurnal[lower])


def generate(spec: WorkloadSpec, slot_start_minutes: list[int], surges: list[Surge] = ()) -> dict:
    """Return {"series": {fingerprint: {tenant: [qps per slot]}}, "digest": sha256 of the series}."""
    series: dict[str, dict[str, list[float]]] = {}
    for fp in spec.fingerprints:
        series[fp.id] = {}
        for tenant, base in sorted(fp.base_qps_by_tenant.items()):
            rng = random.Random(f"{spec.seed}:{fp.id}:{tenant}")
            values = []
            for slot, minute in enumerate(slot_start_minutes):
                qps = base * _diurnal_factor(spec.diurnal, minute)
                for s in surges:
                    applies = getattr(fp, s.flag) and (s.tenant is None or s.tenant == tenant)
                    if applies and s.start_slot <= slot < s.end_slot:
                        qps *= s.multiplier
                qps *= 1.0 + rng.uniform(-spec.noise_pct, spec.noise_pct)
                values.append(round(qps, 4))
            series[fp.id][tenant] = values
    digest = hashlib.sha256(json.dumps(series, sort_keys=True).encode()).hexdigest()
    return {"series": series, "digest": digest}
