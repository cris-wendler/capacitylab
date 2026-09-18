# SPDX-License-Identifier: AGPL-3.0-or-later
"""The decision record merges reports of the same gap instead of listing each role's wording separately."""

from capacitylab.simulation.decision import group_missing_evidence


def test_reports_of_the_same_gap_merge_whatever_the_wording():
    grouped = group_missing_evidence([
        ("reliability_engineer", "GAP-PROD-PLAN-VALIDATION: index or rewrite benefit unproven outside the sandbox"),
        ("database_engineer", "GAP-PROD-PLAN-VALIDATION: no production-scale test exists for the EXISTS rewrite"),
        ("finops_analyst", "A dollar figure for failover risk, blocked by GAP-FAILOVER-DURATION."),
        ("reliability_engineer", "GAP-FAILOVER-DURATION"),
    ])
    by_item = {g["item"]: g for g in grouped}

    plan = by_item["GAP-PROD-PLAN-VALIDATION"]
    assert plan["raised_by"] == ["database_engineer", "reliability_engineer"]
    assert len(plan["details"]) == 2, "each role's distinct reason is kept"

    assert by_item["GAP-FAILOVER-DURATION"]["details"] == [], "a bare gap id adds no detail"
    assert "A dollar figure for failover risk, blocked by GAP-FAILOVER-DURATION." in by_item, \
        "a request that only mentions a gap mid-sentence is a different request"
    assert len(grouped) == 3


def test_identical_reports_from_two_roles_become_one_entry():
    grouped = group_missing_evidence([("application_owner", "Batch deadline owner"),
                                      ("reliability_engineer", "Batch deadline owner")])
    assert grouped == [{"item": "Batch deadline owner", "raised_by": ["application_owner", "reliability_engineer"],
                        "details": []}]
