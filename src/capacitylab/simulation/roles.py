# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stakeholder roles: responsibilities, constraints, evidence access, and permitted tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from capacitylab.evidence.bundle import Contradiction, EvidenceBundle, MissingEvidence
from capacitylab.evidence.models import EvidenceItem
from capacitylab.evidence.models import EvidenceKind as K
from capacitylab.scenarios.models import Scenario


class RoleId(StrEnum):
    DATABASE_ENGINEER = "database_engineer"
    APPLICATION_OWNER = "application_owner"
    RELIABILITY_ENGINEER = "reliability_engineer"
    FINOPS_ANALYST = "finops_analyst"
    TENANT_REPRESENTATIVE = "tenant_representative"
    SINGLE_AGENT = "single_agent"  # evaluation baseline only


@dataclass(frozen=True)
class RoleDefinition:
    id: RoleId
    title: str
    responsibilities: tuple[str, ...]
    constraints: tuple[str, ...]
    evidence_kinds: frozenset[K]
    tools: frozenset[str]
    tenant_scoped: bool = False
    priorities: tuple[str, ...] = field(default_factory=tuple)


ROLES: dict[RoleId, RoleDefinition] = {
    RoleId.DATABASE_ENGINEER: RoleDefinition(
        id=RoleId.DATABASE_ENGINEER,
        title="Database engineer",
        responsibilities=(
            "Attribute load to specific statements before accepting that capacity is the problem.",
            "Diagnose plans, indexes, cardinality estimates, statistics, locks, spills, and tenant skew.",
            "Propose index or rewrite changes with evidence, uncertainty, tradeoffs, validation, and rollback.",
        ),
        constraints=(
            "Must not conclude a table needs optimization because it is large.",
            "Sandbox measurements are local and relative; they are not production results.",
            "A rewrite is only an optimization if results are equivalent, including duplicates, NULLs, and tenant boundaries.",
        ),
        evidence_kinds=frozenset({K.TOPOLOGY, K.METRIC_SERIES, K.METRIC_SUMMARY, K.QUERY_DIGEST, K.QUERY_PLAN, K.SCHEMA,
                                  K.TABLE_STATS, K.WORKLOAD_FORECAST, K.EVENT_CALENDAR, K.BATCH_SCHEDULE, K.BATCH_RUN_LOG,
                                  K.EXPERIMENT_RESULT, K.CALCULATION}),
        tools=frozenset({"top_queries", "tenant_skew", "explain_query", "index_experiment", "rewrite_equivalence",
                         "row_estimate_check", "table_growth_review", "bottleneck_classifier", "capacity_forecast",
                         "lab_load_test", "percona_duplicate_keys"}),
        priorities=("fix the workload before buying capacity", "correctness of any rewrite", "measured evidence"),
    ),
    RoleId.APPLICATION_OWNER: RoleDefinition(
        id=RoleId.APPLICATION_OWNER,
        title="Application owner",
        responsibilities=(
            "Represent releases, campaign schedules, batch jobs, and application constraints.",
            "State which workload changes are possible and which are fixed commitments.",
        ),
        constraints=(
            "Cannot move customer-announced events.",
            "Batch jobs must still meet downstream deadlines.",
            "Behavior changes to application queries need a product decision.",
        ),
        evidence_kinds=frozenset({K.TOPOLOGY, K.EVENT_CALENDAR, K.RELEASE_CALENDAR, K.BATCH_SCHEDULE, K.BATCH_RUN_LOG, K.SLO,
                                  K.QUERY_DIGEST, K.WORKLOAD_FORECAST, K.TENANT_PROFILE, K.TENANT_ENTITLEMENTS,
                                  K.CALCULATION}),
        tools=frozenset({"top_queries", "batch_reschedule_check", "capacity_forecast", "cost_estimate",
                         "tenant_entitlement_review"}),
        priorities=("keep commitments to tenants", "respect change freezes", "minimal operational churn"),
    ),
    RoleId.RELIABILITY_ENGINEER: RoleDefinition(
        id=RoleId.RELIABILITY_ENGINEER,
        title="Reliability engineer",
        responsibilities=(
            "Protect service level objectives and the remaining error budget.",
            "Assess failover, resilience, and operational risk of each change.",
        ),
        constraints=(
            "Unmeasured failover behavior is a risk, not a zero.",
            "Headroom margins matter, not only whether a model shows zero breaches.",
        ),
        evidence_kinds=frozenset({K.TOPOLOGY, K.METRIC_SERIES, K.METRIC_SUMMARY, K.WORKLOAD_FORECAST, K.SLO,
                                  K.RELIABILITY_HISTORY, K.EVENT_CALENDAR, K.RELEASE_CALENDAR, K.BATCH_SCHEDULE,
                                  K.BATCH_RUN_LOG, K.EXPERIMENT_RESULT, K.TENANT_ENTITLEMENTS, K.CALCULATION}),
        tools=frozenset({"tenant_skew", "bottleneck_classifier", "batch_reschedule_check", "capacity_forecast",
                         "cost_estimate", "lab_load_test", "tenant_entitlement_review"}),
        priorities=("zero SLO breaches with margin", "fewest unmeasured risks", "reversibility"),
    ),
    RoleId.FINOPS_ANALYST: RoleDefinition(
        id=RoleId.FINOPS_ANALYST,
        title="FinOps analyst",
        responsibilities=(
            "Quantify cost of each option using the rate card and budget only.",
            "Expose recurring versus one-off costs and tradeoffs against risk.",
        ),
        constraints=(
            "Every amount must come from a cost calculation or rate card evidence.",
            "Illustrative rates must be called illustrative.",
        ),
        evidence_kinds=frozenset({K.TOPOLOGY, K.METRIC_SUMMARY, K.WORKLOAD_FORECAST, K.RATE_CARD, K.BUDGET, K.TABLE_STATS,
                                  K.EVENT_CALENDAR, K.EXPERIMENT_RESULT, K.TENANT_ENTITLEMENTS, K.CALCULATION}),
        tools=frozenset({"tenant_skew", "table_growth_review", "capacity_forecast", "cost_estimate",
                         "tenant_entitlement_review"}),
        priorities=("lowest recurring cost that meets objectives", "budget headroom", "avoid permanent drift"),
    ),
    RoleId.TENANT_REPRESENTATIVE: RoleDefinition(
        id=RoleId.TENANT_REPRESENTATIVE,
        title="Tenant representative (synthetic persona)",
        responsibilities=(
            "Speak for the focal tenant's workload characteristics and service expectations.",
            "Flag risks to the tenant's customer journeys.",
        ),
        constraints=(
            "This is an explicitly fictional persona, not a real customer.",
            "Sees only the focal tenant's data; other tenants' breakdowns are redacted.",
        ),
        evidence_kinds=frozenset({K.TENANT_PROFILE, K.TENANT_ENTITLEMENTS, K.EVENT_CALENDAR, K.SLO, K.CALCULATION}),
        tools=frozenset({"tenant_skew", "capacity_forecast", "tenant_entitlement_review"}),
        tenant_scoped=True,
        priorities=("latency of the tenant's journeys", "no disruption during the event"),
    ),
}

ROLES[RoleId.SINGLE_AGENT] = RoleDefinition(
    id=RoleId.SINGLE_AGENT,
    title="Single decision-maker (evaluation baseline)",
    responsibilities=tuple(r for d in ROLES.values() for r in d.responsibilities),
    constraints=tuple(dict.fromkeys(c for d in ROLES.values() if not d.tenant_scoped for c in d.constraints)),
    evidence_kinds=frozenset(k for d in ROLES.values() for k in d.evidence_kinds),
    tools=frozenset(t for d in ROLES.values() for t in d.tools),
    priorities=("meet objectives", "lowest risk", "lowest cost"),
)

# Reasoning effort per role, used when the provider is set to "auto". Roles whose turns mostly quote
# measurements run low, which keeps answers close to the evidence; roles that weigh tradeoffs between
# risk, cost and commitments get more room. Numbers are validated against cited evidence either way.
EFFORT: dict[RoleId, str] = {
    RoleId.DATABASE_ENGINEER: "low",
    RoleId.APPLICATION_OWNER: "low",
    RoleId.RELIABILITY_ENGINEER: "medium",
    RoleId.FINOPS_ANALYST: "low",
    RoleId.TENANT_REPRESENTATIVE: "low",
    RoleId.SINGLE_AGENT: "medium",
}

ROLE_ORDER = [
    RoleId.DATABASE_ENGINEER,
    RoleId.APPLICATION_OWNER,
    RoleId.RELIABILITY_ENGINEER,
    RoleId.FINOPS_ANALYST,
    RoleId.TENANT_REPRESENTATIVE,
]


def visible_evidence(role: RoleDefinition, bundle: EvidenceBundle, scenario: Scenario) -> list[EvidenceItem]:
    """Evidence the role may see, with tenant scoping and redaction applied."""
    out = []
    for item in bundle:
        if item.kind not in role.evidence_kinds:
            continue
        if role.tenant_scoped:
            if item.tenant_id is not None and item.tenant_id != scenario.focal_tenant:
                continue
            out.append(item.redacted_for_tenant(scenario.focal_tenant))
        else:
            out.append(item)
    return out


def gaps_for(role: RoleDefinition, bundle: EvidenceBundle) -> list[MissingEvidence]:
    if role.id == RoleId.SINGLE_AGENT:  # the baseline gets every gap the stakeholders collectively see
        return list(bundle.missing)
    return [g for g in bundle.missing if not g.relevant_roles or role.id.value in g.relevant_roles]


def visible_contradictions(bundle: EvidenceBundle, visible_ids: set[str]) -> list[Contradiction]:
    return [c for c in bundle.contradictions() if all(i in visible_ids for i in c.evidence_ids)]
