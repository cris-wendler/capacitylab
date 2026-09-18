# Evaluation method

`capacitylab evaluate <scenario>` compares three approaches to the same decision.

| Approach | What runs | Evidence and tools |
|---|---|---|
| **Five-role review** | The five roles, the full orchestrator, the scenario's round and tool budgets | Role-scoped |
| **Single reviewer** | One role (`single reviewer`) that sees every evidence kind and every gap, can use every tool, same budgets | Everything |
| **Rule-based** | Capacity-only rules: scale up when the status quo peaks over the threshold; downsize to the cheapest class under the threshold when peak is below half of it | Capacity model only |

All three start from an **identical evidence bundle** (the test suite asserts the bundle digest is unchanged).

## Reference outcomes

Synthetic scenarios carry a hidden `ground_truth` block that no stakeholder sees:

- **assumption overrides** (for example, the true campaign multiplier is 4.2×, between the tenant's 5× claim and the
  previous sale's 3.1×);
- **root-cause fingerprints** (for example, `QF-AUDIENCE`);
- **expected gap flags** (the missing evidence a careful reviewer should raise).

Each recommendation is evaluated inside the **same capacity model** with the hidden assumptions and index effects
measured by a reference sandbox experiment. The "reference best" is the cheapest option with zero breach slots, zero
saturated slots, zero slots over threshold, and **no unmeasured risks**. That last condition is a stated risk
preference; changing it changes the reference.

## Metrics

| Metric | Definition |
|---|---|
| Breach-slot regret | SLO breach slots of the recommendation minus those of the reference best |
| Cost regret | 12-month cost (one-off + 12 × monthly) of the recommendation minus that of the reference best |
| Root cause found | An optimization proposal targets a root-cause fingerprint |
| Gap recall | Share of expected gap flags present in the final missing-evidence list |
| Citation errors | Invalid or inaccessible citations across all turns |
| Ungrounded numbers | Numbers in claims not present in the cited evidence |
| Open disagreements | Position splits plus unanswered challenges in the decision record |

The five-role review run is scored on its **plurality** final position. That is only for scoring; CapacityLab's
output is the set of positions, not a vote. A tie is reported and broken alphabetically.

## Current results (mock provider, SQLite sandbox)

| Scenario | Approach | Recommendation | Breach regret | Cost regret | Root cause | Gap recall | Citation errors | Ungrounded | Disagreements |
|---|---|---|---|---|---|---|---|---|---|
| campaign-overlap | five-role review | OPT-INDEX-RESCHEDULE | 0 | 0.00 | yes | 1.0 | 0 | 0 | 7 |
| campaign-overlap | single reviewer | OPT-INDEX-RESCHEDULE | 0 | 0.00 | yes | 1.0 | 0 | 0 | 0 |
| campaign-overlap | rule-based | OPT-SCALE-TEMP | 0 | 128.96 | no | 0.0 | 0 | 0 | 0 |
| downsize-reader | five-role review | OPT-KEEP | 0 | 0.00 | n/a | 1.0 | 0 | 0 | 6 |
| downsize-reader | single reviewer | OPT-KEEP | 0 | 0.00 | n/a | 1.0 | 0 | 0 | 0 |
| downsize-reader | rule-based | OPT-DOWNSIZE-16XL | 0 | −81,292.80 | n/a | 0.0 | 0 | 0 | 0 |

### What these results do and do not show

- The mock stakeholders and the mock single reviewer are **hand-written policies written by the same author**. Their
  agreement on the recommendation tests the simulation code, the checks, and the scoring. It is **not** evidence about how
  language models compare.
- Zero citation errors and zero ungrounded numbers are expected for mock policies, which were built to cite correctly.
  For a real model these two metrics are the interesting ones.
- The rule-based baseline's negative cost regret in `downsize-reader` means it saves money by accepting risks
  (working set above the modeled buffer pool, a smaller failover target) that the reference excludes.

## Running a live evaluation

```bash
export ANTHROPIC_API_KEY=...
capacitylab evaluate campaign-overlap --provider anthropic --out runs/eval-live.json
```

This runs two full simulations (five-role review and single reviewer), each bounded by the scenario's `max_usd` and
`CAPACITYLAB_MAX_USD_PER_RUN`. Run it several times before drawing conclusions: model outputs vary between runs, and one
run per approach is an anecdote, not a result.

## What one live run showed

`campaign-overlap`, Claude Sonnet 5, 2026-09-18, one run per approach. The scored result is committed at
[docs/evaluations/campaign-overlap-sonnet-2026-09-18.json](evaluations/campaign-overlap-sonnet-2026-09-18.json),
so every figure below can be checked against it rather than taken on trust:

| Approach | Recommendation | Breach regret | Cost regret | Root cause | Gap recall | Unsupported numbers | Open disagreements | Tool calls | Spend |
|---|---|---:|---:|:---:|---:|---:|---:|---:|---:|
| five-role review | `OPT-INDEX-RESCHEDULE` | 0 | $0.00 | yes | 1.0 | 5 | 7 | 16 | $2.85 |
| single reviewer | `OPT-INDEX-RESCHEDULE` | 0 | $0.00 | yes | 0.5 | 2 | 0 | 7 | $0.64 |
| simple rules | `OPT-SCALE-TEMP` | 0 | $128.96 | no | 0.0 | 0 | 0 | 0 | $0.00 |

Read honestly:

- **The decision did not need five roles.** One reviewer reached the same option for 22% of the cost. On a scenario
  this clear, the ensemble is not what finds the answer.
- **The difference was scepticism, not accuracy.** The five-role review flagged both hidden gaps where the single
  reviewer flagged one, ran 16 checks against 7, and left 7 disagreements unresolved rather than papering over them.
- **It costs more than money.** Five unsupported numbers against two: more voices produce more claims the evidence
  does not carry. Each was flagged by the same grounding check, which is why they are countable at all.
- **The rules were not wrong about the SLO, only about the price.** A threshold cannot distinguish a heavy query from
  a busy cluster, so it buys capacity and misses the cause.
- **It stopped early.** The five-role run hit the $2.80 per-run cap in round 3, so its last round is incomplete.

## The same test on a free local model

`campaign-overlap`, `llama3.2:3b` through Ollama with an 8k context window, two rounds, 2026-09-18
(`runs/evaluate-campaign-ollama.json`, not committed because it holds no usable result):

| Approach | Recommendation | Citation errors | Gap recall | Spend |
|---|---|---:|---:|---:|
| five-role review | `Evolutionary Experiment Designer` (not an option) | 96 | 1.0 | $0.00 |
| single reviewer | a sentence of prose, not an option id | 14 | 0.0 | $0.00 |
| simple rules | `OPT-SCALE-TEMP` | 0 | 0.0 | $0.00 |

Neither model run is scoreable: `breach_slot_regret` and `cost_regret_usd` are `None` because the recommendation does
not name an option the scenario offers. The gap recall of 1.0 for the five-role run is not a success either; naming
every gap while failing to produce a decision is not a useful review.

The finding worth keeping is about the guardrails, not the model. A weak model does not fail quietly here: 96 invented
citations were rejected and counted, and an invented option name could not be scored, so nothing plausible-looking
reached the decision record. A tool that only summarised model output would have reported a confident recommendation
built on nothing.

Practical notes from the run: a turn carries about 5,600 tokens, so Ollama's default 4k window truncates it silently;
an 8 GB Docker allowance runs a 3B model at an 8k window but was killed at 16k, and killed an 8B model outright.

**What this run is not.** It compares a 3B model on CPU with a frontier hosted model, which is not a comparison
between vendors or between open and closed weights. The machine could not load an 8B model at all. An open-weight
model of serious size, served with enough memory - Llama 3.3 70B or DeepSeek V3, both reachable through the settings
page - is the test that would say something about open weights, and it has not been run.

What this does not show: whether the ensemble decides better when evidence is contested, missing or asymmetric, which
is the case it is built for. That needs a scenario designed to punish a single confident reviewer, and several runs of
each approach. Until then this is one data point, in favour of "use one reviewer for a clear decision, and five when
you need to know what nobody has measured".
