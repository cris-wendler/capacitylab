"""Build evidence from Azure: flexible server topology (MySQL or PostgreSQL), Azure Monitor metrics, on-demand prices
and month-to-date cost.

By default this talks to a local emulator (floci-az on 127.0.0.1:4577). Reading a real subscription needs `live=True`,
which uses `azure-identity` (DefaultAzureCredential). Only read calls are made:

    Microsoft.DBforMySQL / Microsoft.DBforPostgreSQL flexibleServers: get, list, replicas
    Microsoft.Insights metrics: list          Retail Prices API (public, no account)
    Microsoft.CostManagement query (month to date, database service only)

Prices and cost are read only with `live`: the emulator has neither. Nothing that identifies the subscription is stored:
no subscription id, resource group, server name, resource id, host name or tag. Servers become `writer`, `reader-1`,
...; the import is named by a label you pass or a short hash of the names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from capacitylab.cloud_common import (
    CloudApiError,
    CloudImport,
    JsonClient,
    check_target,
    parse_time,
    slot_series,
    tag_for,
)
from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.evidence.normalize import summarize_datapoints

EMULATOR_ENDPOINT = "http://127.0.0.1:4577"
MANAGEMENT = "https://management.azure.com"
RETAIL_PRICES = "https://prices.azure.com/api/retail/prices"
ENGINES = {  # engine -> (resource provider, api-version, retail price service name, IO metric)
    "mysql": ("Microsoft.DBforMySQL", "2023-12-30", "Azure Database for MySQL", "io_consumption_percent"),
    "postgres": ("Microsoft.DBforPostgreSQL", "2024-08-01", "Azure Database for PostgreSQL", "disk_iops_consumed_percentage"),
}
METRICS_API = "2023-10-01"
COST_API = "2023-11-01"
READ_CALLS = ("flexibleServers/read", "flexibleServers/replicas/read", "Microsoft.Insights/metrics/read",
              "retail prices (public)", "Microsoft.CostManagement/query/read")
EMULATOR_CAVEAT = ("Read from a local Azure emulator (floci-az). Its data is whatever was created in it, so this shows "
                   "the collection path, not a real subscription.")
LIVE_CAVEAT = "Read from an Azure subscription through its APIs; CapacityLab did not verify how it is configured."
UNITS = {"cpu_percent": "Percent", "memory_percent": "Percent", "active_connections": "Count",
         "io_consumption_percent": "Percent", "disk_iops_consumed_percentage": "Percent"}


@dataclass(frozen=True)
class AzureTarget:
    subscription: str
    resource_group: str
    engine: str = "mysql"
    endpoint_url: str | None = EMULATOR_ENDPOINT
    live: bool = False

    def __post_init__(self):
        if self.engine not in ENGINES:
            raise ValueError(f"engine must be one of {sorted(ENGINES)}")
        check_target(self.live, self.endpoint_url, "Azure")

    @property
    def kind(self) -> str:
        return "azure" if self.live else "floci-az"

    @property
    def base(self) -> str:
        return MANAGEMENT if self.live else self.endpoint_url.rstrip("/")

    def servers_path(self) -> str:
        provider = ENGINES[self.engine][0]
        return (f"/subscriptions/{self.subscription}/resourceGroups/{self.resource_group}"
                f"/providers/{provider}/flexibleServers")

    @property
    def api_version(self) -> str:
        return ENGINES[self.engine][1]

    def client(self) -> JsonClient:
        if not self.live:
            return JsonClient()
        from azure.identity import DefaultAzureCredential  # optional dependency

        credential = DefaultAzureCredential()
        return JsonClient(lambda: credential.get_token("https://management.azure.com/.default").token)


def sku_shape(sku: str) -> dict:
    """vCPU (and memory for D and E series) from a flexible server SKU such as Standard_D16ds_v4."""
    match = re.fullmatch(r"Standard_([A-Z])(\d+)([a-z]*)_?(v\d+)?", sku or "")
    if not match:
        return {}
    series, vcpu = match.group(1), int(match.group(2))
    shape = {"vcpu": vcpu}
    if series in {"D", "E"}:
        shape["memory_gib"] = vcpu * (4 if series == "D" else 8)
    return shape


def price_series(sku: str) -> str | None:
    """The VM series token the retail price list uses in product names: Standard_D16ds_v4 -> Ddsv4."""
    match = re.fullmatch(r"Standard_([A-Z])\d+([a-z]*)_?(v\d+)?", sku or "")
    return f"{match.group(1)}{match.group(2)}{match.group(3) or ''}" if match else None


def _node(server: dict, role: str) -> dict:
    props = server.get("properties", {})
    sku = server.get("sku", {}).get("name", "")
    ha = (props.get("highAvailability") or {}).get("mode", "Disabled")
    return {"role": role, "instance_class": sku, **sku_shape(sku),
            "availability_zone_count": 2 if ha in {"ZoneRedundant", "SameZone"} else 1,
            "status": (props.get("state") or "").lower()}


def list_servers(target: AzureTarget, http=None) -> list[dict]:
    http = http or target.client()
    body = http.get(f"{target.base}{target.servers_path()}", {"api-version": target.api_version})
    return sorted(({"id": s["name"], "instance_class": s.get("sku", {}).get("name", ""), "engine": target.engine,
                    "status": (s.get("properties", {}).get("state") or "").lower(),
                    "replica_of": s.get("properties", {}).get("replicationRole") in {"Replica", "AsyncReplica"}}
                   for s in body.get("value", [])), key=lambda s: (s["replica_of"], s["id"]))


def read_topology(http, target: AzureTarget, server_name: str) -> tuple[dict, list[dict], list[str]]:
    params = {"api-version": target.api_version}
    path = f"{target.base}{target.servers_path()}"
    writer = http.get(f"{path}/{server_name}", params)
    source = writer.get("properties", {}).get("sourceServerResourceId")
    if source:  # asked for a replica: read the source server it follows
        writer = http.get(f"{path}/{source.rstrip('/').split('/')[-1]}", params)
    readers = sorted(http.get(f"{path}/{writer['name']}/replicas", params).get("value", []), key=lambda s: s["name"])
    props = writer.get("properties", {})
    storage = props.get("storage", {})
    data = {
        "engine": target.engine,
        "engine_version": props.get("version"),
        "tier": writer.get("sku", {}).get("tier"),
        "writer": _node(writer, "writer"),
        "readers": [_node(r, f"reader-{n}") for n, r in enumerate(readers, start=1)],
        "storage": {"type": storage.get("type") or storage.get("tier"), "allocated_gib": storage.get("storageSizeGB")},
        "high_availability": (props.get("highAvailability") or {}).get("mode", "Disabled"),
        "unknown_instance_classes": sorted({s.get("sku", {}).get("name", "") for s in [writer, *readers]
                                            if not sku_shape(s.get("sku", {}).get("name", ""))}),
    }
    return data, [writer, *readers], [writer["name"], *(r["name"] for r in readers)]


def read_metrics(http, target: AzureTarget, server: dict, start: datetime, end: datetime,
                 period_s: int) -> dict[str, tuple[str, list[dict]]]:
    names = ["cpu_percent", "memory_percent", "active_connections", ENGINES[target.engine][3]]
    minutes = max(1, period_s // 60)
    params = {"api-version": METRICS_API, "metricnames": ",".join(names),
              "timespan": f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end.strftime('%Y-%m-%dT%H:%M:%SZ')}",
              "interval": f"PT{minutes}M", "aggregation": "Average,Maximum"}
    body = http.get(f"{target.base}{server['id']}/providers/Microsoft.Insights/metrics", params)
    out = {name: (UNITS[name], []) for name in names}
    for metric in body.get("value", []):
        name = metric.get("name", {}).get("value")
        if name not in out:
            continue
        points = []
        for series in metric.get("timeseries", []):
            for d in series.get("data", []):
                if d.get("average") is None:
                    continue
                points.append({"Timestamp": parse_time(d["timeStamp"]), "Average": float(d["average"]),
                               "Maximum": float(d.get("maximum", d["average"]))})
        out[name] = (UNITS[name], sorted(points, key=lambda p: p["Timestamp"]))
    return out


def read_prices(http, target: AzureTarget, region: str, sku: str, max_pages: int = 20) -> dict | None:
    """On-demand compute price per vCore-hour for the server's series, times its vCores. None when not listed."""
    series, shape = price_series(sku), sku_shape(sku)
    if not series or not shape:
        return None
    service = ENGINES[target.engine][2]
    params = {"$filter": f"serviceName eq '{service}' and armRegionName eq '{region}' and priceType eq 'Consumption'"}
    url, found = RETAIL_PRICES, None
    for _ in range(max_pages):
        page = http.get(url, params)
        for item in page.get("Items", []):
            text = f"{item.get('productName', '')} {item.get('skuName', '')}"
            if (series.lower() in text.lower() and "vcore" in item.get("meterName", "").lower()
                    and item.get("unitOfMeasure") == "1 Hour" and "flexible" in item.get("productName", "").lower()
                    and item.get("unitPrice")):
                price = float(item["unitPrice"])
                found = price if found is None else min(found, price)
        url, params = page.get("NextPageLink"), None
        if found is not None or not url:
            break
    if found is None:
        return None
    return {"per_vcore_hour": found, "vcores": shape["vcpu"], "instance_hourly": round(found * shape["vcpu"], 4),
            "series": series, "region": region, "currency": "USD"}


def read_month_to_date_cost(http, target: AzureTarget, now: datetime) -> dict:
    service = ENGINES[target.engine][2]
    body = {"type": "ActualCost", "timeframe": "MonthToDate",
            "dataset": {"granularity": "None", "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                        "filter": {"dimensions": {"name": "ServiceName", "operator": "In", "values": [service]}}}}
    response = http.post(f"{target.base}/subscriptions/{target.subscription}/providers/Microsoft.CostManagement/query",
                         body, {"api-version": COST_API})
    props = response.get("properties", {})
    columns = [c.get("name") for c in props.get("columns", [])]
    total, currency = 0.0, "USD"
    for row in props.get("rows", []):
        record = dict(zip(columns, row, strict=False))
        total += float(record.get("Cost") or record.get("PreTaxCost") or 0)
        currency = record.get("Currency", currency)
    start = now.replace(day=1).date()
    return {"service": service, "period_start": start.isoformat(), "period_end_exclusive": (now.date() + timedelta(days=1)).isoformat(),
            "month_to_date": round(total, 2), "currency": currency, "metric": "ActualCost"}


def import_azure(target: AzureTarget, server_name: str, hours: float = 24.0, period_s: int = 300,
                 label: str | None = None, now: datetime | None = None, http=None, prices_http=None,
                 include_cost: bool = True) -> CloudImport:
    """Read one flexible server and its read replicas into evidence items. `http` lets tests pass recorded responses."""
    http = http or target.client()
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = now - timedelta(hours=hours)
    result = CloudImport()

    topology, servers, identifiers = read_topology(http, target, server_name)
    tag, name = tag_for(label, identifiers, "AZURE")
    common = dict(provenance=Provenance.OBSERVED, synthetic=not target.live, environment="import",
                  caveats=[LIVE_CAVEAT if target.live else EMULATOR_CAVEAT])
    service = ENGINES[target.engine][2]
    result.items.append(EvidenceItem(
        id=f"EV-AZ-TOPO-{tag}", kind=EvidenceKind.TOPOLOGY, title=f"{service} flexible server topology ({name})",
        source=f"{target.kind}:{ENGINES[target.engine][0]}:flexibleServers.get", method="identifiers not stored",
        data=topology, **common))

    names = ["writer"] + [f"reader-{n}" for n in range(1, len(servers))]
    metrics_by_node: dict[str, dict] = {}
    writer_cpu: list[dict] = []
    try:
        for node_name, server in zip(names, servers, strict=True):
            raw = read_metrics(http, target, server, start, now, period_s)
            metrics_by_node[node_name] = {m: summarize_datapoints(points, unit, period_s) for m, (unit, points) in raw.items()}
            if node_name == "writer":
                writer_cpu = raw["cpu_percent"][1]
    except CloudApiError as exc:
        result.skipped.append(f"metrics: Azure Monitor answered {exc.status}"
                              + (" (the emulator does not serve metrics)" if not target.live else ""))
    window = dict(window_start=start, window_end=now)
    if writer_cpu:
        result.items.append(EvidenceItem(
            id=f"EV-AZ-CPU-{tag}", kind=EvidenceKind.METRIC_SERIES,
            title=f"Writer CPU utilization, last {hours:g} h in 15-minute slots ({name})",
            source=f"{target.kind}:insights:metrics", method=f"{period_s} s interval; per-slot average and maximum",
            data={"metric": "CPUUtilization", "unit": "Percent", **slot_series(writer_cpu)}, **window, **common))
    elif metrics_by_node:
        result.skipped.append("writer CPU series: Azure Monitor returned no points for the window")
    if metrics_by_node:
        result.items.append(EvidenceItem(
            id=f"EV-AZ-MET-{tag}", kind=EvidenceKind.METRIC_SUMMARY, title=f"Azure Monitor metrics, last {hours:g} h ({name})",
            source=f"{target.kind}:insights:metrics", method=f"{period_s} s interval",
            data={"nodes": metrics_by_node}, **window, **common))

    region = (servers[0].get("location") or "").replace(" ", "").lower()
    sku = topology["writer"]["instance_class"]
    if not target.live:
        result.skipped.append("prices: read only with --live (the emulator has no retail price list)")
    else:
        try:
            price = read_prices(prices_http or JsonClient(), target, region, sku)
        except CloudApiError as exc:
            price = None
            result.skipped.append(f"prices: the retail price API answered {exc.status}")
        if price:
            result.items.append(EvidenceItem(
                id=f"EV-AZ-RATE-{tag}", kind=EvidenceKind.RATE_CARD, title=f"On-demand {service} compute price ({name})",
                source=f"azure:retail-prices:{price['series']}",
                method=f"{price['series']} series, {region}, on-demand: price per vCore-hour × vCores",
                data={"currency": price["currency"], "instance_hourly": {sku: price["instance_hourly"]}, "hours_per_month": 730,
                      "per_vcore_hour": price["per_vcore_hour"], "vcores": price["vcores"],
                      "source_note": "List compute price only; storage, backup, I/O and reservations are not included."},
                **common))
        elif not any(s.startswith("prices") for s in result.skipped):
            result.skipped.append(f"prices: no on-demand vCore price listed for {sku} in {region}")

    if include_cost and not target.live:
        result.skipped.append("month-to-date cost: read only with --live (the emulator has no cost data)")
    elif include_cost:
        try:
            cost = read_month_to_date_cost(http, target, now)
            result.items.append(EvidenceItem(
                id=f"EV-AZ-COST-{tag}", kind=EvidenceKind.METRIC_SUMMARY, title=f"{service} cost this month so far ({name})",
                source="azure:costmanagement:query", method="ActualCost, whole subscription, this database service only",
                data=cost, **common))
        except CloudApiError as exc:
            result.skipped.append(f"month-to-date cost: Cost Management answered {exc.status}")

    result.items[0].data["not_imported"] = list(result.skipped)
    return result
