# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only collection from a PostgreSQL lab database: pg_stat_statements, pg_stat_database, plans, table sizes."""

from __future__ import annotations

import json
import re
from typing import Any

from capacitylab.evidence.normalize import redact_text

DATABASE_KEYS = ("xact_commit", "xact_rollback", "deadlocks", "blks_read", "blks_hit", "temp_files", "temp_bytes",
                 "tup_returned", "tup_fetched", "tup_inserted", "tup_updated")

_STATEMENTS_SQL = (
    "SELECT query, calls, total_exec_time, rows, shared_blks_hit, shared_blks_read, temp_blks_written "
    "FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())"
)


def reset_statements(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_stat_statements_reset()")


def database_stats(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_stat_clear_snapshot()")
        cur.execute(f"SELECT {', '.join(DATABASE_KEYS)} FROM pg_stat_database WHERE datname = current_database()")
        row = cur.fetchone()
    return {key: int(value or 0) for key, value in zip(DATABASE_KEYS, row, strict=True)}


def stats_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {k: after.get(k, 0) - before.get(k, 0) for k in DATABASE_KEYS}


def normalize_statement(sql: str) -> str:
    """Comparable form of a statement: placeholders of either driver or pg_stat_statements become `?`."""
    text = re.sub(r"%\(\w+\)s|\$\d+|:\w+", "?", sql)
    text = re.sub(r"(?<![\w.])-?\d+(?:\.\d+)?\b|'(?:[^']|'')*'", "?", text)
    return re.sub(r"\s+", " ", text).strip().lower()


_LAB_OVERHEAD = re.compile(r"^\s*(set\b|explain\b|select\b.*\bpg_stat_(activity|database|statements))", re.I | re.S)


def is_lab_overhead(sql: str) -> bool:
    """The lab's own sampling and collection statements, which are not part of the simulated workload."""
    return bool(_LAB_OVERHEAD.match(sql))


def statement_rows(conn, templates: dict[str, list[str]]) -> list[dict]:
    """pg_stat_statements rows, each mapped to a scenario statement id when its text matches a known template."""
    lookup = {normalize_statement(sql): fid for fid, statements in templates.items() for sql in statements}
    with conn.cursor() as cur:
        cur.execute(_STATEMENTS_SQL)
        rows = cur.fetchall()
    out = []
    for query, calls, total_ms, returned, hit, read, temp in rows:
        out.append({
            "fingerprint_id": lookup.get(normalize_statement(query)),
            "lab_overhead": is_lab_overhead(query),
            "statement_text": redact_text(query[:400]),
            "calls": int(calls),
            "total_db_time_ms": round(float(total_ms), 3),
            "rows": int(returned),
            "shared_blocks": int(hit) + int(read),
            "shared_blocks_read": int(read),
            "temp_blocks_written": int(temp),
        })
    return out


def explain_analyze(conn, sql: str, params: dict[str, Any]) -> dict:
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
        doc = cur.fetchone()[0]
    return (json.loads(doc) if isinstance(doc, str) else doc)[0]


def parse_plan(doc: dict) -> list[dict]:
    """Plan steps in the same shape as the MySQL parser. Rows are per loop, as PostgreSQL reports them."""
    steps: list[dict] = []

    def walk(node: dict, depth: int) -> None:
        est, act = node.get("Plan Rows"), node.get("Actual Rows")
        step = {
            "depth": depth,
            "operation": " ".join(p for p in (node.get("Node Type"), node.get("Join Type")) if p),
            "access": node.get("Node Type"),
            "table": node.get("Relation Name"),
            "index": node.get("Index Name"),
            "estimated_rows": float(est) if est is not None else None,
            "actual_rows": float(act) if act is not None else None,
            "loops": int(node["Actual Loops"]) if node.get("Actual Loops") is not None else None,
            "actual_time_ms": float(node["Actual Total Time"]) if node.get("Actual Total Time") is not None else None,
            "shared_blocks": int(node.get("Shared Hit Blocks", 0) + node.get("Shared Read Blocks", 0)),
        }
        if step["estimated_rows"] is not None and step["actual_rows"] is not None:
            e, a = max(step["estimated_rows"], 1.0), max(step["actual_rows"], 1.0)
            step["q_error"] = round(max(e / a, a / e), 1)
        steps.append(step)
        for child in node.get("Plans", []):
            walk(child, depth + 1)

    walk(doc["Plan"], 0)
    return steps


def table_stats(conn, tables: list[str]) -> list[dict]:
    out = []
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"ANALYZE {table}")
        cur.execute(
            "SELECT c.relname, c.reltuples, pg_relation_size(c.oid), pg_indexes_size(c.oid) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "ORDER BY pg_relation_size(c.oid) DESC")
        sizes = cur.fetchall()
        cur.execute("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
        indexes: dict[str, dict[str, list[str]]] = {}
        for table, index, definition in cur.fetchall():
            columns = re.search(r"\((.*)\)", definition)
            indexes.setdefault(table, {})[index] = [c.strip() for c in columns.group(1).split(",")] if columns else []
    for table, rows, data, index in sizes:
        out.append({"table": table, "rows_estimate": int(max(rows or 0, 0)), "data_mib": round(int(data) / 2**20, 2),
                    "index_mib": round(int(index) / 2**20, 2), "indexes": indexes.get(table, {})})
    return out
