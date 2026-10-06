"""Describe a change: which files, how many lines, and what kinds of thing they are."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

KIND_RULES = [
    ("migration", ("migrations/", "alembic/", "flyway/"), (".sql",)),
    ("infra", ("terraform/", "helm/", "k8s/", "kubernetes/", ".github/workflows/", "deploy/"),
     (".tf", "Dockerfile", "docker-compose.yml")),
    ("dependency", (), ("requirements.txt", "package.json", "package-lock.json", "go.mod", "go.sum",
                        "poetry.lock", "pom.xml", "Cargo.lock", "Cargo.toml")),
    ("config", ("config/", "conf/"), (".yaml", ".yml", ".toml", ".ini", ".env", ".properties")),
    ("docs", ("docs/",), (".md", ".rst")),
]
TEST_MARKERS = ("test_", "_test.", ".test.", ".spec.", "/tests/", "/test/", "tests/")
# Most dangerous first: a change is as risky as its riskiest file.
KIND_ORDER = ["migration", "infra", "config", "dependency", "code", "docs"]


def kind_of(path: str) -> str:
    normalised = path.replace("\\", "/")
    for kind, folders, endings in KIND_RULES:
        if any(f in normalised for f in folders) or normalised.endswith(endings):
            return kind
    return "code"


def is_test(path: str) -> bool:
    normalised = "/" + path.replace("\\", "/")
    return any(marker in normalised for marker in TEST_MARKERS)


@dataclass
class ChangedFile:
    path: str
    added: int = 0
    deleted: int = 0


@dataclass
class Change:
    service: str
    files: list = field(default_factory=list)
    title: str = ""
    ref: str = ""

    @property
    def lines(self) -> int:
        return sum(f.added + f.deleted for f in self.files)

    def kinds(self) -> list:
        """Kinds present among non-test files, most dangerous first."""
        present = {kind_of(f.path) for f in self.files if not is_test(f.path)}
        return [k for k in KIND_ORDER if k in present]

    @classmethod
    def from_dict(cls, d: dict) -> "Change":
        return cls(service=d["service"], title=d.get("title", ""), ref=d.get("ref", ""),
                   files=[ChangedFile(f["path"], f.get("added", 0), f.get("deleted", 0)) for f in d.get("files", [])])

    @classmethod
    def from_file(cls, path) -> "Change":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_git(cls, service: str, base: str, head: str = "HEAD", repo: str = ".") -> "Change":
        """Read the change from `git diff --numstat base...head`."""
        proc = subprocess.run(["git", "-C", repo, "diff", "--numstat", f"{base}...{head}"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(f"git diff failed: {proc.stderr.strip()}")
        files = []
        for line in proc.stdout.splitlines():
            added, deleted, path = line.split("\t", 2)
            # Binary files report "-" for both counts.
            files.append(ChangedFile(path, int(added) if added.isdigit() else 0, int(deleted) if deleted.isdigit() else 0))
        title = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%s", head], capture_output=True,
                               text=True).stdout.strip()
        sha = subprocess.run(["git", "-C", repo, "rev-parse", "--short", head], capture_output=True,
                             text=True).stdout.strip()
        return cls(service=service, files=files, title=title, ref=sha)
