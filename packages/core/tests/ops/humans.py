"""People who sign through the real passphrase backend, for the tests of the human operations (C7).

``HumanWs`` is the ``Ws`` of the C6 tests with an owner whose device key lives in a ``PassphraseBackend`` in the place
the CLI looks (``<state dir>/hosts/<workspace id>/person/dk.key.json``). The passphrase is typed by a **provider** (the
underscore seam of C2): it records what the prompt showed and answers with ``ws.passphrase``. The tests make the CLI
use that backend by replacing ``orch.ops.human.open_backend`` (``human_cli`` below); no production flag skips the
prompt. A low scrypt cost (the floor, 2^15) keeps a signature near 0.1 s.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from orch import crypto
from orch.custody import KdfParams, PassphraseBackend, PassphraseRequest, render_prompt
from orch.ops import human
from tests.identity.helpers import Person
from tests.ops.helpers import Cli, Ws
from tests.store.helpers import WS

PASSPHRASE = "Zq7!mPx2-vL9#rTb4w"
KDF = KdfParams(n=2**15)  # the floor the production backend accepts: a key made here loads in a real process too


class Provider:
    """The passphrase prompt as a callback: records every request and the text the prompt would show."""

    def __init__(self, passphrase: str = PASSPHRASE) -> None:
        self.passphrase = passphrase
        self.requests: list[PassphraseRequest] = []
        self.shown: list[str] = []

    #: runs while the person is "typing": a test makes something happen during the prompt
    on_prompt: Callable[[PassphraseRequest], None] | None = None

    def __call__(self, request: PassphraseRequest) -> str:
        self.requests.append(request)
        if self.on_prompt is not None:
            self.on_prompt(request)
        self.shown.append(render_prompt(request))
        return self.passphrase


class BackedPerson(Person):
    """A ``Person`` whose device signing key is a passphrase-backend key (the person key stays in memory)."""

    def __init__(self, directory: Path, provider: Provider) -> None:
        super().__init__()
        self.backend = PassphraseBackend(directory, _passphrase_provider=provider, _kdf=KDF)
        self.sig_pub = self.backend.create("dk", role="device")
        self.directory = directory

    def dk_sign(self, payload: bytes) -> bytes:
        return self.backend.sign("dk", payload, action="test")


class HumanWs(Ws):
    def __init__(self, tmp_path: Path, *, live: bool = False) -> None:
        super().__init__(tmp_path, live=live)
        self.provider = Provider()
        self.shown_secrets: list[str] = []
        self.reviews: list[str] = []  # what the terminal showed before the passphrase prompt
        self.confirm = True
        self.expects: list[str] = []  # what the person has to type to go on: the ticket key
        self.on_review: Callable[[str], None] | None = None
        self.owner = BackedPerson(tmp_path / "keys-owner", self.provider)
        self.people = {"owner": self.owner}
        self.person_dir = self.host_state / "hosts" / WS / "person"
        self.act_as(self.owner)

    def bootstrap(self, **kw):
        """The default ``verify`` policy wants a ticket reviewer who is no assignee; these tests let the owner verify
        (a sole owner may verify work of their own agents, F1 5.7), as the sole-owner workspace of the examples does."""
        s = super().bootstrap(**kw)
        policy = {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False}
        s.append(
            self.person_event(self.owner, "workspace", "policy.changed", gates={"verify": policy}), log="workspace"
        )
        return s

    def review_prompt(self, text: str, expect: str) -> bool:
        self.reviews.append(text)
        self.expects.append(expect)
        if self.on_review is not None:
            self.on_review(text)
        return self.confirm

    def act_as(self, person: BackedPerson) -> None:
        """The key on 'this machine' is ``person``'s (the CLI reads one device key per workspace)."""
        self.person_dir.mkdir(parents=True, exist_ok=True)
        dst = self.person_dir / "dk.key.json"
        dst.unlink(missing_ok=True)
        shutil.copyfile(person.directory / "dk.key.json", dst)
        dst.chmod(0o600)
        self.person_dir.chmod(0o700)

    def add_member(
        self, name: str, role: str = "member", *, scopes: tuple[str, ...] = ("look", "decide", "operate", "type")
    ):
        p = BackedPerson(self.tmp / f"keys-{name}", self.provider)
        self.people[name] = p
        self.store.append(
            self.person_event(
                self.owner,
                "workspace",
                "member.added",
                person=p.ref,
                name=name,
                role=role,
                pk_pub=crypto.b64u(p.pk_pub),
                device_cert=p.cert(created=self.clock[0] * 1000, scopes=scopes),
            ),
            log="workspace",
        )
        return p

    def backend(self, directory: Path) -> PassphraseBackend:
        return PassphraseBackend(directory, _passphrase_provider=self.provider, _kdf=KDF)


@pytest.fixture
def hws(tmp_path, monkeypatch):
    yield from _hws(tmp_path, monkeypatch, live=False)


@pytest.fixture
def live_hws(tmp_path, monkeypatch):
    """The same on the wall clock, for tests that start the binary in a subprocess."""
    yield from _hws(tmp_path, monkeypatch, live=True)


def _hws(tmp_path, monkeypatch, *, live):
    w = HumanWs(tmp_path, live=live)
    w.bootstrap()
    monkeypatch.setattr(human, "open_backend", w.backend)
    monkeypatch.setattr(human, "show_secret", w.shown_secrets.append)
    monkeypatch.setattr(human, "review_prompt", w.review_prompt)
    w.provider.requests.clear()  # what the setup signed is not what the test is about
    w.provider.shown.clear()
    yield w
    if w.store is not None:
        w.store.close()


@pytest.fixture
def agent(hws):
    """An agent with the grant: the C6 CLI."""
    return Cli(hws)


@pytest.fixture
def me(hws):
    """The person at the terminal: no grant, no session."""
    return Cli(hws, grant=False, session=None)


@pytest.fixture
def live_agent(live_hws):
    return Cli(live_hws)


def make_ticket(agent: Cli, title: str = "Load the tables", *, tasks: bool = True) -> str:
    """A feature ticket the agent has claimed and filled, ready for the person to approve requirements and plan."""
    r = agent("new", title, "--priority", "high", "-m", "Load the tables.")
    assert r.code == 0, r.err
    key = r.first.split()[1]
    assert agent("claim", key).code == 0
    for sec in ("context", "requirements", "out_of_scope", "plan", "decisions", "verification"):
        assert agent("section", "set", sec, "-m", f"text of {sec} for {title}").code == 0, sec
    assert agent("ac", "add", f"the build is green for {title}").code == 0
    if tasks:
        r = agent("task", "add", f"run it for {title}", "--verify", f"{sys.executable} -c pass", "--proves", "AC1")
        assert r.code == 0, r.err
    return key


def to_testing(agent: Cli, me: Cli, tmp: Path, key: str) -> None:
    """Approve requirements and plan, do the task, submit: the ticket is in testing."""
    assert me("approve", "requirements", "--ref", key).code == 0
    assert me("approve", "plan", "--ref", key).code == 0
    assert agent("task", "start", "T1").code == 0
    r = agent("task", "done", "T1", "--run", "-m", "ran")
    assert r.code == 0, r.err
    log = tmp / "evidence.log"
    log.write_text("all green\n")
    assert agent("artifact", "add", str(log), "--ac", "AC1").code == 0
    r = agent("submit")
    assert r.code == 0, r.err
