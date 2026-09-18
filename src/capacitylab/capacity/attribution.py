# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is causing the load in a slot: which statements, which tenants, and whether the batch job is running.

"The writer is saturated from 19:00" is a symptom. The remedy depends entirely on what is behind it, and the
difference is not subtle: a batch job that can move costs nothing to fix, one statement that dominates is worth an
index or a rewrite, one tenant taking far more than they pay for is a commercial conversation, and load spread evenly
across everything is the only case where buying capacity is the honest answer.

This module decomposes the same demand the capacity model uses - CPU cores per slot, from each statement's rate and
its cost per execution - so the shares add up to the modeled utilization rather than to a separate estimate.

What it does not do is decide. It reports the shares and which remedies the shape makes worth considering, with the
thresholds it applied, and leaves the choice to the review. It also cannot see below the statement: which service or
code path issues a statement is not in this evidence, so attribution stops at the tenant and the fingerprint.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from capacitylab.capacity.options import (  # noqa: PLC2701 - same package, one model
    ModelContext,
    _Plan,
    _resolve,
)

# How sure the reading is. A subject that dominates every breaching slot by a wide margin is a different claim from
# one that scrapes past the threshold in half of them, and a recommendation should not sound the same in both cases.
CLEAR_LEAD_PCT = 15.0  # points ahead of the next subject
CLEAR_MARGIN_PCT = 15.0  # points above the threshold that named it
CONSISTENT_SHARE = 0.8  # share of the slots it leads in
PRESENT_SHARE = 0.5

DOMINANT_STATEMENT_PCT = 40.0  # one statement at or above this share is worth fixing before buying capacity
DOMINANT_TENANT_PCT = 40.0
BATCH_SHARE_PCT = 15.0  # a batch job holding this much of a breached slot is worth moving first


@dataclass(frozen=True)
class Share:
    name: str
    cores: float
    pct: float


@dataclass(frozen=True)
class SlotAttribution:
    """Where one slot's CPU demand comes from. Shares are of the slot's total demand, batch reservation included."""

    slot: str
    minute: int
    utilization_pct: float
    demand_cores: float
    batch_cores: float
    by_statement: list[Share]
    by_tenant: list[Share]
    breached_slos: list[str] = field(default_factory=list)

    @property
    def batch_pct(self) -> float:
        return round(100 * self.batch_cores / self.demand_cores, 1) if self.demand_cores else 0.0

    def as_dict(self) -> dict:
        return {"slot": self.slot, "utilization_pct": self.utilization_pct, "demand_cores": self.demand_cores,
                "batch_job_cores": self.batch_cores, "batch_job_pct": self.batch_pct,
                "breached_slos": self.breached_slos,
                "by_statement": [{"id": s.name, "cores": s.cores, "pct": s.pct} for s in self.by_statement],
                "by_tenant": [{"id": s.name, "cores": s.cores, "pct": s.pct} for s in self.by_tenant]}


@dataclass(frozen=True)
class Cause:
    """One remedy the shape of the load makes worth considering, with the share that suggests it."""

    kind: str  # batch_job | statement | tenant | broad_load
    subject: str
    pct: float
    remedy: str
    detail: str
    confidence: str = "medium"  # high | medium | low
    confidence_reason: str = ""
    slots_present: int = 0  # breaching slots where it is above the threshold
    slots_total: int = 0
    lead_pct: float = 0.0  # points ahead of the next subject of the same kind

    def as_dict(self) -> dict:
        return {"kind": self.kind, "subject": self.subject, "share_pct": self.pct, "remedy": self.remedy,
                "detail": self.detail, "confidence": self.confidence, "confidence_reason": self.confidence_reason,
                "slots_present": self.slots_present, "slots_total": self.slots_total, "lead_pct": self.lead_pct}


@dataclass(frozen=True)
class Attribution:
    option_id: str
    slots: list[SlotAttribution]
    causes: list[Cause]
    window: str
    thresholds: dict[str, float]

    @property
    def worst(self) -> SlotAttribution | None:
        return max(self.slots, key=lambda s: s.utilization_pct, default=None)

    def as_dict(self) -> dict:
        return {"option_id": self.option_id, "window": self.window, "thresholds": self.thresholds,
                "causes": [c.as_dict() for c in self.causes], "slots": [s.as_dict() for s in self.slots]}


def _shares(totals: dict[str, float], demand: float) -> list[Share]:
    return sorted(
        (Share(name, round(cores, 3), round(100 * cores / demand, 1) if demand else 0.0)
         for name, cores in totals.items() if cores > 0),
        key=lambda s: -s.cores)


def attribute(ctx: ModelContext, option_id: str = "OPT-KEEP", effects: dict | None = None, *,
              only_breached: bool = True, threshold_pct: float | None = None) -> Attribution:
    """Decompose the slots that matter under one option: the breached ones, or those over the utilization threshold.

    `effects` are the measured optimization effects, so attribution reflects the workload as that option would leave
    it rather than as it is today.
    """
    scenario = ctx.scenario
    plan: _Plan = _resolve(scenario.option(option_id), ctx, effects or {}, set())
    multipliers: dict[str, float] = {}
    for effect in plan.effects:
        for fid, value in effect.cpu_multiplier_by_fingerprint.items():
            multipliers[fid] = multipliers.get(fid, 1.0) * value

    from capacitylab.capacity.catalog import get_instance
    from capacitylab.capacity.queueing import StatementLoad, evaluate_slot

    fps = {f.id: f for f in scenario.workload.fingerprints}
    slot_minutes = scenario.horizon.slot_minutes
    cut = threshold_pct if threshold_pct is not None else scenario.utilization_threshold_pct
    batch_window = None
    if ctx.batch and plan.batch_start_minutes is not None and not plan.batch_next_day:
        batch_window = (plan.batch_start_minutes, plan.batch_start_minutes + (ctx.batch_duration_minutes or 0))

    slots: list[SlotAttribution] = []
    for i, minute in enumerate(ctx.slot_starts):
        batch_active = batch_window is not None and minute < batch_window[1] and minute + slot_minutes > batch_window[0]
        loads, by_statement, by_tenant = [], {}, {}
        for fid, series_by_tenant in ctx.series.items():
            cpu_ms = ctx.cpu_ms[fid] * multipliers.get(fid, 1.0)
            qps = sum(values[i] for values in series_by_tenant.values())
            extra = (fps[fid] and ctx.batch and batch_active and fid in ctx.batch.contended_fingerprints)
            loads.append(StatementLoad(fid, qps, cpu_ms,
                                       fps[fid].lock_wait_ms_during_batch if extra else 0.0))
            by_statement[fid] = qps * cpu_ms / 1000.0
            for tenant, values in series_by_tenant.items():
                by_tenant[tenant] = by_tenant.get(tenant, 0.0) + values[i] * cpu_ms / 1000.0
        reserved = ctx.batch.cpu_cores if (batch_active and ctx.batch) else 0.0
        result = evaluate_slot(get_instance(plan.instance_by_slot[i]).vcpu, loads, reserved)

        breached = []
        for sid, slo in ctx.slos.items():
            worst = max(result.p95_latency_ms.get(fid, 0.0) for fid in slo["fingerprint_ids"])
            if worst > slo["p95_ms"]:
                breached.append(sid)
        if only_breached and not breached and result.utilization_pct <= cut:
            continue
        demand = result.cpu_cores_demand
        if reserved:
            by_statement = {**by_statement, ctx.batch.name: reserved}
        slots.append(SlotAttribution(
            slot=ctx.slot_labels[i], minute=minute, utilization_pct=result.utilization_pct,
            demand_cores=round(demand, 3), batch_cores=round(reserved, 3),
            by_statement=_shares(by_statement, demand), by_tenant=_shares(by_tenant, demand),
            breached_slos=breached))

    return Attribution(option_id=option_id, slots=slots, causes=_causes(ctx, slots, plan),
                       window=f"{slots[0].slot} to {slots[-1].slot}" if slots else "no slot over the threshold",
                       thresholds={"utilization_pct": cut, "dominant_statement_pct": DOMINANT_STATEMENT_PCT,
                                   "dominant_tenant_pct": DOMINANT_TENANT_PCT, "batch_share_pct": BATCH_SHARE_PCT})


def _mean_share(slots: list[SlotAttribution], pick) -> dict[str, float]:
    totals: dict[str, float] = {}
    for slot in slots:
        for share in pick(slot):
            totals[share.name] = totals.get(share.name, 0.0) + share.pct
    return {name: round(total / len(slots), 1) for name, total in totals.items()} if slots else {}


def _confidence(pct: float, threshold: float, lead: float, present: int, total: int) -> tuple[str, str]:
    """How sure this reading is, from the margin over the threshold, the lead over the next subject, and consistency.

    None of this says the remedy will work. It says how firmly the evidence points at this subject rather than
    another, which is a different and more answerable question.
    """
    share = present / total if total else 0.0
    parts = [f"{pct:g}% of demand", f"{lead:g} points ahead of the next", f"leads in {present} of {total} slots"]
    if pct >= threshold + CLEAR_MARGIN_PCT and lead >= CLEAR_LEAD_PCT and share >= CONSISTENT_SHARE:
        return "high", "; ".join(parts)
    if share < PRESENT_SHARE:
        return "low", "; ".join(parts) + ", so it drives only part of the window"
    if lead < CLEAR_LEAD_PCT:
        return "low", "; ".join(parts) + ", too close to the next to separate them"
    return "medium", "; ".join(parts)


def _per_slot_shares(slots: list[SlotAttribution], pick, subject: str) -> list[float]:
    return [next((s.pct for s in pick(slot) if s.name == subject), 0.0) for slot in slots]


def _lead(means: dict[str, float], subject: str) -> float:
    others = [pct for name, pct in means.items() if name != subject]
    return round(means.get(subject, 0.0) - (max(others) if others else 0.0), 1)


def _causes(ctx: ModelContext, slots: list[SlotAttribution], plan: _Plan) -> list[Cause]:
    """Which remedies the shape of the load makes worth considering. Suggestions, not a decision."""
    if not slots:
        return []
    causes: list[Cause] = []
    # Averaged over the slots the job actually runs in: a job that holds a third of two slots is worth moving, and
    # averaging it over a quiet evening would hide that.
    overlapping = [s for s in slots if s.batch_cores > 0]
    batch_share = round(sum(s.batch_pct for s in overlapping) / len(overlapping), 1) if overlapping else 0.0
    if ctx.batch and batch_share >= BATCH_SHARE_PCT:
        present = len(overlapping)
        names = [s.slot for s in overlapping]
        # A job either runs in a slot or it does not, so its "lead" is its own share: there is nothing to confuse it
        # with. Confidence then rests on how much of the window it covers.
        grade, why = _confidence(batch_share, BATCH_SHARE_PCT, batch_share, present, len(slots))
        causes.append(Cause(
            "batch_job", ctx.batch.name, batch_share, "move the batch job",
            f"It holds {ctx.batch.cpu_cores:g} cores in {present} of these slots "
            f"({names[0]} to {names[-1]}), {batch_share:g}% of the demand while it runs. Moving work that "
            "has a deadline rather than an audience is the cheapest remedy when the deadline still holds.",
            confidence=grade, confidence_reason=why, slots_present=present, slots_total=len(slots),
            lead_pct=batch_share))

    by_statement = _mean_share(slots, lambda s: s.by_statement)
    for fid, pct in sorted(by_statement.items(), key=lambda kv: -kv[1]):
        if pct >= DOMINANT_STATEMENT_PCT and not (ctx.batch and fid == ctx.batch.name):
            present = sum(share >= DOMINANT_STATEMENT_PCT
                          for share in _per_slot_shares(slots, lambda s: s.by_statement, fid))
            grade, why = _confidence(pct, DOMINANT_STATEMENT_PCT, _lead(by_statement, fid), present, len(slots))
            causes.append(Cause(
                "statement", fid, pct, "index or rewrite this statement",
                f"{fid} is {pct:g}% of the demand in these slots. An index experiment or a rewrite equivalence check "
                "would show whether its cost per execution can be cut; either is cheaper than buying capacity.",
                confidence=grade, confidence_reason=why, slots_present=present, slots_total=len(slots),
                lead_pct=_lead(by_statement, fid)))

    by_tenant = _mean_share(slots, lambda s: s.by_tenant)
    for tenant, pct in sorted(by_tenant.items(), key=lambda kv: -kv[1]):
        if pct >= DOMINANT_TENANT_PCT:
            present = sum(share >= DOMINANT_TENANT_PCT
                          for share in _per_slot_shares(slots, lambda s: s.by_tenant, tenant))
            grade, why = _confidence(pct, DOMINANT_TENANT_PCT, _lead(by_tenant, tenant), present, len(slots))
            causes.append(Cause(
                "tenant", tenant, pct, "check this tenant's share against what they pay for",
                f"{tenant} drives {pct:g}% of the demand in these slots. Whether that is fair is an entitlement "
                "question, not a capacity one.",
                confidence=grade, confidence_reason=why, slots_present=present, slots_total=len(slots),
                lead_pct=_lead(by_tenant, tenant)))

    if not causes:
        top = max(by_statement.items(), key=lambda kv: kv[1], default=("nothing", 0.0))
        causes.append(Cause(
            "broad_load", "the whole workload", round(top[1], 1), "capacity is the honest remedy",
            "No single statement, tenant or job dominates these slots, so there is nothing cheaper to fix first.",
            confidence="high", confidence_reason=f"the largest single share is {top[1]:g}%, below every threshold",
            slots_present=len(slots), slots_total=len(slots)))
    return causes
