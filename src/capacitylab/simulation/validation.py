"""Checks every stakeholder turn for traceability: citations, access, grounded numbers, tool permissions."""

from __future__ import annotations

import json
import re

from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.evidence.models import EvidenceKind, iter_numbers
from capacitylab.scenarios.models import Scenario
from capacitylab.simulation.roles import RoleDefinition, RoleId
from capacitylab.simulation.schema import TurnDraft, ValidationFinding

# Identifiers, dates, and clock times are not measurements.
_MASK_RE = re.compile(
    r"\b(?:EV|QF|OPT|GAP|IDX|RW|SLO|A|TC|PROP|EVT)-[A-Z0-9-]+\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}:\d{2}\b"
    r"|\bdb\.[a-z0-9]+\.[a-z0-9]+\b"
    r"|\b[Rr]\d\b"
    r"|\b(?:[Rr]elease|[Vv]ersion|v)\s*\d+(?:\.\d+)+\b"
)
_NUMBER_RE = re.compile(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)(\s*(?:%|ms|s\b|x\b|GiB|MiB|bytes|USD|rows|cores))?")


def extract_numbers(text: str) -> list[tuple[float, bool]]:
    """Return (value, has_unit) for each number in `text` that could be a measurement."""
    masked = _MASK_RE.sub(" ", text).replace("$", " ")
    out = []
    for m in _NUMBER_RE.finditer(masked):
        raw = m.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        has_unit = bool(m.group(2))
        if not has_unit and value.is_integer() and abs(value) < 10:
            continue  # small counts ("2 failovers", "3 rounds") are not checked
        out.append((value, has_unit))
    return out


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _grounded(value: float, pool: list[float]) -> bool:
    for n in pool:
        if abs(value - n) <= max(0.01 * abs(n), 0.051):
            return True
        if value.is_integer() and abs(value - n) < 0.5:
            return True  # rounded to a whole number
        if abs(value - n * 100) <= max(0.01 * abs(n * 100), 0.051):
            return True  # fraction expressed as a percentage
    return False


def validate_turn(
    draft: TurnDraft,
    role: RoleDefinition,
    scenario: Scenario,
    bundle: EvidenceBundle,
    visible_ids: set[str],
    tool_names: set[str],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []

    def add(code, severity, detail, location):
        findings.append(ValidationFinding(code=code, severity=severity, detail=detail, location=location))

    option_ids = {o.id for o in scenario.options}
    if draft.position != "undecided" and draft.position not in option_ids:
        add("unknown_position", "error", f"position {draft.position!r} is not an option", "position")

    def check_ids(ids: list[str], location: str) -> list[str]:
        ok = []
        for evidence_id in ids:
            if evidence_id not in bundle:
                add("invalid_citation", "error", f"{evidence_id} does not exist", location)
            elif evidence_id not in visible_ids:
                add("inaccessible_citation", "error", f"{evidence_id} is not accessible to {role.id.value}", location)
            else:
                ok.append(evidence_id)
        return ok

    assumption_pool = [float(a.value) for a in scenario.assumptions if isinstance(a.value, int | float)]
    for i, claim in enumerate(draft.claims):
        location = f"claims[{i}]"
        valid = check_ids(claim.evidence_ids, location)
        if not claim.evidence_ids and claim.basis not in ("judgment", "assumption"):
            add("missing_citation", "warning", f"{claim.basis} claim without evidence ids", location)
        cited = [bundle.get(evidence_id) for evidence_id in valid]
        pool = [n for item in cited for n in iter_numbers(item.data)]
        pool += [float(a.value) for item in cited for a in item.assertions if isinstance(a.value, int | float)]
        # Numbers written as text in the cited evidence count too (a title such as "last 14 runs", a version "2.14").
        pool += [v for item in cited for text in (item.title, *_strings(item.data)) for v, _ in extract_numbers(text)]
        if claim.basis == "assumption":
            pool += assumption_pool
        for value, _ in extract_numbers(claim.statement):
            if not _grounded(value, pool):
                add("ungrounded_number", "error", f"{value:g} does not appear in the cited evidence", location)

    for i, request in enumerate(draft.tool_requests):
        location = f"tool_requests[{i}]"
        if request.tool not in tool_names:
            add("unknown_tool", "error", f"unknown tool {request.tool}", location)
        elif request.tool not in role.tools:
            add("tool_not_permitted", "error", f"{role.id.value} may not use {request.tool}", location)
        try:
            if not isinstance(json.loads(request.arguments_json or "{}"), dict):
                raise ValueError
        except ValueError:
            add("bad_tool_arguments", "error", "arguments_json is not a JSON object", location)

    role_values = {r.value for r in RoleId}
    for i, challenge in enumerate(draft.challenges):
        location = f"challenges[{i}]"
        if challenge.target_role not in role_values:
            add("unknown_target_role", "error", f"unknown role {challenge.target_role}", location)
        check_ids(challenge.evidence_ids, location)

    for i, proposal in enumerate(draft.optimization_proposals):
        location = f"optimization_proposals[{i}]"
        check_ids(proposal.supporting_evidence_ids, location)
        results = check_ids(proposal.result_evidence_ids, location)
        needed = {"measured_local": EvidenceKind.EXPERIMENT_RESULT, "modeled": EvidenceKind.CALCULATION}.get(
            proposal.result_status
        )
        if needed and not any(bundle.get(r).kind == needed for r in results):
            add("unsupported_result_status", "error",
                f"result_status {proposal.result_status} requires a cited {needed.value} evidence item", location)
    return findings
