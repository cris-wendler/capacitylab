# SPDX-License-Identifier: AGPL-3.0-or-later
from dataclasses import dataclass

from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.evidence.models import EvidenceKind, Provenance
from capacitylab.scenarios.state import RunStatus
from capacitylab.simulation.orchestrator import Orchestrator
from capacitylab.simulation.providers.base import PROMPT_VERSION, ProviderError, ProviderResult
from capacitylab.simulation.providers.mock import MockProvider
from capacitylab.simulation.roles import ROLES, RoleId, visible_evidence
from capacitylab.simulation.run import RunConfig
from capacitylab.simulation.schema import Claim, ModelCall, ToolRequest
from capacitylab.simulation.tools import TOOLS, ToolEnvironment, execute_tool
from capacitylab.simulation.validation import extract_numbers, validate_turn
from tests.conftest import make_draft


@dataclass
class ScriptedProvider:
    script: callable
    name: str = "scripted"
    model: str = "scripted-v1"
    mocked: bool = True
    estimate: float = 0.0

    def estimate_max_cost_usd(self, ctx):
        return self.estimate

    def generate_turn(self, ctx):
        return ProviderResult(self.script(ctx), ModelCall(provider=self.name, model=self.model,
                                                          prompt_version=PROMPT_VERSION, mocked=True))


def test_campaign_run_requests_tests_then_revises(campaign_run):
    run = campaign_run
    assert run.state.status == RunStatus.CONCLUDED and run.mocked
    round1 = [t for t in run.turns if t.round == 1]
    assert {t.draft.position for t in round1} == {"undecided"}
    assert all(t.draft.tool_requests for t in round1)
    executed_after_round1 = [r for r in run.tool_calls if r.round == 1 and r.status == "ok"]
    assert executed_after_round1
    revised = [t for t in run.turns if t.draft.revised_from_previous]
    assert revised and all("EV-TOOL-" in t.draft.revision_reason or "challenges from" in t.draft.revision_reason
                           for t in revised)
    assert any("challenges from reliability_engineer" in t.draft.revision_reason for t in revised), \
        "a challenge should be able to change a position"


def test_every_executed_tool_is_traceable_evidence(campaign_run):
    evidence = {e.id: e for e in campaign_run.tool_evidence}
    for record in campaign_run.tool_calls:
        if record.status == "ok":
            item = evidence[record.evidence_id]
            assert item.produced_by_tool_call == record.call_id and record.result_digest
    by_tool = {e.data["tool"]: e.provenance for e in evidence.values()}
    assert by_tool["index_experiment"] == Provenance.MEASURED
    assert by_tool["capacity_forecast"] == Provenance.MODELED
    assert any(len(r.requested_by) > 1 for r in campaign_run.tool_calls), "identical requests should be deduplicated"


def test_mock_turns_pass_citation_and_grounding_checks(campaign_run, downsize_run):
    for run in (campaign_run, downsize_run):
        codes = [f.code for t in run.turns for f in t.findings]
        assert "invalid_citation" not in codes and "inaccessible_citation" not in codes
        assert "ungrounded_number" not in codes


def test_database_engineer_proposal_is_complete_and_measured(campaign_run):
    proposals = campaign_run.decision.optimization_proposals
    proposal = next(p for p in proposals if p["role"] == "database_engineer")
    for field in ("supporting_evidence_ids", "hypothesis", "uncertainty", "proposed_change", "tradeoffs",
                  "validation_method", "rollback", "result_evidence_ids"):
        assert proposal[field]
    assert proposal["target_fingerprint"] == "QF-AUDIENCE" and proposal["result_status"] == "measured_local"
    kinds = {e.id: e.kind for e in campaign_run.tool_evidence}
    assert any(kinds.get(i) == EvidenceKind.EXPERIMENT_RESULT for i in proposal["result_evidence_ids"])


def test_rewrite_results_reach_stakeholders(campaign_run):
    db_claims = [c.statement for t in campaign_run.turns if t.role == "database_engineer" for c in t.draft.claims]
    assert any("RW-NOT-EXISTS is not equivalent" in s for s in db_claims)
    assert any(c.target_role == "application_owner" and "NULL" in c.reason
               for t in campaign_run.turns for c in t.draft.challenges)


def test_disagreement_is_preserved_not_forced(campaign_run):
    d = campaign_run.decision
    if d.distinct_positions > 1:
        assert any(x.kind == "positions" for x in d.disagreements)
    assert any("not evidence that a recommendation is correct" in c for c in d.caveats)
    assert {m["item"] for m in d.missing_evidence} >= {"GAP-FAILOVER-DURATION", "GAP-PROD-PLAN-VALIDATION"}


def test_contradictory_evidence_triggers_sensitivity_request(campaign_run):
    owner_claims = [c for t in campaign_run.turns if t.role == "application_owner" for c in t.draft.claims]
    assert any(set(c.evidence_ids) == {"EV-CAL-002", "EV-TEN-ALDER-001"} for c in owner_claims)
    assert any(r.tool == "capacity_forecast" and r.arguments.get("assumption_overrides") for r in campaign_run.tool_calls)


def test_tenant_representative_is_scoped(campaign_run, campaign):
    scenario, _ = campaign
    for turn in (t for t in campaign_run.turns if t.role == "tenant_representative"):
        assert "EV-TEN-BIRCH-001" not in turn.visible_evidence_ids
        assert "EV-PLAN-001" not in turn.visible_evidence_ids
    bundle = EvidenceBundle(campaign_run.tool_evidence)
    for item in visible_evidence(ROLES[RoleId.TENANT_REPRESENTATIVE], bundle, scenario):
        text = item.canonical_json()
        for other in ("birch", "cedar", "dune", "elm"):
            assert f'"{other}"' not in text, f"{item.id} exposes {other} to the tenant persona"


def test_validator_flags_invented_numbers_and_bad_citations(campaign):
    scenario, bundle = campaign
    role = ROLES[RoleId.DATABASE_ENGINEER]
    visible = {i.id for i in visible_evidence(role, bundle, scenario)}
    draft = make_draft(
        position="OPT-NOPE",
        claims=[
            Claim(statement="QF-AUDIENCE accounts for 96.2% of rows examined at 19:00 on 2026-10-15.",
                  evidence_ids=["EV-DIG-001"], basis="observed"),
            Claim(statement="An index cuts CPU by 63%.", evidence_ids=["EV-DIG-001"], basis="modeled"),
            Claim(statement="SLO is fine.", evidence_ids=["EV-SLO-001"], basis="observed"),
            Claim(statement="Made up.", evidence_ids=["EV-NOPE-001"], basis="observed"),
        ],
        tool_requests=[ToolRequest(tool="cost_estimate", arguments_json="{}", purpose="x"),
                       ToolRequest(tool="teleport", arguments_json="[1]", purpose="x")],
    )
    findings = validate_turn(draft, role, scenario, bundle, visible, set(TOOLS))
    by_location = {(f.code, f.location) for f in findings}
    assert ("unknown_position", "position") in by_location
    assert not any(loc == "claims[0]" for _, loc in by_location)
    assert ("ungrounded_number", "claims[1]") in by_location
    assert ("inaccessible_citation", "claims[2]") in by_location
    assert ("invalid_citation", "claims[3]") in by_location
    assert ("tool_not_permitted", "tool_requests[0]") in by_location
    assert {("unknown_tool", "tool_requests[1]"), ("bad_tool_arguments", "tool_requests[1]")} <= by_location


def test_number_extraction_ignores_ids_times_and_small_counts():
    values = [v for v, _ in extract_numbers("EV-TOOL-003 at 18:00 on 2026-10-15: 2 failovers, 57.5% peak, $16.8")]
    assert values == [57.5, 16.8]


def test_unauthorized_tool_is_denied_without_evidence(campaign):
    scenario, bundle = campaign
    env = ToolEnvironment(scenario, bundle.copy())
    record, item = execute_tool(env, "TC-001", 1, [RoleId.TENANT_REPRESENTATIVE],
                                ToolRequest(tool="index_experiment", arguments_json="{}", purpose="x"))
    assert record.status == "denied" and item is None
    record, item = execute_tool(env, "TC-002", 1, [RoleId.FINOPS_ANALYST],
                                ToolRequest(tool="capacity_forecast", arguments_json='{"option_ids": ["OPT-NOPE"]}', purpose="x"))
    assert record.status == "error" and "unknown options" in record.error
    env.close()


def test_spend_guard_stops_before_calling_the_model(campaign):
    scenario, bundle = campaign
    provider = ScriptedProvider(lambda ctx: make_draft(), estimate=1.0)
    run = Orchestrator(scenario, bundle, provider, RunConfig(max_usd=0.5)).run()
    assert run.state.status == RunStatus.BUDGET_EXHAUSTED and run.turns == [] and run.warnings


def test_tool_budget_skips_excess_requests(campaign):
    scenario, bundle = campaign
    run = Orchestrator(scenario, bundle, MockProvider(), RunConfig(max_rounds=3, max_tool_calls=2)).run()
    assert sum(r.status == "ok" for r in run.tool_calls) <= 2
    assert any(r.status == "skipped_budget" for r in run.tool_calls)


def test_provider_errors_fail_the_run_cleanly(campaign):
    scenario, bundle = campaign

    def boom(ctx):
        raise ProviderError("simulated outage")

    run = Orchestrator(scenario, bundle, ScriptedProvider(boom)).run()
    assert run.state.status == RunStatus.FAILED and "simulated outage" in run.warnings[0]


def test_run_stops_when_positions_are_stable(campaign):
    scenario, bundle = campaign
    run = Orchestrator(scenario, bundle, ScriptedProvider(lambda ctx: make_draft())).run()
    assert run.state.status == RunStatus.CONCLUDED and run.rounds_completed == 2
    assert run.state.history[-1].reason == "positions stable"


def test_final_round_requests_are_recorded_not_executed(campaign):
    scenario, bundle = campaign

    def script(ctx):
        return make_draft(tool_requests=[ToolRequest(tool="capacity_forecast", arguments_json="{}", purpose="late")])

    run = Orchestrator(scenario, bundle, ScriptedProvider(script), RunConfig(max_rounds=1)).run()
    assert {r.status for r in run.tool_calls} == {"skipped_final_round"}
    assert run.decision.open_tool_requests


def test_missing_digest_and_plan_are_acknowledged(campaign):
    scenario, bundle = campaign
    reduced = EvidenceBundle([i for i in bundle if i.kind not in {EvidenceKind.QUERY_DIGEST, EvidenceKind.QUERY_PLAN}],
                             bundle.missing)
    run = Orchestrator(scenario, reduced, MockProvider(), RunConfig(max_rounds=2)).run()
    db_turn = next(t for t in run.turns if t.role == "database_engineer")
    assert any("No statement digest" in m for m in db_turn.draft.missing_evidence)
    assert not any(r.tool == "index_experiment" for r in run.tool_calls)
    assert run.state.status == RunStatus.CONCLUDED


def test_downsizing_run_preserves_risk_objections(downsize_run):
    final = {p.role: p.position for p in downsize_run.decision.final_positions}
    assert final["reliability_engineer"] == "OPT-KEEP"
    assert {m["item"] for m in downsize_run.decision.missing_evidence} >= {"GAP-WORKING-SET-TEST"}


def test_single_agent_role_sees_everything_but_tenant_scope(campaign):
    scenario, bundle = campaign
    role = ROLES[RoleId.SINGLE_AGENT]
    visible = {i.id for i in visible_evidence(role, bundle, scenario)}
    assert {"EV-PLAN-001", "EV-RATE-001", "EV-SLO-001", "EV-TEN-BIRCH-001"} <= visible
    assert set(TOOLS) == set(role.tools)
