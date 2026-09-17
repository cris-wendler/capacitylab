# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build evidence from Google Cloud: Cloud SQL topology and Cloud Monitoring metrics.

By default this talks to a local emulator (floci-gcp on 127.0.0.1:4588). Reading a real project needs `live=True`,
which uses Application Default Credentials (the `google-auth` package). Only read calls are made:

    sqladmin: instances.get, instances.list          monitoring: projects.timeSeries.list

Not imported, and said so in the evidence: Cloud SQL prices (they live in the Cloud Billing Catalog, which this import
does not read yet) and month-to-date cost (Google Cloud exposes it only through a BigQuery billing export).

Nothing that identifies the project is stored: no project id, instance name, connection name, IP address or label.
Instances become `writer`, `reader-1`, ...; the import is named by a label you pass or a short hash of the names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from capacitylab.cloud_common import CloudImport, JsonClient, check_target, parse_time, slot_series, tag_for
from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.evidence.normalize import summarize_datapoints

EMULATOR_ENDPOINT = "http://127.0.0.1:4588"
SQLADMIN = "https://sqladmin.googleapis.com"
MONITORING = "https://monitoring.googleapis.com"
READ_CALLS = ("sqladmin.instances.get", "sqladmin.instances.list", "monitoring.projects.timeSeries.list")
EMULATOR_CAVEAT = ("Read from a local Google Cloud emulator (floci-gcp). Its metrics are whatever was loaded into it, "
                   "so this shows the collection path, not a real project.")
LIVE_CAVEAT = "Read from a Google Cloud project through its APIs; CapacityLab did not verify how it is configured."
NOT_PRICED = ("prices: Cloud SQL prices are in the Cloud Billing Catalog, which this import does not read yet; "
              "attach a rate card instead")
NO_COST = "month-to-date cost: Google Cloud exposes it only through a BigQuery billing export, which is not read"

# metric -> (unit shown, multiplier to that unit, aligner for the average, aligner for the maximum)
METRICS = {
    "cpu/utilization": ("Percent", 100.0, "ALIGN_MEAN", "ALIGN_MAX"),
    "memory/utilization": ("Percent", 100.0, "ALIGN_MEAN", "ALIGN_MAX"),
    "disk/read_ops_count": ("Count/Second", 1.0, "ALIGN_RATE", "ALIGN_RATE"),
    "disk/write_ops_count": ("Count/Second", 1.0, "ALIGN_RATE", "ALIGN_RATE"),
}
CONNECTIONS = {"MYSQL": "network/connections", "POSTGRES": "postgresql/num_backends", "SQLSERVER": "network/connections"}


@dataclass(frozen=True)
class GcpTarget:
    project: str
    endpoint_url: str | None = EMULATOR_ENDPOINT
    live: bool = False

    def __post_init__(self):
        check_target(self.live, self.endpoint_url, "Google Cloud")

    @property
    def kind(self) -> str:
        return "gcp" if self.live else "floci-gcp"

    def urls(self) -> tuple[str, str]:
        if self.live:
            return SQLADMIN, MONITORING
        base = self.endpoint_url.rstrip("/")
        return base, base

    def client(self) -> JsonClient:
        if not self.live:
            return JsonClient()
        import google.auth  # optional dependency
        from google.auth.transport.requests import Request

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])

        def token() -> str:
            if not credentials.valid:
                credentials.refresh(Request())
            return credentials.token

        return JsonClient(token)


def tier_shape(tier: str) -> dict:
    """vCPU and memory for a Cloud SQL machine tier, when the tier name states them."""
    if match := re.fullmatch(r"db-custom-(\d+)-(\d+)", tier):
        return {"vcpu": int(match.group(1)), "memory_gib": round(int(match.group(2)) / 1024, 2)}
    if match := re.fullmatch(r"db-perf-optimized-N-(\d+)", tier):
        return {"vcpu": int(match.group(1)), "memory_gib": int(match.group(1)) * 8}
    if match := re.fullmatch(r"db-n1-(standard|highmem)-(\d+)", tier):
        per_vcpu = 3.75 if match.group(1) == "standard" else 6.5
        return {"vcpu": int(match.group(2)), "memory_gib": round(int(match.group(2)) * per_vcpu, 2)}
    return {}


def _node(instance: dict, role: str) -> dict:
    settings = instance.get("settings", {})
    tier = settings.get("tier", "")
    return {"role": role, "instance_class": tier, **tier_shape(tier),
            "availability_zone_count": 2 if settings.get("availabilityType") == "REGIONAL" else 1,
            "status": (instance.get("state") or "").lower()}


def list_instances(target: GcpTarget, http=None) -> list[dict]:
    http = http or target.client()
    sqladmin, _ = target.urls()
    items = http.get(f"{sqladmin}/v1/projects/{target.project}/instances").get("items", [])
    return sorted(({"id": i["name"], "instance_class": i.get("settings", {}).get("tier", ""),
                    "engine": i.get("databaseVersion", ""), "status": (i.get("state") or "").lower(),
                    "replica_of": bool(i.get("masterInstanceName"))} for i in items),
                  key=lambda i: (i["replica_of"], i["id"]))


def read_topology(http, target: GcpTarget, instance_name: str) -> tuple[dict, list[dict], list[str]]:
    sqladmin, _ = target.urls()
    base = f"{sqladmin}/v1/projects/{target.project}/instances"
    writer = http.get(f"{base}/{instance_name}")
    if writer.get("masterInstanceName"):  # asked for a replica: read the primary it follows
        writer = http.get(f"{base}/{writer['masterInstanceName'].split(':')[-1]}")
    readers = [http.get(f"{base}/{name}") for name in sorted(writer.get("replicaNames", []))]
    version = writer.get("databaseVersion", "")
    settings = writer.get("settings", {})
    data = {
        "engine": version.split("_", 1)[0].lower(),
        "engine_version": version.split("_", 1)[1].replace("_", ".") if "_" in version else version,
        "writer": _node(writer, "writer"),
        "readers": [_node(r, f"reader-{n}") for n, r in enumerate(readers, start=1)],
        "storage": {"type": settings.get("dataDiskType"), "allocated_gib": int(settings.get("dataDiskSizeGb") or 0)},
        "high_availability": settings.get("availabilityType") == "REGIONAL",
        "unknown_instance_classes": sorted({n.get("settings", {}).get("tier", "") for n in [writer, *readers]
                                            if not tier_shape(n.get("settings", {}).get("tier", ""))}),
    }
    return data, [writer, *readers], [writer["name"], *(r["name"] for r in readers)]


def _series(http, target: GcpTarget, metric: str, database_id: str, start: datetime, end: datetime,
            period_s: int, aligner: str, scale: float) -> dict[datetime, float]:
    _, monitoring = target.urls()
    params = {
        "filter": f'metric.type = "cloudsql.googleapis.com/database/{metric}" '
                  f'AND resource.labels.database_id = "{database_id}"',
        "interval.startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval.endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aggregation.alignmentPeriod": f"{period_s}s",
        "aggregation.perSeriesAligner": aligner,
    }
    values: dict[datetime, float] = {}
    for series in http.get(f"{monitoring}/v3/projects/{target.project}/timeSeries", params).get("timeSeries", []):
        for point in series.get("points", []):
            raw = point.get("value", {})
            number = raw.get("doubleValue", raw.get("int64Value"))
            if number is None:
                continue
            values[parse_time(point["interval"]["endTime"])] = float(number) * scale
    return values


def read_metrics(http, target: GcpTarget, instance_name: str, engine_family: str, start: datetime, end: datetime,
                 period_s: int) -> dict[str, tuple[str, list[dict]]]:
    """{metric: (unit, datapoints)} with CloudWatch-style Timestamp / Average / Maximum points."""
    database_id = f"{target.project}:{instance_name}"
    wanted = dict(METRICS)
    wanted[CONNECTIONS.get(engine_family.upper(), "network/connections")] = ("Count", 1.0, "ALIGN_MEAN", "ALIGN_MAX")
    out = {}
    for metric, (unit, scale, mean_aligner, max_aligner) in wanted.items():
        mean = _series(http, target, metric, database_id, start, end, period_s, mean_aligner, scale)
        peak = mean if max_aligner == mean_aligner else _series(http, target, metric, database_id, start, end,
                                                                period_s, max_aligner, scale)
        points = [{"Timestamp": ts, "Average": round(value, 4), "Maximum": round(max(value, peak.get(ts, value)), 4)}
                  for ts, value in sorted(mean.items())]
        out[metric] = (unit, points)
    return out


def import_gcp(target: GcpTarget, instance_name: str, hours: float = 24.0, period_s: int = 300,
               label: str | None = None, now: datetime | None = None, http=None) -> CloudImport:
    """Read one Cloud SQL instance and its read replicas into evidence items. `http` lets tests pass recorded responses."""
    http = http or target.client()
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = now - timedelta(hours=hours)
    result = CloudImport()

    topology, instances, identifiers = read_topology(http, target, instance_name)
    tag, name = tag_for(label, identifiers, "GCP")
    common = dict(provenance=Provenance.OBSERVED, synthetic=not target.live, environment="import",
                  caveats=[LIVE_CAVEAT if target.live else EMULATOR_CAVEAT])
    result.items.append(EvidenceItem(
        id=f"EV-GCP-TOPO-{tag}", kind=EvidenceKind.TOPOLOGY, title=f"Cloud SQL topology ({name})",
        source=f"{target.kind}:sqladmin:instances.get", method="identifiers not stored", data=topology, **common))

    engine_family = instances[0].get("databaseVersion", "").split("_", 1)[0]
    names = ["writer"] + [f"reader-{n}" for n in range(1, len(instances))]
    metrics_by_node: dict[str, dict] = {}
    writer_cpu: list[dict] = []
    for node_name, instance in zip(names, instances, strict=True):
        raw = read_metrics(http, target, instance["name"], engine_family, start, now, period_s)
        metrics_by_node[node_name] = {m: summarize_datapoints(points, unit, period_s) for m, (unit, points) in raw.items()}
        if node_name == "writer":
            writer_cpu = raw["cpu/utilization"][1]
    window = dict(window_start=start, window_end=now)
    if writer_cpu:
        result.items.append(EvidenceItem(
            id=f"EV-GCP-CPU-{tag}", kind=EvidenceKind.METRIC_SERIES,
            title=f"Writer CPU utilization, last {hours:g} h in 15-minute slots ({name})",
            source=f"{target.kind}:monitoring:timeSeries.list",
            method=f"{period_s} s alignment; per-slot average of means and maximum of maxima",
            data={"metric": "CPUUtilization", "unit": "Percent", **slot_series(writer_cpu)}, **window, **common))
    else:
        result.skipped.append("writer CPU series: Cloud Monitoring returned no points for the window")
    result.items.append(EvidenceItem(
        id=f"EV-GCP-MET-{tag}", kind=EvidenceKind.METRIC_SUMMARY, title=f"Cloud SQL metrics, last {hours:g} h ({name})",
        source=f"{target.kind}:monitoring:timeSeries.list", method=f"{period_s} s alignment",
        data={"nodes": metrics_by_node}, **window, **common))

    result.skipped += [NOT_PRICED, NO_COST]
    result.items[0].data["not_imported"] = list(result.skipped)
    return result
