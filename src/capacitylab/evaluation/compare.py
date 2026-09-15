"""Compare the five-role review with a single reviewer and a rule-based baseline on the same evidence.

Scoring uses the synthetic scenario's hidden ground truth (for example the true campaign multiplier)
inside the same capacity model. That is a reference outcome under the model, not real-world truth.
With the mock provider, every role and the single reviewer are scripted rules: the comparison exercises the simulation code
and says nothing about how real models perform.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable

from pydantic import BaseModel, Field

from capacitylab.capacity.options import OptimizationEffect, build_context, evaluate_all, evaluate_option
from capacitylab.diagnostics import fixture_db as fx
from capacitylab.diagnostics.sandbox import Sandbox, SQLiteSandbox
from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.scenarios.models import Scenario
from capacitylab.settings import MySQLSettings
from capacitylab.simulation.orchestrator import Orchestrator
from capacitylab.simulation.providers.base import TurnProvider
from capacitylab.simulation.roles import RoleId
from capacitylab.simulation.run import RunConfig, SimulationRun
from capacitylab.simulation.schema import ToolRequest
from capacitylab.simulation.tools import ToolEnvironment, execute_tool


class ApproachResult(BaseModel):
    approach: str
    recommendation: str | None
    distinct_final_positions: list[str] = Field(default_factory=list)
    outcome_under_reference: dict | None = None
    reference_best: list[str] = Field(default_factory=list)
    breach_slot_regret: int | None = None
    cost_regret_usd: float | None = None
    root_cause_identified: bool | None = None
    expected_gaps_flagged: list[str] = Field(default_factory=list)
    gap_recall: float | None = None
    citation_errors: int = 0
    ungrounded_numbers: int = 0
    tool_calls_executed: int = 0
    unresolved_disagreements: int = 0
    spend_usd: float = 0.0
    notes: list[str] = Field(default_factory=list)


class ComparisonReport(BaseModel):
    scenario_id: str
    provider: str
    mocked: bool
    reference_assumptions: dict
    approaches: list[ApproachResult]
    caveats: list[str]


def _cost(o) -> float:
    return o.cost_delta_event_usd + o.cost_delta_month_usd


def _reference_effects(scenario: Scenario, bundle: EvidenceBundle, sandbox_factory: Callable[[], Sandbox]) \
        -> dict[str, OptimizationEffect]:
    if not any(o.kind == "optimize_index" for o in scenario.options):
        return {}
    env = ToolEnvironment(scenario, bundle.copy(), sandbox_factory)
    try:
        request = ToolRequest(tool="index_experiment", arguments_json=json.dumps({"candidates": sorted(fx.INDEX_CANDIDATES)}),
                              purpose="reference effect for scoring")
        execute_tool(env, "TC-REF", 0, [RoleId.DATABASE_ENGINEER], request)
        return dict(env.effects)
    finally:
        env.close()


def _score_run(name: str, run: SimulationRun, scenario: Scenario, reference: dict, best: list[str], best_cost: float,
               best_breach: int) -> ApproachResult:
    decision = run.decision
    positions = [p.position for p in decision.final_positions if p.position != "undecided"]
    counts = Counter(positions)
    top = counts.most_common()
    recommendation = None
    notes = []
    if top:
        leaders = [opt for opt, n in top if n == top[0][1]]
        recommendation = sorted(leaders)[0]
        if len(decision.final_positions) > 1:
            notes.append(f"Scored on the plurality position ({top[0][1]} of {len(decision.final_positions)} roles); "
                         "CapacityLab itself reports positions, not a vote.")
        if len(leaders) > 1:
            notes.append(f"Tie between {', '.join(leaders)}; first alphabetically scored.")
    truth = run.scenario_id and scenario.ground_truth
    roots = set(truth.root_cause_fingerprints) if truth else set()
    proposals = {p["target_fingerprint"] for p in decision.optimization_proposals}
    root_found = bool(roots & proposals) if roots else None
    flagged = sorted({m["item"] for m in decision.missing_evidence} & set(truth.expected_gap_flags)) if truth else []
    result = ApproachResult(
        approach=name,
        recommendation=recommendation,
        distinct_final_positions=sorted(set(positions)),
        reference_best=best,
        root_cause_identified=root_found,
        expected_gaps_flagged=flagged,
        gap_recall=round(len(flagged) / len(truth.expected_gap_flags), 2) if truth and truth.expected_gap_flags else None,
        citation_errors=decision.validation["by_code"].get("invalid_citation", 0)
        + decision.validation["by_code"].get("inaccessible_citation", 0),
        ungrounded_numbers=decision.validation["by_code"].get("ungrounded_number", 0),
        tool_calls_executed=sum(1 for r in run.tool_calls if r.status == "ok"),
        unresolved_disagreements=len(decision.disagreements),
        notes=notes,
    )
    if recommendation and recommendation in reference:
        o = reference[recommendation]
        result.outcome_under_reference = _summary(o)
        result.breach_slot_regret = o.total_slo_breach_slots - best_breach
        result.cost_regret_usd = round(_cost(o) - best_cost, 2)
    return result


def _summary(o) -> dict:
    return {"option_id": o.option_id, "total_slo_breach_slots": o.total_slo_breach_slots, "saturated_slots": o.saturated_slots,
            "slots_over_threshold": o.slots_over_threshold, "peak_utilization_pct": o.peak_utilization_pct,
            "cost_delta_event_usd": o.cost_delta_event_usd, "cost_delta_month_usd": o.cost_delta_month_usd,
            "unknowns": len(o.unknowns)}


def rule_based_recommendation(scenario: Scenario, bundle: EvidenceBundle) -> tuple[str, str]:
    """Capacity-only rules: scale up when the status quo exceeds the threshold, downsize when far below it."""
    ctx = build_context(scenario, bundle)
    keep = next(o for o in scenario.options if o.kind == "keep")
    base = evaluate_option(ctx, keep.id)
    threshold = scenario.utilization_threshold_pct
    if base.peak_utilization_pct > threshold:
        scale = [o for o in scenario.options if o.kind == "scale_temporary"]
        if scale:
            return scale[0].id, f"peak {base.peak_utilization_pct}% > {threshold}%: scale temporarily"
        return keep.id, "over threshold but no scaling option"
    if base.peak_utilization_pct < threshold / 2:
        downs = [evaluate_option(ctx, o.id) for o in scenario.options if o.kind == "resize_permanent"]
        downs = [d for d in downs if d.peak_utilization_pct <= threshold and d.total_slo_breach_slots == 0]
        if downs:
            chosen = min(downs, key=_cost)
            return chosen.option_id, f"peak {base.peak_utilization_pct}% < {threshold / 2}%: downsize to the cheapest class under threshold"
    return keep.id, "within threshold: keep"


def compare(scenario: Scenario, bundle: EvidenceBundle, provider_factory: Callable[[], TurnProvider],
            sandbox_factory: Callable[[], Sandbox] = SQLiteSandbox, max_usd_per_run: float | None = None,
            lab_mysql: MySQLSettings | None = None) -> ComparisonReport:
    truth = scenario.ground_truth
    overrides = truth.assumption_overrides if truth else {}
    effects = _reference_effects(scenario, bundle, sandbox_factory)
    reference = {o.option_id: o for o in evaluate_all(build_context(scenario, bundle, overrides), effects)}
    eligible = [o for o in reference.values() if o.total_slo_breach_slots == 0 and o.saturated_slots == 0
                and o.slots_over_threshold == 0 and not o.unknowns]
    if not eligible:
        eligible = sorted(reference.values(), key=lambda o: (o.total_slo_breach_slots, o.slots_over_threshold))[:1]
    best_cost = min(_cost(o) for o in eligible)
    best = sorted(o.option_id for o in eligible if _cost(o) == best_cost)
    best_breach = min(o.total_slo_breach_slots for o in eligible)

    provider = provider_factory()
    config = RunConfig(max_rounds=scenario.budgets.max_rounds, max_tool_calls=scenario.budgets.max_tool_calls,
                       max_tool_calls_per_turn=scenario.budgets.max_tool_calls_per_turn,
                       max_usd=scenario.budgets.max_usd if max_usd_per_run is None else max_usd_per_run)
    multi = Orchestrator(scenario, bundle, provider, config, sandbox_factory=sandbox_factory, lab_mysql=lab_mysql).run()
    single = Orchestrator(scenario, bundle, provider_factory(), config, sandbox_factory=sandbox_factory,
                          roles=[RoleId.SINGLE_AGENT], lab_mysql=lab_mysql).run()
    approaches = [
        _score_run("multi_stakeholder", multi, scenario, reference, best, best_cost, best_breach),
        _score_run("single_agent", single, scenario, reference, best, best_cost, best_breach),
    ]
    approaches[0].spend_usd, approaches[1].spend_usd = multi.spend_usd, single.spend_usd
    for run, result in ((multi, approaches[0]), (single, approaches[1])):
        if run.state.status.value != "concluded":
            result.notes.append(f"Run ended with status {run.state.status.value}: {'; '.join(run.warnings) or 'no warning'}")
    rule_option, rule_reason = rule_based_recommendation(scenario, bundle)
    o = reference[rule_option]
    approaches.append(ApproachResult(
        approach="rule_based", recommendation=rule_option, distinct_final_positions=[rule_option],
        outcome_under_reference=_summary(o), reference_best=best,
        breach_slot_regret=o.total_slo_breach_slots - best_breach, cost_regret_usd=round(_cost(o) - best_cost, 2),
        root_cause_identified=False if truth and truth.root_cause_fingerprints else None,
        gap_recall=0.0 if truth and truth.expected_gap_flags else None, notes=[rule_reason],
    ))
    caveats = [
        "Reference outcomes use the scenario's hidden assumptions inside the same capacity model; they are not real-world truth.",
        "Reference best excludes options with unmeasured risks (unknowns); a different risk preference changes it.",
    ]
    if provider.mocked:
        caveats.insert(0, "MOCK provider: every role and the single reviewer are scripted rules, not a language model. This comparison "
                          "tests the simulation code and the scoring, not model quality.")
    return ComparisonReport(scenario_id=scenario.id, provider=provider.name, mocked=provider.mocked,
                            reference_assumptions=overrides, approaches=approaches, caveats=caveats)
