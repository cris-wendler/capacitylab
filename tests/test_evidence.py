from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.evidence.collectors import CloudWatchMetricsCollector
from capacitylab.evidence.models import Assertion, EvidenceItem, EvidenceKind, Provenance
from capacitylab.evidence.normalize import (
    TenantAttribution,
    digest_slow_log,
    fingerprint_sql,
    parse_slow_log,
    redact_text,
    summarize_datapoints,
)

SLOW_LOG = """# Time: 2026-10-08T19:01:02.123456Z
# User@Host: app_rw[app_rw] @  [198.51.100.7]  Id:    42
# Query_time: 6.100000  Lock_time: 0.000100 Rows_sent: 3  Rows_examined: 900000
use shop_a;
SET timestamp=1791486062;
SELECT * FROM orders WHERE tenant_id = 1 AND customer_id IN (4, 5, 6) AND note = 'someone@example.com';
# Time: 2026-10-08T19:02:00Z
# User@Host: app_rw[app_rw] @ web01 [198.51.100.8] Id: 42
# Query_time: 7.5  Lock_time: 0.2 Rows_sent: 1  Rows_examined: 800000
SET timestamp=1791486120;
SELECT * FROM orders WHERE tenant_id = 2 AND customer_id IN (9) AND note = 'x';
# Time: 2026-10-08T19:03:00Z
# User@Host: report[report] @  [198.51.100.9]  Id: 77
# Query_time: 5.2  Lock_time: 1.5 Rows_sent: 10  Rows_examined: 10
UPDATE customers SET loyalty_points = 10 WHERE tenant_id = 3 AND customer_id = 12;
"""


def test_slow_log_parse_fingerprint_redact_and_attribute():
    attribution = TenantAttribution(mode="tenant_column", literal_map={"1": "alder", "2": "birch"})
    entries = parse_slow_log(SLOW_LOG, attribution)
    assert len(entries) == 3
    first, second, third = entries
    assert first.fingerprint_id == second.fingerprint_id
    assert "tenant_id = ?" in first.fingerprint and "in (?+)" in first.fingerprint
    assert first.thread_id == 42 and first.schema_name == "shop_a" and second.schema_name == "shop_a"
    assert first.query_time_s == pytest.approx(6.1) and first.rows_examined == 900000
    assert "example.com" not in first.sql and "198.51" not in first.sql and "<email>" in first.sql
    assert "set timestamp" not in first.sql.lower()
    assert (first.tenant, second.tenant, third.tenant) == ("alder", "birch", "tenant:3")
    assert third.schema_name is None  # different thread, no USE seen


def test_schema_map_attribution_separates_tenants():
    entries = parse_slow_log(SLOW_LOG, TenantAttribution(mode="schema_map", schema_map={"shop_a": "alder"}))
    assert [e.tenant for e in entries] == ["alder", "alder", None]


def test_digest_shares_and_tenant_breakdown():
    entries = parse_slow_log(SLOW_LOG, TenantAttribution(mode="tenant_column", literal_map={"1": "alder", "2": "birch"}))
    digest = digest_slow_log(entries, long_query_time_s=5)
    assert digest["statements"] == 3
    assert sum(f["share_of_query_time_pct"] for f in digest["fingerprints"]) == pytest.approx(100, abs=0.2)
    top = digest["fingerprints"][0]
    assert top["calls"] == 2 and set(top["by_tenant"]) == {"alder", "birch"}
    assert any("long_query_time" in c for c in digest["caveats"])


def test_datapoint_summary_exposes_hidden_peaks_and_uses_nearest_rank():
    start = datetime(2026, 10, 8, tzinfo=UTC)
    points = [{"Timestamp": start + timedelta(minutes=5 * i), "Average": float(i + 1), "Maximum": float(i + 1),
               "Minimum": 0.0} for i in range(20)]
    points[3]["Maximum"] = 90.0
    summary = summarize_datapoints(points, "Percent", 300)
    assert summary["p95_of_period_averages"] == 19.0
    assert summary["max_of_period_maxima"] == 90.0
    assert "hidden" in summary["caveat"]
    assert summarize_datapoints([], "Percent")["data_points"] == 0


def test_redact_text():
    assert redact_text("mail a.b@example.org from 203.0.113.9") == "mail <email> from <ip>"


def test_fingerprint_collapses_values_lists():
    a = fingerprint_sql("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y')")
    b = fingerprint_sql("insert into t (a, b) values (3, 'z')")
    assert a == b


def test_evidence_id_and_labels():
    with pytest.raises(ValidationError):
        EvidenceItem(id="bad id", kind=EvidenceKind.SLO, title="x", provenance=Provenance.OBSERVED, source="s")
    observed = EvidenceItem(id="EV-X-001", kind=EvidenceKind.SLO, title="x", provenance=Provenance.OBSERVED, source="s")
    measured = observed.model_copy(update={"provenance": Provenance.MEASURED})
    assert "synthetic fixture" in observed.label
    assert "local sandbox" in measured.label


def test_tenant_redaction_does_not_mutate_original():
    item = EvidenceItem(id="EV-D-001", kind=EvidenceKind.QUERY_DIGEST, title="d", provenance=Provenance.OBSERVED, source="s",
                        data={"fingerprints": [{"by_tenant": {"alder": {"n": 1}, "birch": {"n": 2}}}]})
    redacted = item.redacted_for_tenant("alder")
    assert redacted.data["fingerprints"][0]["by_tenant"] == {"alder": {"n": 1}}
    assert "birch" in item.data["fingerprints"][0]["by_tenant"]
    assert any("redacted" in c for c in redacted.caveats)


def test_contradictions_detected_in_campaign_bundle(campaign):
    _, bundle = campaign
    found = {c.key: c for c in bundle.contradictions()}
    assert set(found["campaign.alder.traffic_multiplier"].evidence_ids) == {"EV-CAL-002", "EV-TEN-ALDER-001"}


def test_values_within_tolerance_are_not_contradictions():
    def item(i, v):
        return EvidenceItem(id=f"EV-A-00{i}", kind=EvidenceKind.SLO, title="t", provenance=Provenance.OBSERVED, source="s",
                            assertions=[Assertion(key="k", value=v, tolerance=0.15)])

    assert EvidenceBundle([item(1, 5.0), item(2, 4.6)]).contradictions() == []
    assert len(EvidenceBundle([item(1, 5.0), item(2, 3.1)]).contradictions()) == 1


def test_bundle_rejects_duplicates_and_digest_tracks_content(campaign):
    _, bundle = campaign
    copy = bundle.copy()
    assert copy.digest() == bundle.digest()
    with pytest.raises(ValueError):
        copy.add(copy.get("EV-SLO-001"))
    copy.add(EvidenceItem(id="EV-NEW-001", kind=EvidenceKind.SLO, title="n", provenance=Provenance.OBSERVED, source="s"))
    assert copy.digest() != bundle.digest()


def test_cloudwatch_collector_uses_injected_client_only():
    calls = []

    class FakeClient:
        def get_metric_statistics(self, **kwargs):
            calls.append(kwargs)
            t = datetime(2026, 1, 1, tzinfo=UTC)
            return {"Datapoints": [{"Timestamp": t, "Average": 40.0, "Maximum": 70.0, "Minimum": 10.0}]}

    start = datetime(2026, 1, 1, tzinfo=UTC)
    items = CloudWatchMetricsCollector(FakeClient()).collect("demo-writer", [("CPUUtilization", "Percent")], start,
                                                            start + timedelta(hours=1))
    assert calls[0]["Dimensions"] == [{"Name": "DBInstanceIdentifier", "Value": "demo-writer"}]
    assert items[0].provenance == Provenance.OBSERVED and items[0].synthetic is False
    assert items[0].data["max_of_period_maxima"] == 70.0
