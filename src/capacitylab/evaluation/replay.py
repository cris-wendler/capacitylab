"""Replay a run ledger: re-execute every recorded tool call and rebuild the decision without a model."""

from __future__ import annotations

import json
from collections.abc import Callable

from pydantic import BaseModel, Field

from capacitylab.diagnostics.sandbox import Sandbox, SQLiteSandbox
from capacitylab.scenarios.loader import load_scenario
from capacitylab.simulation.decision import synthesize
from capacitylab.simulation.roles import RoleId
from capacitylab.simulation.run import SimulationRun, scenario_digest
from capacitylab.simulation.schema import ToolRequest
from capacitylab.simulation.tools import TOOLS, ToolEnvironment, execute_tool

SANDBOX_TOOLS = {"index_experiment", "rewrite_equivalence", "explain_query"}


class ReplayReport(BaseModel):
    run_id: str
    scenario_match: bool
    initial_evidence_match: bool
    tool_calls_replayed: int = 0
    identical: int = 0
    mismatches: list[dict] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)
    decision_match: bool = False
    notes: list[str] = Field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.scenario_match and self.initial_evidence_match and not self.mismatches and self.decision_match


def replay(run: SimulationRun, sandbox_factory: Callable[[], Sandbox] | None = None) -> ReplayReport:
    scenario, bundle = load_scenario(run.scenario_id)
    for item in run.extra_evidence_items:
        bundle.add(item.model_copy(deep=True))
    report = ReplayReport(
        run_id=run.run_id,
        scenario_match=scenario_digest(scenario) == run.scenario_digest,
        initial_evidence_match=bundle.digest() == run.initial_evidence_digest,
    )
    recorded_engine = run.sandbox_engine or ""
    if sandbox_factory is None and recorded_engine and not recorded_engine.startswith("SQLite"):
        report.notes.append(f"Run used {recorded_engine}; pass a matching sandbox to replay sandbox tools.")
    factory = sandbox_factory or (SQLiteSandbox if not recorded_engine or recorded_engine.startswith("SQLite") else None)
    env = ToolEnvironment(scenario, bundle.copy(), factory or SQLiteSandbox)
    recorded_items = {e.id: e for e in run.tool_evidence}
    try:
        for record in run.tool_calls:
            if record.status.startswith("skipped"):
                continue
            spec = TOOLS.get(record.tool)
            reason = None
            if spec is not None and not spec.deterministic:
                reason = "non-deterministic lab measurement; recorded result reused, not re-executed"
            elif factory is None and record.tool in SANDBOX_TOOLS:
                reason = "sandbox engine unavailable; recorded result reused"
            if reason:
                report.skipped.append({"call_id": record.call_id, "tool": record.tool, "reason": reason})
                if record.evidence_id in recorded_items:  # keep later evidence ids aligned with the ledger
                    env.bundle.add(recorded_items[record.evidence_id].model_copy(deep=True))
                continue
            request = ToolRequest(tool=record.tool, arguments_json=json.dumps(record.arguments), purpose=record.purpose)
            new, _ = execute_tool(env, record.call_id, record.round, [RoleId(r) for r in record.requested_by], request)
            report.tool_calls_replayed += 1
            same = (new.status == record.status and new.result_digest == record.result_digest
                    and new.evidence_id == record.evidence_id)
            if same:
                report.identical += 1
            else:
                report.mismatches.append({"call_id": record.call_id, "tool": record.tool,
                                          "recorded": [record.status, record.result_digest],
                                          "replayed": [new.status, new.result_digest]})
        if run.decision is not None:
            rebuilt = synthesize(scenario, env.bundle, run.turns, run.tool_calls, env.effects)
            report.decision_match = rebuilt.model_dump(mode="json") == run.decision.model_dump(mode="json")
        report.notes.append("Stakeholder turns are taken from the ledger; no model is called during replay.")
    finally:
        env.close()
    return report
