from __future__ import annotations

from pathlib import Path

import pytest

from capacitylab.diagnostics import fixture_db as fx
from capacitylab.diagnostics.sandbox import SQLiteSandbox
from capacitylab.scenarios.loader import load_scenario
from capacitylab.simulation.orchestrator import Orchestrator
from capacitylab.simulation.providers.mock import MockProvider
from capacitylab.simulation.schema import TurnDraft

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def campaign():
    return load_scenario("campaign-overlap")


@pytest.fixture(scope="session")
def downsize():
    return load_scenario("downsize-reader")


@pytest.fixture(scope="module")
def sandbox():
    sb = SQLiteSandbox()
    sb.stats = fx.build(sb)
    yield sb
    sb.close()


@pytest.fixture(scope="session")
def campaign_run(campaign):
    scenario, bundle = campaign
    return Orchestrator(scenario, bundle, MockProvider()).run()


@pytest.fixture(scope="session")
def downsize_run(downsize):
    scenario, bundle = downsize
    return Orchestrator(scenario, bundle, MockProvider()).run()


def make_draft(**overrides) -> TurnDraft:
    base = dict(
        position="undecided",
        position_rationale="test",
        confidence="low",
        claims=[],
        assumptions=[],
        tool_requests=[],
        challenges=[],
        missing_evidence=[],
        optimization_proposals=[],
        revised_from_previous=False,
        revision_reason="",
    )
    base.update(overrides)
    return TurnDraft(**base)
