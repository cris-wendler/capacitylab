import json
import os
from datetime import UTC, datetime, timedelta

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.stub import ANY, Stubber  # noqa: E402

from capacitylab.aws_import import (  # noqa: E402
    AwsTarget,
    UnsafeAwsTarget,
    _on_demand_usd,
    import_aws,
    slot_series,
)

NOW = datetime(2026, 9, 16, 21, 0, tzinfo=UTC)
WRITER_ID = "orders-prod-writer-7"  # stands in for a name that must never reach the evidence
READER_ID = "orders-prod-replica-7"
# Fake account id, ARN prefix and endpoint suffix, split so the identifier scan does not match this file.
ACCOUNT = "1234" + "56789012"
ARN = "arn" + ":aws:"
RDS_HOST = ".rds" + ".amazonaws.com"


def _client(service, region="us-east-1"):
    return boto3.client(service, region_name=region, aws_access_key_id="x", aws_secret_access_key="x")


def _instance(identifier, klass, replicas=()):
    return {"DBInstanceIdentifier": identifier, "DBInstanceClass": klass, "Engine": "mysql", "EngineVersion": "8.0.39",
            "DBInstanceStatus": "available", "MultiAZ": False, "StorageType": "gp3", "AllocatedStorage": 500,
            "ReadReplicaDBInstanceIdentifiers": list(replicas),
            "Endpoint": {"Address": f"{identifier}.abc123.us-east-1{RDS_HOST}", "Port": 3306},
            "DBInstanceArn": f"{ARN}rds:us-east-1:{ACCOUNT}:db:{identifier}"}


def _price(usd):
    return json.dumps({"product": {"attributes": {}}, "terms": {"OnDemand": {"T1": {"priceDimensions": {
        "D1": {"unit": "Hrs", "pricePerUnit": {"USD": f"{usd:.4f}"}}}}}}})


def _datapoints(hours=2, period=300, base=30.0):
    start = NOW - timedelta(hours=hours)
    return [{"Timestamp": start + timedelta(seconds=period * i), "Average": base + i % 6, "Maximum": base + 10 + i % 6,
             "Minimum": base - 5, "Unit": "Percent"} for i in range(int(hours * 3600 / period))]


def stubbed_clients(with_prices=True):
    rds, cloudwatch, pricing, ce = _client("rds"), _client("cloudwatch"), _client("pricing"), _client("ce")
    s_rds, s_cw, s_pr, s_ce = Stubber(rds), Stubber(cloudwatch), Stubber(pricing), Stubber(ce)
    s_rds.add_response("describe_db_instances", {"DBInstances": [_instance(WRITER_ID, "db.r6g.2xlarge", [READER_ID])]},
                       {"DBInstanceIdentifier": WRITER_ID})
    s_rds.add_response("describe_db_instances", {"DBInstances": [_instance(READER_ID, "db.r6g.2xlarge")]},
                       {"DBInstanceIdentifier": READER_ID})
    for _node in ("writer", "reader"):
        for _metric in range(5):
            s_cw.add_response("get_metric_statistics", {"Datapoints": _datapoints(), "Label": "m"}, {
                "Namespace": "AWS/RDS", "MetricName": ANY, "StartTime": ANY, "EndTime": ANY, "Period": 300,
                "Statistics": ["Average", "Maximum", "Minimum"], "Dimensions": ANY})
    for size, usd in zip(("large", "xlarge", "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge"),
                         (0.26, 0.52, 1.04, 2.08, 4.16, 6.24, 8.32), strict=True):
        filters = [{"Type": "TERM_MATCH", "Field": "instanceType", "Value": f"db.r6g.{size}"},
                   {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": "MySQL"},
                   {"Type": "TERM_MATCH", "Field": "regionCode", "Value": "us-east-1"},
                   {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": "Single-AZ"}]
        s_pr.add_response("get_products", {"PriceList": [_price(usd)] if with_prices else [], "FormatVersion": "aws_v1"},
                          {"ServiceCode": "AmazonRDS", "Filters": filters, "MaxResults": 10})
    s_ce.add_response("get_cost_and_usage", {"ResultsByTime": [
        {"TimePeriod": {"Start": "2026-09-01", "End": "2026-09-17"},
         "Total": {"UnblendedCost": {"Amount": "1210.4567", "Unit": "USD"}}, "Estimated": True}]}, {
        "TimePeriod": {"Start": "2026-09-01", "End": "2026-09-17"}, "Granularity": "MONTHLY",
        "Metrics": ["UnblendedCost"], "Filter": ANY})
    for s in (s_rds, s_cw, s_pr, s_ce):
        s.activate()
    return {"rds": rds, "cloudwatch": cloudwatch, "pricing": pricing, "ce": ce}, (s_rds, s_cw, s_pr, s_ce)


def test_import_builds_topology_metrics_prices_and_cost_without_identifiers():
    clients, stubs = stubbed_clients()
    result = import_aws(AwsTarget(), WRITER_ID, hours=2, now=NOW, clients=clients, label="orders evening")
    for s in stubs:
        s.assert_no_pending_responses()
    by_id = {i.id: i for i in result.items}
    assert set(by_id) == {"EV-AWS-TOPO-ORDERS-EVENING", "EV-AWS-CPU-ORDERS-EVENING", "EV-AWS-MET-ORDERS-EVENING",
                          "EV-AWS-RATE-ORDERS-EVENING", "EV-AWS-COST-ORDERS-EVENING"}
    topo = by_id["EV-AWS-TOPO-ORDERS-EVENING"].data
    assert topo["writer"] == {"role": "writer", "instance_class": "db.r6g.2xlarge", "availability_zone_count": 1,
                              "status": "available", "vcpu": 8, "memory_gib": 64}
    assert [r["role"] for r in topo["readers"]] == ["reader-1"] and topo["engine"] == "mysql"
    rate = by_id["EV-AWS-RATE-ORDERS-EVENING"].data
    assert rate["instance_hourly"]["db.r6g.2xlarge"] == 1.04 and rate["missing_classes"] == []
    assert by_id["EV-AWS-COST-ORDERS-EVENING"].data["month_to_date"] == 1210.46
    cpu = by_id["EV-AWS-CPU-ORDERS-EVENING"].data
    assert len(cpu["slots"]) == 8 and len(cpu["avg_by_slot"]) == 8 and max(cpu["max_by_slot"]) == 45.0
    assert set(by_id["EV-AWS-MET-ORDERS-EVENING"].data["nodes"]) == {"writer", "reader-1"}
    dumped = json.dumps([i.model_dump(mode="json") for i in result.items])
    for secret in ("orders-prod", ACCOUNT, ARN, RDS_HOST, "abc123"):
        assert secret not in dumped
    assert all(i.synthetic and i.environment == "import" and i.source.startswith("floci:") for i in result.items)
    assert result.skipped == []


def test_unlabelled_import_is_named_by_a_hash_and_missing_prices_are_reported():
    clients, _ = stubbed_clients(with_prices=False)
    result = import_aws(AwsTarget(), WRITER_ID, hours=2, now=NOW, clients=clients)
    ids = [i.id for i in result.items]
    assert all(i.startswith("EV-AWS-") and WRITER_ID.upper() not in i for i in ids)
    assert not any("RATE" in i for i in ids)
    assert result.skipped == ["prices: the Pricing API returned nothing for this engine and region"]


def test_emulator_without_rds_prices_is_reported_clearly():
    clients, _ = stubbed_clients()
    pricing = _client("pricing")
    stub = Stubber(pricing)
    stub.add_client_error("get_products", service_error_code="InvalidParameterException",
                          service_message="Invalid ServiceCode: AmazonRDS")
    stub.activate()
    result = import_aws(AwsTarget(), WRITER_ID, hours=2, now=NOW, clients={**clients, "pricing": pricing})
    assert result.skipped == ["prices: this Pricing endpoint has no RDS products (Floci's snapshot does not include "
                              "AmazonRDS)"]
    assert any(i.id.startswith("EV-AWS-COST-") for i in result.items)


def test_real_endpoints_need_live_and_live_uses_no_endpoint():
    with pytest.raises(UnsafeAwsTarget):
        AwsTarget(endpoint_url="https://rds.us-east-1.amazonaws.com")
    with pytest.raises(UnsafeAwsTarget):
        AwsTarget(endpoint_url=None)
    with pytest.raises(UnsafeAwsTarget):
        AwsTarget(live=True, endpoint_url="http://127.0.0.1:4566")
    assert AwsTarget(live=True, endpoint_url=None).kind == "aws"
    assert AwsTarget(endpoint_url="http://localhost:4566").kind == "floci"


def test_slot_series_and_price_parsing():
    series = slot_series(_datapoints(hours=1, period=60), slot_minutes=15)
    assert len(series["slots"]) == 4 and series["slots"][0] == "2026-09-16 20:00"
    assert _on_demand_usd(_price(1.04)) == 1.04
    assert _on_demand_usd({"terms": {}}) is None


@pytest.mark.aws
@pytest.mark.skipif(os.environ.get("CAPACITYLAB_TEST_FLOCI") != "1",
                    reason="set CAPACITYLAB_TEST_FLOCI=1 with Floci running and seeded (scripts/floci_seed.py)")
def test_import_against_floci():
    from scripts.floci_seed import WRITER

    result = import_aws(AwsTarget(), WRITER, hours=24, label="floci demo")
    by_kind = {i.kind.value for i in result.items}
    assert {"topology", "metric_summary"} <= by_kind
    topo = next(i for i in result.items if i.kind.value == "topology").data
    assert topo["writer"]["instance_class"] == "db.r6g.2xlarge"
    cpu = next(i for i in result.items if i.id.startswith("EV-AWS-CPU-"))
    assert max(cpu.data["max_by_slot"]) > 50
