# Changelog

Notable changes, newest first. Dates are the day the work landed on `main`.

## Unreleased

**Added**

- History store and capacity envelope: `capacitylab history collect|status|envelope`. Metrics from AWS, Google Cloud
  or Azure accumulate in a local SQLite file, idempotently, with every collection recorded so a quiet stretch can be
  told from one nobody collected. The envelope reports what each hour of each weekday reaches, weighted towards recent
  days, with level shifts detected and thin evidence flagged. It is descriptive statistics, not a forecast.
- Query rewrite as an option that competes on price, gated on an equivalence check: no benefit is credited until a
  rewrite has been measured, and none at all if it returns different rows than the original.
- Load attribution: each breaching slot broken down by statement, tenant and batch job, with the remedy that shape
  argues for. Available as the `load_attribution` tool and as a finding.
- Google Cloud and Azure imports (Cloud SQL and Cloud Monitoring, flexible servers and Azure Monitor), verified
  against the floci-gcp and floci-az emulators in CI.
- A reason the project exists, in the README: why reactive autoscaling does not answer a known event.

- Password sign-in for the web UI: a PBKDF2 hash in `.env`, a signed session cookie, rate-limited attempts, and
  `serve` refusing a non-local address while no password is set. `capacitylab hash-password` generates the lines.
- Model settings page: provider, model, endpoint, prices and spend caps, with presets for Claude, OpenAI, Ollama and
  vLLM. No API key is ever stored; the page chooses which environment variable holds it and reports presence only.
- An Ollama container in `docker-compose.yml` (`--profile ollama`), so a review can run free and offline.

**Changed**

- Licensed under AGPL-3.0-or-later (was Apache-2.0), with a commercial licence available separately.
- CI runs the cheap checks on every change and the container suites on request, which keeps it inside the free tier.

## 0.1.0, 2026-09-17

First working version.

- Five-role review of a database capacity decision, run by a language model, with a decision record that keeps
  disagreements, unanswered challenges and evidence nobody has.
- Deterministic capacity model: M/M/c queueing per 15-minute slot, options priced from the scenario's rate card,
  revenue at risk from named assumptions.
- Local database lab on MySQL 8.0 and PostgreSQL 17, with Percona Toolkit support, and a SQLite experiment sandbox for
  index and rewrite experiments.
- Importers for slow logs, digest exports, `EXPLAIN ANALYZE` output, CloudWatch exports, Percona Toolkit reports, and
  a read-only AWS import.
- Provider-agnostic LLM layer: the Anthropic API natively, or any OpenAI-compatible endpoint, with prompt caching,
  token counting and hard spend caps.
- Web UI, replay of any run, and an evaluation that compares the five-role review with a single reviewer and with
  simple rules.
