import pytest

from capacitylab.diagnostics import experiments as ex
from capacitylab.diagnostics import fixture_db as fx
from capacitylab.diagnostics.sandbox import MySQLSandbox, SQLiteSandbox, UnsafeSandboxTarget, _mysql_params


def test_fixture_is_deterministic(sandbox):
    other = SQLiteSandbox()
    try:
        stats = fx.build(other)
        assert stats == sandbox.stats
        checksum = "SELECT SUM(total_cents), COUNT(DISTINCT customer_id) FROM orders"
        assert other.fetchall(checksum) == sandbox.fetchall(checksum)
    finally:
        other.close()


def test_fixture_contains_required_edge_cases(sandbox):
    assert sandbox.fetchall("SELECT COUNT(*) FROM suppressions WHERE customer_id IS NULL AND tenant_id = 3")[0][0] > 0
    assert sandbox.fetchall("SELECT COUNT(DISTINCT tenant_id) FROM customers WHERE customer_id = 1")[0][0] == 5
    duplicates = sandbox.fetchall("SELECT COUNT(*) FROM (SELECT tenant_id, customer_id FROM suppressions "
                                  "WHERE customer_id IS NOT NULL GROUP BY tenant_id, customer_id HAVING COUNT(*) > 1) d")
    assert duplicates[0][0] > 0


def test_work_measurement_is_repeatable(sandbox):
    params = {"tenant": 1, "since_day": fx.RECENT_SINCE_DAY}
    first = sandbox.measure(fx.QUERIES["QF-AUDIENCE"], params)
    assert first == sandbox.measure(fx.QUERIES["QF-AUDIENCE"], params)
    assert first.work_units > 0 and first.rows_returned > 0


def test_index_experiment_measures_benefit_cost_and_cleans_up(sandbox):
    result = ex.index_experiment(sandbox, sorted(fx.INDEX_CANDIDATES), sorted(fx.QUERIES), 0.8, 7_200_000,
                                 sandbox.stats.orders)
    range_index = result["candidates"]["IDX-TENANT-DAY-CUSTOMER"]
    audience = range_index["measured"]["queries"]["QF-AUDIENCE"]
    assert audience["work_reduction_pct"] >= 50
    assert "idx_orders_tenant_day_customer" in audience["plan_after"]["indexes_used"]
    assert range_index["measured"]["index_bytes_local"] > 0
    assert range_index["measured"]["write_overhead_pct"] > 0
    assert range_index["modeled_production_translation"]["cpu_multiplier_by_fingerprint"]["QF-AUDIENCE"] < 1

    extended = result["candidates"]["IDX-TENANT-CUSTOMER-DAY"]
    assert extended["redundancy"]["makes_existing_index_redundant"] == ["idx_orders_tenant_customer"]
    history = extended["measured"]["queries"]["QF-ORDER-HISTORY"]
    assert history["plan_before"]["temporary_or_sort"] and not history["plan_after"]["temporary_or_sort"]

    plan = sandbox.plan(fx.QUERIES["QF-AUDIENCE"], {"tenant": 1, "since_day": 330})
    assert "idx_orders_tenant_day_customer" not in plan.indexes_used  # candidate indexes were dropped

    again = ex.index_experiment(sandbox, sorted(fx.INDEX_CANDIDATES), sorted(fx.QUERIES), 0.8, 7_200_000,
                                sandbox.stats.orders)
    assert again == result


def test_rewrite_equivalence_catches_duplicates_nulls_and_tenant_leaks(sandbox):
    result = ex.rewrite_equivalence(sandbox, sorted(fx.REWRITES))["rewrites"]
    assert result["RW-EXISTS"]["equivalent_on_fixtures"] and result["RW-EXISTS"]["failures"] == []

    join = result["RW-JOIN"]
    assert not join["equivalent_on_fixtures"]
    assert {f["difference"] for f in join["failures"]} == {"duplicate_rows_differ"}

    not_exists = result["RW-NOT-EXISTS"]
    assert {f["case"]["tenant"] for f in not_exists["failures"]} == {"cedar"}
    assert all(any("NULL" in h for h in f["hints"]) for f in not_exists["failures"])

    leak = result["RW-EXISTS-NO-TENANT"]
    assert any("tenant boundary" in h for f in leak["failures"] for h in f["hints"])
    assert {f["difference"] for f in leak["failures"]} == {"extra_rows"}


def test_explain_query_with_candidate(sandbox):
    base = ex.explain_query(sandbox, "QF-ORDER-HISTORY")
    with_index = ex.explain_query(sandbox, "QF-ORDER-HISTORY", "IDX-TENANT-CUSTOMER-DAY")
    assert base["plan"]["temporary_or_sort"] and not with_index["plan"]["temporary_or_sort"]
    assert with_index["rows_returned"] == base["rows_returned"]
    assert "not evidence of equivalent performance" in base["caveat"]


def test_unknown_candidates_and_rewrites_are_rejected(sandbox):
    with pytest.raises(ValueError):
        ex.index_experiment(sandbox, ["IDX-NOPE"], ["QF-AUDIENCE"], 0.8, None, 1)
    with pytest.raises(ValueError):
        ex.rewrite_equivalence(sandbox, ["RW-NOPE"])


def test_mysql_sandbox_refuses_non_local_or_non_sandbox_targets():
    with pytest.raises(UnsafeSandboxTarget):
        MySQLSandbox("db.internal.example", 3306, "u", "p", "capacitylab_sandbox")
    with pytest.raises(UnsafeSandboxTarget):
        MySQLSandbox("127.0.0.1", 3306, "u", "p", "production")


def test_mysql_parameter_conversion():
    assert _mysql_params("a = :tenant AND b >= :since_day") == "a = %(tenant)s AND b >= %(since_day)s"
