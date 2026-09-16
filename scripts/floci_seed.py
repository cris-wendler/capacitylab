"""Load a local AWS emulator (Floci) with a synthetic RDS writer, a read replica and a day of CloudWatch metrics.

    docker compose --profile aws up -d floci
    python scripts/floci_seed.py
    capacitylab import aws --instance demo-writer --label "floci demo" --out runs/imports/aws.yaml

The metrics follow the shape of the campaign-overlap scenario: a quiet day, an evening rise, and a batch-job bump
from 19:00 to 20:30 UTC. Everything here is synthetic. Refuses any endpoint that is not local.
"""

from __future__ import annotations

import math
import random
import sys
import time
from datetime import UTC, datetime, timedelta

from capacitylab.aws_import import EMULATOR_ENDPOINT, AwsTarget

WRITER = "demo-writer"
READER = "demo-reader"
INSTANCE_CLASS = "db.r6g.2xlarge"


def cpu_at(ts: datetime, rng: random.Random, reader: bool = False) -> tuple[float, float]:
    """(average, maximum) CPU percent for a 5-minute period."""
    hour = ts.hour + ts.minute / 60
    level = 12 + 20 * math.exp(-((hour - 19.5) / 3.2) ** 2)  # evening peak
    if 19 <= hour < 20.5:
        level += 16  # batch job
    if reader:
        level *= 0.45
    average = max(2.0, level + rng.uniform(-2, 2))
    return round(average, 2), round(min(100.0, average * rng.uniform(1.15, 1.45)), 2)


def wait_available(rds, identifier: str, timeout_s: float = 300) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = rds.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0]["DBInstanceStatus"]
        if status == "available":
            return
        time.sleep(3)
    raise TimeoutError(f"{identifier} did not become available in {timeout_s:.0f}s")


def ensure_instances(rds) -> list[str]:
    """Create the writer and, where the emulator supports it, a read replica. Returns the instances created."""
    existing = {i["DBInstanceIdentifier"] for i in rds.describe_db_instances()["DBInstances"]}
    if WRITER not in existing:
        rds.create_db_instance(DBInstanceIdentifier=WRITER, DBInstanceClass=INSTANCE_CLASS, Engine="mysql",
                               EngineVersion="8.0", MasterUsername="admin", MasterUserPassword="change-me-local-only",
                               AllocatedStorage=500, StorageType="gp3")
    wait_available(rds, WRITER)
    if READER not in existing:
        try:
            rds.create_db_instance_read_replica(DBInstanceIdentifier=READER, SourceDBInstanceIdentifier=WRITER,
                                                DBInstanceClass=INSTANCE_CLASS)
        except rds.exceptions.ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "UnsupportedOperation":
                raise
            print("read replicas are not supported by this emulator version; seeding the writer only")
            return [WRITER]
    wait_available(rds, READER)
    return [WRITER, READER]


def put_metrics(cloudwatch, now: datetime, instances: list[str]) -> int:
    rng = random.Random(7)
    start = (now - timedelta(hours=24)).replace(minute=0, second=0, microsecond=0)
    sent = 0
    for identifier in instances:
        reader = identifier != WRITER
        batch = []
        ts = start
        while ts < now:
            average, maximum = cpu_at(ts, rng, reader)
            dims = [{"Name": "DBInstanceIdentifier", "Value": identifier}]
            batch.append({"MetricName": "CPUUtilization", "Dimensions": dims, "Timestamp": ts, "Unit": "Percent",
                          "StatisticValues": {"SampleCount": 5, "Sum": average * 5, "Minimum": average * 0.8,
                                              "Maximum": maximum}})
            batch.append({"MetricName": "DatabaseConnections", "Dimensions": dims, "Timestamp": ts, "Unit": "Count",
                          "Value": float(round(40 + average * 3))})
            if len(batch) >= 20:  # the emulator rejects larger PutMetricData requests
                cloudwatch.put_metric_data(Namespace="AWS/RDS", MetricData=batch)
                sent += len(batch)
                batch = []
            ts += timedelta(minutes=5)
        if batch:
            cloudwatch.put_metric_data(Namespace="AWS/RDS", MetricData=batch)
            sent += len(batch)
    return sent


def main() -> int:
    target = AwsTarget(endpoint_url=sys.argv[1] if len(sys.argv) > 1 else EMULATOR_ENDPOINT)  # refuses non-local
    print(f"seeding {target.endpoint_url}")
    instances = ensure_instances(target.client("rds"))
    print(f"instances available: {', '.join(instances)} ({INSTANCE_CLASS})")
    sent = put_metrics(target.client("cloudwatch"), datetime.now(UTC), instances)
    print(f"{sent} metric datapoints loaded for the last 24 h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
