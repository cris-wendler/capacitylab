"""Structured stakeholder output. `TurnDraft` is exactly what a provider must return."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Basis = Literal["observed", "forecast", "assumption", "modeled", "measured", "judgment"]


class Claim(BaseModel):
    statement: str
    evidence_ids: list[str]
    basis: Basis


class ToolRequest(BaseModel):
    tool: str
    arguments_json: str = Field(description="JSON object with the tool arguments, e.g. {\"option_ids\": [\"OPT-KEEP\"]}")
    purpose: str


class Challenge(BaseModel):
    target_role: str = Field(description="Role id: database_engineer, application_owner, reliability_engineer, "
                                         "finops_analyst, or tenant_representative")
    target_statement: str
    reason: str
    evidence_ids: list[str]


class OptimizationProposal(BaseModel):
    proposal_id: str
    target_fingerprint: str
    supporting_evidence_ids: list[str]
    hypothesis: str
    uncertainty: str
    proposed_change: str
    tradeoffs: list[str]
    validation_method: str
    rollback: str
    result_status: Literal["not_tested", "modeled", "measured_local"]
    result_evidence_ids: list[str]


class TurnDraft(BaseModel):
    position: str = Field(description="One option id, or 'undecided'")
    position_rationale: str
    confidence: Literal["low", "medium", "high"]
    claims: list[Claim]
    assumptions: list[str]
    tool_requests: list[ToolRequest]
    challenges: list[Challenge]
    missing_evidence: list[str]
    optimization_proposals: list[OptimizationProposal]
    revised_from_previous: bool
    revision_reason: str


class ValidationFinding(BaseModel):
    code: Literal[
        "unknown_position",
        "missing_citation",
        "invalid_citation",
        "inaccessible_citation",
        "ungrounded_number",
        "unknown_tool",
        "tool_not_permitted",
        "bad_tool_arguments",
        "unknown_target_role",
        "unsupported_result_status",
    ]
    severity: Literal["error", "warning"]
    detail: str
    location: str


class ModelCall(BaseModel):
    provider: str
    model: str
    prompt_version: str
    mocked: bool
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    request_id: str | None = None
    stop_reason: str | None = None


class StakeholderTurn(BaseModel):
    turn_id: str
    round: int
    role: str
    draft: TurnDraft
    model_call: ModelCall
    findings: list[ValidationFinding]
    visible_evidence_ids: list[str]
