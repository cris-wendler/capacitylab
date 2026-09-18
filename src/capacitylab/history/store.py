# SPDX-License-Identifier: AGPL-3.0-or-later
"""An append-only history of what a cluster actually did, so later collections accumulate instead of replacing.

One SQLite file (`runs/history.db` by default) holds:

    samples       one row per (cluster, node, metric, timestamp): the period average and maximum
    collections   one row per collection attempt: which window was asked for and how much came back
    clusters      one row per cluster, with the first and last time anything was collected
    nodes         the node roles seen for a cluster and the instance class each was last seen with

Writes are idempotent: collecting an overlapping window again writes nothing new, so a watch loop can run as often
as it likes. Nothing is averaged away on write; roll-ups happen on read (see envelope.py).

A missing stretch is a fact of its own, not a hole to interpolate over: `collections` records every attempt, so
"nothing arrived after 14:00" is answerable, and `gaps()` finds the stretches where samples are missing.

Identities are never stored. A cluster is keyed by an HMAC of the identifiers the caller saw, under a salt generated
in this file on first use, so the same cluster accumulates across collections while the key reveals nothing and does
not travel between machines.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clusters (
    key        TEXT PRIMARY KEY,
    label      TEXT,
    cloud      TEXT,
    engine     TEXT,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    cluster_key    TEXT NOT NULL,
    role           TEXT NOT NULL,
    instance_class TEXT,
    vcpu           INTEGER,
    memory_gib     REAL,
    last_seen      INTEGER NOT NULL,
    PRIMARY KEY (cluster_key, role)
);
CREATE TABLE IF NOT EXISTS samples (
    cluster_key TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    metric      TEXT    NOT NULL,
    ts          INTEGER NOT NULL,  -- epoch seconds, UTC, the end of the period
    period_s    INTEGER NOT NULL,
    unit        TEXT,
    avg         REAL    NOT NULL,
    max         REAL    NOT NULL,
    PRIMARY KEY (cluster_key, role, metric, ts)
);
CREATE TABLE IF NOT EXISTS collections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_key  TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    window_start INTEGER NOT NULL,
    window_end   INTEGER NOT NULL,
    collected_at INTEGER NOT NULL,
    seen         INTEGER NOT NULL,  -- datapoints the source returned
    written      INTEGER NOT NULL,  -- rows this collection added (the rest were already held)
    note         TEXT
);
CREATE INDEX IF NOT EXISTS samples_by_metric ON samples (cluster_key, metric, role, ts);
CREATE INDEX IF NOT EXISTS collections_by_cluster ON collections (cluster_key, window_end);
"""


@dataclass(frozen=True)
class Sample:
    ts: datetime
    avg: float
    max: float
    period_s: int


@dataclass(frozen=True)
class ClusterRow:
    key: str
    label: str | None
    cloud: str | None
    engine: str | None
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True)
class Gap:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def _epoch(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp())


def _when(value: int) -> datetime:
    return datetime.fromtimestamp(value, UTC)


class History:
    """Read and write the sample history. Use as a context manager, or call close()."""

    def __init__(self, path: str | Path = "runs/history.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> History:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # -- identity ------------------------------------------------------------------------------------------------

    def _salt(self) -> bytes:
        row = self.db.execute("SELECT value FROM meta WHERE key = 'key_salt'").fetchone()
        if row is None:
            salt = secrets.token_hex(32)
            self.db.execute("INSERT INTO meta (key, value) VALUES ('key_salt', ?)", (salt,))
            self.db.commit()
            return bytes.fromhex(salt)
        return bytes.fromhex(row["value"])

    def cluster_key(self, identifiers: Iterable[str]) -> str:
        """A stable key for the cluster these identifiers name. The identifiers themselves are not stored."""
        joined = "\n".join(sorted(str(i) for i in identifiers))
        digest = hmac.new(self._salt(), joined.encode(), hashlib.sha256).hexdigest()
        return f"CL-{digest[:12].upper()}"

    # -- writing -------------------------------------------------------------------------------------------------

    def note_cluster(self, key: str, *, label: str | None = None, cloud: str | None = None,
                     engine: str | None = None, when: datetime | None = None) -> None:
        stamp = _epoch(when or datetime.now(UTC))
        self.db.execute(
            """INSERT INTO clusters (key, label, cloud, engine, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (key) DO UPDATE SET last_seen = excluded.last_seen,
                   label = COALESCE(excluded.label, clusters.label),
                   cloud = COALESCE(excluded.cloud, clusters.cloud),
                   engine = COALESCE(excluded.engine, clusters.engine)""",
            (key, label, cloud, engine, stamp, stamp))
        self.db.commit()

    def note_node(self, key: str, role: str, *, instance_class: str | None = None, vcpu: int | None = None,
                  memory_gib: float | None = None, when: datetime | None = None) -> None:
        stamp = _epoch(when or datetime.now(UTC))
        self.db.execute(
            """INSERT INTO nodes (cluster_key, role, instance_class, vcpu, memory_gib, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (cluster_key, role) DO UPDATE SET last_seen = excluded.last_seen,
                   instance_class = COALESCE(excluded.instance_class, nodes.instance_class),
                   vcpu = COALESCE(excluded.vcpu, nodes.vcpu),
                   memory_gib = COALESCE(excluded.memory_gib, nodes.memory_gib)""",
            (key, role, instance_class, vcpu, memory_gib, stamp))
        self.db.commit()

    def add_samples(self, key: str, role: str, metric: str, points: list[dict], *, unit: str = "",
                    period_s: int = 300) -> tuple[int, int]:
        """Append CloudWatch-style points ({Timestamp, Average, Maximum}). Returns (seen, written).

        Points already held are left as they were: the first collection of a period wins, so re-collecting an
        overlapping window changes nothing.
        """
        rows = [(key, role, metric, _epoch(p["Timestamp"]), period_s, unit, float(p["Average"]),
                 float(p.get("Maximum", p["Average"]))) for p in points]
        if not rows:
            return 0, 0
        before = self.db.total_changes
        self.db.executemany(
            """INSERT INTO samples (cluster_key, role, metric, ts, period_s, unit, avg, max)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""", rows)
        self.db.commit()
        return len(rows), self.db.total_changes - before

    def record_collection(self, key: str, source: str, window_start: datetime, window_end: datetime, *,
                          seen: int, written: int, note: str | None = None,
                          when: datetime | None = None) -> None:
        """Record that a collection ran, so a stretch with no samples can be told from one never collected."""
        self.db.execute(
            """INSERT INTO collections (cluster_key, source, window_start, window_end, collected_at, seen, written,
                                        note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, source, _epoch(window_start), _epoch(window_end), _epoch(when or datetime.now(UTC)), seen,
             written, note))
        self.db.commit()

    # -- reading -------------------------------------------------------------------------------------------------

    def clusters(self) -> list[ClusterRow]:
        return [ClusterRow(r["key"], r["label"], r["cloud"], r["engine"], _when(r["first_seen"]),
                           _when(r["last_seen"]))
                for r in self.db.execute("SELECT * FROM clusters ORDER BY last_seen DESC")]

    def nodes(self, key: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT role, instance_class, vcpu, memory_gib, last_seen FROM nodes WHERE cluster_key = ? "
            "ORDER BY role", (key,))]

    def metrics(self, key: str) -> list[tuple[str, str, int]]:
        """(metric, role, sample count) held for a cluster."""
        return [(r["metric"], r["role"], r["n"]) for r in self.db.execute(
            "SELECT metric, role, COUNT(*) AS n FROM samples WHERE cluster_key = ? GROUP BY metric, role "
            "ORDER BY metric, role", (key,))]

    def samples(self, key: str, metric: str, role: str = "writer", *, since: datetime | None = None,
                until: datetime | None = None) -> list[Sample]:
        sql = "SELECT ts, avg, max, period_s FROM samples WHERE cluster_key = ? AND metric = ? AND role = ?"
        params: list = [key, metric, role]
        if since is not None:
            sql, _ = sql + " AND ts >= ?", params.append(_epoch(since))
        if until is not None:
            sql, _ = sql + " AND ts <= ?", params.append(_epoch(until))
        return [Sample(_when(r["ts"]), r["avg"], r["max"], r["period_s"])
                for r in self.db.execute(sql + " ORDER BY ts", params)]

    def unit(self, key: str, metric: str, role: str = "writer") -> str:
        row = self.db.execute("SELECT unit FROM samples WHERE cluster_key = ? AND metric = ? AND role = ? "
                              "ORDER BY ts DESC LIMIT 1", (key, metric, role)).fetchone()
        return (row["unit"] if row else "") or ""

    def last_sample_at(self, key: str, metric: str | None = None, role: str | None = None) -> datetime | None:
        sql, params = "SELECT MAX(ts) AS ts FROM samples WHERE cluster_key = ?", [key]
        if metric:
            sql, _ = sql + " AND metric = ?", params.append(metric)
        if role:
            sql, _ = sql + " AND role = ?", params.append(role)
        row = self.db.execute(sql, params).fetchone()
        return _when(row["ts"]) if row and row["ts"] is not None else None

    def stale_for(self, key: str, *, now: datetime | None = None, metric: str | None = None) -> timedelta | None:
        """How long since the newest sample. None when nothing has been collected yet."""
        last = self.last_sample_at(key, metric)
        return None if last is None else (now or datetime.now(UTC)).astimezone(UTC) - last

    def last_collection(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM collections WHERE cluster_key = ? ORDER BY collected_at DESC, id DESC "
                              "LIMIT 1", (key,)).fetchone()
        return dict(row) if row else None

    def gaps(self, key: str, metric: str, role: str = "writer", *, since: datetime | None = None,
             until: datetime | None = None, tolerance_periods: float = 2.0) -> list[Gap]:
        """Stretches with no samples: consecutive timestamps further apart than tolerance x the period."""
        points = self.samples(key, metric, role, since=since, until=until)
        out: list[Gap] = []
        for earlier, later in zip(points, points[1:], strict=False):
            allowed = earlier.period_s * tolerance_periods
            if (later.ts - earlier.ts).total_seconds() > allowed:
                out.append(Gap(earlier.ts, later.ts))
        return out

    def coverage(self, key: str, metric: str, role: str = "writer", *, since: datetime, until: datetime) -> float:
        """Share of the window that has samples, from each sample's own period. 1.0 is a full window."""
        points = self.samples(key, metric, role, since=since, until=until)
        span = (until - since).total_seconds()
        if span <= 0:
            return 0.0
        held = sum(p.period_s for p in points)
        return round(min(held / span, 1.0), 4)


@contextmanager
def open_history(path: str | Path = "runs/history.db") -> Iterator[History]:
    store = History(path)
    try:
        yield store
    finally:
        store.close()
