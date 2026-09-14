"""Cross-reference validation of a scenario against its evidence bundle."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from capacitylab.capacity.catalog import CATALOG
from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.evidence.models import EvidenceKind
from capacitylab.scenarios.models import Scenario, hhmm_to_minutes


class ValidationIssue(BaseModel):
    level: Literal["error", "warning"]
    path: str
    message: str


def validate_scenario(scenario: Scenario, bundle: EvidenceBundle) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    def error(path: str, message: str) -> None:
        issues.append(ValidationIssue(level="error", path=path, message=message))

    def warning(path: str, message: str) -> None:
        issues.append(ValidationIssue(level="warning", path=path, message=message))

    h = scenario.horizon
    if h.end_minutes <= h.start_minutes:
        error("horizon", "horizon end must be after start")
    if (h.end_minutes - h.start_minutes) % h.slot_minutes:
        warning("horizon", "horizon length is not a whole number of slots")

    for name in [scenario.cluster.writer_instance, *scenario.cluster.reader_instances]:
        if name not in CATALOG:
            error("cluster", f"unknown instance class {name}")

    tenant_ids = {t.id for t in scenario.tenants}
    if len(tenant_ids) != len(scenario.tenants):
        error("tenants", "duplicate tenant ids")
    if scenario.focal_tenant not in tenant_ids:
        error("focal_tenant", f"focal tenant {scenario.focal_tenant} is not declared")

    assumption_ids = {a.id for a in scenario.assumptions}
    for a in scenario.assumptions:
        for evidence_id in a.evidence_ids:
            if evidence_id not in bundle:
                error(f"assumptions.{a.id}", f"references missing evidence {evidence_id}")

    fingerprint_ids = {f.id for f in scenario.workload.fingerprints}
    for f in scenario.workload.fingerprints:
        for tenant in f.base_qps_by_tenant:
            if tenant not in tenant_ids:
                error(f"workload.{f.id}", f"rate for undeclared tenant {tenant}")
        if f.cpu_ms_per_exec <= 0:
            error(f"workload.{f.id}", "cpu_ms_per_exec must be positive")

    for e in scenario.events:
        path = f"events.{e.id}"
        starts = [e.start] + ([e.end] if e.kind == "campaign" else [])
        for t in starts:
            m = hhmm_to_minutes(t)
            if not h.start_minutes <= m <= h.end_minutes:
                warning(path, f"time {t} is outside the horizon")
        if e.kind == "campaign":
            if e.tenant not in tenant_ids:
                error(path, f"campaign tenant {e.tenant} is not declared")
            if hhmm_to_minutes(e.end) <= hhmm_to_minutes(e.start):
                error(path, "campaign must end after it starts")
        if getattr(e, "multiplier_assumption", None) and e.multiplier_assumption not in assumption_ids:
            error(path, f"unknown assumption {e.multiplier_assumption}")
        if e.kind == "batch_job":
            for fid in e.contended_fingerprints:
                if fid not in fingerprint_ids:
                    error(path, f"unknown fingerprint {fid}")

    option_ids = {o.id for o in scenario.options}
    if len(option_ids) != len(scenario.options):
        error("options", "duplicate option ids")
    kinds = {o.kind for o in scenario.options}
    if "keep" not in kinds:
        warning("options", "no 'keep' option; the status quo should always be compared")
    for o in scenario.options:
        path = f"options.{o.id}"
        target = o.params.get("target_instance")
        if target is not None and target not in CATALOG:
            error(path, f"unknown instance class {target}")
        if o.kind == "combined":
            for component in o.params.get("components", []):
                if component not in option_ids:
                    error(path, f"unknown component option {component}")
                if component == o.id:
                    error(path, "combined option cannot include itself")
        if o.kind == "reschedule_batch" and not scenario.events_of("batch_job"):
            error(path, "reschedule option without a batch_job event")
        if o.kind == "scale_temporary":
            for key in ("target_instance", "window_start", "window_end"):
                if key not in o.params:
                    error(path, f"missing parameter {key}")

    for item in bundle:
        if item.tenant_id is not None and item.tenant_id not in tenant_ids:
            error(f"evidence.{item.id}", f"tenant {item.tenant_id} is not declared in the scenario")

    for required in (EvidenceKind.TOPOLOGY, EvidenceKind.SLO, EvidenceKind.RATE_CARD):
        if not bundle.by_kind(required):
            warning("evidence", f"no {required.value} evidence; related stakeholders will flag the gap")

    for contradiction in bundle.contradictions():
        warning("evidence", f"contradiction on {contradiction.key}: {', '.join(contradiction.evidence_ids)}")
    return issues
