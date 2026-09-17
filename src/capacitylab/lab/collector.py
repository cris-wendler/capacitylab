# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only collection from a MySQL 8.0 lab database (performance_schema, information_schema, status)."""

from __future__ import annotations

from typing import Any

from capacitylab.evidence.normalize import redact_text

STATUS_KEYS = (
    "Questions",
    "Innodb_row_lock_waits",
    "Innodb_row_lock_time",
    "Created_tmp_disk_tables",
    "Created_tmp_tables",
    "Handler_read_rnd_next",
    "Handler_read_key",
    "Handler_read_next",
    "Innodb_rows_read",
    "Innodb_rows_inserted",
    "Innodb_rows_updated",
)

_DIGEST_SQL = (
    "SELECT DIGEST, DIGEST_TEXT, COUNT_STAR, SUM_TIMER_WAIT, SUM_LOCK_TIME, SUM_ROWS_EXAMINED, SUM_ROWS_SENT, "
    "SUM_ROWS_AFFECTED, SUM_CREATED_TMP_DISK_TABLES, SUM_NO_INDEX_USED, SUM_ERRORS, QUANTILE_95 "
    "FROM performance_schema.events_statements_summary_by_digest WHERE SCHEMA_NAME = %s AND DIGEST IS NOT NULL"
)

PICO_PER_MS = 1e9


def reset_digests(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE performance_schema.events_statements_summary_by_digest")


def global_status(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SHOW GLOBAL STATUS WHERE Variable_name IN (" + ", ".join(["%s"] * len(STATUS_KEYS)) + ")", STATUS_KEYS)
        return {name: int(value) for name, value in cur.fetchall()}


def status_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {k: after.get(k, 0) - before.get(k, 0) for k in STATUS_KEYS}


def innodb_metric(conn, name: str) -> int:
    """InnoDB counter from information_schema.INNODB_METRICS (for example lock_deadlocks)."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT FROM information_schema.INNODB_METRICS WHERE NAME = %s", (name,))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def server_time(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT DATE_FORMAT(NOW(), '%Y-%m-%dT%H:%i:%s')")
        return cur.fetchone()[0]


def enable_slow_log(conn, path: str) -> None:
    """Log every statement from new sessions (long_query_time only applies to sessions opened afterwards)."""
    with conn.cursor() as cur:
        cur.execute("SET GLOBAL slow_query_log_file = %s", (path,))
        cur.execute("SET GLOBAL long_query_time = 0")
        cur.execute("SET GLOBAL slow_query_log = 1")


def disable_slow_log(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SET GLOBAL slow_query_log = 0")
        cur.execute("SET GLOBAL long_query_time = 10")


def threads_running(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SHOW GLOBAL STATUS LIKE 'Threads_running'")
        return int(cur.fetchone()[1])


def statement_digest(conn, sql: str, params: dict[str, Any]) -> str:
    """performance_schema digest of a parameterized statement (literal values do not change the digest)."""
    with conn.cursor() as cur:
        literal = cur.mogrify(sql, params)
        cur.execute("SELECT STATEMENT_DIGEST(%s)", (literal,))
        return cur.fetchone()[0]


def digest_rows(conn, schema: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_DIGEST_SQL, (schema,))
        rows = cur.fetchall()
    out = []
    for (digest, text, count, timer, lock, examined, sent, affected, tmp_disk, no_index, errors, q95) in rows:
        out.append({
            "digest": digest,
            "digest_text": redact_text((text or "")[:400]),
            "calls": int(count),
            "total_db_time_ms": round(int(timer) / PICO_PER_MS, 3),
            "avg_latency_ms": round(int(timer) / PICO_PER_MS / max(1, int(count)), 3),
            "p95_latency_ms": round(int(q95 or 0) / PICO_PER_MS, 3),
            "total_lock_time_ms": round(int(lock) / PICO_PER_MS, 3),
            "rows_examined": int(examined),
            "rows_sent": int(sent),
            "rows_affected": int(affected),
            "tmp_disk_tables": int(tmp_disk),
            "no_index_used": int(no_index),
            "errors": int(errors),
        })
    return out


def explain_analyze(conn, sql: str, params: dict[str, Any]) -> str:
    with conn.cursor() as cur:
        cur.execute("EXPLAIN ANALYZE " + sql, params)
        return cur.fetchone()[0]


def table_stats(conn, schema: str, tables: list[str]) -> list[dict]:
    out = []
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"ANALYZE TABLE `{table}`")
            cur.fetchall()
        cur.execute(
            "SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s ORDER BY DATA_LENGTH DESC", (schema,))
        sizes = cur.fetchall()
        cur.execute(
            "SELECT TABLE_NAME, INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = %s GROUP BY TABLE_NAME, INDEX_NAME", (schema,))
        indexes: dict[str, dict[str, list[str]]] = {}
        for table, index, cols in cur.fetchall():
            indexes.setdefault(table, {})[index] = cols.split(",")
    for table, rows, data, index in sizes:
        out.append({"table": table, "rows_estimate": int(rows or 0), "data_mib": round(int(data) / 2**20, 2),
                    "index_mib": round(int(index) / 2**20, 2), "indexes": indexes.get(table, {})})
    return out
