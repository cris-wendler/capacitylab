"""Temporary: find which PutMetricData shapes the emulator accepts."""
from datetime import UTC, datetime, timedelta

from capacitylab.aws_import import AwsTarget

cw = AwsTarget().client("cloudwatch")
print("protocol", cw.meta.service_model.protocol, getattr(cw.meta.service_model, "resolved_protocol", None))
now = datetime.now(UTC).replace(microsecond=0)
dims = [{"Name": "DBInstanceIdentifier", "Value": "probe"}]
cases = {
    "value_no_ts": [{"MetricName": "P1", "Dimensions": dims, "Value": 1.0}],
    "value_ts": [{"MetricName": "P2", "Dimensions": dims, "Value": 1.0, "Timestamp": now}],
    "value_ts_unit": [{"MetricName": "P3", "Dimensions": dims, "Value": 1.0, "Timestamp": now, "Unit": "Percent"}],
    "stats": [{"MetricName": "P4", "Dimensions": dims, "Timestamp": now, "Unit": "Percent",
               "StatisticValues": {"SampleCount": 5, "Sum": 50.0, "Minimum": 8.0, "Maximum": 12.0}}],
    "batch_20": [{"MetricName": "P5", "Dimensions": dims, "Value": float(i), "Timestamp": now - timedelta(minutes=5 * i)}
                 for i in range(20)],
}
for name, data in cases.items():
    try:
        cw.put_metric_data(Namespace="AWS/RDS", MetricData=data)
        print(name, "ok")
    except Exception as exc:
        print(name, "FAIL", str(exc)[:160])
r = cw.get_metric_statistics(Namespace="AWS/RDS", MetricName="P5", Dimensions=dims, StartTime=now - timedelta(hours=3),
                             EndTime=now + timedelta(minutes=1), Period=300, Statistics=["Average", "Maximum"])
print("readback P5", len(r.get("Datapoints", [])))
