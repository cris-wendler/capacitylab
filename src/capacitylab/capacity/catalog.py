"""Instance class catalog (vCPU and memory per class, from public instance specifications)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstanceClass:
    name: str
    vcpu: int
    memory_gib: int


CATALOG: dict[str, InstanceClass] = {
    c.name: c
    for c in [
        InstanceClass("db.r6g.large", 2, 16),
        InstanceClass("db.r6g.xlarge", 4, 32),
        InstanceClass("db.r6g.2xlarge", 8, 64),
        InstanceClass("db.r6g.4xlarge", 16, 128),
        InstanceClass("db.r6g.8xlarge", 32, 256),
        InstanceClass("db.r6g.12xlarge", 48, 384),
        InstanceClass("db.r6g.16xlarge", 64, 512),
    ]
}

ORDERED = list(CATALOG)


def get_instance(name: str) -> InstanceClass:
    try:
        return CATALOG[name]
    except KeyError as exc:
        raise ValueError(f"unknown instance class {name!r}; known: {', '.join(ORDERED)}") from exc


def buffer_pool_gib(instance: InstanceClass, fraction: float = 0.75) -> float:
    """Approximate InnoDB buffer pool size. The fraction is a configurable assumption, not a measurement."""
    return round(instance.memory_gib * fraction, 1)
