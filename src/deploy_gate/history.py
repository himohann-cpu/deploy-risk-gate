"""A record of what happened to each rollout: the evidence the risk weights learn from.

One JSON object per line, append-only. If a change is recorded more than once
(completed first, then tied to an incident a day later), the last record wins.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUTCOMES = ("completed", "rolled_back", "incident", "held")


def append_record(path, assessment, outcome: str, when: datetime | None = None, note: str = "") -> dict:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
    when = when or datetime.now(timezone.utc)
    record = {
        "at": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "service": assessment.service, "ref": assessment.ref, "title": assessment.title,
        "score": assessment.score, "tier": assessment.tier, "outcome": outcome,
        "factors": [{"key": f.key, "points": f.points} for f in assessment.factors if f.points],
    }
    if note:
        record["note"] = note
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return record


def load(path) -> list:
    """Return one record per change (the latest), oldest first. A missing file is an empty history."""
    path = Path(path)
    if not path.exists():
        return []
    latest = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        key = (record["service"], record["ref"]) if record.get("ref") else ("line", number)
        latest[key] = record
    return sorted(latest.values(), key=lambda r: r["at"])


def recent_failures(records: list, service: str, now: datetime, policy: dict) -> int:
    """Failed rollouts and incidents on this service inside the look-back window."""
    learning = policy["learning"]
    since = now - timedelta(days=learning["recent_failure_days"])
    return sum(1 for r in records
               if r["service"] == service and r["outcome"] in learning["failure_outcomes"]
               and since <= datetime.fromisoformat(r["at"].replace("Z", "+00:00")) <= now)
