# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine-matched sandbox tests. Run with `make mysql-up && make test-mysql`."""

import os

import pytest

from capacitylab.diagnostics import experiments as ex
from capacitylab.diagnostics import fixture_db as fx
from capacitylab.settings import Settings

pytestmark = [
    pytest.mark.mysql,
    pytest.mark.skipif(os.environ.get("CAPACITYLAB_TEST_MYSQL") != "1", reason="set CAPACITYLAB_TEST_MYSQL=1 with the sandbox up"),
]


@pytest.fixture(scope="module")
def mysql_sandbox():
    from capacitylab.diagnostics.sandbox import MySQLSandbox

    m = Settings.from_env().mysql
    sb = MySQLSandbox(m.host, m.port, m.user, m.password or "change-me-local-only", m.database)
    sb.stats = fx.build(sb)
    yield sb
    sb.close()


def test_mysql_index_experiment_reduces_handler_reads(mysql_sandbox):
    result = ex.index_experiment(mysql_sandbox, ["IDX-TENANT-DAY-CUSTOMER"], ["QF-AUDIENCE"], 0.8, 7_200_000,
                                 mysql_sandbox.stats.orders)
    q = result["candidates"]["IDX-TENANT-DAY-CUSTOMER"]["measured"]["queries"]["QF-AUDIENCE"]
    assert result["work_unit"] == "mysql_handler_reads"
    assert q["work_after"] < q["work_before"]
    assert result["engine"].startswith("MySQL 8.0")


def test_mysql_rewrite_semantics_match_sqlite(mysql_sandbox, sandbox):
    mysql = ex.rewrite_equivalence(mysql_sandbox, sorted(fx.REWRITES))["rewrites"]
    sqlite = ex.rewrite_equivalence(sandbox, sorted(fx.REWRITES))["rewrites"]
    for rid in fx.REWRITES:
        assert mysql[rid]["equivalent_on_fixtures"] == sqlite[rid]["equivalent_on_fixtures"]
        assert [f["case"] for f in mysql[rid]["failures"]] == [f["case"] for f in sqlite[rid]["failures"]]
