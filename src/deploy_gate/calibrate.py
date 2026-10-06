"""Adjust the risk weights from what actually happened.

For each risk factor, compare how often changes carrying it failed (rolled
back, or caused an incident) with how often changes fail overall. A factor
that fails more than average gets a higher weight; one that fails less gets a
lower one.

The adjustment is deliberately cautious:

- The estimate is smoothed toward the overall rate, so one bad rollout cannot
  swing a factor that has little history.
- A weight moves at most `max_step` (10%) per calibration, and not at all if
  it is already within `deadband` of where the evidence points.
- A weight never leaves [`min_multiplier`, `max_multiplier`] (0.5x to 2x).
- Nothing changes until there is a minimum amount of history with both
  failures and successes in it.

The same history always produces the same weights. Nothing here uses a model.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Calibration:
    ready: bool
    reason: str = ""
    rollouts: int = 0
    failures: int = 0
    overall_failure_rate: float = 0.0
    rows: list = field(default_factory=list)          # one per factor
    multipliers: dict = field(default_factory=dict)   # the proposed set, including unchanged ones
    by_tier: list = field(default_factory=list)
    concordance: float | None = None                  # chance a failed change outscored a successful one


def calibrate(records: list, policy: dict) -> Calibration:
    learning = policy["learning"]
    current = dict(policy["risk"].get("multipliers", {}))
    judged = [r for r in records if r["outcome"] in learning["failure_outcomes"] + learning["success_outcomes"]]
    failed = [r for r in judged if r["outcome"] in learning["failure_outcomes"]]
    result = Calibration(ready=False, rollouts=len(judged), failures=len(failed), multipliers=current)

    result.by_tier = []
    for tier in ("low", "medium", "high", "critical"):
        in_tier = [r for r in judged if r["tier"] == tier]
        bad = sum(r in failed for r in in_tier)
        result.by_tier.append({"tier": tier, "rollouts": len(in_tier), "failures": bad,
                               "failure_rate": round(bad / len(in_tier), 3) if in_tier else None})
    succeeded = [r for r in judged if r not in failed]
    if failed and succeeded:
        wins = sum((f["score"] > s["score"]) + 0.5 * (f["score"] == s["score"]) for f in failed for s in succeeded)
        result.concordance = round(wins / (len(failed) * len(succeeded)), 3)

    if len(judged) < learning["min_history"]:
        result.reason = f"only {len(judged)} finished rollouts recorded; {learning['min_history']} are needed"
        return result
    if not failed or not succeeded:
        result.reason = "the history needs at least one failure and one success to compare"
        return result

    overall = len(failed) / len(judged)
    result.overall_failure_rate = round(overall, 4)
    strength = learning["prior_strength"]
    keys = sorted({f["key"] for r in judged for f in r["factors"]})
    for key in keys:
        with_factor = [r for r in judged if any(f["key"] == key for f in r["factors"])]
        bad = sum(r in failed for r in with_factor)
        # Smoothed failure rate: as if `strength` extra rollouts at the overall rate had been seen.
        smoothed = (bad + strength * overall) / (len(with_factor) + strength)
        lift = smoothed / overall
        target = min(learning["max_multiplier"], max(learning["min_multiplier"], lift))
        before = current.get(key, 1.0)
        gap = target - before
        if abs(gap) < learning["deadband"]:
            gap = 0.0                      # close enough: do not churn the policy over noise
        step = max(-learning["max_step"], min(learning["max_step"], gap))
        after = round(before + step, 3)
        result.rows.append({"factor": key, "rollouts": len(with_factor), "failures": bad,
                            "failure_rate": round(bad / len(with_factor), 3), "lift": round(lift, 2),
                            "multiplier_before": before, "multiplier_after": after})
        if after != 1.0:
            result.multipliers[key] = after
        else:
            result.multipliers.pop(key, None)
    result.ready = True
    return result


def save(c: Calibration, path) -> None:
    """Write the adjusted weights into a policy file, keeping anything else already in it."""
    import json
    from pathlib import Path
    target = Path(path)
    existing = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    existing.setdefault("risk", {})["multipliers"] = c.multipliers
    existing["calibrated_at_rollouts"] = c.rollouts
    target.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


def learn(records: list, policy: dict, path) -> str:
    """Recalibrate automatically, but only once enough new rollouts have finished.

    Calibrating after every single rollout would apply the 10% step again and
    again on nearly the same evidence, which defeats the point of the limit.
    """
    c = calibrate(records, policy)
    due = policy.get("calibrated_at_rollouts", 0) + policy["learning"]["recalibrate_every"]
    if not c.ready:
        return f"weights unchanged: {c.reason}"
    if c.rollouts < due:
        return f"weights unchanged: next adjustment after {due} finished rollouts (now {c.rollouts})"
    save(c, path)
    changed = [r for r in c.rows if r["multiplier_after"] != r["multiplier_before"]]
    moves = ", ".join(f"{r['factor']} x{r['multiplier_before']:g} -> x{r['multiplier_after']:g}" for r in changed)
    return f"weights adjusted in {path}: {moves or 'no factor moved'}"


def render(c: Calibration) -> str:
    out = [f"Finished rollouts: {c.rollouts} · failures: {c.failures}"
           + (f" · overall failure rate: {c.overall_failure_rate:.1%}" if c.ready else ""), ""]
    out += ["| Tier | Rollouts | Failures | Failure rate |", "|---|---|---|---|"]
    out += [f"| {t['tier']} | {t['rollouts']} | {t['failures']} | "
            f"{'-' if t['failure_rate'] is None else format(t['failure_rate'], '.0%')} |" for t in c.by_tier]
    if c.concordance is not None:
        out += ["", f"A failed change outscored a successful one {c.concordance:.0%} of the time "
                    "(50% would mean the score carries no information)."]
    out.append("")
    if not c.ready:
        out.append(f"No weights changed: {c.reason}.")
        return "\n".join(out) + "\n"
    out += ["| Factor | Rollouts | Failures | Failure rate | Lift over average | Weight before | Weight after |",
            "|---|---|---|---|---|---|---|"]
    for r in sorted(c.rows, key=lambda r: -r["lift"]):
        arrow = "" if r["multiplier_after"] == r["multiplier_before"] else \
            (" ▲" if r["multiplier_after"] > r["multiplier_before"] else " ▼")
        out.append(f"| {r['factor']} | {r['rollouts']} | {r['failures']} | {r['failure_rate']:.0%} | "
                   f"{r['lift']:.2f}x | x{r['multiplier_before']:g} | x{r['multiplier_after']:g}{arrow} |")
    return "\n".join(out) + "\n"
