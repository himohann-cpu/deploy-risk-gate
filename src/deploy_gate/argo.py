"""Fit a rollout plan into an existing Argo Rollouts manifest.

Only the traffic steps change. Everything else in the file is left as it was:
analysis steps, background analysis, traffic routing, the pod template,
comments, and any other documents in the file.

Rules for the canary `steps`:

- Every `setWeight` and `pause` step is replaced by the plan's weights and bake times.
- Every other step (analysis, experiment, setCanaryScale, setHeaderRoute, ...) is kept.
  Steps that came before the first `setWeight` stay first. The rest are
  repeated after the bake of every stage, so the new rollout never runs fewer
  checks than the old one.
- Where the plan needs a person's approval, an indefinite `pause: {}` is added.
- With `gated=True`, each bake is an indefinite pause instead of a timed one,
  so an external controller (`deploy-gate run`) decides when to promote. That
  controller also handles approval, so no separate approval pause is added.

Needs ruamel.yaml:  pip install "deploy-gate[argo]"
"""
from __future__ import annotations

import difflib
import io

from .rollout import Plan, needs_approval

TRAFFIC_STEPS = ("setWeight", "pause")
ANNOTATIONS = "deploy-gate.dev/"


class ManifestError(Exception):
    pass


def _yaml():
    try:
        from ruamel.yaml import YAML
    except ImportError as error:
        raise ManifestError('reading a Rollout needs ruamel.yaml: pip install "deploy-gate[argo]"') from error
    yaml = YAML()                      # round-trip mode keeps comments and layout
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _plain(node):
    """A comparable copy of a YAML node, without comments."""
    if isinstance(node, dict):
        return {k: _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    return node


def new_steps(old_steps: list, plan: Plan, gated: bool = False) -> list:
    """Build the replacement step list as plain data."""
    leading, repeated, seen_weight = [], [], False
    for step in old_steps or []:
        kind = next(iter(step), None) if isinstance(step, dict) else None
        if kind == "setWeight":
            seen_weight = True
        elif kind in TRAFFIC_STEPS:
            continue
        elif not seen_weight:
            leading.append(_plain(step))
        elif _plain(step) not in repeated:
            repeated.append(_plain(step))

    steps = list(leading)
    for index, stage in enumerate(plan.steps):
        if needs_approval(plan, index) and not gated:
            steps.append({"pause": {}})      # when gated, the controller asks for approval itself
        steps.append({"setWeight": stage["weight"]})
        steps.append({"pause": {}} if gated else {"pause": {"duration": f"{stage['bake_minutes']}m"}})
        steps.extend(repeated)
    return steps


def apply_plan(manifest_text: str, plan: Plan, assessment=None, gated: bool = False, name: str | None = None):
    """Return (new_text, summary). Raises ManifestError if no suitable Rollout is found."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    yaml = _yaml()
    documents = list(yaml.load_all(manifest_text))
    rollouts = [d for d in documents if isinstance(d, dict) and d.get("kind") == "Rollout"
                and (name is None or d.get("metadata", {}).get("name") == name)]
    if not rollouts:
        raise ManifestError("no Rollout" + (f" named {name!r}" if name else "") + " found in the manifest")
    if len(rollouts) > 1:
        names = ", ".join(r.get("metadata", {}).get("name", "?") for r in rollouts)
        raise ManifestError(f"several Rollouts found ({names}); choose one with --name")
    rollout = rollouts[0]
    strategy = rollout.get("spec", {}).get("strategy", {})
    if "canary" not in strategy or strategy["canary"] is None:
        raise ManifestError("this Rollout does not use the canary strategy, so there are no traffic steps to set")
    canary = strategy["canary"]

    old = _plain(canary.get("steps") or [])
    steps = new_steps(old, plan, gated)

    def to_node(value):
        if isinstance(value, dict):
            node = CommentedMap((k, to_node(v)) for k, v in value.items())
            if not value or set(value) == {"duration"}:
                node.fa.set_flow_style()          # pause: {} and pause: {duration: 10m} on one line
            return node
        if isinstance(value, list):
            return CommentedSeq(to_node(v) for v in value)
        return value

    canary["steps"] = to_node(steps)

    if assessment is not None:
        metadata = rollout.setdefault("metadata", CommentedMap())
        annotations = metadata.get("annotations")
        if annotations is None:
            annotations = metadata["annotations"] = CommentedMap()
        annotations[ANNOTATIONS + "tier"] = assessment.tier
        annotations[ANNOTATIONS + "score"] = str(assessment.score)
        if assessment.ref:
            annotations[ANNOTATIONS + "change"] = assessment.ref

    buffer = io.StringIO()
    yaml.dump_all(documents, buffer)
    new_text = buffer.getvalue()

    kept = [next(iter(s)) for s in steps if next(iter(s)) not in TRAFFIC_STEPS]
    summary = {
        "rollout": rollout.get("metadata", {}).get("name"),
        "weights_before": [s["setWeight"] for s in old if "setWeight" in s],
        "weights_after": [s["setWeight"] for s in steps if "setWeight" in s],
        "kept_steps": kept,
        "kept_step_kinds": sorted(set(kept)),
        "approval_pauses": 0 if gated else sum(needs_approval(plan, i) for i in range(len(plan.steps))),
        "gated": gated,
        "background_analysis_untouched": "analysis" in canary,
    }
    return new_text, summary


def diff(before: str, after: str, path: str = "rollout.yaml") -> str:
    label = path.replace("\\", "/").lstrip("/")
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=f"a/{label}", tofile=f"b/{label}"))
