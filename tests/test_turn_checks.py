# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks tightened after the first run with a real model: lab tools without a lab, numbers written as text."""

from capacitylab.simulation.roles import ROLES, RoleId, visible_evidence
from capacitylab.simulation.schema import Claim
from capacitylab.simulation.tools import LAB_TOOLS, TOOLS, tool_catalog
from capacitylab.simulation.validation import extract_numbers, validate_turn
from tests.conftest import make_draft


def test_lab_tools_are_not_offered_without_a_lab():
    without = {t["name"] for t in tool_catalog(RoleId.DATABASE_ENGINEER, lab_available=False)}
    assert without and not without & LAB_TOOLS
    assert LAB_TOOLS <= {t["name"] for t in tool_catalog(RoleId.DATABASE_ENGINEER)}


def test_version_labels_are_not_measurements():
    assert extract_numbers("Release 2.14 and version 3.2 ship with v1.10") == []
    assert extract_numbers("checkout p95 was 2.14 ms") == [(2.14, True)]


def test_numbers_in_titles_and_text_fields_count_but_calculated_numbers_do_not(campaign):
    scenario, bundle = campaign
    role = ROLES[RoleId.APPLICATION_OWNER]
    visible = {i.id for i in visible_evidence(role, bundle, scenario)}
    draft = make_draft(claims=[
        Claim(statement="The run log covers the last 14 runs.", evidence_ids=["EV-BAT-002"], basis="observed"),
        Claim(statement="The release shipping the widget is 2.14.", evidence_ids=["EV-REL-001"], basis="observed"),
        Claim(statement="The job averages 997 minutes per run.", evidence_ids=["EV-BAT-002"], basis="observed"),
    ])
    findings = validate_turn(draft, role, scenario, bundle, visible, set(TOOLS))
    errors = [(f.code, f.location) for f in findings if f.severity == "error"]
    assert errors == [("ungrounded_number", "claims[2]")], "a number the model calculated itself is still flagged"
