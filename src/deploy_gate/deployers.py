"""Deployers: the part that actually changes how much traffic the new version gets.

Two are provided. Both act through kubectl, and both default to a dry run that
records and prints the commands without running them.

  ArgoRolloutsDeployer    Argo holds each stage at an indefinite pause; this
                          promotes past it when the gate passes, or aborts.
  IstioVirtualServiceDeployer
                          No Argo: sets the route weights on a VirtualService directly.

The rollout controller calls set_weight / rollback / promote / tick on whichever is used.
"""
from __future__ import annotations

import json
import re
import subprocess
import time

SAFE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")   # Kubernetes resource names


class CommandRunner:
    """Runs commands, or only records them when `execute` is False."""

    def __init__(self, execute: bool = False, echo=print):
        self.execute, self.echo, self.commands = execute, echo, []

    def run(self, argv: list) -> str:
        self.commands.append(argv)
        if self.echo:
            self.echo(("" if self.execute else "[dry run] ") + " ".join(argv))
        if not self.execute:
            return ""
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(argv[:4])} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout


class _Deployer:
    def __init__(self, name: str, namespace: str, runner: CommandRunner | None = None, sleep=time.sleep):
        for value in (name, namespace):
            if not SAFE.match(value):
                raise ValueError(f"not a valid Kubernetes name: {value!r}")
        self.name, self.namespace = name, namespace
        self.runner = runner or CommandRunner()
        self._sleep = sleep
        self.weight = 0

    def tick(self) -> None:
        """Wait one minute of real time between gate checks."""
        self._sleep(60)


class ArgoRolloutsDeployer(_Deployer):
    """Drives a Rollout whose stages end in `pause: {}` (see `deploy-gate apply-plan --gated`).

    Argo applies the first weight by itself when the new version is deployed.
    Each later weight is reached by promoting past the pause in front of it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._started = False

    def _rollouts(self, verb: str, *extra: str) -> None:
        self.runner.run(["kubectl", "argo", "rollouts", verb, self.name, "-n", self.namespace, *extra])

    def set_weight(self, percent: int) -> None:
        if self._started:
            self._rollouts("promote")       # leave the current pause; Argo moves to the next setWeight
        self._started = True
        self._wait_for(percent)
        self.weight = percent

    def _wait_for(self, percent: int, attempts: int = 60, every_seconds: int = 10) -> None:
        """Do not start judging the canary until Argo has really applied the weight.

        Analysis steps kept between stages run before the next setWeight, and a
        failed one makes Argo abort; either way the weight tells us.
        """
        if not self.runner.execute:
            return
        seen = None
        for _ in range(attempts):
            seen = self.current_weight()
            if seen == percent:
                return
            self._sleep(every_seconds)
        raise RuntimeError(f"rollout {self.name} did not reach {percent}% (Argo reports {seen}%); "
                           "check `kubectl argo rollouts get rollout` before continuing")

    def promote(self) -> None:
        self._rollouts("promote")           # leave the final pause; the new version becomes stable

    def rollback(self) -> None:
        self._rollouts("abort")             # all traffic back to the stable version
        self.weight = 0

    def current_weight(self):
        """The canary weight Argo reports, or None in a dry run."""
        out = self.runner.run(["kubectl", "get", "rollout", self.name, "-n", self.namespace, "-o", "json"])
        if not out:
            return None
        status = json.loads(out).get("status", {})
        return status.get("canary", {}).get("weights", {}).get("canary", {}).get("weight")


class IstioVirtualServiceDeployer(_Deployer):
    """Sets weights on a VirtualService that splits traffic between two routes.

    Expects spec.http[<http_index>].route to list the stable destination at
    `stable_index` and the canary at `canary_index`.
    """

    def __init__(self, *args, http_index: int = 0, stable_index: int = 0, canary_index: int = 1, **kwargs):
        super().__init__(*args, **kwargs)
        self.http_index, self.stable_index, self.canary_index = http_index, stable_index, canary_index

    def set_weight(self, percent: int) -> None:
        if not 0 <= percent <= 100:
            raise ValueError(f"weight must be between 0 and 100, got {percent}")
        base = f"/spec/http/{self.http_index}/route"
        patch = [{"op": "replace", "path": f"{base}/{self.stable_index}/weight", "value": 100 - percent},
                 {"op": "replace", "path": f"{base}/{self.canary_index}/weight", "value": percent}]
        self.runner.run(["kubectl", "patch", "virtualservice", self.name, "-n", self.namespace,
                         "--type=json", "-p", json.dumps(patch, separators=(",", ":"))])
        self.weight = percent

    def promote(self) -> None:
        if self.weight != 100:
            self.set_weight(100)

    def rollback(self) -> None:
        self.set_weight(0)
