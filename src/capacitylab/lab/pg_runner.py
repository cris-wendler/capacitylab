# SPDX-License-Identifier: AGPL-3.0-or-later
"""The same lab phases on PostgreSQL 17, producing the same evidence ids as the MySQL runner.

What differs is where the numbers come from: pg_stat_statements instead of performance_schema, shared buffer
blocks instead of rows examined, pg_stat_database for deadlocks, and sampled pg_stat_activity for lock waits
(PostgreSQL has no cumulative row-lock-wait counter).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime

from capacitylab.diagnostics import fixture_db as fx
from capacitylab.diagnostics.sandbox import PostgresSandbox
from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.lab import pg_collector
from capacitylab.lab.runner import MIN_RELIABLE_CALLS, LabConfig, PhaseSpec, _latency_summary, default_phases
from capacitylab.lab.workload import (
    AUDIENCE,
    BATCH_UPDATE_POSTGRES,
    DIGEST_STATEMENTS,
    ORDER_HISTORY,
    ConnectionInfo,
    build_schedule,
    run_phase,
)
from capacitylab.scenarios.models import Scenario
from capacitylab.settings import PostgresSettings

LAB_DATABASE = "capacitylab_sandbox_lab"
SAMPLE_INTERVAL_S = 0.1
PG_CAVEAT = ("Observed on a local PostgreSQL 17 container with a synthetic workload and dataset at small scale. "
             "Not evidence of production (e.g. Aurora PostgreSQL or RDS) behavior.")
LOCK_WAIT_NOTE = (f"PostgreSQL keeps no cumulative lock-wait counter. lock_waits is the number of sessions seen "
                  f"waiting on a lock, summed over samples every {SAMPLE_INTERVAL_S:g}s; multiply by the interval "
                  "for session-seconds spent waiting.")


def statement_templates() -> dict[str, list[str]]:
    templates = {fid: [sql for sql, _ in statements] for fid, statements in DIGEST_STATEMENTS.items()}
    templates["LAB-BATCH"] = [BATCH_UPDATE_POSTGRES]
    return templates


def fingerprint_summary(rows: list[dict], duration_s: float, latency: dict) -> tuple[list[dict], float, float]:
    """Group pg_stat_statements rows by scenario statement id. Returns (fingerprints, total ms, unmapped %).

    The lab's own sampling and collection statements are left out of the total."""
    rows = [r for r in rows if not r.get("lab_overhead")]
    total_time = sum(r["total_db_time_ms"] for r in rows) or 1.0
    mapped = [r for r in rows if r["fingerprint_id"]]
    total_blocks = sum(r["shared_blocks"] for r in mapped) or 1
    grouped: dict[str, dict] = {}
    for r in mapped:
        g = grouped.setdefault(r["fingerprint_id"], defaultdict(float))
        for key in ("calls", "total_db_time_ms", "rows", "shared_blocks", "shared_blocks_read", "temp_blocks_written"):
            g[key] += r[key]
    fingerprints = []
    for fid, g in sorted(grouped.items(), key=lambda kv: -kv[1]["total_db_time_ms"]):
        calls = max(1, int(g["calls"]))
        fingerprints.append({
            "fingerprint_id": fid,
            "calls": int(g["calls"]),
            "calls_per_s": round(g["calls"] / duration_s, 2),
            "avg_latency_ms": round(g["total_db_time_ms"] / calls, 3),
            "p95_latency_ms": latency.get(fid, {}).get("p95_ms"),
            "avg_rows_returned": round(g["rows"] / calls, 1),
            "avg_shared_blocks": round(g["shared_blocks"] / calls, 1),
            "shared_blocks_read_from_disk": int(g["shared_blocks_read"]),
            "temp_blocks_written": int(g["temp_blocks_written"]),
            "share_of_db_time_pct": round(100 * g["total_db_time_ms"] / total_time, 1),
            "share_of_shared_blocks_pct": round(100 * g["shared_blocks"] / total_blocks, 1),
            "by_tenant": latency.get(fid, {}).get("by_tenant", {}),
        })
    mapped_share = sum(r["total_db_time_ms"] for r in mapped) / total_time
    return fingerprints, total_time, round(100 * (1 - mapped_share), 1)


def run_lab_postgres(scenario: Scenario, pg: PostgresSettings, config: LabConfig | None = None,
                     phases: list[PhaseSpec] | None = None,
                     progress: Callable[[str], None] = lambda m: None) -> list[EvidenceItem]:
    config = config or LabConfig()
    if config.percona:
        raise ValueError("Percona Toolkit's MySQL tools do not apply to the PostgreSQL lab; drop --percona")
    phases = phases or default_phases(scenario)
    progress("building lab database")
    sandbox = PostgresSandbox(pg.host, pg.port, pg.user, pg.password, LAB_DATABASE)  # enforces localhost + prefix
    try:
        stats = fx.build(sandbox)
        sandbox.analyze()
        conn = sandbox.conn
        engine = sandbox.engine_version()
        info = ConnectionInfo(pg.host, pg.port, pg.user, pg.password, LAB_DATABASE, engine="postgres")
        templates = statement_templates()
        now = datetime.now(UTC)
        common = dict(synthetic=True, environment="lab", cluster_id=f"lab:{engine}", caveats=[PG_CAVEAT])
        items: list[EvidenceItem] = [EvidenceItem(
            id="EV-LAB-TBL", kind=EvidenceKind.TABLE_STATS, title=f"Lab table sizes and indexes ({engine})",
            provenance=Provenance.OBSERVED, source="lab:pg_class+pg_indexes", method="ANALYZE + pg_relation_size",
            data={"tables": pg_collector.table_stats(conn, fx.SCHEMA_IDENTIFIERS["tables"]),
                  "fixture": {"orders": stats.orders, "customers": stats.customers}}, **common)]

        summaries: dict[str, dict] = {}
        for phase in phases:
            progress(f"phase {phase.name}: {phase.label}")
            created = None
            if phase.index_candidate:
                spec = fx.INDEX_CANDIDATES[phase.index_candidate]
                sandbox.create_index(spec["name"], spec["table"], spec["columns"])
                created = spec
            try:
                schedule = build_schedule(scenario, phase.multiplier, config.total_qps, config.duration_s,
                                          f"{config.seed}:{phase.name}")
                pg_collector.reset_statements(conn)
                before = pg_collector.database_stats(conn)
                run = run_phase(info, schedule, config.duration_s, config.workers, phase.batch,
                                f"{config.seed}:{phase.name}", sample_interval_s=SAMPLE_INTERVAL_S)
                time.sleep(1.2)  # closed sessions flush their statistics at most once a second
                after = pg_collector.database_stats(conn)
                latency = _latency_summary(run, config.duration_s)
                fingerprints, total_time, unmapped_pct = fingerprint_summary(
                    pg_collector.statement_rows(conn, templates), config.duration_s, latency)
                plans = {fid: pg_collector.explain_analyze(conn, sql, params) for fid, sql, params in (
                    ("QF-AUDIENCE", AUDIENCE, {"tenant": 1, "since_day": fx.RECENT_SINCE_DAY}),
                    ("QF-ORDER-HISTORY", ORDER_HISTORY, {"tenant": 1, "customer": 1}),
                )}
            finally:
                if created:
                    sandbox.drop_index(created["name"], created["table"])

            delta = pg_collector.stats_delta(before, after)
            running = run.threads_running_samples or [0]
            waiting = run.lock_waiting_samples or [0]
            lock_timeouts = sum(1 for c in run.calls if c.error and "55P03" in c.error)
            label = f"lab phase {phase.name} ({phase.label}), {config.duration_s:g}s at {config.total_qps:g} qps baseline"
            provenance = Provenance.MEASURED if phase.index_candidate else Provenance.OBSERVED
            counters = {**delta, "transactions_per_s": round((delta["xact_commit"] + delta["xact_rollback"])
                                                              / config.duration_s, 1),
                        "lock_waits": sum(waiting), "lock_wait_session_seconds": round(sum(waiting) * SAMPLE_INTERVAL_S, 2),
                        "max_sessions_waiting_on_locks": max(waiting), "lock_timeouts": lock_timeouts,
                        "lock_wait_note": LOCK_WAIT_NOTE}
            items.append(EvidenceItem(
                id=f"EV-LAB-{phase.name}-DIG", kind=EvidenceKind.QUERY_DIGEST, title=f"Statement statistics, {label}",
                provenance=provenance, source="lab:pg_stat_statements",
                method=f"{config.workers} workers, Poisson arrivals, seed {config.seed}", window_start=now,
                data={"phase": phase.name, "window": label, "fingerprints": fingerprints, "unmapped_db_time_pct": unmapped_pct},
                **common))
            lat_item = EvidenceItem(
                id=f"EV-LAB-{phase.name}-LAT", kind=EvidenceKind.METRIC_SUMMARY, title=f"Client latency and server counters, {label}",
                provenance=provenance, source="lab:client+pg_stat_database+pg_stat_activity", method="per-call client timing",
                data={"phase": phase.name, "client_latency": latency, "server_counters": counters,
                      "active_sessions": {"max": max(running), "mean": round(sum(running) / len(running), 2)},
                      "db_load_average_active_sessions": round(total_time / (config.duration_s * 1000), 3),
                      "batch_job": {"running": phase.batch, "chunks_committed": run.batch_chunks, "errors": run.batch_errors},
                      "index_candidate": phase.index_candidate},
                **common)
            items.append(lat_item)
            for fid, doc in plans.items():
                steps = pg_collector.parse_plan(doc)
                items.append(EvidenceItem(
                    id=f"EV-LAB-{phase.name}-PLAN-{'AUD' if fid == 'QF-AUDIENCE' else 'OH'}", kind=EvidenceKind.QUERY_PLAN,
                    title=f"EXPLAIN (ANALYZE, BUFFERS) for {fid}, {label}", provenance=provenance,
                    source="lab:EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)",
                    data={"fingerprint_id": fid, "phase": phase.name, "statistics_last_analyzed_days_ago": 0,
                          "steps": steps, "total_latency_ms": doc.get("Execution Time"),
                          "planning_ms": doc.get("Planning Time"), "raw_json": json.dumps(doc)[:20000]},
                    **common))
            summaries[phase.name] = {"latency": latency, "fingerprints": {f["fingerprint_id"]: f for f in fingerprints},
                                     "lock_waits": counters["lock_waits"], "deadlocks": delta["deadlocks"],
                                     "db_load": lat_item.data["db_load_average_active_sessions"],
                                     "sessions_max": max(running)}

        names = [p.name for p in phases]
        comparison = {}
        for fid in sorted({f for s in summaries.values() for f in s["latency"]}):
            comparison[fid] = {n: {"calls": summaries[n]["latency"].get(fid, {}).get("calls", 0),
                                   "low_sample": summaries[n]["latency"].get(fid, {}).get("low_sample", True),
                                   "p95_ms": summaries[n]["latency"].get(fid, {}).get("p95_ms"),
                                   "errors": summaries[n]["latency"].get(fid, {}).get("errors"),
                                   "share_of_db_time_pct": summaries[n]["fingerprints"].get(fid, {}).get("share_of_db_time_pct"),
                                   "avg_shared_blocks": summaries[n]["fingerprints"].get(fid, {}).get("avg_shared_blocks")}
                               for n in names}
        items.append(EvidenceItem(
            id="EV-LAB-CMP", kind=EvidenceKind.EXPERIMENT_RESULT, title=f"Lab phase comparison ({', '.join(names)})",
            provenance=Provenance.MEASURED, source="lab:runner", method="same seed and rates per phase; index created only in its phase",
            data={"scenario_id": scenario.id,
                  "phases": [{"name": p.name, "label": p.label, "multiplier": p.multiplier, "batch": p.batch,
                              "index_candidate": p.index_candidate} for p in phases],
                  "by_fingerprint": comparison,
                  "lock_waits": {n: summaries[n]["lock_waits"] for n in names},
                  "avg_row_lock_wait_ms": {n: None for n in names},
                  "deadlocks": {n: summaries[n]["deadlocks"] for n in names},
                  "db_load_average_active_sessions": {n: summaries[n]["db_load"] for n in names},
                  "threads_running_max": {n: summaries[n]["sessions_max"] for n in names},
                  "lock_wait_note": LOCK_WAIT_NOTE,
                  "low_sample_note": f"p95 values from fewer than {MIN_RELIABLE_CALLS} calls are unreliable; "
                                     "run longer phases or a higher --qps before relying on them.",
                  "percona_toolkit": None, "caveat": PG_CAVEAT,
                  "engine": engine, "config": config.model_dump()},
            **common))
        return items
    finally:
        sandbox.close()
