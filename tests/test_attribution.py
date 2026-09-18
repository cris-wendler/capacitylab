# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is causing a breaching slot, and which remedy that shape argues for."""

import pytest

from capacitylab.capacity.attribution import BATCH_SHARE_PCT, attribute
from capacitylab.capacity.options import OptimizationEffect, build_context, evaluate_option
from capacitylab.scenarios.loader import load_scenario


@pytest.fixture
def campaign():
    return load_scenario("campaign-overlap")


def test_shares_add_up_to_the_modeled_demand(campaign):
    """Attribution must decompose the same number the capacity model used, not a second estimate of it."""
    scenario, bundle = campaign
    ctx = build_context(scenario, bundle)
    result = attribute(ctx)
    worst = result.worst

    assert worst.slot == "19:00" and worst.utilization_pct == 115.0
    assert worst.utilization_pct == evaluate_option(ctx, "OPT-KEEP").peak_utilization_pct
    assert sum(s.cores for s in worst.by_statement) == pytest.approx(worst.demand_cores, abs=0.01)
    assert sum(s.pct for s in worst.by_statement) == pytest.approx(100.0, abs=0.5)
    # Tenants account for everything except the batch job, which belongs to no tenant.
    assert sum(s.cores for s in worst.by_tenant) == pytest.approx(worst.demand_cores - worst.batch_cores, abs=0.01)
    assert set(worst.breached_slos) == {"SLO-CHECKOUT", "SLO-ORDER-HISTORY"}
    assert [s.slot for s in result.slots][0] == "18:00" and len(result.slots) == 12


def test_the_batch_job_and_the_dominant_tenant_are_named(campaign):
    scenario, bundle = campaign
    result = attribute(build_context(scenario, bundle))
    causes = {c.kind: c for c in result.causes}

    assert causes["batch_job"].subject == "loyalty-recalculation"
    assert causes["batch_job"].pct >= BATCH_SHARE_PCT
    assert "move the batch job" in causes["batch_job"].remedy
    assert "19:00" in causes["batch_job"].detail

    assert causes["tenant"].subject == "alder" and causes["tenant"].pct > 60
    assert "entitlement" in causes["tenant"].detail
    # No single statement dominates this scenario, so none is proposed for an index or a rewrite.
    assert "statement" not in causes


def test_moving_the_batch_job_removes_it_from_the_causes(campaign):
    scenario, bundle = campaign
    ctx = build_context(scenario, bundle)
    after = attribute(ctx, "OPT-RESCHEDULE")
    assert "batch_job" not in {c.kind for c in after.causes}
    assert all(s.batch_cores == 0 for s in after.slots)
    # The tenant's share rises once the job it was sharing the slot with is gone.
    assert after.causes[0].pct > next(c for c in attribute(ctx).causes if c.kind == "tenant").pct


def test_a_measured_rewrite_changes_who_is_to_blame(campaign):
    """Attribution reflects the workload an option would leave behind, not only today's."""
    scenario, bundle = campaign
    ctx = build_context(scenario, bundle)
    effects = {"RW-EXISTS": OptimizationEffect(
        index_candidate="RW-EXISTS", kind="rewrite", equivalent=True,
        cpu_multiplier_by_fingerprint={"QF-AUDIENCE": 0.35})}
    before = attribute(ctx, "OPT-KEEP")
    after = attribute(ctx, "OPT-REWRITE", effects)

    def audience(result):
        return next(s.pct for s in result.worst.by_statement if s.name == "QF-AUDIENCE")

    assert audience(after) < audience(before)
    assert after.worst.utilization_pct < before.worst.utilization_pct


def test_a_quiet_scenario_blames_nothing(campaign):
    """An oversized cluster has no breaching slot, so there is nothing to attribute and nothing to recommend."""
    scenario, bundle = load_scenario("downsize-reader")
    result = attribute(build_context(scenario, bundle))
    assert result.slots == [] and result.causes == []
    assert result.window == "no slot over the threshold"
    assert result.worst is None


def test_evenly_spread_load_says_capacity_is_the_honest_answer(campaign):
    """With no dominant statement, tenant or job, nothing cheaper exists to fix first."""
    scenario, bundle = campaign
    ctx = build_context(scenario, bundle)
    # Every statement equally costly, every tenant equally busy, and no batch job: a genuinely flat workload.
    tenants = ["alder", "birch", "cedar", "dune", "elm"]
    slots = len(ctx.slot_labels)
    ctx.series = {fid: {t: [2.0] * slots for t in tenants} for fid in ctx.series}
    ctx.cpu_ms = dict.fromkeys(ctx.cpu_ms, 8.0)
    ctx.batch = None
    result = attribute(ctx, threshold_pct=0.0, only_breached=False)
    assert [c.kind for c in result.causes] == ["broad_load"]
    assert "nothing cheaper to fix first" in result.causes[0].detail


def test_the_tool_reports_attribution_with_its_limits(campaign):
    from capacitylab.simulation.tools import TOOLS, ToolEnvironment

    scenario, bundle = campaign
    env = ToolEnvironment(scenario=scenario, bundle=bundle)
    outcome = TOOLS["load_attribution"].handler(env, {}, "database_engineer")
    assert "loyalty-recalculation" in outcome.title or "OPT-KEEP" in outcome.title
    assert outcome.data["causes"][0]["kind"] == "batch_job"
    assert outcome.data["slots"][0]["by_tenant"][0]["id"] == "alder"
    assert any("not in this evidence" in c for c in outcome.caveats)
    assert any("not a decision" in c for c in outcome.caveats)
    with pytest.raises(ValueError, match="unknown option"):
        TOOLS["load_attribution"].handler(env, {"option_id": "OPT-NOPE"}, "database_engineer")


def test_a_cause_says_how_sure_it_is(campaign):
    """A subject that dominates every slot is a different claim from one that scrapes past the threshold."""
    scenario, bundle = campaign
    causes = {c.kind: c for c in attribute(build_context(scenario, bundle)).causes}

    tenant = causes["tenant"]
    assert tenant.confidence == "high"
    assert tenant.slots_present == tenant.slots_total == 12  # above the threshold in every breaching slot
    assert tenant.lead_pct > 50  # and far ahead of the next tenant
    assert "12 of 12 slots" in tenant.confidence_reason

    # The batch job runs in only part of the window, so it drives the breach less consistently.
    batch = causes["batch_job"]
    assert batch.confidence == "medium" and batch.slots_present < batch.slots_total


def test_confidence_falls_when_the_reading_is_close_or_patchy():
    from capacitylab.capacity.attribution import _confidence

    clear, why = _confidence(pct=70, threshold=40, lead=40, present=10, total=10)
    assert clear == "high" and "10 of 10" in why

    # Just past the threshold, and barely ahead of the next subject: not a finding to act on alone.
    close, why = _confidence(pct=42, threshold=40, lead=3, present=10, total=10)
    assert close == "low" and "too close to the next" in why

    # Decisive, but only in three slots out of ten.
    patchy, why = _confidence(pct=70, threshold=40, lead=40, present=3, total=10)
    assert patchy == "low" and "only part of the window" in why

    steady, _ = _confidence(pct=55, threshold=40, lead=20, present=7, total=10)
    assert steady == "medium"


def test_an_evenly_spread_workload_is_a_confident_reading_too(campaign):
    """"Nothing dominates" is a firm conclusion, not an absence of one."""
    scenario, bundle = campaign
    ctx = build_context(scenario, bundle)
    tenants = ["alder", "birch", "cedar", "dune", "elm"]
    ctx.series = {fid: {t: [2.0] * len(ctx.slot_labels) for t in tenants} for fid in ctx.series}
    ctx.cpu_ms = dict.fromkeys(ctx.cpu_ms, 8.0)
    ctx.batch = None
    cause = attribute(ctx, threshold_pct=0.0, only_breached=False).causes[0]
    assert cause.kind == "broad_load" and cause.confidence == "high"
    assert "below every threshold" in cause.confidence_reason


def test_the_finding_and_the_tool_both_carry_the_confidence(campaign):
    from capacitylab.findings import review_findings
    from capacitylab.simulation.tools import TOOLS, ToolEnvironment

    scenario, bundle = campaign
    finding = next(f for f in review_findings(scenario, bundle) if f.id == "FND-CAP-CAUSE")
    assert "high confidence" in finding.recommendation and "medium confidence" in finding.recommendation

    outcome = TOOLS["load_attribution"].handler(ToolEnvironment(scenario=scenario, bundle=bundle), {},
                                                "database_engineer")
    assert {c["confidence"] for c in outcome.data["causes"]} <= {"high", "medium", "low"}
    assert all(c["confidence_reason"] for c in outcome.data["causes"])
