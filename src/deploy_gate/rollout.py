"""Turn a risk tier into a rollout plan, and walk a rollout through it.

`run_rollout` is a small controller: set a traffic weight, watch the gate for
the bake time, then promote, wait, or roll back. It talks to the world through
two narrow interfaces, so the same loop drives a simulation or a real system.

    metrics.snapshot(version, window_minutes) -> Snapshot or None   version: "canary" | "stable"
    deployer.set_weight(percent) / .rollback() / .promote() / .tick()
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import gate as g


@dataclass
class Plan:
    tier: str
    approval: str                  # none | before_weight_50 | before_start
    steps: list                    # [{"weight": 10, "bake_minutes": 10}, ...]
    blocked: bool = False
    blocked_reason: str = ""
    warning: str = ""

    def total_bake_minutes(self) -> int:
        return sum(s["bake_minutes"] for s in self.steps)


def build_plan(assessment, policy: dict) -> Plan:
    spec = policy["rollouts"][assessment.tier]
    approval, warning = spec["approval"], ""
    irreversible = [k for k in assessment.kinds if k in policy.get("irreversible_kinds", [])]
    if irreversible:
        # Shifting traffic back does not undo a schema change, so automatic
        # rollback is not a safety net here. A person approves before it starts.
        approval = "before_start"
        warning = (f"Includes a {irreversible[0]}: rolling back traffic will not undo it. "
                   "Approval is required before the rollout starts, whatever the score.")
    return Plan(tier=assessment.tier, approval=approval, steps=[dict(s) for s in spec["steps"]],
                blocked=assessment.blocked, blocked_reason=assessment.blocked_reason, warning=warning)


def needs_approval(plan: Plan, index: int) -> bool:
    if plan.approval == "before_start":
        return index == 0
    if plan.approval == "before_weight_50":
        first_big = next((i for i, s in enumerate(plan.steps) if s["weight"] >= 50), None)
        return index == first_big
    return False


def to_argo_steps(plan: Plan) -> str:
    """The plan as the `steps` of an Argo Rollouts canary strategy.

    A pause with no duration waits for a person to promote. Gate checks are not
    included: pair these steps with your own AnalysisTemplate.
    """
    lines = ["strategy:", "  canary:", "    steps:"]
    for index, step in enumerate(plan.steps):
        if needs_approval(plan, index):
            lines.append("      - pause: {}          # wait for a person to promote")
        lines.append(f"      - setWeight: {step['weight']}")
        lines.append(f"      - pause: {{duration: {step['bake_minutes']}m}}")
    return "\n".join(lines) + "\n"


@dataclass
class RolloutResult:
    outcome: str                   # completed | rolled_back | held | awaiting_approval | blocked
    reason: str = ""
    peak_weight: int = 0
    minutes: int = 0
    checks: int = 0
    timeline: list = field(default_factory=list)


def run_rollout(plan: Plan, metrics, deployer, gate: dict, approve=lambda step: True) -> RolloutResult:
    result = RolloutResult(outcome="completed")

    def log(event: str, detail: str = ""):
        result.timeline.append({"minute": result.minutes, "event": event, "detail": detail})

    if plan.blocked:
        result.outcome, result.reason = "blocked", plan.blocked_reason
        log("blocked", plan.blocked_reason)
        return result

    for index, step in enumerate(plan.steps):
        if needs_approval(plan, index) and not approve(step):
            result.outcome = "awaiting_approval"
            result.reason = f"a person must approve moving to {step['weight']}%"
            log("awaiting_approval", result.reason)
            return result

        try:
            deployer.set_weight(step["weight"])
        except RuntimeError as error:
            # The platform did not do what was asked. Stop and hand over; do not guess.
            result.outcome, result.reason = "held", f"could not set {step['weight']}%: {error}"
            log("held", result.reason)
            return result
        result.peak_weight = max(result.peak_weight, step["weight"])
        log("set_weight", f"{step['weight']}%")

        budget = step["bake_minutes"] * (1 + gate["max_bake_extensions"])
        baked, last = 0, None
        while baked < budget:
            deployer.tick()
            baked += 1
            result.minutes += 1
            if baked % gate["check_every_minutes"]:
                continue
            try:
                canary = metrics.snapshot("canary", gate["window_minutes"])
                stable = metrics.snapshot("stable", gate["window_minutes"]) if step["weight"] < 100 else None
                last = g.evaluate(canary, stable, gate)
            except Exception as error:
                # Blind is not healthy: without metrics the gate can neither promote nor roll back.
                last = g.Decision(g.INSUFFICIENT, [f"metrics unavailable: {type(error).__name__}"])
            result.checks += 1
            if last.verdict == g.BREACH:
                deployer.rollback()
                result.outcome, result.reason = "rolled_back", "; ".join(last.reasons)
                log("rollback", result.reason)
                return result
            if baked >= step["bake_minutes"] and last.verdict == g.PASS:
                break
            if baked == step["bake_minutes"]:
                log("bake_extended", f"{last.verdict}: {'; '.join(last.reasons)}")

        if last is None or last.verdict != g.PASS:
            verdict = last.verdict if last else g.INSUFFICIENT
            result.outcome = "held"
            result.reason = f"{verdict} at {step['weight']}%: " + ("; ".join(last.reasons) if last else "no checks ran")
            log("held", result.reason)
            return result
        log("step_passed", f"{step['weight']}% healthy for {baked} min")

    deployer.promote()
    log("promoted", "new version is serving all traffic")
    return result
