# SPDX-License-Identifier: AGPL-3.0-or-later
from capacitylab.capacity.cost import choose_rate_card
from capacitylab.capacity.options import build_context, evaluate_option, needed_instance_classes
from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.scenarios.loader import load_scenario

# Test prices, not AWS list prices: double the demo card, so the effect on option costs is easy to check.
FAMILY = {f"db.r6i.{s}": p for s, p in (("large", 0.58), ("xlarge", 1.16), ("2xlarge", 2.32), ("4xlarge", 4.64),
                                         ("8xlarge", 9.28), ("12xlarge", 13.92), ("16xlarge", 18.56),
                                         ("24xlarge", 27.84), ("32xlarge", 37.12))}


def aws_rate(prices, item_id="EV-AWS-RATE-TEST"):
    return EvidenceItem(id=item_id, kind=EvidenceKind.RATE_CARD, title="AWS prices", provenance=Provenance.OBSERVED,
                        environment="import", source="aws:pricing:get-products", method="MySQL, us-east-1, on-demand",
                        data={"currency": "USD", "instance_hourly": prices, "hours_per_month": 730,
                              "source_note": "List prices per instance-hour."})


def with_items(bundle, *items):
    return EvidenceBundle([*bundle, *items], bundle.missing)


def test_aws_prices_are_used_when_they_cover_every_needed_class():
    scenario, bundle = load_scenario("campaign-overlap")
    needed = needed_instance_classes(scenario)
    assert {"db.r6i.16xlarge", "db.r6i.32xlarge"} <= needed
    choice = choose_rate_card(bundle.by_kind(EvidenceKind.RATE_CARD) + [aws_rate(FAMILY)], needed)
    assert choice.prices_from_aws and choice.evidence_ids == ["EV-AWS-RATE-TEST", "EV-RATE-001"]
    assert choice.card.hourly("db.r6i.32xlarge") == 37.12 and choice.card.storage_gib_month == 0.10
    assert "Instance prices from EV-AWS-RATE-TEST" in choice.note and "storage rate from EV-RATE-001" in choice.note


def test_partial_aws_prices_fall_back_and_say_what_is_missing():
    scenario, bundle = load_scenario("campaign-overlap")
    partial = {k: v for k, v in FAMILY.items() if k != "db.r6i.32xlarge"}
    choice = choose_rate_card(bundle.by_kind(EvidenceKind.RATE_CARD) + [aws_rate(partial)],
                              needed_instance_classes(scenario))
    assert not choice.prices_from_aws and choice.evidence_ids == ["EV-RATE-001"]
    assert choice.missing_classes == ["db.r6i.32xlarge"] and "do not cover db.r6i.32xlarge" in choice.note


def test_option_costs_follow_the_attached_aws_prices():
    scenario, bundle = load_scenario("campaign-overlap")
    base = evaluate_option(build_context(scenario, bundle), "OPT-SCALE-TEMP")
    ctx = build_context(scenario, with_items(bundle, aws_rate(FAMILY)))
    priced = evaluate_option(ctx, "OPT-SCALE-TEMP")
    assert ctx.prices_from_aws and "EV-AWS-RATE-TEST" in ctx.evidence_ids
    assert base.cost_delta_event_usd > 0 and priced.cost_delta_event_usd == round(2 * base.cost_delta_event_usd, 2)
    assert priced.peak_utilization_pct == base.peak_utilization_pct, "prices change cost, not capacity"


def test_scenario_card_alone_is_unchanged():
    scenario, bundle = load_scenario("campaign-overlap")
    ctx = build_context(scenario, bundle)
    assert not ctx.prices_from_aws and ctx.rate_card_note == "Prices from EV-RATE-001."
