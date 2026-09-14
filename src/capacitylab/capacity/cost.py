"""Deterministic cost arithmetic. Rates come only from a rate card evidence item, never from a model."""

from __future__ import annotations

from pydantic import BaseModel


class RateCard(BaseModel):
    currency: str = "USD"
    instance_hourly: dict[str, float]
    storage_gib_month: float
    hours_per_month: float = 730.0
    source_note: str = ""

    def hourly(self, instance: str) -> float:
        if instance not in self.instance_hourly:
            raise ValueError(f"rate card has no hourly rate for {instance}")
        return self.instance_hourly[instance]


def _money(value: float) -> float:
    return round(value + 0.0, 2)


def instance_cost(card: RateCard, instance: str, hours: float, count: int = 1) -> float:
    return _money(card.hourly(instance) * hours * count)


def resize_delta_for_hours(card: RateCard, current: str, target: str, hours: float, count: int = 1) -> float:
    return _money((card.hourly(target) - card.hourly(current)) * hours * count)


def monthly_instance_cost(card: RateCard, instance: str, count: int = 1) -> float:
    return _money(card.hourly(instance) * card.hours_per_month * count)


def monthly_resize_delta(card: RateCard, current: str, target: str, count: int = 1) -> float:
    return _money((card.hourly(target) - card.hourly(current)) * card.hours_per_month * count)


def storage_cost_month(card: RateCard, gib: float) -> float:
    return _money(card.storage_gib_month * gib)
