import re

import pytest
from fastapi.testclient import TestClient

from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.lab.runner import write_evidence
from capacitylab.settings import MySQLSettings, Settings
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


@pytest.fixture(scope="module")
def runs_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("runs")
    write_evidence([LAB_ITEM, *PERCONA_ITEMS], path / "lab" / "campaign-overlap-test.yaml")
    return path


@pytest.fixture(scope="module")
def client(runs_dir):
    # A non-local MySQL host keeps the lab status check offline and exercises the refusal path.
    settings = Settings(runs_dir=runs_dir, mysql=MySQLSettings("db.example.invalid", 3306, "u", "p", "capacitylab_sandbox"))
    with TestClient(create_app(settings, inline_jobs=True)) as c:
        yield c


def test_home_and_scenario_pages(client):
    home = client.get("/")
    assert home.status_code == 200 and "Flash sale overlapping evening traffic" in home.text and "Decision record" in home.text
    assert "Model budget" in home.text and "Local MySQL lab" in home.text
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
    assert "Where each role landed" in started.text and "Proposed database changes" in started.text
    assert "Lab measurements used" in started.text and "137.85" in started.text
    run_path = started.url.path
    evidence_id = re.search(r'/evidence/(EV-TOOL-\d+)"', started.text).group(1)
    assert "Produced by" in client.get(f"{run_path}/evidence/{evidence_id}").text
    assert client.get(f"{run_path}/evidence/EV-LAB-CMP").status_code == 200
    assert client.get(f"{run_path}/evidence/EV-NOPE-001").status_code == 404
    replay = client.get(f"{run_path}/replay")
    assert replay.status_code == 200 and "verified" in replay.text and "not verified" not in replay.text
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


def test_comparison_page(client):
    page = client.get("/scenarios/downsize-reader/evaluate")
    assert page.status_code == 200 and "Simple rules" in page.text and "Single reviewer" in page.text
