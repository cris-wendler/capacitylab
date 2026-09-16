"""Tenant entitlements: what each tenant's plan guarantees, against the CPU share its workload takes.

Consumption is modeled from the scenario workload (calls per second per tenant times CPU per call), at baseline and
during each campaign window. Plans come from tenant entitlement evidence. Nothing here enforces a limit: it shows where
a plan and the workload disagree, and which levers exist to close the gap.
"""

from __future__ import annotations

from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.evidence.models import EvidenceKind
from capacitylab.scenarios.models import Scenario

OVER_RATIO = 1.25  # using more than 125% of the guaranteed share
SQUEEZED_RATIO = 0.6  # getting less than 60% of the guaranteed share

LEVERS = (
    "Levers, most to least contained: cap the campaign tenant's concurrency for campaign-only statements in the "
    "application's per-tenant connection pool (with a shared schema, tenants usually share one database account, so "
    "the database cannot tell them apart); run batch and reporting work at lower thread priority with MySQL 8.0 "
    "resource groups where the engine allows it (managed MySQL variants may not); or buy capacity for the window."
)


def _cpu_by_tenant(scenario: Scenario, multipliers: dict[str, float]) -> dict[str, float]:
    """CPU milliseconds per second of wall time, per tenant, with campaign multipliers on campaign-sensitive work."""
    out: dict[str, float] = {}
    for f in scenario.workload.fingerprints:
        for tenant, qps in f.base_qps_by_tenant.items():
            factor = multipliers.get(tenant, 1.0) if f.campaign_sensitive else 1.0
            out[tenant] = out.get(tenant, 0.0) + qps * f.cpu_ms_per_exec * factor
    return out


def _shares(cpu: dict[str, float]) -> dict[str, float]:
    total = sum(cpu.values()) or 1.0
    return {tenant: round(100 * value / total, 1) for tenant, value in cpu.items()}


def status(share_pct: float, entitled_pct: float) -> str:
    if not entitled_pct:
        return "no plan"
    ratio = share_pct / entitled_pct
    if ratio > OVER_RATIO:
        return "over"
    if ratio < SQUEEZED_RATIO:
        return "squeezed"
    return "within"


def tenant_entitlement_review(scenario: Scenario, bundle: EvidenceBundle,
                              viewer_tenant: str | None = None) -> tuple[dict, list[str]]:
    """Per-tenant plan versus modeled CPU share, at baseline and during each campaign. Raises LookupError without plans."""
    plans_item = bundle.first(EvidenceKind.TENANT_ENTITLEMENTS)
    if plans_item is None:
        raise LookupError("no tenant entitlement evidence in this scenario")
    plans: dict = plans_item.data.get("by_tenant", {})
    baseline = _shares(_cpu_by_tenant(scenario, {}))
    windows = []
    for event in scenario.events_of("campaign"):
        multiplier = float(scenario.assumption(event.multiplier_assumption).value)
        windows.append((event, multiplier, _shares(_cpu_by_tenant(scenario, {event.tenant: multiplier}))))

    by_tenant: dict[str, dict] = {}
    for tenant, plan in plans.items():
        if viewer_tenant is not None and tenant != viewer_tenant:
            continue
        entitled = float(plan.get("entitled_cpu_share_pct") or 0)
        by_tenant[tenant] = {
            "tier": plan.get("tier"),
            "monthly_contract_usd": plan.get("monthly_contract_usd"),
            "entitled_cpu_share_pct": entitled,
            "baseline_cpu_share_pct": baseline.get(tenant, 0.0),
            "baseline_status": status(baseline.get(tenant, 0.0), entitled),
            "during_campaigns": [
                {"campaign_tenant": event.tenant, "window": f"{event.start}-{event.end}",
                 "cpu_share_pct": shares.get(tenant, 0.0), "status": status(shares.get(tenant, 0.0), entitled)}
                for event, _multiplier, shares in windows
            ],
        }
    return {
        "basis": ("Modeled from the scenario workload: calls per second per tenant times CPU per call, with each "
                  "campaign's multiplier applied to its tenant's campaign-sensitive statements."),
        "thresholds": {"over_above_ratio": OVER_RATIO, "squeezed_below_ratio": SQUEEZED_RATIO},
        "campaigns": [{"tenant": event.tenant, "window": f"{event.start}-{event.end}", "multiplier": multiplier,
                       "assumption_id": event.multiplier_assumption} for event, multiplier, _ in windows],
        "by_tenant": by_tenant,
        "levers": LEVERS,
    }, [plans_item.id]
