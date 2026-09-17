"""Deterministic evaluation of decision options over the scenario horizon.

Every number produced here is a MODELED outcome derived from scenario assumptions, evidence items,
and (when available) locally measured experiment effects. Nothing here consults a language model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from capacitylab.capacity.catalog import buffer_pool_gib, get_instance
from capacitylab.capacity.cost import (
    RateCard,
    choose_rate_card,
    monthly_resize_delta,
    resize_delta_for_hours,
    storage_cost_month,
)
from capacitylab.capacity.queueing import MODEL_ASSUMPTIONS, StatementLoad, evaluate_slot
from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.evidence.models import EvidenceKind
from capacitylab.scenarios.models import BatchEvent, OptionSpec, Scenario, hhmm_to_minutes
from capacitylab.workload.generator import Surge, generate


class OptimizationEffect(BaseModel):
    """Effect of an index candidate, derived from a sandbox experiment and translated by an assumption."""

    index_candidate: str
    cpu_multiplier_by_fingerprint: dict[str, float] = Field(default_factory=dict)
    index_storage_gib: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)
    basis: str = "measured_local_translated"


class SloOutcome(BaseModel):
    target_p95_ms: float
    worst_p95_ms: float | None  # None means unbounded (saturated)
    breach_slots: int


class OptionOutcome(BaseModel):
    option_id: str
    option_kind: str
    label: str
    peak_utilization_pct: float
    peak_slot: str
    slots_over_threshold: int
    saturated_slots: int
    slo: dict[str, SloOutcome]
    total_slo_breach_slots: int
    cost_delta_event_usd: float
    cost_delta_month_usd: float
    operational_events: list[str]
    batch_deadline_ok: bool | None
    unknowns: list[str]
    assumptions_used: dict[str, float | str | None]
    model_assumptions: list[str]
    evidence_ids: list[str]
    utilization_by_slot: list[float]


@dataclass
class ModelContext:
    scenario: Scenario
    bundle: EvidenceBundle
    slot_labels: list[str]
    slot_starts: list[int]
    series: dict[str, dict[str, list[float]]]
    cpu_ms: dict[str, float]
    slos: dict[str, dict]
    rate_card: RateCard | None
    batch: BatchEvent | None
    batch_duration_minutes: int | None
    assumptions: dict[str, float | str | None]
    evidence_ids: list[str] = field(default_factory=list)
    rate_card_note: str = ""
    prices_from_aws: bool = False


def needed_instance_classes(scenario: Scenario) -> set[str]:
    """Every instance class the cost model may have to price: the current nodes and every option's target."""
    classes = {scenario.cluster.writer_instance, *scenario.cluster.reader_instances}
    classes |= {o.params["target_instance"] for o in scenario.options if "target_instance" in o.params}
    return classes


def assumption_values(scenario: Scenario, overrides: dict[str, float] | None = None) -> dict[str, float | str | None]:
    values = {a.id: a.value for a in scenario.assumptions}
    for key, value in (overrides or {}).items():
        if key not in values:
            raise ValueError(f"unknown assumption override {key}")
        values[key] = value
    return values


def build_forecast(scenario: Scenario, assumptions: dict[str, float | str | None]) -> dict:
    horizon = scenario.horizon
    surges: list[Surge] = []
    for event in scenario.events:
        if event.kind == "campaign":
            surges.append(
                Surge(
                    start_slot=horizon.slot_index(event.start),
                    end_slot=horizon.slot_index(event.end),
                    multiplier=float(assumptions[event.multiplier_assumption]),
                    tenant=event.tenant,
                    flag="campaign_sensitive",
                )
            )
        elif event.kind == "release":
            surges.append(
                Surge(
                    start_slot=horizon.slot_index(event.start),
                    end_slot=len(horizon.slot_starts()),
                    multiplier=float(assumptions[event.multiplier_assumption]),
                    flag="release_sensitive",
                )
            )
    return generate(scenario.workload, horizon.slot_starts(), surges)


def build_context(scenario: Scenario, bundle: EvidenceBundle, overrides: dict[str, float] | None = None) -> ModelContext:
    assumptions = assumption_values(scenario, overrides)
    forecast = build_forecast(scenario, assumptions)
    evidence_ids: list[str] = []

    cpu_ms = {f.id: f.cpu_ms_per_exec for f in scenario.workload.fingerprints}
    digest = bundle.first(EvidenceKind.QUERY_DIGEST)
    if digest:
        evidence_ids.append(digest.id)
        for row in digest.data.get("fingerprints", []):
            if row.get("fingerprint_id") in cpu_ms and row.get("avg_cpu_ms") is not None:
                cpu_ms[row["fingerprint_id"]] = float(row["avg_cpu_ms"])

    slos: dict[str, dict] = {}
    slo_item = bundle.first(EvidenceKind.SLO)
    if slo_item:
        evidence_ids.append(slo_item.id)
        for s in slo_item.data.get("slos", []):
            slos[s["id"]] = s

    choice = choose_rate_card(bundle.by_kind(EvidenceKind.RATE_CARD), needed_instance_classes(scenario))
    rate_card = choice.card if choice else None
    if choice:
        evidence_ids.extend(choice.evidence_ids)

    batches = scenario.events_of("batch_job")
    batch = batches[0] if batches else None
    duration = batch.duration_minutes if batch else None
    run_log = bundle.first(EvidenceKind.BATCH_RUN_LOG)
    if batch and run_log and run_log.data.get("durations_minutes"):
        duration = int(max(run_log.data["durations_minutes"]))
        evidence_ids.append(run_log.id)

    for kind in (EvidenceKind.TOPOLOGY, EvidenceKind.BATCH_SCHEDULE, EvidenceKind.WORKLOAD_FORECAST):
        item = bundle.first(kind)
        if item:
            evidence_ids.append(item.id)

    return ModelContext(
        scenario=scenario,
        bundle=bundle,
        slot_labels=scenario.horizon.slot_labels(),
        slot_starts=scenario.horizon.slot_starts(),
        series=forecast["series"],
        cpu_ms=cpu_ms,
        slos=slos,
        rate_card=rate_card,
        batch=batch,
        batch_duration_minutes=duration,
        assumptions=assumptions,
        evidence_ids=evidence_ids,
        rate_card_note=choice.note if choice else "No rate card; costs are not modeled.",
        prices_from_aws=bool(choice and choice.prices_from_aws),
    )


@dataclass
class _Plan:
    instance_by_slot: list[str]
    batch_start_minutes: int | None
    batch_next_day: bool = False
    effects: list[OptimizationEffect] = field(default_factory=list)
    cost_event: float = 0.0
    cost_month: float = 0.0
    events: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    batch_deadline_ok: bool | None = None


def _node_instance(scenario: Scenario) -> str:
    if scenario.cluster.evaluated_node == "reader" and scenario.cluster.reader_instances:
        return scenario.cluster.reader_instances[0]
    return scenario.cluster.writer_instance


def _batch_deadline_ok(start_minutes: int, next_day: bool, duration: int, deadline_next_day: str) -> bool:
    end = start_minutes + duration + (24 * 60 if next_day else 0)
    return end <= 24 * 60 + hhmm_to_minutes(deadline_next_day)


def _resolve(option: OptionSpec, ctx: ModelContext, effects: dict[str, OptimizationEffect], seen: set[str]) -> _Plan:
    scenario = ctx.scenario
    if option.id in seen:
        raise ValueError(f"option {option.id} is referenced cyclically")
    seen = seen | {option.id}
    base_instance = _node_instance(scenario)
    n = len(ctx.slot_starts)
    batch_start = hhmm_to_minutes(ctx.batch.start) if ctx.batch else None
    plan = _Plan(instance_by_slot=[base_instance] * n, batch_start_minutes=batch_start)
    card = ctx.rate_card
    kind = option.kind
    p = option.params

    if kind == "keep":
        pass
    elif kind == "scale_temporary":
        target = p["target_instance"]
        start, end = hhmm_to_minutes(p["window_start"]), hhmm_to_minutes(p["window_end"])
        for i, minute in enumerate(ctx.slot_starts):
            if start <= minute < end:
                plan.instance_by_slot[i] = target
        nodes = p.get("nodes", ["writer"])
        hours = (end - start) / 60
        if card:
            for node in nodes:
                current = scenario.cluster.writer_instance if node == "writer" else scenario.cluster.reader_instances[0]
                plan.cost_event += resize_delta_for_hours(card, current, target, hours)
        plan.events.append(f"{2 * len(nodes)} instance class modifications ({', '.join(nodes)}: up at "
                           f"{p['window_start']}, down at {p['window_end']})")
        if "writer" in nodes:
            failover = ctx.assumptions.get(p.get("failover_assumption", ""), None)
            if failover in (None, ""):
                plan.unknowns.append("Writer modification requires failover; failover duration is not measured.")
                plan.events.append("2 writer failovers (duration unknown)")
            else:
                plan.events.append(f"2 writer failovers (assumed {failover} s each)")
    elif kind == "resize_permanent":
        target = p["target_instance"]
        node = p.get("node", "writer")
        plan.instance_by_slot = [target] * n
        current = scenario.cluster.writer_instance if node == "writer" else scenario.cluster.reader_instances[0]
        if card:
            plan.cost_month += monthly_resize_delta(card, current, target)
        plan.events.append(f"1 permanent instance class modification ({node}: {current} -> {target})")
        ws = scenario.cluster.working_set_gib
        if ws is not None:
            pool = buffer_pool_gib(get_instance(target), scenario.cluster.buffer_pool_fraction_assumption)
            if ws > pool:
                plan.unknowns.append(
                    f"Working set {ws} GiB exceeds the modeled buffer pool of {pool} GiB on {target}; "
                    "the I/O latency impact is not modeled and needs a test."
                )
        if node == "reader":
            plan.unknowns.append(
                "The reader is a failover target: after failover the writer workload would run on "
                f"{target}; that case is not evaluated by this option."
            )
    elif kind == "optimize_index":
        candidate = p["index_candidate"]
        effect = effects.get(candidate)
        if effect is None:
            plan.unknowns.append(
                f"No measured effect for {candidate}; benefit is NOT applied until an index experiment runs."
            )
        else:
            plan.effects.append(effect)
            plan.evidence_ids.extend(effect.evidence_ids)
            if card:
                plan.cost_month += storage_cost_month(card, effect.index_storage_gib)
            plan.events.append(f"online index build for {candidate} (~{effect.index_storage_gib} GiB modeled)")
    elif kind == "reschedule_batch":
        if not ctx.batch:
            raise ValueError(f"option {option.id} reschedules a batch job but the scenario has none")
        new_start = hhmm_to_minutes(p["new_start"])
        plan.batch_start_minutes = new_start
        plan.batch_next_day = new_start < scenario.horizon.start_minutes
        plan.events.append(f"batch job {ctx.batch.name} moved to {p['new_start']}"
                           + (" (next day)" if plan.batch_next_day else ""))
    elif kind == "combined":
        for component_id in p["components"]:
            sub = _resolve(scenario.option(component_id), ctx, effects, seen)
            plan.instance_by_slot = [
                max(a, b, key=lambda name: get_instance(name).vcpu)
                for a, b in zip(plan.instance_by_slot, sub.instance_by_slot, strict=True)
            ]
            if sub.batch_start_minutes != batch_start:
                plan.batch_start_minutes, plan.batch_next_day = sub.batch_start_minutes, sub.batch_next_day
            plan.effects.extend(sub.effects)
            plan.cost_event += sub.cost_event
            plan.cost_month += sub.cost_month
            plan.events.extend(sub.events)
            plan.unknowns.extend(sub.unknowns)
            plan.evidence_ids.extend(sub.evidence_ids)
    else:  # pragma: no cover - guarded by the schema
        raise ValueError(f"unsupported option kind {kind}")

    if ctx.batch and plan.batch_start_minutes is not None and ctx.batch_duration_minutes:
        plan.batch_deadline_ok = _batch_deadline_ok(
            plan.batch_start_minutes, plan.batch_next_day, ctx.batch_duration_minutes, ctx.batch.deadline_next_day
        )
    return plan


def evaluate_option(
    ctx: ModelContext, option_id: str, effects: dict[str, OptimizationEffect] | None = None
) -> OptionOutcome:
    scenario = ctx.scenario
    option = scenario.option(option_id)
    plan = _resolve(option, ctx, effects or {}, set())
    fps = {f.id: f for f in scenario.workload.fingerprints}

    multipliers: dict[str, float] = {}
    for effect in plan.effects:
        for fid, m in effect.cpu_multiplier_by_fingerprint.items():
            multipliers[fid] = multipliers.get(fid, 1.0) * m

    utilization: list[float] = []
    saturated = 0
    over = 0
    worst: dict[str, float] = {sid: 0.0 for sid in ctx.slos}
    breaches: dict[str, int] = {sid: 0 for sid in ctx.slos}
    slot = ctx.scenario.horizon.slot_minutes
    batch_window = None
    if ctx.batch and plan.batch_start_minutes is not None and not plan.batch_next_day:
        batch_window = (plan.batch_start_minutes, plan.batch_start_minutes + (ctx.batch_duration_minutes or 0))

    for i, minute in enumerate(ctx.slot_starts):
        batch_active = batch_window is not None and minute < batch_window[1] and minute + slot > batch_window[0]
        loads = []
        for fid, by_tenant in ctx.series.items():
            spec = fps[fid]
            qps = sum(values[i] for values in by_tenant.values())
            extra = spec.lock_wait_ms_during_batch if (batch_active and fid in ctx.batch.contended_fingerprints) else 0.0
            loads.append(StatementLoad(fid, qps, ctx.cpu_ms[fid] * multipliers.get(fid, 1.0), extra))
        reserved = ctx.batch.cpu_cores if batch_active else 0.0
        result = evaluate_slot(get_instance(plan.instance_by_slot[i]).vcpu, loads, reserved)
        utilization.append(result.utilization_pct)
        saturated += int(result.saturated)
        over += int(result.utilization_pct > scenario.utilization_threshold_pct)
        for sid, slo in ctx.slos.items():
            slot_worst = max(result.p95_latency_ms.get(fid, 0.0) for fid in slo["fingerprint_ids"])
            worst[sid] = max(worst[sid], slot_worst)
            if slot_worst > slo["p95_ms"]:
                breaches[sid] += 1

    peak_i = max(range(len(utilization)), key=utilization.__getitem__)
    slo_out = {
        sid: SloOutcome(
            target_p95_ms=ctx.slos[sid]["p95_ms"],
            worst_p95_ms=None if math.isinf(worst[sid]) else round(worst[sid], 1),
            breach_slots=breaches[sid],
        )
        for sid in ctx.slos
    }
    used = {k: v for k, v in ctx.assumptions.items()}
    return OptionOutcome(
        option_id=option.id,
        option_kind=option.kind,
        label=option.label,
        peak_utilization_pct=utilization[peak_i],
        peak_slot=ctx.slot_labels[peak_i],
        slots_over_threshold=over,
        saturated_slots=saturated,
        slo=slo_out,
        total_slo_breach_slots=sum(breaches.values()),
        cost_delta_event_usd=round(plan.cost_event, 2),
        cost_delta_month_usd=round(plan.cost_month, 2),
        operational_events=plan.events,
        batch_deadline_ok=plan.batch_deadline_ok,
        unknowns=plan.unknowns,
        assumptions_used=used,
        model_assumptions=MODEL_ASSUMPTIONS,
        evidence_ids=sorted(set(ctx.evidence_ids + plan.evidence_ids)),
        utilization_by_slot=utilization,
    )


def evaluate_all(ctx: ModelContext, effects: dict[str, OptimizationEffect] | None = None) -> list[OptionOutcome]:
    return [evaluate_option(ctx, o.id, effects) for o in ctx.scenario.options]
