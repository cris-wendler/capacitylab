"""Build evidence straight from AWS APIs: RDS topology, CloudWatch metrics, on-demand prices and month-to-date cost.

By default this talks to a local AWS emulator (Floci on 127.0.0.1:4566) with placeholder credentials. Reading a real
account needs `live=True`, which uses the normal AWS credential chain. Only read calls are made:

    rds:DescribeDBInstances, rds:DescribeDBClusters, cloudwatch:GetMetricStatistics,
    pricing:GetProducts, ce:GetCostAndUsage

Nothing that identifies the account is stored: no account id, ARN, endpoint, hostname, instance or cluster identifier,
or tag. Instances become `writer`, `reader-1`, ...; the import is named by a label you pass or a short hash of the
identifiers, the same way file imports are named by a hash of their contents.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from capacitylab.capacity.catalog import CATALOG
from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.evidence.normalize import redact_text, summarize_datapoints

EMULATOR_ENDPOINT = "http://127.0.0.1:4566"
LOCAL_ENDPOINTS = ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")
READ_CALLS = ("rds:DescribeDBInstances", "rds:DescribeDBClusters", "cloudwatch:GetMetricStatistics",
              "pricing:GetProducts", "ce:GetCostAndUsage")
METRICS = {  # CloudWatch metric -> unit used in the summary
    "CPUUtilization": "Percent",
    "DatabaseConnections": "Count",
    "FreeableMemory": "Bytes",
    "ReadIOPS": "Count/Second",
    "WriteIOPS": "Count/Second",
}
SIZES = ("large", "xlarge", "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge")
PRICING_ENGINE = {"mysql": "MySQL", "postgres": "PostgreSQL", "mariadb": "MariaDB",
                  "aurora-mysql": "Aurora MySQL", "aurora-postgresql": "Aurora PostgreSQL"}
EMULATOR_CAVEAT = ("Read from a local AWS emulator (Floci). Its metrics are whatever was loaded into it, so this shows "
                   "the collection path, not a real account.")
LIVE_CAVEAT = "Read from an AWS account through its APIs; CapacityLab did not verify how the account is configured."


class UnsafeAwsTarget(ValueError):
    """Raised when an import would reach a real AWS endpoint without being asked to."""


@dataclass(frozen=True)
class AwsTarget:
    region: str = "us-east-1"
    endpoint_url: str | None = EMULATOR_ENDPOINT
    live: bool = False

    def __post_init__(self):
        if self.live and self.endpoint_url:
            raise UnsafeAwsTarget("--live reads a real account; do not also pass an emulator endpoint")
        if not self.live and not (self.endpoint_url or "").startswith(LOCAL_ENDPOINTS):
            raise UnsafeAwsTarget(f"refusing endpoint {self.endpoint_url!r}: without --live only a local emulator "
                                  "is allowed")

    @property
    def kind(self) -> str:
        return "aws" if self.live else "floci"

    def client(self, service: str, region: str | None = None):
        import boto3  # optional dependency

        if self.live:
            return boto3.client(service, region_name=region or self.region)
        # Placeholder credentials: the emulator accepts any non-empty value.
        return boto3.client(service, region_name=region or self.region, endpoint_url=self.endpoint_url,
                            aws_access_key_id="test", aws_secret_access_key="test")


@dataclass
class AwsImport:
    items: list[EvidenceItem] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # parts that could not be read, with the reason


def _tag(label: str | None, identifiers: list[str]) -> tuple[str, str]:
    if label and label.strip():
        clean = redact_text(label.strip())
        return re.sub(r"[^A-Z0-9]+", "-", clean.upper()).strip("-") or "AWS", clean
    digest = hashlib.sha256("\n".join(sorted(identifiers)).encode()).hexdigest()[:10]
    return digest.upper(), f"aws {digest}"


def _family_classes(instance_class: str) -> list[str]:
    match = re.match(r"^(db\.[a-z0-9]+)\.", instance_class)
    return [f"{match.group(1)}.{size}" for size in SIZES] if match else [instance_class]


def _node(instance: dict, role: str) -> dict:
    spec = CATALOG.get(instance["DBInstanceClass"])
    node = {"role": role, "instance_class": instance["DBInstanceClass"],
            "availability_zone_count": 2 if instance.get("MultiAZ") else 1,
            "status": instance.get("DBInstanceStatus")}
    if spec:
        node.update(vcpu=spec.vcpu, memory_gib=spec.memory_gib)
    if instance.get("PromotionTier") is not None and role != "writer":
        node["promotion_tier"] = instance["PromotionTier"]
    return node


def read_topology(rds, instance_id: str) -> tuple[dict, list[dict], list[str]]:
    """Writer and readers for an instance (RDS read replicas, or the other members of its Aurora cluster).

    Returns (topology data, instances in writer-first order, identifiers used to name the import)."""
    writer = rds.describe_db_instances(DBInstanceIdentifier=instance_id)["DBInstances"][0]
    readers: list[dict] = []
    identifiers = [instance_id]
    cluster_id = writer.get("DBClusterIdentifier")
    if cluster_id:
        cluster = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
        members = {m["DBInstanceIdentifier"]: m for m in cluster.get("DBClusterMembers", [])}
        writer_id = next((i for i, m in members.items() if m.get("IsClusterWriter")), instance_id)
        if writer_id != instance_id:
            writer = rds.describe_db_instances(DBInstanceIdentifier=writer_id)["DBInstances"][0]
        for member_id in sorted(i for i in members if i != writer_id):
            readers.append(rds.describe_db_instances(DBInstanceIdentifier=member_id)["DBInstances"][0])
        identifiers += [cluster_id, *members]
    else:
        for replica_id in sorted(writer.get("ReadReplicaDBInstanceIdentifiers", [])):
            readers.append(rds.describe_db_instances(DBInstanceIdentifier=replica_id)["DBInstances"][0])
            identifiers.append(replica_id)
    data = {
        "engine": writer.get("Engine"),
        "engine_version": writer.get("EngineVersion"),
        "writer": _node(writer, "writer"),
        "readers": [_node(r, f"reader-{n}") for n, r in enumerate(readers, start=1)],
        "storage": {"type": writer.get("StorageType"), "allocated_gib": writer.get("AllocatedStorage")},
        "aurora_cluster": bool(cluster_id),
        "unknown_instance_classes": sorted({n["DBInstanceClass"] for n in [writer, *readers]
                                            if n["DBInstanceClass"] not in CATALOG}),
    }
    return data, [writer, *readers], identifiers


def slot_series(points: list[dict], slot_minutes: int = 15) -> dict:
    """Roll CloudWatch datapoints into fixed slots: the average of averages and the maximum of maxima per slot."""
    slots: dict[datetime, list[dict]] = defaultdict(list)
    for p in points:
        ts = p["Timestamp"].astimezone(UTC)
        slots[ts.replace(minute=ts.minute - ts.minute % slot_minutes, second=0, microsecond=0)].append(p)
    ordered = sorted(slots)
    return {
        "slot_minutes": slot_minutes,
        "slots": [s.strftime("%Y-%m-%d %H:%M") for s in ordered],
        "avg_by_slot": [round(sum(p["Average"] for p in slots[s]) / len(slots[s]), 2) for s in ordered],
        "max_by_slot": [round(max(p.get("Maximum", p["Average"]) for p in slots[s]), 2) for s in ordered],
    }


def read_metrics(cloudwatch, instance_id: str, start: datetime, end: datetime, period_s: int) -> dict[str, list]:
    out = {}
    for metric in METRICS:
        response = cloudwatch.get_metric_statistics(
            Namespace="AWS/RDS", MetricName=metric, StartTime=start, EndTime=end, Period=period_s,
            Statistics=["Average", "Maximum", "Minimum"],
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": instance_id}])
        out[metric] = response.get("Datapoints", [])
    return out


def _on_demand_usd(price_item: str | dict) -> float | None:
    doc = json.loads(price_item) if isinstance(price_item, str) else price_item
    for term in doc.get("terms", {}).get("OnDemand", {}).values():
        for dimension in term.get("priceDimensions", {}).values():
            usd = dimension.get("pricePerUnit", {}).get("USD")
            if usd not in (None, ""):
                return float(usd)
    return None


def read_prices(pricing, engine: str, region: str, classes: list[str], multi_az: bool) -> dict[str, float]:
    database_engine = PRICING_ENGINE.get(engine, engine)
    prices = {}
    for instance_class in classes:
        filters = [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_class},
            {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": database_engine},
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        ]
        if not engine.startswith("aurora"):
            filters.append({"Type": "TERM_MATCH", "Field": "deploymentOption",
                            "Value": "Multi-AZ" if multi_az else "Single-AZ"})
        response = pricing.get_products(ServiceCode="AmazonRDS", Filters=filters, MaxResults=10)
        found = [p for p in (_on_demand_usd(x) for x in response.get("PriceList", [])) if p]
        if found:
            prices[instance_class] = min(found)
    return prices


def read_month_to_date_cost(ce, today: datetime) -> dict:
    start = today.replace(day=1).date()
    end = today.date() + timedelta(days=1)
    response = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()}, Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Relational Database Service"]}})
    total = 0.0
    currency = "USD"
    for period in response.get("ResultsByTime", []):
        cost = period.get("Total", {}).get("UnblendedCost", {})
        total += float(cost.get("Amount", 0) or 0)
        currency = cost.get("Unit", currency)
    return {"service": "Amazon RDS", "period_start": start.isoformat(), "period_end_exclusive": end.isoformat(),
            "month_to_date": round(total, 2), "currency": currency, "metric": "UnblendedCost"}


def import_aws(target: AwsTarget, instance_id: str, hours: float = 24.0, period_s: int = 300,
               label: str | None = None, now: datetime | None = None, clients: dict[str, Any] | None = None,
               include_cost: bool = True) -> AwsImport:
    """Read one RDS instance (and its readers) into evidence items. `clients` lets tests pass stubbed boto3 clients."""
    clients = clients or {}
    client = lambda name, region=None: clients.get(name) or target.client(name, region)  # noqa: E731
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = now - timedelta(hours=hours)
    result = AwsImport()

    topology, instances, identifiers = read_topology(client("rds"), instance_id)
    tag, name = _tag(label, identifiers)
    caveats = [LIVE_CAVEAT if target.live else EMULATOR_CAVEAT]
    common = dict(provenance=Provenance.OBSERVED, synthetic=not target.live, environment="import", caveats=caveats)
    result.items.append(EvidenceItem(
        id=f"EV-AWS-TOPO-{tag}", kind=EvidenceKind.TOPOLOGY, title=f"RDS topology ({name})",
        source=f"{target.kind}:rds:describe-db-instances", method=f"region {target.region}; identifiers not stored",
        data=topology, **common))

    names = ["writer"] + [f"reader-{n}" for n in range(1, len(instances))]
    metrics_by_node: dict[str, dict] = {}
    writer_cpu: list[dict] = []
    cloudwatch = client("cloudwatch")
    for node_name, instance in zip(names, instances, strict=True):
        raw = read_metrics(cloudwatch, instance["DBInstanceIdentifier"], start, now, period_s)
        metrics_by_node[node_name] = {m: summarize_datapoints(points, METRICS[m], period_s) for m, points in raw.items()}
        if node_name == "writer":
            writer_cpu = raw["CPUUtilization"]
    window = dict(window_start=start, window_end=now)
    if writer_cpu:
        result.items.append(EvidenceItem(
            id=f"EV-AWS-CPU-{tag}", kind=EvidenceKind.METRIC_SERIES,
            title=f"Writer CPU utilization, last {hours:g} h in 15-minute slots ({name})",
            source=f"{target.kind}:cloudwatch:get-metric-statistics",
            method=f"{period_s} s period; per-slot average of averages and maximum of maxima",
            data={"metric": "CPUUtilization", "unit": "Percent", **slot_series(writer_cpu)}, **window, **common))
    else:
        result.skipped.append("writer CPU series: CloudWatch returned no datapoints for the window")
    result.items.append(EvidenceItem(
        id=f"EV-AWS-MET-{tag}", kind=EvidenceKind.METRIC_SUMMARY, title=f"RDS CloudWatch metrics, last {hours:g} h ({name})",
        source=f"{target.kind}:cloudwatch:get-metric-statistics", method=f"{period_s} s period",
        data={"nodes": metrics_by_node}, **window, **common))

    classes = _family_classes(topology["writer"]["instance_class"])
    try:
        # The Pricing API is served from us-east-1 whatever region the database is in.
        prices = read_prices(client("pricing", "us-east-1"), topology["engine"] or "", target.region, classes,
                             topology["writer"]["availability_zone_count"] > 1)
    except Exception as exc:  # pricing is optional; the rest of the import still stands
        prices = {}
        message = str(exc)
        if "Invalid ServiceCode" in message:
            result.skipped.append("prices: this Pricing endpoint has no RDS products (Floci's snapshot does not "
                                  "include AmazonRDS)")
        else:
            result.skipped.append(f"prices: {type(exc).__name__}: {message.splitlines()[0][:160] if message else ''}")
    if prices:
        result.items.append(EvidenceItem(
            id=f"EV-AWS-RATE-{tag}", kind=EvidenceKind.RATE_CARD,
            title=f"On-demand RDS prices for the {classes[0].rsplit('.', 1)[0]} family ({name})",
            source=f"{target.kind}:pricing:get-products",
            method=f"{PRICING_ENGINE.get(topology['engine'], topology['engine'])}, {target.region}, on-demand, per instance",
            data={"currency": "USD", "instance_hourly": prices, "hours_per_month": 730,
                  "missing_classes": [c for c in classes if c not in prices],
                  "source_note": "List prices per instance-hour; storage, I/O and discounts are not included."},
            **common))
    elif not any(s.startswith("prices") for s in result.skipped):
        result.skipped.append("prices: the Pricing API returned nothing for this engine and region")

    if include_cost:
        try:
            cost = read_month_to_date_cost(client("ce", "us-east-1"), now)
            result.items.append(EvidenceItem(
                id=f"EV-AWS-COST-{tag}", kind=EvidenceKind.METRIC_SUMMARY, title=f"RDS cost this month so far ({name})",
                source=f"{target.kind}:ce:get-cost-and-usage", method="UnblendedCost, whole account, RDS service only",
                data=cost, **common))
        except Exception as exc:
            result.skipped.append(f"month-to-date cost: {type(exc).__name__}")
    return result
