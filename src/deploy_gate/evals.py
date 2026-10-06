"""Run simulated rollouts whose right outcome is known, and score the gate.

Two numbers matter most. Bad releases must be stopped, with as little traffic
exposed as possible. And healthy releases must not be rolled back, including
when something else is failing around them.
"""
from __future__ import annotations

from .rollout import Plan, run_rollout
from .simulate import SimulatedService

# (name, tier, what the service does, right outcome, is the new version actually bad?)
SCENARIOS = [
    ("healthy-release", "medium", {}, "completed", False),
    ("error-regression", "medium", {"canary_error_rate": 0.08}, "rolled_back", True),
    ("latency-regression", "medium", {"canary_p99_ms": 1400.0}, "rolled_back", True),
    ("small-regression-high-risk", "critical", {"canary_error_rate": 0.03}, "rolled_back", True),
    ("fault-only-under-load", "high", {"canary_error_rate": 0.05, "bad_from_weight": 50}, "rolled_back", True),
    ("dependency-outage-both-versions", "medium",
     {"canary_error_rate": 0.06, "stable_error_rate": 0.06}, "held", False),
    ("too-little-traffic-to-judge", "high", {"requests_per_minute": 300}, "held", False),
    ("slightly-noisy-but-healthy", "medium", {"canary_error_rate": 0.004, "stable_error_rate": 0.003},
     "completed", False),
]


def _plan(policy: dict, tier: str) -> Plan:
    spec = policy["rollouts"][tier]
    return Plan(tier=tier, approval="none", steps=[dict(s) for s in spec["steps"]])


def run_scenario(policy: dict, tier: str, behaviour: dict):
    service = SimulatedService(**behaviour)
    result = run_rollout(_plan(policy, tier), service, service, policy["gate"])
    return service, result


def evaluate(policy: dict) -> dict:
    rows = []
    for name, tier, behaviour, expected, is_bad in SCENARIOS:
        service, result = run_scenario(policy, tier, behaviour)
        # The same release pushed straight to 100% behind the same gate.
        direct_service, direct = run_scenario(policy, "low", behaviour)
        rows.append({
            "scenario": name, "tier": tier, "expected": expected, "outcome": result.outcome,
            "correct": result.outcome == expected, "is_bad": is_bad,
            "peak_weight": result.peak_weight, "minutes": result.minutes,
            "failed_requests": service.excess_failed_requests(),
            "slow_requests": service.slow_canary_requests(policy["gate"]["max_p99_ms"]),
            "direct_failed_requests": direct_service.excess_failed_requests(),
            "direct_slow_requests": direct_service.slow_canary_requests(policy["gate"]["max_p99_ms"]),
            "direct_outcome": direct.outcome,
        })
    bad = [r for r in rows if r["is_bad"]]
    good = [r for r in rows if not r["is_bad"]]
    return {
        "rows": rows,
        "metrics": {
            "scenarios": len(rows),
            "correct_outcomes": sum(r["correct"] for r in rows),
            "bad_releases_stopped": f"{sum(r['outcome'] == 'rolled_back' for r in bad)} of {len(bad)}",
            "bad_releases_reaching_100": sum(r["peak_weight"] == 100 and r["outcome"] == "completed" for r in bad),
            "false_rollbacks": sum(r["outcome"] == "rolled_back" for r in good),
            "harmed_requests_staged": sum(r["failed_requests"] + r["slow_requests"] for r in bad),
            "harmed_requests_direct": sum(r["direct_failed_requests"] + r["direct_slow_requests"] for r in bad),
        },
    }


def render_eval(result: dict) -> str:
    out = ["| Scenario | Plan | Expected | Outcome | Peak traffic | Harmed requests | Same release sent straight to 100% |",
           "|---|---|---|---|---|---|---|"]
    for r in result["rows"]:
        harmed = r["failed_requests"] + r["slow_requests"]
        direct = r["direct_failed_requests"] + r["direct_slow_requests"]
        out.append(f"| {r['scenario']} | {r['tier']} | {r['expected']} | "
                   f"{'' if r['correct'] else 'WRONG: '}{r['outcome']} | {r['peak_weight']}% | "
                   f"{harmed if r['is_bad'] else '-'} | {direct if r['is_bad'] else '-'} |")
    out.append("")
    out += [f"- {name}: {value}" for name, value in result["metrics"].items()]
    return "\n".join(out) + "\n"
