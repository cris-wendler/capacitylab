# SPDX-License-Identifier: AGPL-3.0-or-later
from capacitylab.evaluation.compare import compare, rule_based_recommendation
from capacitylab.simulation.providers.mock import MockProvider


def test_three_approaches_scored_on_identical_evidence(campaign):
    scenario, bundle = campaign
    digest = bundle.digest()
    report = compare(scenario, bundle, MockProvider)
    assert bundle.digest() == digest, "comparison must not mutate the shared evidence"
    by_name = {a.approach: a for a in report.approaches}
    assert set(by_name) == {"multi_stakeholder", "single_agent", "rule_based"}
    assert report.mocked and "Scripted agents" in report.caveats[0]
    rule = by_name["rule_based"]
    assert rule.recommendation == "OPT-SCALE-TEMP" and rule.root_cause_identified is False and rule.gap_recall == 0.0
    assert by_name["multi_stakeholder"].root_cause_identified is True
    for approach in report.approaches:
        assert approach.outcome_under_reference is not None
        assert approach.breach_slot_regret is not None and approach.cost_regret_usd is not None
    # Deliberately no assertion that any approach wins: results are reported, not presumed.


def test_rule_based_downsizing(downsize):
    option, reason = rule_based_recommendation(*downsize)
    assert option == "OPT-DOWNSIZE-16XL" and "downsize" in reason
