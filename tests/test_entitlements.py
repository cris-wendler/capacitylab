"""Tenant plans against modeled consumption, at baseline and during campaigns."""

from capacitylab.capacity.entitlements import status, tenant_entitlement_review
from capacitylab.findings import review_findings
from capacitylab.scenarios.loader import load_scenario
from capacitylab.simulation.roles import RoleId
from capacitylab.simulation.schema import ToolRequest
from capacitylab.simulation.tools import ToolEnvironment, execute_tool


def test_status_thresholds():
    assert status(78.7, 35) == "over" and status(8.5, 20) == "squeezed" and status(20, 20) == "within"
    assert status(10, 0) == "no plan"


def test_campaign_squeezes_standard_tenants_below_their_plans(campaign):
    scenario, bundle = campaign
    review, cited = tenant_entitlement_review(scenario, bundle)
    assert cited == ["EV-ENT-001"]
    alder, birch = review["by_tenant"]["alder"], review["by_tenant"]["birch"]
    assert alder["baseline_cpu_share_pct"] == 42.5 and alder["baseline_status"] == "within"
    assert alder["during_campaigns"][0]["cpu_share_pct"] == 78.7 and alder["during_campaigns"][0]["status"] == "over"
    assert birch["during_campaigns"][0] == {"campaign_tenant": "alder", "window": "18:00-21:00", "cpu_share_pct": 8.5,
                                            "status": "squeezed"}

    found = {f.id: f for f in review_findings(scenario, bundle)}
    squeeze = found["FND-TENANT-SQUEEZED-ALDER"]
    assert squeeze.severity == "high" and "birch 8.5% of 20%" in squeeze.detail
    assert "resource groups" in squeeze.recommendation and "managed MySQL variants may not" in squeeze.recommendation
    assert found["FND-TENANT-OVER-ALDER"].severity == "medium"


def test_balanced_scenario_reports_every_tenant_within_plan():
    scenario, bundle = load_scenario("downsize-reader")
    review, _ = tenant_entitlement_review(scenario, bundle)
    assert {t: r["baseline_status"] for t, r in review["by_tenant"].items()} == {
        "fir": "within", "hazel": "within", "juniper": "within"}
    assert "FND-TENANT-WITHIN-PLANS" in {f.id for f in review_findings(scenario, bundle)}


def test_tenant_representative_sees_only_its_own_plan(campaign):
    scenario, bundle = campaign
    env = ToolEnvironment(scenario, bundle.copy())
    record, item = execute_tool(env, "TC-001", 1, [RoleId.TENANT_REPRESENTATIVE], ToolRequest(
        tool="tenant_entitlement_review", arguments_json="{}", purpose="own plan"))
    assert record.status == "ok", record.error
    assert set(item.data["result"]["by_tenant"]) == {"alder"}

    record, item = execute_tool(env, "TC-002", 1, [RoleId.FINOPS_ANALYST], ToolRequest(
        tool="tenant_entitlement_review", arguments_json="{}", purpose="all plans"))
    assert record.status == "ok" and len(item.data["result"]["by_tenant"]) == 5

    record, _ = execute_tool(env, "TC-003", 1, [RoleId.DATABASE_ENGINEER], ToolRequest(
        tool="tenant_entitlement_review", arguments_json="{}", purpose="not mine"))
    assert record.status == "denied"
