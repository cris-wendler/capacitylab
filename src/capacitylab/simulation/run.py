"""The run ledger: everything needed to audit, report, and replay a simulation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from capacitylab import __version__
from capacitylab.evidence.models import EvidenceItem
from capacitylab.scenarios.models import Scenario
from capacitylab.scenarios.state import RunState
from capacitylab.scenarios.validate import ValidationIssue
from capacitylab.simulation.decision import DecisionRecord
from capacitylab.simulation.schema import StakeholderTurn
from capacitylab.simulation.tools import ToolCallRecord

SCHEMA_VERSION = "capacitylab.run/v1"


class RunConfig(BaseModel):
    max_rounds: int = 3
    max_tool_calls: int = 40
    max_tool_calls_per_turn: int = 4
    max_usd: float = 3.0
    sandbox: str = "sqlite"
    stop_when_stable: bool = True


class SimulationRun(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    created_at: datetime
    capacitylab_version: str = __version__
    scenario_id: str
    scenario_digest: str
    initial_evidence_digest: str
    provider: str
    model: str
    prompt_version: str
    mocked: bool
    config: RunConfig
    state: RunState = Field(default_factory=RunState)
    rounds_completed: int = 0
    turns: list[StakeholderTurn] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    tool_evidence: list[EvidenceItem] = Field(default_factory=list)
    extra_evidence_items: list[EvidenceItem] = Field(default_factory=list)  # imported or lab evidence merged at start
    spend_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    sandbox_engine: str | None = None
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    decision: DecisionRecord | None = None

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.model_dump_json(indent=2))
        return p

    @classmethod
    def load(cls, path: str | Path) -> SimulationRun:
        return cls.model_validate_json(Path(path).read_text())


def scenario_digest(scenario: Scenario) -> str:
    return hashlib.sha256(json.dumps(scenario.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
