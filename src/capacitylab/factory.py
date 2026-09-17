# SPDX-License-Identifier: AGPL-3.0-or-later
"""Construct providers and sandboxes from settings."""

from __future__ import annotations

from collections.abc import Callable

from capacitylab.diagnostics.sandbox import MySQLSandbox, Sandbox, SQLiteSandbox
from capacitylab.settings import Settings
from capacitylab.simulation.providers.base import TurnProvider
from capacitylab.simulation.providers.mock import MockProvider


def make_provider(name: str, settings: Settings) -> TurnProvider:
    if name == "mock":
        return MockProvider()
    if name == "anthropic":
        from capacitylab.simulation.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            model=settings.model,
            effort=settings.effort,
            input_usd_per_mtok=settings.input_usd_per_mtok,
            output_usd_per_mtok=settings.output_usd_per_mtok,
            refusal_fallback=settings.refusal_fallback,
            max_tokens=settings.max_output_tokens,
        )
    if name == "openai":
        import os

        from capacitylab.simulation.providers.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            model=settings.model,
            base_url=settings.llm_base_url,
            api_key=os.environ.get(settings.llm_api_key_env),
            input_usd_per_mtok=settings.input_usd_per_mtok,
            output_usd_per_mtok=settings.output_usd_per_mtok,
            cached_input_multiplier=settings.cached_input_multiplier,
            max_tokens=settings.max_output_tokens,
            reasoning_effort=settings.reasoning_effort,
        )
    raise ValueError(f"unknown provider {name!r}; use 'mock', 'anthropic' or 'openai'")


def make_sandbox_factory(kind: str, settings: Settings) -> Callable[[], Sandbox]:
    if kind == "sqlite":
        return SQLiteSandbox
    if kind == "mysql":
        m = settings.mysql
        if m is None:
            raise ValueError("MySQL sandbox settings are missing")
        return lambda: MySQLSandbox(m.host, m.port, m.user, m.password, m.database)
    raise ValueError(f"unknown sandbox {kind!r}; use 'sqlite' or 'mysql'")
