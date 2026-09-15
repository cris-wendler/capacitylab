"""Concurrent, open-loop workload driver for the MySQL lab.

Arrivals follow a seeded Poisson schedule derived from the scenario's statement mix. Workers execute
statements on their own connections; latency is measured per call, and queueing delay is recorded
separately so saturation shows up as delay rather than being hidden.
"""

from __future__ import annotations

import itertools
import queue
import random
import threading
import time
from dataclasses import dataclass, field

import pymysql

from capacitylab.diagnostics import fixture_db as fx
from capacitylab.diagnostics.sandbox import _mysql_params
from capacitylab.scenarios.models import Scenario

ORDER_HISTORY = _mysql_params(fx.QUERIES["QF-ORDER-HISTORY"])
AUDIENCE = _mysql_params(fx.QUERIES["QF-AUDIENCE"])
CHECKOUT_INSERT = _mysql_params(fx.CHECKOUT_INSERT)
CHECKOUT_UPDATE = ("UPDATE customers SET loyalty_points = loyalty_points + %(pts)s "
                   "WHERE tenant_id = %(tenant)s AND customer_id = %(customer)s")
OTHER = "SELECT tier FROM tenants WHERE tenant_id = %(tenant)s"
BATCH_UPDATE = (
    "UPDATE customers c SET loyalty_points = (SELECT COALESCE(SUM(o.total_cents), 0) DIV 100 FROM orders o "
    "WHERE o.tenant_id = c.tenant_id AND o.customer_id = c.customer_id AND o.order_state = 'paid') "
    "WHERE c.tenant_id = %(tenant)s AND c.customer_id BETWEEN %(lo)s AND %(hi)s"
)

SUPPORTED_FINGERPRINTS = {"QF-ORDER-HISTORY", "QF-AUDIENCE", "QF-CHECKOUT-WRITE", "QF-OTHER"}

# Representative statement (and parameters) per fingerprint, used to map performance_schema digests.
DIGEST_STATEMENTS = {
    "QF-ORDER-HISTORY": [(ORDER_HISTORY, {"tenant": 1, "customer": 1})],
    "QF-AUDIENCE": [(AUDIENCE, {"tenant": 1, "since_day": fx.RECENT_SINCE_DAY})],
    "QF-CHECKOUT-WRITE": [
        (CHECKOUT_INSERT, {"order_id": 1, "tenant_id": 1, "customer_id": 1, "total": 1, "day": 1}),
        (CHECKOUT_UPDATE, {"pts": 1, "tenant": 1, "customer": 1}),
    ],
    "QF-OTHER": [(OTHER, {"tenant": 1})],
    "LAB-BATCH": [(BATCH_UPDATE, {"tenant": 1, "lo": 1, "hi": 2})],
}


@dataclass(frozen=True)
class ConnectionInfo:
    host: str
    port: int
    user: str
    password: str
    database: str

    def connect(self):
        return pymysql.connect(host=self.host, port=self.port, user=self.user, password=self.password,
                               database=self.database, autocommit=True)


@dataclass
class Call:
    fingerprint_id: str
    tenant: str
    scheduled_s: float
    started_s: float = 0.0
    latency_ms: float = 0.0
    error: str | None = None


@dataclass
class PhaseRun:
    calls: list[Call] = field(default_factory=list)
    threads_running_samples: list[int] = field(default_factory=list)
    batch_chunks: int = 0
    batch_errors: int = 0
    wall_s: float = 0.0


def build_schedule(scenario: Scenario, multiplier: float, total_qps: float, duration_s: float, seed: str) \
        -> list[tuple[float, str, str]]:
    """Poisson arrivals. Rates keep the scenario mix; the baseline mix is scaled to `total_qps`."""
    unsupported = {f.id for f in scenario.workload.fingerprints} - SUPPORTED_FINGERPRINTS
    if unsupported:
        raise ValueError(f"the lab fixture cannot execute {sorted(unsupported)}")
    unknown_tenants = {t for f in scenario.workload.fingerprints for t in f.base_qps_by_tenant} - set(fx.TENANT_IDS)
    if unknown_tenants:
        raise ValueError(f"the lab fixture has no tenants {sorted(unknown_tenants)}")
    base_total = sum(sum(f.base_qps_by_tenant.values()) for f in scenario.workload.fingerprints)
    factor = total_qps / base_total
    schedule: list[tuple[float, str, str]] = []
    for f in scenario.workload.fingerprints:
        for tenant, base in sorted(f.base_qps_by_tenant.items()):
            boost = multiplier if (f.campaign_sensitive and tenant == scenario.focal_tenant) else 1.0
            rate = base * boost * factor
            if rate <= 0:
                continue
            rng = random.Random(f"{seed}:{f.id}:{tenant}")
            t = rng.expovariate(rate)
            while t < duration_s:
                schedule.append((t, f.id, tenant))
                t += rng.expovariate(rate)
    schedule.sort()
    return schedule


def _execute(cur, fid: str, tenant: str, rng: random.Random, order_ids) -> None:
    tenant_id = fx.TENANT_IDS[tenant]
    customer = rng.randint(1, fx.CUSTOMERS_PER_TENANT[tenant_id])
    if fid == "QF-ORDER-HISTORY":
        cur.execute(ORDER_HISTORY, {"tenant": tenant_id, "customer": customer})
        cur.fetchall()
    elif fid == "QF-AUDIENCE":
        cur.execute(AUDIENCE, {"tenant": tenant_id, "since_day": fx.RECENT_SINCE_DAY})
        cur.fetchall()
    elif fid == "QF-CHECKOUT-WRITE":
        cur.execute("START TRANSACTION")
        try:
            cur.execute(CHECKOUT_INSERT, {"order_id": next(order_ids), "tenant_id": tenant_id, "customer_id": customer,
                                          "total": rng.randint(500, 40000), "day": fx.TODAY - 1})
            cur.execute(CHECKOUT_UPDATE, {"pts": rng.randint(1, 50), "tenant": tenant_id, "customer": customer})
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
    else:
        cur.execute(OTHER, {"tenant": tenant_id})
        cur.fetchall()


def run_phase(conn_info: ConnectionInfo, schedule: list[tuple[float, str, str]], duration_s: float, workers: int,
              batch: bool, seed: str, batch_hold_s: float = 0.25, lock_wait_timeout_s: int = 5) -> PhaseRun:
    result = PhaseRun()
    work: queue.Queue = queue.Queue()
    lock = threading.Lock()
    order_ids = itertools.count(int(time.time() * 1000) % 10**9 + 20_000_000)
    stop = threading.Event()
    start = time.perf_counter() + 0.2  # let workers connect first

    def worker(n: int) -> None:
        conn = conn_info.connect()
        rng = random.Random(f"{seed}:worker:{n}")
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION innodb_lock_wait_timeout = %s", (lock_wait_timeout_s,))
                while True:
                    item = work.get()
                    if item is None:
                        return
                    scheduled, fid, tenant = item
                    call = Call(fid, tenant, scheduled)
                    delay = start + scheduled - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                    t0 = time.perf_counter()
                    call.started_s = t0 - start
                    try:
                        with lock:
                            ids = order_ids
                        _execute(cur, fid, tenant, rng, ids)
                    except pymysql.MySQLError as exc:
                        call.error = type(exc).__name__ + f"({exc.args[0] if exc.args else ''})"
                    call.latency_ms = (time.perf_counter() - t0) * 1000
                    with lock:
                        result.calls.append(call)
        finally:
            conn.close()

    def batch_job() -> None:
        conn = conn_info.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION innodb_lock_wait_timeout = %s", (lock_wait_timeout_s,))
                while not stop.is_set():
                    for tenant_id, customers in fx.CUSTOMERS_PER_TENANT.items():
                        for lo in range(1, customers + 1, 200):
                            if stop.is_set():
                                return
                            try:
                                cur.execute("START TRANSACTION")
                                cur.execute(BATCH_UPDATE, {"tenant": tenant_id, "lo": lo, "hi": lo + 199})
                                time.sleep(batch_hold_s)  # holds row locks like a long recalculation transaction
                                cur.execute("COMMIT")
                                result.batch_chunks += 1
                            except pymysql.MySQLError:
                                cur.execute("ROLLBACK")
                                result.batch_errors += 1
        finally:
            conn.close()

    def sampler() -> None:
        conn = conn_info.connect()
        try:
            with conn.cursor() as cur:
                while not stop.is_set():
                    cur.execute("SHOW GLOBAL STATUS LIKE 'Threads_running'")
                    result.threads_running_samples.append(int(cur.fetchone()[1]))
                    time.sleep(0.5)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(n,), daemon=True) for n in range(workers)]
    side = [threading.Thread(target=sampler, daemon=True)] + ([threading.Thread(target=batch_job, daemon=True)] if batch else [])
    for t in threads + side:
        t.start()
    for item in schedule:
        work.put(item)
    for _ in threads:
        work.put(None)
    wall_start = time.perf_counter()
    for t in threads:
        t.join()
    remaining = start + duration_s - time.perf_counter()
    if remaining > 0:
        time.sleep(remaining)
    stop.set()
    for t in side:
        t.join(timeout=lock_wait_timeout_s + 2)
    result.wall_s = round(time.perf_counter() - wall_start, 3)
    return result
