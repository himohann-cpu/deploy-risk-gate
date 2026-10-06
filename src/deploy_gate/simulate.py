"""A simulated service, for the eval and the demo. It is both the deployer
(it accepts traffic weights) and the metrics source (it reports what happened).

Counts are expected values, not random draws, so every run gives the same result.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .gate import Snapshot


@dataclass
class SimulatedService:
    requests_per_minute: int = 6000
    stable_error_rate: float = 0.002
    stable_p99_ms: float = 400.0
    canary_error_rate: float = 0.002
    canary_p99_ms: float = 400.0
    # Some faults only show under load: the canary turns bad at or above this weight.
    bad_from_weight: int = 0
    weight: int = 0
    rolled_back: bool = False
    promoted: bool = False
    history: list = field(default_factory=list)   # one entry per minute

    # ------------------------------------------------------------ deployer
    def set_weight(self, percent: int) -> None:
        self.weight = percent

    def rollback(self) -> None:
        self.weight, self.rolled_back = 0, True

    def promote(self) -> None:
        self.promoted = True

    def tick(self) -> None:
        """One minute of traffic at the current weight."""
        canary_requests = round(self.requests_per_minute * self.weight / 100)
        stable_requests = self.requests_per_minute - canary_requests
        faulty = self.weight >= self.bad_from_weight
        canary_rate = self.canary_error_rate if faulty else self.stable_error_rate
        self.history.append({
            "weight": self.weight,
            "canary": (canary_requests, canary_requests * canary_rate,
                       self.canary_p99_ms if faulty else self.stable_p99_ms),
            "stable": (stable_requests, stable_requests * self.stable_error_rate, self.stable_p99_ms),
        })

    # ------------------------------------------------------------- metrics
    def snapshot(self, version: str, window_minutes: int) -> Snapshot:
        recent = [minute[version] for minute in self.history[-window_minutes:]]
        requests = sum(r for r, _, _ in recent)
        return Snapshot(requests=requests, errors=round(sum(e for _, e, _ in recent)),
                        p99_ms=max((p for r, _, p in recent if r), default=None))

    # ------------------------------------------------------------ outcomes
    def excess_failed_requests(self) -> int:
        """Requests that failed because they hit the canary instead of stable."""
        return round(sum(m["canary"][1] - m["canary"][0] * self.stable_error_rate for m in self.history))

    def slow_canary_requests(self, limit_ms: float) -> int:
        return sum(m["canary"][0] for m in self.history if m["canary"][2] > limit_ms)
