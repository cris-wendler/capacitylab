"""Provider interface and the context pack each stakeholder receives."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from typing import Protocol

from capacitylab.evidence.bundle import Contradiction, MissingEvidence
from capacitylab.evidence.models import EvidenceItem
from capacitylab.scenarios.models import Scenario
from capacitylab.simulation.roles import RoleDefinition
from capacitylab.simulation.schema import ModelCall, StakeholderTurn, TurnDraft
from capacitylab.simulation.tools import ToolCallRecord

PROMPT_TEMPLATE = (resources.files("capacitylab") / "simulation" / "prompts" / "stakeholder_system.md").read_text()
PROMPT_VERSION = "stakeholder-v1-" + hashlib.sha256(PROMPT_TEMPLATE.encode()).hexdigest()[:8]


class ProviderError(RuntimeError):
    """A turn could not be produced. A call that reached the model still carries its cost, so spend stays accurate."""

    def __init__(self, message: str, cost_usd: float = 0.0, input_tokens: int = 0, output_tokens: int = 0):
        super().__init__(message)
        self.cost_usd = cost_usd
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@dataclass
class TurnContext:
    scenario: Scenario
    role: RoleDefinition
    round: int
    max_rounds: int
    visible: list[EvidenceItem]
    gaps: list[MissingEvidence]
    contradictions: list[Contradiction]
    prior_turns: list[StakeholderTurn]
    tool_records: list[ToolCallRecord]
    tool_catalog: list[dict]
    per_turn_tool_limit: int
    remaining_tool_calls: int

    @property
    def visible_ids(self) -> set[str]:
        return {i.id for i in self.visible}

    def own_previous(self) -> StakeholderTurn | None:
        mine = [t for t in self.prior_turns if t.role == self.role.id.value]
        return mine[-1] if mine else None


@dataclass
class ProviderResult:
    draft: TurnDraft
    call: ModelCall


class TurnProvider(Protocol):
    name: str
    model: str
    mocked: bool

    def generate_turn(self, ctx: TurnContext) -> ProviderResult: ...

    def estimate_max_cost_usd(self, ctx: TurnContext) -> float: ...


def render_system_prompt(ctx: TurnContext) -> str:
    role = ctx.role

    def bullets(items):
        return "\n".join(f"- {i}" for i in items)

    return PROMPT_TEMPLATE.format(
        title=role.title,
        responsibilities=bullets(role.responsibilities),
        constraints=bullets(role.constraints),
        priorities=bullets(role.priorities),
        per_turn_tools=ctx.per_turn_tool_limit,
    )


_BULKY_KEYS = {"utilization_by_slot", "total_qps_by_fingerprint", "raw_text", "model_assumptions", "assumptions_used"}


def compact(node, max_list: int = 12):
    """Shrink evidence payloads for the model: drop bulky series, cap long lists, round floats.

    Validation still runs against the full evidence, so rounded numbers remain grounded.
    """
    if isinstance(node, dict):
        return {k: compact(v, max_list) for k, v in node.items() if k not in _BULKY_KEYS}
    if isinstance(node, list):
        head = [compact(v, max_list) for v in node[:max_list]]
        return head + ([f"... {len(node) - max_list} more items omitted"] if len(node) > max_list else [])
    if isinstance(node, float):
        return round(node, 4)
    return node


_TOOL_EVIDENCE_PREFIX = "EV-TOOL-"


def context_blocks(ctx: TurnContext) -> tuple[str, str]:
    """The pack in two parts: one that only grows by appending, and one that changes every round.

    Check results sort last, so a later round's first block starts with the previous round's text. That prefix can be
    cached by the provider, which is what keeps a multi-round run affordable: nothing is left out of the pack.
    """
    visible_ids = ctx.visible_ids
    latest: dict[str, StakeholderTurn] = {}
    for t in ctx.prior_turns:
        latest[t.role] = t
    others = []
    for role, turn in sorted(latest.items()):
        d = turn.draft
        others.append({
            "role": role,
            "round": turn.round,
            "position": d.position,
            "confidence": d.confidence,
            "rationale": d.position_rationale,
            "claims": [
                {"statement": c.statement, "basis": c.basis,
                 "evidence_ids": [e if e in visible_ids else f"{e} (not accessible to you)" for e in c.evidence_ids]}
                for c in d.claims
            ],
            "challenges": [c.model_dump() for c in d.challenges],
            "missing_evidence": d.missing_evidence,
        })
    header = {
        "max_rounds": ctx.max_rounds,
        "decision_question": ctx.scenario.decision_question.strip(),
        "scenario": {"id": ctx.scenario.id, "title": ctx.scenario.title, "synthetic": True,
                     "horizon": ctx.scenario.horizon.model_dump(mode="json"),
                     "focal_tenant": ctx.scenario.focal_tenant},
        "options": [o.model_dump() for o in ctx.scenario.options],
        "scenario_assumptions": [a.model_dump() for a in ctx.scenario.assumptions],
        "tools_available_to_you": ctx.tool_catalog,
    }
    evidence = [
        {"id": i.id, "kind": i.kind.value, "title": i.title, "provenance": i.label, "data": compact(i.data),
         "caveats": i.caveats}
        for i in sorted(ctx.visible, key=lambda x: (x.id.startswith(_TOOL_EVIDENCE_PREFIX), x.id))
    ]
    per_round = {
        "round": ctx.round,
        "final_round": ctx.round == ctx.max_rounds,
        "known_missing_evidence": [g.model_dump() for g in ctx.gaps],
        "contradictions_between_your_sources": [c.model_dump() for c in ctx.contradictions],
        "your_tool_requests_so_far": [r.model_dump(exclude={"duration_ms", "result_digest"}) for r in ctx.tool_records],
        "remaining_tool_calls_in_run": ctx.remaining_tool_calls,
        "other_stakeholders_latest_turns": others,
        "instruction": (
            "Produce your turn. Request tests you need; they run after this round and their results appear next round."
            if ctx.round < ctx.max_rounds
            else "Final round: tool requests will be recorded as open but not executed. State your final position."
        ),
    }
    def dump(pack) -> str:
        return json.dumps(pack, separators=(",", ":"), sort_keys=False, default=str)

    # One line per evidence item, never an array: a later round appends lines and leaves earlier bytes untouched,
    # which is what lets the provider reuse the cached prefix.
    return "\n".join([dump(header), *(dump(item) for item in evidence)]), dump(per_round)


def render_context_pack(ctx: TurnContext) -> str:
    """Everything the stakeholder may see this turn, as one JSON pair (used for cost estimates and replay)."""
    return "\n".join(context_blocks(ctx))
