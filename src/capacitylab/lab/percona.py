"""Percona Toolkit against the local MySQL lab, run from the official container image.

Each tool runs in a throwaway container that joins the lab container's network namespace, so it can only reach
the lab server at 127.0.0.1 inside that namespace. Output is parsed into plain dictionaries for evidence items.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from capacitylab.diagnostics.sandbox import UnsafeSandboxTarget
from capacitylab.evidence.normalize import fingerprint_sql, redact_text
from capacitylab.settings import MySQLSettings

DEFAULT_IMAGE = os.environ.get("CAPACITYLAB_PERCONA_IMAGE", "percona/percona-toolkit:latest")
DEFAULT_CONTAINER = os.environ.get("CAPACITYLAB_LAB_CONTAINER", "capacitylab-sandbox-mysql")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class PerconaUnavailable(RuntimeError):
    """Docker, the toolkit image, or the lab container is not available, or a tool failed."""


class PerconaToolkit:
    def __init__(self, mysql: MySQLSettings, image: str = DEFAULT_IMAGE, container: str = DEFAULT_CONTAINER,
                 runner=subprocess.run):
        if mysql.host not in LOCAL_HOSTS:
            raise UnsafeSandboxTarget(f"refusing non-local MySQL host {mysql.host!r} for Percona Toolkit")
        if not container.startswith("capacitylab-"):
            raise UnsafeSandboxTarget(f"refusing container {container!r}; only capacitylab lab containers are allowed")
        self.mysql = mysql
        self.image = image
        self.container = container
        self.runner = runner

    def _dsn(self) -> str:
        # Inside the lab container's network namespace MySQL listens on its internal port.
        return f"h=127.0.0.1,P=3306,u={self.mysql.user},p={self.mysql.password}"

    def _exec(self, cmd: list[str], what: str, stdin: str | None = None, timeout: int = 600) -> str:
        try:
            proc = self.runner(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PerconaUnavailable(f"{what} could not run: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().replace(self.mysql.password, "***")[:300]
            raise PerconaUnavailable(f"{what} exited with {proc.returncode}: {detail}")
        return proc.stdout

    def run(self, tool: str, args: list[str], stdin: str | None = None, timeout: int = 600) -> str:
        cmd = ["docker", "run", "--rm", *(["-i"] if stdin is not None else []), f"--network=container:{self.container}",
               self.image, tool, *args]
        return self._exec(cmd, tool, stdin, timeout)

    def version(self) -> str:
        return self.run("pt-query-digest", ["--version"]).strip()

    def duplicate_keys(self, database: str) -> str:
        return self.run("pt-duplicate-key-checker", [self._dsn(), "--databases", database])

    def variable_advisor(self) -> str:
        return self.run("pt-variable-advisor", [self._dsn()])

    def mysql_summary(self) -> str:
        return self.run("pt-mysql-summary", ["--host", "127.0.0.1", "--port", "3306", "--user", self.mysql.user,
                                             "--password", self.mysql.password])

    def deadlocks(self, run_time: int = 1) -> str:
        return self.run("pt-deadlock-logger", [self._dsn(), "--run-time", str(run_time), "--tab"])

    def query_digest(self, slow_log_text: str) -> dict:
        """Digest slow log text passed on stdin (no host files or mounts involved)."""
        # The default --limit 95%:20 drops rare statements, which are often the ones a scenario is about.
        out = self.run("pt-query-digest", ["--output", "json", "--limit", "100%:200", "-"], stdin=slow_log_text)
        start = out.find("{")  # the tool prints "Reading from STDIN ..." before the JSON
        if start < 0:
            raise PerconaUnavailable("pt-query-digest produced no JSON output")
        return json.loads(out[start:])

    def _lab_path(self, path: str) -> str:
        if not re.fullmatch(r"/tmp/capacitylab-[\w-]+\.log", path):
            raise UnsafeSandboxTarget(f"refusing lab file path {path!r}")
        return path

    def read_lab_file(self, path: str) -> str:
        return self._exec(["docker", "exec", self.container, "cat", self._lab_path(path)], f"reading {path}")

    def remove_lab_file(self, path: str) -> None:
        self._exec(["docker", "exec", self.container, "rm", "-f", self._lab_path(path)], f"removing {path}")


# --- statement mapping -----------------------------------------------------------------------------


def lab_fingerprint_map() -> dict[str, str]:
    """Fingerprint of each lab statement (with literals) -> scenario statement id."""
    from capacitylab.lab.workload import DIGEST_STATEMENTS

    out = {}
    for fid, statements in DIGEST_STATEMENTS.items():
        for sql, _params in statements:
            out[fingerprint_sql(re.sub(r"%\((\w+)\)s", "1", sql))] = fid
    return out


# --- parsers ---------------------------------------------------------------------------------------


def _float(metrics: dict, name: str, stat: str) -> float:
    try:
        return float(metrics.get(name, {}).get(stat, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def summarize_query_digest(doc: dict, fingerprint_map: dict[str, str] | None = None) -> dict:
    classes = []
    for c in doc.get("classes", []):
        m = c.get("metrics", {})
        example = (c.get("example") or {}).get("query", "")
        mapped = (fingerprint_map or {}).get(fingerprint_sql(example)) if example else None
        tables = []
        for t in c.get("tables", []):
            match = re.search(r"LIKE '([^']+)'", t.get("status", ""))
            if match:
                tables.append(match.group(1))
        classes.append({
            "fingerprint_id": mapped or f"PT-{(c.get('checksum') or 'UNKNOWN')[:12]}",
            "checksum": c.get("checksum"),
            "fingerprint": redact_text((c.get("fingerprint") or "")[:400]),
            "calls": int(c.get("query_count", 0)),
            "total_query_time_s": round(_float(m, "Query_time", "sum"), 6),
            "avg_latency_ms": round(_float(m, "Query_time", "avg") * 1000, 3),
            "p95_latency_ms": round(_float(m, "Query_time", "pct_95") * 1000, 3),
            "avg_rows_examined": round(_float(m, "Rows_examined", "avg"), 1),
            "avg_rows_sent": round(_float(m, "Rows_sent", "avg"), 1),
            "total_lock_time_s": round(_float(m, "Lock_time", "sum"), 6),
            "tables": tables,
        })
    total = sum(c["total_query_time_s"] for c in classes) or 1.0
    for c in classes:
        c["share_of_query_time_pct"] = round(100 * c["total_query_time_s"] / total, 1)
    classes.sort(key=lambda c: c["total_query_time_s"], reverse=True)
    glob = doc.get("global", {})
    return {"source": "pt-query-digest", "statements": glob.get("query_count"), "unique_statements": glob.get("unique_query_count"),
            "classes": classes}


def parse_duplicate_keys(text: str) -> dict:
    findings: list[dict] = []
    table = None
    for line in text.splitlines():
        header = re.match(r"^# (\S+\.\S+)\s*$", line)
        if header:
            table = header.group(1)
            continue
        relation = re.match(r"^# (\S+) is (?:a )?(left-prefix|duplicate) of (\S+)", line)
        if relation:
            findings.append({"table": table, "redundant_index": relation.group(1), "relation": relation.group(2),
                             "covered_by": relation.group(3), "drop_statement": None})
            continue
        if line.startswith("ALTER TABLE") and findings and findings[-1]["drop_statement"] is None:
            findings[-1]["drop_statement"] = line.strip()
    summary = {k: int(v) for k, v in re.findall(r"^# (Size Duplicate Indexes|Total Duplicate Indexes|Total Indexes)\s+(\d+)",
                                                 text, re.MULTILINE)}
    return {"findings": findings, "total_indexes": summary.get("Total Indexes"),
            "duplicate_indexes": summary.get("Total Duplicate Indexes", 0),
            "duplicate_index_bytes": summary.get("Size Duplicate Indexes", 0)}


def parse_variable_advisor(text: str) -> list[dict]:
    return [{"level": level, "variable": name, "message": message.strip()}
            for level, name, message in re.findall(r"^# (WARN|NOTE|CRIT) ([^:\s]+): (.+)$", text, re.MULTILINE)]


def parse_deadlocks(tsv: str, since: str | None = None, fingerprint_map: dict[str, str] | None = None) -> dict:
    lines = [line for line in tsv.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("server\t"):
        return {"deadlocks": []}
    header = lines[0].split("\t")
    events: dict[str, list[dict]] = {}
    for line in lines[1:]:
        values = line.split("\t", len(header) - 1)
        row = dict(zip(header, values, strict=False))
        ts = row.get("ts", "")
        if since and ts < since:
            continue
        query = row.get("query", "")
        events.setdefault(ts, []).append({
            "thread": row.get("thread"),
            "database": row.get("db"),
            "table": row.get("tbl"),
            "index": row.get("idx"),
            "lock_type": row.get("lock_type"),
            "lock_mode": row.get("lock_mode"),
            "waiting": row.get("wait_hold") == "w",
            "victim": row.get("victim") == "1",
            "statement_id": (fingerprint_map or {}).get(fingerprint_sql(query)),
            "query": redact_text(query[:400]),
        })
    return {"deadlocks": [{"ts": ts, "transactions": txs} for ts, txs in sorted(events.items())]}


SUMMARY_FIELDS = {"version", "built_on", "databases", "replication"}  # hostnames, paths, and clocks are left out


def parse_mysql_summary_header(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if line.startswith("# Processlist"):
            break
        match = re.match(r"^\s*([A-Za-z][A-Za-z ]+?)\s+\|\s+(.+)$", line)
        if match:
            key = match.group(1).strip().lower().replace(" ", "_")
            if key in SUMMARY_FIELDS:
                out[key] = match.group(2).strip()
    return out
