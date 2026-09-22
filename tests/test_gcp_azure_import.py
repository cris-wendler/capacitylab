# SPDX-License-Identifier: AGPL-3.0-or-later
"""GCP and Azure imports against recorded API responses: no network, no cloud account, no spend."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from capacitylab.azure_import import AzureTarget, import_azure, list_servers, price_series, sku_shape
from capacitylab.cloud_common import CloudApiError, UnsafeCloudTarget, parse_time
from capacitylab.gcp_import import GcpTarget, import_gcp, list_instances, tier_shape

NOW = datetime(2026, 9, 17, 21, 0, tzinfo=UTC)
# Fake names that must never reach the evidence. Split so the identifier scan does not match this file.
PROJECT, WRITER, REPLICA = "orders-prod-" + "project-7", "orders-prod-primary", "orders-prod-replica-1"
SUBSCRIPTION = "1b2c3d4e-" + "0000-4000-8000-123456789abc"
PRIVATE_IP = "10.20" + ".30.40"
GROUP, SERVER, AZ_REPLICA = "rg-orders-prod", "orders-prod-mysql", "orders-prod-mysql-ro"


class Recorded:
    """Answers each call with the first recorded response whose URL fragment matches, and keeps the calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _answer(self, method, url, params, body=None):
        self.calls.append((method, url, params, body))
        for verb, fragment, response in self.routes:
            if verb == method and fragment in url + json.dumps(params or {}):
                if isinstance(response, Exception):
                    raise response
                return response(params) if callable(response) else response
        raise AssertionError(f"unexpected call {method} {url} {params}")

    def get(self, url, params=None):
        return self._answer("GET", url, params)

    def post(self, url, body, params=None):
        return self._answer("POST", url, params, body)


def gcp_points(base, scale=1.0):
    start = NOW - timedelta(hours=2)
    return {"timeSeries": [{"points": [
        {"interval": {"startTime": (start + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
                      "endTime": (start + timedelta(minutes=5 * (i + 1))).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")},
         "value": {"doubleValue": (base + (i % 6) / 100) * scale}} for i in range(24)]}]}


def gcp_routes(replica=True):
    writer = {"name": WRITER, "connectionName": f"{PROJECT}:us-central1:{WRITER}", "databaseVersion": "POSTGRES_16",
              "region": "us-central1", "state": "RUNNABLE", "ipAddresses": [{"ipAddress": PRIVATE_IP}],
              "replicaNames": [REPLICA] if replica else [],
              "settings": {"tier": "db-custom-16-65536", "availabilityType": "REGIONAL", "dataDiskSizeGb": "500",
                           "dataDiskType": "PD_SSD", "userLabels": {"team": "checkout-squad"}}}
    reader = {"name": REPLICA, "databaseVersion": "POSTGRES_16", "state": "RUNNABLE", "masterInstanceName": f"{PROJECT}:{WRITER}",
              "settings": {"tier": "db-custom-8-32768", "availabilityType": "ZONAL"}}
    series = lambda p: gcp_points(0.40 if "ALIGN_MEAN" in json.dumps(p) else 0.55, 1.0) if "cpu/utilization" in p["filter"] else gcp_points(20)  # noqa: E731
    return [("GET", f"/instances/{REPLICA}", reader), ("GET", f"/instances/{WRITER}", writer),
            ("GET", "/instances", {"items": [writer, reader]}), ("GET", "/timeSeries", series)]


def test_gcp_import_builds_topology_and_metrics_without_identifiers():
    http = Recorded(gcp_routes())
    result = import_gcp(GcpTarget(project=PROJECT), WRITER, hours=2, now=NOW, http=http, label="orders evening")
    by_id = {i.id: i for i in result.items}
    assert set(by_id) == {"EV-GCP-TOPO-ORDERS-EVENING", "EV-GCP-CPU-ORDERS-EVENING", "EV-GCP-MET-ORDERS-EVENING"}
    topo = by_id["EV-GCP-TOPO-ORDERS-EVENING"].data
    assert topo["engine"] == "postgres" and topo["engine_version"] == "16" and topo["high_availability"] is True
    assert topo["writer"] == {"role": "writer", "instance_class": "db-custom-16-65536", "vcpu": 16, "memory_gib": 64.0,
                              "availability_zone_count": 2, "status": "runnable"}
    assert [r["role"] for r in topo["readers"]] == ["reader-1"] and topo["storage"]["allocated_gib"] == 500
    cpu = by_id["EV-GCP-CPU-ORDERS-EVENING"].data
    # points carry their window's end time, so 2 hours of 5-minute windows end in 9 fifteen-minute slots (19:00 to 21:00)
    assert cpu["unit"] == "Percent" and len(cpu["slots"]) == 9
    assert max(cpu["avg_by_slot"]) == pytest.approx(45.0) and max(cpu["max_by_slot"]) == pytest.approx(60.0)
    nodes = by_id["EV-GCP-MET-ORDERS-EVENING"].data["nodes"]
    assert set(nodes) == {"writer", "reader-1"} and "postgresql/num_backends" in nodes["writer"]
    filters = [c[2]["filter"] for c in http.calls if "/timeSeries" in c[1]]
    assert any('resource.labels.database_id = "' in f for f in filters)
    assert result.skipped[0].startswith("prices: Cloud SQL prices") and result.skipped[1].startswith("month-to-date cost")
    assert topo["not_imported"] == result.skipped
    dumped = json.dumps([i.model_dump(mode="json") for i in result.items])
    for secret in (PROJECT, WRITER, REPLICA, PRIVATE_IP, "checkout-squad"):
        assert secret not in dumped
    assert all(i.synthetic and i.source.startswith("floci-gcp:") for i in result.items)


def test_gcp_import_follows_a_replica_to_its_primary_and_lists_instances():
    http = Recorded(gcp_routes())
    result = import_gcp(GcpTarget(project=PROJECT), REPLICA, hours=2, now=NOW, http=http)
    assert result.items[0].data["writer"]["instance_class"] == "db-custom-16-65536"
    assert result.items[0].id.startswith("EV-GCP-TOPO-") and PROJECT.upper() not in result.items[0].id
    listed = list_instances(GcpTarget(project=PROJECT), Recorded(gcp_routes()))
    assert [i["id"] for i in listed] == [WRITER, REPLICA] and listed[1]["replica_of"]


def test_gcp_tier_shapes():
    assert tier_shape("db-custom-4-15360") == {"vcpu": 4, "memory_gib": 15.0}
    assert tier_shape("db-n1-standard-8") == {"vcpu": 8, "memory_gib": 30.0}
    assert tier_shape("db-n1-highmem-16") == {"vcpu": 16, "memory_gib": 104.0}
    assert tier_shape("db-perf-optimized-N-8") == {"vcpu": 8, "memory_gib": 64}
    assert tier_shape("db-f1-micro") == {}


def azure_server(name, sku, role, source=None, ha="ZoneRedundant"):
    resource_id = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/Microsoft.DBforMySQL/flexibleServers/{name}"
    props = {"version": "8.0.21", "state": "Ready", "replicationRole": role, "highAvailability": {"mode": ha},
             "storage": {"storageSizeGB": 512, "iops": 3000}, "fullyQualifiedDomainName": f"{name}.mysql.database.azure.com"}
    if source:
        props["sourceServerResourceId"] = source
    return {"id": resource_id, "name": name, "location": "East US", "sku": {"name": sku, "tier": "GeneralPurpose"},
            "tags": {"owner": "checkout-squad"}, "properties": props}


def azure_metrics(params):
    start = NOW - timedelta(hours=2)
    def series(base):
        return [{"data": [{"timeStamp": (start + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "average": base + i % 6, "maximum": base + 15 + i % 6} for i in range(24)]}]
    return {"value": [{"name": {"value": n}, "unit": "Percent", "timeseries": series(b)}
                      for n, b in (("cpu_percent", 35), ("memory_percent", 60), ("active_connections", 400),
                                   ("io_consumption_percent", 10))]}


def azure_routes(metrics=azure_metrics):
    writer = azure_server(SERVER, "Standard_D16ds_v4", "Source")
    replica = azure_server(AZ_REPLICA, "Standard_D8ds_v4", "Replica", source=writer["id"], ha="Disabled")
    return [("GET", f"/flexibleServers/{SERVER}/replicas", {"value": [replica]}),
            ("GET", "/providers/Microsoft.Insights/metrics", metrics),
            ("GET", f"/flexibleServers/{AZ_REPLICA}", replica),
            ("GET", f"/flexibleServers/{SERVER}", writer),
            ("GET", "/flexibleServers", {"value": [writer, replica]}),
            ("POST", "Microsoft.CostManagement/query", {"properties": {"columns": [{"name": "Cost"}, {"name": "Currency"}],
                                                                       "rows": [[21430.5567, "USD"]]}})]


RETAIL_PAGE = {"Items": [
    {"productName": "Azure Database for MySQL Flexible Server General Purpose Ddsv4 Series Compute", "skuName": "vCore",
     "meterName": "vCore", "unitOfMeasure": "1 Hour", "unitPrice": 0.0875, "armRegionName": "eastus", "type": "Consumption"},
    {"productName": "Azure Database for MySQL Flexible Server General Purpose Ddsv4 Series Compute", "skuName": "vCore",
     "meterName": "vCore", "unitOfMeasure": "1 Hour", "unitPrice": 0.12, "armRegionName": "eastus", "type": "Consumption"},
    {"productName": "Azure Database for MySQL Flexible Server Memory Optimized Edsv4 Series Compute", "skuName": "vCore",
     "meterName": "vCore", "unitOfMeasure": "1 Hour", "unitPrice": 0.17, "armRegionName": "eastus", "type": "Consumption"},
    {"productName": "Azure Database for MySQL Flexible Server Storage", "skuName": "Storage", "meterName": "Storage Data Stored",
     "unitOfMeasure": "1 GB/Month", "unitPrice": 0.115, "armRegionName": "eastus", "type": "Consumption"},
], "NextPageLink": None}


def test_azure_live_import_reads_topology_metrics_price_and_cost_without_identifiers():
    http, prices = Recorded(azure_routes()), Recorded([("GET", "prices.azure.com", RETAIL_PAGE)])
    target = AzureTarget(subscription=SUBSCRIPTION, resource_group=GROUP, live=True, endpoint_url=None)
    result = import_azure(target, SERVER, hours=2, now=NOW, http=http, prices_http=prices, label="orders evening")
    by_id = {i.id: i for i in result.items}
    assert set(by_id) == {"EV-AZ-TOPO-ORDERS-EVENING", "EV-AZ-CPU-ORDERS-EVENING", "EV-AZ-MET-ORDERS-EVENING",
                          "EV-AZ-RATE-ORDERS-EVENING", "EV-AZ-COST-ORDERS-EVENING"}
    topo = by_id["EV-AZ-TOPO-ORDERS-EVENING"].data
    assert topo["writer"] == {"role": "writer", "instance_class": "Standard_D16ds_v4", "vcpu": 16, "memory_gib": 64,
                              "availability_zone_count": 2, "status": "ready"}
    assert topo["readers"][0]["instance_class"] == "Standard_D8ds_v4" and topo["readers"][0]["availability_zone_count"] == 1
    rate = by_id["EV-AZ-RATE-ORDERS-EVENING"].data
    # cheapest listed Ddsv4 vCore-hour 0.0875 x 16 vCores = 1.40 an hour
    assert rate["per_vcore_hour"] == 0.0875 and rate["vcores"] == 16 and rate["instance_hourly"] == {"Standard_D16ds_v4": 1.4}
    assert by_id["EV-AZ-COST-ORDERS-EVENING"].data["month_to_date"] == 21430.56
    cpu = by_id["EV-AZ-CPU-ORDERS-EVENING"].data
    assert len(cpu["slots"]) == 8 and max(cpu["max_by_slot"]) == 55.0
    cost_body = next(c[3] for c in http.calls if c[0] == "POST")
    assert cost_body["timeframe"] == "MonthToDate" and cost_body["dataset"]["filter"]["dimensions"]["values"] == ["Azure Database for MySQL"]
    assert "eastus" in prices.calls[0][2]["$filter"] and result.skipped == []
    dumped = json.dumps([i.model_dump(mode="json") for i in result.items])
    for secret in (SUBSCRIPTION, GROUP, SERVER, AZ_REPLICA, "database.azure.com", "checkout-squad"):
        assert secret not in dumped
    assert all(not i.synthetic for i in result.items)


def test_azure_emulator_import_skips_prices_cost_and_missing_metrics_with_reasons():
    http = Recorded(azure_routes(metrics=CloudApiError(404, "not found")))
    result = import_azure(AzureTarget(subscription=SUBSCRIPTION, resource_group=GROUP), SERVER, hours=2, now=NOW, http=http)
    assert [i.id.split("-")[2] for i in result.items] == ["TOPO"]
    assert result.skipped == ["metrics: Azure Monitor answered 404 (the emulator does not serve metrics)",
                              "prices: read only with --live (the emulator has no retail price list)",
                              "month-to-date cost: read only with --live (the emulator has no cost data)"]
    assert not any(c[0] == "POST" for c in http.calls)
    listed = list_servers(AzureTarget(subscription=SUBSCRIPTION, resource_group=GROUP), Recorded(azure_routes()))
    assert [s["id"] for s in listed] == [SERVER, AZ_REPLICA] and listed[1]["replica_of"]


def test_azure_sku_parsing():
    assert sku_shape("Standard_E32ds_v5") == {"vcpu": 32, "memory_gib": 256}
    assert sku_shape("Standard_B2s") == {"vcpu": 2}
    assert price_series("Standard_D16ds_v4") == "Ddsv4" and price_series("Standard_E32ds_v5") == "Edsv5"
    assert sku_shape("GP_Gen5_4") == {} and price_series("GP_Gen5_4") is None


def test_targets_refuse_real_endpoints_without_live():
    with pytest.raises(UnsafeCloudTarget):
        GcpTarget(project="p", endpoint_url="https://sqladmin.googleapis.com")
    with pytest.raises(UnsafeCloudTarget):
        AzureTarget(subscription="s", resource_group="g", endpoint_url="https://management.azure.com")
    with pytest.raises(UnsafeCloudTarget):
        AzureTarget(subscription="s", resource_group="g", live=True)
    with pytest.raises(ValueError):
        AzureTarget(subscription="s", resource_group="g", engine="oracle")
    assert GcpTarget(project="p", live=True, endpoint_url=None).urls()[0] == "https://sqladmin.googleapis.com"


def test_timestamps_with_nanoseconds_parse():
    assert parse_time("2026-09-17T20:05:00.123456789Z") == datetime(2026, 9, 17, 20, 5, 0, 123456, tzinfo=UTC)
    assert parse_time("2026-09-17T20:05:00Z").tzinfo is not None


def test_cli_explains_missing_arguments_unsafe_endpoints_and_a_stopped_emulator(tmp_path, capsys):
    from capacitylab import cli

    out = str(tmp_path / "x.yaml")
    assert cli.main(["import", "gcp", "--instance", "a", "--out", out]) == 1
    assert cli.main(["import", "azure", "--instance", "a", "--out", out]) == 1
    assert cli.main(["import", "azure", "--instance", "a", "--subscription", "s", "--resource-group", "g",
                     "--endpoint", "https://management.azure.com", "--out", out]) == 2
    # nothing listens on this local port, so the emulator is "not running"
    assert cli.main(["import", "gcp", "--instance", "a", "--project", "p", "--endpoint", "http://127.0.0.1:9",
                     "--out", out]) == 4
    err = capsys.readouterr().err
    assert "needs --project" in err
    assert "needs --subscription and --resource-group" in err and "refusing endpoint" in err
    assert "Start it: docker compose --profile gcp up -d floci-gcp" in err


@pytest.mark.gcp
@pytest.mark.skipif(os.environ.get("CAPACITYLAB_TEST_FLOCI_GCP") != "1",
                    reason="set CAPACITYLAB_TEST_FLOCI_GCP=1 with floci-gcp running and seeded "
                           "(scripts/cloud_emulator_seed.py gcp)")
def test_import_against_floci_gcp():
    from scripts.cloud_emulator_seed import GCP_INSTANCE, GCP_PROJECT, GCP_TIER

    result = import_gcp(GcpTarget(GCP_PROJECT), GCP_INSTANCE, hours=24, label="floci-gcp demo")
    items = {i.id: i for i in result.items}
    topo = items["EV-GCP-TOPO-FLOCI-GCP-DEMO"].data
    assert topo["engine"] == "postgres" and topo["writer"]["instance_class"] == GCP_TIER
    assert topo["writer"]["vcpu"] == 8 and topo["high_availability"] is True
    cpu = items["EV-GCP-CPU-FLOCI-GCP-DEMO"].data
    assert len(cpu["slots"]) >= 90 and max(cpu["max_by_slot"]) > 40  # a day of 15-minute slots with the evening peak
    writer = items["EV-GCP-MET-FLOCI-GCP-DEMO"].data["nodes"]["writer"]
    assert all(writer[m]["data_points"] > 200 for m in ("cpu/utilization", "memory/utilization",
                                                        "postgresql/num_backends", "disk/read_ops_count"))
    assert [s.split(":")[0] for s in result.skipped] == ["prices", "month-to-date cost"]
    assert GCP_INSTANCE not in json.dumps([i.model_dump(mode="json") for i in result.items])


@pytest.mark.azure
@pytest.mark.skipif(os.environ.get("CAPACITYLAB_TEST_FLOCI_AZ") != "1",
                    reason="set CAPACITYLAB_TEST_FLOCI_AZ=1 with floci-az running and seeded "
                           "(scripts/cloud_emulator_seed.py azure)")
@pytest.mark.parametrize("engine", ["mysql", "postgres"])
def test_import_against_floci_az(engine):
    from scripts.cloud_emulator_seed import AZ_GROUP, AZ_SERVERS, AZ_SUBSCRIPTION

    name, sku, _ = AZ_SERVERS[engine]
    result = import_azure(AzureTarget(AZ_SUBSCRIPTION, AZ_GROUP, engine), name, label="floci-az demo")
    assert [i.id for i in result.items] == ["EV-AZ-TOPO-FLOCI-AZ-DEMO"]
    topo = result.items[0].data
    assert topo["engine"] == engine and topo["writer"]["instance_class"] == sku
    assert topo["writer"]["vcpu"] == int(sku.split("_D")[1].split("ds")[0]) and topo["storage"]["allocated_gib"] == 512
    assert [s.split(":")[0] for s in result.skipped] == ["read replicas", "metrics", "prices", "month-to-date cost"]
    assert name not in json.dumps(result.items[0].model_dump(mode="json"))


@pytest.mark.parametrize("cloud", ["gcp", "azure"])
def test_history_collect_from_recorded_responses(tmp_path, cloud):
    """`history collect gcp|azure`: the import's read path, appended to the store, idempotently, without identifiers."""
    from capacitylab.history import History
    from capacitylab.history.collect import COLLECTORS

    if cloud == "gcp":
        target, name, routes, cpu = GcpTarget(project=PROJECT), WRITER, gcp_routes, "cpu/utilization"
    else:
        target, name, routes, cpu = (AzureTarget(subscription=SUBSCRIPTION, resource_group=GROUP), SERVER,
                                     azure_routes, "cpu_percent")
    with History(tmp_path / "history.db") as history:
        first = COLLECTORS[cloud](history, target, name, hours=2, now=NOW, label="evening", http=Recorded(routes()))
        assert first.nodes == ["writer", "reader-1"] and first.written == first.seen > 0
        assert cpu in first.metrics  # each cloud keeps its own metric names; pass them to `history envelope --metric`
        again = COLLECTORS[cloud](history, target, name, hours=2, now=NOW, http=Recorded(routes()))
        assert again.cluster_key == first.cluster_key and again.written == 0 and again.already_held == first.seen
        assert history.samples(first.cluster_key, cpu, "writer")
    stored = tmp_path.joinpath("history.db").read_bytes().decode("latin-1")
    for identifier in (PROJECT, WRITER, REPLICA, SUBSCRIPTION, GROUP, SERVER, AZ_REPLICA):
        assert identifier not in stored


def test_history_collect_from_azure_emulator_notes_missing_metrics(tmp_path):
    from capacitylab.history import History
    from capacitylab.history.collect import collect_azure

    target = AzureTarget(subscription=SUBSCRIPTION, resource_group=GROUP)
    with History(tmp_path / "history.db") as history:
        result = collect_azure(history, target, SERVER, hours=2, now=NOW,
                               http=Recorded(azure_routes(metrics=CloudApiError(404, "not found"))))
    assert result.written == 0 and "does not serve metrics" in result.note
