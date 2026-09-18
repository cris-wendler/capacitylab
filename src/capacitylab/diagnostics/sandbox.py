# SPDX-License-Identifier: AGPL-3.0-or-later
"""Isolated experiment databases.

Two backends share one interface:
- SQLiteSandbox: in-process, in-memory. Work is measured in SQLite VM instructions (deterministic).
- MySQLSandbox: a disposable local MySQL 8.0 container. Work is measured with Handler_read_* counters.

Neither backend is evidence of production (e.g. Aurora) performance. Results carry the backend name.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
SANDBOX_DB_PREFIX = "capacitylab_sandbox"


class UnsafeSandboxTarget(RuntimeError):
    """Raised when a sandbox is pointed at anything other than a local, disposable database."""


class WorkMeasure(BaseModel):
    rows_returned: int
    work_units: int
    unit: str


class PlanSummary(BaseModel):
    backend: str
    full_scans: list[str] = Field(default_factory=list)
    indexes_used: list[str] = Field(default_factory=list)
    uses_temporary_or_sort: bool = False
    raw: list[str] = Field(default_factory=list)


class Sandbox(ABC):
    backend: str

    @abstractmethod
    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None: ...

    @abstractmethod
    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None: ...

    @abstractmethod
    def fetchall(self, sql: str, params: dict[str, Any] | None = None) -> list[tuple]: ...

    @abstractmethod
    def measure(self, sql: str, params: dict[str, Any] | None = None) -> WorkMeasure: ...

    @abstractmethod
    def plan(self, sql: str, params: dict[str, Any] | None = None) -> PlanSummary: ...

    @abstractmethod
    def create_index(self, name: str, table: str, columns: list[str]) -> int:
        """Create an index and return its size in bytes (measured)."""

    @abstractmethod
    def drop_index(self, name: str, table: str) -> None: ...

    @abstractmethod
    def write_overhead(self, sql: str, rows: list[dict[str, Any]]) -> WorkMeasure:
        """Execute writes inside a transaction that is rolled back; return the measured work."""

    @abstractmethod
    def analyze(self) -> None: ...

    @abstractmethod
    def engine_version(self) -> str: ...

    def close(self) -> None:  # noqa: B027 - optional hook
        pass


class SQLiteSandbox(Sandbox):
    backend = "sqlite"
    _STEP = 1  # count every VM instruction so work units are exactly repeatable

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self._steps = 0

    def _count(self) -> int:
        self._steps += 1
        return 0

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.conn.execute(sql, params or {})

    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None:
        self.conn.execute("BEGIN")
        self.conn.executemany(sql, rows)
        self.conn.execute("COMMIT")

    def fetchall(self, sql: str, params: dict[str, Any] | None = None) -> list[tuple]:
        return self.conn.execute(sql, params or {}).fetchall()

    def measure(self, sql: str, params: dict[str, Any] | None = None) -> WorkMeasure:
        # Uncounted warm-up: the first execution after a schema change re-prepares the statement,
        # which would add a few VM steps and make identical measurements differ.
        self.conn.execute(sql, params or {}).fetchall()
        self._steps = 0
        self.conn.set_progress_handler(self._count, self._STEP)
        try:
            rows = self.conn.execute(sql, params or {}).fetchall()
        finally:
            self.conn.set_progress_handler(None, 0)
        return WorkMeasure(rows_returned=len(rows), work_units=self._steps * self._STEP, unit="sqlite_vm_instructions")

    def plan(self, sql: str, params: dict[str, Any] | None = None) -> PlanSummary:
        details = [row[3] for row in self.conn.execute("EXPLAIN QUERY PLAN " + sql, params or {}).fetchall()]
        summary = PlanSummary(backend=self.backend, raw=details)
        for d in details:
            if d.startswith("SCAN ") and "COVERING INDEX" not in d and "USING INDEX" not in d:
                summary.full_scans.append(d.split()[1])
            for m in re.finditer(r"USING (?:COVERING )?INDEX (\w+)", d):
                summary.indexes_used.append(m.group(1))
            if "PRIMARY KEY" in d:
                summary.indexes_used.append("PRIMARY")
            if "TEMP B-TREE" in d:
                summary.uses_temporary_or_sort = True
        return summary

    def _bytes(self) -> int:
        pages = self.conn.execute("PRAGMA page_count").fetchone()[0]
        size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        return pages * size

    def create_index(self, name: str, table: str, columns: list[str]) -> int:
        before = self._bytes()
        self.conn.execute(f"CREATE INDEX {name} ON {table} ({', '.join(columns)})")
        size = self._bytes() - before
        # SQLite has no automatic statistics for a new index; InnoDB does. Refresh them so the planner
        # is not judged on missing statistics.
        self.conn.execute("ANALYZE")
        return size

    def drop_index(self, name: str, table: str) -> None:
        self.conn.execute(f"DROP INDEX IF EXISTS {name}")
        # Release freed pages so the next index size measurement is not masked by page reuse.
        self.conn.execute("VACUUM")

    def write_overhead(self, sql: str, rows: list[dict[str, Any]]) -> WorkMeasure:
        self.conn.execute("BEGIN")  # uncounted warm-up, rolled back (see measure)
        self.conn.executemany(sql, rows)
        self.conn.execute("ROLLBACK")
        self._steps = 0
        self.conn.execute("BEGIN")
        self.conn.set_progress_handler(self._count, self._STEP)
        try:
            self.conn.executemany(sql, rows)
        finally:
            self.conn.set_progress_handler(None, 0)
            self.conn.execute("ROLLBACK")
        return WorkMeasure(rows_returned=0, work_units=self._steps * self._STEP, unit="sqlite_vm_instructions")

    def analyze(self) -> None:
        self.conn.execute("ANALYZE")

    def engine_version(self) -> str:
        return f"SQLite {sqlite3.sqlite_version}"

    def close(self) -> None:
        self.conn.close()


def _mysql_params(sql: str) -> str:
    return re.sub(r"(?<!:):(\w+)", r"%(\1)s", sql)


class MySQLSandbox(Sandbox):
    backend = "mysql"

    def __init__(self, host: str, port: int, user: str, password: str, database: str):
        if host not in _LOCAL_HOSTS:
            raise UnsafeSandboxTarget(f"refusing non-local MySQL host {host!r}; sandboxes must be local")
        if not database.startswith(SANDBOX_DB_PREFIX):
            raise UnsafeSandboxTarget(f"refusing database {database!r}; name must start with {SANDBOX_DB_PREFIX}")
        import pymysql  # optional dependency

        self.database = database
        self.conn = pymysql.connect(host=host, port=port, user=user, password=password, autocommit=True)
        with self.conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{database}`")
            cur.execute(f"CREATE DATABASE `{database}`")
            cur.execute(f"USE `{database}`")

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(_mysql_params(sql), params or None)

    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None:
        with self.conn.cursor() as cur:
            cur.execute("START TRANSACTION")
            cur.executemany(_mysql_params(sql), rows)
            cur.execute("COMMIT")

    def fetchall(self, sql: str, params: dict[str, Any] | None = None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(_mysql_params(sql), params or None)
            return [tuple(r) for r in cur.fetchall()]

    def _handler_reads(self, cur) -> int:
        cur.execute("SHOW SESSION STATUS LIKE 'Handler_read%'")
        return sum(int(v) for _, v in cur.fetchall())

    def measure(self, sql: str, params: dict[str, Any] | None = None) -> WorkMeasure:
        with self.conn.cursor() as cur:
            cur.execute("FLUSH STATUS")
            cur.execute(_mysql_params(sql), params or None)
            rows = cur.fetchall()
            work = self._handler_reads(cur)
        return WorkMeasure(rows_returned=len(rows), work_units=work, unit="mysql_handler_reads")

    def plan(self, sql: str, params: dict[str, Any] | None = None) -> PlanSummary:
        with self.conn.cursor() as cur:
            cur.execute("EXPLAIN FORMAT=JSON " + _mysql_params(sql), params or None)
            doc = json.loads(cur.fetchone()[0])
        summary = PlanSummary(backend=self.backend, raw=[json.dumps(doc, sort_keys=True)])

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if "table_name" in node and "access_type" in node:
                    if node["access_type"] in ("ALL", "index") and not node.get("using_index"):
                        summary.full_scans.append(node["table_name"])
                    if node.get("key"):
                        summary.indexes_used.append(node["key"])
                if node.get("using_filesort") or node.get("using_temporary_table"):
                    summary.uses_temporary_or_sort = True
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(doc)
        return summary

    def create_index(self, name: str, table: str, columns: list[str]) -> int:
        with self.conn.cursor() as cur:
            cur.execute(f"CREATE INDEX {name} ON {table} ({', '.join(columns)})")
            cur.execute(f"ANALYZE TABLE {table}")
            cur.fetchall()
            cur.execute(
                "SELECT stat_value * @@innodb_page_size FROM mysql.innodb_index_stats "
                "WHERE database_name=%s AND table_name=%s AND index_name=%s AND stat_name='size'",
                (self.database, table, name),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def drop_index(self, name: str, table: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(f"DROP INDEX {name} ON {table}")

    def write_overhead(self, sql: str, rows: list[dict[str, Any]], repeats: int = 5) -> WorkMeasure:
        """Median elapsed time of rolled-back batches. Timing-based, so noisy; excluded from replay digests."""
        samples = []
        with self.conn.cursor() as cur:
            for _ in range(repeats):
                cur.execute("START TRANSACTION")
                started = time.perf_counter()
                cur.executemany(_mysql_params(sql), rows)
                samples.append(int((time.perf_counter() - started) * 1_000_000))
                cur.execute("ROLLBACK")
        samples.sort()
        return WorkMeasure(rows_returned=0, work_units=samples[len(samples) // 2],
                           unit="mysql_median_elapsed_microseconds (noisy)")

    def analyze(self) -> None:
        with self.conn.cursor() as cur:
            for table in ("customers", "orders", "suppressions", "audit_events", "tenants"):
                cur.execute(f"ANALYZE TABLE {table}")
                cur.fetchall()

    def engine_version(self) -> str:
        with self.conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            return f"MySQL {cur.fetchone()[0]}"

    def close(self) -> None:
        self.conn.close()


def _walk_pg_plan(node: Any, visit) -> None:
    if isinstance(node, dict):
        if "Node Type" in node:
            visit(node)
        for value in node.values():
            _walk_pg_plan(value, visit)
    elif isinstance(node, list):
        for value in node:
            _walk_pg_plan(value, visit)


class PostgresSandbox(Sandbox):
    """A disposable database in a local PostgreSQL container. Work is measured in shared buffer blocks touched."""

    backend = "postgres"

    def __init__(self, host: str, port: int, user: str, password: str, database: str):
        if host not in _LOCAL_HOSTS:
            raise UnsafeSandboxTarget(f"refusing non-local PostgreSQL host {host!r}; sandboxes must be local")
        if not database.startswith(SANDBOX_DB_PREFIX):
            raise UnsafeSandboxTarget(f"refusing database {database!r}; name must start with {SANDBOX_DB_PREFIX}")
        import psycopg  # optional dependency

        self.database = database
        admin = psycopg.connect(host=host, port=port, user=user, password=password, dbname="postgres", autocommit=True)
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            admin.execute(f'CREATE DATABASE "{database}"')
        finally:
            admin.close()
        self.conn = psycopg.connect(host=host, port=port, user=user, password=password, dbname=database,
                                    autocommit=True, cursor_factory=psycopg.ClientCursor)
        self.conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(_mysql_params(sql), params or None)

    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None:
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany(_mysql_params(sql), rows)

    def fetchall(self, sql: str, params: dict[str, Any] | None = None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(_mysql_params(sql), params or None)
            return [tuple(r) for r in cur.fetchall()]

    def _explain(self, options: str, sql: str, params: dict[str, Any] | None) -> dict:
        with self.conn.cursor() as cur:
            cur.execute(f"EXPLAIN ({options}, FORMAT JSON) " + _mysql_params(sql), params or None)
            doc = cur.fetchone()[0]
        doc = json.loads(doc) if isinstance(doc, str) else doc
        return doc[0]

    def measure(self, sql: str, params: dict[str, Any] | None = None) -> WorkMeasure:
        root = self._explain("ANALYZE, BUFFERS", sql, params)["Plan"]
        rows = int(root.get("Actual Rows", 0) * max(1, root.get("Actual Loops", 1)))
        blocks = int(root.get("Shared Hit Blocks", 0) + root.get("Shared Read Blocks", 0))
        return WorkMeasure(rows_returned=rows, work_units=blocks, unit="postgres_shared_blocks")

    def plan(self, sql: str, params: dict[str, Any] | None = None) -> PlanSummary:
        doc = self._explain("COSTS", sql, params)
        summary = PlanSummary(backend=self.backend, raw=[json.dumps(doc, sort_keys=True)])

        def visit(node: dict) -> None:
            if node["Node Type"] == "Seq Scan" and node.get("Relation Name"):
                summary.full_scans.append(node["Relation Name"])
            if node.get("Index Name"):
                summary.indexes_used.append(node["Index Name"])
            if node["Node Type"] in ("Sort", "Incremental Sort", "Hash", "Materialize"):
                summary.uses_temporary_or_sort = True

        _walk_pg_plan(doc, visit)
        return summary

    def create_index(self, name: str, table: str, columns: list[str]) -> int:
        with self.conn.cursor() as cur:
            cur.execute(f"CREATE INDEX {name} ON {table} ({', '.join(columns)})")
            cur.execute(f"ANALYZE {table}")
            cur.execute("SELECT pg_relation_size(%s::regclass)", (name,))
            return int(cur.fetchone()[0])

    def drop_index(self, name: str, table: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS {name}")

    def write_overhead(self, sql: str, rows: list[dict[str, Any]], repeats: int = 5) -> WorkMeasure:
        """Median elapsed time of rolled-back batches. Timing-based, so noisy; excluded from replay digests."""
        samples = []
        with self.conn.cursor() as cur:
            for _ in range(repeats):
                cur.execute("START TRANSACTION")
                started = time.perf_counter()
                cur.executemany(_mysql_params(sql), rows)
                samples.append(int((time.perf_counter() - started) * 1_000_000))
                cur.execute("ROLLBACK")
        samples.sort()
        return WorkMeasure(rows_returned=0, work_units=samples[len(samples) // 2],
                           unit="postgres_median_elapsed_microseconds (noisy)")

    def analyze(self) -> None:
        with self.conn.cursor() as cur:
            for table in ("customers", "orders", "suppressions", "audit_events", "tenants"):
                cur.execute(f"ANALYZE {table}")

    def engine_version(self) -> str:
        with self.conn.cursor() as cur:
            cur.execute("SHOW server_version")
            return f"PostgreSQL {cur.fetchone()[0].split()[0]}"

    def close(self) -> None:
        self.conn.close()
