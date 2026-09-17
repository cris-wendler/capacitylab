# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime configuration read from the environment (and an optional local `.env`).

Secrets are only ever checked for presence; they are never logged or rendered.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE lines into os.environ without overriding variables that are already set."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MySQLSettings:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class PostgresSettings:
    host: str = "127.0.0.1"
    port: int = 5433
    user: str = "postgres"
    password: str = "change-me-local-only"
    database: str = "capacitylab_sandbox"


@dataclass(frozen=True)
class Settings:
    provider: str = "mock"  # mock | anthropic | openai (any OpenAI-compatible endpoint)
    llm_provider: str = "anthropic"  # which LLM provider the web UI offers next to the scripted agents
    model: str = "claude-opus-5"
    llm_base_url: str | None = None  # openai provider: endpoint (OpenAI, Gemini, Mistral, Groq, Ollama, vLLM, LiteLLM...)
    llm_api_key_env: str = "OPENAI_API_KEY"  # openai provider: the environment variable holding the key
    cached_input_multiplier: float = 1.0  # openai provider: price of cached input tokens relative to input tokens
    reasoning_effort: str | None = None  # openai provider: sent only when set
    effort: str = "auto"  # per-role effort (see simulation.roles.EFFORT); low/medium/high applies to every role
    refusal_fallback: bool = True
    input_usd_per_mtok: float = 5.0
    output_usd_per_mtok: float = 25.0
    max_usd_per_run: float = 3.0
    max_usd_total: float = 3.0
    max_output_tokens: int = 5000
    max_rounds: int = 3
    max_tool_calls: int = 40
    sandbox: str = "sqlite"
    mysql: MySQLSettings | None = None
    postgres: PostgresSettings = PostgresSettings()
    aws_region: str = "us-east-1"
    aws_endpoint: str = "http://127.0.0.1:4566"  # local emulator (Floci)
    aws_live: bool = False  # the web UI reads a real AWS account only when this is set explicitly
    gcp_project: str = "capacitylab-demo"
    gcp_endpoint: str = "http://127.0.0.1:4588"  # local emulator (floci-gcp)
    gcp_live: bool = False
    azure_subscription: str = "demo-subscription"
    azure_resource_group: str = "capacitylab-demo"
    azure_engine: str = "mysql"
    azure_endpoint: str = "http://127.0.0.1:4577"  # local emulator (floci-az)
    azure_live: bool = False
    # AGPL section 13: when you host CapacityLab for other people, point this at your own source so the footer can
    # link to it.
    source_url: str | None = None
    runs_dir: Path = Path("runs")

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        mysql = MySQLSettings(
            host=env.get("CAPACITYLAB_MYSQL_HOST", "127.0.0.1"),
            port=int(env.get("CAPACITYLAB_MYSQL_PORT", "3307")),
            user=env.get("CAPACITYLAB_MYSQL_USER", "root"),
            # Matches the docker-compose default; a local-only placeholder, never a real credential.
            password=env.get("CAPACITYLAB_MYSQL_PASSWORD", "change-me-local-only"),
            database=env.get("CAPACITYLAB_MYSQL_DATABASE", "capacitylab_sandbox"),
        )
        return cls(
            provider=env.get("CAPACITYLAB_PROVIDER", "mock"),
            llm_provider=env.get("CAPACITYLAB_LLM_PROVIDER",
                                 env.get("CAPACITYLAB_PROVIDER") if env.get("CAPACITYLAB_PROVIDER") not in (None, "", "mock")
                                 else "anthropic"),
            llm_base_url=env.get("CAPACITYLAB_LLM_BASE_URL") or None,
            llm_api_key_env=env.get("CAPACITYLAB_LLM_API_KEY_ENV", "OPENAI_API_KEY"),
            cached_input_multiplier=float(env.get("CAPACITYLAB_CACHED_INPUT_MULTIPLIER", "1.0")),
            reasoning_effort=env.get("CAPACITYLAB_REASONING_EFFORT") or None,
            model=env.get("CAPACITYLAB_MODEL", "claude-opus-5"),
            effort=env.get("CAPACITYLAB_EFFORT", "auto"),
            refusal_fallback=_bool(env.get("CAPACITYLAB_REFUSAL_FALLBACK"), True),
            input_usd_per_mtok=float(env.get("CAPACITYLAB_INPUT_USD_PER_MTOK", "5.0")),
            output_usd_per_mtok=float(env.get("CAPACITYLAB_OUTPUT_USD_PER_MTOK", "25.0")),
            max_usd_per_run=float(env.get("CAPACITYLAB_MAX_USD_PER_RUN", "3.0")),
            max_usd_total=float(env.get("CAPACITYLAB_MAX_USD_TOTAL", "3.0")),
            max_output_tokens=int(env.get("CAPACITYLAB_MAX_OUTPUT_TOKENS", "5000")),
            max_rounds=int(env.get("CAPACITYLAB_MAX_ROUNDS", "3")),
            max_tool_calls=int(env.get("CAPACITYLAB_MAX_TOOL_CALLS", "40")),
            sandbox=env.get("CAPACITYLAB_SANDBOX", "sqlite"),
            mysql=mysql,
            postgres=PostgresSettings(
                host=env.get("CAPACITYLAB_POSTGRES_HOST", "127.0.0.1"),
                port=int(env.get("CAPACITYLAB_POSTGRES_PORT", "5433")),
                user=env.get("CAPACITYLAB_POSTGRES_USER", "postgres"),
                # Matches the docker-compose default; a local-only placeholder, never a real credential.
                password=env.get("CAPACITYLAB_POSTGRES_PASSWORD", "change-me-local-only"),
                database=env.get("CAPACITYLAB_POSTGRES_DATABASE", "capacitylab_sandbox"),
            ),
            aws_region=env.get("CAPACITYLAB_AWS_REGION", "us-east-1"),
            aws_endpoint=env.get("CAPACITYLAB_AWS_ENDPOINT", "http://127.0.0.1:4566"),
            aws_live=_bool(env.get("CAPACITYLAB_AWS_LIVE"), False),
            gcp_project=env.get("CAPACITYLAB_GCP_PROJECT", "capacitylab-demo"),
            gcp_endpoint=env.get("CAPACITYLAB_GCP_ENDPOINT", "http://127.0.0.1:4588"),
            gcp_live=_bool(env.get("CAPACITYLAB_GCP_LIVE"), False),
            azure_subscription=env.get("CAPACITYLAB_AZURE_SUBSCRIPTION", "demo-subscription"),
            azure_resource_group=env.get("CAPACITYLAB_AZURE_RESOURCE_GROUP", "capacitylab-demo"),
            azure_engine=env.get("CAPACITYLAB_AZURE_ENGINE", "mysql"),
            azure_endpoint=env.get("CAPACITYLAB_AZURE_ENDPOINT", "http://127.0.0.1:4577"),
            azure_live=_bool(env.get("CAPACITYLAB_AZURE_LIVE"), False),
            runs_dir=Path(env.get("CAPACITYLAB_RUNS_DIR", "runs")),
            source_url=env.get("CAPACITYLAB_SOURCE_URL") or None,
        )

    @staticmethod
    def anthropic_credentials_present() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def credentials_present(self, provider: str) -> bool:
        if provider == "anthropic":
            return self.anthropic_credentials_present()
        if provider == "openai":
            local = (self.llm_base_url or "").startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
            return local or bool(os.environ.get(self.llm_api_key_env))
        return True
