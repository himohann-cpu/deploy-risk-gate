"""Markdown for humans. Numbers and decisions are written from the data;
model text appears only in a labelled section."""
from __future__ import annotations

from .rollout import needs_approval, to_argo_steps

APPROVAL_TEXT = {"none": "No approval needed.", "before_weight_50": "A person must approve before 50% of traffic.",
                 "before_start": "A person must approve before the rollout starts."}


def render_assessment(assessment, plan, model: dict | None = None, argo: bool = False) -> str:
    a = assessment
    out = [f"## Deployment risk: {a.tier.upper()} ({a.score}/100)", "",
           f"**Service:** {a.service}" + (f" · **Change:** {a.title}" if a.title else "")
           + (f" (`{a.ref}`)" if a.ref else ""), ""]
    if a.blocked:
        out += [f"> **Blocked:** {a.blocked_reason}", ""]
    out += ["| Factor | Points | Why |", "|---|---|---|"]
    out += [f"| {f.name} | {f.points} | {f.detail} |" for f in a.factors]
    out += ["", "### Rollout plan", ""]
    if plan.warning:
        out += [f"> {plan.warning}", ""]
    out += [APPROVAL_TEXT[plan.approval], "",
            "| Step | Traffic to new version | Bake time | Approval first |", "|---|---|---|---|"]
    out += [f"| {i + 1} | {s['weight']}% | {s['bake_minutes']} min | {'yes' if needs_approval(plan, i) else 'no'} |"
            for i, s in enumerate(plan.steps)]
    out += ["", f"Minimum time to full rollout: {plan.total_bake_minutes()} minutes. "
                "Each step is promoted only if the SLO gate passes; a breach rolls back automatically."]
    if argo:
        out += ["", "```yaml", to_argo_steps(plan).rstrip(), "```"]
    if a.explanation:
        out += ["", "### Explanation *(written by a model, checked against the factors above)*", ""]
        if a.explanation.get("summary"):
            out += [a.explanation["summary"], ""]
        out += [f"- **{item['factor']}:** {item['note']}" for item in a.explanation.get("review_focus", [])]
    if model and model.get("used") and model.get("rejected"):
        out += ["", "Rejected from the model's reply:"] + [f"- {r}" for r in model["rejected"]]
    elif model and not model.get("used"):
        out += ["", f"_No model explanation ({model.get('reason')})._"]
    return "\n".join(out) + "\n"


def render_rollout(name: str, plan, result, service=None) -> str:
    out = [f"## Rollout: {name}", "",
           f"**Plan:** {plan.tier} ({' → '.join(str(s['weight']) + '%' for s in plan.steps)}) · "
           f"**Outcome:** `{result.outcome}` · **Peak traffic on new version:** {result.peak_weight}% · "
           f"**Elapsed:** {result.minutes} min · **Gate checks:** {result.checks}", ""]
    if result.reason:
        out += [f"**Reason:** {result.reason}", ""]
    if service is not None:
        out += [f"**Requests that failed because of the new version:** {service.excess_failed_requests()}", ""]
    out += ["| Minute | Event | Detail |", "|---|---|---|"]
    out += [f"| {e['minute']} | {e['event']} | {e['detail']} |" for e in result.timeline]
    return "\n".join(out) + "\n"
