"""Read canary and stable metrics from Prometheus, using Istio's standard metrics.

Versions are told apart by `destination_canonical_revision`, which Istio takes
from each pod's `version` label. GET requests only.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from .gate import Snapshot

SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


class PrometheusMetrics:
    def __init__(self, base_url: str, service: str, canary_revision: str, stable_revision: str,
                 token: str | None = None, timeout: float = 15.0):
        for value in (service, canary_revision, stable_revision):
            if not SAFE.match(value):
                raise ValueError(f"unsafe label value: {value!r}")
        self.base_url, self.service, self.token, self.timeout = base_url.rstrip("/"), service, token, timeout
        self.revisions = {"canary": canary_revision, "stable": stable_revision}

    def _selector(self, version: str, extra: str = "") -> str:
        return (f'{{reporter="destination", destination_canonical_service="{self.service}", '
                f'destination_canonical_revision="{self.revisions[version]}"{extra}}}')

    def queries(self, version: str, window_minutes: int) -> dict:
        w = f"{window_minutes}m"
        all_requests = self._selector(version)
        server_errors = self._selector(version, ', response_code=~"5.."')
        return {
            "requests": f"sum(increase(istio_requests_total{all_requests}[{w}]))",
            "errors": f"sum(increase(istio_requests_total{server_errors}[{w}]))",
            "p99_ms": (f"histogram_quantile(0.99, sum by (le) "
                       f"(rate(istio_request_duration_milliseconds_bucket{all_requests}[{w}])))"),
        }

    def _scalar(self, promql: str):
        url = f"{self.base_url}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
        request = urllib.request.Request(url, method="GET")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            result = json.loads(response.read().decode("utf-8"))["data"]["result"]
        if not result:
            return None
        value = float(result[0]["value"][1])
        return None if value != value else value   # NaN means no data

    def snapshot(self, version: str, window_minutes: int) -> Snapshot:
        q = self.queries(version, window_minutes)
        requests, errors, p99 = (self._scalar(q[name]) for name in ("requests", "errors", "p99_ms"))
        return Snapshot(requests=round(requests or 0), errors=round(errors or 0), p99_ms=p99)
