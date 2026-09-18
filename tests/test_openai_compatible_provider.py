# SPDX-License-Identifier: AGPL-3.0-or-later
"""The OpenAI-compatible provider, exercised with a fake SDK client (no network, no credentials)."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("openai", reason="pip install -e '.[openai]' to run these")

from capacitylab.factory import make_provider
from capacitylab.scenarios.state import RunStatus
from capacitylab.settings import Settings
from capacitylab.simulation.orchestrator import Orchestrator
from capacitylab.simulation.providers.openai_compatible import OpenAICompatibleProvider
from capacitylab.simulation.run import RunConfig
from capacitylab.simulation.schema import Claim
from tests.conftest import make_draft


class FakeCompletions:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses[min(len(self.calls), len(self.responses)) - 1]


def fake_response(text, finish_reason="stop", cached=0, refusal=None):
    return SimpleNamespace(
        id="chatcmpl-test", model="any-model",
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=text, refusal=refusal))],
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=200,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=cached)),
    )


def fake_client(*responses):
    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(*responses)))


def one_round(campaign, provider, **config):
    scenario, bundle = campaign
    return Orchestrator(scenario, bundle, provider, RunConfig(max_rounds=1, **config)).run()


def test_a_full_round_through_a_compatible_endpoint(campaign):
    draft = make_draft(claims=[Claim(statement="Digest reviewed.", evidence_ids=["EV-DIG-001"], basis="observed")])
    client = fake_client(fake_response(draft.model_dump_json()))
    provider = OpenAICompatibleProvider(model="any-model", client=client, input_usd_per_mtok=5, output_usd_per_mtok=25)
    run = one_round(campaign, provider, max_usd=10)
    assert run.state.status == RunStatus.CONCLUDED and not run.mocked and len(run.turns) == 5
    call = client.chat.completions.calls[0]
    assert call["model"] == "any-model" and "reasoning_effort" not in call
    assert call["response_format"]["type"] == "json_schema"
    assert "claims" in call["response_format"]["json_schema"]["schema"]["properties"]
    system, user = call["messages"]
    assert system["role"] == "system" and "Database engineer" in system["content"]
    header = json.loads(user["content"].split("\n", 1)[0])
    assert header["scenario"]["id"] == "campaign-overlap", "the same context pack as every other provider"
    first = run.turns[0].model_call
    assert first.provider == "openai" and first.cost_usd == pytest.approx(0.01) and first.request_id == "chatcmpl-test"


def test_cached_tokens_use_the_configured_multiplier_and_effort_is_opt_in(campaign):
    client = fake_client(fake_response(make_draft().model_dump_json(), cached=800))
    provider = OpenAICompatibleProvider(model="m", client=client, input_usd_per_mtok=5, output_usd_per_mtok=25,
                                        cached_input_multiplier=0.5, reasoning_effort="low")
    run = one_round(campaign, provider, max_usd=10)
    # 200 uncached + 800 cached at half price + 200 output tokens
    assert run.turns[0].model_call.cost_usd == pytest.approx((200 + 400) * 5e-6 + 200 * 25e-6)
    assert client.chat.completions.calls[0]["reasoning_effort"] == "low"


def test_cut_off_turn_is_retried_once_and_fenced_json_is_accepted(campaign):
    text = "```json\n" + make_draft().model_dump_json() + "\n```"
    client = fake_client(fake_response('{"position": "OPT-', "length"), fake_response(text))
    provider = OpenAICompatibleProvider(model="m", client=client, input_usd_per_mtok=5, output_usd_per_mtok=25)
    scenario, bundle = campaign
    run = Orchestrator(scenario, bundle, provider, RunConfig(max_rounds=1, max_usd=10)).run()
    assert run.state.status == RunStatus.CONCLUDED
    assert "retry" in client.chat.completions.calls[1]["messages"][1]["content"]
    assert run.turns[0].model_call.cost_usd == pytest.approx(0.02), "both attempts are charged"


def test_refusals_empty_and_malformed_turns_fail_cleanly(campaign):
    for response in (fake_response("", refusal="no"), fake_response(None), fake_response('{"position": "undecided"}'),
                     fake_response("x", "content_filter")):
        run = one_round(campaign, OpenAICompatibleProvider(model="m", client=fake_client(response)), max_usd=10)
        assert run.state.status == RunStatus.FAILED and "Provider error" in run.warnings[-1]


def test_spend_guard_blocks_before_any_call(campaign):
    client = fake_client(fake_response(make_draft().model_dump_json()))
    provider = OpenAICompatibleProvider(model="m", client=client, input_usd_per_mtok=5, output_usd_per_mtok=25)
    run = one_round(campaign, provider, max_usd=0.01)
    assert run.state.status == RunStatus.BUDGET_EXHAUSTED and client.chat.completions.calls == []


def test_factory_and_settings_choose_the_endpoint(monkeypatch):
    monkeypatch.setenv("CAPACITYLAB_PROVIDER", "openai")
    monkeypatch.setenv("CAPACITYLAB_MODEL", "llama3.1")
    monkeypatch.setenv("CAPACITYLAB_LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = Settings.from_env()
    assert settings.llm_provider == "openai" and settings.credentials_present("openai"), "local servers need no key"
    provider = make_provider("openai", settings)
    assert isinstance(provider, OpenAICompatibleProvider) and provider.model == "llama3.1"
    assert provider.base_url == "http://localhost:11434/v1"
    monkeypatch.setenv("CAPACITYLAB_LLM_BASE_URL", "https://api.example.com/v1")
    assert not Settings.from_env().credentials_present("openai")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert Settings.from_env().credentials_present("openai")
