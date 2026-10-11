"""Fixtures of the instruction and ``orch init`` tests: a scripted terminal and a cheap-scrypt backend."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from orch.custody import KdfParams, NoPrompt, PassphraseBackend, PassphraseRequest
from orch.ops import workspace_init as wi

OWN = "Zq7!mPx2-vL9#rTb4w"  # a passphrase of the person's own that passes the strength check


class FakeTerminal:
    """What the person sees and types on ``/dev/tty``. ``typed`` is the queue of answers to the passphrase prompts
    (empty: type the generated passphrase back); the recovery-code words are read from what was shown."""

    def __init__(self) -> None:
        self.ok = True
        self.shown: list[str] = []
        self.waits: list[str] = []
        self.asked: list[str] = []
        self.cleared = 0
        self.typed: list[str] = []
        self.wrong_words = False
        self.final: str | None = None  # the passphrase the person settled on, if their own (the signing prompt gets it)
        self.interrupt_at: str | None = None  # a prompt prefix at which the person presses Ctrl-C

    def available(self) -> bool:
        return self.ok

    @property
    def text(self) -> str:
        return "".join(self.shown)

    @property
    def generated(self) -> str:
        return re.findall(r"Your generated passphrase:\n\n    (.+)\n", self.text)[-1]

    @property
    def code(self) -> str:
        return re.findall(r"^([a-z]+(?: [a-z]+){23})$", self.text, flags=re.M)[-1]

    def show(self, text: str) -> None:
        if not self.ok:
            raise NoPrompt("no terminal")
        self.shown.append(text)

    def clear(self) -> None:
        self.cleared += 1

    def _maybe_interrupt(self, prompt: str) -> None:
        if self.interrupt_at and prompt.startswith(self.interrupt_at):
            raise KeyboardInterrupt

    def wait(self, prompt: str) -> None:
        if not self.ok:
            raise NoPrompt("no terminal")
        self._maybe_interrupt(prompt)
        self.waits.append(prompt)

    def ask_secret(self, prompt: str) -> str:
        if not self.ok:
            raise NoPrompt("no terminal")
        self._maybe_interrupt(prompt)
        self.asked.append(prompt)
        if prompt.startswith("Word "):
            n = int(prompt[5:].split(":")[0])
            return "wrong" if self.wrong_words else self.code.split()[n - 1]
        return self.typed.pop(0) if self.typed else self.generated


class Passphrases:
    """The passphrase provider of the test backend (the signing prompt): records each request, answers ``answer``."""

    def __init__(self, term: FakeTerminal) -> None:
        self.requests: list[PassphraseRequest] = []
        self.term = term
        self.override: str | None = None
        self.interrupt = False

    def __call__(self, request: PassphraseRequest) -> str:
        self.requests.append(request)
        if self.interrupt:
            raise KeyboardInterrupt
        return self.override or self.term.final or self.term.generated


@pytest.fixture
def term(monkeypatch) -> FakeTerminal:
    t = FakeTerminal()
    monkeypatch.setattr(wi, "TERMINAL", t)
    return t


@pytest.fixture
def passphrases(monkeypatch, term) -> Passphrases:
    """The generated passphrase is typed back by default; the backend's own prompt (signing) gets the same one."""
    p = Passphrases(term)

    def backend(directory):
        return PassphraseBackend(directory, _passphrase_provider=p, _kdf=KdfParams(2**15, 8, 1))

    monkeypatch.setattr(wi, "open_backend", backend)
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
