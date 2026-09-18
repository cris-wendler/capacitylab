# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load scenarios and their evidence bundles from YAML."""

from __future__ import annotations

from collections.abc import Iterable
from importlib import resources
from pathlib import Path

import yaml

from capacitylab.evidence.bundle import EvidenceBundle
from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.scenarios.models import Scenario

SCENARIO_DIR = Path(str(resources.files("capacitylab") / "data" / "scenarios"))


def list_scenarios() -> list[str]:
    return sorted(p.stem.replace("_", "-") for p in SCENARIO_DIR.glob("*.yaml") if not p.stem.endswith("_evidence"))


def scenario_path(id_or_path: str) -> Path:
    candidate = Path(id_or_path)
    if candidate.suffix in {".yaml", ".yml"} and candidate.is_file():
        return candidate
    path = SCENARIO_DIR / f"{id_or_path.replace('-', '_')}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"unknown scenario {id_or_path!r}; available: {', '.join(list_scenarios())}")
    return path


def load_evidence_file(path: str | Path) -> list[EvidenceItem]:
    doc = yaml.safe_load(Path(path).read_text()) or {}
    return [EvidenceItem.model_validate(raw) for raw in doc.get("items", [])]


def load_scenario(id_or_path: str, extra_evidence: Iterable[str | Path] = ()) -> tuple[Scenario, EvidenceBundle]:
    path = scenario_path(id_or_path)
    scenario = Scenario.model_validate(yaml.safe_load(path.read_text()))
    bundle = EvidenceBundle(load_evidence_file(path.parent / scenario.evidence_file), scenario.missing_evidence)
    bundle.add(forecast_evidence(scenario))
    for extra in extra_evidence:
        for item in load_evidence_file(extra):
            if item.id in bundle:
                raise ValueError(f"evidence id {item.id} from {extra} already exists in the scenario bundle")
            bundle.add(item)
    return scenario, bundle


def forecast_evidence(scenario: Scenario) -> EvidenceItem:
    # Imported lazily: capacity.options depends on scenario models.
    from capacitylab.capacity.options import assumption_values, build_forecast

    assumptions = assumption_values(scenario)
    forecast = build_forecast(scenario, assumptions)
    labels = scenario.horizon.slot_labels()
    totals = {
        fid: [round(sum(v[i] for v in by_tenant.values()), 2) for i in range(len(labels))]
        for fid, by_tenant in forecast["series"].items()
    }
    peak = {fid: max(values) for fid, values in totals.items()}
    used = sorted(
        {e.multiplier_assumption for e in scenario.events if hasattr(e, "multiplier_assumption")}
    )
    return EvidenceItem(
        id="EV-FC-001",
        kind=EvidenceKind.WORKLOAD_FORECAST,
        title=f"Statement rate forecast for {scenario.horizon.date} ({scenario.horizon.slot_minutes}-min slots)",
        provenance=Provenance.FORECAST,
        synthetic=True,
        source="generator:capacitylab.workload",
        method=f"seeded generator seed={scenario.workload.seed}; assumptions {', '.join(used) or 'none'}",
        cluster_id=scenario.cluster.id,
        data={
            "slots": labels,
            "total_qps_by_fingerprint": totals,
            "peak_qps_by_fingerprint": peak,
            "assumptions_applied": {a: assumptions[a] for a in used},
            "series_digest": forecast["digest"],
        },
        caveats=["Forecast inherits every multiplier assumption listed above; it is not an observation."],
    )
