"""Score a change from 0 to 100. Every point is attributed to a named factor,
so the score can be argued with: change the policy file, not the prompt."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from .change import Change, is_test
from .policy import banded, tier_for


@dataclass
class Factor:
    name: str
    points: int
    detail: str
    key: str = ""        # what calibration learns a weight for; defaults to the name

    def __post_init__(self):
        self.key = self.key or self.name


@dataclass
class Assessment:
    service: str
    title: str
    ref: str
    score: int
    tier: str
    factors: list
    kinds: list = field(default_factory=list)
    blocked: bool = False
    blocked_reason: str = ""
    explanation: dict = field(default_factory=dict)   # optional, from the model

    def to_dict(self) -> dict:
        return asdict(self)


def _window(policy: dict, when: datetime):
    """Return (risky, reason, frozen, freeze_reason) for the planned deploy time."""
    w = policy["deploy_window"]
    local = when.astimezone(timezone(timedelta(hours=w["utc_offset_hours"])))
    for freeze in w.get("freezes", []):
        if freeze["from"] <= local.strftime("%Y-%m-%d") <= freeze["to"]:
            return False, "", True, f"change freeze: {freeze.get('reason', 'no reason given')} ({freeze['from']} to {freeze['to']})"
    if local.weekday() not in w["workdays"]:
        return True, "outside working days", False, ""
    if not w["start_hour"] <= local.hour < w["end_hour"]:
        return True, "outside working hours", False, ""
    if local.weekday() == 4 and local.hour >= w["friday_cutoff_hour"]:
        return True, "late on a Friday", False, ""
    return False, "", False, ""


def assess(change: Change, policy: dict, upstream=None, recent_incidents: int = 0,
           when: datetime | None = None) -> Assessment:
    """`upstream` is the list of services that depend on this one, or None if the service is not in the map."""
    r = policy["risk"]
    when = when or datetime.now(timezone.utc)
    kinds = change.kinds()
    factors = []

    risky_time, time_reason, frozen, freeze_reason = _window(policy, when)

    if kinds in ([], ["docs"]):
        factors.append(Factor("change_kind", 0, "documentation or tests only; nothing that runs in production"))
    else:
        top = kinds[0]
        factors.append(Factor("change_kind", r["kind_points"][top],
                              f"includes a {top} change" + (f" (also: {', '.join(kinds[1:])})" if kinds[1:] else ""),
                              key=f"change_kind:{top}"))
        factors.append(Factor("size", banded(r["size_points"], change.lines),
                              f"{change.lines} lines across {len(change.files)} files"))
        if len(change.files) > r["many_files"]["over"]:
            factors.append(Factor("many_files", r["many_files"]["points"],
                                  f"touches more than {r['many_files']['over']} files"))

        sensitive = sorted({f.path for f in change.files
                            if any(p in f.path.lower() for p in r["sensitive_paths"]["patterns"])})
        if sensitive:
            factors.append(Factor("sensitive_paths", r["sensitive_paths"]["points"],
                                  f"touches sensitive code: {', '.join(sensitive[:3])}"))

        code_changed = any(k in kinds for k in ("code", "migration"))
        tests_changed = any(is_test(f.path) for f in change.files)
        if code_changed and not tests_changed:
            factors.append(Factor("no_tests", r["no_tests_points"], "changes code or schema but no test file"))

        if upstream is None:
            factors.append(Factor("blast_radius", r["unknown_service_points"],
                                  f"{change.service} is not in the dependency map, so its reach is unknown"))
        else:
            factors.append(Factor("blast_radius", banded(r["blast_radius_points"], len(upstream)),
                                  f"{len(upstream)} service(s) depend on it"
                                  + (f": {', '.join(upstream[:5])}" if upstream else "")))

        if recent_incidents:
            factors.append(Factor("recent_incidents", banded(r["incident_points"], recent_incidents),
                                  f"{recent_incidents} incident(s) or failed rollout(s) on this service "
                                  f"in the last 30 days"))
        if risky_time:
            factors.append(Factor("timing", r["risky_time_points"], f"planned deploy is {time_reason}"))

    # Weights learned from past rollouts (see calibrate.py) scale the hand-set points.
    for factor in factors:
        multiplier = r.get("multipliers", {}).get(factor.key, 1.0)
        if multiplier != 1.0 and factor.points:
            factor.points = round(factor.points * multiplier)
            factor.detail += f" (weight x{multiplier:g}, learned from past rollouts)"

    score = min(100, sum(f.points for f in factors))
    return Assessment(service=change.service, title=change.title, ref=change.ref, score=score,
                      tier=tier_for(policy, score), factors=[f for f in factors if f.points or f.name == "change_kind"],
                      kinds=kinds, blocked=frozen, blocked_reason=freeze_reason)
