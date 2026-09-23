# How a review works

## Tech stack

| Layer | Built with | What it does here |
|---|---|---|
| ![](https://img.shields.io/badge/LLM%20agents-7a5ad6?style=for-the-badge) | Any LLM: the Anthropic API natively, or any OpenAI-compatible endpoint (OpenAI, Gemini, Mistral, Groq, Ollama, vLLM, LiteLLM) | One call per turn, returning JSON that matches a Pydantic schema. Prompt caching, token counting before each call, effort per role, one retry when a turn is cut off. |
| ![](https://img.shields.io/badge/orchestration-4a3aa7?style=for-the-badge) | Plain Python, no agent framework | Rounds, what each role may see, checks between rounds, stopping when positions settle, a log that replays. |
| ![](https://img.shields.io/badge/guardrails-b42318?style=for-the-badge) | Turn validation in code | Citations must exist and be visible to that role. Every number must appear in the evidence it cites. Spend must fit the cap. |
| ![](https://img.shields.io/badge/capacity-2d6cdf?style=for-the-badge) | M/M/c queueing model | Utilisation per slot, SLO breach slots, every option scored against the others. |
| ![](https://img.shields.io/badge/FinOps-1b9b6d?style=for-the-badge) | Cost model, tenant entitlements, cloud pricing APIs | Option costs from a rate card, real prices and month-to-date spend, each tenant's share against what their plan guarantees. |
| ![](https://img.shields.io/badge/history-0f766e?style=for-the-badge) | SQLite, append-only | What a cluster actually does, accumulated: the envelope per hour and weekday, drift detection, and whether the history can carry a decision at all. |
| ![](https://img.shields.io/badge/clouds-c47f00?style=for-the-badge) | AWS (boto3), Google Cloud and Azure (REST); Floci emulators | Topology, metrics, prices and cost, read-only. A local emulator by default, a real account only with `--live`. |
| ![](https://img.shields.io/badge/database-31577d?style=for-the-badge) | MySQL 8.0, PostgreSQL 17, SQLite, Percona Toolkit | Real load tests, `performance_schema` and `pg_stat_statements`, `EXPLAIN ANALYZE`, index and rewrite experiments. |
| ![](https://img.shields.io/badge/app-0f6f78?style=for-the-badge) | FastAPI, Jinja, SVG charts, argparse | Web UI with password sign-in and a model settings page; every feature also on the command line. |
| ![](https://img.shields.io/badge/quality-4b5563?style=for-the-badge) | pytest, ruff, GitHub Actions | The test suite on every push, plus the container and cloud-emulator suites; replay of a full run; a scan for leftover identifiers. |

## The five agents

The short version is in the [README](../README.md#the-five-agents). In full, each agent gets a role, the evidence
that role would normally see, and the checks it may ask for. None of them sees everything.

> [!NOTE]
> Concurrency shows up here as evidence, not as a constraint. Connections are imported, the bottleneck check flags a
> node running near its connection limit, and the lab records threads running, lock waits and deadlocks. What the
> capacity model does **not** yet do is treat `max_connections`, pool size per application pod, or a saturating pool
> as a limit on the options. See the limitations in the [README](../README.md#limitations).

| Agent | Looks at | Can ask for | Reasoning effort |
|---|---|---|:---:|
| 🟦 **Database engineer** | metrics (CPU, **connections**, memory, IOPS), statement digests, plans, schema, table stats, forecast, batch schedule, experiments | top queries, tenant skew, plan review, index experiment, rewrite check, row-estimate check, table growth, bottleneck check, capacity forecast, load attribution, lab load test, redundant-index check | low |
| 🟩 **Application owner** | calendars, releases, batch schedule and history, SLOs, digests, tenant profiles | top queries, batch reschedule check, capacity forecast, cost, load attribution, tenant entitlements | low |
| 🟨 **Reliability engineer** · SRE | metrics (**connection saturation** included), forecast, SLOs, incident and failover history, calendars, batch, experiments | tenant skew, bottleneck check, batch reschedule check, capacity forecast, cost, load attribution, tenant entitlements, lab load test | medium |
| 🟧 **FinOps analyst** | metric summary, forecast, rate card, budget, table stats, experiments | tenant skew, table growth, capacity forecast, cost, load attribution, tenant entitlements | low |
| 🟪 **Tenant representative** | its own profile, calendar and SLOs, and model results with other tenants removed | tenant skew (own share only), capacity forecast, tenant entitlements (its own plan only) | low |

**Why the effort differs.** Reasoning effort is how much the model thinks before it writes a turn: more effort means
longer internal reasoning, more tokens and a higher cost. Four agents mostly read evidence and quote it (digests,
plans, calendars, the rate card, the budget), and extra thinking there tends to produce numbers the model worked out
itself, which the checks then flag. The reliability engineer is the one agent that has to weigh something nobody
measured, such as an unknown failover time against headroom and cost, so it gets more room. `CAPACITYLAB_EFFORT=high`
(or `low`, `medium`) applies one level to every agent instead. The same agents can also run from scripted rules, which
is free, offline and repeatable.

## What you get

![The CapacityLab overview: decisions on the table with revenue at risk, cheapest option that keeps SLOs and instance cost, recent reviews and the agents](media/home.png)

| | |
|---|---|
| 📝 **A decision record** | What each agent recommends and why, open disagreements, unanswered challenges, evidence nobody has, and checks that were requested but never ran. |
| 🩺 **Findings before any review** | Whether a node runs out of CPU or is oversized, whether failover has ever been measured, which tables grow in a way that hurts, and which statements one tenant dominates. `capacitylab findings <scenario>` or the scenario page. |
| 🔎 **Why a slot breaches** | Each breaching slot broken down by statement, by tenant and by batch job, so the remedy follows the cause: a job that can move, a statement worth an index or a rewrite, a tenant taking more than they pay for, or load spread evenly, which is the only case where buying capacity is the honest answer. |
| 🧪 **Measurements from a real engine** | A local MySQL 8.0 or PostgreSQL 17 lab runs the scenario's statement mix over many connections and records latency percentiles, lock waits, deadlocks, statement digests and plans, optionally with Percona Toolkit. |
| 💸 **Costs next to the risk** | Every option priced from the scenario's rate card, and each tenant's share of the cluster compared with what it pays for. |
| 📈 **What your cluster actually does** | `history collect` appends each window of metrics to a local store, and `history envelope` reports what every hour of every weekday reaches (typically, at the high end and at worst), weighted towards recent days, with level shifts detected and thin evidence flagged. |
| 📥 **Your own data** | Read database topology, metrics, prices and cost straight from AWS, Google Cloud or Azure, or import slow logs, `performance_schema` digest exports, `EXPLAIN ANALYZE` output, CloudWatch exports and Percona Toolkit reports. |
| 🔁 **Replay** | Every item is labelled observed, forecast, assumption, modeled or measured. Every run is a log you can replay to re-check each result. |

## How a review runs

The diagram of the whole loop is in the [README](../README.md#the-five-agents). What follows is what happens
inside one round.

Each round, every agent receives only the evidence its role gives it, plus the other agents' latest positions and
challenges. It returns a structured turn: a position, claims with cited evidence ids, assumptions, challenges, missing
evidence and requests for checks. The requested checks run between rounds, and their results arrive as new evidence in
the next one. A run stops at the round limit, or earlier when positions stop changing and nothing is left unanswered.

```mermaid
sequenceDiagram
  autonumber
  participant O as Orchestrator
  participant A as Agent (LLM)
  participant V as Turn checks
  participant T as Checks and experiments
  rect rgb(234, 242, 252)
    O->>A: role prompt + evidence this role may see + other positions
    A-->>O: structured turn (JSON schema)
  end
  rect rgb(255, 244, 220)
    O->>V: citations, numbers, permissions
    V-->>O: turn kept, problems flagged in the record
  end
  rect rgb(227, 245, 238)
    O->>T: requested checks (forecast, cost, index experiment, lab load test)
    T-->>O: new evidence, labelled modeled or measured
  end
  Note over O,A: next round, until positions settle or the round limit
```

**Evidence labels.** Each item carries one of five labels, with the same colors as the web UI:

| Label | Meaning |
|---|---|
| ![Observed](https://img.shields.io/badge/-Observed-6b6a65?style=flat-square) | Collected from a system, or scenario data standing in for it |
| ![Forecast](https://img.shields.io/badge/-Forecast-1c5cab?style=flat-square) | Projected from stated assumptions |
| ![Assumption](https://img.shields.io/badge/-Assumption-9a6700?style=flat-square) | Stated, not measured (for example the tenant's 5× expectation) |
| ![Modeled](https://img.shields.io/badge/-Modeled-4a3aa7?style=flat-square) | Output of the capacity or cost model |
| ![Measured](https://img.shields.io/badge/-Measured-127a55?style=flat-square) | Measured by an experiment CapacityLab ran, in the local lab or experiment database |

Each item also records where it came from: scenario data, the local lab, or an imported file.

> [!CAUTION]
> The experiment database and both labs only connect to `localhost` and only to databases named
> `capacitylab_sandbox…`. Percona Toolkit only attaches to containers named `capacitylab-*`. Nothing in CapacityLab
> connects to a cloud account.
