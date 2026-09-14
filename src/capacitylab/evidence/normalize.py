"""Normalization of raw database telemetry into evidence payloads.

Covers the two most common exports (MySQL-format slow query logs and CloudWatch-style metric
datapoints) plus query fingerprinting, tenant attribution, and redaction.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_STRING_LIT_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"")
_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?\b")
_IN_LIST_RE = re.compile(r"\bin\s*\((?:\s*\?\s*,)*\s*\?\s*\)", re.IGNORECASE)
_VALUES_RE = re.compile(r"\bvalues\s*(?:\((?:[^()]*)\)\s*,?\s*)+", re.IGNORECASE)
_COMMENT_RE = re.compile(r"/\*.*?\*/|--[^\n]*|#[^\n]*", re.DOTALL)


def redact_text(text: str) -> str:
    """Remove personal data and network identifiers from free text (e.g. SQL samples, log lines)."""
    text = _EMAIL_RE.sub("<email>", text)
    return _IPV4_RE.sub("<ip>", text)


def fingerprint_sql(sql: str) -> str:
    """Abstract a statement into a literal-free fingerprint (similar in spirit to pt-query-digest)."""
    s = _COMMENT_RE.sub(" ", sql)
    s = _STRING_LIT_RE.sub("?", s)
    s = _NUMBER_RE.sub("?", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(";").strip().lower()
    s = _IN_LIST_RE.sub("in (?+)", s)
    s = _VALUES_RE.sub("values (?+)", s)
    return s


def fingerprint_id(fingerprint: str) -> str:
    return "QF-" + hashlib.sha1(fingerprint.encode()).hexdigest()[:10].upper()


class TenantAttribution(BaseModel):
    """How statements are attributed to tenants. The right mode depends on the isolation model."""

    mode: Literal["schema_map", "tenant_column", "none"] = "tenant_column"
    schema_map: dict[str, str] = Field(default_factory=dict)  # schema name -> tenant key
    tenant_column: str = "tenant_id"
    literal_map: dict[str, str] = Field(default_factory=dict)  # tenant column literal -> tenant key

    def attribute(self, sql: str, schema: str | None) -> str | None:
        if self.mode == "schema_map":
            return self.schema_map.get(schema or "")
        if self.mode == "tenant_column":
            m = re.search(rf"\b(?:\w+\.)?{re.escape(self.tenant_column)}\s*=\s*'?([\w-]+)'?", sql, re.IGNORECASE)
            if not m:
                return None
            return self.literal_map.get(m.group(1), f"tenant:{m.group(1)}")
        return None


class SlowLogEntry(BaseModel):
    timestamp: datetime | None
    user: str | None
    thread_id: int | None
    query_time_s: float
    lock_time_s: float
    rows_sent: int
    rows_examined: int
    schema_name: str | None
    sql: str  # redacted
    fingerprint: str
    fingerprint_id: str
    tenant: str | None = None


_TIME_RE = re.compile(r"^# Time:\s*(\S+)")
_USER_RE = re.compile(r"^# User@Host:\s*([\w-]+)\[[^\]]*\]\s*@\s*\S*\s*(?:\[[^\]]*\])?\s*(?:Id:\s*(\d+))?")
_STATS_RE = re.compile(
    r"^# Query_time:\s*([\d.]+)\s+Lock_time:\s*([\d.]+)\s+Rows_sent:\s*(\d+)\s+Rows_examined:\s*(\d+)"
)
_USE_RE = re.compile(r"^use\s+`?(\w+)`?\s*;", re.IGNORECASE)


def parse_slow_log(text: str, attribution: TenantAttribution | None = None) -> list[SlowLogEntry]:
    """Parse MySQL-format slow query log text. Unparseable fragments are skipped, not guessed."""
    attribution = attribution or TenantAttribution(mode="none")
    entries: list[SlowLogEntry] = []
    schema_by_thread: dict[int | None, str | None] = {}
    ts: datetime | None = None
    user: str | None = None
    thread: int | None = None
    stats: tuple[float, float, int, int] | None = None
    sql_lines: list[str] = []

    def flush() -> None:
        nonlocal stats, sql_lines
        if stats is None:
            sql_lines = []
            return
        statement = "\n".join(line for line in sql_lines if not line.lower().startswith("set timestamp")).strip()
        if statement:
            fp = fingerprint_sql(statement)
            schema = schema_by_thread.get(thread)
            entries.append(
                SlowLogEntry(
                    timestamp=ts,
                    user=user,
                    thread_id=thread,
                    query_time_s=stats[0],
                    lock_time_s=stats[1],
                    rows_sent=stats[2],
                    rows_examined=stats[3],
                    schema_name=schema,
                    sql=redact_text(statement),
                    fingerprint=fp,
                    fingerprint_id=fingerprint_id(fp),
                    tenant=attribution.attribute(statement, schema),
                )
            )
        stats = None
        sql_lines = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if m := _TIME_RE.match(line):
            flush()
            try:
                ts = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            except ValueError:
                ts = None
            continue
        if m := _USER_RE.match(line):
            flush()
            user = m.group(1)
            thread = int(m.group(2)) if m.group(2) else None
            continue
        if m := _STATS_RE.match(line):
            flush()
            stats = (float(m.group(1)), float(m.group(2)), int(m.group(3)), int(m.group(4)))
            continue
        if line.startswith("#"):
            continue
        if m := _USE_RE.match(line):
            schema_by_thread[thread] = m.group(1)
            continue
        if stats is not None:
            sql_lines.append(line)
    flush()
    return entries


def _nearest_rank(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return math.nan
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def digest_slow_log(entries: list[SlowLogEntry], long_query_time_s: float | None = None) -> dict:
    """Aggregate slow log entries by fingerprint, with per-tenant breakdowns."""
    groups: dict[str, list[SlowLogEntry]] = defaultdict(list)
    for e in entries:
        groups[e.fingerprint_id].append(e)
    total_time = sum(e.query_time_s for e in entries) or 1.0
    total_examined = sum(e.rows_examined for e in entries) or 1
    fingerprints = []
    for fid, group in groups.items():
        times = sorted(e.query_time_s for e in group)
        by_tenant: dict[str, dict] = defaultdict(lambda: {"calls": 0, "query_time_s": 0.0})
        for e in group:
            bucket = by_tenant[e.tenant or "unattributed"]
            bucket["calls"] += 1
            bucket["query_time_s"] = round(bucket["query_time_s"] + e.query_time_s, 3)
        fingerprints.append(
            {
                "fingerprint_id": fid,
                "fingerprint": group[0].fingerprint,
                "calls": len(group),
                "total_query_time_s": round(sum(times), 3),
                "p95_query_time_s": round(_nearest_rank(times, 95), 3),
                "total_lock_time_s": round(sum(e.lock_time_s for e in group), 3),
                "avg_rows_examined": round(sum(e.rows_examined for e in group) / len(group), 1),
                "avg_rows_sent": round(sum(e.rows_sent for e in group) / len(group), 1),
                "share_of_query_time_pct": round(100 * sum(times) / total_time, 1),
                "share_of_rows_examined_pct": round(100 * sum(e.rows_examined for e in group) / total_examined, 1),
                "by_tenant": dict(by_tenant),
            }
        )
    fingerprints.sort(key=lambda f: f["total_query_time_s"], reverse=True)
    caveats = [
        "Slow logs only contain statements above long_query_time; frequent fast statements that dominate load "
        "can be absent. Prefer a statement digest summary for load attribution."
    ]
    if long_query_time_s is not None:
        caveats.append(f"long_query_time assumed to be {long_query_time_s} s.")
    return {"source": "slow_log", "statements": len(entries), "fingerprints": fingerprints, "caveats": caveats}


def summarize_datapoints(datapoints: list[dict], unit: str = "", period_seconds: int = 300) -> dict:
    """Summarize CloudWatch-style datapoints (Average/Maximum/Minimum per period).

    A "p95" computed over period averages is a common summary, and it hides spikes shorter than the
    period, so it is reported here under an explicit name next to the true maximum.
    """
    points = sorted(datapoints, key=lambda d: d["Timestamp"])
    averages = [float(d["Average"]) for d in points if "Average" in d]
    if not averages:
        return {"unit": unit, "data_points": 0, "period_seconds": period_seconds}
    maxima = [float(d["Maximum"]) for d in points if "Maximum" in d] or averages
    minima = [float(d["Minimum"]) for d in points if "Minimum" in d] or averages
    p95_avg = _nearest_rank(sorted(averages), 95)
    peak = max(maxima)
    summary = {
        "unit": unit,
        "period_seconds": period_seconds,
        "data_points": len(averages),
        "mean_of_period_averages": round(sum(averages) / len(averages), 4),
        "p95_of_period_averages": round(p95_avg, 4),
        "max_of_period_maxima": round(peak, 4),
        "min_of_period_minima": round(min(minima), 4),
        "latest_period_average": round(averages[-1], 4),
    }
    if p95_avg > 0 and peak / p95_avg >= 1.25:
        summary["caveat"] = (
            f"Peak ({peak:.4g}) exceeds the p95 of {period_seconds}s averages ({p95_avg:.4g}) by "
            f"{peak / p95_avg:.2f}x; short spikes are hidden by period averaging."
        )
    return summary
