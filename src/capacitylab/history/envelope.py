# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a cluster usually does at this hour of this weekday, from its own recent history.

This is deliberately not a forecast. Nothing is trained, nothing is fitted, and no future value is predicted. The
envelope is descriptive statistics recomputed on read: group the stored samples into slots, keep the slots that fall
in the same hour of the same weekday, and report what that group reached - typically, at the high end, and at worst.
A capacity decision then sizes for the high end plus headroom, which is wrong in the direction that costs money
rather than the direction that drops checkouts.

Three properties matter more than accuracy:

* **Recency.** Samples are weighted by age with a half-life (7 days by default), so last week counts and last quarter
  barely does. Anything older than the window is dropped outright.
* **Drift.** A workload that changes fast makes its own history misleading. The last day is compared with the
  baseline before it, and when the level has clearly shifted the envelope is rebuilt with a short half-life so recent
  behaviour dominates. Fast change is detected, not predicted.
* **Honesty about thin evidence.** Every slot reports how many observations and how many distinct days it rests on,
  and says when that is too few to lean on.

Known events (a campaign, a release) are not modeled here: a date someone tells you about beats anything inferred
from history, so those stay scenario assumptions and multiply the envelope in the planner.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.history.store import History, Sample

WINDOW_DAYS = 28  # how much history is considered at all
HALF_LIFE_DAYS = 7.0  # a sample this old counts half as much as one from now
DRIFT_HALF_LIFE_DAYS = 1.5  # used instead once the level has shifted
DRIFT_RECENT_HOURS = 24
DRIFT_UP = 1.25  # recent high end this much above the baseline counts as a shift up
DRIFT_DOWN = 0.8
DRIFT_MIN_RECENT_SLOTS = 6
DRIFT_MIN_BASELINE_SLOTS = 24
THIN_DAYS = 3  # a slot resting on fewer distinct days than this is flagged

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class Slot:
    """One (weekday, time of day) group: what this cluster reached then, over the window."""

    weekday: int  # 0 = Monday
    minute_of_day: int
    observations: int  # slots that fell in this group
    days: int  # distinct calendar days they came from
    typical: float  # weighted median of the per-slot maxima
    high: float  # weighted p95 of the per-slot maxima
    peak: float  # highest value seen
    mean_of_averages: float

    @property
    def clock(self) -> str:
        return f"{self.minute_of_day // 60:02d}:{self.minute_of_day % 60:02d}"

    @property
    def label(self) -> str:
        return f"{WEEKDAYS[self.weekday]} {self.clock}"

    @property
    def thin(self) -> bool:
        return self.days < THIN_DAYS

    @property
    def spread(self) -> float:
        return round(self.high - self.typical, 2)

    def as_dict(self) -> dict:
        return {"weekday": WEEKDAYS[self.weekday], "time": self.clock, "observations": self.observations,
                "days": self.days, "typical": self.typical, "high": self.high, "peak": self.peak,
                "spread": self.spread, "mean_of_averages": self.mean_of_averages, "thin_evidence": self.thin}


@dataclass(frozen=True)
class Drift:
    """Whether the last day sits clearly away from the baseline before it."""

    detected: bool
    direction: str | None = None  # "up" | "down"
    ratio: float | None = None
    recent_high: float | None = None
    baseline_high: float | None = None
    recent_slots: int = 0
    baseline_slots: int = 0

    @property
    def note(self) -> str:
        if not self.detected:
            if self.recent_slots < DRIFT_MIN_RECENT_SLOTS or self.baseline_slots < DRIFT_MIN_BASELINE_SLOTS:
                return "not enough history on one side to compare the last day with the baseline"
            return f"the last {DRIFT_RECENT_HOURS} h sit within the baseline ({self.ratio:g}x its high end)"
        return (f"the last {DRIFT_RECENT_HOURS} h are {self.ratio:g}x the baseline high end ({self.baseline_high:g} "
                f"to {self.recent_high:g}), so the envelope was rebuilt with a {DRIFT_HALF_LIFE_DAYS:g}-day half-life")

    def as_dict(self) -> dict:
        return {"detected": self.detected, "direction": self.direction, "ratio": self.ratio,
                "recent_high": self.recent_high, "baseline_high": self.baseline_high,
                "recent_slots": self.recent_slots, "baseline_slots": self.baseline_slots, "note": self.note}


@dataclass(frozen=True)
class Envelope:
    cluster_key: str
    role: str
    metric: str
    unit: str
    slot_minutes: int
    half_life_days: float
    window_start: datetime
    window_end: datetime
    slots: dict[tuple[int, int], Slot] = field(default_factory=dict)
    drift: Drift = Drift(False)
    observations: int = 0
    days_covered: int = 0
    coverage: float = 0.0

    def at(self, when: datetime) -> Slot | None:
        """The slot covering this moment, or None when that hour of that weekday has never been collected."""
        when = when.astimezone(UTC)
        minute = (when.hour * 60 + when.minute) // self.slot_minutes * self.slot_minutes
        return self.slots.get((when.weekday(), minute))

    def over(self, start: datetime, end: datetime) -> list[tuple[datetime, Slot | None]]:
        """The envelope for each slot of a future window, for example the evening a campaign starts."""
        step = timedelta(minutes=self.slot_minutes)
        start = start.astimezone(UTC).replace(second=0, microsecond=0)
        start -= timedelta(minutes=start.minute % self.slot_minutes)
        out, cursor = [], start
        while cursor < end.astimezone(UTC):
            out.append((cursor, self.at(cursor)))
            cursor += step
        return out

    def busiest(self, limit: int = 5) -> list[Slot]:
        return sorted(self.slots.values(), key=lambda s: (-s.high, s.weekday, s.minute_of_day))[:limit]

    def quietest(self, limit: int = 5) -> list[Slot]:
        return sorted(self.slots.values(), key=lambda s: (s.high, s.weekday, s.minute_of_day))[:limit]

    @property
    def thin(self) -> bool:
        return not self.slots or all(s.thin for s in self.slots.values())

    def to_evidence(self, *, evidence_id: str | None = None, name: str | None = None,
                    environment: str = "import") -> EvidenceItem:
        """The envelope as evidence an agent can cite: modeled from observed history, with its limits stated."""
        tag = re.sub(r"[^A-Z0-9]+", "", self.metric.upper())[:12] or "METRIC"
        label = name or self.cluster_key
        caveats = [
            "Descriptive statistics over recent history, not a prediction: it says what this hour of this weekday has "
            "reached before, weighted towards recent days.",
            "It carries no knowledge of anything planned. A campaign, release or migration ahead of this window is a "
            "scenario assumption, not part of these numbers.",
        ]
        if self.thin:
            caveats.append(f"Every slot rests on fewer than {THIN_DAYS} distinct days; treat the shape as provisional.")
        if self.drift.detected:
            caveats.append(f"The level has shifted: {self.drift.note}.")
        if self.coverage < 0.9:
            caveats.append(f"Collection covered {self.coverage:.0%} of the window, so some periods are missing "
                           "entirely rather than quiet.")
        return EvidenceItem(
            id=evidence_id or f"EV-ENV-{tag}-{self.cluster_key.replace('-', '')}",
            kind=EvidenceKind.METRIC_SUMMARY,
            title=f"{self.metric} envelope by hour and weekday, last {self.window_days:g} days ({label})",
            provenance=Provenance.MODELED, synthetic=False, environment=environment,
            source=f"history:{self.cluster_key}:{self.role}",
            method=(f"{self.slot_minutes}-minute slots grouped by weekday and time of day; per-slot maximum; "
                    f"weighted median and p95 with a {self.half_life_days:g}-day half-life; "
                    f"{WINDOW_DAYS}-day window"),
            window_start=self.window_start, window_end=self.window_end,
            data={
                "metric": self.metric, "unit": self.unit, "node": self.role, "slot_minutes": self.slot_minutes,
                "half_life_days": self.half_life_days, "observations": self.observations,
                "days_covered": self.days_covered, "coverage": self.coverage,
                "drift": self.drift.as_dict(),
                "busiest_slots": [s.as_dict() for s in self.busiest()],
                "quietest_slots": [s.as_dict() for s in self.quietest()],
                "slots": [s.as_dict() for s in sorted(self.slots.values(),
                                                      key=lambda s: (s.weekday, s.minute_of_day))],
            },
            caveats=caveats)

    @property
    def window_days(self) -> float:
        return round((self.window_end - self.window_start).total_seconds() / 86400, 2)


def _slot_values(samples: list[Sample], slot_minutes: int) -> dict[datetime, tuple[float, float]]:
    """Roll raw periods into fixed slots: {slot start: (average of averages, maximum of maxima)}."""
    grouped: dict[datetime, list[Sample]] = defaultdict(list)
    for s in samples:
        ts = s.ts.astimezone(UTC).replace(second=0, microsecond=0)
        grouped[ts - timedelta(minutes=ts.minute % slot_minutes)].append(s)
    return {slot: (round(sum(p.avg for p in points) / len(points), 4), round(max(p.max for p in points), 4))
            for slot, points in grouped.items()}


def _weighted_quantile(pairs: list[tuple[float, float]], quantile: float) -> float:
    """Nearest-rank quantile over (value, weight) pairs: the first value whose cumulative weight passes it."""
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    if total <= 0:
        return ordered[-1][0] if ordered else 0.0
    target, run = total * quantile, 0.0
    for value, weight in ordered:
        run += weight
        if run >= target:
            return value
    return ordered[-1][0]


def _weight(slot: datetime, now: datetime, half_life_days: float) -> float:
    age_days = max((now - slot).total_seconds() / 86400, 0.0)
    return math.exp(-math.log(2) * age_days / half_life_days)


def _detect_drift(slot_values: dict[datetime, tuple[float, float]], now: datetime) -> Drift:
    boundary = now - timedelta(hours=DRIFT_RECENT_HOURS)
    recent = [v[1] for slot, v in slot_values.items() if slot >= boundary]
    baseline = [v[1] for slot, v in slot_values.items() if slot < boundary]
    if len(recent) < DRIFT_MIN_RECENT_SLOTS or len(baseline) < DRIFT_MIN_BASELINE_SLOTS:
        return Drift(False, recent_slots=len(recent), baseline_slots=len(baseline))
    recent_high = _weighted_quantile([(v, 1.0) for v in recent], 0.95)
    baseline_high = _weighted_quantile([(v, 1.0) for v in baseline], 0.95)
    if baseline_high <= 0:
        return Drift(False, recent_slots=len(recent), baseline_slots=len(baseline))
    ratio = round(recent_high / baseline_high, 3)
    direction = "up" if ratio >= DRIFT_UP else "down" if ratio <= DRIFT_DOWN else None
    return Drift(direction is not None, direction, ratio, round(recent_high, 2), round(baseline_high, 2),
                 len(recent), len(baseline))


def _slots(slot_values: dict[datetime, tuple[float, float]], now: datetime, slot_minutes: int,
           half_life_days: float) -> dict[tuple[int, int], Slot]:
    groups: dict[tuple[int, int], list[tuple[datetime, float, float]]] = defaultdict(list)
    for slot, (average, maximum) in slot_values.items():
        minute = (slot.hour * 60 + slot.minute) // slot_minutes * slot_minutes
        groups[(slot.weekday(), minute)].append((slot, average, maximum))
    out = {}
    for (weekday, minute), points in groups.items():
        weighted = [(maximum, _weight(slot, now, half_life_days)) for slot, _, maximum in points]
        out[(weekday, minute)] = Slot(
            weekday=weekday, minute_of_day=minute, observations=len(points),
            days=len({slot.date() for slot, _, _ in points}),
            typical=round(_weighted_quantile(weighted, 0.5), 2),
            high=round(_weighted_quantile(weighted, 0.95), 2),
            peak=round(max(maximum for _, _, maximum in points), 2),
            mean_of_averages=round(sum(a for _, a, _ in points) / len(points), 2))
    return out


def build_envelope(history: History, cluster_key: str, metric: str, role: str = "writer", *,
                   now: datetime | None = None, window_days: int = WINDOW_DAYS, slot_minutes: int = 60,
                   half_life_days: float = HALF_LIFE_DAYS) -> Envelope:
    """Build the envelope for one metric on one node from the stored history."""
    if 1440 % slot_minutes or slot_minutes < 5:
        raise ValueError("slot_minutes must divide a day and be at least 5")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = now - timedelta(days=window_days)
    samples = history.samples(cluster_key, metric, role, since=start, until=now)
    slot_values = _slot_values(samples, slot_minutes)
    drift = _detect_drift(slot_values, now)
    effective_half_life = DRIFT_HALF_LIFE_DAYS if drift.detected else half_life_days
    return Envelope(
        cluster_key=cluster_key, role=role, metric=metric, unit=history.unit(cluster_key, metric, role),
        slot_minutes=slot_minutes, half_life_days=effective_half_life, window_start=start, window_end=now,
        slots=_slots(slot_values, now, slot_minutes, effective_half_life), drift=drift,
        observations=len(samples), days_covered=len({s.ts.date() for s in samples}),
        coverage=history.coverage(cluster_key, metric, role, since=start, until=now))
