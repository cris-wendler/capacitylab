# SPDX-License-Identifier: AGPL-3.0-or-later
"""Percona Toolkit parsing and command construction, using real output captured from the local lab."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from capacitylab.diagnostics.sandbox import UnsafeSandboxTarget
from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.importers import import_pt_deadlocks, import_pt_duplicate_keys, import_pt_query_digest
from capacitylab.lab.percona import (
    PerconaToolkit,
    PerconaUnavailable,
    lab_fingerprint_map,
    parse_deadlocks,
    parse_duplicate_keys,
    parse_variable_advisor,
    summarize_query_digest,
)
from capacitylab.settings import MySQLSettings
from capacitylab.simulation.roles import RoleId
from capacitylab.simulation.schema import ToolRequest
from capacitylab.simulation.tools import ToolEnvironment, execute_tool

DATA = Path(__file__).parent / "data" / "percona"
LOCAL = MySQLSettings("127.0.0.1", 3307, "root", "change-me-local-only", "capacitylab_sandbox")


def test_duplicate_key_report():
    report = parse_duplicate_keys((DATA / "pt-duplicate-key-checker.txt").read_text())
    assert report["total_indexes"] == 8 and report["duplicate_indexes"] == 1 and report["duplicate_index_bytes"] == 1009992
    (finding,) = report["findings"]
    assert finding["table"] == "capacitylab_sandbox_lab.orders"
    assert (finding["redundant_index"], finding["relation"], finding["covered_by"]) == (
        "idx_orders_tenant_customer", "left-prefix", "idx_orders_tenant_customer_day")
    assert finding["drop_statement"].startswith("ALTER TABLE `capacitylab_sandbox_lab`.`orders` DROP INDEX")


def test_variable_advisor():
    advice = parse_variable_advisor((DATA / "pt-variable-advisor.txt").read_text())
    assert len(advice) == 9 and {a["level"] for a in advice} == {"WARN", "NOTE"}
    assert any(a["variable"] == "innodb_buffer_pool_size" for a in advice)


def test_deadlock_report_maps_statements_and_filters_by_time():
    tsv = (DATA / "pt-deadlock-logger.tsv").read_text()
    report = parse_deadlocks(tsv, fingerprint_map=lab_fingerprint_map())
    (deadlock,) = report["deadlocks"]
    assert len(deadlock["transactions"]) == 2
    victim = next(tx for tx in deadlock["transactions"] if tx["victim"])
    assert victim["statement_id"] == "QF-CHECKOUT-WRITE" and victim["table"] == "customers"
    assert {tx["statement_id"] for tx in deadlock["transactions"]} == {"QF-CHECKOUT-WRITE", "LAB-BATCH"}
    assert "192.0.2" not in json.dumps(report), "client addresses are not kept"
    assert parse_deadlocks(tsv, since="2030-01-01T00:00:00")["deadlocks"] == []
    assert parse_deadlocks("not a report")["deadlocks"] == []


def test_query_digest_summary_maps_lab_statements():
    summary = summarize_query_digest(json.loads((DATA / "pt-query-digest.json").read_text()), lab_fingerprint_map())
    by_id = {c["fingerprint_id"]: c for c in summary["classes"]}
    audience = by_id["QF-AUDIENCE"]
    assert audience["calls"] == 3 and audience["p95_latency_ms"] == 15.685 and audience["avg_rows_examined"] == 11021
    assert set(audience["tables"]) >= {"customers", "orders", "suppressions"}
    assert any(fid.startswith("PT-") for fid in by_id), "statements outside the lab mix keep the checksum id"
    assert sum(c["share_of_query_time_pct"] for c in summary["classes"]) == pytest.approx(100, abs=0.2)
    assert summary["statements"] == 13


def test_toolkit_command_is_confined_to_the_lab_container():
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=(DATA / "pt-duplicate-key-checker.txt").read_text(), stderr="")

    toolkit = PerconaToolkit(LOCAL, image="percona/percona-toolkit:test", runner=runner)
    toolkit.duplicate_keys("capacitylab_sandbox_lab")
    cmd = calls[0]
    assert cmd[:4] == ["docker", "run", "--rm", "--network=container:capacitylab-sandbox-mysql"]
    assert "percona/percona-toolkit:test" in cmd and "pt-duplicate-key-checker" in cmd
    assert any(arg.startswith("h=127.0.0.1,P=3306,") for arg in cmd)

    toolkit.read_lab_file("/tmp/capacitylab-20260101T000000-EVENT.log")
    assert calls[-1] == ["docker", "exec", "capacitylab-sandbox-mysql", "cat", "/tmp/capacitylab-20260101T000000-EVENT.log"]
    for bad in ("/etc/passwd", "/tmp/capacitylab-../../etc/x.log", "/tmp/other.log"):
        with pytest.raises(UnsafeSandboxTarget):
            toolkit.read_lab_file(bad)

    def digest_runner(cmd, **kwargs):
        calls.append((cmd, kwargs.get("input")))
        return SimpleNamespace(returncode=0, stdout="Reading from STDIN ...\n\n" + (DATA / "pt-query-digest.json").read_text(),
                               stderr="")

    PerconaToolkit(LOCAL, runner=digest_runner).query_digest("# Time: x\nSELECT 1;\n")
    cmd, stdin = calls[-1]
    assert "-i" in cmd and cmd[-1] == "-" and stdin.startswith("# Time")

    with pytest.raises(UnsafeSandboxTarget):
        PerconaToolkit(MySQLSettings("db.example.invalid", 3306, "u", "p", "x"), runner=runner)
    with pytest.raises(UnsafeSandboxTarget):
        PerconaToolkit(LOCAL, container="some-production-db", runner=runner)


def test_toolkit_failures_hide_the_password():
    def failing(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="Access denied using p=change-me-local-only")

    with pytest.raises(PerconaUnavailable) as err:
        PerconaToolkit(LOCAL, runner=failing).variable_advisor()
    assert "change-me-local-only" not in str(err.value) and "***" in str(err.value)


def test_percona_importers():
    digest = import_pt_query_digest(DATA / "pt-query-digest.json")
    assert digest.kind == EvidenceKind.QUERY_DIGEST and digest.environment == "import" and digest.data["classes"]
    duplicates = import_pt_duplicate_keys(DATA / "pt-duplicate-key-checker.txt")
    assert duplicates.kind == EvidenceKind.SCHEMA and duplicates.data["duplicate_indexes"] == 1
    deadlocks = import_pt_deadlocks(DATA / "pt-deadlock-logger.tsv")
    assert deadlocks.kind == EvidenceKind.METRIC_SUMMARY and len(deadlocks.data["deadlocks"]) == 1
    with pytest.raises(ValueError):
        import_pt_deadlocks(DATA / "pt-variable-advisor.txt")


def test_percona_tool_requires_the_lab(campaign):
    scenario, bundle = campaign
    env = ToolEnvironment(scenario, bundle.copy())
    record, item = execute_tool(env, "TC-001", 1, [RoleId.DATABASE_ENGINEER], ToolRequest(
        tool="percona_duplicate_keys", arguments_json='{"index_candidate": "IDX-TENANT-CUSTOMER-DAY"}', purpose="x"))
    assert record.status == "error" and "MySQL lab" in record.error and item is None
    record, _ = execute_tool(env, "TC-002", 1, [RoleId.FINOPS_ANALYST], ToolRequest(
        tool="percona_duplicate_keys", arguments_json='{"index_candidate": "IDX-TENANT-CUSTOMER-DAY"}', purpose="x"))
    assert record.status == "denied"


def test_roles_cite_percona_findings(campaign):
    from capacitylab.simulation.orchestrator import Orchestrator
    from capacitylab.simulation.providers.mock import MockProvider
    from capacitylab.simulation.run import RunConfig

    scenario, bundle = campaign
    merged = bundle.copy()
    merged.add(EvidenceItem(
        id="EV-LAB-EVENTIDX-PTDK", kind=EvidenceKind.SCHEMA, title="dup keys", provenance=Provenance.OBSERVED, environment="lab",
        source="percona:pt-duplicate-key-checker", data={"index_candidate": "IDX-TENANT-CUSTOMER-DAY",
                                                         **parse_duplicate_keys((DATA / "pt-duplicate-key-checker.txt").read_text())}))
    merged.add(EvidenceItem(
        id="EV-LAB-PTDL", kind=EvidenceKind.METRIC_SUMMARY, title="deadlocks", provenance=Provenance.OBSERVED, environment="lab",
        source="percona:pt-deadlock-logger",
        data=parse_deadlocks((DATA / "pt-deadlock-logger.tsv").read_text(), fingerprint_map=lab_fingerprint_map())))
    run = Orchestrator(scenario, merged, MockProvider(), RunConfig(max_rounds=1)).run()
    claims = {t.role: [c.statement for c in t.draft.claims] for t in run.turns}
    assert any("idx_orders_tenant_customer" in s and "1009992 bytes" in s for s in claims["database_engineer"])
    assert any("pt-deadlock-logger" in s and "QF-CHECKOUT-WRITE" in s for s in claims["reliability_engineer"])
    assert not [f for t in run.turns for f in t.findings if f.severity == "error"]
