"""The policy is data: thresholds, tiers and rollout plans live in one JSON file
that a team reviews like any other change. Nothing here is decided by a model."""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path


def load_policy(paths=None) -> dict:
    """Load the built-in policy, then lay each file in `paths` over the top, in order (one level deep).

    Typical use is two files: your own overrides, then the weights learned by `deploy-gate calibrate`.
    """
    policy = json.loads(resources.files("deploy_gate").joinpath("default_policy.json").read_text(encoding="utf-8"))
    if paths is None:
        paths = []
    elif isinstance(paths, (str, Path)):
        paths = [paths]
    for path in paths:
        for key, value in json.loads(Path(path).read_text(encoding="utf-8")).items():
            if isinstance(value, dict) and isinstance(policy.get(key), dict):
                policy[key].update(value)
            else:
                policy[key] = value
    return policy


def banded(table: list, value) -> int:
    """Look up points in a [[upper_bound, points], ...] table; the first bound >= value wins."""
    for bound, points in table:
        if value <= bound:
            return points
    return table[-1][1]


def tier_for(policy: dict, score: int) -> str:
    for tier in policy["tiers"]:
        if score < tier["below"]:
            return tier["name"]
    return policy["tiers"][-1]["name"]
