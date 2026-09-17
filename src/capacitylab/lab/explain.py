# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parse MySQL 8.0 `EXPLAIN ANALYZE` tree output into plan steps."""

from __future__ import annotations

import re

_NUM = r"[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?"
_LINE_RE = re.compile(
    rf"^(?P<indent>\s*)->\s*(?P<op>.*?)\s*"
    rf"(?:\(cost=(?P<cost>{_NUM})(?:\.\.{_NUM})?\s+rows=(?P<est>{_NUM})\))?\s*"
    rf"(?:\(actual time=(?P<t0>{_NUM})\.\.(?P<t1>{_NUM})\s+rows=(?P<act>{_NUM})\s+loops=(?P<loops>\d+)\))?\s*$",
    re.IGNORECASE,
)
_TABLE_RE = re.compile(r"\bon (?P<table><?[\w`]+>?)(?: using (?P<index><?[\w`]+>?))?", re.IGNORECASE)


def parse_explain_analyze(text: str) -> list[dict]:
    """One dict per plan node. Row counts are per loop, as MySQL reports them."""
    steps: list[dict] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = _LINE_RE.match(raw.rstrip())
        if not m:
            continue
        op = m.group("op")
        table = _TABLE_RE.search(op)
        step = {
            "depth": len(m.group("indent")) // 4,
            "operation": op,
            "access": op.split(" on ")[0] if " on " in op else op.split(":")[0],
            "table": table.group("table").strip("`") if table else None,
            "index": (table.group("index") or "").strip("`") or None if table else None,
            "estimated_rows": float(m.group("est")) if m.group("est") else None,
            "actual_rows": float(m.group("act")) if m.group("act") else None,
            "loops": int(m.group("loops")) if m.group("loops") else None,
            "actual_time_ms": float(m.group("t1")) if m.group("t1") else None,
        }
        if step["estimated_rows"] is not None and step["actual_rows"] is not None:
            est, act = max(step["estimated_rows"], 1.0), max(step["actual_rows"], 1.0)
            step["q_error"] = round(max(est / act, act / est), 1)
        steps.append(step)
    return steps


def root_latency_ms(steps: list[dict]) -> float | None:
    for step in steps:
        if step["actual_time_ms"] is not None:
            return round(step["actual_time_ms"] * (step["loops"] or 1), 3)
    return None
