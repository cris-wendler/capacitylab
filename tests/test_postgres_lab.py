# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
from pathlib import Path

import pytest

from capacitylab.diagnostics.sandbox import PostgresSandbox, UnsafeSandboxTarget
from capacitylab.lab import pg_collector
from capacitylab.lab.pg_runner import fingerprint_summary, statement_templates
from capacitylab.lab.workload import (
    BATCH_UPDATE,
    BATCH_UPDATE_POSTGRES,
    CHECKOUT_UPDATE,
    ORDER_HISTORY,
    ConnectionInfo,
)

PLAN = json.loads((Path(__file__).parent / "data" / "pg_explain_audience.json").read_text())


def test_statement_text_from_pg_stat_statements_maps_to_the_driver_template():
    stored = ("UPDATE customers SET loyalty_points = loyalty_points + $1 WHERE tenant_id = $2 "
              "AND customer_id = $3")
    assert pg_collector.normalize_statement(stored) == pg_collector.normalize_statement(CHECKOUT_UPDATE)
    assert pg_collector.normalize_statement("SELECT tier FROM tenants WHERE tenant_id = 7") == \
        pg_collector.normalize_statement("SELECT tier FROM tenants WHERE tenant_id = %(tenant)s")
    assert pg_collector.normalize_statement(ORDER_HISTORY) != pg_collector.normalize_statement(CHECKOUT_UPDATE)


def test_lab_monitoring_statements_are_recognised():
    assert pg_collector.is_lab_overhead("SELECT count(*) FILTER (WHERE state = $1) FROM pg_stat_activity WHERE x")
    assert pg_collector.is_lab_overhead("SET lock_timeout = '5s'")
    assert pg_collector.is_lab_overhead("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT 1")
    assert not pg_collector.is_lab_overhead(ORDER_HISTORY)
    assert not pg_collector.is_lab_overhead(CHECKOUT_UPDATE)


def test_templates_use_the_postgres_batch_statement():
    templates = statement_templates()
    assert templates["LAB-BATCH"] == [BATCH_UPDATE_POSTGRES]
    assert " DIV " in BATCH_UPDATE and " DIV " not in BATCH_UPDATE_POSTGRES


def test_json_plan_is_parsed_into_the_shared_step_shape():
    steps = pg_collector.parse_plan(PLAN)
    assert steps[0]["depth"] == 0
    assert {"depth", "operation", "access", "table", "index", "estimated_rows", "actual_rows", "loops",
            "actual_time_ms", "q_error", "shared_blocks"} <= set(steps[0])
    assert any(s["table"] == "orders" and s["index"] == "idx_orders_tenant_day_customer" for s in steps)
    assert all(s["q_error"] >= 1 for s in steps if "q_error" in s)
    assert max(s["depth"] for s in steps) >= 1


def test_fingerprint_summary_groups_rows_and_reports_unmapped_time():
    rows = [
        {"fingerprint_id": "QF-OTHER", "calls": 100, "total_db_time_ms": 10.0, "rows": 100, "shared_blocks": 200,
         "shared_blocks_read": 0, "temp_blocks_written": 0},
        {"fingerprint_id": "QF-CHECKOUT-WRITE", "calls": 20, "total_db_time_ms": 30.0, "rows": 20, "shared_blocks": 100,
         "shared_blocks_read": 4, "temp_blocks_written": 0},
        {"fingerprint_id": "QF-CHECKOUT-WRITE", "calls": 20, "total_db_time_ms": 50.0, "rows": 20, "shared_blocks": 100,
         "shared_blocks_read": 0, "temp_blocks_written": 0},
        {"fingerprint_id": None, "calls": 5, "total_db_time_ms": 10.0, "rows": 5, "shared_blocks": 1,
         "shared_blocks_read": 0, "temp_blocks_written": 0},
        {"fingerprint_id": None, "lab_overhead": True, "calls": 80, "total_db_time_ms": 500.0, "rows": 80,
         "shared_blocks": 0, "shared_blocks_read": 0, "temp_blocks_written": 0},
    ]
    latency = {"QF-CHECKOUT-WRITE": {"p95_ms": 4.2, "by_tenant": {"alder": {"calls": 40}}}}
    fingerprints, total, unmapped = fingerprint_summary(rows, 10.0, latency)
    assert [f["fingerprint_id"] for f in fingerprints] == ["QF-CHECKOUT-WRITE", "QF-OTHER"]
    write = fingerprints[0]
    assert write["calls"] == 40 and write["avg_latency_ms"] == 2.0 and write["avg_shared_blocks"] == 5.0
    assert write["p95_latency_ms"] == 4.2 and write["shared_blocks_read_from_disk"] == 4
    assert total == 100.0 and unmapped == 10.0


def test_connection_info_speaks_each_engine():
    pg = ConnectionInfo("127.0.0.1", 5433, "postgres", "x", "capacitylab_sandbox_lab", engine="postgres")
    my = ConnectionInfo("127.0.0.1", 3307, "root", "x", "capacitylab_sandbox_lab")
    assert pg.batch_update == BATCH_UPDATE_POSTGRES and my.batch_update == BATCH_UPDATE

    class Cursor:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None):
            self.sql.append(sql)

    cur = Cursor()
    pg.set_lock_timeout(cur, 5)
    assert cur.sql == ["SET lock_timeout = '5s'"]


@pytest.mark.parametrize("host, database", [("db.example.internal", "capacitylab_sandbox_lab"),
                                            ("127.0.0.1", "postgres")])
def test_postgres_sandbox_refuses_unsafe_targets(host, database):
    with pytest.raises(UnsafeSandboxTarget):
        PostgresSandbox(host, 5432, "postgres", "x", database)


@pytest.mark.postgres
@pytest.mark.skipif(os.environ.get("CAPACITYLAB_TEST_POSTGRES") != "1",
                    reason="set CAPACITYLAB_TEST_POSTGRES=1 with the PostgreSQL sandbox up")
def test_postgres_lab_run_collects_real_evidence():
    from capacitylab.lab.pg_runner import run_lab_postgres
    from capacitylab.lab.runner import LabConfig
    from capacitylab.scenarios.loader import load_scenario
    from capacitylab.settings import Settings

    scenario, _ = load_scenario("campaign-overlap")
    items = run_lab_postgres(scenario, Settings.from_env().postgres, LabConfig(duration_s=4, total_qps=80, workers=8))
    by_id = {i.id: i for i in items}
    for phase in ("BASE", "EVENT", "EVENTIDX"):
        digest = by_id[f"EV-LAB-{phase}-DIG"].data
        assert {f["fingerprint_id"] for f in digest["fingerprints"]} >= {"QF-ORDER-HISTORY", "QF-OTHER"}
        assert digest["unmapped_db_time_pct"] < 20
    cmp = by_id["EV-LAB-CMP"].data
    assert cmp["engine"].startswith("PostgreSQL")
    aud = {s["index"] for s in by_id["EV-LAB-EVENTIDX-PLAN-AUD"].data["steps"]}
    assert "idx_orders_tenant_day_customer" in aud
