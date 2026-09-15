from capacitylab import cli
from capacitylab.evaluation.replay import replay
from capacitylab.report import render_markdown
from capacitylab.simulation.run import SimulationRun


def test_ledger_roundtrip(campaign_run, tmp_path):
    path = campaign_run.save(tmp_path / "run.json")
    loaded = SimulationRun.load(path)
    assert loaded.model_dump(mode="json") == campaign_run.model_dump(mode="json")
    assert loaded.prompt_version.startswith("stakeholder-v1-") and loaded.provider == "mock"


def test_replay_reproduces_tools_and_decision(campaign_run):
    report = replay(campaign_run)
    assert report.scenario_match and report.initial_evidence_match
    assert report.tool_calls_replayed == sum(1 for r in campaign_run.tool_calls if not r.status.startswith("skipped"))
    assert report.mismatches == [] and report.decision_match and report.verified


def test_replay_detects_tampering(campaign_run):
    tampered = campaign_run.model_copy(deep=True)
    target = next(r for r in tampered.tool_calls if r.tool == "index_experiment")
    target.result_digest = "0" * 64
    tampered.scenario_digest = "not-the-scenario"
    report = replay(tampered)
    assert not report.verified and not report.scenario_match
    assert [m["call_id"] for m in report.mismatches] == [target.call_id]


def test_markdown_report_labels_mock_and_provenance(campaign_run, campaign):
    text = render_markdown(campaign_run, campaign[0])
    assert "MOCK RUN" in text and "## Final positions" in text and "## Optimization proposals" in text
    assert "not evidence that a recommendation is correct" in text
    for option in campaign[0].options:
        assert option.id in text
    assert "Experimentally measured" in text and "Modeled outcome" in text


def test_cli_run_replay_validate(tmp_path, capsys):
    ledger = tmp_path / "demo.json"
    assert cli.main(["validate", "campaign-overlap"]) == 0
    assert cli.main(["run", "campaign-overlap", "--provider", "mock", "--out", str(ledger), "--quiet"]) == 0
    assert ledger.is_file() and ledger.with_suffix(".md").is_file()
    assert cli.main(["replay", str(ledger)]) == 0
    out = capsys.readouterr().out
    assert "MOCK provider" in out and "replay verified" in out
