"""V3 G3 test helpers: a signed official index with real (in-memory) packages, a fake network, and the stage / sign /
submit round trip on a served workspace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orch.addons import package
from orch.host.marketstate import MarketFiles
from orch.market import MarketError, address, constants, fetch
from tests.addons import market_ref as ref
from tests.host.dispatch_helpers import SESSION, err, ok
from tests.market import helpers as H

__all__ = ["Net", "SESSION", "agent", "err", "manifest", "net", "ok", "person"]

GIT = "https://github.com/acme/"


def manifest(name: str = "standup", version: str = "1.0.0", caps: tuple[str, ...] = ("network",), **more: Any) -> bytes:
    doc = {
        "schema": "orch.addon/3", "name": name, "version": version, "title": name.title(), "api": ">=1.0,<2",
        "min_core": "2.0.0.dev0", "capabilities": sorted(caps), "entry": {"cmd": ["python3", "{pkg}/main.py"]},
        **more,
    }  # fmt: skip
    return json.dumps(doc, sort_keys=True).encode()


def files(name: str = "standup", version: str = "1.0.0", caps: tuple[str, ...] = ("network",), **more: Any) -> dict:
    return {"orch-addon.json": manifest(name, version, caps, **more), "main.py": f"print({version!r})\n".encode()}


class Net:
    """Replaces the network. ``index`` is what the marketplace serves now; ``packages`` maps a commit to its files;
    ``fetches`` records every package fetch (an agent must cause none)."""

    def __init__(self) -> None:
        self.version = 1
        self.entries: list[dict[str, Any]] = []
        self.packages: dict[str, dict[str, bytes]] = {}
        self.fetches: list[str] = []
        self.index_calls: list[str | None] = []
        self.url = H.OFFICIAL
        self.key = H.KEY
        self.fail: MarketError | None = None

    def add(self, name: str = "standup", version: str = "1.0.0", caps: tuple[str, ...] = ("network",), *,
            commit: str | None = None, signed: bool = True, digest: str | None = None, entry_caps=None,
            min_orch: str = "2.0.0.dev0", pkg: dict[str, bytes] | None = None, key=None, **more) -> dict[str, Any]:  # fmt: skip
        pkg = pkg if pkg is not None else files(name, version, caps)
        commit = commit or (f"{abs(hash((name, version, tuple(sorted(pkg)), tuple(pkg.values())))):040x}")[-40:]
        entry: dict[str, Any] = {
            "id": name, "version": version, "source": {"git": GIT + name}, "commit": commit,
            "package_sha256": digest or package.package_digest(pkg),
            "capabilities": sorted(entry_caps if entry_caps is not None else caps), "min_orch": min_orch, **more,
        }  # fmt: skip
        if signed:
            entry["publisher"] = {"key": H.KEY_B64 if key is None else key[1], "sig": "A" * 86}
            entry["publisher"]["sig"] = ref.sign_entry(entry, key[0] if key else self.key, self.url)
        self.entries = [e for e in self.entries if e["id"] != name] + [entry]
        self.packages[commit] = pkg
        return entry

    def doc(self, **more) -> dict[str, Any]:
        return H.index(self.version, addons=self.entries, url=self.url, **more)

    def bump(self, **more) -> None:
        self.version += 1
        self.next = None

    # the patched functions
    def fetch_index(self, url, commit):
        self.index_calls.append(commit)
        if self.fail:
            raise self.fail
        return (commit or H.COMMIT_A), H.raw(self.doc())

    def fetch_package(self, entry):
        self.fetches.append(entry["id"])
        pkg = self.packages.get(entry["commit"])
        if pkg is None:
            raise MarketError("market.fetch_failed")
        return package.from_files(pkg)

    def resolve(self, host, timeout):
        return ["93.184.216.34"]


@pytest.fixture
def net(monkeypatch):
    n = Net()
    monkeypatch.setattr(fetch, "fetch_index", n.fetch_index)
    monkeypatch.setattr(fetch, "fetch_package", n.fetch_package)
    monkeypatch.setattr(address, "resolve", n.resolve)
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (H.KEY_B64,))
    return n


def person(served, op, args=None):
    return served.client().req(op, args or {}, idem=True)


def agent(served, op, args=None):
    return served.client().req(op, args or {}, grant=served.grant, session=SESSION, idem=True)


def mfiles(served) -> MarketFiles:
    s = served.service
    return MarketFiles(Path(s.state_dir), s.workspace_id, s.host_uid)


def refresh(served, net) -> None:
    ok(person(served, "market.refresh"))


def add_market(served, net) -> None:
    ok(person(served, "market.add", {"url": net.url}))


def stage(served, name="standup", **more):
    return person(served, "market.stage", {"id": name, **more})


def install(served, human, name="standup", **more):
    """stage → prepare → sign → submit; returns (stage result, prepared, submit result)."""
    st = ok(stage(served, name, **more))["data"]
    prepared = human.prepare("addon.grant", {"name": name, "staged": st["stage"]})
    signed = human.sign(prepared)
    return st, prepared, ok(human.submit_res(prepared["request"]["id"], signed.events))


def addon_dir(served, name="standup") -> Path:
    return Path(served.service.store.layout.store) / "addons" / name
