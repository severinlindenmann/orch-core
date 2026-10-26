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
    "workspace_name": "Personal Workspace",
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


#: fixed product wording the setup tests assert on; a random secret must never be able to hide it
_FIXED_WORDING = (
    "macOS did not let orch use the keychain",
    "one-click could not be set up because macOS did not let orch use the keychain; "
    "this workspace uses the terminal for now. Write it down, never on the page",
)


@pytest.fixture(autouse=True)
def _secrets_that_never_collide_with_the_fixed_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redraw a generated passphrase / recovery code until none of its words occurs in the fixed wording above, so no
    test depends on a lucky draw (the word-collision flake family). Real redaction is tested with chosen words in
    test_redaction.py."""
    import orch.identity as identity
    from orch.custody import strength
    from orch.identity import generate_recovery_code as real_code

    real_pass = strength.generate_passphrase
    fixed = {w for s in _FIXED_WORDING for w in s.lower().replace(";", " ").replace(",", " ").split()}

    def clean(draw):
        def go(*a, **k):
            while True:
                v = draw(*a, **k)
                if not fixed & set(v.split()):
                    return v

        return go

    monkeypatch.setattr(strength, "generate_passphrase", clean(real_pass))
    monkeypatch.setattr(identity, "generate_recovery_code", clean(real_code))
