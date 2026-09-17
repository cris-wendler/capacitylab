# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scenario definitions: topology, tenants, events, decision options, and explicit assumptions."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

from capacitylab.evidence.bundle import MissingEvidence
from capacitylab.evidence.normalize import TenantAttribution
from capacitylab.workload.generator import WorkloadSpec


def hhmm_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    total = int(hours) * 60 + int(minutes)
    if not 0 <= total <= 24 * 60 or not 0 <= int(minutes) < 60:
        raise ValueError(f"invalid time {value!r}")
    return total


class Horizon(BaseModel):
    date: date
    start: str
    end: str
    slot_minutes: int = 15

    @field_validator("start", "end")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        hhmm_to_minutes(v)
        return v

    @property
    def start_minutes(self) -> int:
        return hhmm_to_minutes(self.start)

    @property
    def end_minutes(self) -> int:
        return hhmm_to_minutes(self.end)

    def slot_starts(self) -> list[int]:
        return list(range(self.start_minutes, self.end_minutes, self.slot_minutes))

    def slot_labels(self) -> list[str]:
        return [f"{m // 60:02d}:{m % 60:02d}" for m in self.slot_starts()]

    def slot_index(self, hhmm: str) -> int:
        """Index of the slot containing `hhmm`, clamped to [0, n_slots]."""
        minutes = hhmm_to_minutes(hhmm)
        idx = (minutes - self.start_minutes) // self.slot_minutes
        return max(0, min(len(self.slot_starts()), idx))


class ClusterSpec(BaseModel):
    id: str
    engine_assumption: str
    writer_instance: str
    reader_instances: list[str] = Field(default_factory=list)
    evaluated_node: Literal["writer", "reader"] = "writer"
    working_set_gib: float | None = None
    buffer_pool_fraction_assumption: float = 0.75


class TenantSpec(BaseModel):
    id: str
    label: str
    tier: str = "standard"
    synthetic: Literal[True] = True


class TenantIsolation(BaseModel):
    model: Literal["shared_schema_tenant_column", "schema_per_tenant", "cluster_per_tenant"]
    attribution: TenantAttribution
    notes: str = ""


class CampaignEvent(BaseModel):
    kind: Literal["campaign"]
    id: str
    tenant: str
    start: str
    end: str
    multiplier_assumption: str


class BatchEvent(BaseModel):
    kind: Literal["batch_job"]
    id: str
    name: str
    start: str
    duration_minutes: int
    cpu_cores: float
    deadline_next_day: str
    contended_fingerprints: list[str] = Field(default_factory=list)


class ReleaseEvent(BaseModel):
    kind: Literal["release"]
    id: str
    name: str
    start: str
    multiplier_assumption: str


Event = Annotated[CampaignEvent | BatchEvent | ReleaseEvent, Field(discriminator="kind")]

OptionKind = Literal["keep", "scale_temporary", "scale_season", "optimize_index", "reschedule_batch", "resize_permanent",
                     "combined"]


class OptionSpec(BaseModel):
    id: str
    kind: OptionKind
    label: str
    params: dict[str, Any] = Field(default_factory=dict)


class Assumption(BaseModel):
    id: str
    statement: str
    value: float | str | None = None
    unit: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Budgets(BaseModel):
    max_rounds: int = 3
    max_tool_calls: int = 40
    max_tool_calls_per_turn: int = 4
    max_usd: float = 3.0


class RevenueAtRisk(BaseModel):
    """How to put a price on a missed SLO during an event: every slot in the event window where any of these SLOs is
    breached loses `loss_share` of the revenue earned in that slot. Both inputs are named assumptions."""

    event_id: str
    slo_ids: list[str]
    revenue_per_hour_assumption: str
    loss_share_assumption: str


class GroundTruth(BaseModel):
    """Known truth of a synthetic scenario. Used only by evaluation; never shown to stakeholders."""

    assumption_overrides: dict[str, float] = Field(default_factory=dict)
    root_cause_fingerprints: list[str] = Field(default_factory=list)
    expected_gap_flags: list[str] = Field(default_factory=list)
    notes: str = ""


class Scenario(BaseModel):
    id: str
    title: str
    summary: str
    decision_question: str
    synthetic: Literal[True] = True
    horizon: Horizon
    cluster: ClusterSpec
    tenant_isolation: TenantIsolation
    tenants: list[TenantSpec]
    focal_tenant: str
    workload: WorkloadSpec
    events: list[Event]
    utilization_threshold_pct: float = 80.0
    options: list[OptionSpec]
    assumptions: list[Assumption]
    missing_evidence: list[MissingEvidence] = Field(default_factory=list)
    budgets: Budgets = Field(default_factory=Budgets)
    evidence_file: str
    sandbox_fixture: str = "retail_v1"
    ground_truth: GroundTruth | None = None
    revenue_at_risk: RevenueAtRisk | None = None

    def assumption(self, assumption_id: str) -> Assumption:
        for a in self.assumptions:
            if a.id == assumption_id:
                return a
        raise KeyError(assumption_id)

    def option(self, option_id: str) -> OptionSpec:
        for o in self.options:
            if o.id == option_id:
                return o
        raise KeyError(option_id)

    def events_of(self, kind: str) -> list:
        return [e for e in self.events if e.kind == kind]

    def public_view(self) -> dict:
        """Scenario description safe to show stakeholders (ground truth removed)."""
        return self.model_dump(mode="json", exclude={"ground_truth"})
