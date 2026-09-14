"""Deterministic synthetic retail dataset ("retail_v1") with the edge cases rewrites must survive.

Edge cases deliberately present:
- customer_id values repeat across tenants (tenant-boundary bugs change results);
- customers with many recent orders (IN -> JOIN rewrites produce duplicates);
- NULL customer_id in suppressions for tenant 3 (NOT IN -> NOT EXISTS changes results);
- skew: tenant 1 owns ~40% of customers and orders.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from capacitylab.diagnostics.sandbox import Sandbox

TENANT_KEYS = {1: "alder", 2: "birch", 3: "cedar", 4: "dune", 5: "elm"}
TENANT_IDS = {v: k for k, v in TENANT_KEYS.items()}
CUSTOMERS_PER_TENANT = {1: 1600, 2: 900, 3: 700, 4: 500, 5: 300}
RECENT_SINCE_DAY = 330  # "ordered in the last 35 days"
TODAY = 365

DDL = [
    "CREATE TABLE tenants (tenant_id INTEGER PRIMARY KEY, display_label VARCHAR(40) NOT NULL, tier VARCHAR(12) NOT NULL)",
    "CREATE TABLE customers (tenant_id INTEGER NOT NULL, customer_id INTEGER NOT NULL, marketing_opt_in INTEGER NOT NULL, "
    "loyalty_points INTEGER NOT NULL, region_code VARCHAR(8), PRIMARY KEY (tenant_id, customer_id))",
    "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, customer_id INTEGER NOT NULL, "
    "order_state VARCHAR(12) NOT NULL, total_cents INTEGER NOT NULL, created_day INTEGER NOT NULL)",
    "CREATE INDEX idx_orders_tenant_customer ON orders (tenant_id, customer_id)",
    "CREATE TABLE suppressions (suppression_id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, customer_id INTEGER, "
    "reason_code VARCHAR(16) NOT NULL)",
    "CREATE INDEX idx_suppressions_tenant ON suppressions (tenant_id)",
    "CREATE TABLE audit_events (event_id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, action_code VARCHAR(16) NOT NULL, "
    "payload_bytes INTEGER NOT NULL, occurred_day INTEGER NOT NULL)",
]

SCHEMA_IDENTIFIERS = {
    "tables": ["tenants", "customers", "orders", "suppressions", "audit_events"],
    "columns": [
        "tenant_id", "display_label", "tier", "customer_id", "marketing_opt_in", "loyalty_points", "region_code",
        "order_id", "order_state", "total_cents", "created_day", "suppression_id", "reason_code", "event_id",
        "action_code", "payload_bytes", "occurred_day",
    ],
}


@dataclass(frozen=True)
class FixtureStats:
    customers: int
    orders: int
    suppressions: int
    audit_events: int
    seed: int


def build(sandbox: Sandbox, seed: int = 7, scale: float = 1.0) -> FixtureStats:
    rng = random.Random(f"retail_v1:{seed}")
    for stmt in DDL:
        sandbox.execute(stmt)
    sandbox.executemany(
        "INSERT INTO tenants (tenant_id, display_label, tier) VALUES (:tenant_id, :label, :tier)",
        [{"tenant_id": t, "label": f"Tenant {k.title()} (synthetic)", "tier": "standard"} for t, k in TENANT_KEYS.items()],
    )
    customers, orders, suppressions = [], [], []
    order_id = 1
    suppression_id = 1
    for tenant, count in CUSTOMERS_PER_TENANT.items():
        n = max(20, int(count * scale))
        for customer in range(1, n + 1):
            customers.append(
                {
                    "tenant_id": tenant,
                    "customer_id": customer,
                    "opt": 1 if rng.random() < 0.7 else 0,
                    "pts": rng.randint(0, 5000),
                    "region": rng.choice(["north", "south", "east", "west"]),
                }
            )
            heavy = rng.random() < 0.08
            for _ in range(rng.randint(60, 200) if heavy else rng.randint(5, 40)):
                orders.append(
                    {
                        "order_id": order_id,
                        "tenant_id": tenant,
                        "customer_id": customer,
                        "state": rng.choices(["paid", "refunded", "pending"], [0.86, 0.06, 0.08])[0],
                        "total": rng.randint(500, 40000),
                        "day": rng.randint(0, TODAY - 1),
                    }
                )
                order_id += 1
            if rng.random() < 0.05:
                for _ in range(2 if rng.random() < 0.2 else 1):  # occasional duplicate suppression rows
                    suppressions.append({"sid": suppression_id, "tenant_id": tenant, "customer_id": customer, "reason": "unsubscribe"})
                    suppression_id += 1
        if tenant == 3:
            for _ in range(3):  # email-only suppressions with no customer_id
                suppressions.append({"sid": suppression_id, "tenant_id": tenant, "customer_id": None, "reason": "email_only"})
                suppression_id += 1

    sandbox.executemany(
        "INSERT INTO customers (tenant_id, customer_id, marketing_opt_in, loyalty_points, region_code) "
        "VALUES (:tenant_id, :customer_id, :opt, :pts, :region)",
        customers,
    )
    sandbox.executemany(
        "INSERT INTO orders (order_id, tenant_id, customer_id, order_state, total_cents, created_day) "
        "VALUES (:order_id, :tenant_id, :customer_id, :state, :total, :day)",
        orders,
    )
    sandbox.executemany(
        "INSERT INTO suppressions (suppression_id, tenant_id, customer_id, reason_code) VALUES (:sid, :tenant_id, :customer_id, :reason)",
        suppressions,
    )
    audit = [
        {"eid": i, "tenant_id": rng.choice(list(TENANT_KEYS)), "action": rng.choice(["login", "view", "export"]),
         "bytes": rng.randint(80, 4000), "day": rng.randint(0, TODAY - 1)}
        for i in range(1, int(40000 * scale) + 1)
    ]
    sandbox.executemany(
        "INSERT INTO audit_events (event_id, tenant_id, action_code, payload_bytes, occurred_day) VALUES (:eid, :tenant_id, :action, :bytes, :day)",
        audit,
    )
    sandbox.analyze()
    return FixtureStats(len(customers), len(orders), len(suppressions), len(audit), seed)


# --- Statements under study -------------------------------------------------------------------

QUERIES = {
    "QF-AUDIENCE": (
        "SELECT c.customer_id FROM customers c "
        "WHERE c.tenant_id = :tenant AND c.marketing_opt_in = 1 "
        "AND c.customer_id IN (SELECT o.customer_id FROM orders o WHERE o.tenant_id = :tenant AND o.created_day >= :since_day) "
        "AND c.customer_id NOT IN (SELECT s.customer_id FROM suppressions s WHERE s.tenant_id = :tenant)"
    ),
    "QF-ORDER-HISTORY": (
        "SELECT order_id, order_state, total_cents, created_day FROM orders "
        "WHERE tenant_id = :tenant AND customer_id = :customer ORDER BY created_day DESC, order_id DESC LIMIT 20"
    ),
}

CHECKOUT_INSERT = (
    "INSERT INTO orders (order_id, tenant_id, customer_id, order_state, total_cents, created_day) "
    "VALUES (:order_id, :tenant_id, :customer_id, 'paid', :total, :day)"
)

INDEX_CANDIDATES = {
    "IDX-TENANT-DAY-CUSTOMER": {"name": "idx_orders_tenant_day_customer", "table": "orders", "columns": ["tenant_id", "created_day", "customer_id"]},
    "IDX-TENANT-CUSTOMER-DAY": {"name": "idx_orders_tenant_customer_day", "table": "orders", "columns": ["tenant_id", "customer_id", "created_day"]},
}

EXISTING_INDEXES = {"idx_orders_tenant_customer": {"table": "orders", "columns": ["tenant_id", "customer_id"]}}

REWRITES = {
    "RW-EXISTS": {
        "original": "QF-AUDIENCE",
        "description": "Replace IN (subquery) with a correlated EXISTS that keeps the tenant predicate.",
        "sql": (
            "SELECT c.customer_id FROM customers c "
            "WHERE c.tenant_id = :tenant AND c.marketing_opt_in = 1 "
            "AND EXISTS (SELECT 1 FROM orders o WHERE o.tenant_id = c.tenant_id AND o.customer_id = c.customer_id AND o.created_day >= :since_day) "
            "AND c.customer_id NOT IN (SELECT s.customer_id FROM suppressions s WHERE s.tenant_id = :tenant)"
        ),
    },
    "RW-JOIN": {
        "original": "QF-AUDIENCE",
        "description": "Replace IN (subquery) with an inner JOIN on orders.",
        "sql": (
            "SELECT c.customer_id FROM customers c JOIN orders o ON o.tenant_id = c.tenant_id AND o.customer_id = c.customer_id "
            "WHERE c.tenant_id = :tenant AND c.marketing_opt_in = 1 AND o.created_day >= :since_day "
            "AND c.customer_id NOT IN (SELECT s.customer_id FROM suppressions s WHERE s.tenant_id = :tenant)"
        ),
    },
    "RW-NOT-EXISTS": {
        "original": "QF-AUDIENCE",
        "description": "Use EXISTS and replace NOT IN with NOT EXISTS.",
        "sql": (
            "SELECT c.customer_id FROM customers c "
            "WHERE c.tenant_id = :tenant AND c.marketing_opt_in = 1 "
            "AND EXISTS (SELECT 1 FROM orders o WHERE o.tenant_id = c.tenant_id AND o.customer_id = c.customer_id AND o.created_day >= :since_day) "
            "AND NOT EXISTS (SELECT 1 FROM suppressions s WHERE s.tenant_id = c.tenant_id AND s.customer_id = c.customer_id)"
        ),
    },
    "RW-EXISTS-NO-TENANT": {
        "original": "QF-AUDIENCE",
        "description": "Correlated EXISTS that omits the tenant predicate in the subquery.",
        "sql": (
            "SELECT c.customer_id FROM customers c "
            "WHERE c.tenant_id = :tenant AND c.marketing_opt_in = 1 "
            "AND EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id AND o.created_day >= :since_day) "
            "AND c.customer_id NOT IN (SELECT s.customer_id FROM suppressions s WHERE s.tenant_id = :tenant)"
        ),
    },
}
