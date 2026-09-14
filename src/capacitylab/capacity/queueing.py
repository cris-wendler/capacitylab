"""A deliberately simple CPU queueing model (M/M/c). Every output of this module is a *modeled* value.

Assumptions (surfaced in every option outcome):
- Statements are CPU-bound; I/O, buffer pool misses, and network time are not modeled.
- Arrivals are Poisson and service times exponential, pooled across statement types.
- Batch work is treated as a constant reservation of cores while it runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MODEL_ASSUMPTIONS = [
    "M/M/c CPU queueing model: CPU-bound statements, Poisson arrivals, pooled exponential service times.",
    "I/O latency, buffer pool misses and network time are not modeled.",
    "Batch jobs reserve a constant number of cores while running.",
]


def erlang_c(servers: int, offered_load: float) -> float:
    """Probability that an arrival waits, for `servers` servers and offered load `a` (Erlangs)."""
    if servers < 1:
        raise ValueError("servers must be >= 1")
    if offered_load <= 0:
        return 0.0
    if offered_load >= servers:
        return 1.0
    inv_b = 1.0
    for k in range(1, servers + 1):
        inv_b = 1.0 + inv_b * k / offered_load
    erlang_b = 1.0 / inv_b
    rho = offered_load / servers
    return erlang_b / (1.0 - rho + rho * erlang_b)


def wait_percentile_s(servers: int, arrival_rate: float, mean_service_s: float, pct: float = 95.0) -> float:
    """Waiting-time percentile for M/M/c; `math.inf` when the queue is unstable."""
    if arrival_rate <= 0 or mean_service_s <= 0:
        return 0.0
    mu = 1.0 / mean_service_s
    a = arrival_rate / mu
    if a >= servers:
        return math.inf
    c = erlang_c(servers, a)
    tail = 1.0 - pct / 100.0
    if c <= tail:
        return 0.0
    return math.log(c / tail) / (servers * mu - arrival_rate)


@dataclass(frozen=True)
class StatementLoad:
    fingerprint_id: str
    qps: float
    cpu_ms: float
    extra_latency_ms: float = 0.0


@dataclass(frozen=True)
class SlotEvaluation:
    cpu_cores_demand: float
    utilization_pct: float
    saturated: bool
    p95_latency_ms: dict[str, float]


def evaluate_slot(vcpu: int, loads: list[StatementLoad], reserved_cores: float = 0.0) -> SlotEvaluation:
    demand = sum(s.qps * s.cpu_ms / 1000.0 for s in loads)
    arrivals = sum(s.qps for s in loads)
    utilization = 100.0 * (demand + reserved_cores) / vcpu
    servers = max(1, math.floor(vcpu - reserved_cores))
    saturated = demand >= (vcpu - reserved_cores) or demand >= servers
    if saturated or arrivals == 0:
        wait_ms = math.inf if saturated else 0.0
    else:
        wait_ms = 1000.0 * wait_percentile_s(servers, arrivals, demand / arrivals)
    latency = {
        s.fingerprint_id: (math.inf if math.isinf(wait_ms) else round(s.cpu_ms + wait_ms + s.extra_latency_ms, 1))
        for s in loads
    }
    return SlotEvaluation(
        cpu_cores_demand=round(demand + reserved_cores, 3),
        utilization_pct=round(utilization, 1),
        saturated=saturated,
        p95_latency_ms=latency,
    )
