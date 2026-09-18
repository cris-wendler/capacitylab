# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collect one window of metrics from a cloud and append it to the history.

This is the same read path the imports use (`aws_import`, `gcp_import`, `azure_import`), pointed at the store instead
of at an evidence file: topology to learn the nodes, then each node's metrics. Run it as often as you like - a window
already held is written once, so overlapping runs cost nothing.

    capacitylab history collect aws --instance demo-writer --hours 3

An import answers "what does this cluster look like right now" for one review. Collecting answers "what does this
cluster do" over weeks, which is what the envelope needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from capacitylab.history.store import History


@dataclass
class Collected:
    cluster_key: str
    source: str
    window_start: datetime
    window_end: datetime
    nodes: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    seen: int = 0
    written: int = 0
    note: str | None = None

    @property
    def already_held(self) -> int:
        return self.seen - self.written


def _finish(history: History, result: Collected) -> Collected:
    history.record_collection(result.cluster_key, result.source, result.window_start, result.window_end,
                              seen=result.seen, written=result.written, note=result.note)
    return result


def collect_aws(history: History, target, instance_id: str, *, hours: float = 3.0, period_s: int = 300,
                label: str | None = None, now: datetime | None = None, clients: dict | None = None) -> Collected:
    from capacitylab.aws_import import METRICS, read_metrics, read_topology

    clients = clients or {}
    client = lambda name: clients.get(name) or target.client(name)  # noqa: E731
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = now - timedelta(hours=hours)

    topology, instances, identifiers = read_topology(client("rds"), instance_id)
    key = history.cluster_key(identifiers)
    result = Collected(key, f"{target.kind}:cloudwatch", start, now)
    history.note_cluster(key, label=label, cloud=target.kind, engine=topology.get("engine"), when=now)

    roles = ["writer"] + [f"reader-{n}" for n in range(1, len(instances))]
    cloudwatch = client("cloudwatch")
    for role, instance, node in zip(roles, instances, [topology["writer"], *topology["readers"]], strict=True):
        history.note_node(key, role, instance_class=node.get("instance_class"), vcpu=node.get("vcpu"),
                          memory_gib=node.get("memory_gib"), when=now)
        raw = read_metrics(cloudwatch, instance["DBInstanceIdentifier"], start, now, period_s)
        for metric, points in raw.items():
            seen, written = history.add_samples(key, role, metric, points, unit=METRICS[metric], period_s=period_s)
            result.seen, result.written = result.seen + seen, result.written + written
            if points and metric not in result.metrics:
                result.metrics.append(metric)
        result.nodes.append(role)
    return _finish(history, result)


def collect_gcp(history: History, target, instance_name: str, *, hours: float = 3.0, period_s: int = 300,
                label: str | None = None, now: datetime | None = None, http=None) -> Collected:
    from capacitylab.gcp_import import read_metrics, read_topology

    http = http or target.client()
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = now - timedelta(hours=hours)

    topology, instances, identifiers = read_topology(http, target, instance_name)
    key = history.cluster_key(identifiers)
    result = Collected(key, f"{target.kind}:monitoring", start, now)
    history.note_cluster(key, label=label, cloud=target.kind, engine=topology.get("engine"), when=now)

    engine_family = instances[0].get("databaseVersion", "").split("_", 1)[0]
    roles = ["writer"] + [f"reader-{n}" for n in range(1, len(instances))]
    for role, instance, node in zip(roles, instances, [topology["writer"], *topology["readers"]], strict=True):
        history.note_node(key, role, instance_class=node.get("instance_class"), vcpu=node.get("vcpu"),
                          memory_gib=node.get("memory_gib"), when=now)
        raw = read_metrics(http, target, instance["name"], engine_family, start, now, period_s)
        for metric, (unit, points) in raw.items():
            seen, written = history.add_samples(key, role, metric, points, unit=unit, period_s=period_s)
            result.seen, result.written = result.seen + seen, result.written + written
            if points and metric not in result.metrics:
                result.metrics.append(metric)
        result.nodes.append(role)
    return _finish(history, result)


def collect_azure(history: History, target, server_name: str, *, hours: float = 3.0, period_s: int = 300,
                  label: str | None = None, now: datetime | None = None, http=None) -> Collected:
    from capacitylab.azure_import import read_metrics, read_topology
    from capacitylab.cloud_common import CloudApiError

    http = http or target.client()
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = now - timedelta(hours=hours)

    topology, servers, identifiers = read_topology(http, target, server_name)
    key = history.cluster_key(identifiers)
    result = Collected(key, f"{target.kind}:insights", start, now)
    history.note_cluster(key, label=label, cloud=target.kind, engine=topology.get("engine"), when=now)

    roles = ["writer"] + [f"reader-{n}" for n in range(1, len(servers))]
    for role, server, node in zip(roles, servers, [topology["writer"], *topology["readers"]], strict=True):
        history.note_node(key, role, instance_class=node.get("instance_class"), vcpu=node.get("vcpu"),
                          memory_gib=node.get("memory_gib"), when=now)
        try:
            raw = read_metrics(http, target, server, start, now, period_s)
        except CloudApiError as exc:
            result.note = (f"Azure Monitor answered {exc.status}"
                           + ("; the emulator does not serve metrics" if not target.live else ""))
            break
        for metric, (unit, points) in raw.items():
            seen, written = history.add_samples(key, role, metric, points, unit=unit, period_s=period_s)
            result.seen, result.written = result.seen + seen, result.written + written
            if points and metric not in result.metrics:
                result.metrics.append(metric)
        result.nodes.append(role)
    return _finish(history, result)


COLLECTORS = {"aws": collect_aws, "gcp": collect_gcp, "azure": collect_azure}
