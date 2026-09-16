"""Temporary: find which Pricing GetProducts requests the emulator accepts."""
from capacitylab.aws_import import AwsTarget, _on_demand_usd

pr = AwsTarget().client("pricing", "us-east-1")
try:
    svc = pr.describe_services(ServiceCode="AmazonRDS")
    print("attributes", svc["Services"][0].get("AttributeNames", [])[:60])
except Exception as exc:
    print("describe_services FAIL", str(exc)[:200])
for field in ("databaseEngine", "deploymentOption", "regionCode", "location", "instanceType"):
    try:
        vals = pr.get_attribute_values(ServiceCode="AmazonRDS", AttributeName=field, MaxResults=20)
        print("values", field, [v["Value"] for v in vals.get("AttributeValues", [])][:12])
    except Exception as exc:
        print("values", field, "FAIL", str(exc)[:160])
base = [{"Type": "TERM_MATCH", "Field": "instanceType", "Value": "db.r6g.2xlarge"}]
cases = {
    "instance_only": dict(Filters=base),
    "instance_only_max10": dict(Filters=base, MaxResults=10),
    "engine": dict(Filters=base + [{"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": "MySQL"}]),
    "regionCode": dict(Filters=base + [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": "us-east-1"}]),
    "location": dict(Filters=base + [{"Type": "TERM_MATCH", "Field": "location", "Value": "US East (N. Virginia)"}]),
    "deployment": dict(Filters=base + [{"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": "Single-AZ"}]),
}
for name, kw in cases.items():
    try:
        r = pr.get_products(ServiceCode="AmazonRDS", **kw)
        items = r.get("PriceList", [])
        print(name, "ok", len(items), [_on_demand_usd(x) for x in items[:3]], str(items[0])[:300] if items else "")
    except Exception as exc:
        print(name, "FAIL", str(exc)[:200])
