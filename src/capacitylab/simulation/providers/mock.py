"""Deterministic MOCK stakeholders for local development, demos, and CI.

These are hand-written policies, not a language model. They read only the evidence their role can
see, take every number they state from that evidence, request tools, and revise positions when tool
results arrive. Comparisons that involve mock stakeholders say nothing about how a real model behaves.
"""

from __future__ import annotations

from capacitylab.diagnostics import fixture_db as fx
from capacitylab.evidence.models import EvidenceItem, EvidenceKind
from capacitylab.simulation.providers.base import PROMPT_VERSION, ProviderResult, TurnContext
from capacitylab.simulation.roles import RoleId
from capacitylab.simulation.schema import (
    Challenge,
    Claim,
    ModelCall,
    OptimizationProposal,
    ToolRequest,
    TurnDraft,
)

MOCK_MODEL = "mock-policy-v1"


# --- evidence helpers -----------------------------------------------------------------------------


def _first(ctx: TurnContext, kind: EvidenceKind) -> EvidenceItem | None:
    items = [i for i in ctx.visible if i.kind == kind and not i.produced_by_tool_call]
    return sorted(items, key=lambda i: i.id)[0] if items else None


def _tool_items(ctx: TurnContext, tool: str) -> list[EvidenceItem]:
    return sorted((i for i in ctx.visible if i.data.get("tool") == tool), key=lambda i: int(i.id.rsplit("-", 1)[1]))


def _latest(ctx: TurnContext, tool: str, predicate=lambda item: True) -> EvidenceItem | None:
    items = [i for i in _tool_items(ctx, tool) if predicate(i)]
    return items[-1] if items else None


def _forecast(ctx: TurnContext) -> EvidenceItem | None:
    """Latest base-assumption forecast, preferring one that includes measured effects."""
    base = [i for i in _tool_items(ctx, "capacity_forecast") if not i.data["result"]["assumption_overrides"]]
    with_effects = [i for i in base if i.data["result"]["effects_applied"]]
    if with_effects:
        return with_effects[-1]
    return base[-1] if base else None


def _outcomes(item: EvidenceItem) -> dict[str, dict]:
    return {o["option_id"]: o for o in item.data["result"]["outcomes"]}


def _safe(o: dict) -> bool:
    return o["total_slo_breach_slots"] == 0 and o["saturated_slots"] == 0


def _uses_index(o: dict) -> bool:
    return any("online index build" in e for e in o["operational_events"])


def _new_tool_ids(ctx: TurnContext) -> list[str]:
    previous = ctx.own_previous()
    seen = set(previous.visible_evidence_ids) if previous else set()
    return sorted(i.id for i in ctx.visible if i.produced_by_tool_call and i.id not in seen)


def _latest_positions(ctx: TurnContext) -> dict[str, str]:
    positions: dict[str, str] = {}
    for t in ctx.prior_turns:
        positions[t.role] = t.draft.position
    return positions


def _challenged_by(ctx: TurnContext, role: RoleId) -> list[Challenge]:
    return [c for t in ctx.prior_turns for c in t.draft.challenges if c.target_role == ctx.role.id.value and t.role == role]


class _Turn:
    def __init__(self, ctx: TurnContext):
        self.ctx = ctx
        self.claims: list[Claim] = []
        self.assumptions: list[str] = []
        self.requests: list[ToolRequest] = []
        self.challenges: list[Challenge] = []
        self.missing: list[str] = []
        self.proposals: list[OptimizationProposal] = []
        self.position = "undecided"
        self.rationale = "Waiting for requested diagnostics before taking a position."
        self.confidence = "low"
        self._requested = {(r.tool, str(sorted(r.arguments.items()))) for r in ctx.tool_records}

    def claim(self, statement: str, ids: list[str], basis: str) -> None:
        if all(i in self.ctx.visible_ids for i in ids):
            self.claims.append(Claim(statement=statement, evidence_ids=ids, basis=basis))

    def request(self, tool: str, args: dict, purpose: str) -> None:
        import json

        key = (tool, str(sorted(args.items())))
        if (
            tool not in self.ctx.role.tools
            or key in self._requested
            or self.ctx.round >= self.ctx.max_rounds
            or len(self.requests) >= self.ctx.per_turn_tool_limit
            or self.ctx.remaining_tool_calls <= len(self.requests)
        ):
            return
        self._requested.add(key)
        self.requests.append(ToolRequest(tool=tool, arguments_json=json.dumps(args, sort_keys=True), purpose=purpose))

    def challenge(self, target: RoleId, statement: str, reason: str, ids: list[str]) -> None:
        if target != self.ctx.role.id and all(i in self.ctx.visible_ids for i in ids):
            self.challenges.append(Challenge(target_role=target.value, target_statement=statement, reason=reason, evidence_ids=ids))

    def gaps(self) -> None:
        for gap in self.ctx.gaps:
            if gap.id not in self.missing:
                self.missing.append(gap.id)

    def decide(self, option_id: str | None, rationale: str, confidence: str) -> None:
        if option_id:
            self.position, self.rationale, self.confidence = option_id, rationale, confidence

    def finish(self) -> TurnDraft:
        previous = self.ctx.own_previous()
        revised = previous is not None and previous.draft.position != self.position
        reason = ""
        if revised:
            new = _new_tool_ids(self.ctx)
            challengers = sorted({t.role for t in self.ctx.prior_turns if t.round == self.ctx.round - 1
                                  for c in t.draft.challenges if c.target_role == self.ctx.role.id.value})
            causes = []
            if new:
                causes.append(f"new results {', '.join(new)}")
            if challengers:
                causes.append(f"challenges from {', '.join(challengers)}")
            reason = f"Changed from {previous.draft.position} after {' and '.join(causes) or 'reconsideration'}: {self.rationale}"
        return TurnDraft(
            position=self.position,
            position_rationale=self.rationale,
            confidence=self.confidence,
            claims=self.claims,
            assumptions=self.assumptions,
            tool_requests=self.requests,
            challenges=self.challenges,
            missing_evidence=self.missing,
            optimization_proposals=self.proposals,
            revised_from_previous=revised,
            revision_reason=reason,
        )


def _n(value) -> str:
    """Thousands separators for large integers; the number-grounding validator accepts them."""
    return f"{value:,}" if isinstance(value, int) and abs(value) >= 10_000 else str(value)


def _describe(o: dict) -> str:
    return (f"{o['option_id']} models {o['total_slo_breach_slots']} SLO breach slots and "
            f"{o['saturated_slots']} saturated slots, peaking at {o['peak_utilization_pct']}% at {o['peak_slot']}")


# --- role policies --------------------------------------------------------------------------------


def _database_engineer(ctx: TurnContext, t: _Turn) -> None:
    digest = _first(ctx, EvidenceKind.QUERY_DIGEST)
    root = None
    if digest:
        rows = [f for f in digest.data.get("fingerprints", []) if f.get("share_of_cpu_pct") is not None]
        rows.sort(key=lambda f: (f.get("share_of_rows_examined_pct") or 0, f.get("share_of_cpu_pct") or 0), reverse=True)
        if rows:
            top = rows[0]
            root = top["fingerprint_id"]
            text = f"{root} has the largest share of work in the digest: {top['share_of_cpu_pct']}% of statement CPU"
            if top.get("share_of_rows_examined_pct") is not None:
                text += f" and {top['share_of_rows_examined_pct']}% of rows examined"
            if top.get("avg_rows_sent"):
                text += f", reading {_n(top['avg_rows_examined'])} rows per call to return {_n(top['avg_rows_sent'])}"
            t.claim(text + ".", [digest.id], "observed")
    else:
        t.missing.append("No statement digest is available, so load cannot be attributed to statements.")

    plans = [i for i in ctx.visible if i.kind == EvidenceKind.QUERY_PLAN and i.data.get("fingerprint_id") == root]
    if plans:
        plan = plans[0]
        worst = max(plan.data["steps"], key=lambda s: (s.get("actual_rows") or 0) / max(1, s.get("estimated_rows") or 1))
        t.claim(
            f"The {root} plan estimated {_n(worst['estimated_rows'])} rows on {worst['table']} but read {_n(worst['actual_rows'])} "
            f"({worst['access']}); statistics were last analyzed {plan.data.get('statistics_last_analyzed_days_ago')} days ago.",
            [plan.id], "observed",
        )
        t.request("row_estimate_check", {"fingerprint_id": root}, "Quantify the cardinality misestimate.")
    elif root:
        t.missing.append(f"No execution plan evidence for {root}.")
        if root in fx.QUERIES:
            t.request("explain_query", {"fingerprint_id": root}, "Get a sandbox plan because no captured plan exists.")

    t.request("bottleneck_classifier", {}, "Establish whether CPU, memory/I-O, contention, or connections is the constraint.")
    if root in fx.QUERIES:
        t.request("index_experiment", {"candidates": sorted(fx.INDEX_CANDIDATES), "fingerprint_ids": sorted(fx.QUERIES)},
                  "Measure whether a composite index bounds the range without regressing other statements.")
    if _first(ctx, EvidenceKind.TABLE_STATS):
        t.request("table_growth_review", {}, "Check whether table size or growth is relevant to the hot statements.")
    t.request("capacity_forecast", {}, "Model the options with current evidence.")

    bottleneck = _latest(ctx, "bottleneck_classifier")
    if bottleneck:
        r = bottleneck.data["result"]
        details = "; ".join(f["detail"] for f in r["findings"]) or "no resource finding"
        t.claim(f"Bottleneck classification: primary {r['primary']} ({details}); not indicated: {', '.join(r['not_indicated'])}.",
                [bottleneck.id], "observed")

    growth = _latest(ctx, "table_growth_review")
    if growth:
        for table in growth.data["result"]["tables"][:2]:
            if table["growth_sensitive_statements"]:
                t.claim(f"{table['table']} matters because {', '.join(table['growth_sensitive_statements'])} read more rows as it "
                        f"grows ({table['growth_pct_per_week']}% per week).", [growth.id], "observed")
            else:
                t.claim(f"{table['table']} is the largest table at {table['data_gib']} GiB, but its accesses are bounded; its size "
                        "alone does not justify optimization work.", [growth.id], "observed")

    estimate = _latest(ctx, "row_estimate_check")
    if estimate:
        r = estimate.data["result"]
        t.claim(f"Worst q-error for {r['fingerprint_id']} is {r['worst_q_error']}. " + " ".join(r["interpretation"]),
                [estimate.id], "observed")

    experiment = _latest(ctx, "index_experiment")
    supporting = [i.id for i in [digest, *plans] if i]
    if experiment and root in fx.QUERIES:
        result = experiment.data["result"]
        cands = result["candidates"]
        best = max(cands, key=lambda c: cands[c]["measured"]["queries"][root]["work_reduction_pct"])
        regressions = []
        for cid, c in sorted(cands.items()):
            m = c["measured"]
            parts = [f"{fid} work {q['work_reduction_pct']}% lower ({_n(q['work_before'])} to {_n(q['work_after'])})"
                     for fid, q in sorted(m["queries"].items())]
            timing = " (timing-based, noisy)" if result["engine"].startswith("MySQL") else ""
            t.claim(f"{cid}: " + "; ".join(parts) + f"; insert work {m['write_overhead_pct']:+}%{timing}; "
                    f"{_n(m['index_bytes_local'])} bytes locally ({result['engine']}).", [experiment.id], "measured")
            regressions += [(cid, fid, q) for fid, q in m["queries"].items() if q["work_reduction_pct"] < 0]
        spec = cands[best]
        translation = spec["modeled_production_translation"]
        tradeoffs = [f"Insert work +{spec['measured']['write_overhead_pct']}% in the sandbox (measured)",
                     f"Modeled production index size {translation['index_storage_gib']} GiB",
                     spec["redundancy"]["note"]]
        for cid, fid, q in regressions:
            if cid == best:
                tradeoffs.append(f"{fid} regressed {q['work_reduction_pct']}% locally with this index present")
                t.claim(f"With {cid}, {fid} regressed {q['work_reduction_pct']}% in the sandbox, so the planner's index choice "
                        "must be verified before rollout.", [experiment.id], "measured")
        t.proposals.append(OptimizationProposal(
            proposal_id="PROP-INDEX-1",
            target_fingerprint=root,
            supporting_evidence_ids=supporting,
            hypothesis="The only usable orders index covers (tenant_id, customer_id), so the created_day range cannot be bounded "
                       "and the statement reads the tenant's whole order range.",
            uncertainty="Measured on a local sandbox with synthetic data; the production planner and data distribution may "
                        "differ (GAP-PROD-PLAN-VALIDATION). The CPU translation depends on A-CPU-ROWS-EXPONENT.",
            proposed_change=f"CREATE INDEX {spec['index']['name']} ON {spec['index']['table']} "
                            f"({', '.join(spec['index']['columns'])}) using an online build before the change freeze.",
            tradeoffs=tradeoffs,
            validation_method="Replay the audience and order-history statements on a production-like replica; compare plans, "
                              "rows examined, p95 latency and insert latency; monitor lock waits during the build.",
            rollback="Drop the new index; keep the existing index until the new plans are verified.",
            result_status="measured_local",
            result_evidence_ids=[experiment.id],
        ))
        t.request("rewrite_equivalence", {"rewrite_ids": sorted(fx.REWRITES)},
                  "Check whether a rewrite reduces work further without changing results.")
        t.assumptions.append("Sandbox work units are a proxy for CPU; production translation uses A-CPU-ROWS-EXPONENT.")
    elif root in fx.QUERIES and supporting:
        t.proposals.append(OptimizationProposal(
            proposal_id="PROP-INDEX-1", target_fingerprint=root, supporting_evidence_ids=supporting,
            hypothesis="The created_day range is not bounded by the existing index.",
            uncertainty="Not tested yet.", proposed_change="Composite index leading with tenant_id and created_day (to be measured).",
            tradeoffs=["Write overhead and storage not measured yet"], validation_method="index_experiment in the sandbox",
            rollback="Drop the index.", result_status="not_tested", result_evidence_ids=[]))

    lab = next((i for i in ctx.visible if i.id == "EV-LAB-CMP"), None)
    if lab:
        audience = lab.data["by_fingerprint"].get("QF-AUDIENCE", {})
        event, indexed = audience.get("EVENT", {}), audience.get("EVENTIDX", {})
        if event.get("p95_ms") is not None and indexed.get("p95_ms") is not None:
            low = " (low sample; treat as indicative)" if event.get("low_sample") or indexed.get("low_sample") else ""
            t.claim(f"In the local MySQL lab under the event mix, QF-AUDIENCE p95 was {event['p95_ms']} ms without the candidate "
                    f"index and {indexed['p95_ms']} ms with it{low}. This is a lab observation, not production.",
                    [lab.id], "measured")
        checkout = lab.data["by_fingerprint"].get("QF-CHECKOUT-WRITE", {})
        before, after = checkout.get("EVENT", {}).get("p95_ms"), checkout.get("EVENTIDX", {}).get("p95_ms")
        if before is not None and after is not None and after > before:
            t.claim(f"The same lab run shows a cost: QF-CHECKOUT-WRITE p95 rose from {before} ms to {after} ms with the "
                    "candidate index present, so write latency must be part of the index decision.", [lab.id], "measured")
            t.assumptions.append("The index benefit seen in the lab is small relative to lock contention from the batch job.")

    duplicates = next((i for i in ctx.visible if i.id.endswith("-PTDK")), None) or _latest(ctx, "percona_duplicate_keys")
    if duplicates:
        report = duplicates.data.get("result", {}).get("report") or duplicates.data
        candidate = duplicates.data.get("index_candidate") or duplicates.data.get("result", {}).get("index_candidate")
        findings = report.get("findings", [])
        if findings:
            for f in findings[:2]:
                t.claim(f"pt-duplicate-key-checker reports that {f['redundant_index']} on {f['table']} is a {f['relation']} of "
                        f"{f['covered_by']} ({report['duplicate_index_bytes']} bytes of duplicate index); it could be dropped "
                        "only after checking every statement that uses it.", [duplicates.id], "observed")
        else:
            t.claim(f"pt-duplicate-key-checker found no duplicate or left-prefix indexes with {candidate} present.",
                    [duplicates.id], "observed")

    rewrites = _latest(ctx, "rewrite_equivalence")
    if rewrites:
        for rid, r in sorted(rewrites.data["result"]["rewrites"].items()):
            if r["equivalent_on_fixtures"]:
                t.claim(f"{rid} matched the original on all {r['cases_checked']} fixture cases with a work ratio of {r['work_ratio']}; "
                        "equivalence on fixtures is evidence, not proof.", [rewrites.id], "measured")
            else:
                f = r["failures"][0]
                t.claim(f"{rid} is not equivalent: {f['difference']} for tenant {f['case']['tenant']} "
                        f"({f['original_rows']} vs {f['rewrite_rows']} rows). {' '.join(f['hints'])}", [rewrites.id], "measured")
                if any("NULL" in h for h in f["hints"]):
                    t.challenge(RoleId.APPLICATION_OWNER, "Audience selection semantics",
                                f"For tenant {f['case']['tenant']} the current NOT IN query returns {f['original_rows']} rows "
                                "because suppressions contain NULL customer ids. Changing that is a behavior decision, not an "
                                "optimization.", [rewrites.id])

    forecast = _forecast(ctx)
    if forecast:
        outcomes = _outcomes(forecast)
        keep = next((o for o in outcomes.values() if o["option_kind"] == "keep"), None)
        if keep:
            t.claim(_describe(keep) + ".", [forecast.id], "modeled")
        best_index = bool(experiment)
        candidates = [o for o in outcomes.values() if _safe(o) and o["slots_over_threshold"] == 0]
        candidates.sort(key=lambda o: (0 if (best_index and _uses_index(o)) else 1, len(o["unknowns"]),
                                       o["cost_delta_month_usd"] + o["cost_delta_event_usd"], o["peak_utilization_pct"]))
        if candidates:
            chosen = candidates[0]
            t.claim(_describe(chosen) + ".", [forecast.id], "modeled")
            t.decide(chosen["option_id"], "Lowest-risk option that removes the modeled breaches without failovers, "
                     "preferring a measured workload fix over added capacity.",
                     "medium" if forecast.data["result"]["effects_applied"] else "low")
            reliability = _latest_positions(ctx).get(RoleId.RELIABILITY_ENGINEER.value)
            if reliability and reliability in outcomes and reliability != chosen["option_id"] and outcomes[reliability]["unknowns"]:
                t.challenge(RoleId.RELIABILITY_ENGINEER, f"Position {reliability}",
                            f"{reliability} depends on unmeasured failovers, while {chosen['option_id']} models "
                            f"{chosen['total_slo_breach_slots']} breach slots without instance changes.", [forecast.id])
    t.gaps()


def _application_owner(ctx: TurnContext, t: _Turn) -> None:
    releases = _first(ctx, EvidenceKind.RELEASE_CALENDAR)
    freeze = None
    if releases:
        for r in releases.data.get("releases", []):
            t.claim(f"Release {r['version']} starts at {r['start']} ({r['change']}) with an estimated order-history call "
                    f"multiplier of {r['estimated_order_history_call_multiplier']}.", [releases.id], "assumption")
        freeze = releases.data.get("change_freeze")
        if freeze:
            t.claim(f"A change freeze on {freeze['scope']} runs from {freeze['start']} to {freeze['end']}.", [releases.id], "observed")
            t.assumptions.append(f"Any schema change must finish and be verified before {freeze['start']}.")
    for cal in [i for i in ctx.visible if i.kind == EvidenceKind.EVENT_CALENDAR and "events" in i.data]:
        for e in cal.data["events"]:
            if not e.get("movable", True):
                t.claim(f"The {e['event']} for {e['tenant']} ({e['start']}-{e['end']}) is publicly announced and cannot move.",
                        [cal.id], "observed")
    batch = _first(ctx, EvidenceKind.BATCH_SCHEDULE)
    log = _first(ctx, EvidenceKind.BATCH_RUN_LOG)
    if batch:
        t.claim(f"{batch.data['job']} starts at {batch.data['start']} and must finish before {batch.data['must_finish_before_next_day']} "
                f"for the {batch.data.get('downstream_dependency', 'downstream work')}.", [batch.id], "observed")
        t.request("batch_reschedule_check", {"candidate_starts": ["01:00", "22:30"]}, "Find a batch start that meets the deadline.")
    if log:
        t.claim(f"Recent runs took up to {log.data['max_minutes']} minutes (median {log.data['p50_minutes']}).", [log.id], "observed")

    for c in ctx.contradictions:
        if not c.key.startswith("campaign."):
            continue
        t.claim(f"Sources disagree on {c.key}: " + "; ".join(f"{v} in {i}" for v, i in zip(c.values, c.evidence_ids, strict=True)) + ".",
                list(c.evidence_ids), "observed")
        campaign = next((e for e in ctx.scenario.events_of("campaign")), None)
        numeric = [v for v in c.values if isinstance(v, int | float)]
        if campaign and numeric:
            t.request("capacity_forecast", {"assumption_overrides": {campaign.multiplier_assumption: min(numeric)}},
                      "Sensitivity: does the decision change at the lower observed multiplier?")
    if _first(ctx, EvidenceKind.QUERY_DIGEST):
        t.request("top_queries", {"rank_by": "cpu"}, "See which application statements drive load.")
    t.request("capacity_forecast", {}, "Model the options.")

    check = _latest(ctx, "batch_reschedule_check")
    if check:
        r = check.data["result"]
        for cand in r["candidates"]:
            overlap = f"overlapping {', '.join(cand['overlaps_events'])}" if cand["overlaps_events"] else "with no event overlap"
            verdict = "meeting" if cand["meets_deadline"] else "missing"
            t.claim(f"Starting at {cand['start']} ends by {cand['worst_case_end']} in the worst case, {verdict} the "
                    f"{r['deadline_next_day']} deadline, {overlap}.", [check.id], "modeled")

    sensitivity = _latest(ctx, "capacity_forecast", lambda i: bool(i.data["result"]["assumption_overrides"]))
    if sensitivity:
        overrides = sensitivity.data["result"]["assumption_overrides"]
        keep = next((o for o in _outcomes(sensitivity).values() if o["option_kind"] == "keep"), None)
        if keep:
            t.claim(f"With overrides {overrides}, " + _describe(keep) + ".", [sensitivity.id], "modeled")

    forecast = _forecast(ctx)
    if forecast:
        outcomes = _outcomes(forecast)
        ok = [o for o in outcomes.values() if _safe(o) and o["batch_deadline_ok"] is not False]
        ok.sort(key=lambda o: (o["slots_over_threshold"], len(o["operational_events"]), o["option_id"]))
        if ok:
            chosen = ok[0]
            t.decide(chosen["option_id"], "Meets the tenant commitment and the batch deadline with the fewest operational "
                     "changes on the event day.", "medium")
            if _uses_index(chosen) and freeze:
                t.challenge(RoleId.DATABASE_ENGINEER, "Index rollout timing",
                            f"The change freeze starts at {freeze['start']}; the index must be built and verified before then or "
                            "this option is unavailable.", [releases.id])
    for c in _challenged_by(ctx, RoleId.DATABASE_ENGINEER):
        if "NULL" in c.reason:
            t.claim("The NULL-suppression behavior needs a product decision; no query rewrite ships for this event.", [], "judgment")
    t.gaps()


def _reliability_engineer(ctx: TurnContext, t: _Turn) -> None:
    slo = _first(ctx, EvidenceKind.SLO)
    if slo:
        for s in slo.data.get("slos", []):
            t.claim(f"{s['id']} requires p95 at or under {s['p95_ms']} ms.", [slo.id], "observed")
        t.claim(f"Error budget remaining is {slo.data['error_budget_remaining_pct']}% against a "
                f"{slo.data['availability_target_pct']}% availability target.", [slo.id], "observed")
    else:
        t.missing.append("No SLO evidence; breach risk cannot be judged.")
    lab = next((i for i in ctx.visible if i.id == "EV-LAB-CMP"), None)
    if lab and {"BASE", "EVENT"} <= set(lab.data.get("lock_waits", {})):
        waits, avg = lab.data["lock_waits"], lab.data["avg_row_lock_wait_ms"]
        t.claim(f"In the local MySQL lab, row lock waits went from {waits['BASE']} at baseline to {waits['EVENT']} with the batch "
                f"job running, averaging {avg['EVENT']} ms per wait.", [lab.id], "observed")
        checkout = lab.data["by_fingerprint"].get("QF-CHECKOUT-WRITE", {})
        with_batch, without = checkout.get("EVENT", {}).get("p95_ms"), checkout.get("EVENTNB", {}).get("p95_ms")
        if with_batch is not None and without is not None:
            t.claim(f"With the batch job moved out of the event window, lab QF-CHECKOUT-WRITE p95 was {without} ms versus "
                    f"{with_batch} ms with it running ({waits.get('EVENTNB', 'unknown')} lock waits versus {waits['EVENT']}).",
                    [lab.id], "measured")
    deadlock_log = next((i for i in ctx.visible if i.id.endswith("-PTDL")), None)
    if deadlock_log and deadlock_log.data.get("deadlocks"):
        latest = deadlock_log.data["deadlocks"][-1]
        involved = sorted({tx.get("statement_id") or tx.get("table") or "unknown" for tx in latest["transactions"]})
        victims = sorted({tx.get("statement_id") or tx.get("table") or "unknown" for tx in latest["transactions"] if tx["victim"]})
        batch = " The batch job was one side of it, so running it at the same time as checkout carries a rollback risk, " \
                "not only extra latency." if "LAB-BATCH" in involved else ""
        t.claim(f"pt-deadlock-logger captured a deadlock between {' and '.join(involved)}; InnoDB rolled back "
                f"{', '.join(victims) or 'one transaction'}.{batch}", [deadlock_log.id], "observed")
    history = _first(ctx, EvidenceKind.RELIABILITY_HISTORY)
    if history:
        for inc in history.data.get("incidents", []):
            t.claim(f"On {inc['date']}: {inc['summary']} ({inc['elevated_latency_minutes']} minutes of elevated latency).",
                    [history.id], "observed")
        if history.data.get("measured_failover_seconds") is None:
            t.claim("No failover duration has been measured for this cluster.", [history.id], "observed")
    series = _first(ctx, EvidenceKind.METRIC_SERIES)
    if series and series.data.get("max_by_slot"):
        t.claim(f"On the reference day the writer's 1-minute CPU peaked at {max(series.data['max_by_slot'])}%.",
                [series.id], "observed")

    t.request("capacity_forecast", {}, "Model breach risk and headroom for every option.")
    t.request("bottleneck_classifier", {}, "Check for contention or connection pressure in addition to CPU.")
    if ctx.scenario.events_of("batch_job"):
        t.request("batch_reschedule_check", {"candidate_starts": ["01:00"]}, "Confirm a batch move is safe for downstream work.")

    forecast = _forecast(ctx)
    if forecast:
        outcomes = _outcomes(forecast)
        threshold = forecast.data["result"]["utilization_threshold_pct"]
        keep = next((o for o in outcomes.values() if o["option_kind"] == "keep"), None)
        if keep:
            t.claim(_describe(keep) + ".", [forecast.id], "modeled")

        def risk(o: dict) -> int:
            return len(o["unknowns"]) + (1 if _uses_index(o) else 0)

        ok = [o for o in outcomes.values() if _safe(o)]
        ok.sort(key=lambda o: (o["slots_over_threshold"], risk(o), o["peak_utilization_pct"]))
        if ok:
            chosen = ok[0]
            t.claim(_describe(chosen) + f", against a {threshold}% threshold.", [forecast.id], "modeled")
            t.decide(chosen["option_id"], "Zero modeled breaches with the most headroom and the fewest unmeasured risks.",
                     "medium" if chosen["unknowns"] else "high")
            for o in ok[1:]:
                if _uses_index(o) and o["slots_over_threshold"] == 0:
                    t.claim(f"{o['option_id']} also models zero breaches, peaking at {o['peak_utilization_pct']}%, but its benefit "
                            "rests on a local sandbox measurement (GAP-PROD-PLAN-VALIDATION).", [forecast.id], "modeled")
                    break
            for role, pos in _latest_positions(ctx).items():
                if role == ctx.role.id.value or pos not in outcomes or pos == chosen["option_id"]:
                    continue
                o = outcomes[pos]
                if not _safe(o) or o["slots_over_threshold"] > 0:
                    t.challenge(RoleId(role), f"Position {pos}",
                                f"{pos} peaks at {o['peak_utilization_pct']}% against the {threshold}% threshold with "
                                f"{o['total_slo_breach_slots']} breach slots.", [forecast.id])
                elif _uses_index(o):
                    t.challenge(RoleId(role), f"Position {pos}",
                                f"{pos} peaks at {o['peak_utilization_pct']}% versus {chosen['peak_utilization_pct']}% for "
                                f"{chosen['option_id']}, and its benefit is not validated outside the sandbox.", [forecast.id])
    t.gaps()


def _finops_analyst(ctx: TurnContext, t: _Turn) -> None:
    budget = _first(ctx, EvidenceKind.BUDGET)
    if budget:
        t.claim(f"The month is forecast at ${budget.data['forecast_month_usd']} against a ${budget.data['monthly_budget_usd']} budget.",
                [budget.id], "observed")
    rate = _first(ctx, EvidenceKind.RATE_CARD)
    if rate:
        t.claim("The rate card uses on-demand rates without reserved-instance or savings-plan discounts; it is not a "
                "provider quote.", [rate.id], "observed")
    else:
        t.missing.append("No rate card; costs cannot be calculated.")
    t.request("cost_estimate", {}, "Cost every option against the budget.")
    t.request("capacity_forecast", {}, "Know which options meet objectives before comparing cost.")
    if _first(ctx, EvidenceKind.TABLE_STATS):
        t.request("table_growth_review", {}, "Separate storage growth cost from performance work.")

    cost = _latest(ctx, "cost_estimate")
    if cost:
        for row in cost.data["result"]["options"]:
            if row["cost_delta_event_usd"] or row["cost_delta_month_usd"]:
                t.claim(f"{row['option_id']} changes cost by ${row['cost_delta_event_usd']} one-off and ${row['cost_delta_month_usd']} "
                        "per month.", [cost.id], "modeled")
            if any("failover" in u for u in row["unknowns"]):
                t.claim(f"{row['option_id']} carries an unpriced risk: failover duration is not measured.", [cost.id], "modeled")
    growth = _latest(ctx, "table_growth_review")
    if growth:
        big = growth.data["result"]["tables"][0]
        if not big["growth_sensitive_statements"]:
            t.claim(f"{big['table']} holds {big['data_gib']} GiB growing {big['growth_pct_per_week']}% per week; retention is a "
                    "storage-cost question, separate from this decision.", [growth.id], "observed")

    forecast = _forecast(ctx)
    if forecast:
        outcomes = _outcomes(forecast)
        strict = bool(_challenged_by(ctx, RoleId.RELIABILITY_ENGINEER))
        ok = [o for o in outcomes.values() if _safe(o) and (o["slots_over_threshold"] == 0 or not strict)]
        at_risk = [o for o in outcomes.values() if o.get("revenue_at_risk_usd")]
        for o in sorted(at_risk, key=lambda o: -o["revenue_at_risk_usd"])[:2]:
            t.claim(f"{o['option_id']} puts ${o['revenue_at_risk_usd']:,.0f} of sale revenue at risk across "
                    f"{o['revenue_at_risk_slots']} breached sale slots.", [forecast.id], "modeled")
        # Twelve-month view: recurring monthly deltas plus the one-off cost of this event.
        ok.sort(key=lambda o: (round(12 * o["cost_delta_month_usd"] + o["cost_delta_event_usd"], 2), o["slots_over_threshold"]))
        if ok:
            chosen = ok[0]
            t.decide(chosen["option_id"], "Cheapest option with zero modeled SLO breaches"
                     + (" and no slots over the utilization threshold, after the reliability challenge." if strict else "."),
                     "medium")
            t.claim(f"{chosen['option_id']} costs ${chosen['cost_delta_event_usd']} one-off and ${chosen['cost_delta_month_usd']} per "
                    f"month with {chosen['total_slo_breach_slots']} modeled breach slots.", [forecast.id], "modeled")
            for role, pos in _latest_positions(ctx).items():
                if pos in outcomes and pos != chosen["option_id"] and role != ctx.role.id.value:
                    other = outcomes[pos]
                    if other["cost_delta_event_usd"] + other["cost_delta_month_usd"] > chosen["cost_delta_event_usd"] + chosen["cost_delta_month_usd"]:
                        t.challenge(RoleId(role), f"Position {pos}",
                                    f"{chosen['option_id']} models zero SLO breaches at ${chosen['cost_delta_event_usd']} one-off "
                                    f"and ${chosen['cost_delta_month_usd']} per month, versus ${other['cost_delta_event_usd']} and "
                                    f"${other['cost_delta_month_usd']} with {len(other['operational_events'])} operational "
                                    f"changes for {pos}.", [forecast.id])
    t.gaps()


def _tenant_representative(ctx: TurnContext, t: _Turn) -> None:
    tenant = ctx.scenario.focal_tenant
    profile = next((i for i in ctx.visible if i.kind == EvidenceKind.TENANT_PROFILE and i.tenant_id == tenant), None)
    if profile:
        d = profile.data
        if "stated_expected_traffic_multiplier" in d:
            t.claim(f"Tenant {tenant} (synthetic) expects about {d['stated_expected_traffic_multiplier']}x traffic during its event.",
                    [profile.id], "assumption")
        if d.get("campaign_time_movable") is False:
            t.claim(f"The event time cannot move: {d.get('reason', 'it was announced')}", [profile.id], "observed")
        if d.get("most_sensitive_journeys"):
            t.claim(f"Most sensitive journeys: {', '.join(d['most_sensitive_journeys'])}; tolerance for brief errors is "
                    f"{d.get('tolerance_for_brief_errors', 'unstated')}.", [profile.id], "observed")
    t.request("capacity_forecast", {}, "See the modeled latency for our journeys under each option.")
    t.request("tenant_skew", {}, "Understand our share of the hot statements.")

    skew = _latest(ctx, "tenant_skew")
    if skew:
        for f in skew.data["result"]["fingerprints"]:
            if f.get("own_share_pct"):
                t.claim(f"Our tenant generates {f['own_share_pct']}% of {f['fingerprint_id']} calls.", [skew.id], "observed")

    forecast = _forecast(ctx)
    if forecast:
        outcomes = _outcomes(forecast)

        def latency(o: dict) -> float:
            return sum((s["worst_p95_ms"] if s["worst_p95_ms"] is not None else 1e9) for s in o["slo"].values())

        ok = sorted((o for o in outcomes.values() if _safe(o)), key=lambda o: (latency(o), len(o["unknowns"])))
        if ok:
            chosen = ok[0]
            detail = ", ".join(f"{sid} {s['worst_p95_ms']} ms" for sid, s in sorted(chosen["slo"].items()))
            t.claim(f"{chosen['option_id']} gives the lowest modeled worst-case p95 for our journeys: {detail}.", [forecast.id], "modeled")
            t.decide(chosen["option_id"], "Protects the tenant's journeys with the most latency headroom during the event.", "medium")
            calendar = next((i for i in ctx.visible if i.kind == EvidenceKind.EVENT_CALENDAR and "events" in i.data), None)
            db_position = _latest_positions(ctx).get(RoleId.DATABASE_ENGINEER.value)
            if calendar and db_position in outcomes and _uses_index(outcomes[db_position]):
                t.challenge(RoleId.DATABASE_ENGINEER, f"Position {db_position}",
                            "An index build on the day of the sale puts checkout at risk unless it is finished and verified well "
                            "before the sale starts.", [calendar.id])
    t.gaps()


def _single_agent(ctx: TurnContext, t: _Turn) -> None:
    """Baseline: one decision-maker with all evidence and tools, choosing by a single scalar ordering."""
    _database_engineer(ctx, t)
    t.challenges.clear()
    t.request("cost_estimate", {}, "Cost the options.")
    if ctx.scenario.events_of("batch_job"):
        t.request("batch_reschedule_check", {"candidate_starts": ["01:00"]}, "Check the batch deadline.")
    forecast = _forecast(ctx)
    t.position, t.rationale, t.confidence = "undecided", "Waiting for a forecast.", "low"
    if forecast:
        ok = [o for o in _outcomes(forecast).values() if _safe(o) and o["slots_over_threshold"] == 0]
        ok.sort(key=lambda o: (len(o["unknowns"]), o["cost_delta_month_usd"] + o["cost_delta_event_usd"], o["peak_utilization_pct"]))
        if ok:
            t.decide(ok[0]["option_id"], "Meets objectives with the fewest unknowns, then lowest cost.", "medium")


POLICIES = {
    RoleId.SINGLE_AGENT: _single_agent,
    RoleId.DATABASE_ENGINEER: _database_engineer,
    RoleId.APPLICATION_OWNER: _application_owner,
    RoleId.RELIABILITY_ENGINEER: _reliability_engineer,
    RoleId.FINOPS_ANALYST: _finops_analyst,
    RoleId.TENANT_REPRESENTATIVE: _tenant_representative,
}


class MockProvider:
    name = "mock"
    model = MOCK_MODEL
    mocked = True

    def estimate_max_cost_usd(self, ctx: TurnContext) -> float:
        return 0.0

    def generate_turn(self, ctx: TurnContext) -> ProviderResult:
        turn = _Turn(ctx)
        POLICIES[ctx.role.id](ctx, turn)
        return ProviderResult(
            draft=turn.finish(),
            call=ModelCall(provider=self.name, model=self.model, prompt_version=PROMPT_VERSION, mocked=True),
        )
