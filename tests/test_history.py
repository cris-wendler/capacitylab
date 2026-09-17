# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sample store and the envelope: accumulation, gaps, recency weighting and drift. No network, no cloud."""

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from capacitylab.history import History, build_envelope
from capacitylab.history.envelope import DRIFT_HALF_LIFE_DAYS, THIN_DAYS

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)  # a Thursday
# Fake names that must never reach the store. Split so the identifier scan does not match this file.
IDENTIFIERS = ["orders-prod-" + "cluster-9", "orders-prod-" + "writer-1"]


def cpu_points(start: datetime, hours: int, level, period_s: int = 300):
    """CloudWatch-style points, one per period, with `level(ts)` giving the average."""
    points, ts = [], start
    while ts < start + timedelta(hours=hours):
        average = level(ts)
        points.append({"Timestamp": ts, "Average": average, "Maximum": average * 1.2})
        ts += timedelta(seconds=period_s)
    return points


@pytest.fixture
def store(tmp_path):
    with History(tmp_path / "history.db") as history:
        yield history


def test_cluster_key_is_stable_per_store_and_stores_no_identifier(store, tmp_path):
    key = store.cluster_key(IDENTIFIERS)
    assert key == store.cluster_key(reversed(IDENTIFIERS))  # order does not matter
    assert key != store.cluster_key([*IDENTIFIERS, "orders-prod-" + "reader-2"])
    store.note_cluster(key, label="evening cluster", cloud="aws", engine="mysql")
    store.add_samples(key, "writer", "CPUUtilization", cpu_points(NOW - timedelta(hours=1), 1, lambda _: 40.0))
    dumped = tmp_path.joinpath("history.db").read_bytes().decode("latin-1")
    for identifier in IDENTIFIERS:
        assert identifier not in dumped
    with History(tmp_path / "other.db") as other:  # a different store salts differently
        assert other.cluster_key(IDENTIFIERS) != key


def test_samples_accumulate_and_recollecting_the_same_window_adds_nothing(store):
    key = store.cluster_key(IDENTIFIERS)
    first = cpu_points(NOW - timedelta(hours=6), 6, lambda _: 30.0)
    seen, written = store.add_samples(key, "writer", "CPUUtilization", first)
    assert (seen, written) == (72, 72)
    # An overlapping collection: three hours already held, three new.
    overlap = cpu_points(NOW - timedelta(hours=3), 6, lambda _: 55.0)
    seen, written = store.add_samples(key, "writer", "CPUUtilization", overlap)
    assert (seen, written) == (72, 36)
    kept = store.samples(key, "CPUUtilization", "writer")
    assert len(kept) == 108
    # The first collection of a period wins, so the held values are not rewritten.
    assert kept[0].avg == 30.0 and kept[-1].avg == 55.0
    assert store.samples(key, "CPUUtilization", "writer", since=NOW)[0].avg == 55.0


def test_collections_and_gaps_make_missing_stretches_visible(store):
    key = store.cluster_key(IDENTIFIERS)
    store.add_samples(key, "writer", "CPUUtilization", cpu_points(NOW - timedelta(hours=8), 2, lambda _: 20.0))
    store.record_collection(key, "aws:cloudwatch", NOW - timedelta(hours=8), NOW - timedelta(hours=6),
                            seen=24, written=24)
    # Nothing collected between -6 h and -2 h: the collector was not running.
    store.add_samples(key, "writer", "CPUUtilization", cpu_points(NOW - timedelta(hours=2), 2, lambda _: 20.0))
    store.record_collection(key, "aws:cloudwatch", NOW - timedelta(hours=2), NOW, seen=24, written=24,
                            note="resumed")
    gaps = store.gaps(key, "CPUUtilization", "writer")
    assert len(gaps) == 1
    assert gaps[0].start == NOW - timedelta(hours=6, minutes=5) and gaps[0].end == NOW - timedelta(hours=2)
    assert round(gaps[0].minutes) == 245
    assert store.coverage(key, "CPUUtilization", "writer", since=NOW - timedelta(hours=8), until=NOW) == 0.5
    assert store.last_collection(key)["note"] == "resumed"
    assert store.stale_for(key, now=NOW) == timedelta(minutes=5)
    assert store.metrics(key) == [("CPUUtilization", "writer", 48)]


def test_envelope_reports_the_evening_shape_per_weekday(store):
    key = store.cluster_key(IDENTIFIERS)
    # Four weeks of a daily shape: quiet by day, a peak at 19:00, and a higher peak on Thursdays.
    def level(ts):
        base = 25.0 + (35.0 if ts.hour == 19 else 0.0)
        return base + (20.0 if ts.hour == 19 and ts.weekday() == 3 else 0.0)

    store.add_samples(key, "writer", "CPUUtilization",
                      cpu_points(NOW - timedelta(days=28), 28 * 24, level), unit="Percent")
    envelope = build_envelope(store, key, "CPUUtilization", now=NOW)

    thursday_evening = envelope.at(datetime(2026, 9, 24, 19, 30, tzinfo=UTC))
    monday_evening = envelope.at(datetime(2026, 9, 21, 19, 30, tzinfo=UTC))
    monday_morning = envelope.at(datetime(2026, 9, 21, 9, 0, tzinfo=UTC))
    assert thursday_evening.label == "Thursday 19:00"
    assert thursday_evening.high == pytest.approx(80 * 1.2, abs=0.1)  # 25 + 35 + 20, times the 1.2 maximum
    assert monday_evening.high == pytest.approx(60 * 1.2, abs=0.1)
    assert monday_morning.high == pytest.approx(25 * 1.2, abs=0.1)
    assert thursday_evening.days >= THIN_DAYS and not thursday_evening.thin
    assert envelope.busiest(1)[0].label == "Thursday 19:00"
    assert envelope.quietest(1)[0].high == pytest.approx(30.0, abs=0.1)
    assert envelope.unit == "Percent" and envelope.observations == 28 * 24 * 12
    assert not envelope.drift.detected

    # A future evening can be read slot by slot, which is what a planner needs.
    window = envelope.over(datetime(2026, 9, 24, 18, 0, tzinfo=UTC), datetime(2026, 9, 24, 21, 0, tzinfo=UTC))
    assert [slot.high for _, slot in window] == [
        pytest.approx(25 * 1.2, abs=0.1), pytest.approx(80 * 1.2, abs=0.1), pytest.approx(25 * 1.2, abs=0.1)]


def test_recent_days_outweigh_older_ones(store):
    key = store.cluster_key(IDENTIFIERS)
    # Same hour every day: 20% for three weeks, then 50% for the last three days.
    def level(ts):
        return 50.0 if ts >= NOW - timedelta(days=3) else 20.0

    store.add_samples(key, "writer", "CPUUtilization",
                      cpu_points(NOW - timedelta(days=24), 24 * 24, level), unit="Percent")
    weighted = build_envelope(store, key, "CPUUtilization", now=NOW, half_life_days=3)
    flat = build_envelope(store, key, "CPUUtilization", now=NOW, half_life_days=10_000)
    slot = datetime(2026, 9, 16, 8, 30, tzinfo=UTC)  # a Wednesday, inside the shifted days
    # With a short half-life the recent level is the typical one; with a flat weighting the old level still wins.
    assert weighted.at(slot).typical == pytest.approx(50 * 1.2, abs=0.1)
    assert flat.at(slot).typical == pytest.approx(20 * 1.2, abs=0.1)
    assert weighted.at(slot).peak == flat.at(slot).peak == pytest.approx(50 * 1.2, abs=0.1)


def test_a_level_shift_is_detected_and_shortens_the_half_life(store):
    key = store.cluster_key(IDENTIFIERS)
    def level(ts):
        return 70.0 if ts >= NOW - timedelta(hours=20) else 30.0

    store.add_samples(key, "writer", "CPUUtilization",
                      cpu_points(NOW - timedelta(days=14), 14 * 24, level), unit="Percent")
    envelope = build_envelope(store, key, "CPUUtilization", now=NOW)
    drift = envelope.drift
    assert drift.detected and drift.direction == "up"
    assert drift.ratio == pytest.approx(70 / 30, abs=0.05)
    assert envelope.half_life_days == DRIFT_HALF_LIFE_DAYS
    assert "rebuilt" in drift.note
    item = envelope.to_evidence(name="evening cluster")
    assert any("level has shifted" in c for c in item.caveats)


def test_quiet_history_reports_no_drift_and_thin_evidence_is_flagged(store):
    key = store.cluster_key(IDENTIFIERS)
    store.add_samples(key, "writer", "CPUUtilization",
                      cpu_points(NOW - timedelta(days=1), 24, lambda _: 40.0), unit="Percent")
    envelope = build_envelope(store, key, "CPUUtilization", now=NOW)
    assert not envelope.drift.detected
    assert envelope.thin  # one day of history
    assert envelope.at(NOW - timedelta(hours=2)).days == 1
    item = envelope.to_evidence()
    assert any("provisional" in c for c in item.caveats)


def test_envelope_evidence_is_modeled_and_states_what_it_is_not(store):
    key = store.cluster_key(IDENTIFIERS)
    store.add_samples(key, "writer", "CPUUtilization",
                      cpu_points(NOW - timedelta(days=10), 10 * 24, lambda ts: 30.0 + ts.hour), unit="Percent")
    item = build_envelope(store, key, "CPUUtilization", now=NOW).to_evidence(name="evening cluster")
    assert item.provenance.value == "modeled" and item.kind.value == "metric_summary"
    assert item.id.startswith("EV-ENV-CPUUTILIZATI-CL")  # the metric tag is capped at 12 characters
    assert "not a prediction" in " ".join(item.caveats)
    assert "not part of these numbers" in " ".join(item.caveats)
    assert item.data["slot_minutes"] == 60 and item.data["unit"] == "Percent"
    assert len(item.data["slots"]) == 7 * 24
    assert item.data["busiest_slots"][0]["time"] == "23:00"
    payload = json.dumps(item.model_dump(mode="json"))
    for identifier in IDENTIFIERS:
        assert identifier not in payload


def test_weighting_and_slot_maths():
    from capacitylab.history.envelope import _slot_values, _weight, _weighted_quantile

    # Weighted quantile is nearest-rank: with equal weights it returns a value that is actually in the data.
    pairs = [(10.0, 1.0), (20.0, 1.0), (30.0, 1.0), (100.0, 1.0)]
    assert _weighted_quantile(pairs, 0.5) == 20.0
    assert _weighted_quantile(pairs, 0.95) == 100.0
    # Weight halves every half-life.
    assert _weight(NOW - timedelta(days=7), NOW, 7) == pytest.approx(0.5)
    assert _weight(NOW - timedelta(days=14), NOW, 7) == pytest.approx(0.25)
    assert _weight(NOW, NOW, 7) == 1.0
    # A dominant weight pulls the median onto its value.
    assert _weighted_quantile([(10.0, 0.01), (90.0, 10.0)], 0.5) == 90.0
    # Raw periods roll into one slot: average of averages, maximum of maxima.
    samples = [type("S", (), {"ts": NOW + timedelta(minutes=m), "avg": 10.0 + m, "max": 50.0 + m,
                              "period_s": 300})() for m in (0, 5, 10)]
    rolled = _slot_values(samples, 60)
    assert rolled[NOW] == (15.0, 60.0)
    assert math.isclose(sum(a for a, _ in rolled.values()), 15.0)


def test_store_rejects_an_impossible_slot_size(store):
    key = store.cluster_key(IDENTIFIERS)
    with pytest.raises(ValueError, match="divide a day"):
        build_envelope(store, key, "CPUUtilization", slot_minutes=7)


def test_collect_appends_two_windows_from_a_stubbed_cloud(tmp_path, monkeypatch):
    """The AWS read path, pointed at the store: two overlapping collections accumulate without duplicating."""
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import ANY, Stubber

    from capacitylab.aws_import import AwsTarget
    from capacitylab.history.collect import collect_aws

    writer, reader = "orders-prod-" + "writer-3", "orders-prod-" + "replica-3"

    def instance(identifier, replicas=()):
        return {"DBInstanceIdentifier": identifier, "DBInstanceClass": "db.r6i.16xlarge", "Engine": "mysql",
                "EngineVersion": "8.0.39", "DBInstanceStatus": "available", "MultiAZ": False, "StorageType": "gp3",
                "AllocatedStorage": 500, "ReadReplicaDBInstanceIdentifiers": list(replicas)}

    def clients(window_start, hours):
        rds, cloudwatch = (boto3.client(s, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
                           for s in ("rds", "cloudwatch"))
        s_rds, s_cw = Stubber(rds), Stubber(cloudwatch)
        s_rds.add_response("describe_db_instances", {"DBInstances": [instance(writer, [reader])]},
                           {"DBInstanceIdentifier": writer})
        s_rds.add_response("describe_db_instances", {"DBInstances": [instance(reader)]},
                           {"DBInstanceIdentifier": reader})
        for _node in ("writer", "reader"):
            for _metric in range(5):
                s_cw.add_response("get_metric_statistics",
                                  {"Datapoints": cpu_points(window_start, hours, lambda ts: 20.0 + ts.hour),
                                   "Label": "m"},
                                  {"Namespace": "AWS/RDS", "MetricName": ANY, "StartTime": ANY, "EndTime": ANY,
                                   "Period": 300, "Statistics": ["Average", "Maximum", "Minimum"], "Dimensions": ANY})
        s_rds.activate(), s_cw.activate()
        return {"rds": rds, "cloudwatch": cloudwatch}

    target = AwsTarget(endpoint_url="http://127.0.0.1:4566")
    with History(tmp_path / "history.db") as history:
        first = collect_aws(history, target, writer, hours=3, now=NOW, label="evening cluster",
                            clients=clients(NOW - timedelta(hours=3), 3))
        assert first.nodes == ["writer", "reader-1"]
        assert first.written == first.seen == 2 * 5 * 36  # two nodes, five metrics, 36 periods
        assert first.already_held == 0

        # An hour later, a window that overlaps two of the three hours already held.
        later = NOW + timedelta(hours=1)
        second = collect_aws(history, target, writer, hours=3, now=later,
                            clients=clients(later - timedelta(hours=3), 3))
        assert second.cluster_key == first.cluster_key
        assert second.written == 2 * 5 * 12  # only the new hour
        assert second.already_held == 2 * 5 * 24

        key = first.cluster_key
        assert [n["instance_class"] for n in history.nodes(key)] == ["db.r6i.16xlarge"] * 2
        assert history.nodes(key)[0]["vcpu"] == 64
        assert history.clusters()[0].label == "evening cluster"
        assert history.last_collection(key)["seen"] == 2 * 5 * 36
        assert len(history.samples(key, "CPUUtilization", "writer")) == 48
        for identifier in (writer, reader):
            assert identifier not in tmp_path.joinpath("history.db").read_bytes().decode("latin-1")


def test_history_cli_collects_status_and_envelope(tmp_path, capsys):
    """The three commands on a store seeded directly, so no cloud is involved."""
    from capacitylab.cli import main

    store = tmp_path / "history.db"
    with History(store) as history:
        key = history.cluster_key(IDENTIFIERS)
        history.note_cluster(key, label="evening cluster", cloud="aws", engine="mysql")
        history.note_node(key, "writer", instance_class="db.r6i.16xlarge", vcpu=64, memory_gib=512)
        history.add_samples(key, "writer", "CPUUtilization",
                            cpu_points(NOW - timedelta(days=14), 14 * 24,
                                       lambda ts: 30.0 + (40.0 if ts.hour == 19 else 0.0)), unit="Percent")
        history.record_collection(key, "aws:cloudwatch", NOW - timedelta(days=14), NOW, seen=4032, written=4032)

    assert main(["history", "status", "--store", str(store)]) == 0
    out = capsys.readouterr().out
    assert key in out and "evening cluster" in out and "db.r6i.16xlarge" in out
    assert "CPUUtilization on writer: 4032 samples" in out

    out_file = tmp_path / "envelope.yaml"
    assert main(["history", "envelope", key, "--store", str(store), "--top", "2", "--out", str(out_file)]) == 0
    out = capsys.readouterr().out
    assert "19:00" in out.split("busiest slots")[1]
    assert "84.0" in out  # (30 + 40) x 1.2
    assert out_file.exists() and "EV-ENV-CPUUTILIZATI" in out_file.read_text()

    assert main(["history", "envelope", key, "--metric", "Nope", "--store", str(store)]) == 1
