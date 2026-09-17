# SPDX-License-Identifier: AGPL-3.0-or-later
"""Findings computed from a scenario's evidence alone, before any review runs.

Each finding names the evidence it rests on and says what it is: a measurement, a model output, or a
gap where nobody has measured anything. Nothing here is produced by a language model.
"""

from __future__ import annotations

from pydantic import BaseModel

from capacitylab.capacity.attribution import attribute
from capacitylab.capacity.entitlements import LEVERS, tenant_entitlement_review
from capacitylab.capacity.options import build_context, evaluate_all
from capacitylab.diagnostics.analysis import bottleneck_classifier, table_growth_review, tenant_skew
from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.evidence.models import EvidenceKind
from capacitylab.scenarios.models import Scenario

SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}
AREAS = ("capacity", "high_availability", "data_growth", "contention", "tenant_share")


class Finding(BaseModel):
    id: str
    area: str
    severity: str  # high | medium | info
    headline: str
    detail: str
    recommendation: str
    evidence_ids: list[str] = []
    basis: str = "modeled"  # observed | modeled | measured | gap


def _first(bundle: EvidenceBundle, kind: EvidenceKind):
    items = list(bundle.by_kind(kind))
    return items[0] if items else None


def _cause_finding(scenario: Scenario, bundle: EvidenceBundle) -> list[Finding]:
    """What is behind the breaching slots, so the remedy can follow the cause instead of defaulting to capacity."""
    ctx = build_context(scenario, bundle)
    result = attribute(ctx)
    if not result.causes or not result.slots:
        return []
    worst = result.worst
    top = ", ".join(f"{s.name} {s.pct:g}%" for s in worst.by_statement[:3])
    tenants = ", ".join(f"{s.name} {s.pct:g}%" for s in worst.by_tenant[:2])
    return [Finding(
        id="FND-CAP-CAUSE", area="capacity", severity="medium",
        headline="What is driving the busiest slots: " + ", ".join(f"{c.subject} {c.pct:g}%" for c in result.causes),
        detail=(f"Across {len(result.slots)} slots ({result.window}), the worst is {worst.slot} at "
                f"{worst.utilization_pct:g}% of the node. By statement: {top}. By tenant: {tenants}."),
        recommendation="; ".join(f"{c.remedy} ({c.subject}, {c.pct:g}%)" for c in result.causes)
                       + ". Shares are modeled from the forecast rates and each statement's cost per execution.",
        evidence_ids=list(ctx.evidence_ids))]


def _capacity(scenario: Scenario, bundle: EvidenceBundle) -> list[Finding]:
    outcomes = list(evaluate_all(build_context(scenario, bundle)))
    keep = next((o for o in outcomes if o.option_kind == "keep"), None)
    if keep is None:
        return []
    threshold = scenario.utilization_threshold_pct
    node = scenario.cluster.evaluated_node
    out: list[Finding] = []
    if keep.peak_utilization_pct >= 100:
        best = min((o for o in outcomes if o.option_id != keep.option_id),
                   key=lambda o: (o.total_slo_breach_slots, o.peak_utilization_pct), default=None)
        out.append(Finding(
            id="FND-CAP-SATURATED", area="capacity", severity="high",
            headline=f"The {node} runs out of CPU in this window",
            detail=(f"Modeled peak {keep.peak_utilization_pct:g}% at {keep.peak_slot} with the current instance, "
                    f"{keep.slots_over_threshold} slots above the {threshold:g}% threshold and "
                    f"{keep.total_slo_breach_slots} slots breaching an objective."),
            recommendation=(f"Do not keep capacity unchanged. Lowest-breach option modeled: {best.label} "
                            f"(peak {best.peak_utilization_pct:g}%)." if best else "Reduce work or add capacity."),
            evidence_ids=list(keep.evidence_ids)))
    elif keep.slots_over_threshold:
        out.append(Finding(
            id="FND-CAP-THIN", area="capacity", severity="medium",
            headline=f"The {node} has little headroom in this window",
            detail=(f"Modeled peak {keep.peak_utilization_pct:g}% at {keep.peak_slot}, "
                    f"{keep.slots_over_threshold} slots above the {threshold:g}% threshold, "
                    f"{keep.total_slo_breach_slots} breach slots."),
            recommendation="Keep is viable but leaves no margin for forecast error; compare against the other options.",
            evidence_ids=list(keep.evidence_ids)))
    elif keep.peak_utilization_pct < threshold / 2:
        topo = _first(bundle, EvidenceKind.TOPOLOGY)
        # Topology stores the writer as an object and readers as a list; the evaluated node can be either.
        instance = {}
        if topo:
            instance = topo.data.get("writer") or {} if node == "writer" else next(iter(topo.data.get("readers") or []), {})
        memory = instance.get("memory_gib")
        working_set = scenario.cluster.working_set_gib
        pool = round(memory * scenario.cluster.buffer_pool_fraction_assumption, 1) if memory else None
        fits = working_set is None or (pool is not None and working_set <= pool / 2)
        out.append(Finding(
            id="FND-CAP-OVERSIZED", area="capacity", severity="info" if fits else "medium",
            headline=f"The {node} is larger than this workload needs on CPU",
            detail=(f"Modeled peak {keep.peak_utilization_pct:g}% against a {threshold:g}% threshold."
                    + (f" Working set {working_set:g} GiB against a modeled buffer pool of {pool:g} GiB on the current "
                       f"class." if working_set and pool else "")),
            recommendation=("A smaller class is worth pricing, but check memory before CPU: a smaller class halves the "
                            "buffer pool, and the working set has to keep fitting."
                            if not fits else "A smaller class is worth pricing."),
            evidence_ids=list(keep.evidence_ids)))
    return out


def _availability(scenario: Scenario, bundle: EvidenceBundle) -> list[Finding]:
    topo, history, slo = (_first(bundle, k) for k in
                          (EvidenceKind.TOPOLOGY, EvidenceKind.RELIABILITY_HISTORY, EvidenceKind.SLO))
    out: list[Finding] = []
    readers = (topo.data.get("readers") or []) if topo else []
    if topo and not readers:
        out.append(Finding(
            id="FND-HA-NO-READER", area="high_availability", severity="high", basis="observed",
            headline="No reader: the cluster has no failover target",
            detail=f"{topo.data.get('cluster_id', scenario.cluster.id)} has a writer and no reader instance.",
            recommendation="Losing the writer means downtime until a new instance is created. Add a reader.",
            evidence_ids=[topo.id]))
    if history:
        data = history.data
        if data.get("measured_failover_seconds") is None:
            tested = data.get("failover_tests")
            out.append(Finding(
                id="FND-HA-FAILOVER-UNMEASURED", area="high_availability", severity="high", basis="gap",
                headline="Failover duration has never been measured",
                detail=("No measured failover time for this cluster"
                        + (f", and {tested} failover tests have been run." if tested is not None else ".")),
                recommendation=("Any option that restarts or replaces the writer carries an unknown outage. Measure it "
                                "before planning around it, or prefer options that avoid a failover."),
                evidence_ids=[history.id]))
        elif data.get("measured_failover_seconds"):
            out.append(Finding(
                id="FND-HA-FAILOVER-KNOWN", area="high_availability", severity="info", basis="observed",
                headline=f"Failover took {data['measured_failover_seconds']:g} s when it last happened",
                detail=f"{data.get('unplanned_failovers', 0)} unplanned failover(s) recorded.",
                recommendation="Use the measured duration when weighing options that replace an instance.",
                evidence_ids=[history.id]))
        if data.get("reader_is_failover_target") and (topo or {}) and topo.data.get("reader_receives_application_reads"):
            out.append(Finding(
                id="FND-HA-READER-DOUBLE-DUTY", area="high_availability", severity="medium", basis="observed",
                headline="The reader both serves application reads and is the failover target",
                detail="Resizing or losing the reader takes read capacity and the failover target at the same time.",
                recommendation="Size the reader for the failover role, not only for its read workload.",
                evidence_ids=[topo.id, history.id]))
    if slo and (slo.data.get("error_budget_remaining_pct") or 100) < 50:
        out.append(Finding(
            id="FND-HA-ERROR-BUDGET", area="high_availability", severity="medium", basis="observed",
            headline=f"{slo.data['error_budget_remaining_pct']:g}% of the error budget is left",
            detail=f"Availability target {slo.data.get('availability_target_pct')}%.",
            recommendation="Prefer reversible changes; a failed change spends budget that is already short.",
            evidence_ids=[slo.id]))
    return out


def _data_growth(bundle: EvidenceBundle) -> list[Finding]:
    try:
        review, ids = table_growth_review(bundle)
    except LookupError:
        return []
    out: list[Finding] = []
    for table in review["tables"]:
        growth = table.get("growth_pct_per_week")
        if table["growth_sensitive_statements"]:
            out.append(Finding(
                id=f"FND-GROWTH-{table['table'].upper()}", area="data_growth", severity="medium", basis="observed",
                headline=f"{table['table']} grows and feeds statements that read more as it grows",
                detail=(f"{table.get('rows'):,} rows, {table.get('data_gib')} GiB"
                        + (f", {growth:g}% per week" if growth else "")
                        + f". Growth-sensitive statements: {', '.join(table['growth_sensitive_statements'])}."),
                recommendation="Bound these statements (index or rewrite) before the table gets larger; archiving alone "
                               "will not fix their latency.",
                evidence_ids=list(ids)))
        elif (table.get("data_gib") or 0) >= 100:
            out.append(Finding(
                id=f"FND-GROWTH-{table['table'].upper()}", area="data_growth", severity="info", basis="observed",
                headline=f"{table['table']} is large but its accesses are bounded",
                detail=f"{table.get('rows'):,} rows, {table.get('data_gib')} GiB"
                       + (f", {growth:g}% per week" if growth else "") + ". Reads are by primary key or inserts only.",
                recommendation="Size alone is a storage-cost question here, not a latency problem. Do not optimize it "
                               "for performance without a statement that says otherwise.",
                evidence_ids=list(ids)))
    return out


def _contention(bundle: EvidenceBundle) -> list[Finding]:
    try:
        result, ids = bottleneck_classifier(bundle)
    except LookupError:
        return []
    severity = {"high": "high", "medium": "medium", "low": "info"}
    return [Finding(
        id=f"FND-BOTTLENECK-{f['resource'].upper().replace('_', '-')}", area="contention",
        severity=severity.get(f["severity"], "info"), basis="observed",
        headline=f"{f['resource'].replace('_', ' ').capitalize()} is a bottleneck signal in the observed window",
        detail=f["detail"],
        recommendation=("This is the resource to explain before adding capacity."
                        if f["resource"] == result["primary"] else "Secondary signal; check it after the primary one."),
        evidence_ids=list(ids)) for f in result["findings"]]


def _tenant_share(bundle: EvidenceBundle) -> list[Finding]:
    try:
        result, ids = tenant_skew(bundle)
    except LookupError:
        return []
    skewed = [f for f in result["fingerprints"] if f.get("skewed")]
    if not skewed:
        return []
    worst = max(skewed, key=lambda f: f.get("top_tenant_share_pct", 0))
    return [Finding(
        id="FND-TENANT-SKEW", area="tenant_share", severity="info", basis="observed",
        headline=f"One tenant drives {worst['top_tenant_share_pct']:g}% of calls for {worst['fingerprint_id']}",
        detail=f"{len(skewed)} statement(s) are dominated by a single tenant on a shared cluster.",
        recommendation="Work aimed at these statements benefits one tenant most; weigh that against what each tenant "
                       "is entitled to.",
        evidence_ids=list(ids))]


def _entitlements(scenario: Scenario, bundle: EvidenceBundle) -> list[Finding]:
    try:
        review, ids = tenant_entitlement_review(scenario, bundle)
    except LookupError:
        return []
    out: list[Finding] = []
    tenants = review["by_tenant"]
    for campaign in review["campaigns"]:
        rows = {t: next(c for c in r["during_campaigns"] if c["campaign_tenant"] == campaign["tenant"])
                for t, r in tenants.items()}
        squeezed = {t: c for t, c in rows.items() if c["status"] == "squeezed"}
        if squeezed:
            listed = ", ".join(f"{t} {c['cpu_share_pct']:g}% of {tenants[t]['entitled_cpu_share_pct']:g}%"
                               for t, c in sorted(squeezed.items()))
            out.append(Finding(
                id=f"FND-TENANT-SQUEEZED-{campaign['tenant'].upper()}", area="tenant_share", severity="high",
                headline=(f"During {campaign['tenant']}'s campaign, {len(squeezed)} tenant(s) get less than their plans "
                          f"guarantee"),
                detail=(f"Modeled CPU share against the plan's guaranteed share, {campaign['window']} at "
                        f"{campaign['multiplier']:g}× ({campaign['assumption_id']}): {listed}."),
                recommendation=f"Protect the guaranteed shares during the window. {LEVERS}",
                evidence_ids=list(ids)))
        owner = rows.get(campaign["tenant"])
        if owner and owner["status"] == "over":
            plan = tenants[campaign["tenant"]]
            out.append(Finding(
                id=f"FND-TENANT-OVER-{campaign['tenant'].upper()}", area="tenant_share", severity="medium",
                headline=(f"{campaign['tenant']} needs {owner['cpu_share_pct']:g}% of CPU during its campaign; its "
                          f"{plan['tier']} plan guarantees {plan['entitled_cpu_share_pct']:g}%"),
                detail=(f"Baseline use is {plan['baseline_cpu_share_pct']:g}%. The campaign multiplier is an "
                        f"assumption ({campaign['assumption_id']}), not a measurement."),
                recommendation=("Decide who pays for the burst: a plan that includes capacity for announced events, "
                                "or capacity bought for this window. Without either, the burst comes out of other "
                                "tenants' guaranteed shares."),
                evidence_ids=list(ids)))
    if not out and tenants and all(r["baseline_status"] == "within" for r in tenants.values()):
        out.append(Finding(
            id="FND-TENANT-WITHIN-PLANS", area="tenant_share", severity="info",
            headline="Every tenant's modeled use is within what its plan guarantees",
            detail=", ".join(f"{t} {r['baseline_cpu_share_pct']:g}% of {r['entitled_cpu_share_pct']:g}%"
                             for t, r in sorted(tenants.items())) + ".",
            recommendation="No plan conflicts to resolve; revisit when a tenant schedules a campaign.",
            evidence_ids=list(ids)))
    return out


def review_findings(scenario: Scenario, bundle: EvidenceBundle) -> list[Finding]:
    """Everything the evidence already says, ordered by severity. No review run, no model calls."""
    findings = (_capacity(scenario, bundle) + _cause_finding(scenario, bundle) + _availability(scenario, bundle)
                + _data_growth(bundle)
                + _contention(bundle) + _tenant_share(bundle) + _entitlements(scenario, bundle))
    return sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 3), AREAS.index(f.area)))
