# SPDX-License-Identifier: AGPL-3.0-or-later
"""Markdown rendering of a run ledger."""

from __future__ import annotations

from capacitylab.evidence.models import PROVENANCE_LABELS
from capacitylab.scenarios.models import Scenario
from capacitylab.simulation.run import SimulationRun


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(run: SimulationRun, scenario: Scenario) -> str:
    out: list[str] = []
    w = out.append
    w(f"# CapacityLab run {run.run_id}")
    if run.mocked:
        w("\n> **SCRIPTED RUN.** The turns came from fixed rules, not a language model.\n")
    w(f"\n**Scenario:** {scenario.title} (`{scenario.id}`, synthetic)  ")
    w(f"**Provider:** {run.provider} / {run.model} · **Prompt:** {run.prompt_version}  ")
    w(f"**Status:** {run.state.status.value} after {run.rounds_completed} round(s) · **Model spend:** ${run.spend_usd:.4f} "
      f"({run.input_tokens} in / {run.output_tokens} out tokens)  ")
    w(f"**Sandbox:** {run.sandbox_engine or 'not used'}\n")
    w(f"**Decision question.** {scenario.decision_question.strip()}\n")
    for warning in run.warnings:
        w(f"> Warning: {warning}\n")

    d = run.decision
    if d is None:
        w("No decision record (run did not start).")
        return "\n".join(out)

    w("## Final positions\n")
    w("| Role | Final position | Confidence | Position by round | Rationale |")
    w("|---|---|---|---|---|")
    for p in d.final_positions:
        w(f"| {p.role} | **{p.position}** | {p.confidence} | {' → '.join(p.position_history)} | {_cell(p.rationale)} |")
    w("")
    if d.disagreements:
        w("## Unresolved disagreements\n")
        for x in d.disagreements:
            ids = f" ({', '.join(x.evidence_ids)})" if x.evidence_ids else ""
            w(f"- **{x.kind}**: {_cell(x.summary)}{ids}")
        w("")
    else:
        w("All five agents ended on the same position. Agreement is not evidence of correctness.\n")

    w("## Options (modeled)\n")
    w(f"_{d.outcome_basis}_\n")
    w("| Option | Peak util. | Slots over threshold | Saturated slots | SLO breach slots | One-off cost | Monthly cost | Unknowns |")
    w("|---|---|---|---|---|---|---|---|")
    for o in d.option_outcomes:
        w(f"| {o.option_id} | {o.peak_utilization_pct}% @ {o.peak_slot} | {o.slots_over_threshold} | {o.saturated_slots} | "
          f"{o.total_slo_breach_slots} | ${o.cost_delta_event_usd} | ${o.cost_delta_month_usd} | {_cell('; '.join(o.unknowns)) or '-'} |")
    w("")

    if d.optimization_proposals:
        w("## Optimization proposals\n")
        for p in d.optimization_proposals:
            w(f"### {p['proposal_id']} ({p['role']}) → {p['target_fingerprint']}\n")
            w(f"1. **Evidence:** {', '.join(p['supporting_evidence_ids'])}")
            w(f"2. **Hypothesis:** {p['hypothesis']} **Uncertainty:** {p['uncertainty']}")
            w(f"3. **Change:** `{p['proposed_change']}`")
            w(f"4. **Tradeoffs:** {'; '.join(p['tradeoffs'])}")
            w(f"5. **Validation:** {p['validation_method']}")
            w(f"6. **Rollback:** {p['rollback']}")
            w(f"7. **Result:** {p['result_status']} ({', '.join(p['result_evidence_ids']) or 'no result evidence'})\n")

    if d.missing_evidence:
        w("## Missing evidence\n")
        gaps = {g.id: g.description for g in scenario.missing_evidence}
        for m in d.missing_evidence:
            w(f"- **{m['item']}** {gaps.get(m['item'], '')} (raised by {', '.join(m['raised_by'])})")
        w("")
    if d.open_tool_requests:
        w("## Requested but not executed\n")
        for r in d.open_tool_requests:
            w(f"- {r['call_id']} `{r['tool']}` by {', '.join(r['requested_by'])} ({r['status']}): {r['purpose']}")
        w("")

    w("## Tool calls\n")
    w("| Call | Round | Requested by | Tool | Status | Evidence |")
    w("|---|---|---|---|---|---|")
    for r in run.tool_calls:
        w(f"| {r.call_id} | {r.round} | {', '.join(r.requested_by)} | {r.tool} | {r.status}{(': ' + _cell(r.error)) if r.error else ''} "
          f"| {r.evidence_id or '-'} |")
    w("")

    w("## Transcript\n")
    for rnd in range(1, run.rounds_completed + 1):
        w(f"### Round {rnd}\n")
        for t in [t for t in run.turns if t.round == rnd]:
            dr = t.draft
            revised = f" _(revised: {dr.revision_reason})_" if dr.revised_from_previous else ""
            w(f"**{t.role}** → `{dr.position}` ({dr.confidence}){revised}\n")
            for c in dr.claims:
                w(f"- [{c.basis}] {c.statement} {' '.join('`' + i + '`' for i in c.evidence_ids)}")
            for a in dr.assumptions:
                w(f"- [assumption] {a}")
            for ch in dr.challenges:
                w(f"- **Challenge → {ch.target_role}** ({ch.target_statement}): {ch.reason}")
            for rq in dr.tool_requests:
                w(f"- Requests `{rq.tool}` {rq.arguments_json}: {rq.purpose}")
            for f in t.findings:
                w(f"- ⚠ validation {f.severity} `{f.code}` at {f.location}: {f.detail}")
            w("")

    w("## Provenance legend\n")
    for label in PROVENANCE_LABELS.values():
        w(f"- {label}")
    w("\n## Caveats\n")
    for c in d.caveats:
        w(f"- {c}")
    return "\n".join(out) + "\n"
