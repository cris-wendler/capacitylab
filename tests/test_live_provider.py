# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional live evaluation. Spends real money. Run explicitly: `pytest -m live`."""

import os

import pytest

from capacitylab.scenarios.state import RunStatus
from capacitylab.simulation.orchestrator import Orchestrator
from capacitylab.simulation.providers.anthropic_provider import AnthropicProvider
from capacitylab.simulation.run import RunConfig

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
                       reason="no Anthropic credentials in the environment"),
]


def test_live_round_produces_structured_traceable_turns(campaign):
    scenario, bundle = campaign
    provider = AnthropicProvider(model=os.environ.get("CAPACITYLAB_MODEL", "claude-opus-5"), effort="low")
    run = Orchestrator(scenario, bundle, provider, RunConfig(max_rounds=2, max_usd=float(os.environ.get("CAPACITYLAB_MAX_USD_PER_RUN", "3")))).run()
    assert run.state.status in {RunStatus.CONCLUDED, RunStatus.BUDGET_EXHAUSTED}
    assert run.turns and all(t.model_call.mocked is False for t in run.turns)
    # No assertion on wording or on zero findings: validation findings are measurements of model behavior.
    print({t.turn_id: [f.code for f in t.findings] for t in run.turns})
