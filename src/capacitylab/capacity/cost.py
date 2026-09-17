# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic cost arithmetic. Rates come only from a rate card evidence item, never from a model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from capacitylab.evidence.models import EvidenceItem

AWS_PRICE_SOURCES = ("aws:pricing:", "floci:pricing:", "azure:retail-prices:")  # imported cloud prices


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


@dataclass
class RateCardChoice:
    card: RateCard
    evidence_ids: list[str]
    note: str
    prices_from_aws: bool = False
    missing_classes: list[str] = field(default_factory=list)


def choose_rate_card(items: list[EvidenceItem], needed_classes: set[str]) -> RateCardChoice | None:
    """Pick the prices the cost model uses.

    On-demand prices imported from AWS win when they cover every instance class the options need; storage, which
    they do not include, still comes from the scenario's rate card. Otherwise the scenario's rate card is used and the
    note says which classes the AWS prices were missing."""
    aws = [i for i in items if i.source.startswith(AWS_PRICE_SOURCES)]
    base = next((i for i in items if not i.source.startswith(AWS_PRICE_SOURCES)), None)
    for item in reversed(aws):  # the most recently attached import first
        prices = item.data.get("instance_hourly", {})
        missing = sorted(needed_classes - set(prices))
        if missing:
            continue
        storage = float(base.data.get("storage_gib_month", 0.0)) if base else 0.0
        card = RateCard(currency=item.data.get("currency", "USD"), instance_hourly=prices, storage_gib_month=storage,
                        hours_per_month=item.data.get("hours_per_month", 730.0),
                        source_note=item.data.get("source_note", ""))
        storage_note = (f"storage rate from {base.id}" if base
                        else "no storage rate, so index storage is not priced")
        return RateCardChoice(card, [item.id] + ([base.id] if base else []),
                              f"Instance prices from {item.id} ({item.method}); {storage_note}.", prices_from_aws=True)
    if base is None:
        return None
    note = f"Prices from {base.id}."
    gaps = sorted({c for i in aws for c in needed_classes - set(i.data.get("instance_hourly", {}))})
    if aws:
        note = (f"Prices from {base.id}: the AWS prices in {', '.join(i.id for i in aws)} do not cover "
                f"{', '.join(gaps)}.")
    return RateCardChoice(RateCard(**base.data), [base.id], note, missing_classes=gaps)
