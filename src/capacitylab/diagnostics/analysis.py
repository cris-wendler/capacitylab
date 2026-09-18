# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evidence-only diagnostics (no database access). Each returns (payload, cited evidence ids)."""

from __future__ import annotations

from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.evidence.models import EvidenceKind
from capacitylab.scenarios.models import Scenario, hhmm_to_minutes

RANK_FIELDS = {"cpu": "share_of_cpu_pct", "rows_examined": "share_of_rows_examined_pct", "latency": "total_latency_ms_per_s"}


def _require(bundle: EvidenceBundle, kind: EvidenceKind):
    item = bundle.first(kind)
    if item is None:
        raise LookupError(f"no {kind.value} evidence available")
    return item


def top_queries(bundle: EvidenceBundle, rank_by: str = "cpu", limit: int = 5) -> tuple[dict, list[str]]:
    if rank_by not in RANK_FIELDS:
        raise ValueError(f"rank_by must be one of {sorted(RANK_FIELDS)}")
    digest = _require(bundle, EvidenceKind.QUERY_DIGEST)
    rows = []
    for f in digest.data.get("fingerprints", []):
        total_latency = (f.get("calls_per_s") or 0) * (f.get("avg_latency_ms") or 0)
        rows.append(
            {
                "fingerprint_id": f["fingerprint_id"],
                "label": f.get("label", ""),
                "calls_per_s": f.get("calls_per_s"),
                "share_of_cpu_pct": f.get("share_of_cpu_pct"),
                "share_of_rows_examined_pct": f.get("share_of_rows_examined_pct"),
                "total_latency_ms_per_s": round(total_latency, 1),
                "avg_rows_examined": f.get("avg_rows_examined"),
                "avg_rows_sent": f.get("avg_rows_sent"),
                "examined_to_sent_ratio": round(f["avg_rows_examined"] / f["avg_rows_sent"], 1)
                if f.get("avg_rows_sent")
                else None,
            }
        )
    key = RANK_FIELDS[rank_by]
    rows.sort(key=lambda r: r.get(key) or 0, reverse=True)
    return {"ranked_by": key, "queries": rows[:limit], "window": digest.data.get("window")}, [digest.id]


def tenant_skew(bundle: EvidenceBundle, viewer_tenant: str | None = None) -> tuple[dict, list[str]]:
    digest = _require(bundle, EvidenceKind.QUERY_DIGEST)
    out = []
    for f in digest.data.get("fingerprints", []):
        shares = {t: v.get("share_of_calls_pct", 0) for t, v in (f.get("by_tenant") or {}).items()}
        if not shares:
            continue
        top_share = max(shares.values())
        entry = {"fingerprint_id": f["fingerprint_id"], "skewed": top_share >= 50}
        if viewer_tenant is None:
            # Per-tenant values live under "by_tenant" so evidence redaction can scope them for tenant viewers.
            entry["top_tenant_share_pct"] = top_share
            entry["by_tenant"] = {t: {"share_of_calls_pct": v} for t, v in shares.items()}
        else:
            entry["own_share_pct"] = shares.get(viewer_tenant, 0)
        out.append(entry)
    return {"fingerprints": out, "viewer_tenant": viewer_tenant}, [digest.id]


def table_growth_review(bundle: EvidenceBundle) -> tuple[dict, list[str]]:
    stats = _require(bundle, EvidenceKind.TABLE_STATS)
    reviews = []
    for t in stats.data.get("tables", []):
        accesses = t.get("accessed_by", {})
        growth_sensitive = {fid: text for fid, text in accesses.items() if "grow" in text or "scan" in text}
        if growth_sensitive:
            conclusion = (
                "Investigate: statements whose examined rows grow with this table touch it: "
                + ", ".join(sorted(growth_sensitive))
            )
        else:
            conclusion = (
                "No query-latency optimization indicated by size alone: accesses are bounded "
                "(inserts or index lookups). Retention or archiving is a storage-cost question, not a performance fix."
            )
        reviews.append(
            {
                "table": t["table"],
                "rows": t.get("rows"),
                "data_gib": t.get("data_gib"),
                "growth_pct_per_week": t.get("growth_pct_per_week"),
                "growth_sensitive_statements": sorted(growth_sensitive),
                "conclusion": conclusion,
            }
        )
    reviews.sort(key=lambda r: r.get("data_gib") or 0, reverse=True)
    return {"tables": reviews}, [stats.id]


def bottleneck_classifier(bundle: EvidenceBundle) -> tuple[dict, list[str]]:
    summary = _require(bundle, EvidenceKind.METRIC_SUMMARY)
    m = summary.data
    findings = []
    cpu = m.get("CPUUtilization", {})
    if (cpu.get("max_1min_pct") or 0) >= 70:
        findings.append({"resource": "cpu", "severity": "high", "detail": f"1-minute CPU peak {cpu['max_1min_pct']}%"})
    hit = (m.get("BufferPoolHitRatioPct") or {}).get("min")
    if hit is not None and hit < 99.0:
        findings.append({"resource": "memory_io", "severity": "high", "detail": f"buffer pool hit ratio down to {hit}%"})
    locks = m.get("RowLockWaitsPerSecond") or {}
    if locks.get("outside_batch") and locks.get("during_batch", 0) / locks["outside_batch"] >= 10:
        findings.append(
            {"resource": "contention", "severity": "medium",
             "detail": f"row lock waits {locks['during_batch']}/s during batch vs {locks['outside_batch']}/s outside"}
        )
    conns = m.get("DatabaseConnections") or {}
    if conns.get("configured_max_connections") and conns.get("max", 0) / conns["configured_max_connections"] >= 0.8:
        findings.append({"resource": "connections", "severity": "high", "detail": "connections near the configured maximum"})
    spills = (m.get("TmpDiskTablesPerSecond") or {}).get("mean")
    if spills:
        findings.append({"resource": "temporary_spills", "severity": "low", "detail": f"{spills} on-disk temporary tables/s"})
    not_indicated = sorted({"cpu", "memory_io", "contention", "connections"} - {f["resource"] for f in findings})
    primary = findings[0]["resource"] if findings else "none_detected"
    return {"primary": primary, "findings": findings, "not_indicated": not_indicated}, [summary.id]


def row_estimate_check(bundle: EvidenceBundle, fingerprint_id: str) -> tuple[dict, list[str]]:
    plans = [p for p in bundle.by_kind(EvidenceKind.QUERY_PLAN) if p.data.get("fingerprint_id") == fingerprint_id]
    if not plans:
        raise LookupError(f"no execution plan evidence for {fingerprint_id}")
    plan = plans[0]
    steps = []
    for s in plan.data.get("steps", []):
        est, act = s.get("estimated_rows"), s.get("actual_rows")
        # Floor at one row so fractional per-loop actuals (e.g. 0.06 rows) do not produce absurd ratios.
        q_error = (round(max(max(est, 1) / max(act, 1), max(act, 1) / max(est, 1)), 1)
                   if est is not None and act is not None else None)
        steps.append({"table": s.get("table"), "estimated_rows": est, "actual_rows": act, "q_error": q_error})
    worst = max((s["q_error"] or 1 for s in steps), default=1)
    age = plan.data.get("statistics_last_analyzed_days_ago")
    interpretation = []
    if worst >= 10:
        interpretation.append(f"Severe cardinality misestimate (worst q-error {worst}); the optimizer may choose a poor plan.")
    if age is not None and age > 30:
        interpretation.append(f"Statistics are {age} days old; refreshing them is a cheap first test.")
    interpretation.append("Fresh statistics cannot make an index usable for a range it does not cover.")
    return {"fingerprint_id": fingerprint_id, "steps": steps, "worst_q_error": worst, "statistics_age_days": age,
            "interpretation": interpretation}, [plan.id]


def batch_reschedule_check(scenario: Scenario, bundle: EvidenceBundle, candidate_starts: list[str]) -> tuple[dict, list[str]]:
    batches = scenario.events_of("batch_job")
    if not batches:
        raise LookupError("scenario has no batch job")
    batch = batches[0]
    cited = []
    duration = batch.duration_minutes
    log = bundle.first(EvidenceKind.BATCH_RUN_LOG)
    if log and log.data.get("durations_minutes"):
        duration = max(log.data["durations_minutes"])
        cited.append(log.id)
    schedule = bundle.first(EvidenceKind.BATCH_SCHEDULE)
    if schedule:
        cited.append(schedule.id)
    busy = [(e.id, hhmm_to_minutes(e.start), hhmm_to_minutes(e.end)) for e in scenario.events_of("campaign")]
    deadline = 24 * 60 + hhmm_to_minutes(batch.deadline_next_day)
    results = []
    for start in candidate_starts:
        s = hhmm_to_minutes(start)
        next_day = s < scenario.horizon.start_minutes
        abs_start = s + (24 * 60 if next_day else 0)
        abs_end = abs_start + duration
        overlaps = [eid for eid, bs, be in busy if abs_start < be and abs_end > bs]
        results.append(
            {
                "start": start,
                "next_day": next_day,
                "worst_case_end": f"{(abs_end // 60) % 24:02d}:{abs_end % 60:02d}",
                "meets_deadline": abs_end <= deadline,
                "overlaps_events": overlaps,
            }
        )
    return {"job": batch.name, "worst_case_duration_minutes": duration, "deadline_next_day": batch.deadline_next_day,
            "candidates": results}, cited
