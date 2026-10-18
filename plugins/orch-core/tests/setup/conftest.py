from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from orch.setup.flow import Env, SetupFlow


class Tmp:
    def __init__(self, d: str) -> None:
        self.root = Path(d).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "state"
        self.work = self.root / "work"
        self.work.mkdir()


@pytest.fixture
def tmp() -> Iterator[Tmp]:
    with tempfile.TemporaryDirectory(prefix="hk", dir="/tmp") as d:
        yield Tmp(d)


def make_env(t: Tmp, **kw) -> Env:
    base = dict(env={"USER": "ann"}, home=t.home, state_dir=t.state, config_dirs=(t.root / "config",))
    base.update(kw)
    return Env(**base)


@pytest.fixture
def flow(tmp: Tmp) -> SetupFlow:
    f = SetupFlow(make_env(tmp))
    f.launch(str(tmp.work))
    return f


PROFILE = {
    "name": "Ann",
    "prefix": "ANN",
    "addons": ["dashboard", "terminals"],
    "method": "terminal",
    "agent_hours": 8,
    "agent_grant": False,
    "relay": False,
}


def post(f: SetupFlow, route: str, body=None):
    return f.dispatch("POST", route, {}, body if body is not None else {})


def get(f: SetupFlow, route: str, **q):
    return f.dispatch("GET", route, q, None)
