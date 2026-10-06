"""Decide whether a canary is healthy enough to take more traffic.

The canary is judged against the stable version running beside it, not only
against a fixed threshold. That matters when a dependency is down: both
versions fail together, and rolling back the canary would fix nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

PASS = "pass"
BREACH = "breach"                      # roll back
INSUFFICIENT = "insufficient_data"     # too few requests to judge: wait, never promote
BASELINE_UNHEALTHY = "baseline_unhealthy"   # stable version is failing too: wait, do not blame the canary
INCONCLUSIVE = "inconclusive"          # over the SLO, but not clearly worse than stable: wait


@dataclass
class Snapshot:
    requests: int
    errors: int = 0
    p99_ms: float | None = None

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 0.0


@dataclass
class Decision:
    verdict: str
    reasons: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def z_two_proportions(canary: Snapshot, baseline: Snapshot) -> float:
    """How many standard errors the canary's error rate sits above the baseline's."""
    n1, n2 = canary.requests, baseline.requests
    pooled = (canary.errors + baseline.errors) / (n1 + n2)
    spread = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    return (canary.error_rate - baseline.error_rate) / spread if spread else 0.0


def z_against_limit(canary: Snapshot, limit: float) -> float:
    """How many standard errors the canary's error rate sits above a fixed limit."""
    spread = math.sqrt(limit * (1 - limit) / canary.requests)
    return (canary.error_rate - limit) / spread if spread else 0.0


def evaluate(canary: Snapshot, baseline: Snapshot | None, gate: dict) -> Decision:
    """`baseline` is None when no stable version is serving traffic (the 100% step)."""
    has_baseline = baseline is not None and baseline.requests >= gate["min_requests"]
    stats = {"canary_requests": canary.requests, "canary_error_rate": round(canary.error_rate, 5),
             "canary_p99_ms": canary.p99_ms,
             "baseline_error_rate": round(baseline.error_rate, 5) if has_baseline else None,
             "baseline_p99_ms": baseline.p99_ms if has_baseline else None}

    if canary.requests < gate["min_requests"]:
        return Decision(INSUFFICIENT, [f"only {canary.requests} canary requests in the window; "
                                       f"{gate['min_requests']} are needed to judge"], stats)

    breaches, holds = [], []

    # ---- errors
    limit = gate["max_error_rate"]
    if canary.error_rate > limit:
        if has_baseline and baseline.error_rate > limit:
            worse = canary.error_rate > baseline.error_rate * gate["error_ratio_over_baseline"] + gate["error_margin"]
            z = z_two_proportions(canary, baseline)
            stats["error_z"] = round(z, 2)
            if worse and z >= gate["z_threshold"]:
                breaches.append(f"canary error rate {canary.error_rate:.2%} is well above stable's "
                                f"{baseline.error_rate:.2%}, even though stable is also over the limit")
            else:
                holds.append((BASELINE_UNHEALTHY, f"both versions are over the {limit:.2%} error limit "
                                                  f"(canary {canary.error_rate:.2%}, stable {baseline.error_rate:.2%})"))
        elif has_baseline:
            worse = canary.error_rate > baseline.error_rate * gate["error_ratio_over_baseline"] + gate["error_margin"]
            z = z_two_proportions(canary, baseline)
            stats["error_z"] = round(z, 2)
            if worse and z >= gate["z_threshold"]:
                breaches.append(f"canary error rate {canary.error_rate:.2%} against stable's "
                                f"{baseline.error_rate:.2%} (limit {limit:.2%})")
            else:
                holds.append((INCONCLUSIVE, f"canary error rate {canary.error_rate:.2%} is over the limit but "
                                            f"not clearly worse than stable's {baseline.error_rate:.2%}"))
        else:
            z = z_against_limit(canary, limit)
            stats["error_z"] = round(z, 2)
            if z >= gate["z_threshold"]:
                breaches.append(f"error rate {canary.error_rate:.2%} is over the {limit:.2%} limit")
            else:
                holds.append((INCONCLUSIVE, f"error rate {canary.error_rate:.2%} is over the limit, "
                                            f"but within what chance could explain"))

    # ---- latency
    p99_limit = gate["max_p99_ms"]
    if canary.p99_ms is not None and canary.p99_ms > p99_limit:
        base_p99 = baseline.p99_ms if has_baseline else None
        if base_p99 is not None and base_p99 > p99_limit and \
                canary.p99_ms <= base_p99 * gate["latency_ratio_over_baseline"]:
            holds.append((BASELINE_UNHEALTHY, f"both versions are slow (canary p99 {canary.p99_ms:.0f} ms, "
                                              f"stable {base_p99:.0f} ms)"))
        elif base_p99 is None or canary.p99_ms > base_p99 * gate["latency_ratio_over_baseline"]:
            breaches.append(f"canary p99 {canary.p99_ms:.0f} ms is over the {p99_limit} ms limit"
                            + (f" (stable: {base_p99:.0f} ms)" if base_p99 is not None else ""))
        else:
            holds.append((INCONCLUSIVE, f"canary p99 {canary.p99_ms:.0f} ms is over the limit but close to "
                                        f"stable's {base_p99:.0f} ms"))

    if breaches:
        return Decision(BREACH, breaches, stats)
    if holds:
        return Decision(holds[0][0], [reason for _, reason in holds], stats)
    return Decision(PASS, [], stats)
