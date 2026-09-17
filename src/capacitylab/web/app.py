"""Local web UI (FastAPI + server-rendered templates). Binds to localhost by default; no external assets."""

from __future__ import annotations

import re
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from capacitylab.capacity.cost import choose_rate_card
from capacitylab.capacity.options import build_context, evaluate_all, needed_instance_classes
from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.evidence.models import PROVENANCE_LABELS, EvidenceKind
from capacitylab.factory import make_provider, make_sandbox_factory
from capacitylab.findings import review_findings
from capacitylab.report import render_markdown
from capacitylab.scenarios.loader import list_scenarios, load_evidence_file, load_scenario
from capacitylab.scenarios.validate import validate_scenario
from capacitylab.settings import Settings
from capacitylab.simulation.orchestrator import Orchestrator
from capacitylab.simulation.roles import ROLE_ORDER, ROLES
from capacitylab.simulation.run import RunConfig, SimulationRun
from capacitylab.spend import SpendLedger
from capacitylab.web.charts import event_bands, shared_y_max, utilization_panel

HERE = Path(__file__).parent


def usd(value) -> str:
    """$129.92 below a thousand, $18,708 above it; negative amounts are savings and keep their sign."""
    if value is None:
        return "not modeled"
    sign = "-" if value < 0 else ""
    amount = abs(float(value))
    if amount == 0:
        return "$0"
    return f"{sign}${amount:,.0f}" if amount >= 1000 else f"{sign}${amount:,.2f}"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
LAB_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.yaml$")
AWS_DIR = "imports"
AWS_NAME_RE = re.compile(r"^aws-[A-Za-z0-9_.-]+\.yaml$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

STATUS_LABELS = {
    "concluded": "Finished",
    "budget_exhausted": "Stopped: budget reached",
    "failed": "Failed",
    "invalid": "Invalid scenario",
    "created": "Created",
    "validated": "Validated",
    "deliberating": "In progress",
    "executing_tools": "Running checks",
}
APPROACH_LABELS = {"multi_stakeholder": "Five-role review", "single_agent": "Single reviewer", "rule_based": "Simple rules"}
ENVIRONMENT_LABELS = {"fixture": "scenario data", "lab": "local lab", "import": "imported"}


@dataclass
class Job:
    id: str
    kind: str  # "run" | "lab"
    title: str
    messages: list[str] = field(default_factory=list)
    target: str | None = None
    error: str | None = None
    done: bool = False


def create_app(settings: Settings | None = None, inline_jobs: bool = False) -> FastAPI:
    settings = settings or Settings.from_env()
    runs_dir = Path(settings.runs_dir)
    lab_dir = runs_dir / "lab"
    app = FastAPI(title="CapacityLab", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        provenance_labels=PROVENANCE_LABELS,
        role_titles={r.value: d.title for r, d in ROLES.items()},
        status_labels=STATUS_LABELS,
        approach_labels=APPROACH_LABELS,
        environment_labels=ENVIRONMENT_LABELS,
        role_initials={"database_engineer": "DB", "application_owner": "AO", "reliability_engineer": "SRE",
                       "finops_analyst": "FO", "tenant_representative": "TR", "single_agent": "1"},
    )
    role_names = {r.value: d.title for r, d in ROLES.items()}
    templates.env.filters["role"] = lambda role: role_names.get(role, str(role).replace("_", " "))
    templates.env.filters["usd"] = usd
    jobs: dict[str, Job] = {}
    evaluations: dict[str, object] = {}

    def ledger() -> SpendLedger:
        return SpendLedger(runs_dir / "spend-ledger.json")

    def page(request: Request, name: str, **context) -> HTMLResponse:
        budget = {"spent": ledger().spent_usd(), "total": settings.max_usd_total}
        return templates.TemplateResponse(request, name, {"settings": settings, "budget": budget, **context})

    def start_job(job: Job, work) -> RedirectResponse:
        jobs[job.id] = job

        def wrapped() -> None:
            try:
                work(job)
            except Exception as exc:  # shown on the job page
                job.error = f"{type(exc).__name__}: {exc}"
                job.messages.append(traceback.format_exc(limit=3))
            finally:
                job.done = True

        if inline_jobs:
            wrapped()
        else:
            threading.Thread(target=wrapped, daemon=True).start()
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    def scenario_or_404(sid: str, extra: list[Path] | None = None):
        try:
            return load_scenario(sid, extra or [])
        except FileNotFoundError as exc:
            raise HTTPException(404, "unknown scenario") from exc

    def run_or_404(run_id: str) -> SimulationRun:
        path = runs_dir / f"{run_id}.json"
        if not RUN_ID_RE.match(run_id) or not path.is_file():
            raise HTTPException(404, "unknown run")
        return SimulationRun.load(path)

    def option_labels(scenario) -> dict[str, str]:
        return {"undecided": "No position yet", **{o.id: o.label for o in scenario.options}}

    def panels(scenario, outcomes: list[dict]) -> list[dict]:
        labels = scenario.horizon.slot_labels()
        y_max = shared_y_max(outcomes, scenario.utilization_threshold_pct)
        bands = event_bands(scenario, labels)
        return [{"outcome": o, "svg": utilization_panel(o, labels, scenario.utilization_threshold_pct, y_max, bands)}
                for o in outcomes]

    def evidence_files() -> list[dict]:
        """Attachable evidence files (from `capacitylab lab run` or `capacitylab import`) under the runs directory."""
        if not runs_dir.is_dir():
            return []
        out = []
        for path in sorted(runs_dir.rglob("*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                items = load_evidence_file(path)
            except Exception:  # not an evidence file
                continue
            if not items:
                continue
            envs = sorted({i.environment for i in items})
            out.append({"path": str(path.relative_to(runs_dir)), "name": path.name, "item_count": len(items),
                        "environments": [ENVIRONMENT_LABELS.get(e, e) for e in envs],
                        "modified": datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%d %H:%M UTC")})
        return out

    def resolve_evidence(paths: list[str]) -> list[Path]:
        resolved = []
        root = runs_dir.resolve()
        for rel in paths:
            candidate = (runs_dir / rel).resolve()
            if not rel.endswith(".yaml") or root not in candidate.parents or not candidate.is_file():
                raise HTTPException(400, f"invalid evidence file {rel!r}")
            resolved.append(candidate)
        return resolved

    def recent_runs(limit: int = 12) -> list[dict]:
        if not runs_dir.is_dir():
            return []
        rows = []
        for path in sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if len(rows) >= limit:
                break
            try:
                run = SimulationRun.load(path)
            except ValueError:
                continue
            rows.append({"run_id": run.run_id, "scenario_id": run.scenario_id, "provider": run.provider, "model": run.model,
                         "status": run.state.status.value, "mocked": run.mocked, "rounds": run.rounds_completed,
                         "extra": len(run.extra_evidence_items), "spend": run.spend_usd,
                         "created_at": run.created_at.strftime("%Y-%m-%d %H:%M UTC"), "file": path.stem})
        return rows

    def lab_status(engine: str = "mysql") -> dict:
        if engine == "postgres":
            pg = settings.postgres
            if pg.host not in LOCAL_HOSTS:
                return {"ok": False, "detail": f"Refusing non-local host {pg.host}; the lab only runs against a local container."}
            try:
                import psycopg

                with psycopg.connect(host=pg.host, port=pg.port, user=pg.user, password=pg.password,
                                     dbname="postgres", connect_timeout=1) as conn:
                    version = conn.execute("SHOW server_version").fetchone()[0].split()[0]
                return {"ok": True, "detail": f"PostgreSQL {version} at {pg.host}:{pg.port}"}
            except ImportError:
                return {"ok": False, "detail": "psycopg is not installed (pip install -e '.[postgres]')."}
            except Exception as exc:
                return {"ok": False, "detail": f"Not reachable at {pg.host}:{pg.port} ({type(exc).__name__})."}
        m = settings.mysql
        if m is None:
            return {"ok": False, "detail": "No MySQL settings configured."}
        if m.host not in LOCAL_HOSTS:
            return {"ok": False, "detail": f"Refusing non-local host {m.host}; the lab only runs against a local container."}
        try:
            import pymysql

            conn = pymysql.connect(host=m.host, port=m.port, user=m.user, password=m.password, connect_timeout=1)
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                version = cur.fetchone()[0]
            conn.close()
            return {"ok": True, "detail": f"MySQL {version} at {m.host}:{m.port}"}
        except Exception as exc:
            return {"ok": False, "detail": f"Not reachable at {m.host}:{m.port} ({type(exc).__name__})."}

    def lab_files() -> list[dict]:
        if not lab_dir.is_dir():
            return []
        out = []
        for path in sorted(lab_dir.glob("*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                items = {i.id: i for i in load_evidence_file(path)}
            except Exception:
                continue
            cmp = items.get("EV-LAB-CMP")
            if cmp is None:
                continue
            config = cmp.data.get("config", {})
            out.append({"name": path.name, "scenario_id": cmp.data.get("scenario_id", "?"), "engine": cmp.data.get("engine", "?"),
                        "phases": [p["name"] for p in cmp.data.get("phases", [])], "duration_s": config.get("duration_s"),
                        "total_qps": config.get("total_qps"),
                        "modified": datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%d %H:%M UTC")})
        return out

    def lab_compatible(scenario) -> bool:
        from capacitylab.lab.workload import build_schedule

        try:
            build_schedule(scenario, 1.0, 10, 0.1, "check")
            return True
        except ValueError:
            return False

    # --- pages -------------------------------------------------------------------------------------

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        cards = []
        for sid in list_scenarios():
            scenario, bundle = load_scenario(sid)
            cards.append({"scenario": scenario, "evidence": len(bundle), "gaps": len(bundle.missing),
                          "findings": review_findings(scenario, bundle)[:2]})
        return page(request, "index.html", cards=cards, runs=recent_runs(), lab=lab_status(), lab_runs=lab_files()[:3])

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        return page(request, "runs.html", runs=recent_runs(200))

    def what_if_sliders(scenario, bundle) -> list[dict]:
        """Numeric assumptions that drive the traffic forecast, with values other evidence puts on them."""
        sliders = []
        for event in scenario.events:
            aid = getattr(event, "multiplier_assumption", None)
            if not aid or any(s["id"] == aid for s in sliders):
                continue
            assumption = scenario.assumption(aid)
            if not isinstance(assumption.value, int | float):
                continue
            tenant = getattr(event, "tenant", None)
            marks = sorted({(float(a.value), item.id) for item in bundle for a in item.assertions
                            if tenant and a.key == f"campaign.{tenant}.traffic_multiplier"
                            and isinstance(a.value, int | float)})
            value = float(assumption.value)
            sliders.append({"id": aid, "value": value, "min": 1.0, "max": max(6.0, round(value * 1.5)), "step": 0.1,
                            "label": event.name if getattr(event, "name", None) else f"{event.kind.replace('_', ' ')} for {tenant}",
                            "statement": assumption.statement,
                            "marks": [{"value": v, "evidence_id": e} for v, e in marks]})
        return sliders

    def options_view(scenario, bundle, overrides: dict[str, float] | None = None) -> dict:
        context = build_context(scenario, bundle, overrides)
        outcomes = [o.model_dump() for o in evaluate_all(context)]
        safe = [o for o in outcomes if o["total_slo_breach_slots"] == 0]
        free = [o for o in safe if o["cost_delta_event_usd"] == 0 and o["cost_delta_month_usd"] == 0]
        headroom = [o for o in safe if o["slots_over_threshold"] == 0]
        return {"panels": panels(scenario, outcomes), "prices_note": context.rate_card_note, "safe_count": len(safe),
                "option_count": len(outcomes), "free_safe": free, "headroom_safe": headroom}

    @app.get("/scenarios/{sid}/options", response_class=HTMLResponse)
    def scenario_options(request: Request, sid: str):
        scenario, bundle = scenario_or_404(sid)
        allowed = {s["id"]: s for s in what_if_sliders(scenario, bundle)}
        overrides = {}
        for key, raw in request.query_params.items():
            if key not in allowed:
                raise HTTPException(400, f"{key} cannot be changed here")
            try:
                value = float(raw)
            except ValueError as exc:
                raise HTTPException(400, f"{key} must be a number") from exc
            if not allowed[key]["min"] <= value <= allowed[key]["max"]:
                raise HTTPException(400, f"{key} must be between {allowed[key]['min']} and {allowed[key]['max']}")
            overrides[key] = value
        return templates.TemplateResponse(request, "_options_live.html",
                                          {"scenario": scenario, **options_view(scenario, bundle, overrides)})

    @app.get("/scenarios/{sid}", response_class=HTMLResponse)
    def scenario_page(request: Request, sid: str, evidence: str | None = None):
        scenario, bundle = scenario_or_404(sid)
        grouped: dict[str, list] = {}
        for item in bundle:
            grouped.setdefault(item.kind.value.replace("_", " "), []).append(item)
        return page(request, "scenario.html", scenario=scenario, bundle=bundle, grouped=grouped,
                    issues=validate_scenario(scenario, bundle), contradictions=bundle.contradictions(),
                    sliders=what_if_sliders(scenario, bundle), **options_view(scenario, bundle),
                    llm_ready=settings.credentials_present(settings.llm_provider),
                    findings=review_findings(scenario, bundle),
                    files=evidence_files(), preselected=evidence, lab=lab_status(), lab_ok=lab_compatible(scenario))

    @app.post("/scenarios/{sid}/run")
    async def start_run(request: Request, sid: str):
        form = await request.form()
        provider = form.get("provider", "mock")
        sandbox = form.get("sandbox", "sqlite")
        try:
            max_rounds = int(form.get("max_rounds", 4))
        except ValueError as exc:
            raise HTTPException(400, "invalid rounds") from exc
        if provider not in {"mock", settings.llm_provider} or sandbox not in {"sqlite", "mysql"} or not 1 <= max_rounds <= 6:
            raise HTTPException(400, "invalid run parameters")
        extra_paths = resolve_evidence([str(v) for v in form.getlist("evidence")])
        scenario, bundle = scenario_or_404(sid, extra_paths)
        extra_items = [item for path in extra_paths for item in load_evidence_file(path)]

        def work(job: Job) -> None:
            turn_provider = make_provider(provider, settings)
            max_usd = min(scenario.budgets.max_usd, settings.max_usd_per_run)
            spend = ledger()
            if not turn_provider.mocked:
                remaining = spend.remaining_usd(settings.max_usd_total)
                if remaining <= 0:
                    raise RuntimeError(f"the total model budget of ${settings.max_usd_total:.2f} is used up")
                max_usd = min(max_usd, remaining)
            config = RunConfig(max_rounds=max_rounds, max_tool_calls=min(scenario.budgets.max_tool_calls, settings.max_tool_calls),
                               max_tool_calls_per_turn=scenario.budgets.max_tool_calls_per_turn, max_usd=max_usd,
                               sandbox=sandbox)
            run = Orchestrator(scenario, bundle, turn_provider, config, make_sandbox_factory(sandbox, settings),
                               progress=job.messages.append,
                               lab_mysql=settings.mysql if sandbox == "mysql" else None).run()
            run.extra_evidence_items = extra_items
            if not turn_provider.mocked:
                spend.record(run.run_id, run.provider, run.model, run.spend_usd, run.state.status.value)
            path = run.save(runs_dir / f"{run.run_id}.json")
            path.with_suffix(".md").write_text(render_markdown(run, scenario))
            job.target = f"/runs/{run.run_id}"

        who = "scripted roles" if provider == "mock" else "the Anthropic model"
        return start_job(Job(id=uuid.uuid4().hex[:10], kind="run", title=f"{scenario.title} with {who}"), work)

    @app.get("/jobs/{job_id}.json")
    def job_status(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return JSONResponse({"done": job.done, "target": job.target, "error": job.error, "messages": job.messages[-60:]})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        if job.done and job.target:
            return RedirectResponse(job.target, status_code=303)
        return page(request, "job.html", job=job)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        run = run_or_404(run_id)
        scenario, base_bundle = scenario_or_404(run.scenario_id)
        rate_cards = [i for i in [*base_bundle, *run.extra_evidence_items] if i.kind == EvidenceKind.RATE_CARD]
        price_choice = choose_rate_card(rate_cards, needed_instance_classes(scenario))
        roles = [r.value for r in ROLE_ORDER if any(t.role == r.value for t in run.turns)] or sorted({t.role for t in run.turns})
        matrix = {role: {t.round: t for t in run.turns if t.role == role} for role in roles}
        outcomes = [o.model_dump() for o in run.decision.option_outcomes] if run.decision else []
        evidence_titles = {e.id: e.title for e in [*run.tool_evidence, *run.extra_evidence_items]}
        lab = next((e for e in run.extra_evidence_items if e.id == "EV-LAB-CMP"), None)
        # The last round in which each agent's position actually moved. Moving off "undecided" is a first decision,
        # not a change of mind. Positions are compared directly rather than trusting the turn's own revision flag.
        shift_in: dict[str, tuple[int, str]] = {}
        for role in roles:
            seen = None
            for t in sorted((t for t in run.turns if t.role == role), key=lambda t: t.round):
                if seen is not None and t.draft.position != seen:
                    shift_in[role] = (t.round, "decided" if seen == "undecided" else "changed")
                seen = t.draft.position
        labels = option_labels(scenario)
        player = []
        previous: dict[str, str] = {}
        for rnd in range(1, run.rounds_completed + 1):
            turns = {}
            for t in run.turns:
                if t.round != rnd:
                    continue
                rationale = t.draft.position_rationale
                before = previous.get(t.role)
                # Moving off "undecided" is a first decision, not a change of mind (same rule as the final cards).
                shift = None if before is None or before == t.draft.position else (
                    "decided" if before == "undecided" else "changed")
                turns[t.role] = {
                    "position": t.draft.position, "label": labels.get(t.draft.position, t.draft.position),
                    "confidence": t.draft.confidence, "changed": shift == "changed", "shift": shift,
                    "rationale": rationale if len(rationale) <= 260 else rationale[:257].rsplit(" ", 1)[0] + "…",
                    "challenges": [{"target": role_names.get(c.target_role, c.target_role), "reason": c.reason}
                                   for c in t.draft.challenges],
                    "requests": [q.tool for q in t.draft.tool_requests],
                    "flags": sum(1 for f in t.findings if f.severity == "error"),
                }
            previous.update({role: turn["position"] for role, turn in turns.items()})
            checks = [{"tool": c.tool, "by": [role_names.get(r, r) for r in c.requested_by], "status": c.status,
                       "evidence_id": c.evidence_id, "title": evidence_titles.get(c.evidence_id, "")}
                      for c in run.tool_calls if c.round == rnd]
            player.append({"round": rnd, "turns": turns, "checks": checks})
        return page(request, "run.html", run=run, scenario=scenario, roles=roles, matrix=matrix, player=player,
                    prices_note=price_choice.note if price_choice else "",
                    rounds=list(range(1, run.rounds_completed + 1)), panels=panels(scenario, outcomes) if outcomes else [],
                    evidence_titles=evidence_titles, labels=option_labels(scenario), lab=lab, shift_in=shift_in,
                    gaps={g.id: g for g in scenario.missing_evidence},
                    checks_run=sum(1 for c in run.tool_calls if c.status == "ok"))

    @app.get("/runs/{run_id}/evidence/{evidence_id}", response_class=HTMLResponse)
    def evidence_page(request: Request, run_id: str, evidence_id: str):
        run = run_or_404(run_id)
        _, bundle = scenario_or_404(run.scenario_id)
        combined = EvidenceBundle([*bundle, *run.extra_evidence_items, *run.tool_evidence])
        if evidence_id not in combined:
            raise HTTPException(404, "unknown evidence")
        item = combined.get(evidence_id)
        call = next((c for c in run.tool_calls if c.evidence_id == evidence_id), None)
        return page(request, "evidence.html", run=run, item=item, call=call)

    @app.get("/runs/{run_id}/report.md", response_class=PlainTextResponse)
    def run_report(run_id: str):
        run = run_or_404(run_id)
        scenario, _ = scenario_or_404(run.scenario_id)
        return PlainTextResponse(render_markdown(run, scenario), media_type="text/markdown")

    @app.get("/runs/{run_id}/ledger.json")
    def run_ledger(run_id: str):
        return JSONResponse(run_or_404(run_id).model_dump(mode="json"))

    @app.get("/runs/{run_id}/replay", response_class=HTMLResponse)
    def run_replay(request: Request, run_id: str):
        from capacitylab.evaluation.replay import replay

        run = run_or_404(run_id)
        return page(request, "replay.html", run=run, report=replay(run))

    @app.get("/scenarios/{sid}/evaluate", response_class=HTMLResponse)
    def evaluate_page(request: Request, sid: str):
        from capacitylab.evaluation.compare import compare
        from capacitylab.simulation.providers.mock import MockProvider

        scenario, bundle = scenario_or_404(sid)
        if sid not in evaluations:
            evaluations[sid] = compare(scenario, bundle, MockProvider)
        return page(request, "evaluate.html", scenario=scenario, report=evaluations[sid], labels=option_labels(scenario))

    # --- lab -----------------------------------------------------------------------------------------

    @app.get("/lab", response_class=HTMLResponse)
    def lab_page(request: Request):
        scenarios = []
        for sid in list_scenarios():
            scenario, _ = load_scenario(sid)
            scenarios.append({"id": sid, "title": scenario.title, "compatible": lab_compatible(scenario)})
        return page(request, "lab.html", status=lab_status(), pg_status=lab_status("postgres"), files=lab_files(),
                    scenarios=scenarios)

    @app.post("/lab/run")
    def start_lab(scenario_id: str = Form(...), duration: float = Form(30), qps: float = Form(150), workers: int = Form(16),
                  percona: bool = Form(False), engine: str = Form("mysql")):
        if not (5 <= duration <= 120 and 20 <= qps <= 1000 and 2 <= workers <= 64):
            raise HTTPException(400, "duration 5-120 s, qps 20-1000, workers 2-64")
        if engine not in ("mysql", "postgres"):
            raise HTTPException(400, "engine must be mysql or postgres")
        if engine == "postgres" and percona:
            raise HTTPException(400, "Percona Toolkit checks apply to the MySQL lab only")
        scenario, _ = scenario_or_404(scenario_id)
        if not lab_compatible(scenario):
            raise HTTPException(400, "this scenario uses statements the lab fixture cannot run")
        if not lab_status(engine)["ok"]:
            raise HTTPException(400, f"the {engine} lab is not reachable; start it with "
                                     f"docker compose up -d --wait sandbox-{engine}")

        def work(job: Job) -> None:
            from capacitylab.lab.runner import LabConfig, run_lab, write_evidence

            config = LabConfig(duration_s=duration, total_qps=qps, workers=workers, percona=percona)
            if engine == "postgres":
                from capacitylab.lab.pg_runner import run_lab_postgres

                items = run_lab_postgres(scenario, settings.postgres, config, progress=job.messages.append)
            else:
                items = run_lab(scenario, settings.mysql, config, progress=job.messages.append)
            name = f"{scenario.id}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.yaml"
            write_evidence(items, lab_dir / name)
            job.target = f"/lab/{name}"

        return start_job(Job(id=uuid.uuid4().hex[:10], kind="lab", title=f"Lab run for {scenario.title}"), work)

    @app.get("/lab/{name}", response_class=HTMLResponse)
    def lab_detail(request: Request, name: str):
        path = lab_dir / name
        if not LAB_NAME_RE.match(name) or not path.is_file():
            raise HTTPException(404, "unknown lab run")
        items = {i.id: i for i in load_evidence_file(path)}
        cmp = items.get("EV-LAB-CMP")
        if cmp is None:
            raise HTTPException(404, "not a lab result")
        phases = cmp.data.get("phases", [])
        latency = {p["name"]: items[f"EV-LAB-{p['name']}-LAT"].data for p in phases if f"EV-LAB-{p['name']}-LAT" in items}
        plans = {p["name"]: [items[k] for k in (f"EV-LAB-{p['name']}-PLAN-AUD", f"EV-LAB-{p['name']}-PLAN-OH") if k in items]
                 for p in phases}
        digests = {p["name"]: items[f"EV-LAB-{p['name']}-DIG"].data.get("fingerprints", [])
                   for p in phases if f"EV-LAB-{p['name']}-DIG" in items}
        tables = next((i for i in items.values() if i.kind == EvidenceKind.TABLE_STATS), None)
        percona = {
            "digests": {p["name"]: items[f"EV-LAB-{p['name']}-PTQD"].data for p in phases if f"EV-LAB-{p['name']}-PTQD" in items},
            "duplicates": {p["name"]: items[f"EV-LAB-{p['name']}-PTDK"].data for p in phases if f"EV-LAB-{p['name']}-PTDK" in items},
            "deadlocks": items.get("EV-LAB-PTDL"),
            "advisor": items.get("EV-LAB-PTVA"),
        }
        return page(request, "lab_detail.html", file_name=name, cmp=cmp, phases=phases, latency=latency, plans=plans,
                    digests=digests, tables=tables, percona=percona, rel_path=str(path.relative_to(runs_dir)))

    # --- AWS import --------------------------------------------------------------------------------

    aws_dir = runs_dir / AWS_DIR

    def aws_target():
        from capacitylab.aws_import import AwsTarget

        if settings.aws_live:
            return AwsTarget(region=settings.aws_region, live=True, endpoint_url=None)
        return AwsTarget(region=settings.aws_region, endpoint_url=settings.aws_endpoint)

    def aws_status() -> dict:
        try:
            import boto3  # noqa: F401
        except ImportError:
            return {"ok": False, "detail": "boto3 is not installed (pip install -e '.[aws]').", "instances": []}
        from capacitylab.aws_import import UnsafeAwsTarget, list_instances

        try:
            target = aws_target()
        except UnsafeAwsTarget as exc:
            return {"ok": False, "detail": str(exc), "instances": []}
        where = f"AWS account, {settings.aws_region}" if target.live else f"emulator at {target.endpoint_url}"
        try:
            instances = list_instances(target)
        except Exception as exc:
            return {"ok": False, "detail": f"Not reachable: {where} ({type(exc).__name__}).", "instances": [],
                    "live": target.live}
        return {"ok": True, "detail": f"{len(instances)} DB instance{'s' if len(instances) != 1 else ''} in the {where}",
                "instances": instances, "live": target.live}

    def aws_files() -> list[dict]:
        if not aws_dir.is_dir():
            return []
        out = []
        for path in sorted(aws_dir.glob("aws-*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                items = load_evidence_file(path)
            except Exception:
                continue
            topo = next((i for i in items if i.kind == EvidenceKind.TOPOLOGY and i.id.startswith("EV-AWS-")), None)
            if topo is None:
                continue
            out.append({"name": path.name, "title": topo.title, "item_count": len(items),
                        "writer": topo.data.get("writer", {}).get("instance_class"), "engine": topo.data.get("engine"),
                        "source": topo.source.split(":", 1)[0],
                        "modified": datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%d %H:%M UTC")})
        return out

    @app.get("/aws", response_class=HTMLResponse)
    def aws_page(request: Request):
        return page(request, "aws.html", status=aws_status(), files=aws_files())

    @app.post("/aws/import")
    def start_aws_import(instance: str = Form(...), label: str = Form(""), hours: float = Form(24),
                         include_cost: bool = Form(False)):
        if not (1 <= hours <= 24 * 14):
            raise HTTPException(400, "hours must be between 1 and 336")
        status = aws_status()
        if not status["ok"]:
            raise HTTPException(400, f"AWS is not reachable: {status['detail']}")
        if instance not in {i["id"] for i in status["instances"]}:
            raise HTTPException(400, "unknown DB instance")

        def work(job: Job) -> None:
            from capacitylab.aws_import import import_aws
            from capacitylab.lab.runner import write_evidence

            job.messages.append("reading topology, metrics, prices and cost")
            result = import_aws(aws_target(), instance, hours=hours, label=label or None, include_cost=include_cost)
            for reason in result.skipped:
                job.messages.append(f"skipped {reason}")
            tag = result.items[0].id.removeprefix("EV-AWS-TOPO-").lower()
            name = f"aws-{tag}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.yaml"
            write_evidence(result.items, aws_dir / name)
            job.target = f"/aws/{name}"

        return start_job(Job(id=uuid.uuid4().hex[:10], kind="aws", title="AWS import"), work)

    @app.get("/aws/{name}", response_class=HTMLResponse)
    def aws_detail(request: Request, name: str):
        path = aws_dir / name
        if not AWS_NAME_RE.match(name) or not path.is_file():
            raise HTTPException(404, "unknown AWS import")
        items = {i.id: i for i in load_evidence_file(path)}
        find = lambda prefix: next((i for k, i in items.items() if k.startswith(prefix)), None)  # noqa: E731
        topo = find("EV-AWS-TOPO-")
        if topo is None:
            raise HTTPException(404, "not an AWS import")
        cpu = find("EV-AWS-CPU-")
        charts = []
        if cpu:
            labels = [s[-5:] for s in cpu.data["slots"]]
            for key, title in (("avg_by_slot", "Average CPU per 15 minutes"), ("max_by_slot", "Highest CPU per 15 minutes")):
                outcome = {"option_id": title, "utilization_by_slot": cpu.data[key],
                           "peak_utilization_pct": max(cpu.data[key])}
                y_max = shared_y_max([outcome], 80)
                charts.append({"title": title, "svg": utilization_panel(outcome, labels, 80, y_max, [], width=560, height=190)})
        scenarios = [{"id": sid, "title": load_scenario(sid)[0].title} for sid in list_scenarios()]
        return page(request, "aws_detail.html", file_name=name, topo=topo, cpu=cpu, charts=charts,
                    metrics=find("EV-AWS-MET-"), rate=find("EV-AWS-RATE-"), cost=find("EV-AWS-COST-"),
                    item_ids=list(items), scenarios=scenarios, rel_path=str(path.relative_to(runs_dir)))

    return app
