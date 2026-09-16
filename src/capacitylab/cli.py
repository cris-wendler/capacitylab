"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from capacitylab.settings import Settings, load_dotenv


def _settings() -> Settings:
    load_dotenv()
    return Settings.from_env()


def _ledger(settings: Settings):
    from capacitylab.spend import SpendLedger

    return SpendLedger(settings.runs_dir / "spend-ledger.json")


def _pairs(text: str | None) -> dict[str, str]:
    out = {}
    for part in filter(None, (text or "").split(",")):
        if "=" not in part:
            raise SystemExit(f"expected key=value pairs, got {part!r}")
        key, value = part.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _load(args):
    from capacitylab.scenarios.loader import load_scenario

    return load_scenario(args.scenario, getattr(args, "evidence", None) or [])


def cmd_scenarios(args, settings) -> int:
    from capacitylab.scenarios.loader import list_scenarios, load_scenario

    for sid in list_scenarios():
        scenario, bundle = load_scenario(sid)
        print(f"{sid:20s} {len(bundle):3d} evidence items  {len(scenario.options)} options  {scenario.title}")
    return 0


def cmd_validate(args, settings) -> int:
    from capacitylab.scenarios.validate import validate_scenario

    scenario, bundle = _load(args)
    issues = validate_scenario(scenario, bundle)
    for issue in issues:
        print(f"{issue.level:7s} {issue.path}: {issue.message}")
    errors = sum(i.level == "error" for i in issues)
    print(f"{scenario.id}: {len(bundle)} evidence items, {errors} errors, {len(issues) - errors} warnings")
    return 1 if errors else 0


def cmd_run(args, settings) -> int:
    from capacitylab.factory import make_provider, make_sandbox_factory
    from capacitylab.report import render_markdown
    from capacitylab.scenarios.loader import load_evidence_file
    from capacitylab.simulation.orchestrator import Orchestrator
    from capacitylab.simulation.run import RunConfig

    scenario, bundle = _load(args)
    extra_items = [item for path in (args.evidence or []) for item in load_evidence_file(path)]
    provider_name = args.provider or settings.provider
    if provider_name == "anthropic" and not Settings.anthropic_credentials_present():
        print("note: ANTHROPIC_API_KEY is not set; the SDK will try other credential sources.", file=sys.stderr)
    provider = make_provider(provider_name, settings)
    max_usd = args.max_usd if args.max_usd is not None else min(scenario.budgets.max_usd, settings.max_usd_per_run)
    ledger = _ledger(settings)
    if not provider.mocked:
        remaining = ledger.remaining_usd(settings.max_usd_total)
        print(f"model budget: ${ledger.spent_usd():.4f} spent of ${settings.max_usd_total:.2f} total")
        if remaining <= 0:
            print("error: the total model budget is used up (raise CAPACITYLAB_MAX_USD_TOTAL to continue)", file=sys.stderr)
            return 3
        max_usd = min(max_usd, remaining)
        print(f"this run is capped at ${max_usd:.2f}")
    config = RunConfig(
        max_rounds=args.max_rounds or scenario.budgets.max_rounds,
        max_tool_calls=min(scenario.budgets.max_tool_calls, settings.max_tool_calls),
        max_tool_calls_per_turn=scenario.budgets.max_tool_calls_per_turn,
        max_usd=max_usd,
        sandbox=args.sandbox or settings.sandbox,
    )
    if provider.mocked:
        print("MOCK provider: stakeholder turns come from deterministic policies, not a language model.")
    progress = (lambda m: None) if args.quiet else (lambda m: print(f"  … {m}", flush=True))
    run = Orchestrator(scenario, bundle, provider, config, make_sandbox_factory(config.sandbox, settings),
                       progress=progress, lab_mysql=settings.mysql if config.sandbox == "mysql" else None).run()
    run.extra_evidence_items = extra_items
    if not provider.mocked:
        ledger.record(run.run_id, run.provider, run.model, run.spend_usd, run.state.status.value)
    out = Path(args.out) if args.out else settings.runs_dir / f"{run.run_id}.json"
    run.save(out)
    report_path = Path(args.report) if args.report else out.with_suffix(".md")
    report_path.write_text(render_markdown(run, scenario))
    print(f"status: {run.state.status.value}; rounds: {run.rounds_completed}; tool calls: {len(run.tool_calls)}; "
          f"spend: ${run.spend_usd:.4f} ({run.input_tokens} in / {run.output_tokens} out tokens)")
    for warning in run.warnings:
        print(f"warning: {warning}")
    if run.decision:
        for p in run.decision.final_positions:
            print(f"  {p.role:24s} {p.position:22s} ({p.confidence})")
        validation = run.decision.validation
        print(f"  disagreements: {len(run.decision.disagreements)}; validation errors: {validation['errors']} "
              f"{validation['by_code'] or ''}")
    print(f"ledger: {out}\nreport: {report_path}")
    return 0 if run.state.status.value == "concluded" else 2


def cmd_replay(args, settings) -> int:
    from capacitylab.evaluation.replay import replay
    from capacitylab.factory import make_sandbox_factory
    from capacitylab.simulation.run import SimulationRun

    run = SimulationRun.load(args.ledger)
    factory = make_sandbox_factory(args.sandbox, settings) if args.sandbox else None
    report = replay(run, factory)
    print(json.dumps(report.model_dump(), indent=2))
    print("replay verified" if report.verified else "replay NOT verified")
    return 0 if report.verified else 1


def cmd_report(args, settings) -> int:
    from capacitylab.report import render_markdown
    from capacitylab.scenarios.loader import load_scenario
    from capacitylab.simulation.run import SimulationRun

    run = SimulationRun.load(args.ledger)
    scenario, _ = load_scenario(run.scenario_id)
    text = render_markdown(run, scenario)
    if args.out:
        Path(args.out).write_text(text)
        print(args.out)
    else:
        print(text)
    return 0


def cmd_evaluate(args, settings) -> int:
    from capacitylab.evaluation.compare import compare
    from capacitylab.factory import make_provider, make_sandbox_factory

    scenario, bundle = _load(args)
    provider_name = args.provider or settings.provider
    ledger = _ledger(settings)
    per_run = None
    if provider_name != "mock":
        remaining = ledger.remaining_usd(settings.max_usd_total)
        if remaining <= 0:
            print("error: the total model budget is used up", file=sys.stderr)
            return 3
        per_run = min(settings.max_usd_per_run, remaining / 2)
        print(f"model budget: ${remaining:.2f} remaining; each of the two model runs is capped at ${per_run:.2f}")
    sandbox = args.sandbox or settings.sandbox
    report = compare(scenario, bundle, lambda: make_provider(provider_name, settings),
                     make_sandbox_factory(sandbox, settings), max_usd_per_run=per_run,
                     lab_mysql=settings.mysql if sandbox == "mysql" else None)
    spent = sum(a.spend_usd for a in report.approaches)
    if provider_name != "mock":
        ledger.record(f"evaluate-{scenario.id}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}", provider_name, settings.model, spent,
                      "evaluation")
    for c in report.caveats:
        print(f"! {c}")
    print(f"{'approach':18s} {'recommend':22s} {'breach regret':>13s} {'cost regret':>11s} {'root cause':>10s} "
          f"{'gap recall':>10s} {'cite err':>8s} {'ungrounded':>10s} {'disagree':>8s} {'spend':>8s}")
    for a in report.approaches:
        print(f"{a.approach:18s} {str(a.recommendation):22s} {str(a.breach_slot_regret):>13s} {str(a.cost_regret_usd):>11s} "
              f"{str(a.root_cause_identified):>10s} {str(a.gap_recall):>10s} {a.citation_errors:>8d} {a.ungrounded_numbers:>10d} "
              f"{a.unresolved_disagreements:>8d} {a.spend_usd:>8.4f}")
        for note in a.notes:
            print(f"    {note}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report.model_dump_json(indent=2))
        print(f"written: {args.out}")
    return 0


def cmd_lab(args, settings) -> int:
    import pymysql

    from capacitylab.lab.runner import LabConfig, run_lab_repeats, write_evidence
    from capacitylab.scenarios.loader import load_scenario

    scenario, _ = load_scenario(args.scenario)
    from capacitylab.lab.percona import DEFAULT_IMAGE, PerconaUnavailable

    config = LabConfig(duration_s=args.duration, total_qps=args.qps, workers=args.workers, percona=args.percona,
                       repeats=args.repeats)
    try:
        items = run_lab_repeats(scenario, settings.mysql, config, progress=lambda m: print(f"  … {m}", flush=True))
    except pymysql.err.OperationalError as exc:
        print(f"error: MySQL lab not reachable ({exc.args[0]}). Start it: docker compose up -d --wait sandbox-mysql",
              file=sys.stderr)
        return 4
    except PerconaUnavailable as exc:
        print(f"error: {exc}\nCheck that docker is running, the lab is up, and the image is present: docker pull {DEFAULT_IMAGE}",
              file=sys.stderr)
        return 4
    out = Path(args.out) if args.out else settings.runs_dir / "lab" / f"{scenario.id}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.yaml"
    write_evidence(items, out)
    cmp = next(i for i in items if i.id == "EV-LAB-CMP").data
    names = [p["name"] for p in cmp["phases"]]
    print(f"\n{cmp['engine']} · {config.duration_s:g}s per phase · {config.total_qps:g} qps baseline · {config.workers} workers")
    header = ["statement"] + [f"{n}: p95 ms (calls, errors)" for n in names]
    rows = []
    for fid, phases in cmp["by_fingerprint"].items():
        cells = []
        for n in names:
            ph = phases[n]
            flag = " low-sample" if ph["low_sample"] and ph["calls"] else ""
            cells.append(f"{ph['p95_ms'] if ph['p95_ms'] is not None else '-'} ({ph['calls']}, {ph['errors'] or 0}){flag}")
        rows.append([fid, *cells])
    widths = [max(len(r[i]) for r in [header, *rows]) + 3 for i in range(len(header))]
    for row in [header, *rows]:
        print("".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)))
    print()
    for key in ("lock_waits", "avg_row_lock_wait_ms", "deadlocks", "db_load_average_active_sessions", "threads_running_max"):
        print(f"{key:34s}" + "   ".join(f"{n}={cmp[key][n]}" for n in names))
    print(cmp["low_sample_note"])
    spread = next((i.data for i in items if i.id == "EV-LAB-SPREAD"), None)
    if spread:
        print(f"\nvariation across {spread['passes']} passes (median, then range across passes)")
        for fid, phases_data in spread["p95_ms"].items():
            cells = [f"{n}={phases_data[n]['median'] if phases_data[n]['median'] is not None else '-'}"
                     f" ±{phases_data[n]['range_pct'] if phases_data[n]['range_pct'] is not None else '-'}%"
                     for n in names]
            print(f"{fid:20s}" + "   ".join(cells))
        print(spread["note"])
    if cmp.get("percona_toolkit"):
        by_id = {i.id: i for i in items}
        print(f"\n{cmp['percona_toolkit']}")
        for item in items:
            if item.id.endswith("-PTDK"):
                for f in item.data["findings"]:
                    print(f"  {item.id}: {f['redundant_index']} is a {f['relation']} of {f['covered_by']}")
        if "EV-LAB-PTDL" in by_id:
            print(f"  EV-LAB-PTDL: {len(by_id['EV-LAB-PTDL'].data['deadlocks'])} deadlock(s) still in the InnoDB status")
        if "EV-LAB-PTVA" in by_id:
            advice = by_id["EV-LAB-PTVA"].data["advice"]
            print(f"  EV-LAB-PTVA: {sum(a['level'] == 'WARN' for a in advice)} warnings, "
                  f"{sum(a['level'] == 'NOTE' for a in advice)} notes about the lab server settings")
    print(f"\n{len(items)} evidence items written to {out}")
    print(f"use them: capacitylab run {scenario.id} --evidence {out}")
    return 0


def cmd_import(args, settings) -> int:
    from capacitylab import importers
    from capacitylab.evidence.normalize import TenantAttribution
    from capacitylab.lab.runner import write_evidence
    from capacitylab.scenarios.loader import load_evidence_file

    if args.kind == "slowlog":
        attribution = None
        if args.schema_map:
            attribution = TenantAttribution(mode="schema_map", schema_map=_pairs(args.schema_map))
        elif args.tenant_map or args.tenant_column:
            attribution = TenantAttribution(mode="tenant_column", tenant_column=args.tenant_column or "tenant_id",
                                            literal_map=_pairs(args.tenant_map))
        item = importers.import_slow_log(args.file, attribution, args.long_query_time)
    elif args.kind == "digest":
        item = importers.import_digest_export(args.file, args.window_seconds)
    elif args.kind == "plan":
        if not args.fingerprint:
            print("error: --fingerprint is required for plans", file=sys.stderr)
            return 1
        item = importers.import_explain_analyze(args.file, args.fingerprint)
    elif args.kind == "pt-query-digest":
        item = importers.import_pt_query_digest(args.file)
    elif args.kind == "pt-duplicate-keys":
        item = importers.import_pt_duplicate_keys(args.file)
    elif args.kind == "pt-deadlocks":
        item = importers.import_pt_deadlocks(args.file)
    else:
        item = importers.import_cloudwatch_json(args.file, args.unit or "", args.period)
    out = Path(args.out)
    existing = load_evidence_file(out) if out.is_file() else []
    if any(e.id == item.id for e in existing):
        print(f"error: {item.id} already exists in {out}; rename the input file or use another --out", file=sys.stderr)
        return 1
    write_evidence([*existing, item], out)
    print(f"{item.id}: {item.kind.value}, {item.label} -> {out}")
    return 0


def cmd_findings(args, settings) -> int:
    from capacitylab.findings import review_findings

    scenario, bundle = _load(args)
    findings = review_findings(scenario, bundle)
    label = {"high": "ACT  ", "medium": "WEIGH", "info": "NOTE "}
    for f in findings:
        print(f"\n{label[f.severity]} [{f.area}] {f.headline}")
        print(f"      {f.detail}")
        print(f"      what to do: {f.recommendation}")
        print(f"      evidence: {', '.join(f.evidence_ids)}")
    print(f"\n{len(findings)} findings from evidence alone (no review run, no model calls)")
    return 0


def cmd_spend(args, settings) -> int:
    ledger = _ledger(settings)
    spent = ledger.spent_usd()
    print(f"model spend: ${spent:.4f} of ${settings.max_usd_total:.2f} total "
          f"(${ledger.remaining_usd(settings.max_usd_total):.4f} remaining) — {ledger.path}")
    if ledger.path.is_file():
        for entry in json.loads(ledger.path.read_text())["entries"]:
            print(f"  {entry['at'][:19]}  {entry['run_id']:40s} {entry['model']:18s} ${entry['usd']:.4f}  {entry['status']}")
    return 0


def cmd_serve(args, settings) -> int:
    import uvicorn

    from capacitylab.web.app import create_app

    uvicorn.run(create_app(settings), host=args.host, port=args.port)
    return 0


def cmd_scan(args, settings) -> int:
    from capacitylab.scan import scan

    root = Path(args.root)
    denylist = root / ".local" / "denylist.json"
    patterns = root / ".local" / "patterns.json"
    findings = scan(root, denylist, patterns)
    for f in findings:
        print(f"{f.path}:{f.line}: {f.rule}")
    local = [name for name, path in (("denylist", denylist), ("patterns", patterns)) if path.is_file()]
    print(f"{len(findings)} findings" + ("" if local else " (no local rules present; generic rules only)"))
    return 1 if findings else 0


def cmd_sandbox_check(args, settings) -> int:
    from capacitylab.diagnostics import fixture_db as fx
    from capacitylab.factory import make_sandbox_factory

    sandbox = make_sandbox_factory(args.sandbox, settings)()
    try:
        stats = fx.build(sandbox)
        print(f"{sandbox.engine_version()}: fixture built ({stats.orders} orders, {stats.customers} customers)")
    finally:
        sandbox.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capacitylab", description="Stakeholder simulation for database capacity decisions")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scenarios", help="list scenarios").set_defaults(func=cmd_scenarios)

    p = sub.add_parser("validate", help="validate a scenario and its evidence")
    p.add_argument("scenario")
    p.add_argument("--evidence", action="append", help="extra evidence YAML (from `lab run` or `import`); repeatable")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("findings", help="what the evidence already says: capacity, availability, growth, contention")
    p.add_argument("scenario")
    p.add_argument("--evidence", action="append", help="extra evidence YAML (from `lab run` or `import`); repeatable")
    p.set_defaults(func=cmd_findings)

    p = sub.add_parser("run", help="run a stakeholder simulation")
    p.add_argument("scenario")
    p.add_argument("--provider", choices=["mock", "anthropic"])
    p.add_argument("--evidence", action="append", help="extra evidence YAML (from `lab run` or `import`); repeatable")
    p.add_argument("--max-rounds", type=int)
    p.add_argument("--max-usd", type=float, help="cap for this run (the total budget still applies)")
    p.add_argument("--sandbox", choices=["sqlite", "mysql"], help="mysql also enables the lab_load_test tool")
    p.add_argument("--out")
    p.add_argument("--report")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("replay", help="re-execute tool calls from a ledger and verify results")
    p.add_argument("ledger")
    p.add_argument("--sandbox", choices=["sqlite", "mysql"])
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("report", help="render a ledger as Markdown")
    p.add_argument("ledger")
    p.add_argument("--out")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("evaluate", help="compare the five-role review with a single reviewer and simple rules")
    p.add_argument("scenario")
    p.add_argument("--provider", choices=["mock", "anthropic"])
    p.add_argument("--evidence", action="append")
    p.add_argument("--sandbox", choices=["sqlite", "mysql"])
    p.add_argument("--out")
    p.set_defaults(func=cmd_evaluate)

    lab = sub.add_parser("lab", help="drive a real workload on the local MySQL lab and collect evidence")
    lab_sub = lab.add_subparsers(dest="lab_command", required=True)
    p = lab_sub.add_parser("run", help="run baseline, event, and event+index phases for a scenario")
    p.add_argument("scenario")
    p.add_argument("--duration", type=float, default=30.0, help="seconds per phase (default 30)")
    p.add_argument("--qps", type=float, default=150.0, help="baseline statements per second (default 150)")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--repeats", type=int, default=1,
                   help="passes over the phase set, each with its own seed; more than one adds a spread item")
    p.add_argument("--percona", action="store_true",
                   help="also run Percona Toolkit (pt-query-digest, pt-duplicate-key-checker, pt-deadlock-logger, "
                        "pt-variable-advisor, pt-mysql-summary) from its docker image")
    p.add_argument("--out")
    p.set_defaults(func=cmd_lab)

    p = sub.add_parser("import", help="build evidence from your own exports")
    p.add_argument("kind", choices=["slowlog", "digest", "plan", "metrics", "pt-query-digest", "pt-duplicate-keys",
                                    "pt-deadlocks"],
                   help="pt-query-digest: --output json; pt-duplicate-keys: pt-duplicate-key-checker text; "
                        "pt-deadlocks: pt-deadlock-logger --tab")
    p.add_argument("file")
    p.add_argument("--out", required=True, help="evidence YAML to create or append to")
    p.add_argument("--tenant-column", help="slowlog: tenant key column name (default tenant_id)")
    p.add_argument("--tenant-map", help="slowlog: literal=tenant pairs, e.g. 1=alder,2=birch")
    p.add_argument("--schema-map", help="slowlog: schema=tenant pairs for database-per-tenant layouts")
    p.add_argument("--long-query-time", type=float, help="slowlog: the server's long_query_time in seconds")
    p.add_argument("--window-seconds", type=float, help="digest: length of the collection window")
    p.add_argument("--fingerprint", help="plan: statement id the plan belongs to")
    p.add_argument("--unit", help="metrics: unit label")
    p.add_argument("--period", type=int, default=60, help="metrics: period in seconds")
    p.set_defaults(func=cmd_import)

    sub.add_parser("spend", help="show recorded model spend against the total budget").set_defaults(func=cmd_spend)

    p = sub.add_parser("serve", help="start the local web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("scan", help="scan for prohibited identities and internal references")
    p.add_argument("--root", default=".")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("sandbox-check", help="build the fixture in a sandbox to verify it works")
    p.add_argument("--sandbox", choices=["sqlite", "mysql"], default="sqlite")
    p.set_defaults(func=cmd_sandbox_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args, _settings())
