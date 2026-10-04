from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from orch.addons.api import HEALTH, Snapshot
from orch.addons.runner import AddonRunError, RunResult
from orch.clock import now

PAYLOAD = "<script>alert('orch')</script>"  # every sample string: proves widgets are escaped


def sample_items(kind: str, text: str = "sample") -> tuple:
    return {
        "reviews": ({"provider": "github", "host": "github.com", "repo": text, "number": 1, "url": "https://example.com/pr/1",
                     "title": text, "state": "open", "draft": False, "review": "required", "checks": {"state": "failed"}},),
        "issues": ({"tracker": "GH", "key": "GH-1", "url": "https://example.com/issues/1", "title": text, "category": "todo"},),
        "pages": ({"provider": "github-wiki", "space": text, "id": "home", "title": text, "url": "https://example.com/wiki",
                   "path": "Home.md", "updated_at": "2026-10-02T09:00:00+00:00"},),
        "status": ({"id": "s1", "label": text, "role": "warn", "text": text},),
    }[kind]


def make_snapshot(provider: str, scope: str, *, health: str = "ok", items=(), message: str = "",
                  fetched_at=None, retry_after=None) -> Snapshot:
    at = fetched_at or now()
    if health == "rate_limited" and retry_after is None:
        retry_after = at + timedelta(minutes=5)
    return Snapshot(provider, scope, at, health=health, items=tuple(items), message=message, retry_after=retry_after)


@dataclass(frozen=True)
class Recording:
    argv: tuple
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    raises: str | None = None  # "timeout" | "missing"

    @classmethod
    def from_dict(cls, d: dict) -> "Recording":
        stdout = json.dumps(d["stdout_json"]) if "stdout_json" in d else d.get("stdout", "")
        return cls(tuple(d["argv"]), int(d.get("returncode", 0)), stdout, d.get("stderr", ""), d.get("raises"))

    def matches(self, argv) -> bool:
        return len(argv) == len(self.argv) and all(r == "*" or r == a for r, a in zip(self.argv, argv))


class FakeRunner:
    """Replays recorded CLI output for ctx.run (e.g. `gh pr list --json …` captured from the demo repo)."""

    def __init__(self, recordings=(), *, strict: bool = True):
        self.recordings = [r if isinstance(r, Recording) else Recording.from_dict(r) for r in recordings]
        self.strict = strict
        self.calls: list[tuple] = []
        self.timeouts: list[float] = []  # the timeout of each call, in the same order as .calls

    @classmethod
    def from_dir(cls, folder, *, strict: bool = True) -> "FakeRunner":
        recs = []
        for path in sorted(Path(folder).glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            recs.extend(data if isinstance(data, list) else [data])
        return cls(recs, strict=strict)

    def add(self, argv, *, stdout: str = "", stdout_json=None, returncode: int = 0, stderr: str = "", raises=None):
        if stdout_json is not None:
            stdout = json.dumps(stdout_json)
        self.recordings.append(Recording(tuple(argv), returncode, stdout, stderr, raises))
        return self

    def __call__(self, argv, timeout: float) -> RunResult:
        self.calls.append(tuple(argv))
        self.timeouts.append(float(timeout))
        for r in self.recordings:
            if r.matches(argv):
                if r.raises == "timeout":
                    raise AddonRunError(f"{argv[0]} timed out after {timeout:g} s")
                if r.raises == "missing":
                    raise AddonRunError(f"{argv[0]} is not installed or not on PATH")
                return RunResult(tuple(argv), r.returncode, r.stdout, r.stderr)
        if self.strict:
            raise AssertionError(f"FakeRunner: no recording for {list(argv)}")
        return RunResult(tuple(argv), 127, "", "no recording")


class FakeRemote:
    """Stands in for ProviderContext.remote_decision in addon tests: records decisions, returns `result`."""

    def __init__(self, result):
        self.result, self.seen = result, []

    def __call__(self, decision: dict):
        self.seen.append(dict(decision))
        return self.result


class FakeProvider:
    """A provider returning canned snapshots, one health state each (for page and widget tests)."""

    def __init__(self, id: str = "fake", kind: str = "status", *, scopes=("default",), health: str = "ok",
                 items=None, interval_s: int = 300):
        self.id, self.kind, self.health, self.interval_s = id, kind, health, interval_s
        self._scopes, self._items = tuple(scopes), items

    def scopes(self, ctx) -> list[str]:
        return list(self._scopes)

    def fetch(self, ctx, scope, previous) -> Snapshot:
        items = self._items if self._items is not None else (sample_items(self.kind) if self.health in ("ok", "stale") else ())
        return make_snapshot(self.id, scope, health=self.health, items=items,
                             message="" if self.health == "ok" else f"fake {self.health}")

    @classmethod
    def each_health(cls, **kw) -> list["FakeProvider"]:
        return [cls(health=h, **kw) for h in HEALTH]
