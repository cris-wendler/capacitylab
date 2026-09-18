# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from capacitylab.evidence.models import EvidenceItem, EvidenceKind, Provenance
from capacitylab.scenarios.models import OptionSpec
from capacitylab.scenarios.state import InvalidTransition, RunState, RunStatus
from capacitylab.scenarios.validate import validate_scenario
from capacitylab.simulation.orchestrator import Orchestrator
from capacitylab.simulation.providers.mock import MockProvider


def errors(scenario, bundle):
    return [i for i in validate_scenario(scenario, bundle) if i.level == "error"]


def test_library_scenarios_validate(campaign, downsize):
    for scenario, bundle in (campaign, downsize):
        assert errors(scenario, bundle) == []


def test_contradiction_is_reported_as_warning(campaign):
    issues = validate_scenario(*campaign)
    assert any(i.level == "warning" and "campaign.alder.traffic_multiplier" in i.message for i in issues)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda s: s.cluster.__setattr__("writer_instance", "db.fake.huge"), "unknown instance class"),
        (lambda s: s.events[0].__setattr__("tenant", "nobody"), "campaign tenant"),
        (lambda s: s.events[0].__setattr__("multiplier_assumption", "A-MISSING"), "unknown assumption"),
        (lambda s: s.options.append(OptionSpec(id="OPT-BAD", kind="combined", label="x",
                                               params={"components": ["OPT-NOPE"]})), "unknown component"),
        (lambda s: s.options.append(OptionSpec(id="OPT-S", kind="scale_temporary", label="x", params={})),
         "missing parameter"),
        (lambda s: s.__setattr__("focal_tenant", "ghost"), "focal tenant"),
    ],
)
def test_validation_catches_broken_references(campaign, mutate, expected):
    scenario, bundle = campaign
    broken = scenario.model_copy(deep=True)
    mutate(broken)
    assert any(expected in e.message for e in errors(broken, bundle))


def test_evidence_for_undeclared_tenant_is_an_error(campaign):
    scenario, bundle = campaign
    b = bundle.copy()
    b.add(EvidenceItem(id="EV-TEN-GHOST-001", kind=EvidenceKind.TENANT_PROFILE, title="g", provenance=Provenance.ASSUMPTION,
                       source="s", tenant_id="ghost"))
    assert any("ghost" in e.message for e in errors(scenario, b))


def test_horizon_slots(campaign):
    h = campaign[0].horizon
    assert len(h.slot_starts()) == 48
    assert h.slot_labels()[0] == "12:00" and h.slot_index("18:00") == 24 and h.slot_index("24:00") == 48


def test_state_machine_transitions():
    state = RunState()
    for status in (RunStatus.VALIDATED, RunStatus.DELIBERATING, RunStatus.EXECUTING_TOOLS, RunStatus.DELIBERATING,
                   RunStatus.CONCLUDED):
        state.transition(status, "test")
    assert state.terminal and len(state.history) == 5
    with pytest.raises(InvalidTransition):
        state.transition(RunStatus.DELIBERATING, "after terminal")
    with pytest.raises(InvalidTransition):
        RunState().transition(RunStatus.EXECUTING_TOOLS, "skip validation")


def test_invalid_scenario_never_reaches_stakeholders(campaign):
    scenario, bundle = campaign
    broken = scenario.model_copy(deep=True)
    broken.cluster.writer_instance = "db.fake.huge"
    run = Orchestrator(broken, bundle, MockProvider()).run()
    assert run.state.status == RunStatus.INVALID and run.turns == []
