"""Load the local Google Cloud and Azure emulators with synthetic databases for `capacitylab import gcp|azure`.

    docker compose --profile gcp up -d floci-gcp
    python scripts/cloud_emulator_seed.py gcp      # a Cloud SQL PostgreSQL instance and a day of Cloud Monitoring points
    capacitylab import gcp --project floci-local --instance demo-pg --label "floci-gcp demo" --out runs/imports/gcp.yaml

    docker compose --profile azure up -d floci-az
    python scripts/cloud_emulator_seed.py azure    # a MySQL and a PostgreSQL flexible server
    capacitylab import azure --subscription demo-subscription --resource-group capacitylab-demo \
      --instance demo-mysql --label "floci-az demo" --out runs/imports/azure.yaml

floci-az serves flexible servers but no Azure Monitor metrics, so only topology is seeded there. The CPU shape follows
the AWS seed (scripts/floci_seed.py): a quiet day, an evening rise and a batch-job bump. Everything here is synthetic,
and only local endpoints are accepted.
"""

from __future__ import annotations

import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.floci_seed import cpu_at  # noqa: E402

from capacitylab import azure_import, gcp_import  # noqa: E402
from capacitylab.cloud_common import CloudApiError, JsonClient, check_target  # noqa: E402

GCP_PROJECT = "floci-local"
GCP_INSTANCE = "demo-pg"
GCP_TIER = "db-custom-8-32768"

AZ_SUBSCRIPTION = "demo-subscription"
AZ_GROUP = "capacitylab-demo"
AZ_SERVERS = {  # engine -> (server name, SKU, engine version)
    "mysql": ("demo-mysql", "Standard_D16ds_v4", "8.0.21"),
    "postgres": ("demo-pg", "Standard_D8ds_v4", "16"),
}
LOCAL_PASSWORD = "Local-only-123!"  # the emulator's throwaway database container, never a real server

http = JsonClient(timeout_s=300)  # creating a server starts a database container, which can take a minute


def _create(call, what: str) -> None:
    try:
        call()
    except CloudApiError as exc:
        if exc.status != 409:  # already exists from an earlier seed
            raise
    print(f"ready: {what}")


def seed_gcp(endpoint: str) -> None:
    check_target(False, endpoint, "Google Cloud")
    base = endpoint.rstrip("/")
    instances = f"{base}/v1/projects/{GCP_PROJECT}/instances"
    body = {"name": GCP_INSTANCE, "databaseVersion": "POSTGRES_16", "region": "us-central1",
            "settings": {"tier": GCP_TIER, "availabilityType": "REGIONAL", "dataDiskType": "PD_SSD",
                         "dataDiskSizeGb": "500"}}
    _create(lambda: http.post(instances, body), f"Cloud SQL instance {GCP_INSTANCE} ({GCP_TIER})")

    rng = random.Random(7)
    now = datetime.now(UTC)
    ts = (now - timedelta(hours=24)).replace(minute=0, second=0, microsecond=0)
    resource = {"type": "cloudsql_database", "labels": {"project_id": GCP_PROJECT, "region": "us-central1",
                                                        "database_id": f"{GCP_PROJECT}:{GCP_INSTANCE}"}}
    sent, read_ops, write_ops = 0, 0, 0
    counting_since = (ts - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    while ts < now:
        average, _ = cpu_at(ts, rng)
        gauges = {"cpu/utilization": average / 100, "memory/utilization": 0.55 + average / 400,
                  "postgresql/num_backends": float(round(40 + average * 3))}
        # Cloud SQL reports disk operations as DELTA counts; floci-gcp only accepts GAUGE or CUMULATIVE for metrics it
        # creates, so they are written as running counters. The import's ALIGN_RATE turns either kind into operations/s.
        read_ops += round((900 + average * 60) * 300)
        write_ops += round((300 + average * 25) * 300)
        counters = {"disk/read_ops_count": read_ops, "disk/write_ops_count": write_ops}
        stamp = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        series = [{"metric": {"type": f"cloudsql.googleapis.com/database/{metric}"}, "resource": resource,
                   "metricKind": "GAUGE", "valueType": "DOUBLE",
                   "points": [{"interval": {"endTime": stamp}, "value": {"doubleValue": value}}]}
                  for metric, value in gauges.items()]
        series += [{"metric": {"type": f"cloudsql.googleapis.com/database/{metric}"}, "resource": resource,
                    "metricKind": "CUMULATIVE", "valueType": "INT64",
                    "points": [{"interval": {"startTime": counting_since, "endTime": stamp},
                                "value": {"int64Value": str(value)}}]}
                   for metric, value in counters.items()]
        http.post(f"{base}/v3/projects/{GCP_PROJECT}/timeSeries", {"timeSeries": series})
        sent += len(series)
        ts += timedelta(minutes=5)
    print(f"{sent} Cloud Monitoring points loaded for the last 24 h")


def seed_azure(endpoint: str) -> None:
    check_target(False, endpoint, "Azure")
    base = endpoint.rstrip("/")
    _create(lambda: http.put(f"{base}/subscriptions/{AZ_SUBSCRIPTION}/resourceGroups/{AZ_GROUP}", {"location": "eastus"},
                             {"api-version": "2021-04-01"}), f"resource group {AZ_GROUP}")
    for engine, (name, sku, version) in AZ_SERVERS.items():
        target = azure_import.AzureTarget(AZ_SUBSCRIPTION, AZ_GROUP, engine, endpoint)
        body = {"location": "eastus", "sku": {"name": sku, "tier": "GeneralPurpose"},
                "properties": {"administratorLogin": "capadmin", "administratorLoginPassword": LOCAL_PASSWORD,
                               "version": version, "storage": {"storageSizeGB": 512}}}
        _create(lambda t=target, n=name, b=body: http.put(f"{base}{t.servers_path()}/{n}", b,
                                                          {"api-version": t.api_version}),
                f"{engine} flexible server {name} ({sku})")


def main() -> int:
    cloud = sys.argv[1] if len(sys.argv) > 1 else ""
    if cloud == "gcp":
        seed_gcp(sys.argv[2] if len(sys.argv) > 2 else gcp_import.EMULATOR_ENDPOINT)
    elif cloud == "azure":
        seed_azure(sys.argv[2] if len(sys.argv) > 2 else azure_import.EMULATOR_ENDPOINT)
    else:
        print("usage: cloud_emulator_seed.py gcp|azure [endpoint]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
