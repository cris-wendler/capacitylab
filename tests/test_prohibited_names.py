# SPDX-License-Identifier: AGPL-3.0-or-later
"""No personal identities, hosts, accounts, or internal database names may ship."""

import json

import pytest

from capacitylab.diagnostics.fixture_db import SCHEMA_IDENTIFIERS
from capacitylab.scan import hmac_token, load_denylist, scan
from tests.conftest import PROJECT_ROOT

DENYLIST = PROJECT_ROOT / ".local" / "denylist.json"


def test_generic_rules_find_nothing_in_project():
    findings = [f for f in scan(PROJECT_ROOT) if not f.rule.startswith("denylist")]
    assert findings == [], "\n".join(f"{f.path}:{f.line} {f.rule}" for f in findings)


@pytest.mark.skipif(not DENYLIST.is_file(), reason="local denylist not present (it is never committed)")
def test_local_denylist_finds_nothing_in_project():
    findings = scan(PROJECT_ROOT, DENYLIST)
    assert findings == [], "\n".join(f"{f.path}:{f.line} {f.rule}" for f in findings)


@pytest.mark.skipif(not DENYLIST.is_file(), reason="local denylist not present (it is never committed)")
def test_fixture_schema_does_not_reuse_denylisted_identifiers():
    key, categories = load_denylist(DENYLIST)
    for table in SCHEMA_IDENTIFIERS["tables"]:
        assert hmac_token(key, table) not in categories["table_names"], table
    for column in SCHEMA_IDENTIFIERS["columns"]:
        assert hmac_token(key, column) not in categories["column_names"], column


def test_scanner_detects_each_generic_rule(tmp_path):
    # Built from fragments so this test file itself stays clean.
    sample = " ".join([
        "ops" + "@" + "corp-internal.net",
        "10." + "20.30.40",
        "4580" + "00000001",
        "/Us" + "ers/someone/project",
        "arn" + ":aws:rds:x",
    ])
    (tmp_path / "notes.md").write_text(sample + "\nallowed: dev@example.com 127.0.0.1 198.51.100.7\n")
    rules = {f.rule for f in scan(tmp_path)}
    assert rules == {"email", "ipv4", "aws_account_id", "home_path", "aws_arn"}


def test_local_pattern_file_adds_project_specific_rules(tmp_path):
    """Site-specific patterns live in a git-ignored file, never in the repository."""
    (tmp_path / "notes.md").write_text("the box is named ho" + "stx-0042 internally\n")
    patterns = tmp_path / "patterns.json"
    patterns.write_text(json.dumps({"internal_host_naming": "ho" + r"stx-\d{2,}"}))
    assert {f.rule for f in scan(tmp_path, patterns=patterns)} == {"internal_host_naming"}
    assert scan(tmp_path) == [], "without the local file the generic rules find nothing here"
