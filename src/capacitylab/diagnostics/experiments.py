"""Experiments executed against an isolated sandbox. Results are MEASURED locally, never in production.

Any translation of a local measurement into a production effect is a separate, explicitly
labeled MODELED step driven by a scenario assumption.
"""

from __future__ import annotations

from collections import Counter

from capacitylab.diagnostics import fixture_db as fx
from capacitylab.diagnostics.sandbox import Sandbox

LOCAL_CAVEAT = (
    "Measured in a local {engine} sandbox on synthetic data. Relative effects only; this is not evidence of "
    "equivalent performance on the production engine (e.g. Aurora)."
)


def default_params(sandbox: Sandbox, fingerprint_id: str, tenant: int = 1) -> dict:
    if fingerprint_id == "QF-AUDIENCE":
        return {"tenant": tenant, "since_day": fx.RECENT_SINCE_DAY}
    if fingerprint_id == "QF-ORDER-HISTORY":
        row = sandbox.fetchall(
            "SELECT customer_id FROM orders WHERE tenant_id = :tenant GROUP BY customer_id "
            "ORDER BY COUNT(*) DESC, customer_id LIMIT 1",
            {"tenant": tenant},
        )
        return {"tenant": tenant, "customer": row[0][0]}
    raise ValueError(f"no sandbox statement for {fingerprint_id}")


def _checkout_rows(n: int = 400) -> list[dict]:
    return [
        {"order_id": 50_000_000 + i, "tenant_id": 1 + i % 5, "customer_id": 1 + i % 300, "total": 1500 + i, "day": fx.TODAY - 1}
        for i in range(n)
    ]


def _plan_dict(sandbox: Sandbox, sql: str, params: dict) -> dict:
    p = sandbox.plan(sql, params)
    return {"indexes_used": sorted(set(p.indexes_used)), "full_scans": p.full_scans, "temporary_or_sort": p.uses_temporary_or_sort}


def explain_query(sandbox: Sandbox, fingerprint_id: str, candidate: str | None = None) -> dict:
    sql = fx.QUERIES[fingerprint_id]
    params = default_params(sandbox, fingerprint_id)
    created = None
    if candidate:
        spec = fx.INDEX_CANDIDATES[candidate]
        sandbox.create_index(spec["name"], spec["table"], spec["columns"])
        created = spec
    try:
        work = sandbox.measure(sql, params)
        return {
            "fingerprint_id": fingerprint_id,
            "with_candidate_index": candidate,
            "parameters": params,
            "plan": _plan_dict(sandbox, sql, params),
            "rows_returned": work.rows_returned,
            "work_units": work.work_units,
            "work_unit": work.unit,
            "engine": sandbox.engine_version(),
            "caveat": LOCAL_CAVEAT.format(engine=sandbox.backend),
        }
    finally:
        if created:
            sandbox.drop_index(created["name"], created["table"])


def index_experiment(
    sandbox: Sandbox,
    candidates: list[str],
    fingerprint_ids: list[str],
    cpu_exponent: float,
    production_rows: float | None,
    local_rows: int,
) -> dict:
    unknown = [c for c in candidates if c not in fx.INDEX_CANDIDATES]
    if unknown:
        raise ValueError(f"unknown index candidates: {unknown}; known: {sorted(fx.INDEX_CANDIDATES)}")
    params = {fid: default_params(sandbox, fid) for fid in fingerprint_ids}
    rows = _checkout_rows()
    baseline = {fid: sandbox.measure(fx.QUERIES[fid], params[fid]) for fid in fingerprint_ids}
    baseline_plans = {fid: _plan_dict(sandbox, fx.QUERIES[fid], params[fid]) for fid in fingerprint_ids}
    write_before = sandbox.write_overhead(fx.CHECKOUT_INSERT, rows)

    results = {}
    for candidate in candidates:
        spec = fx.INDEX_CANDIDATES[candidate]
        size = sandbox.create_index(spec["name"], spec["table"], spec["columns"])
        try:
            per_query = {}
            multipliers = {}
            for fid in fingerprint_ids:
                after = sandbox.measure(fx.QUERIES[fid], params[fid])
                before = baseline[fid]
                if after.rows_returned != before.rows_returned:
                    raise RuntimeError(f"{candidate} changed the result size of {fid}; index experiments must not")
                ratio = after.work_units / before.work_units if before.work_units else 1.0
                per_query[fid] = {
                    "work_before": before.work_units,
                    "work_after": after.work_units,
                    "work_ratio": round(ratio, 4),
                    "work_reduction_pct": round(100 * (1 - ratio), 1),
                    "plan_before": baseline_plans[fid],
                    "plan_after": _plan_dict(sandbox, fx.QUERIES[fid], params[fid]),
                }
                # Not capped at 1.0: a local plan regression must propagate into the modeled outcome.
                multipliers[fid] = round(ratio**cpu_exponent, 4)
            write_after = sandbox.write_overhead(fx.CHECKOUT_INSERT, rows)
        finally:
            sandbox.drop_index(spec["name"], spec["table"])
        redundant = [
            name
            for name, existing in fx.EXISTING_INDEXES.items()
            if existing["table"] == spec["table"] and spec["columns"][: len(existing["columns"])] == existing["columns"]
        ]
        overhead = (write_after.work_units - write_before.work_units) / max(1, write_before.work_units)
        modeled_gib = round(size / local_rows * production_rows / 2**30, 3) if production_rows else None
        results[candidate] = {
            "index": {"name": spec["name"], "table": spec["table"], "columns": spec["columns"]},
            "measured": {
                "index_bytes_local": size,
                "queries": per_query,
                "write_work_before": write_before.work_units,
                "write_work_after": write_after.work_units,
                "write_overhead_pct": round(100 * overhead, 1),
            },
            "redundancy": {
                "makes_existing_index_redundant": redundant,
                "note": (
                    "An existing index that is a left prefix of the candidate may become droppable, but only after "
                    "checking every statement that uses it."
                )
                if redundant
                else "No existing index is a left prefix of this candidate.",
            },
            "modeled_production_translation": {
                "cpu_multiplier_by_fingerprint": multipliers,
                "index_storage_gib": modeled_gib,
                "method": "multiplier = work_ratio ** A-CPU-ROWS-EXPONENT (regressions included); size scaled by row count",
                "exponent": cpu_exponent,
            },
        }
    return {
        "candidates": results,
        "engine": sandbox.engine_version(),
        "work_unit": baseline[fingerprint_ids[0]].unit if fingerprint_ids else None,
        "parameters": params,
        "caveat": LOCAL_CAVEAT.format(engine=sandbox.backend),
    }


def _classify(original: Counter, rewrite: Counter) -> str:
    if original == rewrite:
        return "identical"
    if set(original) == set(rewrite):
        return "duplicate_rows_differ"
    if set(rewrite) > set(original):
        return "extra_rows"
    if set(rewrite) < set(original):
        return "missing_rows"
    return "rows_differ"


def rewrite_equivalence(sandbox: Sandbox, rewrite_ids: list[str]) -> dict:
    unknown = [r for r in rewrite_ids if r not in fx.REWRITES]
    if unknown:
        raise ValueError(f"unknown rewrites: {unknown}; known: {sorted(fx.REWRITES)}")
    cases = [{"tenant": t, "since_day": d} for t in sorted(fx.TENANT_KEYS) for d in (fx.RECENT_SINCE_DAY, 0, fx.TODAY + 1)]
    null_suppression_tenants = {
        t for t in fx.TENANT_KEYS
        if sandbox.fetchall("SELECT COUNT(*) FROM suppressions WHERE tenant_id = :t AND customer_id IS NULL", {"t": t})[0][0]
    }
    out = {}
    for rid in rewrite_ids:
        rewrite = fx.REWRITES[rid]
        original_sql = fx.QUERIES[rewrite["original"]]
        failures = []
        for case in cases:
            a = Counter(sandbox.fetchall(original_sql, case))
            b = Counter(sandbox.fetchall(rewrite["sql"], case))
            kind = _classify(a, b)
            if kind == "identical":
                continue
            hints = []
            if kind == "duplicate_rows_differ":
                hints.append("rewrite returns duplicate rows (join fan-out)")
            if case["tenant"] in null_suppression_tenants and sum(a.values()) == 0:
                hints.append("NULL customer_id in suppressions makes NOT IN return no rows; NOT EXISTS does not")
            extra = [row[0] for row in set(b) - set(a)]
            if extra:
                sample = extra[:20]
                own = sandbox.fetchall(
                    "SELECT COUNT(DISTINCT customer_id) FROM orders WHERE tenant_id = :tenant AND created_day >= :since_day "
                    f"AND customer_id IN ({', '.join(str(int(x)) for x in sample)})",
                    case,
                )[0][0]
                if own < len(sample):
                    hints.append("extra rows qualify only through another tenant's orders (tenant boundary violated)")
            failures.append(
                {
                    "case": {"tenant": fx.TENANT_KEYS[case["tenant"]], "since_day": case["since_day"]},
                    "difference": kind,
                    "original_rows": sum(a.values()),
                    "rewrite_rows": sum(b.values()),
                    "hints": hints,
                }
            )
        params = {"tenant": 1, "since_day": fx.RECENT_SINCE_DAY}
        before = sandbox.measure(original_sql, params)
        after = sandbox.measure(rewrite["sql"], params)
        out[rid] = {
            "description": rewrite["description"],
            "original": rewrite["original"],
            "cases_checked": len(cases),
            "equivalent_on_fixtures": not failures,
            "failures": failures,
            "work_before": before.work_units,
            "work_after": after.work_units,
            "work_ratio": round(after.work_units / before.work_units, 4) if before.work_units else None,
        }
    return {
        "rewrites": out,
        "fixture_edge_cases": [
            "customer_id values repeat across tenants",
            "customers with many recent orders",
            "NULL customer_id suppressions for tenant cedar",
            "duplicate suppression rows",
        ],
        "engine": sandbox.engine_version(),
        "caveat": "Equivalence on fixtures is evidence, not proof. " + LOCAL_CAVEAT.format(engine=sandbox.backend),
    }
