"""Real model provider for stakeholder turns (Anthropic Messages API, structured output).

The model receives the same context pack a mock stakeholder sees and must return a `TurnDraft`.
Numbers, citations, and tool permissions are validated by CapacityLab after every turn; the model
never executes tools itself.
"""

from __future__ import annotations

from capacitylab.simulation.providers.base import (
    PROMPT_VERSION,
    ProviderError,
    ProviderResult,
    TurnContext,
    context_blocks,
    render_system_prompt,
)
from capacitylab.simulation.schema import ModelCall, TurnDraft

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _common_prefix(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i
STRUCTURED_OUTPUTS_BETA = "structured-outputs-2025-12-15"  # what the SDK's beta parse() adds
SHORTER_TURN_NOTICE = (
    '\n{"retry":"Your previous answer was cut off at the output limit and was discarded. Send a shorter turn: '
    'at most 4 claims of one sentence each, at most 2 challenges, and no optimization proposal unless it is the '
    'point of your turn."}'
)


def TURN_SCHEMA() -> dict:  # noqa: N802 - built lazily so importing this module does not require the SDK
    from anthropic import transform_schema

    return transform_schema(TurnDraft.model_json_schema())


class AnthropicProvider:
    name = "anthropic"
    mocked = False

    def __init__(
        self,
        model: str = "claude-opus-5",
        effort: str = "auto",  # "auto" means per-role (see roles.EFFORT); any other value applies to all roles
        input_usd_per_mtok: float = 5.0,
        output_usd_per_mtok: float = 25.0,
        refusal_fallback: bool = True,
        max_tokens: int = 5000,
        client=None,
    ):
        self.model = model
        self.effort = effort
        self.input_usd_per_mtok = input_usd_per_mtok
        self.output_usd_per_mtok = output_usd_per_mtok
        self.refusal_fallback = refusal_fallback
        self.max_tokens = max_tokens
        self._client = client
        self._last_stable: dict[str, str] = {}  # role id -> the stable block sent for that role last round

    @property
    def client(self):
        if self._client is None:
            import os

            import anthropic

            # Keys that are not scoped to a workspace must name one on every request.
            workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
            headers = {"anthropic-workspace-id": workspace} if workspace else None
            self._client = anthropic.Anthropic(default_headers=headers)
        return self._client

    def _input_tokens(self, system: str, blocks: list[dict]) -> int:
        """Exact count from the API (free), or ~1.8 characters per token, which is what this dense JSON measures."""
        try:
            counted = self.client.beta.messages.count_tokens(
                model=self.model, system=[{"type": "text", "text": system}],
                messages=[{"role": "user", "content": blocks}])
            return int(counted.input_tokens)
        except Exception:  # counting unavailable (offline, fake client, unsupported model): fall back to characters
            return int((len(system) + sum(len(b["text"]) for b in blocks)) / 1.8)

    def estimate_max_cost_usd(self, ctx: TurnContext) -> float:
        """Worst case for the spend guard: the whole input plus an answer of the full output length.

        The first block is cached, so whatever this role already sent last round is priced as a cache read.
        """
        system = render_system_prompt(ctx)
        stable, per_round = context_blocks(ctx)
        blocks = [{"type": "text", "text": stable}, {"type": "text", "text": per_round}]
        total = self._input_tokens(system, blocks)
        previous = self._last_stable.get(ctx.role.id.value, "")
        reused_chars = _common_prefix(previous, stable)
        all_chars = max(1, len(system) + len(stable) + len(per_round))
        reused = total * reused_chars / all_chars
        rate_in = self.input_usd_per_mtok / 1e6
        return round(reused * 0.1 * rate_in + (total - reused) * 1.25 * rate_in
                     + self.max_tokens * self.output_usd_per_mtok / 1e6, 6)

    def _effort_for(self, role) -> str:
        """Per-role effort when configured as "auto"; an explicit setting applies to every role."""
        if self.effort != "auto":
            return self.effort
        from capacitylab.simulation.roles import EFFORT

        return EFFORT.get(role.id, "low")

    def _cost(self, usage) -> float:
        rate_in = self.input_usd_per_mtok / 1e6
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        return round(
            usage.input_tokens * rate_in
            + cache_write * rate_in * 1.25
            + cache_read * rate_in * 0.1
            + usage.output_tokens * self.output_usd_per_mtok / 1e6,
            6,
        )

    def generate_turn(self, ctx: TurnContext) -> ProviderResult:
        import anthropic
        import pydantic

        # Same structured-output request the SDK's parse() builds, but the reply is parsed here, after the stop reason
        # and usage are known, so a turn cut off at max_tokens fails cleanly and is still charged.
        stable, per_round = context_blocks(ctx)
        self._last_stable[ctx.role.id.value] = stable
        charged = dict(cost_usd=0.0, input_tokens=0, output_tokens=0)
        extra = ""
        for attempt in (1, 2):  # a turn cut off at the limit is retried once, asking for a shorter one
            params = dict(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{"type": "text", "text": render_system_prompt(ctx), "cache_control": {"type": "ephemeral"}}],
                # Two blocks: the first only grows by appending, so the provider can cache it across rounds.
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": per_round + extra},
                ]}],
                output_config={"effort": self._effort_for(ctx.role),
                               "format": {"type": "json_schema", "schema": TURN_SCHEMA()}},
            )
            try:
                if self.refusal_fallback:
                    response = self.client.beta.messages.create(
                        **params, betas=[FALLBACK_BETA, STRUCTURED_OUTPUTS_BETA], fallbacks="default")
                else:
                    response = self.client.messages.create(**params)
            except anthropic.NotFoundError as exc:
                raise ProviderError(f"model or endpoint not found: {exc.message}", **charged) from exc
            except anthropic.AuthenticationError as exc:
                raise ProviderError("authentication failed; set ANTHROPIC_API_KEY or log in with the ant CLI",
                                    **charged) from exc
            except anthropic.RateLimitError as exc:
                raise ProviderError("rate limited after SDK retries", **charged) from exc
            except anthropic.APIStatusError as exc:
                raise ProviderError(f"API error {exc.status_code}: {exc.message}", **charged) from exc
            except anthropic.APIConnectionError as exc:
                raise ProviderError("could not reach the API", **charged) from exc

            usage = response.usage
            charged = dict(cost_usd=round(charged["cost_usd"] + self._cost(usage), 6),
                           input_tokens=charged["input_tokens"] + usage.input_tokens,
                           output_tokens=charged["output_tokens"] + usage.output_tokens)
            if response.stop_reason == "max_tokens" and attempt == 1:
                extra = SHORTER_TURN_NOTICE
                continue
            break

        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise ProviderError(f"model declined the turn (category: {category})", **charged)
        if response.stop_reason == "max_tokens":
            raise ProviderError(f"turn cut off at the {self.max_tokens}-token output limit", **charged)
        text = "".join(getattr(block, "text", "") for block in response.content if getattr(block, "type", "") == "text")
        if not text.strip():
            raise ProviderError("response did not contain a structured turn", **charged)
        try:
            draft = TurnDraft.model_validate_json(text)
        except pydantic.ValidationError as exc:
            raise ProviderError(f"structured turn did not match the turn format: {exc.errors()[0]['msg']}",
                                **charged) from exc
        return ProviderResult(
            draft=draft,
            call=ModelCall(
                provider=self.name,
                model=response.model,
                prompt_version=PROMPT_VERSION,
                mocked=False,
                input_tokens=charged["input_tokens"],   # totals across attempts, so a retry is not billed for free
                output_tokens=charged["output_tokens"],
                cost_usd=charged["cost_usd"],
                request_id=getattr(response, "_request_id", None),
                stop_reason=response.stop_reason,
            ),
        )
