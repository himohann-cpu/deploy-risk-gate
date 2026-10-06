"""How many services are affected if this one breaks.

Reads either a services.json file (the format the incident agent and the Istio
dependency mapper share) or the dependency mapper's HTTP API.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

DEPTH = 3


def upstream_from_file(path, service: str, depth: int = DEPTH):
    """Return the services that depend on `service` within `depth` hops, or None if it is not in the map."""
    services = json.loads(Path(path).read_text(encoding="utf-8"))["services"]
    known = set(services) | {d for spec in services.values() for d in spec.get("depends_on", [])}
    if service not in known:
        return None
    callers = {}
    for name, spec in services.items():
        for dependency in spec.get("depends_on", []):
            callers.setdefault(dependency, set()).add(name)
    seen, queue, found = {service}, deque([(service, 0)]), []
    while queue:
        node, distance = queue.popleft()
        if distance == depth:
            continue
        for caller in sorted(callers.get(node, ())):
            if caller not in seen:
                seen.add(caller)
                found.append(caller)
                queue.append((caller, distance + 1))
    return found


def upstream_from_api(base_url: str, service: str, depth: int = DEPTH, timeout: float = 5.0):
    """Ask the dependency mapper API. Returns None if the service is unknown or the API cannot be reached."""
    url = f"{base_url.rstrip('/')}/services/{urllib.parse.quote(service)}?depth={depth}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    if depth > 1:
        return [u["service"] for u in body.get("upstream", [])]
    return [c["service"] for c in body.get("incoming", [])]
