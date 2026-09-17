"""Every money figure shown for the scenarios must follow from its inputs by arithmetic.

These tests recompute each figure from the rate card, budget, calendar and assumptions, independently of the
capacity model, and compare it with what the model and the evidence report.
"""

import pytest

from capacitylab.capacity.options import build_context, evaluate_all
from capacitylab.evaluation.compare import compare
from capacitylab.scenarios.loader import load_scenario
from capacitylab.simulation.providers.mock import MockProvider


def outcomes(scenario, bundle, overrides=None):
    return {o.option_id: o for o in evaluate_all(build_context(scenario, bundle, overrides))}


@pytest.fixture(scope="module")
def sale():
    return load_scenario("campaign-overlap")


@pytest.fixture(scope="module")
def reader():
    return load_scenario("downsize-reader")


def rates(bundle):
    card = bundle.get("EV-RATE-001").data
    return card["instance_hourly"], card["hours_per_month"]


def test_every_rate_is_the_same_price_per_vcpu_hour(sale):
    from capacitylab.capacity.catalog import CATALOG

    hourly, _ = rates(sale[1])
    for name, price in hourly.items():
        assert price == pytest.approx(0.145 * CATALOG[name].vcpu), name


def test_budget_evidence_matches_the_rate_card(sale, reader):
    for (scenario, bundle), nodes in ((sale, 2), (reader, 2)):
        hourly, hours = rates(bundle)
        budget = bundle.get("EV-BUD-001").data
        assert budget["current_monthly_instance_cost_usd"] == pytest.approx(
            hourly[scenario.cluster.writer_instance] * hours * nodes)
    budget = sale[1].get("EV-BUD-001").data
    # observed on 15 October of a 31-day month, spending evenly
    assert budget["month_to_date_usd"] == pytest.approx(budget["forecast_month_usd"] * 15 / 31, abs=0.01)


def test_scale_options_cost_the_price_difference_times_hours_times_nodes(sale):
    scenario, bundle = sale
    hourly, hours = rates(bundle)
    step = hourly["db.r6i.32xlarge"] - hourly["db.r6i.16xlarge"]
    o = outcomes(scenario, bundle)
    assert o["OPT-SCALE-TEMP"].cost_delta_event_usd == pytest.approx(step * 7 * 2)  # 16:00 to 23:00
    assert o["OPT-SCALE-SEASON"].cost_delta_event_usd == pytest.approx(step * 24 * 42 * 2)
    assert o["OPT-RESIZE-UP"].cost_delta_month_usd == pytest.approx(step * hours * 2)
    assert o["OPT-SCALE-RESCHEDULE"].cost_delta_event_usd == o["OPT-SCALE-TEMP"].cost_delta_event_usd
    assert 12 * o["OPT-RESIZE-UP"].cost_delta_month_usd == pytest.approx(162585.60)


def test_revenue_at_risk_is_slots_times_hours_times_revenue_times_loss(sale):
    scenario, bundle = sale
    previous = bundle.get("EV-CAL-002").data
    per_hour = scenario.assumption("A-SALE-REVENUE-PER-HOUR").value
    share = scenario.assumption("A-CHECKOUT-LOSS-SHARE").value
    assert per_hour == previous["sale_revenue_usd"] / previous["duration_hours"]
    sale_slots = sum(1 for m in scenario.horizon.slot_starts() if 18 * 60 <= m < 21 * 60)
    for multiplier in (3.1, 4.2, 5.0, 6.0):
        keep = outcomes(scenario, bundle, {"A-CAMPAIGN-MULT": multiplier})["OPT-KEEP"]
        assert 0 <= keep.revenue_at_risk_slots <= sale_slots
        assert keep.revenue_at_risk_usd == pytest.approx(keep.revenue_at_risk_slots * 0.25 * per_hour * share)
    keep = outcomes(scenario, bundle)["OPT-KEEP"]
    assert keep.revenue_at_risk_usd == 304500
    # slots are counted once for revenue, once per SLO for breaches
    assert keep.total_slo_breach_slots == sum(s.breach_slots for s in keep.slo.values())


def test_downsizing_saves_the_price_difference_every_month(reader):
    scenario, bundle = reader
    hourly, hours = rates(bundle)
    current = hourly[scenario.cluster.reader_instances[0]]
    o = outcomes(scenario, bundle)
    assert o["OPT-DOWNSIZE-16XL"].cost_delta_month_usd == pytest.approx((hourly["db.r6i.16xlarge"] - current) * hours)
    assert o["OPT-DOWNSIZE-8XL"].cost_delta_month_usd == pytest.approx((hourly["db.r6i.8xlarge"] - current) * hours)


def test_comparison_costs_one_off_and_monthly_amounts_in_one_unit(sale, reader):
    twelve = lambda x: x["cost_delta_event_usd"] + 12 * x["cost_delta_month_usd"]  # noqa: E731
    for scenario, bundle in (sale, reader):
        by_name = {a.approach: a for a in compare(scenario, bundle, MockProvider).approaches}
        rule, review = by_name["rule_based"], by_name["multi_stakeholder"]
        # the review recommended the reference best here (zero regret), so its outcome carries the best option's costs,
        # measured index effect included
        assert review.cost_regret_usd == 0 and review.recommendation in rule.reference_best
        expected = twelve(rule.outcome_under_reference) - twelve(review.outcome_under_reference)
        assert rule.cost_regret_usd == pytest.approx(expected)
    # evening scale-up $129.92 against index-and-move at 12 x $0.08 = $0.96
    by_name = {a.approach: a for a in compare(*sale, MockProvider).approaches}
    assert by_name["rule_based"].cost_regret_usd == pytest.approx(129.92 - 12 * 0.08)
