"""Bounded, round-based stakeholder deliberation with tool execution between rounds."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from capacitylab.diagnostics.sandbox import Sandbox, SQLiteSandbox
from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.scenarios.models import Scenario
from capacitylab.scenarios.state import RunStatus
from capacitylab.scenarios.validate import validate_scenario
from capacitylab.settings import MySQLSettings
from capacitylab.simulation.decision import synthesize
from capacitylab.simulation.providers.base import (
    PROMPT_VERSION,
    ProviderError,
    TurnContext,
    TurnProvider,
)
from capacitylab.simulation.roles import (
    ROLE_ORDER,
    ROLES,
    RoleId,
    gaps_for,
    visible_contradictions,
    visible_evidence,
)
from capacitylab.simulation.run import RunConfig, SimulationRun, scenario_digest
from capacitylab.simulation.schema import StakeholderTurn, ToolRequest
from capacitylab.simulation.tools import (
    TOOLS,
    ToolCallRecord,
    ToolEnvironment,
    dedupe_key,
    execute_tool,
    tool_catalog,
)
from capacitylab.simulation.validation import validate_turn


class Orchestrator:
    def __init__(
        self,
        scenario: Scenario,
        bundle: EvidenceBundle,
        provider: TurnProvider,
        config: RunConfig | None = None,
        sandbox_factory: Callable[[], Sandbox] = SQLiteSandbox,
        roles: list[RoleId] | None = None,
        progress: Callable[[str], None] | None = None,
        lab_mysql: MySQLSettings | None = None,
    ):
        self.lab_mysql = lab_mysql
        self.scenario = scenario
        self.initial_bundle = bundle
        self.provider = provider
        self.config = config or RunConfig(
            max_rounds=scenario.budgets.max_rounds,
            max_tool_calls=scenario.budgets.max_tool_calls,
            max_tool_calls_per_turn=scenario.budgets.max_tool_calls_per_turn,
            max_usd=scenario.budgets.max_usd,
        )
        self.sandbox_factory = sandbox_factory
        self.roles = roles or list(ROLE_ORDER)
        self.progress = progress or (lambda message: None)

    def run(self) -> SimulationRun:
        bundle = self.initial_bundle.copy()
        run = SimulationRun(
            run_id=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6],
            created_at=datetime.now(UTC),
            scenario_id=self.scenario.id,
            scenario_digest=scenario_digest(self.scenario),
            initial_evidence_digest=bundle.digest(),
            provider=self.provider.name,
            model=self.provider.model,
            prompt_version=PROMPT_VERSION,
            mocked=self.provider.mocked,
            config=self.config,
        )
        run.validation_issues = validate_scenario(self.scenario, bundle)
        if any(i.level == "error" for i in run.validation_issues):
            run.state.transition(RunStatus.INVALID, "scenario validation failed")
            return run
        run.state.transition(RunStatus.VALIDATED, f"{len(run.validation_issues)} warnings")

        env = ToolEnvironment(self.scenario, bundle, self.sandbox_factory, lab_mysql=self.lab_mysql)
        executed = 0
        try:
            for rnd in range(1, self.config.max_rounds + 1):
                run.state.transition(RunStatus.DELIBERATING, f"round {rnd}")
                prior = list(run.turns)
                round_turns: list[StakeholderTurn] = []
                for role_id in self.roles:
                    role = ROLES[role_id]
                    visible = visible_evidence(role, bundle, self.scenario)
                    ids = {i.id for i in visible}
                    ctx = TurnContext(
                        scenario=self.scenario,
                        role=role,
                        round=rnd,
                        max_rounds=self.config.max_rounds,
                        visible=visible,
                        gaps=gaps_for(role, bundle),
                        contradictions=visible_contradictions(bundle, ids),
                        prior_turns=prior,
                        tool_records=[r for r in run.tool_calls if role_id.value in r.requested_by],
                        tool_catalog=tool_catalog(role_id, lab_available=self.lab_mysql is not None),
                        per_turn_tool_limit=self.config.max_tool_calls_per_turn,
                        remaining_tool_calls=max(0, self.config.max_tool_calls - executed),
                    )
                    estimate = self.provider.estimate_max_cost_usd(ctx)
                    if run.spend_usd + estimate > self.config.max_usd:
                        run.warnings.append(
                            f"Stopped before {role_id.value} in round {rnd}: projected spend "
                            f"${run.spend_usd + estimate:.2f} exceeds the ${self.config.max_usd:.2f} limit."
                        )
                        run.state.transition(RunStatus.BUDGET_EXHAUSTED, "model spend limit")
                        return self._finish(run, bundle, env)
                    self.progress(f"round {rnd}: {role.title}")
                    try:
                        result = self.provider.generate_turn(ctx)
                    except ProviderError as exc:
                        run.spend_usd = round(run.spend_usd + exc.cost_usd, 6)
                        run.input_tokens += exc.input_tokens
                        run.output_tokens += exc.output_tokens
                        run.warnings.append(f"Provider error for {role_id.value} in round {rnd}: {exc}")
                        run.state.transition(RunStatus.FAILED, "provider error")
                        return self._finish(run, bundle, env)
                    turn = StakeholderTurn(
                        turn_id=f"R{rnd}-{role_id.value}",
                        round=rnd,
                        role=role_id.value,
                        draft=result.draft,
                        model_call=result.call,
                        findings=validate_turn(result.draft, role, self.scenario, bundle, ids, set(TOOLS)),
                        visible_evidence_ids=sorted(ids),
                    )
                    run.turns.append(turn)
                    round_turns.append(turn)
                    run.spend_usd = round(run.spend_usd + result.call.cost_usd, 6)
                    run.input_tokens += result.call.input_tokens
                    run.output_tokens += result.call.output_tokens
                run.rounds_completed = rnd

                previous_positions = {t.role: t.draft.position for t in prior}
                changed = any(previous_positions.get(t.role) != t.draft.position for t in round_turns)
                requests: list[tuple[RoleId, ToolRequest]] = []
                for turn in round_turns:
                    for i, request in enumerate(turn.draft.tool_requests):
                        if i >= self.config.max_tool_calls_per_turn:
                            run.tool_calls.append(self._skipped(run, rnd, turn.role, request, "skipped_budget",
                                                                "per-turn tool limit"))
                        else:
                            requests.append((RoleId(turn.role), request))

                final = rnd == self.config.max_rounds
                positions = {t.role: t.draft.position for t in round_turns}
                unanswered = any(
                    c.target_role in positions and positions[c.target_role] != t.draft.position
                    for t in round_turns
                    for c in t.draft.challenges
                )
                stable = (self.config.stop_when_stable and rnd >= 2 and not requests and not changed
                          and not unanswered)
                if final or stable:
                    for role_id, request in requests:
                        run.tool_calls.append(self._skipped(run, rnd, role_id.value, request, "skipped_final_round",
                                                            "requested in the final round; not executed"))
                    run.state.transition(RunStatus.CONCLUDED, "max rounds reached" if final else "positions stable")
                    break

                run.state.transition(RunStatus.EXECUTING_TOOLS, f"{len(requests)} requests after round {rnd}")
                groups: dict[str, tuple[ToolRequest, list[RoleId]]] = {}
                for role_id, request in requests:
                    key = dedupe_key(request, role_id)
                    if key in groups:
                        groups[key][1].append(role_id)
                    else:
                        groups[key] = (request, [role_id])
                for request, group_roles in groups.values():
                    call_id = f"TC-{len(run.tool_calls) + 1:03d}"
                    if executed >= self.config.max_tool_calls:
                        run.tool_calls.append(self._skipped(run, rnd, ",".join(r.value for r in group_roles), request,
                                                            "skipped_budget", "run tool-call limit reached"))
                        continue
                    self.progress(f"round {rnd}: tool {request.tool}")
                    record, item = execute_tool(env, call_id, rnd, group_roles, request)
                    run.tool_calls.append(record)
                    if item is not None:
                        executed += 1
                        run.tool_evidence.append(item)
            return self._finish(run, bundle, env)
        finally:
            env.close()

    def _skipped(self, run: SimulationRun, rnd: int, roles: str, request: ToolRequest, status: str, reason: str) \
            -> ToolCallRecord:
        return ToolCallRecord(call_id=f"TC-{len(run.tool_calls) + 1:03d}", round=rnd, requested_by=roles.split(","),
                              tool=request.tool, arguments={"arguments_json": request.arguments_json},
                              purpose=request.purpose, status=status, error=reason)

    def _finish(self, run: SimulationRun, bundle: EvidenceBundle, env: ToolEnvironment) -> SimulationRun:
        if env._sandbox is not None:
            run.sandbox_engine = env._sandbox.engine_version()
        run.decision = synthesize(self.scenario, bundle, run.turns, run.tool_calls, env.effects)
        return run
