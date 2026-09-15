"""Build evidence from exports of a database you own.

Supported inputs:
- MySQL-format slow query logs;
- performance_schema.events_statements_summary_by_digest exports (CSV with a header row, or a JSON list);
- MySQL 8.0 `EXPLAIN ANALYZE` text;
- CloudWatch `get-metric-statistics` JSON output.

Imported items are labeled environment "import" and synthetic=False. SQL text and messages are redacted
(emails, IPv4 addresses) before they are stored. Review imported files before sharing a run ledger.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.evidence.normalize import (
    TenantAttribution,
    digest_slow_log,
    parse_slow_log,
    redact_text,
    summarize_datapoints,
)
from capacitylab.lab.explain import parse_explain_analyze, root_latency_ms

PICO_PER_MS = 1e9
IMPORT_CAVEAT = "Imported from a file you supplied; CapacityLab did not verify how or where it was collected."


def evidence_id(prefix: str, path: str | Path) -> str:
    stem = re.sub(r"[^A-Z0-9]+", "-", Path(path).stem.upper()).strip("-") or "FILE"
    return f"EV-IMP-{prefix}-{stem}"[:60].rstrip("-")


def import_slow_log(path: str | Path, attribution: TenantAttribution | None = None,
                    long_query_time_s: float | None = None) -> EvidenceItem:
    entries = parse_slow_log(Path(path).read_text(errors="replace"), attribution)
    if not entries:
        raise ValueError(f"no slow log entries found in {path}")
    digest = digest_slow_log(entries, long_query_time_s)
    times = [e.timestamp for e in entries if e.timestamp]
    return EvidenceItem(
        id=evidence_id("SLOW", path), kind=EvidenceKind.QUERY_DIGEST, title=f"Slow log digest from {Path(path).name}",
        provenance=Provenance.OBSERVED, synthetic=False, environment="import", source=f"import:slow_log:{Path(path).name}",
        method="parse_slow_log + digest_slow_log (literals removed, emails/IPs redacted)",
        window_start=min(times) if times else None, window_end=max(times) if times else None,
        data=digest, caveats=[IMPORT_CAVEAT, *digest["caveats"]],
    )


def _rows_from_file(path: Path) -> list[dict]:
    text = path.read_text(errors="replace")
    if path.suffix.lower() == ".json":
        doc = json.loads(text)
        rows = doc if isinstance(doc, list) else doc.get("rows", [])
    else:
        rows = list(csv.DictReader(text.splitlines()))
    return [{k.upper(): v for k, v in row.items()} for row in rows]


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def import_digest_export(path: str | Path, window_seconds: float | None = None) -> EvidenceItem:
    """Columns follow performance_schema names (DIGEST_TEXT, COUNT_STAR, SUM_TIMER_WAIT, ...). Timers are picoseconds."""
    p = Path(path)
    rows = _rows_from_file(p)
    if not rows or "COUNT_STAR" not in rows[0]:
        raise ValueError(f"{path} does not look like an events_statements_summary_by_digest export")
    total_time = sum(_num(r.get("SUM_TIMER_WAIT")) for r in rows) or 1.0
    total_examined = sum(_num(r.get("SUM_ROWS_EXAMINED")) for r in rows) or 1.0
    fingerprints = []
    for r in rows:
        calls = max(1.0, _num(r.get("COUNT_STAR")))
        cpu = _num(r.get("SUM_CPU_TIME"))
        entry = {
            "fingerprint_id": "QF-" + (r.get("DIGEST") or "")[:12].upper() if r.get("DIGEST") else f"QF-ROW-{len(fingerprints) + 1}",
            "schema": r.get("SCHEMA_NAME"),
            "normalized": redact_text((r.get("DIGEST_TEXT") or "")[:400]),
            "calls": int(calls),
            "calls_per_s": round(calls / window_seconds, 3) if window_seconds else None,
            "avg_latency_ms": round(_num(r.get("SUM_TIMER_WAIT")) / PICO_PER_MS / calls, 3),
            "p95_latency_ms": round(_num(r.get("QUANTILE_95")) / PICO_PER_MS, 3) if r.get("QUANTILE_95") else None,
            "avg_rows_examined": round(_num(r.get("SUM_ROWS_EXAMINED")) / calls, 1),
            "avg_rows_sent": round(_num(r.get("SUM_ROWS_SENT")) / calls, 1),
            "avg_lock_wait_ms": round(_num(r.get("SUM_LOCK_TIME")) / PICO_PER_MS / calls, 3),
            "tmp_disk_tables": int(_num(r.get("SUM_CREATED_TMP_DISK_TABLES"))),
            "no_index_used": int(_num(r.get("SUM_NO_INDEX_USED"))),
            "share_of_db_time_pct": round(100 * _num(r.get("SUM_TIMER_WAIT")) / total_time, 1),
            "share_of_rows_examined_pct": round(100 * _num(r.get("SUM_ROWS_EXAMINED")) / total_examined, 1),
        }
        if cpu:
            entry["avg_cpu_ms_measured"] = round(cpu / PICO_PER_MS / calls, 3)
        fingerprints.append(entry)
    fingerprints.sort(key=lambda f: f["share_of_db_time_pct"], reverse=True)
    return EvidenceItem(
        id=evidence_id("DIG", p), kind=EvidenceKind.QUERY_DIGEST, title=f"Statement digest export {p.name}",
        provenance=Provenance.OBSERVED, synthetic=False, environment="import", source=f"import:performance_schema:{p.name}",
        method="events_statements_summary_by_digest export", data={"source": "statement_digest_summary", "fingerprints": fingerprints},
        caveats=[IMPORT_CAVEAT] + ([] if window_seconds else ["No window length given; calls_per_s is unknown."]),
    )


def import_explain_analyze(path: str | Path, fingerprint_id: str) -> EvidenceItem:
    p = Path(path)
    text = p.read_text(errors="replace")
    steps = parse_explain_analyze(text)
    if not steps:
        raise ValueError(f"no EXPLAIN ANALYZE plan nodes found in {path}")
    return EvidenceItem(
        id=evidence_id("PLAN", p), kind=EvidenceKind.QUERY_PLAN, title=f"EXPLAIN ANALYZE for {fingerprint_id} ({p.name})",
        provenance=Provenance.OBSERVED, synthetic=False, environment="import", source=f"import:explain_analyze:{p.name}",
        data={"fingerprint_id": fingerprint_id, "statistics_last_analyzed_days_ago": None, "steps": steps,
              "total_latency_ms": root_latency_ms(steps), "raw_text": redact_text(text)},
        caveats=[IMPORT_CAVEAT, "Statistics age unknown for imported plans."],
    )


def import_cloudwatch_json(path: str | Path, unit: str = "", period_seconds: int = 60) -> EvidenceItem:
    p = Path(path)
    doc = json.loads(p.read_text())
    points = doc.get("Datapoints", [])
    for point in points:
        if isinstance(point.get("Timestamp"), str):
            point["Timestamp"] = datetime.fromisoformat(point["Timestamp"].replace("Z", "+00:00"))
    summary = summarize_datapoints(points, unit or (points[0].get("Unit", "") if points else ""), period_seconds)
    metric = doc.get("Label") or p.stem
    return EvidenceItem(
        id=evidence_id("MET", p), kind=EvidenceKind.METRIC_SUMMARY, title=f"{metric} from {p.name}",
        provenance=Provenance.OBSERVED, synthetic=False, environment="import", source=f"import:cloudwatch:{p.name}",
        method=f"get-metric-statistics output, period {period_seconds}s", data={"metric": metric, **summary},
        caveats=[IMPORT_CAVEAT] + ([summary["caveat"]] if "caveat" in summary else []),
    )


# --- Percona Toolkit output ------------------------------------------------------------------------


def import_pt_query_digest(path: str | Path) -> EvidenceItem:
    """`pt-query-digest --output json` output."""
    from capacitylab.lab.percona import summarize_query_digest

    p = Path(path)
    try:
        doc = json.loads(p.read_text())
    except ValueError as exc:
        raise ValueError(f"{path} is not pt-query-digest JSON (run it with --output json)") from exc
    if "classes" not in doc:
        raise ValueError(f"{path} has no 'classes'; expected pt-query-digest --output json")
    return EvidenceItem(
        id=evidence_id("PTQD", p), kind=EvidenceKind.QUERY_DIGEST, title=f"pt-query-digest report {p.name}",
        provenance=Provenance.OBSERVED, synthetic=False, environment="import", source=f"import:pt-query-digest:{p.name}",
        data=summarize_query_digest(doc), caveats=[IMPORT_CAVEAT, "pt-query-digest only sees statements in the log it was given."],
    )


def import_pt_duplicate_keys(path: str | Path) -> EvidenceItem:
    """`pt-duplicate-key-checker` text output."""
    from capacitylab.lab.percona import parse_duplicate_keys

    p = Path(path)
    report = parse_duplicate_keys(p.read_text(errors="replace"))
    if report["total_indexes"] is None:
        raise ValueError(f"{path} does not look like pt-duplicate-key-checker output")
    return EvidenceItem(
        id=evidence_id("PTDK", p), kind=EvidenceKind.SCHEMA, title=f"pt-duplicate-key-checker report {p.name}",
        provenance=Provenance.OBSERVED, synthetic=False, environment="import", source=f"import:pt-duplicate-key-checker:{p.name}",
        data=report, caveats=[IMPORT_CAVEAT, "A redundant index may still be relied on by a statement; check plans before dropping."],
    )


def import_pt_deadlocks(path: str | Path) -> EvidenceItem:
    """`pt-deadlock-logger --tab` output."""
    from capacitylab.lab.percona import parse_deadlocks

    p = Path(path)
    text = p.read_text(errors="replace")
    if not text.startswith("server\t"):
        raise ValueError(f"{path} does not look like pt-deadlock-logger --tab output")
    return EvidenceItem(
        id=evidence_id("PTDL", p), kind=EvidenceKind.METRIC_SUMMARY, title=f"pt-deadlock-logger report {p.name}",
        provenance=Provenance.OBSERVED, synthetic=False, environment="import", source=f"import:pt-deadlock-logger:{p.name}",
        data=parse_deadlocks(text), caveats=[IMPORT_CAVEAT],
    )
