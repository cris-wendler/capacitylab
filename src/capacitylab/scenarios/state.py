# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run lifecycle state machine."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    CREATED = "created"
    VALIDATED = "validated"
    INVALID = "invalid"
    DELIBERATING = "deliberating"
    EXECUTING_TOOLS = "executing_tools"
    CONCLUDED = "concluded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


TERMINAL = {RunStatus.INVALID, RunStatus.CONCLUDED, RunStatus.BUDGET_EXHAUSTED, RunStatus.FAILED}

TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {RunStatus.VALIDATED, RunStatus.INVALID},
    RunStatus.VALIDATED: {RunStatus.DELIBERATING, RunStatus.FAILED},
    RunStatus.DELIBERATING: {
        RunStatus.EXECUTING_TOOLS,
        RunStatus.CONCLUDED,
        RunStatus.BUDGET_EXHAUSTED,
        RunStatus.FAILED,
    },
    RunStatus.EXECUTING_TOOLS: {RunStatus.DELIBERATING, RunStatus.BUDGET_EXHAUSTED, RunStatus.FAILED},
}


class InvalidTransition(RuntimeError):
    pass


class Transition(BaseModel):
    from_status: RunStatus
    to_status: RunStatus
    reason: str
    at: datetime


class RunState(BaseModel):
    status: RunStatus = RunStatus.CREATED
    history: list[Transition] = Field(default_factory=list)

    def transition(self, to: RunStatus, reason: str) -> None:
        allowed = TRANSITIONS.get(self.status, set())
        if to not in allowed:
            raise InvalidTransition(f"cannot move from {self.status} to {to}")
        self.history.append(Transition(from_status=self.status, to_status=to, reason=reason, at=datetime.now(UTC)))
        self.status = to

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL
