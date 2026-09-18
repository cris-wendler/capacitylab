# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evidence items: every fact a stakeholder may cite, with explicit provenance."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

EVIDENCE_ID_RE = re.compile(r"^EV-[A-Z0-9]+(?:-[A-Z0-9]+)*$")


class Provenance(StrEnum):
    """How a piece of evidence came to exist. These categories must never be blurred in reports."""

    OBSERVED = "observed"  # collected from a running system (or a synthetic fixture standing in for one)
    FORECAST = "forecast"  # projected from observed data or seeded synthetic generators
    ASSUMPTION = "assumption"  # explicit scenario assumption; not measured anywhere
    MODELED = "modeled"  # output of a deterministic calculation or model
    MEASURED = "measured"  # measured by an experiment CapacityLab ran (local sandbox)


PROVENANCE_LABELS = {
    Provenance.OBSERVED: "Observed",
    Provenance.FORECAST: "Forecast",
    Provenance.ASSUMPTION: "Scenario assumption",
    Provenance.MODELED: "Modeled outcome",
    Provenance.MEASURED: "Experimentally measured",
}


class EvidenceKind(StrEnum):
    TOPOLOGY = "topology"
    METRIC_SERIES = "metric_series"
    METRIC_SUMMARY = "metric_summary"
    QUERY_DIGEST = "query_digest"
    QUERY_PLAN = "query_plan"
    SCHEMA = "schema"
    TABLE_STATS = "table_stats"
    WORKLOAD_FORECAST = "workload_forecast"
    EVENT_CALENDAR = "event_calendar"
    RELEASE_CALENDAR = "release_calendar"
    BATCH_SCHEDULE = "batch_schedule"
    BATCH_RUN_LOG = "batch_run_log"
    SLO = "slo"
    RELIABILITY_HISTORY = "reliability_history"
    RATE_CARD = "rate_card"
    BUDGET = "budget"
    TENANT_PROFILE = "tenant_profile"
    TENANT_ENTITLEMENTS = "tenant_entitlements"
    EXPERIMENT_RESULT = "experiment_result"
    CALCULATION = "calculation"


class Assertion(BaseModel):
    """A comparable claim an evidence item makes, used to detect contradictions between sources."""

    key: str
    value: float | str
    unit: str | None = None
    tolerance: float = 0.0  # relative tolerance for numeric comparison


class EvidenceItem(BaseModel):
    id: str
    kind: EvidenceKind
    title: str
    provenance: Provenance
    synthetic: bool = True
    # fixture: hand-authored scenario data; lab: measured on the local MySQL lab; import: supplied by the user
    environment: Literal["fixture", "lab", "import"] = "fixture"
    source: str
    method: str = ""
    tenant_id: str | None = None
    cluster_id: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    assertions: list[Assertion] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    produced_by_tool_call: str | None = None

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not EVIDENCE_ID_RE.match(v):
            raise ValueError(f"invalid evidence id {v!r}; expected e.g. EV-MET-001")
        return v

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    @property
    def label(self) -> str:
        base = PROVENANCE_LABELS[self.provenance]
        if self.environment == "lab":
            return f"{base} (local MySQL lab, synthetic workload; not production)"
        if self.environment == "import":
            return f"{base} (imported file)"
        if self.synthetic and self.provenance in (Provenance.OBSERVED, Provenance.FORECAST):
            return f"{base} (synthetic fixture)"
        if self.provenance == Provenance.MEASURED:
            return f"{base} (local sandbox, not production)"
        return base

    def redacted_for_tenant(self, tenant_id: str) -> EvidenceItem:
        """Return a copy exposing only `tenant_id`'s share of any per-tenant breakdowns."""
        clone = self.model_copy(deep=True)
        clone.data = _redact_by_tenant(copy.deepcopy(self.data), tenant_id)
        if clone.data != self.data:
            clone.caveats = [*clone.caveats, "Other tenants' breakdowns redacted for this viewer."]
        return clone


def _redact_by_tenant(node: Any, tenant_id: str) -> Any:
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "by_tenant" and isinstance(value, dict):
                out[key] = {k: v for k, v in value.items() if k == tenant_id}
            else:
                out[key] = _redact_by_tenant(value, tenant_id)
        return out
    if isinstance(node, list):
        return [_redact_by_tenant(v, tenant_id) for v in node]
    return node


def iter_numbers(node: Any):
    """Yield every numeric leaf (including numbers embedded in strings) of an evidence payload."""
    if isinstance(node, bool):
        return
    if isinstance(node, int | float):
        yield float(node)
    elif isinstance(node, str):
        for m in re.finditer(r"-?\d+(?:\.\d+)?", node.replace(",", "")):
            yield float(m.group())
    elif isinstance(node, dict):
        for v in node.values():
            yield from iter_numbers(v)
    elif isinstance(node, list | tuple):
        for v in node:
            yield from iter_numbers(v)
