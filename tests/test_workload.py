import pytest

from capacitylab.capacity.options import build_forecast
from capacitylab.scenarios.loader import load_scenario
from capacitylab.workload.generator import Surge, generate


def test_same_seed_is_reproducible_and_seed_matters(campaign):
    scenario, _ = campaign
    slots = scenario.horizon.slot_starts()
    a = generate(scenario.workload, slots)
    b = generate(scenario.workload, slots)
    assert a == b
    other = scenario.workload.model_copy(update={"seed": scenario.workload.seed + 1})
    assert generate(other, slots)["digest"] != a["digest"]


def test_surge_applies_only_to_tenant_window_and_flag(campaign):
    scenario, _ = campaign
    slots = scenario.horizon.slot_starts()
    base = generate(scenario.workload, slots)["series"]
    surged = generate(scenario.workload, slots, [Surge(start_slot=24, end_slot=36, multiplier=5.0, tenant="alder",
                                                       flag="campaign_sensitive")])["series"]
    alder = zip(base["QF-ORDER-HISTORY"]["alder"], surged["QF-ORDER-HISTORY"]["alder"], strict=True)
    for i, (before, after) in enumerate(alder):
        assert after == pytest.approx(before * (5.0 if 24 <= i < 36 else 1.0), rel=1e-3)
    assert surged["QF-ORDER-HISTORY"]["birch"] == base["QF-ORDER-HISTORY"]["birch"]


def test_forecast_evidence_is_stable_across_loads():
    _, first = load_scenario("campaign-overlap")
    _, second = load_scenario("campaign-overlap")
    assert first.get("EV-FC-001").data["series_digest"] == second.get("EV-FC-001").data["series_digest"]
    assert first.get("EV-FC-001").provenance.value == "forecast"


def test_forecast_uses_assumption_values(campaign):
    scenario, _ = campaign
    values = {a.id: a.value for a in scenario.assumptions}
    low = build_forecast(scenario, {**values, "A-CAMPAIGN-MULT": 1.0})["series"]
    high = build_forecast(scenario, values)["series"]
    assert sum(high["QF-AUDIENCE"]["alder"]) > sum(low["QF-AUDIENCE"]["alder"])
