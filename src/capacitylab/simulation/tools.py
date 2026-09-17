# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tool registry. Stakeholders request tools; the orchestrator executes them deterministically.

Each successful call produces a new evidence item whose provenance reflects how it was produced:
sandbox experiments are MEASURED, capacity and cost calculations are MODELED, and analyses of
observed evidence stay OBSERVED (derived).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from capacitylab.capacity.entitlements import tenant_entitlement_review
from capacitylab.capacity.options import OptimizationEffect, build_context, evaluate_option
from capacitylab.diagnostics import analysis, experiments
from capacitylab.diagnostics import fixture_db as fx
from capacitylab.diagnostics.sandbox import MySQLSandbox, Sandbox, SQLiteSandbox
from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.scenarios.models import Scenario
from capacitylab.settings import MySQLSettings
from capacitylab.simulation.roles import ROLES, RoleId
from capacitylab.simulation.schema import ToolRequest

NONDETERMINISTIC_KEYS = {"duration_ms"}
MYSQL_TIMING_KEYS = {"write_work_before", "write_work_after", "write_overhead_pct"}


class ToolCallRecord(BaseModel):
    call_id: str
    round: int
    requested_by: list[str]
    tool: str
    arguments: dict[str, Any]
    purpose: str
    status: str  # ok | denied | error | skipped_budget | skipped_final_round
    error: str | None = None
    evidence_id: str | None = None
    result_digest: str | None = None
    duration_ms: int | None = None


@dataclass
class ToolEnvironment:
    scenario: Scenario
    bundle: EvidenceBundle
    sandbox_factory: Callable[[], Sandbox] = SQLiteSandbox
    effects: dict[str, OptimizationEffect] = field(default_factory=dict)
    lab_mysql: MySQLSettings | None = None  # enables lab_load_test (local MySQL container only)
    _sandbox: Sandbox | None = None
    _fixture_orders: int = 0

    def sandbox(self) -> Sandbox:
        if self._sandbox is None:
            self._sandbox = self.sandbox_factory()
            stats = fx.build(self._sandbox)
            self._fixture_orders = stats.orders
        return self._sandbox

    def assumption(self, key: str, default: float | None) -> float | None:
        for a in self.scenario.assumptions:
            if a.id == key and isinstance(a.value, int | float):
                return float(a.value)
        return default

    def close(self) -> None:
        if self._sandbox is not None:
            self._sandbox.close()
            self._sandbox = None


@dataclass
class ToolOutcome:
    title: str
    data: dict
    cited: list[str]
    caveats: list[str] = field(default_factory=list)
    after_created: Callable[[str], None] | None = None
    environment: str = "fixture"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    arguments: dict[str, str]  # name -> human-readable type/description
    kind: EvidenceKind
    provenance: Provenance
    handler: Callable[[ToolEnvironment, dict, RoleId], ToolOutcome]
    role_sensitive: bool = False
    deterministic: bool = True  # replay re-executes only deterministic tools

    @property
    def roles(self) -> list[str]:
        return sorted(r.value for r, d in ROLES.items() if self.name in d.tools)


def _list(args: dict, key: str, default: list | None = None) -> list:
    value = args.get(key, default)
    if value is None:
        raise ValueError(f"missing argument {key}")
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"argument {key} must be a list")
    return value


def _capacity_forecast(env: ToolEnvironment, args: dict, role: RoleId) -> ToolOutcome:
    overrides = args.get("assumption_overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError("assumption_overrides must be an object")
    ctx = build_context(env.scenario, env.bundle, {k: float(v) for k, v in overrides.items()})
    option_ids = _list(args, "option_ids", [o.id for o in env.scenario.options])
    known = {o.id for o in env.scenario.options}
    if unknown := [o for o in option_ids if o not in known]:
        raise ValueError(f"unknown options {unknown}")
    outcomes = [evaluate_option(ctx, oid, env.effects) for oid in option_ids]
    cited = sorted({e for o in outcomes for e in o.evidence_ids})
    return ToolOutcome(
        title="Capacity forecast for " + ", ".join(option_ids) + (f" with overrides {overrides}" if overrides else ""),
        data={
            "utilization_threshold_pct": env.scenario.utilization_threshold_pct,
            "assumption_overrides": overrides,
            "effects_applied": {k: v.model_dump() for k, v in env.effects.items()},
            "outcomes": [o.model_dump() for o in outcomes],
        },
        cited=cited,
        caveats=["Modeled with an M/M/c CPU model; I/O and buffer pool effects are not modeled."]
        + ([] if env.effects else ["No measured optimization effect was available; index options show no benefit."]),
    )


def _cost_estimate(env: ToolEnvironment, args: dict, role: RoleId) -> ToolOutcome:
    ctx = build_context(env.scenario, env.bundle)
    option_ids = _list(args, "option_ids", [o.id for o in env.scenario.options])
    budget = env.bundle.first(EvidenceKind.BUDGET)
    rows = []
    for oid in option_ids:
        o = evaluate_option(ctx, oid, env.effects)
        row = {"option_id": oid, "cost_delta_event_usd": o.cost_delta_event_usd,
               "cost_delta_month_usd": o.cost_delta_month_usd, "operational_events": o.operational_events,
               "unknowns": o.unknowns}
        if budget:
            forecast = budget.data.get("forecast_month_usd", 0) + o.cost_delta_month_usd + o.cost_delta_event_usd
            row["forecast_month_with_option_usd"] = round(forecast, 2)
            row["budget_headroom_usd"] = round(budget.data.get("monthly_budget_usd", 0) - forecast, 2)
        rows.append(row)
    rate_ids = [i for i in ctx.evidence_ids if env.bundle.get(i).kind == EvidenceKind.RATE_CARD]
    cited = rate_ids + ([budget.id] if budget else [])
    if ctx.prices_from_aws:
        data = {"options": rows, "rate_card_note": ctx.rate_card.source_note, "prices_used": ctx.rate_card_note}
        caveat = ("Instance amounts use on-demand list prices read from AWS; reserved or savings-plan discounts are not "
                  "included.")
    else:  # unchanged output, so recorded runs still replay
        rate = env.bundle.get(rate_ids[0]) if rate_ids else None
        data = {"options": rows, "rate_card_note": rate.data.get("source_note") if rate else "no rate card"}
        caveat = "Amounts are arithmetic on the rate card evidence; the demo rate card is illustrative."
    return ToolOutcome(title="Cost estimate for " + ", ".join(option_ids), data=data, cited=cited, caveats=[caveat])


def _index_experiment(env: ToolEnvironment, args: dict, role: RoleId) -> ToolOutcome:
    candidates = _list(args, "candidates", list(fx.INDEX_CANDIDATES))
    fingerprints = _list(args, "fingerprint_ids", ["QF-AUDIENCE", "QF-ORDER-HISTORY"])
    if unknown := [f for f in fingerprints if f not in fx.QUERIES]:
        raise ValueError(f"no sandbox statement for {unknown}; available: {sorted(fx.QUERIES)}")
    exponent = env.assumption("A-CPU-ROWS-EXPONENT", 0.8)
    prod_rows = env.assumption("A-PROD-ORDERS-ROWS", None)
    sandbox = env.sandbox()
    result = experiments.index_experiment(sandbox, candidates, fingerprints, exponent, prod_rows, env._fixture_orders)

    def register(evidence_id: str) -> None:
        for cid, c in result["candidates"].items():
            t = c["modeled_production_translation"]
            env.effects[cid] = OptimizationEffect(
                index_candidate=cid,
                cpu_multiplier_by_fingerprint=t["cpu_multiplier_by_fingerprint"],
                index_storage_gib=t["index_storage_gib"] or 0.0,
                evidence_ids=[evidence_id],
            )

    return ToolOutcome(
        title="Index experiment: " + ", ".join(candidates),
        data=result,
        cited=[],
        caveats=[result["caveat"], "Production translation uses assumption A-CPU-ROWS-EXPONENT."],
        after_created=register,
    )


def _rewrite_equivalence(env: ToolEnvironment, args: dict, role: RoleId) -> ToolOutcome:
    rewrite_ids = _list(args, "rewrite_ids", list(fx.REWRITES))
    result = experiments.rewrite_equivalence(env.sandbox(), rewrite_ids)
    exponent = env.assumption("A-CPU-ROWS-EXPONENT", 0.8)

    def register(evidence_id: str) -> None:
        """Make each rewrite modelable, so it can be compared with buying capacity rather than only discussed."""
        for rid, r in result["rewrites"].items():
            ratio = r["work_ratio"]
            multiplier = round(ratio ** exponent, 4) if ratio else 1.0
            env.effects[rid] = OptimizationEffect(
                index_candidate=rid, kind="rewrite",
                cpu_multiplier_by_fingerprint={r["original"]: multiplier},
                equivalent=r["equivalent_on_fixtures"],
                evidence_ids=[evidence_id],
            )

    return ToolOutcome(title="Rewrite equivalence: " + ", ".join(rewrite_ids), data=result, cited=[],
                       caveats=[result["caveat"], "Production translation uses assumption A-CPU-ROWS-EXPONENT."],
                       after_created=register)


def _explain_query(env: ToolEnvironment, args: dict, role: RoleId) -> ToolOutcome:
    fid = args.get("fingerprint_id")
    if fid not in fx.QUERIES:
        raise ValueError(f"fingerprint_id must be one of {sorted(fx.QUERIES)}")
    candidate = args.get("candidate")
    if candidate is not None and candidate not in fx.INDEX_CANDIDATES:
        raise ValueError(f"candidate must be one of {sorted(fx.INDEX_CANDIDATES)}")
    result = experiments.explain_query(env.sandbox(), fid, candidate)
    return ToolOutcome(title=f"Sandbox plan for {fid}" + (f" with {candidate}" if candidate else ""), data=result,
                       cited=[], caveats=[result["caveat"]])


def _lab_load_test(env: ToolEnvironment, args: dict, role: RoleId) -> ToolOutcome:
    if env.lab_mysql is None:
        raise RuntimeError("lab_load_test needs the local MySQL lab: start it and run with --sandbox mysql")
    from capacitylab.lab.runner import LAB_CAVEAT, LabConfig, PhaseSpec, run_lab

    campaign = next(iter(env.scenario.events_of("campaign")), None)
    default_multiplier = env.assumption(campaign.multiplier_assumption, 1.0) if campaign else 1.0
    multiplier = float(args.get("multiplier", default_multiplier))
    duration = min(max(float(args.get("duration_s", 15)), 3.0), 30.0)
    candidate = args.get("index_candidate")
    if candidate is not None and candidate not in fx.INDEX_CANDIDATES:
        raise ValueError(f"index_candidate must be one of {sorted(fx.INDEX_CANDIDATES)}")
    has_batch = bool(env.scenario.events_of("batch_job"))
    batch_in_test = bool(args.get("batch_in_test", has_batch))
    if candidate is None and batch_in_test == has_batch:
        raise ValueError("specify index_candidate and/or batch_in_test different from the control")
    phases = [
        PhaseSpec("CTRL", f"control: {multiplier:g}x focal tenant, batch {'on' if has_batch else 'off'}", multiplier, has_batch),
        PhaseSpec("TEST", f"test: {multiplier:g}x, batch {'on' if batch_in_test else 'off'}"
                  + (f", index {candidate}" if candidate else ""), multiplier, batch_in_test, candidate),
    ]
    items = run_lab(env.scenario, env.lab_mysql, LabConfig(duration_s=duration, total_qps=float(args.get("total_qps", 150))),
                    phases)
    by_id = {i.id: i for i in items}
    return ToolOutcome(
        title="Lab load test: " + " vs ".join(p.label for p in phases),
        data={"comparison": by_id["EV-LAB-CMP"].data,
              "latency_and_counters": {p.name: by_id[f"EV-LAB-{p.name}-LAT"].data for p in phases},
              "digests": {p.name: by_id[f"EV-LAB-{p.name}-DIG"].data["fingerprints"] for p in phases}},
        cited=[],
        caveats=[LAB_CAVEAT, "Timing-based: repeated runs vary; compare phases within one test, not across tests."],
        environment="lab",
    )


def _percona_duplicate_keys(env: ToolEnvironment, args: dict, role: RoleId) -> ToolOutcome:
    if env.lab_mysql is None:
        raise RuntimeError("percona_duplicate_keys needs the local MySQL lab: start it and run with --sandbox mysql")
    candidate = args.get("index_candidate")
    if candidate not in fx.INDEX_CANDIDATES:
        raise ValueError(f"index_candidate must be one of {sorted(fx.INDEX_CANDIDATES)}")
    from capacitylab.lab.percona import PerconaToolkit, PerconaUnavailable, parse_duplicate_keys
    from capacitylab.lab.runner import LAB_CAVEAT, LAB_DATABASE

    m = env.lab_mysql
    toolkit = PerconaToolkit(m)
    try:
        version = toolkit.version()
    except PerconaUnavailable as exc:
        raise RuntimeError(str(exc)) from exc
    sandbox = MySQLSandbox(m.host, m.port, m.user, m.password, LAB_DATABASE)
    try:
        fx.build(sandbox)
        spec = fx.INDEX_CANDIDATES[candidate]
        sandbox.create_index(spec["name"], spec["table"], spec["columns"])
        try:
            report = parse_duplicate_keys(toolkit.duplicate_keys(LAB_DATABASE))
        except PerconaUnavailable as exc:
            raise RuntimeError(str(exc)) from exc
        finally:
            sandbox.drop_index(spec["name"], spec["table"])
    finally:
        sandbox.close()
    return ToolOutcome(
        title=f"pt-duplicate-key-checker with {candidate} added",
        data={"index_candidate": candidate, "index": spec, "tool_version": version, "report": report},
        cited=[],
        caveats=[LAB_CAVEAT, "A redundant index may still be relied on by a statement; check plans before dropping."],
        environment="lab",
    )


def _analysis(fn: Callable, title: str) -> Callable[[ToolEnvironment, dict, RoleId], ToolOutcome]:
    def handler(env: ToolEnvironment, args: dict, role: RoleId) -> ToolOutcome:
        data, cited = fn(env, args, role)
        return ToolOutcome(title=title, data=data, cited=cited,
                           caveats=["Deterministic analysis of the cited evidence."])

    return handler


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        ToolSpec("capacity_forecast", "Model utilization, SLO latency, and cost for options over the horizon.",
                 {"option_ids": "list of option ids (default: all)",
                  "assumption_overrides": "object mapping assumption id to number, for sensitivity analysis"},
                 EvidenceKind.CALCULATION, Provenance.MODELED, _capacity_forecast),
        ToolSpec("cost_estimate", "Cost deltas and budget headroom for options.",
                 {"option_ids": "list of option ids (default: all)"},
                 EvidenceKind.CALCULATION, Provenance.MODELED, _cost_estimate),
        ToolSpec("index_experiment", "Create candidate indexes in the local sandbox and measure work, plans, size, and write overhead.",
                 {"candidates": f"list from {sorted(fx.INDEX_CANDIDATES)}",
                  "fingerprint_ids": f"list from {sorted(fx.QUERIES)}"},
                 EvidenceKind.EXPERIMENT_RESULT, Provenance.MEASURED, _index_experiment),
        ToolSpec("rewrite_equivalence", "Check candidate rewrites for result equivalence on edge-case fixtures and measure work.",
                 {"rewrite_ids": f"list from {sorted(fx.REWRITES)}"},
                 EvidenceKind.EXPERIMENT_RESULT, Provenance.MEASURED, _rewrite_equivalence),
        ToolSpec("explain_query", "Show the sandbox plan and work for a statement, optionally with a candidate index.",
                 {"fingerprint_id": f"one of {sorted(fx.QUERIES)}", "candidate": "optional index candidate"},
                 EvidenceKind.EXPERIMENT_RESULT, Provenance.MEASURED, _explain_query),
        ToolSpec("lab_load_test", "Run a real concurrent workload on the local MySQL lab: a control phase vs a test phase "
                 "with a candidate index and/or the batch job moved away. Returns measured latency, lock waits, and digests.",
                 {"index_candidate": f"optional, one of {sorted(fx.INDEX_CANDIDATES)}",
                  "batch_in_test": "boolean; false simulates moving the batch job out of the window",
                  "multiplier": "focal tenant traffic multiplier (default: scenario assumption)",
                  "duration_s": "seconds per phase, 3-30 (default 15)"},
                 EvidenceKind.EXPERIMENT_RESULT, Provenance.MEASURED, _lab_load_test, deterministic=False),
        ToolSpec("percona_duplicate_keys", "Add a candidate index in the local MySQL lab and run Percona's "
                 "pt-duplicate-key-checker to see whether any index becomes a duplicate or left prefix.",
                 {"index_candidate": f"one of {sorted(fx.INDEX_CANDIDATES)}"},
                 EvidenceKind.EXPERIMENT_RESULT, Provenance.OBSERVED, _percona_duplicate_keys, deterministic=False),
        ToolSpec("top_queries", "Rank statements by CPU share, rows examined share, or total latency.",
                 {"rank_by": "cpu | rows_examined | latency", "limit": "integer"},
                 EvidenceKind.CALCULATION, Provenance.OBSERVED,
                 _analysis(lambda env, a, r: analysis.top_queries(env.bundle, a.get("rank_by", "cpu"), int(a.get("limit", 5))),
                           "Top statements")),
        ToolSpec("tenant_skew", "Per-statement tenant concentration (tenant-scoped viewers see only their own share).",
                 {}, EvidenceKind.CALCULATION, Provenance.OBSERVED,
                 _analysis(lambda env, a, r: analysis.tenant_skew(
                     env.bundle, env.scenario.focal_tenant if ROLES[r].tenant_scoped else None), "Tenant skew"),
                 role_sensitive=True),
        ToolSpec("tenant_entitlement_review",
                 "Compare each tenant's plan with the CPU share its workload takes, at baseline and during campaigns "
                 "(tenant-scoped viewers see only their own).",
                 {}, EvidenceKind.CALCULATION, Provenance.MODELED,
                 _analysis(lambda env, a, r: tenant_entitlement_review(
                     env.scenario, env.bundle, env.scenario.focal_tenant if ROLES[r].tenant_scoped else None),
                     "Tenant entitlement review"),
                 role_sensitive=True),
        ToolSpec("table_growth_review", "Relate table size and growth to the statements that actually touch each table.",
                 {}, EvidenceKind.CALCULATION, Provenance.OBSERVED,
                 _analysis(lambda env, a, r: analysis.table_growth_review(env.bundle), "Table growth review")),
        ToolSpec("bottleneck_classifier", "Classify the bottleneck (CPU, memory/I-O, contention, connections, spills).",
                 {}, EvidenceKind.CALCULATION, Provenance.OBSERVED,
                 _analysis(lambda env, a, r: analysis.bottleneck_classifier(env.bundle), "Bottleneck classification")),
        ToolSpec("row_estimate_check", "Compare optimizer estimates with actual rows for a statement's plan evidence.",
                 {"fingerprint_id": "statement id with plan evidence"}, EvidenceKind.CALCULATION, Provenance.OBSERVED,
                 _analysis(lambda env, a, r: analysis.row_estimate_check(env.bundle, a.get("fingerprint_id", "")),
                           "Row estimate check")),
        ToolSpec("batch_reschedule_check", "Check candidate batch start times against deadlines and events.",
                 {"candidate_starts": "list of HH:MM"}, EvidenceKind.CALCULATION, Provenance.MODELED,
                 _analysis(lambda env, a, r: analysis.batch_reschedule_check(
                     env.scenario, env.bundle, _list(a, "candidate_starts")), "Batch reschedule check")),
    ]
}


LAB_TOOLS = frozenset({"lab_load_test", "percona_duplicate_keys"})  # need the local MySQL lab


def tool_catalog(role: RoleId, lab_available: bool = True) -> list[dict]:
    """Tools offered to a role. Lab tools are left out when no MySQL lab is attached, so they are never requested."""
    return [{"name": s.name, "description": s.description, "arguments": s.arguments}
            for s in TOOLS.values() if s.name in ROLES[role].tools and (lab_available or s.name not in LAB_TOOLS)]


def _digest(data: dict) -> str:
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    engine = str(result.get("engine", ""))

    def strip(node):
        if isinstance(node, dict):
            drop = NONDETERMINISTIC_KEYS | (MYSQL_TIMING_KEYS if engine.startswith("MySQL") else set())
            return {k: strip(v) for k, v in node.items() if k not in drop}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return hashlib.sha256(json.dumps(strip(data), sort_keys=True, default=str).encode()).hexdigest()


def dedupe_key(request: ToolRequest, role: RoleId) -> str:
    try:
        args = json.loads(request.arguments_json or "{}")
    except ValueError:
        args = {"_unparseable": request.arguments_json}
    spec = TOOLS.get(request.tool)
    scope = role.value if spec and spec.role_sensitive else "*"
    return json.dumps([request.tool, args, scope], sort_keys=True)


def execute_tool(env: ToolEnvironment, call_id: str, round_no: int, roles: list[RoleId], request: ToolRequest) \
        -> tuple[ToolCallRecord, EvidenceItem | None]:
    record = ToolCallRecord(call_id=call_id, round=round_no, requested_by=[r.value for r in roles], tool=request.tool,
                            arguments={}, purpose=request.purpose, status="ok")
    try:
        args = json.loads(request.arguments_json or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments_json must be a JSON object")
    except ValueError as exc:
        record.status, record.error = "error", f"bad arguments: {exc}"
        return record, None
    record.arguments = args
    spec = TOOLS.get(request.tool)
    if spec is None:
        record.status, record.error = "denied", f"unknown tool {request.tool}"
        return record, None
    permitted = [r for r in roles if spec.name in ROLES[r].tools]
    if not permitted:
        record.status, record.error = "denied", f"{', '.join(r.value for r in roles)} may not use {spec.name}"
        return record, None
    started = time.perf_counter()
    try:
        outcome = spec.handler(env, args, permitted[0])
    except (ValueError, LookupError, KeyError, RuntimeError) as exc:
        record.status, record.error = "error", str(exc)
        return record, None
    finally:
        record.duration_ms = int((time.perf_counter() - started) * 1000)
    evidence_id = env.bundle.next_id("EV-TOOL")
    data = {"tool": spec.name, "arguments": args, "cited_evidence_ids": outcome.cited, "result": outcome.data}
    synthetic = all(env.bundle.get(c).synthetic for c in outcome.cited) if outcome.cited else True
    item = EvidenceItem(
        id=evidence_id,
        kind=spec.kind,
        title=outcome.title,
        provenance=spec.provenance,
        synthetic=synthetic,
        environment=outcome.environment,
        source=f"tool:{spec.name}",
        method=f"{spec.name} requested by {', '.join(record.requested_by)} in round {round_no}",
        cluster_id=env.scenario.cluster.id,
        data=data,
        caveats=outcome.caveats,
        produced_by_tool_call=call_id,
    )
    env.bundle.add(item)
    if outcome.after_created:
        outcome.after_created(evidence_id)
    record.evidence_id = evidence_id
    record.result_digest = _digest(data)
    return record, item
