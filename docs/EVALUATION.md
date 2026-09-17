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
| Cost regret | (one-off + monthly) cost of the recommendation minus the reference best |
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
| campaign-overlap | rule-based | OPT-SCALE-TEMP | 0 | 129.84 | no | 0.0 | 0 | 0 | 0 |
| downsize-reader | five-role review | OPT-KEEP | 0 | 0.00 | n/a | 1.0 | 0 | 0 | 6 |
| downsize-reader | single reviewer | OPT-KEEP | 0 | 0.00 | n/a | 1.0 | 0 | 0 | 0 |
| downsize-reader | rule-based | OPT-DOWNSIZE-16XL | 0 | −6,774.40 | n/a | 0.0 | 0 | 0 | 0 |

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
