"""Run lab phases for a scenario and turn the measurements into evidence items."""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel

from capacitylab.diagnostics import fixture_db as fx
from capacitylab.diagnostics.sandbox import MySQLSandbox
from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.lab import collector
from capacitylab.lab.explain import parse_explain_analyze, root_latency_ms
from capacitylab.lab.workload import (
    AUDIENCE,
    DIGEST_STATEMENTS,
    ORDER_HISTORY,
    ConnectionInfo,
    PhaseRun,
    build_schedule,
    run_phase,
)
from capacitylab.scenarios.models import Scenario
from capacitylab.settings import MySQLSettings

LAB_DATABASE = "capacitylab_sandbox_lab"
MIN_RELIABLE_CALLS = 20  # percentiles from fewer successful calls are flagged as low-sample
LAB_CAVEAT = ("Observed on a local MySQL 8.0 container with a synthetic workload and dataset at small scale. "
              "Not evidence of production (e.g. Aurora) behavior.")
PERCONA_CAVEAT = ("Percona Toolkit output from the local lab. With --percona the slow log records every statement "
                  "(long_query_time=0) in every phase, which adds some overhead to all phases equally.")


@dataclass(frozen=True)
class PhaseSpec:
    name: str  # uppercase letters/digits, used in evidence ids
    label: str
    multiplier: float
    batch: bool
    index_candidate: str | None = None


class LabConfig(BaseModel):
    duration_s: float = 30.0
    total_qps: float = 150.0
    workers: int = 16
    seed: str = "capacitylab-lab-v1"
    percona: bool = False
    repeats: int = 1  # passes over the whole phase set, each with its own seed, to show run-to-run variation


def default_phases(scenario: Scenario) -> list[PhaseSpec]:
    campaign = next(iter(scenario.events_of("campaign")), None)
    multiplier = float(scenario.assumption(campaign.multiplier_assumption).value) if campaign else 1.0
    has_batch = bool(scenario.events_of("batch_job"))
    phases = [
        PhaseSpec("BASE", "baseline mix, no batch job", 1.0, False),
        PhaseSpec("EVENT", f"event mix ({multiplier:g}x focal tenant) with batch job", multiplier, has_batch),
        PhaseSpec("EVENTIDX", "event mix with batch job and candidate index", multiplier, has_batch, "IDX-TENANT-DAY-CUSTOMER"),
    ]
    if has_batch:
        phases.append(PhaseSpec("EVENTNB", "event mix with the batch job moved out of the window", multiplier, False))
    return phases


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)], 3)


def _latency_summary(run: PhaseRun, duration_s: float) -> dict:
    by_fp: dict[str, list] = defaultdict(list)
    for call in run.calls:
        by_fp[call.fingerprint_id].append(call)
    out = {}
    for fid, calls in sorted(by_fp.items()):
        ok = [c.latency_ms for c in calls if c.error is None]
        delays = [max(0.0, (c.started_s - c.scheduled_s) * 1000) for c in calls]
        errors = Counter(c.error for c in calls if c.error)
        out[fid] = {
            "calls": len(calls),
            "achieved_qps": round(len(calls) / duration_s, 2),
            "p50_ms": _pct(ok, 50),
            "p95_ms": _pct(ok, 95),
            "p99_ms": _pct(ok, 99),
            "max_ms": round(max(ok), 3) if ok else None,
            "p95_queue_delay_ms": _pct(delays, 95),
            "errors": sum(errors.values()),
            "error_types": dict(errors),
            "low_sample": len(ok) < MIN_RELIABLE_CALLS,
            "by_tenant": {t: {"calls": n} for t, n in sorted(Counter(c.tenant for c in calls).items())},
        }
    return out


def _digest_fingerprints(conn, digest_map: dict[str, str], duration_s: float,
                         latency: dict) -> tuple[list[dict], float, float]:
    digests = collector.digest_rows(conn, LAB_DATABASE)
    mapped = [d | {"fingerprint_id": digest_map[d["digest"]]} for d in digests if d["digest"] in digest_map]
    total_time = sum(d["total_db_time_ms"] for d in digests) or 1.0
    total_examined = sum(d["rows_examined"] for d in mapped) or 1
    grouped: dict[str, dict] = {}
    for d in mapped:
        g = grouped.setdefault(d["fingerprint_id"], defaultdict(float))
        for key in ("calls", "total_db_time_ms", "total_lock_time_ms", "rows_examined", "rows_sent", "tmp_disk_tables",
                    "no_index_used", "errors"):
            g[key] += d[key]
        g["p95_latency_ms"] = max(g["p95_latency_ms"], d["p95_latency_ms"])
    fingerprints = []
    for fid, g in sorted(grouped.items(), key=lambda kv: -kv[1]["total_db_time_ms"]):
        calls = max(1, int(g["calls"]))
        fingerprints.append({
            "fingerprint_id": fid,
            "calls": int(g["calls"]),
            "calls_per_s": round(g["calls"] / duration_s, 2),
            "avg_latency_ms": round(g["total_db_time_ms"] / calls, 3),
            "p95_latency_ms": round(g["p95_latency_ms"], 3),
            "avg_rows_examined": round(g["rows_examined"] / calls, 1),
            "avg_rows_sent": round(g["rows_sent"] / calls, 1),
            "avg_lock_wait_ms": round(g["total_lock_time_ms"] / calls, 3),
            "tmp_disk_tables": int(g["tmp_disk_tables"]),
            "no_index_used": int(g["no_index_used"]),
            "share_of_db_time_pct": round(100 * g["total_db_time_ms"] / total_time, 1),
            "share_of_rows_examined_pct": round(100 * g["rows_examined"] / total_examined, 1),
            "by_tenant": latency.get(fid, {}).get("by_tenant", {}),
        })
    mapped_share = sum(d["total_db_time_ms"] for d in mapped) / total_time
    return fingerprints, total_time, round(100 * (1 - mapped_share), 1)


def run_lab(scenario: Scenario, mysql: MySQLSettings, config: LabConfig | None = None,
            phases: list[PhaseSpec] | None = None, progress: Callable[[str], None] = lambda m: None) -> list[EvidenceItem]:
    config = config or LabConfig()
    phases = phases or default_phases(scenario)
    toolkit = None
    toolkit_version = None
    if config.percona:
        from capacitylab.lab.percona import PerconaToolkit

        toolkit = PerconaToolkit(mysql)
        progress("checking Percona Toolkit")
        toolkit_version = toolkit.version()  # raises PerconaUnavailable with a clear message
    progress("building lab database")
    sandbox = MySQLSandbox(mysql.host, mysql.port, mysql.user, mysql.password, LAB_DATABASE)  # enforces localhost + prefix
    try:
        stats = fx.build(sandbox)
        conn = sandbox.conn
        digest_map = {collector.statement_digest(conn, sql, params): fid
                      for fid, statements in DIGEST_STATEMENTS.items() for sql, params in statements}
        engine = sandbox.engine_version()
        lab_started = collector.server_time(conn)
        run_tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        info = ConnectionInfo(mysql.host, mysql.port, mysql.user, mysql.password, LAB_DATABASE)
        now = datetime.now(UTC)
        items: list[EvidenceItem] = []
        tables = collector.table_stats(conn, LAB_DATABASE, fx.SCHEMA_IDENTIFIERS["tables"])
        common = dict(synthetic=True, environment="lab", cluster_id=f"lab:{engine}", caveats=[LAB_CAVEAT])
        percona_common = dict(common, caveats=[LAB_CAVEAT, PERCONA_CAVEAT])
        items.append(EvidenceItem(
            id="EV-LAB-TBL", kind=EvidenceKind.TABLE_STATS, title=f"Lab table sizes and indexes ({engine})",
            provenance=Provenance.OBSERVED, source="lab:information_schema", method="ANALYZE TABLE + information_schema",
            data={"tables": tables, "fixture": {"orders": stats.orders, "customers": stats.customers}}, **common))
        if toolkit:
            from capacitylab.lab.percona import (
                lab_fingerprint_map,
                parse_mysql_summary_header,
                parse_variable_advisor,
            )

            fingerprint_map = lab_fingerprint_map()
            advice = parse_variable_advisor(toolkit.variable_advisor())
            summary = parse_mysql_summary_header(toolkit.mysql_summary())
            items.append(EvidenceItem(
                id="EV-LAB-PTVA", kind=EvidenceKind.TOPOLOGY, title="Lab server configuration review (pt-variable-advisor, pt-mysql-summary)",
                provenance=Provenance.OBSERVED, source="percona:pt-variable-advisor+pt-mysql-summary", method=toolkit_version,
                data={"server": summary, "advice": advice,
                      "note": "The lab container runs with default settings; this advice describes the lab, not production."},
                **percona_common))

        summaries: dict[str, dict] = {}
        for phase in phases:
            progress(f"phase {phase.name}: {phase.label}")
            created = None
            slow_log = f"/tmp/capacitylab-{run_tag}-{phase.name}.log"
            percona_digest = None
            duplicate_report = None
            if phase.index_candidate:
                spec = fx.INDEX_CANDIDATES[phase.index_candidate]
                sandbox.create_index(spec["name"], spec["table"], spec["columns"])
                created = spec
            try:
                schedule = build_schedule(scenario, phase.multiplier, config.total_qps, config.duration_s,
                                          f"{config.seed}:{phase.name}")
                collector.reset_digests(conn)
                before = collector.global_status(conn)
                deadlocks_before = collector.innodb_metric(conn, "lock_deadlocks")
                if toolkit:
                    collector.enable_slow_log(conn, slow_log)
                try:
                    run = run_phase(info, schedule, config.duration_s, config.workers, phase.batch,
                                    f"{config.seed}:{phase.name}")
                finally:
                    if toolkit:
                        collector.disable_slow_log(conn)
                after = collector.global_status(conn)
                deadlocks = collector.innodb_metric(conn, "lock_deadlocks") - deadlocks_before
                latency = _latency_summary(run, config.duration_s)
                fingerprints, total_time, unmapped_pct = _digest_fingerprints(conn, digest_map, config.duration_s, latency)
                plans = {fid: collector.explain_analyze(conn, sql, params) for fid, sql, params in (
                    ("QF-AUDIENCE", AUDIENCE, {"tenant": 1, "since_day": fx.RECENT_SINCE_DAY}),
                    ("QF-ORDER-HISTORY", ORDER_HISTORY, {"tenant": 1, "customer": 1}),
                )}
                if toolkit:
                    from capacitylab.lab.percona import parse_duplicate_keys, summarize_query_digest

                    progress(f"phase {phase.name}: pt-query-digest")
                    try:
                        log_text = toolkit.read_lab_file(slow_log)
                    finally:
                        toolkit.remove_lab_file(slow_log)
                    percona_digest = summarize_query_digest(toolkit.query_digest(log_text), fingerprint_map)
                    if created:
                        duplicate_report = parse_duplicate_keys(toolkit.duplicate_keys(LAB_DATABASE))
            finally:
                if created:
                    sandbox.drop_index(created["name"], created["table"])

            delta = collector.status_delta(before, after)
            samples = run.threads_running_samples or [0]
            label = f"lab phase {phase.name} ({phase.label}), {config.duration_s:g}s at {config.total_qps:g} qps baseline"
            provenance = Provenance.MEASURED if phase.index_candidate else Provenance.OBSERVED
            counters = {**delta, "questions_per_s": round(delta["Questions"] / config.duration_s, 1),
                        "avg_row_lock_wait_ms": round(delta["Innodb_row_lock_time"] / delta["Innodb_row_lock_waits"], 2)
                        if delta["Innodb_row_lock_waits"] else 0.0,
                        "deadlocks": deadlocks}
            items.append(EvidenceItem(
                id=f"EV-LAB-{phase.name}-DIG", kind=EvidenceKind.QUERY_DIGEST, title=f"Statement digests, {label}",
                provenance=provenance, source="lab:performance_schema.events_statements_summary_by_digest",
                method=f"{config.workers} workers, Poisson arrivals, seed {config.seed}", window_start=now,
                data={"phase": phase.name, "window": label, "fingerprints": fingerprints, "unmapped_db_time_pct": unmapped_pct},
                **common))
            lat_item = EvidenceItem(
                id=f"EV-LAB-{phase.name}-LAT", kind=EvidenceKind.METRIC_SUMMARY, title=f"Client latency and server counters, {label}",
                provenance=provenance, source="lab:client+SHOW GLOBAL STATUS", method="per-call client timing",
                data={"phase": phase.name, "client_latency": latency, "server_counters": counters,
                      "threads_running": {"max": max(samples), "mean": round(sum(samples) / len(samples), 2)},
                      "db_load_average_active_sessions": round(total_time / (config.duration_s * 1000), 3),
                      "batch_job": {"running": phase.batch, "chunks_committed": run.batch_chunks, "errors": run.batch_errors},
                      "index_candidate": phase.index_candidate},
                **common)
            items.append(lat_item)
            for fid, text in plans.items():
                steps = parse_explain_analyze(text)
                items.append(EvidenceItem(
                    id=f"EV-LAB-{phase.name}-PLAN-{'AUD' if fid == 'QF-AUDIENCE' else 'OH'}", kind=EvidenceKind.QUERY_PLAN,
                    title=f"EXPLAIN ANALYZE for {fid}, {label}", provenance=provenance, source="lab:EXPLAIN ANALYZE",
                    data={"fingerprint_id": fid, "phase": phase.name, "statistics_last_analyzed_days_ago": 0,
                          "steps": steps, "total_latency_ms": root_latency_ms(steps), "raw_text": text},
                    **common))
            if percona_digest is not None:
                items.append(EvidenceItem(
                    id=f"EV-LAB-{phase.name}-PTQD", kind=EvidenceKind.QUERY_DIGEST, title=f"pt-query-digest of the slow log, {label}",
                    provenance=provenance, source="percona:pt-query-digest", method=toolkit_version,
                    data={"phase": phase.name, **percona_digest}, **percona_common))
            if duplicate_report is not None:
                items.append(EvidenceItem(
                    id=f"EV-LAB-{phase.name}-PTDK", kind=EvidenceKind.SCHEMA,
                    title=f"pt-duplicate-key-checker with {phase.index_candidate} present",
                    provenance=Provenance.OBSERVED, source="percona:pt-duplicate-key-checker", method=toolkit_version,
                    data={"phase": phase.name, "index_candidate": phase.index_candidate, **duplicate_report}, **percona_common))
            summaries[phase.name] = {"latency": latency, "fingerprints": {f["fingerprint_id"]: f for f in fingerprints},
                                     "lock_waits": delta["Innodb_row_lock_waits"],
                                     "avg_row_lock_wait_ms": counters["avg_row_lock_wait_ms"], "deadlocks": deadlocks,
                                     "db_load": lat_item.data["db_load_average_active_sessions"],
                                     "threads_running_max": max(samples)}

        if toolkit:
            from capacitylab.lab.percona import parse_deadlocks

            report = parse_deadlocks(toolkit.deadlocks(), since=lab_started, fingerprint_map=fingerprint_map)
            items.append(EvidenceItem(
                id="EV-LAB-PTDL", kind=EvidenceKind.METRIC_SUMMARY, title="Deadlocks during the lab run (pt-deadlock-logger)",
                provenance=Provenance.OBSERVED, source="percona:pt-deadlock-logger", method=toolkit_version,
                data={**report, "lab_started": lab_started,
                      "note": "InnoDB keeps only the most recent deadlock; the per-phase counts come from INNODB_METRICS."},
                **percona_common))

        names = [p.name for p in phases]
        comparison = {}
        for fid in sorted({f for s in summaries.values() for f in s["latency"]}):
            comparison[fid] = {n: {"calls": summaries[n]["latency"].get(fid, {}).get("calls", 0),
                                   "low_sample": summaries[n]["latency"].get(fid, {}).get("low_sample", True),
                                   "p95_ms": summaries[n]["latency"].get(fid, {}).get("p95_ms"),
                                   "errors": summaries[n]["latency"].get(fid, {}).get("errors"),
                                   "share_of_db_time_pct": summaries[n]["fingerprints"].get(fid, {}).get("share_of_db_time_pct"),
                                   "avg_rows_examined": summaries[n]["fingerprints"].get(fid, {}).get("avg_rows_examined")}
                               for n in names}
        items.append(EvidenceItem(
            id="EV-LAB-CMP", kind=EvidenceKind.EXPERIMENT_RESULT, title=f"Lab phase comparison ({', '.join(names)})",
            provenance=Provenance.MEASURED, source="lab:runner", method="same seed and rates per phase; index created only in its phase",
            data={"scenario_id": scenario.id,
                  "phases": [{"name": p.name, "label": p.label, "multiplier": p.multiplier, "batch": p.batch,
                              "index_candidate": p.index_candidate} for p in phases],
                  "by_fingerprint": comparison,
                  "lock_waits": {n: summaries[n]["lock_waits"] for n in names},
                  "avg_row_lock_wait_ms": {n: summaries[n]["avg_row_lock_wait_ms"] for n in names},
                  "deadlocks": {n: summaries[n]["deadlocks"] for n in names},
                  "db_load_average_active_sessions": {n: summaries[n]["db_load"] for n in names},
                  "threads_running_max": {n: summaries[n]["threads_running_max"] for n in names},
                  "low_sample_note": f"p95 values from fewer than {MIN_RELIABLE_CALLS} calls are unreliable; "
                                     "run longer phases or a higher --qps before relying on them.",
                  "percona_toolkit": toolkit_version,
                  "engine": engine, "config": config.model_dump()},
            **common))
        return items
    finally:
        sandbox.close()


def _spread(values: list) -> dict:
    """Median, range and raw values, so a phase difference can be compared with the variation inside a phase."""
    present = sorted(v for v in values if v is not None)
    if not present:
        return {"values": values, "median": None, "min": None, "max": None, "range_pct": None}
    middle = statistics.median(present)
    return {"values": values, "median": round(middle, 3), "min": present[0], "max": present[-1],
            "range_pct": round(100 * (present[-1] - present[0]) / middle, 1) if middle else None}


def spread_item(comparisons: list[dict], phases: list[PhaseSpec], config: LabConfig) -> EvidenceItem:
    """One item summarising several passes of the same phases."""
    names = [p.name for p in phases]
    fingerprints = sorted({f for c in comparisons for f in c["by_fingerprint"]})
    return EvidenceItem(
        id="EV-LAB-SPREAD", kind=EvidenceKind.EXPERIMENT_RESULT,
        title=f"Lab variation across {len(comparisons)} passes ({', '.join(names)})",
        provenance=Provenance.MEASURED, source="lab:runner", synthetic=True, environment="lab",
        cluster_id=f"lab:{comparisons[0].get('engine', '')}",
        method=f"{len(comparisons)} passes over the same phases, one seed per pass",
        data={"passes": len(comparisons), "scenario_id": comparisons[0].get("scenario_id"),
              "p95_ms": {fid: {n: _spread([c["by_fingerprint"].get(fid, {}).get(n, {}).get("p95_ms")
                                           for c in comparisons]) for n in names} for fid in fingerprints},
              **{key: {n: _spread([c.get(key, {}).get(n) for c in comparisons]) for n in names}
                 for key in ("lock_waits", "deadlocks", "db_load_average_active_sessions")},
              "note": "A difference between phases only means something if it is larger than the range within a phase.",
              "engine": comparisons[0].get("engine"), "config": config.model_dump()},
        caveats=[comparisons[0].get("caveat") or LAB_CAVEAT])


def run_lab_repeats(scenario: Scenario, mysql, config: LabConfig | None = None,
                    phases: list[PhaseSpec] | None = None,
                    progress: Callable[[str], None] = lambda m: None,
                    runner: Callable[..., list[EvidenceItem]] | None = None) -> list[EvidenceItem]:
    """Run the phase set `config.repeats` times. Detailed evidence comes from the first pass, plus a spread item.

    `mysql` is the connection settings for whichever engine `runner` drives (MySQL by default)."""
    runner = runner or run_lab
    config = config or LabConfig()
    phases = phases or default_phases(scenario)
    passes = max(1, config.repeats)
    items: list[EvidenceItem] = []
    comparisons: list[dict] = []
    for rep in range(passes):
        if passes > 1:
            progress(f"pass {rep + 1} of {passes}")
        # Percona tools run on the first pass only: they describe the schema and server, not the workload sample.
        pass_config = config.model_copy(update={"seed": config.seed if rep == 0 else f"{config.seed}:r{rep}",
                                                "percona": config.percona and rep == 0})
        pass_items = runner(scenario, mysql, pass_config, phases, progress)
        comparisons.append(next(i for i in pass_items if i.id == "EV-LAB-CMP").data)
        if rep == 0:
            items = pass_items
    if len(comparisons) > 1:
        items.append(spread_item(comparisons, phases, config))
    return items


def write_evidence(items: list[EvidenceItem], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"items": [i.model_dump(mode="json") for i in items]}, sort_keys=False, width=120))
    return p
