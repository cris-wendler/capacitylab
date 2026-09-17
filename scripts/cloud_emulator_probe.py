"""Temporary: record what floci-gcp and floci-az accept and return, to fix the imports against real responses."""

import json
import sys
import time

from capacitylab.cloud_common import CloudApiError, JsonClient

http = JsonClient(lambda: "test-token")


def show(label, fn):
    try:
        body = fn()
        print(f"OK   {label}: {json.dumps(body)[:700]}")
        return body
    except CloudApiError as exc:
        print(f"ERR  {label}: {exc}"[:500])
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {label}: {type(exc).__name__} {exc}"[:500])
    return None


def gcp():
    base = "http://127.0.0.1:4588"
    project = "floci-local"
    show("gcp health", lambda: http.get(f"{base}/health"))
    body = {"name": "demo-pg", "databaseVersion": "POSTGRES_16", "region": "us-central1",
            "settings": {"tier": "db-custom-2-7680", "availabilityType": "REGIONAL", "dataDiskSizeGb": "100"}}
    for prefix in ("/sqladmin/v1", "/v1", "/sql/v1beta4"):
        show(f"gcp create {prefix}", lambda p=prefix: http.post(f"{base}{p}/projects/{project}/instances", body))
    time.sleep(3)
    for prefix in ("/sqladmin/v1", "/v1"):
        show(f"gcp get {prefix}", lambda p=prefix: http.get(f"{base}{p}/projects/{project}/instances/demo-pg"))
        show(f"gcp list {prefix}", lambda p=prefix: http.get(f"{base}{p}/projects/{project}/instances"))
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for metric in ("cloudsql.googleapis.com/database/cpu/utilization", "custom.googleapis.com/capacitylab/cpu"):
        series = {"timeSeries": [{"metric": {"type": metric},
                                  "resource": {"type": "cloudsql_database",
                                               "labels": {"project_id": project, "database_id": f"{project}:demo-pg",
                                                          "region": "us-central1"}},
                                  "points": [{"interval": {"endTime": now}, "value": {"doubleValue": 0.42}}]}]}
        for prefix in ("/monitoring/v3", "/v3"):
            show(f"gcp write {prefix} {metric}", lambda p=prefix, b=series: http.post(f"{base}{p}/projects/{project}/timeSeries", b))
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))
    for aligner in ("ALIGN_MEAN", None):
        params = {"filter": f'metric.type = "cloudsql.googleapis.com/database/cpu/utilization" AND resource.labels.database_id = "{project}:demo-pg"',
                  "interval.startTime": start, "interval.endTime": end}
        if aligner:
            params.update({"aggregation.alignmentPeriod": "300s", "aggregation.perSeriesAligner": aligner})
        show(f"gcp list series {aligner}", lambda p=params: http.get(f"{base}/monitoring/v3/projects/{project}/timeSeries", p))


def azure():
    base = "http://127.0.0.1:4577"
    sub, rg = "demo-subscription", "capacitylab-demo"
    show("az rg put", lambda: http.put(f"{base}/subscriptions/{sub}/resourceGroups/{rg}", {"location": "eastus"},
                                       {"api-version": "2021-04-01"}))
    body = {"location": "eastus", "sku": {"name": "Standard_D2ds_v4", "tier": "GeneralPurpose"},
            "properties": {"administratorLogin": "capadmin", "administratorLoginPassword": "Local-only-123!", "version": "8.0.21",
                           "storage": {"storageSizeGB": 32}, "highAvailability": {"mode": "Disabled"}}}
    path = f"{base}/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.DBforMySQL/flexibleServers"
    for version in ("2023-12-30", "2021-12-01-preview"):
        show(f"az create {version}", lambda v=version: http.put(f"{path}/demo-mysql", body, {"api-version": v}))
    for _ in range(40):
        got = show("az get 2023-12-30", lambda: http.get(f"{path}/demo-mysql", {"api-version": "2023-12-30"}))
        state = ((got or {}).get("properties") or {}).get("state")
        if state and state.lower() == "ready":
            break
        time.sleep(3)
    show("az get preview", lambda: http.get(f"{path}/demo-mysql", {"api-version": "2021-12-01-preview"}))
    show("az list", lambda: http.get(path, {"api-version": "2023-12-30"}))
    show("az replicas", lambda: http.get(f"{path}/demo-mysql/replicas", {"api-version": "2023-12-30"}))
    rid = f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.DBforMySQL/flexibleServers/demo-mysql"
    show("az metrics", lambda: http.get(f"{base}{rid}/providers/Microsoft.Insights/metrics",
                                        {"api-version": "2023-10-01", "metricnames": "cpu_percent", "interval": "PT5M"}))
    show("az cost", lambda: http.post(f"{base}/subscriptions/{sub}/providers/Microsoft.CostManagement/query",
                                      {"type": "ActualCost", "timeframe": "MonthToDate"}, {"api-version": "2023-11-01"}))


if __name__ == "__main__":
    {"gcp": gcp, "azure": azure}[sys.argv[1]]()
