# SPDX-License-Identifier: AGPL-3.0-or-later
import math

import pytest

from capacitylab.capacity.cost import (
    RateCard,
    instance_cost,
    monthly_resize_delta,
    resize_delta_for_hours,
    storage_cost_month,
)
from capacitylab.capacity.options import OptimizationEffect, build_context, evaluate_all, evaluate_option
from capacitylab.capacity.queueing import StatementLoad, erlang_c, evaluate_slot, wait_percentile_s
from capacitylab.scenarios.models import OptionSpec

# Test prices only. Real runs read the rate card from scenario evidence (EV-RATE-001), never from code.
# These double at each class so the arithmetic below is checkable by eye: 10 h x 2 nodes x 1.2 = 24.0,
# 7 h x (2.4 - 1.2) = 8.4, a month of downsizing = -438.00. Real list prices would obscure that and would
# make the assertions go stale.
CARD = RateCard(instance_hourly={"db.r6g.xlarge": 0.6, "db.r6g.2xlarge": 1.2, "db.r6g.4xlarge": 2.4}, storage_gib_month=0.1)


def test_erlang_c_known_values():
    assert erlang_c(1, 0.5) == pytest.approx(0.5)
    assert erlang_c(2, 1.0) == pytest.approx(1 / 3)
    assert erlang_c(4, 4.0) == 1.0 and erlang_c(4, 0) == 0.0


def test_wait_percentile_is_unbounded_when_unstable():
    assert math.isinf(wait_percentile_s(2, arrival_rate=30, mean_service_s=0.1))
    assert wait_percentile_s(8, arrival_rate=10, mean_service_s=0.01) == 0.0


def test_slot_saturation_and_utilization():
    ok = evaluate_slot(8, [StatementLoad("q", qps=100, cpu_ms=20)], reserved_cores=1.0)
    assert ok.utilization_pct == pytest.approx(37.5) and not ok.saturated
    bad = evaluate_slot(8, [StatementLoad("q", qps=400, cpu_ms=20)])
    assert bad.saturated and math.isinf(bad.p95_latency_ms["q"])


def test_cost_arithmetic_is_exact():
    assert instance_cost(CARD, "db.r6g.2xlarge", hours=10, count=2) == 24.0
    assert resize_delta_for_hours(CARD, "db.r6g.2xlarge", "db.r6g.4xlarge", hours=7) == 8.4
    assert monthly_resize_delta(CARD, "db.r6g.2xlarge", "db.r6g.xlarge") == -438.0
    assert storage_cost_month(CARD, 10) == 1.0
    with pytest.raises(ValueError):
        instance_cost(CARD, "db.r6g.large", 1)


def test_campaign_options_are_deterministic_and_ordered(campaign):
    scenario, bundle = campaign
    ctx = build_context(scenario, bundle)
    outcomes = {o.option_id: o for o in evaluate_all(ctx)}
    assert outcomes["OPT-KEEP"].total_slo_breach_slots > 0 and outcomes["OPT-KEEP"].saturated_slots > 0
    scale = outcomes["OPT-SCALE-TEMP"]
    assert scale.total_slo_breach_slots == 0 and scale.cost_delta_event_usd == 129.92  # (18.56 - 9.28) * 7 h * 2 nodes
    season, resize = outcomes["OPT-SCALE-SEASON"], outcomes["OPT-RESIZE-UP"]
    assert season.cost_delta_event_usd == 18708.48 and season.cost_delta_month_usd == 0  # 9.28 * 24 h * 42 days * 2
    assert resize.cost_delta_month_usd == 13548.8 and resize.cost_delta_event_usd == 0  # 9.28 * 730 h * 2 nodes
    assert season.total_slo_breach_slots == resize.total_slo_breach_slots == 0
    assert any("failover" in u for u in season.unknowns) and any("failover" in u for u in resize.unknowns)
    assert any("failover" in u for u in scale.unknowns)
    assert outcomes["OPT-RESCHEDULE"].batch_deadline_ok is True
    assert outcomes["OPT-RESCHEDULE"].slo["SLO-CHECKOUT"].worst_p95_ms < 250
    index = outcomes["OPT-INDEX"]
    assert any("NOT applied" in u for u in index.unknowns)
    assert index.utilization_by_slot == outcomes["OPT-KEEP"].utilization_by_slot
    assert [o.model_dump() for o in evaluate_all(ctx)] == [o.model_dump() for o in outcomes.values()]


def test_measured_effects_and_regressions_propagate(campaign):
    scenario, bundle = campaign
    ctx = build_context(scenario, bundle)
    keep = evaluate_option(ctx, "OPT-KEEP")
    better = evaluate_option(ctx, "OPT-INDEX", {"IDX-TENANT-DAY-CUSTOMER": OptimizationEffect(
        index_candidate="IDX-TENANT-DAY-CUSTOMER", cpu_multiplier_by_fingerprint={"QF-AUDIENCE": 0.3})})
    worse = evaluate_option(ctx, "OPT-INDEX", {"IDX-TENANT-DAY-CUSTOMER": OptimizationEffect(
        index_candidate="IDX-TENANT-DAY-CUSTOMER", cpu_multiplier_by_fingerprint={"QF-ORDER-HISTORY": 3.0})})
    assert better.peak_utilization_pct < keep.peak_utilization_pct < worse.peak_utilization_pct


def test_assumption_overrides_change_forecast(campaign):
    scenario, bundle = campaign
    base = evaluate_option(build_context(scenario, bundle), "OPT-KEEP")
    lower = evaluate_option(build_context(scenario, bundle, {"A-CAMPAIGN-MULT": 3.1}), "OPT-KEEP")
    assert lower.peak_utilization_pct < base.peak_utilization_pct
    with pytest.raises(ValueError):
        build_context(scenario, bundle, {"A-NOT-REAL": 1})


def test_downsizing_flags_working_set_and_failover_target(downsize):
    scenario, bundle = downsize
    outcomes = {o.option_id: o for o in evaluate_all(build_context(scenario, bundle))}
    two = outcomes["OPT-DOWNSIZE-16XL"]
    assert two.cost_delta_month_usd == -6774.4  # (9.28 - 18.56) * 730 h
    assert two.revenue_at_risk_usd is None, "no event with revenue on the line in this scenario"
    assert any("Working set" in u for u in two.unknowns) and any("failover target" in u for u in two.unknowns)
    assert outcomes["OPT-DOWNSIZE-8XL"].peak_utilization_pct > outcomes["OPT-DOWNSIZE-16XL"].peak_utilization_pct


def test_cyclic_combined_option_is_rejected(campaign):
    scenario, bundle = campaign
    broken = scenario.model_copy(deep=True)
    broken.options.append(OptionSpec(id="OPT-LOOP", kind="combined", label="loop", params={"components": ["OPT-LOOP"]}))
    with pytest.raises(ValueError, match="cyclically"):
        evaluate_option(build_context(broken, bundle), "OPT-LOOP")


def test_revenue_at_risk_counts_breached_slots_inside_the_sale(campaign):
    scenario, bundle = campaign
    outcomes = {o.option_id: o for o in evaluate_all(build_context(scenario, bundle))}
    keep = outcomes["OPT-KEEP"]
    # each breached 15-minute sale slot loses 12% of $1.45M/h: 0.25 h * 1,450,000 * 0.12 = $43,500
    assert keep.revenue_at_risk_slots > 0 and keep.revenue_at_risk_usd == keep.revenue_at_risk_slots * 43500
    assert outcomes["OPT-RESCHEDULE"].revenue_at_risk_usd == 0.0
    lower = evaluate_option(build_context(scenario, bundle, {"A-CAMPAIGN-MULT": 3.1}), "OPT-KEEP")
    higher = evaluate_option(build_context(scenario, bundle, {"A-CAMPAIGN-MULT": 6.0}), "OPT-KEEP")
    assert lower.revenue_at_risk_usd < keep.revenue_at_risk_usd < higher.revenue_at_risk_usd
    unset = build_context(scenario, bundle, {"A-CHECKOUT-LOSS-SHARE": ""})
    assert evaluate_option(unset, "OPT-KEEP").revenue_at_risk_usd is None, "no figure without both assumptions"
