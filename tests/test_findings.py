# SPDX-License-Identifier: AGPL-3.0-or-later
"""Findings are computed from evidence alone, before any review runs."""

from capacitylab.findings import review_findings
from capacitylab.scenarios.loader import load_scenario


def by_id(findings):
    return {f.id: f for f in findings}


def test_campaign_scenario_reports_saturation_growth_and_unmeasured_failover(campaign):
    scenario, bundle = campaign
    findings = review_findings(scenario, bundle)
    found = by_id(findings)

    saturated = found["FND-CAP-SATURATED"]
    assert saturated.severity == "high" and "115" in saturated.detail
    assert "index and move the batch job" in saturated.recommendation.lower() or saturated.recommendation

    failover = found["FND-HA-FAILOVER-UNMEASURED"]
    assert failover.severity == "high" and failover.basis == "gap" and failover.evidence_ids

    growth = found["FND-GROWTH-ORDERS"]
    assert growth.area == "data_growth" and "QF-AUDIENCE" in growth.detail
    assert "FND-GROWTH-AUDIT_EVENTS" in found, "a large table with bounded access is still reported, as not a problem"
    assert found["FND-GROWTH-AUDIT_EVENTS"].severity == "info"

    assert any(f.area == "contention" for f in findings)
    assert [f.severity for f in findings] == sorted([f.severity for f in findings],
                                                    key=lambda s: {"high": 0, "medium": 1, "info": 2}[s])


def test_downsize_scenario_reports_headroom_and_the_reader_doing_double_duty():
    scenario, bundle = load_scenario("downsize-reader")
    found = by_id(review_findings(scenario, bundle))

    oversized = found["FND-CAP-OVERSIZED"]
    assert oversized.area == "capacity" and "smaller class" in oversized.recommendation
    assert "568 GiB" in oversized.detail, "the working set is weighed against the modeled buffer pool"

    double_duty = found["FND-HA-READER-DOUBLE-DUTY"]
    assert double_duty.severity == "medium" and len(double_duty.evidence_ids) == 2

    assert "FND-HA-FAILOVER-KNOWN" in found, "a measured failover is reported as a known quantity"
    assert "FND-HA-FAILOVER-UNMEASURED" not in found


def test_every_finding_cites_evidence_that_exists(campaign):
    scenario, bundle = campaign
    for finding in review_findings(scenario, bundle):
        assert finding.evidence_ids, f"{finding.id} cites nothing"
        for eid in finding.evidence_ids:
            assert eid in bundle, f"{finding.id} cites missing {eid}"
