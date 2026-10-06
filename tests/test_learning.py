"""Risk weights that adjust to what actually happened."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deploy_gate import history
from deploy_gate.calibrate import calibrate, learn
from deploy_gate.change import Change
from deploy_gate.cli import main
from deploy_gate.policy import load_policy
from deploy_gate.risk import assess

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_policy()
TUESDAY = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
SAMPLE_HISTORY = ROOT / "samples" / "history.jsonl"


def record(outcome, keys, score=30, tier="medium", service="svc", ref=None, at=TUESDAY):
    return {"at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "service": service, "ref": ref or "", "score": score,
            "tier": tier, "outcome": outcome, "factors": [{"key": k, "points": 5} for k in keys]}


def rows(result):
    return {r["factor"]: r for r in result.rows}


def mixed_history():
    """Config changes fail 4 times in 10; code changes fail once in 20."""
    return ([record("rolled_back", ["change_kind:config"], score=40)] * 4
            + [record("completed", ["change_kind:config"], score=40)] * 6
            + [record("incident", ["change_kind:code"], score=10)]
            + [record("completed", ["change_kind:code"], score=10)] * 19)


# ----------------------------------------------------------------- calibration

def test_factor_that_fails_more_than_average_gains_weight_and_one_that_fails_less_loses_it():
    result = calibrate(mixed_history(), POLICY)
    assert result.ready and result.overall_failure_rate == round(5 / 30, 4)
    assert rows(result)["change_kind:config"]["multiplier_after"] == 1.1
    assert rows(result)["change_kind:code"]["multiplier_after"] == 0.9
    assert result.multipliers == {"change_kind:config": 1.1, "change_kind:code": 0.9}


def test_a_weight_moves_at_most_ten_percent_per_calibration_and_stays_within_bounds():
    policy, seen = load_policy(), []
    worst = ([record("rolled_back", ["no_tests"])] * 30 + [record("completed", ["size"])] * 70)
    for _ in range(25):
        result = calibrate(worst, policy)
        policy["risk"]["multipliers"] = result.multipliers
        seen.append(result.multipliers["no_tests"])
    assert seen[0] == 1.1 and seen[1] == 1.2                      # one step at a time
    assert max(seen) == 2.0 and seen[-1] == 2.0                   # never past the ceiling
    assert min(calibrate(worst, policy).multipliers["size"], 1) == 0.5   # nor below the floor


def test_one_bad_rollout_on_a_rare_factor_cannot_swing_it_far():
    lone = [record("rolled_back", ["sensitive_paths"])] + [record("completed", ["size"])] * 19 \
        + [record("rolled_back", ["size"])]
    row = rows(calibrate(lone, POLICY))["sensitive_paths"]
    assert row["failure_rate"] == 1.0                             # 1 of 1 failed...
    assert row["lift"] < 2.0 and row["multiplier_after"] == 1.1   # ...but it is smoothed, and stepped


def test_weights_already_close_to_the_evidence_are_left_alone():
    even = ([record("rolled_back", ["a", "b"])] * 2 + [record("completed", ["a", "b"])] * 18)
    result = calibrate(even, POLICY)
    assert all(r["multiplier_after"] == 1.0 for r in result.rows) and result.multipliers == {}


def test_nothing_changes_without_enough_history_or_without_both_outcomes():
    few = calibrate([record("rolled_back", ["a"])] * 3 + [record("completed", ["a"])] * 3, POLICY)
    assert not few.ready and "10 are needed" in few.reason
    all_good = calibrate([record("completed", ["a"])] * 30, POLICY)
    assert not all_good.ready and "at least one failure" in all_good.reason
    assert all_good.multipliers == {}


def test_held_rollouts_are_not_counted_as_success_or_failure():
    base = mixed_history()
    with_held = base + [record("held", ["change_kind:config"])] * 50
    assert calibrate(with_held, POLICY).rows == calibrate(base, POLICY).rows


def test_same_history_always_gives_the_same_weights():
    records = history.load(SAMPLE_HISTORY)
    first, second = calibrate(records, POLICY), calibrate(list(reversed(records)), POLICY)
    assert first.multipliers == second.multipliers and first.ready


def test_report_shows_whether_the_score_predicts_failure():
    result = calibrate(mixed_history(), POLICY)
    assert result.concordance > 0.7                                # failed changes mostly scored higher
    medium = next(t for t in result.by_tier if t["tier"] == "medium")
    assert medium == {"tier": "medium", "rollouts": 30, "failures": 5, "failure_rate": 0.167}


# -------------------------------------------------------- weights in the score

def test_learned_weights_scale_the_points_and_say_so():
    policy = load_policy()
    policy["risk"]["multipliers"] = {"change_kind:config": 1.5, "blast_radius": 0.5}
    change = Change.from_file(ROOT / "samples/changes/05-config-timeout.json")
    a = assess(change, policy, upstream=["web-frontend"], when=TUESDAY)
    points = {f.name: f.points for f in a.factors}
    assert points == {"change_kind": 22, "blast_radius": 2} and a.score == 24   # was 15 + 5 = 20
    assert "weight x1.5, learned from past rollouts" in a.factors[0].detail


def test_learned_weights_can_move_a_change_into_a_stricter_tier():
    change = Change.from_file(ROOT / "samples/changes/05-config-timeout.json")
    assert assess(change, POLICY, upstream=["web-frontend"], when=TUESDAY).tier == "low"
    policy = load_policy()
    policy["risk"]["multipliers"] = {"change_kind:config": 2.0}
    assert assess(change, policy, upstream=["web-frontend"], when=TUESDAY).tier == "medium"


# ------------------------------------------------------------------- history

def test_a_rollback_raises_the_risk_of_the_next_change_to_that_service(tmp_path):
    path = tmp_path / "h.jsonl"
    change = Change.from_file(ROOT / "samples/changes/02-small-fix-with-test.json")
    before = assess(change, POLICY, upstream=[], when=TUESDAY)
    history.append_record(path, before, "rolled_back", when=TUESDAY - timedelta(days=3))
    failures = history.recent_failures(history.load(path), change.service, TUESDAY, POLICY)
    after = assess(change, POLICY, upstream=[], recent_incidents=failures, when=TUESDAY)
    assert failures == 1 and after.score == before.score + 5
    assert history.recent_failures(history.load(path), "another-service", TUESDAY, POLICY) == 0
    assert history.recent_failures(history.load(path), change.service, TUESDAY + timedelta(days=40), POLICY) == 0


def test_later_record_for_the_same_change_replaces_the_earlier_one(tmp_path):
    path = tmp_path / "h.jsonl"
    change = Change.from_file(ROOT / "samples/changes/05-config-timeout.json")
    a = assess(change, POLICY, upstream=[], when=TUESDAY)
    history.append_record(path, a, "completed", when=TUESDAY)
    history.append_record(path, a, "incident", when=TUESDAY + timedelta(hours=6), note="timeouts at peak")
    records = history.load(path)
    assert len(records) == 1 and records[0]["outcome"] == "incident"
    assert len(path.read_text().splitlines()) == 2                 # the file itself is append-only


# ----------------------------------------------------------------------- cli

def test_cli_record_then_calibrate_then_use_the_learned_weights(tmp_path, capsys):
    hist = tmp_path / "h.jsonl"
    hist.write_text(SAMPLE_HISTORY.read_text())
    learned = tmp_path / "learned.json"
    config = str(ROOT / "samples/changes/05-config-timeout.json")
    services = ["--services", str(ROOT / "samples/services.json")]

    assert main(["record", "--change", config, *services, "--history", str(hist), "--outcome", "rolled_back"]) == 0
    assert main(["calibrate", "--history", str(hist)]) == 0
    assert not learned.exists() and "change_kind:config" in capsys.readouterr().out   # shown, not written

    assert main(["calibrate", "--history", str(hist), "--write", str(learned)]) == 0
    saved = json.loads(learned.read_text())
    assert saved["risk"]["multipliers"]["change_kind:config"] == 1.1
    assert saved["risk"]["multipliers"]["change_kind:dependency"] == 0.9

    capsys.readouterr()
    main(["--policy", str(learned), "assess", "--change", config, *services, "--at", "2026-10-06T10:00:00Z"])
    assert "weight x1.1, learned from past rollouts" in capsys.readouterr().out


def test_automatic_learning_waits_for_enough_new_rollouts(tmp_path):
    learned = tmp_path / "learned.json"
    records = mixed_history()                                       # 30 finished rollouts
    assert "weights adjusted" in learn(records, load_policy(), learned)
    after_first = load_policy([learned])
    assert after_first["calibrated_at_rollouts"] == 30
    assert "next adjustment after 40" in learn(records + [record("completed", ["x"])] * 5, after_first, learned)
    message = learn(records + [record("rolled_back", ["change_kind:config"])] * 10, after_first, learned)
    assert "change_kind:config x1.1 -> x1.2" in message


def test_policy_files_layer_in_order(tmp_path):
    mine, learned = tmp_path / "mine.json", tmp_path / "learned.json"
    mine.write_text(json.dumps({"risk": {"no_tests_points": 20}, "gate": {"max_error_rate": 0.005}}))
    learned.write_text(json.dumps({"risk": {"multipliers": {"no_tests": 1.3}}}))
    policy = load_policy([mine, learned])
    assert policy["risk"]["no_tests_points"] == 20 and policy["risk"]["multipliers"] == {"no_tests": 1.3}
    assert policy["gate"]["max_error_rate"] == 0.005 and policy["gate"]["min_requests"] == 200
