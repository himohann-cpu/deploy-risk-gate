"""Command line entry point.

    deploy-gate assess --change samples/changes/03-migration.json --services samples/services.json
    deploy-gate assess --base origin/main --service checkout-api --depmap-url http://localhost:8080
    deploy-gate plan   --change samples/changes/03-migration.json --argo
    deploy-gate apply-plan --change CHANGE.json --rollout k8s/rollout.yaml          # show the diff
    deploy-gate apply-plan --change CHANGE.json --rollout k8s/rollout.yaml --write  # rewrite the file
    deploy-gate run --change CHANGE.json --deployer argo --name checkout-api --namespace shop \
                    --prometheus http://prometheus.istio-system:9090 --canary-revision v2 --stable-revision v1
    deploy-gate record --change CHANGE.json --outcome rolled_back --history deploy-history.jsonl
    deploy-gate calibrate --history deploy-history.jsonl --write learned-policy.json
    deploy-gate simulate error-regression
    deploy-gate eval
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import evals, history
from .calibrate import calibrate, learn
from .calibrate import save as save_calibration
from .calibrate import render as render_calibration
from .argo import ManifestError, apply_plan
from .argo import diff as manifest_diff
from .deployers import ArgoRolloutsDeployer, CommandRunner, IstioVirtualServiceDeployer
from .prommetrics import PrometheusMetrics
from .blast import upstream_from_api, upstream_from_file
from .change import Change
from .llm import explain, make_client
from .policy import load_policy
from .render import render_assessment, render_rollout
from .risk import assess
from .rollout import build_plan, run_rollout, to_argo_steps

ROOT = Path(__file__).resolve().parents[2]


def _assess(args, policy):
    if args.change:
        change = Change.from_file(args.change)
    elif args.base and args.service:
        change = Change.from_git(args.service, args.base, args.head)
    else:
        raise SystemExit("Give --change FILE, or --base REF with --service NAME.")
    if args.depmap_url:
        upstream = upstream_from_api(args.depmap_url, change.service)
    elif args.services:
        upstream = upstream_from_file(args.services, change.service)
    else:
        upstream = None
    when = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    failures = args.incidents
    if getattr(args, "history", None):
        failures += history.recent_failures(history.load(args.history), change.service, when, policy)
    assessment = assess(change, policy, upstream, failures, when)
    client = make_client(args.provider)
    model = explain(assessment, change, client) if client else None
    return change, assessment, build_plan(assessment, policy), model


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="deploy-gate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--policy", action="append", default=[],
                        help="JSON file whose keys override the built-in policy; repeat to layer several, "
                             "e.g. your own file then the learned weights")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("assess", "plan", "apply-plan", "run", "record"):
        p = sub.add_parser(name)
        p.add_argument("--change", help="JSON description of the change")
        p.add_argument("--base", help="git ref to diff against, e.g. origin/main")
        p.add_argument("--head", default="HEAD")
        p.add_argument("--service", help="service the change deploys (with --base)")
        p.add_argument("--services", help="services.json dependency map")
        p.add_argument("--depmap-url", help="base URL of the Istio dependency mapper API")
        p.add_argument("--incidents", type=int, default=0, help="incidents on this service in the last 30 days")
        p.add_argument("--history", help="rollout history file; recent failures on the service raise its risk, "
                                         "and `run` and `record` append to it")
        p.add_argument("--at", help="planned deploy time, ISO-8601 (default: now)")
        p.add_argument("--provider", choices=["none", "gemini", "claude"], default=None)
        p.add_argument("--argo", action="store_true", help="include Argo Rollouts canary steps")
        p.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
        p.add_argument("--fail-on", choices=["medium", "high", "critical"],
                       help="exit 1 if the tier is this or higher (a freeze always exits 1)")

        if name == "apply-plan":
            p.add_argument("--rollout", required=True, help="path to a manifest containing an Argo Rollout")
            p.add_argument("--name", help="which Rollout, if the file has several")
            p.add_argument("--gated", action="store_true",
                           help="end each stage in an indefinite pause, for `deploy-gate run` to promote")
            p.add_argument("--write", action="store_true", help="rewrite the file; without this, only show the diff")
            p.add_argument("--github-output", action="store_true",
                           help="append tier and score to $GITHUB_OUTPUT for later pipeline steps")
        if name in ("record", "run"):
            p.add_argument("--learn", metavar="FILE",
                           help="after recording, adjust the weights in this policy file once enough new "
                                "rollouts have finished (also pass it with --policy so it is read)")
        if name == "record":
            p.add_argument("--outcome", required=True, choices=history.OUTCOMES,
                           help="what happened; use `incident` when a completed change later caused one")
            p.add_argument("--note", default="")
        if name == "run":
            p.add_argument("--deployer", choices=["argo", "istio"], required=True)
            p.add_argument("--name", required=True, help="Rollout name (argo) or VirtualService name (istio)")
            p.add_argument("--namespace", required=True)
            p.add_argument("--prometheus", default=os.environ.get("PROMETHEUS_URL"))
            p.add_argument("--canary-revision", required=True)
            p.add_argument("--stable-revision", required=True)
            p.add_argument("--execute", action="store_true",
                           help="really run kubectl; without this, commands are printed and nothing changes")
            p.add_argument("--approve", action="store_true", help="pre-approve the steps that need a person")

    p = sub.add_parser("simulate", help="run one simulated rollout and print its timeline")
    p.add_argument("scenario", choices=[s[0] for s in evals.SCENARIOS])
    sub.add_parser("eval", help="run every simulated rollout and score the gate")
    p = sub.add_parser("calibrate", help="adjust the risk weights from the rollout history")
    p.add_argument("--history", required=True)
    p.add_argument("--write", metavar="FILE", help="save the adjusted weights to this policy file; "
                                                   "without it, only show what would change")

    args = parser.parse_args(argv)
    policy = load_policy(args.policy)

    if args.command == "record":
        if not args.history:
            raise SystemExit("Give --history FILE to record into.")
        _, assessment, _, _ = _assess(args, policy)
        saved = history.append_record(args.history, assessment, args.outcome, note=args.note)
        print(f"Recorded {saved['outcome']} for {saved['service']} {saved['ref']} "
              f"(risk {saved['tier']}, {saved['score']}/100) in {args.history}")
        if args.learn:
            print(learn(history.load(args.history), policy, args.learn))
        return 0

    if args.command == "calibrate":
        result = calibrate(history.load(args.history), policy)
        sys.stdout.write(render_calibration(result))
        if args.write and result.ready:
            save_calibration(result, args.write)
            print(f"\nWrote {len(result.multipliers)} adjusted weight(s) to {args.write}. "
                  f"Use it with: deploy-gate --policy {args.write} ...")
        elif args.write:
            print(f"\n{args.write} was not changed.")
        return 0

    if args.command == "apply-plan":
        _, assessment, plan, _ = _assess(args, policy)
        path = Path(args.rollout)
        before = path.read_text(encoding="utf-8")
        try:
            after, summary = apply_plan(before, plan, assessment, gated=args.gated, name=args.name)
        except ManifestError as error:
            raise SystemExit(f"{path}: {error}")
        if args.github_output and os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
                out.write(f"tier={assessment.tier}\nscore={assessment.score}\n"
                          f"blocked={'true' if assessment.blocked else 'false'}\n")
        print(f"# {summary['rollout']}: risk {assessment.tier} ({assessment.score}/100); traffic steps "
              f"{summary['weights_before']} -> {summary['weights_after']}; "
              f"kept {len(summary['kept_steps'])} other step(s) ({', '.join(summary['kept_step_kinds']) or 'none'})")
        if plan.warning:
            print(f"# {plan.warning}")
        if assessment.blocked:
            print(f"# BLOCKED: {assessment.blocked_reason}. The file was not changed.")
            return 1
        if args.write:
            path.write_text(after, encoding="utf-8")
            print(f"# wrote {path}")
        else:
            sys.stdout.write(manifest_diff(before, after, str(path)) or "# no change needed\n")
        return 0

    if args.command == "run":
        if not args.prometheus:
            raise SystemExit("Give --prometheus or set PROMETHEUS_URL.")
        _, assessment, plan, _ = _assess(args, policy)
        runner = CommandRunner(execute=args.execute)
        deployer_class = ArgoRolloutsDeployer if args.deployer == "argo" else IstioVirtualServiceDeployer
        deployer = deployer_class(args.name, args.namespace, runner)
        metrics = PrometheusMetrics(args.prometheus, assessment.service, args.canary_revision, args.stable_revision,
                                    token=os.environ.get("PROMETHEUS_TOKEN"))
        print(f"Risk {assessment.tier} ({assessment.score}/100). "
              f"{'Executing' if args.execute else 'Dry run: printing'} kubectl commands. One gate check per minute.")
        result = run_rollout(plan, metrics, deployer, policy["gate"], approve=lambda step: args.approve)
        sys.stdout.write(render_rollout(args.name, plan, result))
        if args.history and args.execute and result.outcome in history.OUTCOMES:
            history.append_record(args.history, assessment, result.outcome, note=result.reason[:200])
            print(f"Recorded {result.outcome} in {args.history}")
            if args.learn:
                print(learn(history.load(args.history), policy, args.learn))
        return 0 if result.outcome == "completed" else 1

    if args.command in ("assess", "plan"):
        _, assessment, plan, model = _assess(args, policy)
        if args.json:
            print(json.dumps({"assessment": assessment.to_dict(), "plan": plan.__dict__, "model": model}, indent=2))
        elif args.command == "plan" and args.argo:
            sys.stdout.write(to_argo_steps(plan))
        else:
            sys.stdout.write(render_assessment(assessment, plan, model, argo=args.argo))
        order = ["low", "medium", "high", "critical"]
        if assessment.blocked or (args.fail_on and order.index(assessment.tier) >= order.index(args.fail_on)):
            return 1
    elif args.command == "simulate":
        name, tier, behaviour, _, _ = next(s for s in evals.SCENARIOS if s[0] == args.scenario)
        service, result = evals.run_scenario(policy, tier, behaviour)
        sys.stdout.write(render_rollout(name, evals._plan(policy, tier), result, service))
    elif args.command == "eval":
        result = evals.evaluate(policy)
        sys.stdout.write(evals.render_eval(result))
        m = result["metrics"]
        if m["correct_outcomes"] != m["scenarios"] or m["false_rollbacks"] or m["bad_releases_reaching_100"]:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
