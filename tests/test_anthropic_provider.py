"""The real provider path, exercised with a fake SDK client (no network, no credentials)."""

import json
from types import SimpleNamespace

import pytest

from capacitylab.scenarios.state import RunStatus
from capacitylab.simulation.orchestrator import Orchestrator
from capacitylab.simulation.providers.anthropic_provider import (
    FALLBACK_BETA,
    STRUCTURED_OUTPUTS_BETA,
    AnthropicProvider,
)
from capacitylab.simulation.providers.base import ProviderError
from capacitylab.simulation.run import RunConfig
from capacitylab.simulation.schema import Claim, TurnDraft
from tests.conftest import make_draft


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def fake_response(draft: TurnDraft | None, stop_reason: str = "end_turn", text: str | None = None):
    body = text if text is not None else (draft.model_dump_json() if draft is not None else "")
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="test") if stop_reason == "refusal" else None,
        content=[SimpleNamespace(type="text", text=body)],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0, cache_read_input_tokens=0),
        model="claude-opus-5",
        _request_id="req_test",
    )


def fake_client(response):
    return SimpleNamespace(messages=FakeMessages(response), beta=SimpleNamespace(messages=FakeMessages(response)))


class CountingMessages(FakeMessages):
    """Fake client that can also answer the token-counting endpoint the spend guard uses."""

    def __init__(self, response, tokens: int | None):
        super().__init__(response)
        self.tokens, self.count_calls = tokens, 0

    def count_tokens(self, **kwargs):
        self.count_calls += 1
        if self.tokens is None:
            raise RuntimeError("counting unavailable")
        return SimpleNamespace(input_tokens=self.tokens)


def counting_client(response, tokens: int | None):
    return SimpleNamespace(messages=FakeMessages(response), beta=SimpleNamespace(messages=CountingMessages(response, tokens)))


def estimates_from(campaign, provider, rounds=2):
    scenario, bundle = campaign
    seen: list[float] = []
    exact = provider.estimate_max_cost_usd
    provider.estimate_max_cost_usd = lambda ctx: (seen.append(exact(ctx)), seen[-1])[1]
    Orchestrator(scenario, bundle, provider, RunConfig(max_rounds=rounds, max_usd=10)).run()
    return seen


def one_round(campaign, provider, **config):
    scenario, bundle = campaign
    return Orchestrator(scenario, bundle, provider, RunConfig(max_rounds=1, **config)).run()


def test_request_shape_structured_output_and_cost(campaign):
    draft = make_draft(claims=[Claim(statement="Digest reviewed.", evidence_ids=["EV-DIG-001"], basis="observed")])
    client = fake_client(fake_response(draft))
    provider = AnthropicProvider(client=client, effort="medium", input_usd_per_mtok=5, output_usd_per_mtok=25)
    run = one_round(campaign, provider, max_usd=10)
    assert run.state.status == RunStatus.CONCLUDED and not run.mocked
    call = client.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5" and call["output_config"]["effort"] == "medium"
    fmt = call["output_config"]["format"]
    assert fmt["type"] == "json_schema" and "claims" in fmt["schema"]["properties"]
    assert call["betas"] == [FALLBACK_BETA, STRUCTURED_OUTPUTS_BETA] and call["fallbacks"] == "default"
    assert "Database engineer" in call["system"][0]["text"] and "Never invent" in call["system"][0]["text"]
    blocks = call["messages"][0]["content"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in blocks[1]
    header_line, *evidence_lines = blocks[0]["text"].split("\n")
    header, evidence = json.loads(header_line), [json.loads(line) for line in evidence_lines]
    per_round = json.loads(blocks[1]["text"])
    assert per_round["round"] == 1 and header["scenario"]["id"] == "campaign-overlap"
    assert any(e["id"] == "EV-DIG-001" for e in evidence)
    ids = [e["id"] for e in evidence]
    assert ids == sorted(ids, key=lambda i: (i.startswith("EV-TOOL-"), i)), \
        "check results sort last so later rounds only append lines to the cached block"
    first = run.turns[0].model_call
    assert first.cost_usd == pytest.approx(0.01) and first.request_id == "req_test" and first.mocked is False
    assert run.spend_usd == pytest.approx(0.01 * len(run.turns))


def test_tenant_persona_pack_excludes_other_tenants(campaign):
    client = fake_client(fake_response(make_draft()))
    one_round(campaign, AnthropicProvider(client=client), max_usd=10)
    tenant_call = client.beta.messages.calls[-1]
    assert "Tenant representative" in tenant_call["system"][0]["text"]
    pack = "".join(b["text"] for b in tenant_call["messages"][0]["content"])
    assert "EV-TEN-BIRCH-001" not in pack and "EV-PLAN-001" not in pack


def test_refusal_and_missing_output_raise_provider_errors(campaign):
    for response in (fake_response(make_draft(), "refusal"), fake_response(None), fake_response(make_draft(), "max_tokens")):
        run = one_round(campaign, AnthropicProvider(client=fake_client(response)), max_usd=10)
        assert run.state.status == RunStatus.FAILED and run.warnings


def test_cut_off_or_malformed_turn_fails_cleanly_and_is_charged(campaign):
    cut_off = '{"position":"OPT-RESCHEDULE","claims":[{"statement":"Lab lock waits","evidence_ids":["EV-LAB-CMP","EV-LAB-EVEN'
    # A reply that is cut off is retried once, so it is charged twice; a malformed reply is not retried.
    for response, attempts in ((fake_response(None, "max_tokens", text=cut_off), 2),
                               (fake_response(None, text=cut_off), 1),
                               (fake_response(None, text='{"position": "undecided"}'), 1)):
        run = one_round(campaign, AnthropicProvider(client=fake_client(response), input_usd_per_mtok=5,
                                                    output_usd_per_mtok=25), max_usd=10)
        assert run.state.status == RunStatus.FAILED and "Provider error" in run.warnings[-1]
        assert run.spend_usd == pytest.approx(0.01 * attempts), "every call that reached the model is charged"
        assert run.output_tokens == 200 * attempts


def test_fallback_can_be_disabled(campaign):
    client = fake_client(fake_response(make_draft()))
    one_round(campaign, AnthropicProvider(client=client, refusal_fallback=False), max_usd=10)
    assert client.messages.calls and not client.beta.messages.calls
    assert "fallbacks" not in client.messages.calls[0]


def test_spend_guard_applies_to_real_provider(campaign):
    client = fake_client(fake_response(make_draft()))
    run = one_round(campaign, AnthropicProvider(client=client), max_usd=0.01)
    assert run.state.status == RunStatus.BUDGET_EXHAUSTED
    assert client.beta.messages.calls == []


def test_provider_error_type():
    assert issubclass(ProviderError, RuntimeError)


def test_a_cut_off_turn_is_retried_once_and_both_attempts_are_charged(campaign):
    cut_off = fake_response(None, "max_tokens", text='{"position":"OPT-KEEP","claims":[{"statement":"long')
    good = fake_response(make_draft())

    class Sequence(FakeMessages):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            return cut_off if len(self.calls) == 1 else good

    client = SimpleNamespace(messages=FakeMessages(good), beta=SimpleNamespace(messages=Sequence(good)))
    run = one_round(campaign, AnthropicProvider(client=client, input_usd_per_mtok=5, output_usd_per_mtok=25), max_usd=10)
    calls = client.beta.messages.calls
    assert run.state.status == RunStatus.CONCLUDED, "the retry saves the run"
    assert "cut off at the output limit" in calls[1]["messages"][0]["content"][1]["text"]
    assert "cut off" not in calls[2]["messages"][0]["content"][1]["text"], "only the retry carries the notice"
    assert run.turns[0].model_call.cost_usd == pytest.approx(0.02), "both attempts are charged"


def test_spend_estimate_uses_the_api_count_and_allows_for_the_cached_prefix(campaign):
    client = counting_client(fake_response(make_draft()), tokens=10_000)
    provider = AnthropicProvider(client=client, input_usd_per_mtok=5, output_usd_per_mtok=25, max_tokens=4000)
    seen = estimates_from(campaign, provider)
    counter = client.beta.messages
    assert counter.count_calls >= 10, "every turn is counted before it is sent"
    first_round = 10_000 * 1.25 * 5 / 1e6 + 4000 * 25 / 1e6
    assert seen[0] == pytest.approx(first_round), "nothing is cached yet in round 1"
    assert seen[5] < seen[0], "round 2 reuses most of the role's first block, which is priced as a cache read"


def test_spend_estimate_falls_back_when_the_api_cannot_count(campaign):
    client = counting_client(fake_response(make_draft()), tokens=None)
    provider = AnthropicProvider(client=client, input_usd_per_mtok=5, output_usd_per_mtok=25, max_tokens=4000)
    seen = estimates_from(campaign, provider, rounds=1)
    assert client.beta.messages.count_calls == len(seen) and all(e > 4000 * 25 / 1e6 for e in seen)


def test_workspace_header_for_keys_without_a_workspace(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    assert AnthropicProvider().client.default_headers["anthropic-workspace-id"] == "wrkspc_test"
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID")
    assert "anthropic-workspace-id" not in AnthropicProvider().client.default_headers
