"""Fitting a plan into an existing Rollout, and the kubectl commands the deployers issue."""
import json
from pathlib import Path

import pytest

pytest.importorskip("ruamel.yaml")

from deploy_gate.argo import ManifestError, apply_plan, new_steps  # noqa: E402
from deploy_gate.cli import main  # noqa: E402
from deploy_gate.deployers import ArgoRolloutsDeployer, CommandRunner, IstioVirtualServiceDeployer  # noqa: E402
from deploy_gate.policy import load_policy  # noqa: E402
from deploy_gate.rollout import Plan, run_rollout  # noqa: E402
from deploy_gate.simulate import SimulatedService  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_policy()
MANIFEST = (ROOT / "samples" / "k8s" / "rollout.yaml").read_text()
SMOKE = {"analysis": {"templates": [{"templateName": "smoke-test"}]}}


def plan(tier: str) -> Plan:
    spec = POLICY["rollouts"][tier]
    return Plan(tier, spec["approval"], [dict(s) for s in spec["steps"]])


def steps_of(text: str) -> list:
    from ruamel.yaml import YAML
    docs = list(YAML(typ="safe").load_all(text))
    return docs[0]["spec"]["strategy"]["canary"]["steps"]


# ------------------------------------------------------------- the manifest

def test_only_the_traffic_steps_change():
    after, summary = apply_plan(MANIFEST, plan("medium"))
    assert summary["weights_before"] == [20, 60, 100] and summary["weights_after"] == [10, 50, 100]
    before_lines, after_lines = MANIFEST.splitlines(), after.splitlines()
    start = before_lines.index("      steps:")
    end_before, end_after = before_lines.index("  template:"), after_lines.index("  template:")
    assert before_lines[:start + 1] == after_lines[:start + 1]            # everything above the steps
    assert before_lines[end_before:] == after_lines[end_after:]           # pod template and the Service
    assert "# bumped by CI" in after and "# Runs for the whole rollout" in after
    assert summary["background_analysis_untouched"] is True


def test_existing_analysis_steps_are_kept_after_every_stage():
    steps = steps_of(apply_plan(MANIFEST, plan("medium"))[0])
    assert steps == [
        {"setWeight": 10}, {"pause": {"duration": "10m"}}, SMOKE,
        {"setWeight": 50}, {"pause": {"duration": "10m"}}, SMOKE,
        {"setWeight": 100}, {"pause": {"duration": "10m"}}, SMOKE,
    ]


def test_approval_becomes_an_indefinite_pause_in_the_right_place():
    high = steps_of(apply_plan(MANIFEST, plan("high"))[0])
    assert high[high.index({"setWeight": 50}) - 1] == {"pause": {}}
    assert high.count({"pause": {}}) == 1
    assert steps_of(apply_plan(MANIFEST, plan("critical"))[0])[0] == {"pause": {}}


def test_gated_mode_pauses_indefinitely_and_leaves_approval_to_the_controller():
    after, summary = apply_plan(MANIFEST, plan("high"), gated=True)
    steps = steps_of(after)
    assert [s for s in steps if "pause" in s] == [{"pause": {}}] * 4      # one per stage, none extra
    assert summary["approval_pauses"] == 0


def test_applying_the_same_plan_twice_changes_nothing_more():
    once, _ = apply_plan(MANIFEST, plan("high"))
    twice, _ = apply_plan(once, plan("high"))
    assert once == twice


def test_steps_before_the_first_weight_stay_first_and_others_are_not_duplicated():
    old = [{"setCanaryScale": {"replicas": 1}}, {"setWeight": 20}, {"pause": {}}, SMOKE,
           {"setWeight": 60}, SMOKE, {"experiment": {"templates": []}}]
    steps = new_steps(old, plan("low"))
    assert steps == [{"setCanaryScale": {"replicas": 1}}, {"setWeight": 100}, {"pause": {"duration": "10m"}},
                     SMOKE, {"experiment": {"templates": []}}]


def test_refuses_what_it_cannot_safely_edit():
    with pytest.raises(ManifestError, match="no Rollout"):
        apply_plan("apiVersion: v1\nkind: Service\nmetadata: {name: x}\n", plan("low"))
    blue_green = MANIFEST.replace("    canary:", "    blueGreen:")
    with pytest.raises(ManifestError, match="canary strategy"):
        apply_plan(blue_green, plan("low"))
    with pytest.raises(ManifestError, match="choose one"):
        apply_plan(MANIFEST + "---\n" + MANIFEST.split("---")[0].replace("name: checkout-api\n", "name: other\n", 1),
                   plan("low"))
    assert apply_plan(MANIFEST, plan("low"), name="checkout-api")[1]["rollout"] == "checkout-api"


def test_cli_shows_a_diff_by_default_and_writes_only_when_asked(tmp_path, capsys, monkeypatch):
    path = tmp_path / "rollout.yaml"
    path.write_text(MANIFEST)
    common = ["apply-plan", "--change", str(ROOT / "samples/changes/04-auth-refactor.json"),
              "--services", str(ROOT / "samples/services.json"), "--at", "2026-10-06T10:00:00Z", "--rollout", str(path)]
    assert main(common) == 0
    out = capsys.readouterr().out
    assert "+        - setWeight: 1" in out and "risk high (62/100)" in out and path.read_text() == MANIFEST

    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    assert main(common + ["--write", "--github-output"]) == 0
    written = path.read_text()
    assert "deploy-gate.dev/tier: high" in written and "deploy-gate.dev/score: '62'" in written
    assert output_file.read_text() == "tier=high\nscore=62\nblocked=false\n"


def test_cli_leaves_the_file_alone_during_a_freeze(tmp_path, capsys):
    path = tmp_path / "rollout.yaml"
    path.write_text(MANIFEST)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"deploy_window": {"freezes": [{"from": "2026-10-01", "to": "2026-10-31",
                                                                  "reason": "audit"}]}}))
    code = main(["--policy", str(policy), "apply-plan", "--change", str(ROOT / "samples/changes/05-config-timeout.json"),
                 "--at", "2026-10-06T10:00:00Z", "--rollout", str(path), "--write"])
    assert code == 1 and path.read_text() == MANIFEST and "BLOCKED" in capsys.readouterr().out


# ---------------------------------------------------------------- deployers

def test_dry_run_records_commands_and_runs_nothing():
    runner = CommandRunner(execute=False, echo=None)
    deployer = IstioVirtualServiceDeployer("checkout-api", "shop", runner, sleep=lambda s: None)
    deployer.set_weight(10)
    assert runner.commands == [["kubectl", "patch", "virtualservice", "checkout-api", "-n", "shop", "--type=json", "-p",
                                '[{"op":"replace","path":"/spec/http/0/route/0/weight","value":90},'
                                '{"op":"replace","path":"/spec/http/0/route/1/weight","value":10}]']]


def test_istio_deployer_sets_weights_through_a_rollout_and_zeroes_them_on_rollback():
    runner = CommandRunner(echo=None)
    service = SimulatedService(canary_error_rate=0.08)

    class Wired(IstioVirtualServiceDeployer):       # mirror each weight into the simulation
        def set_weight(self, percent):
            super().set_weight(percent)
            service.set_weight(percent)

        def tick(self):
            service.tick()

    result = run_rollout(plan("medium"), service, Wired("checkout-api", "shop", runner), POLICY["gate"])
    assert result.outcome == "rolled_back"
    weights = [json.loads(c[-1])[1]["value"] for c in runner.commands]
    assert weights == [10, 0]                        # canary to 10%, then back to nothing


def test_argo_deployer_promotes_between_stages_and_aborts_on_a_breach():
    runner = CommandRunner(echo=None)
    healthy = SimulatedService()

    class Wired(ArgoRolloutsDeployer):
        def __init__(self, service, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.service = service

        def set_weight(self, percent):
            super().set_weight(percent)
            self.service.set_weight(percent)

        def tick(self):
            self.service.tick()

    ungated = Plan("medium", "none", plan("medium").steps)
    assert run_rollout(ungated, healthy, Wired(healthy, "checkout-api", "shop", runner), POLICY["gate"]).outcome == "completed"
    verbs = [c[3] for c in runner.commands]
    assert verbs == ["promote", "promote", "promote"]   # 10 -> 50, 50 -> 100, then past the final pause
    assert runner.commands[0] == ["kubectl", "argo", "rollouts", "promote", "checkout-api", "-n", "shop"]

    runner2, bad = CommandRunner(echo=None), SimulatedService(canary_error_rate=0.08)
    run_rollout(ungated, bad, Wired(bad, "checkout-api", "shop", runner2), POLICY["gate"])
    assert [c[3] for c in runner2.commands] == ["abort"]


def test_argo_deployer_waits_until_argo_reports_the_weight_and_holds_if_it_never_does():
    class FakeKubectl(CommandRunner):
        def __init__(self, weights):
            super().__init__(execute=True, echo=None)
            self.weights = iter(weights)

        def run(self, argv):
            self.commands.append(argv)
            if argv[1] == "get":
                return json.dumps({"status": {"canary": {"weights": {"canary": {"weight": next(self.weights, 10)}}}}})
            return ""

    slept = []
    deployer = ArgoRolloutsDeployer("checkout-api", "shop", FakeKubectl([0, 0, 10]), sleep=slept.append)
    deployer.set_weight(10)
    assert slept == [10, 10] and deployer.weight == 10      # polled until Argo caught up

    stuck = ArgoRolloutsDeployer("checkout-api", "shop", FakeKubectl([]), sleep=lambda s: None)
    stuck._started = True
    result = run_rollout(Plan("medium", "none", [{"weight": 50, "bake_minutes": 1}]), None, stuck, POLICY["gate"])
    assert result.outcome == "held" and "did not reach 50%" in result.reason


def test_losing_metrics_mid_rollout_holds_at_the_current_weight():
    class Blind:
        def snapshot(self, version, window):
            raise ConnectionError("prometheus unreachable")

    runner = CommandRunner(echo=None)
    deployer = IstioVirtualServiceDeployer("checkout-api", "shop", runner, sleep=lambda s: None)
    result = run_rollout(Plan("medium", "none", [{"weight": 10, "bake_minutes": 2}, {"weight": 100, "bake_minutes": 2}]),
                         Blind(), deployer, POLICY["gate"])
    assert result.outcome == "held" and "metrics unavailable" in result.reason
    assert deployer.weight == 10 and len(runner.commands) == 1     # neither promoted nor rolled back


def test_resource_names_are_validated_before_reaching_a_command_line():
    with pytest.raises(ValueError):
        IstioVirtualServiceDeployer("x; rm -rf /", "shop")
    with pytest.raises(ValueError):
        ArgoRolloutsDeployer("checkout-api", "--all-namespaces")
