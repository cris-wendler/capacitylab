import json

import pytest

from capacitylab import cli
from capacitylab.evidence.models import EvidenceKind
from capacitylab.importers import (
    import_cloudwatch_json,
    import_digest_export,
    import_explain_analyze,
    import_slow_log,
)
from capacitylab.lab.explain import parse_explain_analyze, root_latency_ms
from capacitylab.scenarios.loader import load_scenario
from capacitylab.simulation.providers.base import compact
from capacitylab.simulation.roles import RoleId
from capacitylab.simulation.schema import ToolRequest
from capacitylab.simulation.tools import ToolEnvironment, execute_tool
from capacitylab.spend import SpendLedger

# Captured from the local MySQL 8.0 lab on the synthetic retail fixture.
PLAN = """-> Nested loop semijoin  (cost=1745 rows=1646) (actual time=0.118..8.71 rows=864 loops=1)
    -> Filter: ((c.marketing_opt_in = 1) and <in_optimizer>(c.customer_id,c.customer_id in (select #3) is false))  (cost=16.7 rows=160) (actual time=0.0947..0.689 rows=1026 loops=1)
        -> Index lookup on c using PRIMARY (tenant_id=1)  (cost=16.7 rows=1600) (actual time=0.0185..0.135 rows=1600 loops=1)
        -> Select #3 (subquery in condition; run only once)
            -> Filter: ((c.customer_id = `<materialized_subquery>`.customer_id))  (cost=27.1..27.1 rows=1) (actual time=369e-6..369e-6 rows=0.0647 loops=1098)
                -> Limit: 1 row(s)  (cost=27..27 rows=1) (actual time=304e-6..304e-6 rows=0.0647 loops=1098)
                    -> Index lookup on <materialized_subquery> using <auto_distinct_key> (customer_id=c.customer_id)  (actual time=239e-6..239e-6 rows=0.0647 loops=1098)
                        -> Materialize with deduplication  (cost=27..27 rows=131) (actual time=0.0712..0.0712 rows=106 loops=1)
                            -> Index lookup on s using idx_suppressions_tenant (tenant_id=1)  (cost=13.9 rows=131) (actual time=0.0484..0.0527 rows=131 loops=1)
    -> Filter: (o.created_day >= 330)  (cost=79.4 rows=10.3) (actual time=0.00775..0.00775 rows=0.842 loops=1026)
        -> Index lookup on o using idx_orders_tenant_customer (tenant_id=1, customer_id=c.customer_id)  (cost=79.4 rows=30.9) (actual time=0.00713..0.00748 rows=9.04 loops=1026)
"""

def test_spread_reports_median_and_range_across_passes():
    from capacitylab.lab.runner import LabConfig, PhaseSpec, _spread, spread_item

    assert _spread([None, None])["median"] is None
    one = _spread([10.0, 14.0, 12.0])
    assert (one["median"], one["min"], one["max"], one["range_pct"]) == (12.0, 10.0, 14.0, 33.3)

    phases = [PhaseSpec("EVENT", "event mix with batch job", 5.0, True),
              PhaseSpec("EVENTNB", "batch job moved", 5.0, False)]
    comparisons = [
        {"engine": "MySQL 8.0.46", "scenario_id": "campaign-overlap",
         "by_fingerprint": {"QF-CHECKOUT-WRITE": {"EVENT": {"p95_ms": 14.3}, "EVENTNB": {"p95_ms": 5.6}}},
         "lock_waits": {"EVENT": 83, "EVENTNB": 0}, "deadlocks": {"EVENT": 0, "EVENTNB": 0},
         "db_load_average_active_sessions": {"EVENT": 0.24, "EVENTNB": 0.05}},
        {"engine": "MySQL 8.0.46", "scenario_id": "campaign-overlap",
         "by_fingerprint": {"QF-CHECKOUT-WRITE": {"EVENT": {"p95_ms": 27.7}, "EVENTNB": {"p95_ms": 6.2}}},
         "lock_waits": {"EVENT": 61, "EVENTNB": 0}, "deadlocks": {"EVENT": 1, "EVENTNB": 0},
         "db_load_average_active_sessions": {"EVENT": 0.31, "EVENTNB": 0.04}},
    ]
    item = spread_item(comparisons, phases, LabConfig(repeats=2))
    assert item.id == "EV-LAB-SPREAD" and item.environment == "lab" and item.data["passes"] == 2
    checkout = item.data["p95_ms"]["QF-CHECKOUT-WRITE"]
    assert checkout["EVENT"]["values"] == [14.3, 27.7] and checkout["EVENT"]["median"] == 21.0
    assert checkout["EVENT"]["range_pct"] == 63.8, "the range is reported next to the median, not hidden"
    assert item.data["lock_waits"]["EVENTNB"]["max"] == 0 and item.data["deadlocks"]["EVENT"]["max"] == 1


SLOW_LOG = """# Time: 2026-10-08T19:01:02Z
# User@Host: app[app] @  [198.51.100.7]  Id: 42
# Query_time: 6.1  Lock_time: 0.0 Rows_sent: 3  Rows_examined: 900000
SELECT * FROM orders WHERE tenant_id = 1 AND note = 'someone@example.com';
"""


def test_explain_analyze_parser():
    steps = parse_explain_analyze(PLAN)
    assert len(steps) == 11
    assert steps[0]["estimated_rows"] == 1646 and steps[0]["actual_rows"] == 864 and steps[0]["loops"] == 1
    orders = steps[-1]
    assert (orders["table"], orders["index"], orders["loops"]) == ("o", "idx_orders_tenant_customer", 1026)
    assert orders["estimated_rows"] == 30.9 and orders["actual_rows"] == 9.04
    sub = next(s for s in steps if s["index"] == "<auto_distinct_key>")
    assert sub["estimated_rows"] is None and sub["actual_rows"] == 0.0647
    assert steps[3]["estimated_rows"] is None and steps[3]["operation"].startswith("Select #3")
    assert root_latency_ms(steps) == 8.71


def test_digest_export_csv_and_json(tmp_path):
    header = "SCHEMA_NAME,DIGEST,DIGEST_TEXT,COUNT_STAR,SUM_TIMER_WAIT,SUM_LOCK_TIME,SUM_ROWS_EXAMINED,SUM_ROWS_SENT,SUM_CREATED_TMP_DISK_TABLES,SUM_NO_INDEX_USED,QUANTILE_95"
    rows = [
        "shop,abc123def4567890,SELECT * FROM t WHERE e = 'a@example.org',4,2000000000,0,400,4,0,4,1000000000",
        "shop,ffff0000aaaa1111,UPDATE t SET x = ? WHERE id = ?,10,6000000000,500000000,10,0,0,0,900000000",
    ]
    csv_path = tmp_path / "digests.csv"
    csv_path.write_text("\n".join([header, *rows]) + "\n")
    item = import_digest_export(csv_path, window_seconds=10)
    first, second = sorted(item.data["fingerprints"], key=lambda f: f["calls"])
    # performance_schema timers are picoseconds: 2e9 ps = 2 ms over 4 calls.
    assert first["avg_latency_ms"] == 0.5 and first["p95_latency_ms"] == 1.0 and first["calls_per_s"] == 0.4
    assert "<email>" in first["normalized"] and "example.org" not in first["normalized"]
    assert second["avg_lock_wait_ms"] == 0.05
    assert sum(f["share_of_db_time_pct"] for f in item.data["fingerprints"]) == pytest.approx(100)
    assert item.environment == "import" and item.synthetic is False and "imported" in item.label

    json_path = tmp_path / "digests.json"
    json_path.write_text(json.dumps([dict(zip(header.lower().split(","), r.split(","), strict=True)) for r in rows]))
    assert len(import_digest_export(json_path).data["fingerprints"]) == 2

    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n")
    with pytest.raises(ValueError):
        import_digest_export(bad)


def test_slowlog_plan_and_metrics_importers(tmp_path):
    log = tmp_path / "slow.log"
    log.write_text(SLOW_LOG)
    item = import_slow_log(log)
    assert item.kind == EvidenceKind.QUERY_DIGEST and "someone@example.com" not in item.canonical_json()
    empty = tmp_path / "empty.log"
    empty.write_text("")
    with pytest.raises(ValueError):
        import_slow_log(empty)

    plan = tmp_path / "audience-plan.txt"
    plan.write_text(PLAN)
    plan_item = import_explain_analyze(plan, "QF-AUDIENCE")
    assert plan_item.data["steps"][0]["actual_rows"] == 864

    metrics = tmp_path / "cpu.json"
    metrics.write_text(json.dumps({"Label": "CPUUtilization", "Datapoints": [
        {"Timestamp": "2026-01-01T00:00:00Z", "Average": 40.0, "Maximum": 70.0, "Minimum": 10.0, "Unit": "Percent"},
        {"Timestamp": "2026-01-01T00:01:00Z", "Average": 45.0, "Maximum": 50.0, "Minimum": 20.0, "Unit": "Percent"}]}))
    metric_item = import_cloudwatch_json(metrics)
    assert metric_item.data["max_of_period_maxima"] == 70.0 and metric_item.data["unit"] == "Percent"


def test_cli_import_then_run_with_extra_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPACITYLAB_RUNS_DIR", str(tmp_path / "runs"))
    plan = tmp_path / "audience-plan.txt"
    plan.write_text(PLAN)
    evidence = tmp_path / "evidence.yaml"
    assert cli.main(["import", "plan", str(plan), "--fingerprint", "QF-AUDIENCE", "--out", str(evidence)]) == 0
    assert cli.main(["import", "plan", str(plan), "--fingerprint", "QF-AUDIENCE", "--out", str(evidence)]) == 1
    assert cli.main(["validate", "campaign-overlap", "--evidence", str(evidence)]) == 0
    _, bundle = load_scenario("campaign-overlap", [evidence])
    assert "EV-IMP-PLAN-AUDIENCE-PLAN" in bundle
    ledger = tmp_path / "run.json"
    assert cli.main(["run", "campaign-overlap", "--provider", "mock", "--evidence", str(evidence), "--out", str(ledger),
                     "--quiet"]) == 0
    assert cli.main(["replay", str(ledger)]) == 0


def test_extra_evidence_id_collision_is_rejected(tmp_path, campaign):
    from capacitylab.lab.runner import write_evidence

    path = write_evidence([campaign[1].get("EV-SLO-001")], tmp_path / "dup.yaml")
    with pytest.raises(ValueError, match="already exists"):
        load_scenario("campaign-overlap", [path])


def test_spend_ledger_and_cli_budget_refusal(tmp_path, monkeypatch):
    ledger = SpendLedger(tmp_path / "spend-ledger.json")
    ledger.record("r1", "anthropic", "claude-opus-5", 1.25, "concluded")
    ledger.record("r2", "mock", "mock", 0.0, "concluded")
    assert ledger.spent_usd() == 1.25 and ledger.remaining_usd(3.0) == 1.75 and ledger.remaining_usd(1.0) == 0.0

    monkeypatch.setenv("CAPACITYLAB_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("CAPACITYLAB_MAX_USD_TOTAL", "1.0")
    assert cli.main(["run", "campaign-overlap", "--provider", "anthropic"]) == 3  # refused before any API call
    assert cli.main(["spend"]) == 0


def test_compact_pack_drops_bulk_and_rounds():
    data = {"utilization_by_slot": [1] * 48, "slots": list(range(30)), "x": 1.234567, "nested": [{"raw_text": "long"}]}
    small = compact(data)
    assert "utilization_by_slot" not in small and small["x"] == 1.2346 and small["nested"] == [{}]
    assert len(small["slots"]) == 13 and "18 more items omitted" in small["slots"][-1]


def test_replay_reuses_recorded_non_deterministic_results(campaign_run):
    from capacitylab.evaluation.replay import replay

    run = campaign_run.model_copy(deep=True)
    target = next(r for r in run.tool_calls if r.tool == "table_growth_review")
    target.tool = "lab_load_test"  # pretend a lab measurement produced this evidence
    report = replay(run)
    assert any(s["call_id"] == target.call_id and "non-deterministic" in s["reason"] for s in report.skipped)
    assert report.mismatches == [] and report.decision_match


def test_lab_tool_requires_mysql(campaign):
    scenario, bundle = campaign
    env = ToolEnvironment(scenario, bundle.copy())
    record, item = execute_tool(env, "TC-001", 1, [RoleId.RELIABILITY_ENGINEER],
                                ToolRequest(tool="lab_load_test", arguments_json='{"batch_in_test": false}', purpose="x"))
    assert record.status == "error" and "MySQL lab" in record.error and item is None


def test_stakeholders_cite_lab_evidence_when_present(campaign):
    from capacitylab.evidence.models import EvidenceItem, Provenance
    from capacitylab.simulation.orchestrator import Orchestrator
    from capacitylab.simulation.providers.mock import MockProvider
    from capacitylab.simulation.run import RunConfig

    scenario, bundle = campaign
    lab = EvidenceItem(
        id="EV-LAB-CMP", kind=EvidenceKind.EXPERIMENT_RESULT, title="Lab phase comparison", provenance=Provenance.MEASURED,
        environment="lab", source="lab:runner",
        data={"by_fingerprint": {"QF-AUDIENCE": {"EVENT": {"p95_ms": 13.373, "calls": 4, "low_sample": True},
                                                 "EVENTIDX": {"p95_ms": 7.973, "calls": 3, "low_sample": True}},
                                 "QF-CHECKOUT-WRITE": {"EVENT": {"p95_ms": 38.068}, "EVENTIDX": {"p95_ms": 51.987},
                                                       "EVENTNB": {"p95_ms": 9.114}}},
              "lock_waits": {"BASE": 0, "EVENT": 11, "EVENTIDX": 7, "EVENTNB": 0},
              "avg_row_lock_wait_ms": {"BASE": 0.0, "EVENT": 134.09, "EVENTIDX": 147.14, "EVENTNB": 0.0}})
    merged = bundle.copy()
    merged.add(lab)
    run = Orchestrator(scenario, merged, MockProvider(), RunConfig(max_rounds=1)).run()
    claims = {t.role: [c for c in t.draft.claims if "EV-LAB-CMP" in c.evidence_ids] for t in run.turns}
    assert claims["database_engineer"] and "low sample" in claims["database_engineer"][0].statement
    assert any("rose from 38.068 ms to 51.987 ms" in c.statement for c in claims["database_engineer"]), \
        "a measured write regression must be reported, not hidden"
    assert claims["reliability_engineer"] and "134.09 ms" in claims["reliability_engineer"][0].statement
    assert any("9.114 ms" in c.statement for c in claims["reliability_engineer"])
    assert not claims["tenant_representative"]
    assert not [f for t in run.turns for f in t.findings if f.severity == "error"]
