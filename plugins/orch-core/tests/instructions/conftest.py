"""Fixtures of the instruction and ``orch init`` tests: a fake terminal and a cheap-scrypt backend."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from orch.custody import KdfParams, NoPrompt, PassphraseBackend, PassphraseRequest
from orch.ops import workspace_init as wi

PASSPHRASE = "correct horse battery"


class FakeTerminal:
    """What the person sees and types on ``/dev/tty``."""

    def __init__(self) -> None:
        self.ok = True
        self.shown: list[str] = []
        self.waits: list[str] = []

    def available(self) -> bool:
        return self.ok

    def show(self, text: str) -> None:
        if not self.ok:
            raise NoPrompt("no terminal")
        self.shown.append(text)

    def wait(self, prompt: str) -> None:
        if not self.ok:
            raise NoPrompt("no terminal")
        self.waits.append(prompt)


class Passphrases:
    """The passphrase provider of the test backend: records each request and answers with ``PASSPHRASE``."""

    def __init__(self) -> None:
        self.requests: list[PassphraseRequest] = []
        self.answer = PASSPHRASE

    def __call__(self, request: PassphraseRequest) -> str:
        self.requests.append(request)
        return self.answer


@pytest.fixture
def term(monkeypatch) -> FakeTerminal:
    t = FakeTerminal()
    monkeypatch.setattr(wi, "TERMINAL", t)
    return t


@pytest.fixture
def passphrases(monkeypatch) -> Passphrases:
    p = Passphrases()
    monkeypatch.setattr(
        wi,
        "open_backend",
        lambda directory: PassphraseBackend(directory, _passphrase_provider=p, _kdf=KdfParams(2**15, 8, 1)),
    )
    return p


@pytest.fixture
def where(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "work"
    root.mkdir()
    return {"root": root, "state": tmp_path / "state", "home": tmp_path / "home"}


def init_ctx(where: dict[str, Path], *, grant: str | None = None, human: bool = True, **env_extra: str):
    """The Context ``orch init`` gets from the CLI, for a workspace to be created in ``where['root']``."""
    from orch.ops.base import Context
    from orch.ops.runtime import Workspace

    env = {"ORCH_STATE_DIR": str(where["state"]), "HOME": str(where["home"]), "USER": "severin", **env_extra}
    return Context(
        env=env,
        grant=grant,
        human_presence=human,
        now=time.time,
        workspace=Workspace(env, time.time, cwd=str(where["root"])),
    )
