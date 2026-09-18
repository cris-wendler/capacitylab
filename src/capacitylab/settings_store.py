# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model choices made in the web UI, kept in a small file next to the runs.

Everything here can also be set with `CAPACITYLAB_*` environment variables. This file only holds what someone changed
on the settings page, so it stays readable and an untouched setting keeps following the environment:

    runs/llm-settings.json   {"llm_provider": "openai", "model": "llama3.1:8b", ...}

**No API key is ever stored here.** The page chooses which environment variable holds the key (`llm_api_key_env`) and
shows whether that variable is set, nothing more. Keys stay in `.env` or in the environment, which is also the only
place the rest of the code looks for them.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from capacitylab.settings import Settings

FILE = "llm-settings.json"

# What the settings page may change. Everything else (runs directory, database hosts, cloud endpoints) stays with the
# environment, because those decide what the tool is allowed to reach.
TEXT_FIELDS = ("llm_provider", "model", "effort", "llm_base_url", "llm_api_key_env", "reasoning_effort")
NUMBER_FIELDS = ("input_usd_per_mtok", "output_usd_per_mtok", "cached_input_multiplier", "max_usd_per_run",
                 "max_usd_total", "max_output_tokens", "max_rounds")
EFFORTS = ("auto", "low", "medium", "high")

PRESETS = {
    "anthropic-opus": {
        "label": "Anthropic, Claude Opus 5",
        "note": "The strongest reasoning, and the most expensive. Needs ANTHROPIC_API_KEY.",
        "fields": {"llm_provider": "anthropic", "model": "claude-opus-5", "llm_base_url": "", "effort": "auto",
                   "input_usd_per_mtok": 5.0, "output_usd_per_mtok": 25.0, "cached_input_multiplier": 0.1},
    },
    "anthropic-sonnet": {
        "label": "Anthropic, Claude Sonnet 5",
        "note": "Cheaper per token and quick enough for a full review. Needs ANTHROPIC_API_KEY.",
        "fields": {"llm_provider": "anthropic", "model": "claude-sonnet-5", "llm_base_url": "", "effort": "auto",
                   "input_usd_per_mtok": 3.0, "output_usd_per_mtok": 15.0, "cached_input_multiplier": 0.1},
    },
    "openai": {
        "label": "OpenAI",
        "note": "Any model the OpenAI API serves. Set the price fields to match the model you pick.",
        "fields": {"llm_provider": "openai", "model": "gpt-5", "llm_base_url": "", "llm_api_key_env": "OPENAI_API_KEY",
                   "input_usd_per_mtok": 1.25, "output_usd_per_mtok": 10.0, "cached_input_multiplier": 0.1},
    },
    "ollama": {
        "label": "Ollama, on this machine",
        "note": "Free and offline: no key, no spend, nothing leaves the machine. Small local models hold a five-role argument together much less well, so expect thinner turns.",
        "fields": {"llm_provider": "openai", "model": "llama3.1:8b", "llm_base_url": "http://127.0.0.1:11434/v1",
                   "llm_api_key_env": "OLLAMA_API_KEY", "input_usd_per_mtok": 0.0, "output_usd_per_mtok": 0.0,
                   "cached_input_multiplier": 1.0, "reasoning_effort": ""},
    },
    "vllm": {
        "label": "vLLM or LM Studio, on this machine",
        "note": "Any OpenAI-compatible server you run yourself. Adjust the port to match it.",
        "fields": {"llm_provider": "openai", "model": "local-model", "llm_base_url": "http://127.0.0.1:8000/v1",
                   "llm_api_key_env": "OPENAI_API_KEY", "input_usd_per_mtok": 0.0, "output_usd_per_mtok": 0.0,
                   "cached_input_multiplier": 1.0},
    },
}


def path_for(runs_dir: str | Path) -> Path:
    return Path(runs_dir) / FILE


def load(runs_dir: str | Path) -> dict:
    p = path_for(runs_dir)
    if not p.is_file():
        return {}
    try:
        stored = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in stored.items() if k in TEXT_FIELDS + NUMBER_FIELDS}


def save(runs_dir: str | Path, values: dict) -> Path:
    p = path_for(runs_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n")
    return p


def apply(settings: Settings, stored: dict) -> Settings:
    """Settings as the environment gave them, with whatever the settings page changed laid over the top."""
    changes = {k: v for k, v in stored.items() if k in TEXT_FIELDS + NUMBER_FIELDS}
    for key in ("llm_base_url", "reasoning_effort"):
        if key in changes and not changes[key]:
            changes[key] = None  # an empty box means "not set", not an empty string
    return replace(settings, **changes) if changes else settings


def clean(form: dict) -> tuple[dict, list[str]]:
    """Validate what the form sent. Returns (values to store, problems to show)."""
    from capacitylab.simulation.providers.base import LLM_PROVIDERS

    values: dict = {}
    problems: list[str] = []

    provider = str(form.get("llm_provider", "")).strip()
    if provider not in LLM_PROVIDERS:
        problems.append(f"Provider must be one of {', '.join(LLM_PROVIDERS)}.")
    else:
        values["llm_provider"] = provider

    model = str(form.get("model", "")).strip()
    if not model:
        problems.append("Model cannot be empty.")
    else:
        values["model"] = model

    effort = str(form.get("effort", "auto")).strip() or "auto"
    if effort not in EFFORTS:
        problems.append(f"Effort must be one of {', '.join(EFFORTS)}.")
    else:
        values["effort"] = effort

    base_url = str(form.get("llm_base_url", "")).strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        problems.append("The endpoint must start with http:// or https://.")
    else:
        values["llm_base_url"] = base_url

    key_env = str(form.get("llm_api_key_env", "")).strip() or "OPENAI_API_KEY"
    if not key_env.replace("_", "").isalnum():
        problems.append("The key variable name may only hold letters, digits and underscores.")
    else:
        values["llm_api_key_env"] = key_env

    reasoning = str(form.get("reasoning_effort", "")).strip()
    values["reasoning_effort"] = reasoning

    for field in NUMBER_FIELDS:
        raw = str(form.get(field, "")).strip()
        if raw == "":
            continue
        try:
            number = float(raw)
        except ValueError:
            problems.append(f"{field.replace('_', ' ')} must be a number.")
            continue
        if number < 0:
            problems.append(f"{field.replace('_', ' ')} cannot be negative.")
            continue
        values[field] = int(number) if field in ("max_output_tokens", "max_rounds") else number

    if values.get("max_output_tokens", 5000) < 1000:
        problems.append("Max output tokens below 1000 truncates a turn; use at least 1000.")
    if not 1 <= values.get("max_rounds", 3) <= 6:
        problems.append("Rounds must be between 1 and 6.")
    return values, problems


def key_state(settings: Settings) -> dict:
    """Whether the key this configuration needs is present. The value itself is never read out."""
    if settings.llm_provider == "anthropic":
        name = "ANTHROPIC_API_KEY"
        return {"name": name, "present": Settings.anthropic_credentials_present(), "needed": True}
    local = (settings.llm_base_url or "").startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
    return {"name": settings.llm_api_key_env, "present": bool(os.environ.get(settings.llm_api_key_env)),
            "needed": not local}


def local_models(base_url: str | None, timeout_s: float = 1.5) -> list[str]:
    """Models a local OpenAI-compatible server is serving, for the settings page. Local addresses only."""
    if not (base_url or "").startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        return []
    from capacitylab.cloud_common import JsonClient

    try:
        body = JsonClient(timeout_s=timeout_s).get(f"{base_url.rstrip('/')}/models")
    except Exception:  # noqa: BLE001 - not reachable is an answer, not an error
        return []
    return sorted(str(m.get("id", "")) for m in body.get("data", []) if m.get("id"))
