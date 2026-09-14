"""Synthesis of a run into a decision record. It reports positions; it does not manufacture consensus."""

from __future__ import annotations

from collections import Counter, defaultdict

from pydantic import BaseModel, Field

from capacitylab.capacity.options import OptimizationEffect, OptionOutcome, build_context, evaluate_all
from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.scenarios.models import Scenario
from capacitylab.simulation.schema import StakeholderTurn
from capacitylab.simulation.tools import ToolCallRecord

CAVEATS = [
    "Agreement among simulated stakeholders is not evidence that a recommendation is correct.",
    "Option outcomes are modeled with explicit assumptions; local sandbox measurements are not production results.",
    "The scenario, tenants, and evidence are synthetic.",
]


class RolePosition(BaseModel):
    role: str
    position: str
    confidence: str
    rationale: str
    position_history: list[str]


class Disagreement(BaseModel):
    kind: str  # "positions" | "open_challenge"
    roles: list[str]
    summary: str
    evidence_ids: list[str] = Field(default_factory=list)


class DecisionRecord(BaseModel):
    final_positions: list[RolePosition]
    support: dict[str, list[str]]
    distinct_positions: int
    disagreements: list[Disagreement]
    missing_evidence: list[dict]
    open_tool_requests: list[dict]
    optimization_proposals: list[dict]
    option_outcomes: list[OptionOutcome]
    outcome_basis: str
    validation: dict
    caveats: list[str] = Field(default_factory=lambda: list(CAVEATS))


def synthesize(
    scenario: Scenario,
    bundle: EvidenceBundle,
    turns: list[StakeholderTurn],
    tool_calls: list[ToolCallRecord],
    effects: dict[str, OptimizationEffect],
) -> DecisionRecord:
    by_role: dict[str, list[StakeholderTurn]] = defaultdict(list)
    for turn in turns:
        by_role[turn.role].append(turn)
    finals = []
    for role, role_turns in by_role.items():
        last = role_turns[-1]
        finals.append(RolePosition(role=role, position=last.draft.position, confidence=last.draft.confidence,
                                   rationale=last.draft.position_rationale,
                                   position_history=[t.draft.position for t in role_turns]))
    support: dict[str, list[str]] = defaultdict(list)
    for p in finals:
        support[p.position].append(p.role)

    disagreements: list[Disagreement] = []
    decided = {k: v for k, v in support.items() if k != "undecided"}
    if len(decided) > 1:
        disagreements.append(Disagreement(
            kind="positions",
            roles=sorted(r for rs in decided.values() for r in rs),
            summary="; ".join(f"{opt}: {', '.join(sorted(rs))}" for opt, rs in sorted(decided.items())),
        ))
    final_round = max((t.round for t in turns), default=0)
    final_position = {p.role: p.position for p in finals}
    latest_challenge: dict[tuple[str, str], tuple] = {}
    for turn in turns:
        if turn.round < final_round - 1:
            continue
        for c in turn.draft.challenges:
            latest_challenge[(turn.role, c.target_role)] = (turn, c)  # later rounds replace earlier ones
    for (challenger, target), (_turn, c) in latest_challenge.items():
        if final_position.get(target) != final_position.get(challenger):
            disagreements.append(Disagreement(
                kind="open_challenge", roles=[challenger, target],
                summary=f"{challenger} -> {target} ({c.target_statement}): {c.reason}",
                evidence_ids=c.evidence_ids,
            ))

    missing: dict[str, set[str]] = defaultdict(set)
    for p in finals:
        for m in by_role[p.role][-1].draft.missing_evidence:
            missing[m].add(p.role)

    proposals = []
    for p in finals:
        for prop in by_role[p.role][-1].draft.optimization_proposals:
            proposals.append({"role": p.role, **prop.model_dump()})

    findings = [f for t in turns for f in t.findings]
    outcomes = evaluate_all(build_context(scenario, bundle), effects)
    return DecisionRecord(
        final_positions=finals,
        support=dict(support),
        distinct_positions=len(decided),
        disagreements=disagreements,
        missing_evidence=[{"item": k, "raised_by": sorted(v)} for k, v in sorted(missing.items())],
        open_tool_requests=[{"call_id": r.call_id, "tool": r.tool, "requested_by": r.requested_by, "status": r.status,
                             "purpose": r.purpose} for r in tool_calls if r.status.startswith("skipped")],
        optimization_proposals=proposals,
        option_outcomes=outcomes,
        outcome_basis=("Computed by CapacityLab for this report with base scenario assumptions and the optimization "
                       "effects measured during the run. Not produced by any stakeholder."),
        validation={"errors": sum(f.severity == "error" for f in findings),
                    "warnings": sum(f.severity == "warning" for f in findings),
                    "by_code": dict(Counter(f.code for f in findings))},
    )
