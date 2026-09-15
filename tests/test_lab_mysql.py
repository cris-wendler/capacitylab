"""Real workload on the local MySQL 8.0 lab. Run with the container up: CAPACITYLAB_TEST_MYSQL=1 pytest -m mysql"""

import os

import pytest

from capacitylab.lab.runner import LabConfig, run_lab, write_evidence
from capacitylab.scenarios.loader import load_scenario
from capacitylab.scenarios.validate import validate_scenario
from capacitylab.settings import Settings
from capacitylab.simulation.roles import RoleId
from capacitylab.simulation.schema import ToolRequest
from capacitylab.simulation.tools import ToolEnvironment, execute_tool

pytestmark = [
    pytest.mark.mysql,
    pytest.mark.skipif(os.environ.get("CAPACITYLAB_TEST_MYSQL") != "1", reason="set CAPACITYLAB_TEST_MYSQL=1 with the lab up"),
]


def test_lab_run_collects_real_evidence(tmp_path):
    scenario, _ = load_scenario("campaign-overlap")
    items = run_lab(scenario, Settings.from_env().mysql, LabConfig(duration_s=4, total_qps=80, workers=8))
    by_id = {i.id: i for i in items}
    for phase in ("BASE", "EVENT", "EVENTIDX"):
        digest = by_id[f"EV-LAB-{phase}-DIG"]
        assert {f["fingerprint_id"] for f in digest.data["fingerprints"]} >= {"QF-ORDER-HISTORY", "QF-CHECKOUT-WRITE"}
        latency = by_id[f"EV-LAB-{phase}-LAT"].data
        assert latency["client_latency"]["QF-ORDER-HISTORY"]["calls"] > 0
        assert by_id[f"EV-LAB-{phase}-PLAN-AUD"].data["steps"]
        assert all(i.environment == "lab" for i in items)
    assert by_id["EV-LAB-EVENT-LAT"].data["batch_job"]["chunks_committed"] > 0
    assert by_id["EV-LAB-EVENTNB-LAT"].data["batch_job"]["running"] is False
    assert "idx_orders_tenant_day_customer" in by_id["EV-LAB-EVENTIDX-PLAN-AUD"].data["raw_text"]
    assert "idx_orders_tenant_day_customer" not in by_id["EV-LAB-EVENT-PLAN-AUD"].data["raw_text"]

    path = write_evidence(items, tmp_path / "lab.yaml")
    merged_scenario, bundle = load_scenario("campaign-overlap", [path])
    assert not [i for i in validate_scenario(merged_scenario, bundle) if i.level == "error"]


def test_repeated_passes_report_a_spread():
    from capacitylab.lab.runner import run_lab_repeats

    scenario, _ = load_scenario("campaign-overlap")
    items = run_lab_repeats(scenario, Settings.from_env().mysql,
                            LabConfig(duration_s=3, total_qps=60, workers=6, repeats=2))
    by_id = {i.id: i for i in items}
    assert "EV-LAB-BASE-DIG" in by_id, "detailed evidence comes from the first pass"
    assert not [i for i in items if i.id.endswith("-R2-DIG")], "later passes do not duplicate per-phase items"
    spread = by_id["EV-LAB-SPREAD"].data
    assert spread["passes"] == 2 and spread["scenario_id"] == "campaign-overlap"
    checkout = spread["p95_ms"]["QF-CHECKOUT-WRITE"]["EVENT"]
    assert len(checkout["values"]) == 2 and checkout["min"] <= checkout["median"] <= checkout["max"]
    assert set(spread["lock_waits"]["EVENT"]) == {"values", "median", "min", "max", "range_pct"}


def _percona_image_present() -> bool:
    import subprocess

    from capacitylab.lab.percona import DEFAULT_IMAGE

    return subprocess.run(["docker", "image", "inspect", DEFAULT_IMAGE], capture_output=True).returncode == 0


@pytest.mark.skipif(not _percona_image_present(), reason="docker pull percona/percona-toolkit first")
def test_lab_run_with_percona_toolkit():
    scenario, _ = load_scenario("campaign-overlap")
    items = run_lab(scenario, Settings.from_env().mysql, LabConfig(duration_s=4, total_qps=80, workers=8, percona=True))
    by_id = {i.id: i for i in items}
    digest = by_id["EV-LAB-EVENT-PTQD"].data
    assert "QF-ORDER-HISTORY" in {c["fingerprint_id"] for c in digest["classes"]}
    assert by_id["EV-LAB-EVENTIDX-PTDK"].data["total_indexes"] >= 1
    assert by_id["EV-LAB-PTVA"].data["advice"] and "deadlocks" in by_id["EV-LAB-PTDL"].data
    assert by_id["EV-LAB-CMP"].data["percona_toolkit"].startswith("pt-query-digest")
    assert "deadlocks" in by_id["EV-LAB-EVENT-LAT"].data["server_counters"]


@pytest.mark.skipif(not _percona_image_present(), reason="docker pull percona/percona-toolkit first")
def test_percona_duplicate_keys_tool_finds_redundant_index():
    scenario, bundle = load_scenario("campaign-overlap")
    env = ToolEnvironment(scenario, bundle.copy(), lab_mysql=Settings.from_env().mysql)
    record, item = execute_tool(env, "TC-001", 1, [RoleId.DATABASE_ENGINEER], ToolRequest(
        tool="percona_duplicate_keys", arguments_json='{"index_candidate": "IDX-TENANT-CUSTOMER-DAY"}', purpose="redundancy"))
    assert record.status == "ok", record.error
    (finding,) = item.data["result"]["report"]["findings"]
    assert finding["redundant_index"] == "idx_orders_tenant_customer"


def test_lab_load_test_tool():
    scenario, bundle = load_scenario("campaign-overlap")
    env = ToolEnvironment(scenario, bundle.copy(), lab_mysql=Settings.from_env().mysql)
    record, item = execute_tool(env, "TC-001", 1, [RoleId.RELIABILITY_ENGINEER], ToolRequest(
        tool="lab_load_test", arguments_json='{"batch_in_test": false, "duration_s": 3}', purpose="batch move"))
    assert record.status == "ok", record.error
    result = item.data["result"]
    assert item.environment == "lab" and set(result["latency_and_counters"]) == {"CTRL", "TEST"}
    assert result["latency_and_counters"]["CTRL"]["batch_job"]["running"] is True
    assert result["latency_and_counters"]["TEST"]["batch_job"]["running"] is False
