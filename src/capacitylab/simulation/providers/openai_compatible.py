# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stakeholder turns from any OpenAI-compatible chat completions endpoint.

One provider covers the hosted APIs that speak this protocol (OpenAI, Google Gemini's OpenAI endpoint, Mistral, Groq,
DeepSeek), local servers (Ollama, vLLM, LM Studio), and gateways such as a LiteLLM proxy in front of Bedrock, Vertex
or Azure. Set the endpoint with CAPACITYLAB_LLM_BASE_URL and the model with CAPACITYLAB_MODEL.

The model gets the same system prompt and context pack as every other provider and must return the same turn format
(requested as a JSON schema). CapacityLab validates the turn itself, so a model that ignores the schema fails the turn
instead of slipping through. Spend is priced from the configured per-token rates, since prices differ by vendor.
"""

from __future__ import annotations

from capacitylab.simulation.providers.base import (
    PROMPT_VERSION,
    SHORTER_TURN_NOTICE,
    ProviderError,
    ProviderResult,
    TurnContext,
    context_blocks,
    render_system_prompt,
)
from capacitylab.simulation.schema import ModelCall, TurnDraft

CHARS_PER_TOKEN = 1.8  # measured on this context pack; a conservative estimate when no token-count endpoint exists
LOCAL_PREFIXES = ("http://127.0.0.1", "http://localhost", "http://[::1]")


class OpenAICompatibleProvider:
    name = "openai"
    mocked = False

    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None,
                 input_usd_per_mtok: float = 0.0, output_usd_per_mtok: float = 0.0,
                 cached_input_multiplier: float = 1.0, max_tokens: int = 5000, reasoning_effort: str | None = None,
                 client=None):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.input_usd_per_mtok = input_usd_per_mtok
        self.output_usd_per_mtok = output_usd_per_mtok
        self.cached_input_multiplier = cached_input_multiplier
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort  # only sent when set: many compatible servers reject it
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import openai

            key = self.api_key
            if not key and (self.base_url or "").startswith(LOCAL_PREFIXES):
                key = "not-needed"  # local servers accept any key
            self._client = openai.OpenAI(base_url=self.base_url or None, api_key=key or None)
        return self._client

    def _messages(self, ctx: TurnContext, extra: str = "") -> list[dict]:
        stable, per_round = context_blocks(ctx)
        # The stable block goes first and only grows by appending, so endpoints with automatic prefix caching reuse it.
        return [{"role": "system", "content": render_system_prompt(ctx)},
                {"role": "user", "content": stable + "\n" + per_round + extra}]

    def estimate_max_cost_usd(self, ctx: TurnContext) -> float:
        chars = sum(len(m["content"]) for m in self._messages(ctx))
        return round(chars / CHARS_PER_TOKEN * self.input_usd_per_mtok / 1e6
                     + self.max_tokens * self.output_usd_per_mtok / 1e6, 6)

    def _cost(self, usage) -> tuple[float, int, int]:
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        details = getattr(usage, "prompt_tokens_details", None)
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        rate_in = self.input_usd_per_mtok / 1e6
        cost = (prompt - cached) * rate_in + cached * rate_in * self.cached_input_multiplier
        cost += completion * self.output_usd_per_mtok / 1e6
        return round(cost, 6), prompt, completion

    def generate_turn(self, ctx: TurnContext) -> ProviderResult:
        import openai

        schema = {"name": "stakeholder_turn", "schema": TurnDraft.model_json_schema(), "strict": False}
        charged = dict(cost_usd=0.0, input_tokens=0, output_tokens=0)
        extra = ""
        for attempt in (1, 2):  # a turn cut off at the limit is retried once, asking for a shorter one
            params = dict(model=self.model, messages=self._messages(ctx, extra), max_tokens=self.max_tokens,
                          response_format={"type": "json_schema", "json_schema": schema})
            if self.reasoning_effort:
                params["reasoning_effort"] = self.reasoning_effort
            try:
                response = self.client.chat.completions.create(**params)
            except openai.AuthenticationError as exc:
                raise ProviderError("authentication failed; check the API key for this endpoint", **charged) from exc
            except openai.NotFoundError as exc:
                raise ProviderError(f"model or endpoint not found: {exc.message}", **charged) from exc
            except openai.RateLimitError as exc:
                raise ProviderError("rate limited after SDK retries", **charged) from exc
            except openai.BadRequestError as exc:
                raise ProviderError(f"request rejected (the endpoint may not support JSON schema output): {exc.message}",
                                    **charged) from exc
            except openai.APIStatusError as exc:
                raise ProviderError(f"API error {exc.status_code}: {exc.message}", **charged) from exc
            except openai.APIConnectionError as exc:
                raise ProviderError("could not reach the endpoint", **charged) from exc

            cost, prompt, completion = self._cost(response.usage) if response.usage else (0.0, 0, 0)
            charged = dict(cost_usd=round(charged["cost_usd"] + cost, 6), input_tokens=charged["input_tokens"] + prompt,
                           output_tokens=charged["output_tokens"] + completion)
            choice = response.choices[0]
            if choice.finish_reason == "length" and attempt == 1:
                extra = SHORTER_TURN_NOTICE
                continue
            break

        if choice.finish_reason == "content_filter" or getattr(choice.message, "refusal", None):
            raise ProviderError("model declined the turn", **charged)
        if choice.finish_reason == "length":
            raise ProviderError(f"turn cut off at the {self.max_tokens}-token output limit", **charged)
        text = choice.message.content or ""
        if not text.strip():
            raise ProviderError("response did not contain a structured turn", **charged)
        try:
            draft = TurnDraft.model_validate_json(_strip_fences(text))
        except ValueError as exc:
            raise ProviderError("structured turn did not match the turn format", **charged) from exc
        return ProviderResult(
            draft=draft,
            call=ModelCall(provider=self.name, model=getattr(response, "model", None) or self.model,
                           prompt_version=PROMPT_VERSION, mocked=False, input_tokens=charged["input_tokens"],
                           output_tokens=charged["output_tokens"], cost_usd=charged["cost_usd"],
                           request_id=getattr(response, "id", None), stop_reason=choice.finish_reason),
        )


def _strip_fences(text: str) -> str:
    """Some compatible servers wrap JSON in a Markdown code fence even when asked for a schema."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped
