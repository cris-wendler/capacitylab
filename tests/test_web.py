import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.lab.runner import write_evidence
from capacitylab.settings import MySQLSettings, PostgresSettings, Settings
from capacitylab.web.app import create_app

LAB_ITEM = EvidenceItem(
    id="EV-LAB-CMP", kind=EvidenceKind.EXPERIMENT_RESULT, title="Lab phase comparison", provenance=Provenance.MEASURED,
    environment="lab", source="lab:runner",
    data={"scenario_id": "campaign-overlap", "engine": "MySQL 8.0.46",
          "config": {"duration_s": 60, "total_qps": 300, "workers": 16, "seed": "test"},
          "phases": [{"name": "EVENT", "label": "event mix with batch job", "index_candidate": None},
                     {"name": "EVENTNB", "label": "event mix with the batch job moved", "index_candidate": None}],
          "by_fingerprint": {"QF-CHECKOUT-WRITE": {"EVENT": {"p95_ms": 38.068, "calls": 1564, "errors": 0, "low_sample": False},
                                                   "EVENTNB": {"p95_ms": 9.114, "calls": 1570, "errors": 0, "low_sample": False}}},
          "lock_waits": {"EVENT": 80, "EVENTNB": 0}, "avg_row_lock_wait_ms": {"EVENT": 137.85, "EVENTNB": 0.0},
          "db_load_average_active_sessions": {"EVENT": 0.335, "EVENTNB": 0.2},
          "threads_running_max": {"EVENT": 4, "EVENTNB": 3}, "low_sample_note": "p95 values from few calls are unreliable.",
          "deadlocks": {"EVENT": 1, "EVENTNB": 0}, "percona_toolkit": "pt-query-digest 3.7.1-4"})

PERCONA_ITEMS = [
    EvidenceItem(id="EV-LAB-EVENT-PTQD", kind=EvidenceKind.QUERY_DIGEST, title="pt-query-digest", provenance=Provenance.OBSERVED,
                 environment="lab", source="percona:pt-query-digest",
                 data={"phase": "EVENT", "statements": 12019, "unique_statements": 13, "classes": [
                     {"fingerprint_id": "LAB-BATCH", "fingerprint": "update customers c set loyalty_points = (select ...)",
                      "calls": 116, "p95_latency_ms": 4.411, "share_of_query_time_pct": 9.6, "avg_rows_examined": 5801.0,
                      "total_lock_time_s": 0.2}]}),
    EvidenceItem(id="EV-LAB-PTDL", kind=EvidenceKind.METRIC_SUMMARY, title="deadlocks", provenance=Provenance.OBSERVED,
                 environment="lab", source="percona:pt-deadlock-logger",
                 data={"note": "InnoDB keeps only the most recent deadlock.", "deadlocks": [{"ts": "2026-09-15T15:02:11", "transactions": [
                     {"statement_id": "QF-CHECKOUT-WRITE", "table": "customers", "index": "PRIMARY", "lock_type": "RECORD",
                      "lock_mode": "X", "waiting": True, "victim": True}]}]}),
    EvidenceItem(id="EV-LAB-PTVA", kind=EvidenceKind.TOPOLOGY, title="advisor", provenance=Provenance.OBSERVED,
                 environment="lab", source="percona:pt-variable-advisor",
                 data={"server": {"version": "8.0.46 MySQL Community Server - GPL"}, "note": "Describes the lab, not production.",
                       "advice": [{"level": "WARN", "variable": "innodb_buffer_pool_size", "message": "The InnoDB buffer pool size is unconfigured."}]}),
]

PG_ITEMS = [
    EvidenceItem(id="EV-LAB-CMP", kind=EvidenceKind.EXPERIMENT_RESULT, title="Lab phase comparison", provenance=Provenance.MEASURED,
                 environment="lab", source="lab:runner",
                 data={**LAB_ITEM.data, "engine": "PostgreSQL 17.11", "percona_toolkit": None,
                       "avg_row_lock_wait_ms": {"EVENT": None, "EVENTNB": None},
                       "lock_wait_note": "PostgreSQL keeps no cumulative lock-wait counter."}),
    EvidenceItem(id="EV-LAB-EVENT-LAT", kind=EvidenceKind.METRIC_SUMMARY, title="latency", provenance=Provenance.OBSERVED,
                 environment="lab", source="lab:client+pg_stat_database",
                 data={"phase": "EVENT", "client_latency": {}, "active_sessions": {"max": 5, "mean": 1.2},
                       "batch_job": {"running": True, "chunks_committed": 30, "errors": 0},
                       "server_counters": {"transactions_per_s": 210.5, "lock_wait_session_seconds": 3.4,
                                           "max_sessions_waiting_on_locks": 4, "lock_timeouts": 0, "deadlocks": 0,
                                           "blks_hit": 91000, "blks_read": 12, "temp_files": 0}}),
    EvidenceItem(id="EV-LAB-EVENT-DIG", kind=EvidenceKind.QUERY_DIGEST, title="statements", provenance=Provenance.OBSERVED,
                 environment="lab", source="lab:pg_stat_statements",
                 data={"phase": "EVENT", "fingerprints": [{"fingerprint_id": "QF-AUDIENCE", "calls": 9, "share_of_db_time_pct": 4.1,
                                                          "avg_latency_ms": 2.6, "avg_shared_blocks": 3192.0,
                                                          "shared_blocks_read_from_disk": 0}]}),
    EvidenceItem(id="EV-LAB-EVENT-PLAN-AUD", kind=EvidenceKind.QUERY_PLAN, title="plan", provenance=Provenance.OBSERVED,
                 environment="lab", source="lab:EXPLAIN",
                 data={"fingerprint_id": "QF-AUDIENCE", "total_latency_ms": 2.6, "steps": [
                     {"depth": 0, "operation": "Nested Loop Semi", "table": None, "index": None, "estimated_rows": 10.0,
                      "actual_rows": 12.0, "loops": 1, "actual_time_ms": 2.5, "shared_blocks": 3194},
                     {"depth": 1, "operation": "Index Scan", "table": "orders", "index": "idx_orders_tenant_customer",
                      "estimated_rows": 3.0, "actual_rows": 1.0, "loops": 800, "actual_time_ms": 0.01, "shared_blocks": 3146}]}),
]


@pytest.fixture(scope="module")
def runs_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("runs")
    write_evidence([LAB_ITEM, *PERCONA_ITEMS], path / "lab" / "campaign-overlap-test.yaml")
    write_evidence(PG_ITEMS, path / "lab" / "campaign-overlap-pg.yaml")
    (path / "imports").mkdir()
    # A real import from the Floci emulator, captured in CI.
    shutil.copy(Path(__file__).parent / "data" / "aws" / "floci-import.yaml", path / "imports" / "aws-floci-demo-test.yaml")
    from capacitylab.azure_import import AzureTarget, import_azure
    from capacitylab.gcp_import import GcpTarget, import_gcp
    from tests.test_gcp_azure_import import (
        GROUP,
        NOW,
        PROJECT,
        RETAIL_PAGE,
        SERVER,
        SUBSCRIPTION,
        WRITER,
        Recorded,
        azure_routes,
        gcp_routes,
    )

    gcp = import_gcp(GcpTarget(project=PROJECT), WRITER, hours=2, now=NOW, http=Recorded(gcp_routes()), label="gcp test")
    write_evidence(gcp.items, path / "imports" / "gcp-test.yaml")
    azure = import_azure(AzureTarget(subscription=SUBSCRIPTION, resource_group=GROUP, live=True, endpoint_url=None), SERVER,
                         hours=2, now=NOW, http=Recorded(azure_routes()),
                         prices_http=Recorded([("GET", "prices.azure.com", RETAIL_PAGE)]), label="azure test")
    write_evidence(azure.items, path / "imports" / "azure-test.yaml")
    return path


@pytest.fixture(scope="module")
def client(runs_dir):
    # Non-local hosts keep the lab status checks offline and exercise the refusal path.
    settings = Settings(runs_dir=runs_dir, mysql=MySQLSettings("db.example.invalid", 3306, "u", "p", "capacitylab_sandbox"),
                        postgres=PostgresSettings(host="db.example.invalid"),
                        aws_endpoint="https://rds.example.invalid", gcp_endpoint="https://sqladmin.example.invalid",
                        azure_endpoint="https://management.example.invalid")
    with TestClient(create_app(settings, inline_jobs=True)) as c:
        yield c


def test_home_and_scenario_pages(client):
    home = client.get("/")
    assert home.status_code == 200 and "Flash sale overlapping evening traffic" in home.text and "Decision record" in home.text
    assert "Model budget" in home.text and "Local database lab" in home.text
    page = client.get("/scenarios/campaign-overlap")
    assert page.status_code == 200
    assert "EV-DIG-001" in page.text and "<svg class=\"viz\"" in page.text and "GAP-FAILOVER-DURATION" in page.text
    assert "lab/campaign-overlap-test.yaml" in page.text, "attachable evidence files are listed"
    assert "4 items · local lab" in page.text and "built-in method" not in page.text
    assert "Refusing non-local host" in page.text
    assert client.get("/scenarios/nope").status_code == 404


def test_run_flow_with_attached_lab_evidence(client):
    started = client.post("/scenarios/campaign-overlap/run",
                          data={"provider": "mock", "max_rounds": 4, "sandbox": "sqlite", "evidence": "lab/campaign-overlap-test.yaml"})
    assert started.status_code == 200 and "Scripted run" in started.text
    assert "Where each agent landed" in started.text and "Proposed database changes" in started.text
    assert 'id="player-data"' in started.text and "Watch the review" in started.text and 'class="filter-chip' in started.text
    assert started.text.count('class="agent-tile') == 5 and 'class="avatar role-database_engineer' in started.text
    assert "Lab measurements used" in started.text and "137.85" in started.text
    run_path = started.url.path
    evidence_id = re.search(r'/evidence/(EV-TOOL-\d+)"', started.text).group(1)
    assert "Produced by" in client.get(f"{run_path}/evidence/{evidence_id}").text
    assert client.get(f"{run_path}/evidence/EV-LAB-CMP").status_code == 200
    assert client.get(f"{run_path}/evidence/EV-NOPE-001").status_code == 404
    replay = client.get(f"{run_path}/replay")
    assert replay.status_code == 200 and "Verified: this run reproduces" in replay.text
    assert "Not reproduced" not in replay.text and "scenario has changed" not in replay.text
    assert client.get(f"{run_path}/report.md").status_code == 200
    assert client.get(f"{run_path}/ledger.json").json()["extra_evidence_items"][0]["id"] == "EV-LAB-CMP"
    assert run_path.split("/")[-1] in client.get("/runs").text


def test_rejects_bad_parameters_and_path_traversal(client):
    assert client.post("/scenarios/campaign-overlap/run", data={"provider": "other"}).status_code == 400
    assert client.post("/scenarios/campaign-overlap/run", data={"provider": "mock", "evidence": "../../etc/passwd"}).status_code == 400
    assert client.get("/runs/..%2F..%2Fsecrets").status_code == 404
    assert client.get("/lab/..%2Fsecrets.yaml").status_code == 404


def test_lab_pages(client):
    lab = client.get("/lab")
    assert lab.status_code == 200 and "campaign-overlap-test.yaml" in lab.text and "Refusing non-local host" in lab.text
    detail = client.get("/lab/campaign-overlap-test.yaml")
    assert detail.status_code == 200 and "QF-CHECKOUT-WRITE" in detail.text and "9.114" in detail.text
    assert "Use in a review" in detail.text and "Lab run for campaign-overlap" in detail.text
    assert "Slow log digest" in detail.text and "LAB-BATCH" in detail.text and "5801.0" in detail.text
    assert "Percona Toolkit checks" in detail.text and "innodb_buffer_pool_size" in detail.text
    assert "2026-09-15T15:02:11" in detail.text and "Run Percona Toolkit too" in lab.text
    assert client.post("/lab/run", data={"scenario_id": "campaign-overlap", "duration": 1}).status_code == 400
    assert client.post("/lab/run", data={"scenario_id": "campaign-overlap", "duration": 10}).status_code == 400  # lab not reachable
    assert "PostgreSQL 17" in lab.text


def test_postgres_lab_page(client):
    detail = client.get("/lab/campaign-overlap-pg.yaml")
    assert detail.status_code == 200 and "PostgreSQL 17.11" in detail.text
    assert "pg_stat_statements" in detail.text and "3192.0" in detail.text and "Buffer blocks / call" in detail.text
    assert "3.4 session-seconds" in detail.text and "Sessions seen waiting on locks" in detail.text
    assert "using idx_orders_tenant_customer" in detail.text and "x 800 loops" in detail.text
    assert "Average lock wait" not in detail.text and "Row-lock waits" not in detail.text
    started = client.post("/lab/run", data={"scenario_id": "campaign-overlap", "engine": "postgres", "percona": "true"})
    assert started.status_code == 400
    assert client.post("/lab/run", data={"scenario_id": "campaign-overlap", "engine": "oracle"}).status_code == 400


def test_comparison_page(client):
    page = client.get("/scenarios/downsize-reader/evaluate")
    assert page.status_code == 200 and "Simple rules" in page.text and "Single reviewer" in page.text


def test_attached_aws_prices_are_named_on_the_run_page(client, runs_dir):
    from tests.test_rate_card_choice import FAMILY, aws_rate

    write_evidence([aws_rate(FAMILY)], runs_dir / "imports" / "aws-prices-test.yaml")
    started = client.post("/scenarios/campaign-overlap/run",
                          data={"provider": "mock", "max_rounds": 2, "sandbox": "sqlite",
                                "evidence": "imports/aws-prices-test.yaml"})
    assert started.status_code == 200
    assert "Instance prices from EV-AWS-RATE-TEST" in started.text and "storage rate from EV-RATE-001" in started.text
    scenario = client.get("/scenarios/campaign-overlap")
    assert "Prices from EV-RATE-001." in scenario.text


def test_aws_pages(client):
    page = client.get("/aws")
    assert page.status_code == 200 and "Import from AWS" in page.text
    assert "refusing endpoint" in page.text, "a non-local endpoint is refused without CAPACITYLAB_AWS_LIVE"
    assert "aws-floci-demo-test.yaml" in page.text and "emulator" in page.text and "built-in method" not in page.text
    detail = client.get("/aws/aws-floci-demo-test.yaml")
    assert detail.status_code == 200 and "AWS import · floci demo" in detail.text
    assert "db.r6g.2xlarge" in detail.text and "No readers" in detail.text and "local emulator (Floci)" in detail.text
    assert detail.text.count('<svg class="viz"') == 2 and "peak 69.0%" in detail.text
    assert "DatabaseConnections" in detail.text and "no data points" in detail.text
    assert "No prices in this import." in detail.text and "0.00 USD" in detail.text
    assert "/scenarios/campaign-overlap?evidence=imports/aws-floci-demo-test.yaml" in detail.text
    assert client.get("/aws/..%2Fsecrets.yaml").status_code == 404
    assert client.get("/aws/campaign-overlap-pg.yaml").status_code == 404
    assert client.post("/aws/import", data={"instance": "demo-writer"}).status_code == 400
    assert client.post("/aws/import", data={"instance": "demo-writer", "hours": 0}).status_code == 400


def test_what_if_options_recompute_on_the_server(client):
    page = client.get("/scenarios/campaign-overlap")
    assert 'data-whatif' in page.text and 'id="options-live"' in page.text and "3.1× in EV-CAL-002" in page.text
    assert "6<small>/8</small>" in page.text and "$304,500" in page.text and "$18,708.48 one-off" in page.text
    assert "$13,548.80/month" in page.text and "$162,585.60" in page.text, "12-month view of a permanent resize"
    lower = client.get("/scenarios/campaign-overlap/options", params={"A-CAMPAIGN-MULT": "3.1"})
    assert lower.status_code == 200 and "8<small>/8</small>" in lower.text and "<svg class=\"viz\"" in lower.text
    assert 'risk-off' in lower.text
    higher = client.get("/scenarios/campaign-overlap/options", params={"A-CAMPAIGN-MULT": "6"})
    assert "4<small>/8</small>" in higher.text and "0 of them cost nothing" in higher.text and "$522,000" in higher.text
    assert client.get("/scenarios/campaign-overlap/options", params={"A-CAMPAIGN-MULT": "60"}).status_code == 400
    assert client.get("/scenarios/campaign-overlap/options", params={"A-CAMPAIGN-MULT": "lots"}).status_code == 400
    assert client.get("/scenarios/campaign-overlap/options", params={"A-FAILOVER-SECONDS": "5"}).status_code == 400


def test_job_status_json(client):
    assert client.get("/jobs/nope.json").status_code == 404


def test_gcp_and_azure_pages(client):
    for key, heading, emulator in (("gcp", "Import from Google Cloud", "floci-gcp"), ("azure", "Import from Azure", "floci-az")):
        page = client.get(f"/{key}")
        assert page.status_code == 200 and heading in page.text and "refusing endpoint" in page.text
        assert f"{key}-test.yaml" in page.text and emulator in page.text
        assert client.post(f"/{key}/import", data={"instance": "x"}).status_code == 400
        assert client.get(f"/{key}/aws-floci-demo-test.yaml").status_code == 404
    gcp = client.get("/gcp/gcp-test.yaml")
    assert gcp.status_code == 200 and "GCP import · gcp test" in gcp.text and "db-custom-16-65536" in gcp.text
    assert "Cloud Monitoring metrics" in gcp.text and "postgresql/num_backends" in gcp.text
    assert "Cloud SQL prices are in the Cloud Billing Catalog" in gcp.text and "No prices in this import." in gcp.text
    azure = client.get("/azure/azure-test.yaml")
    assert azure.status_code == 200 and "Azure import · azure test" in azure.text and "Azure account" in azure.text
    assert "Standard_D16ds_v4" in azure.text and "$1.40" in azure.text and "$1,022" in azure.text  # 1.40 x 730 h
    assert "21430.56 USD" in azure.text and "Azure Monitor metrics" in azure.text
    nav = client.get("/").text
    assert 'href="/gcp"' in nav and 'href="/azure"' in nav
