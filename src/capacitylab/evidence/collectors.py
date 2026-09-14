"""Neutral collector interfaces.

The demo never talks to a cloud account. `CloudWatchMetricsCollector` accepts an injected client
(anything exposing `get_metric_statistics`, e.g. a boto3 CloudWatch client the operator built with
their own read-only credentials) and converts responses into evidence items. It performs no
credential discovery of its own.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.evidence.normalize import summarize_datapoints


class MetricStatisticsClient(Protocol):
    def get_metric_statistics(self, **kwargs) -> dict: ...


class CloudWatchMetricsCollector:
    def __init__(self, client: MetricStatisticsClient, namespace: str = "AWS/RDS", period_seconds: int = 60):
        self.client = client
        self.namespace = namespace
        self.period_seconds = period_seconds

    def collect(
        self,
        instance_id: str,
        metrics: list[tuple[str, str]],
        start: datetime,
        end: datetime,
        evidence_prefix: str = "EV-CW",
    ) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []
        for n, (metric_name, unit) in enumerate(metrics, start=1):
            response = self.client.get_metric_statistics(
                Namespace=self.namespace,
                MetricName=metric_name,
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": instance_id}],
                StartTime=start,
                EndTime=end,
                Period=self.period_seconds,
                Statistics=["Average", "Maximum", "Minimum"],
            )
            summary = summarize_datapoints(response.get("Datapoints", []), unit, self.period_seconds)
            items.append(
                EvidenceItem(
                    id=f"{evidence_prefix}-{n:03d}",
                    kind=EvidenceKind.METRIC_SUMMARY,
                    title=f"{metric_name} for {instance_id}",
                    provenance=Provenance.OBSERVED,
                    synthetic=False,
                    source=f"cloudwatch:{self.namespace}/{metric_name}",
                    method=f"get_metric_statistics period={self.period_seconds}s",
                    cluster_id=None,
                    window_start=start,
                    window_end=end,
                    data={"metric": metric_name, "instance": instance_id, **summary},
                    caveats=[summary["caveat"]] if "caveat" in summary else [],
                )
            )
        return items
