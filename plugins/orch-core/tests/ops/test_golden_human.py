"""Golden output of the human operations, text and JSON, on one fixed workspace built through the real store and the
real passphrase backend (the prompt is the C2 callback seam). ``--update-golden`` rewrites ``golden_human/``."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from orch import crypto
from orch.ops import human
from tests.identity.helpers import Person
from tests.ops.helpers import Cli
from tests.ops.humans import HumanWs, Provider, make_ticket, to_testing
from tests.ops.test_golden import norm as _norm

GOLDEN = Path(__file__).parent / "golden_human"


def norm(text: str) -> str:
    text = _norm(text)
    return re.sub(r"sha256:[0-9a-f]{64}", "sha256:HASH", text)


def build(tmp: Path, mp: pytest.MonkeyPatch) -> HumanWs:
    ws = HumanWs(tmp)
    ws.bootstrap()
    mp.setattr(human, "open_backend", ws.backend)
    agent, me = Cli(ws), Cli(ws, grant=False, session=None)
    first = make_ticket(agent, "Needs approval")  # DEMO-0001: filled, nothing approved yet
    assert agent("ask", "Which export is the source of truth?", "--options", "csv,api", "--rec", "csv").code == 0
    assert agent("release").code == 0
    second = make_ticket(agent, "Ready for the verdict")  # DEMO-0002
    to_testing(agent, me, ws.tmp, second)
    assert first == "DEMO-0001" and second == "DEMO-0002"
    p = Person()
    (ws.tmp / "invitee.cert.json").write_text(json.dumps(p.cert(created=ws.clock[0] * 1000)))
    (ws.tmp / "invitee.pk").write_text(crypto.b64u(p.pk_pub))
    ws.add_member("quinn", "member")
    return ws


@pytest.fixture(scope="module")
def base():
    root = Path(tempfile.mkdtemp(prefix="orch-golden-human-"))
    with pytest.MonkeyPatch.context() as mp:
        ws = build(root, mp)
    (root / "grant.txt").write_text(ws.grant)
    ws.store.close()
    (root / "quinn.ref").write_text(ws.people["quinn"].ref)
    (root / "owner.ref").write_text(ws.owner.ref)
    yield root
    shutil.rmtree(root, ignore_errors=True)


class Fork(HumanWs):
    """A copy of the base workspace: the same ids and keys, on the same clock."""

    def __init__(self, src: Path, dst: Path) -> None:
        shutil.copytree(src, dst, symlinks=True)
        self.tmp, self.root, self.host_state = dst, dst / "workspace", dst / "host-state"
        self.clock = [1_790_000_000 + 400]
        self.live = False
        self.store = None
        self._grant = (src / "grant.txt").read_text()
        self.provider = Provider()
        self.shown_secrets: list[str] = []

    @property
    def grant(self) -> str:
        return self._grant  # type: ignore[return-value]


CASES = {
    "approve": (["approve", "requirements", "--ref", "DEMO-0001"], "me"),
    "approve-dry-run": (["approve", "requirements", "--ref", "DEMO-0001", "--dry-run"], "me"),
    "approve-human-only": (["approve", "requirements", "--ref", "DEMO-0001"], "agent"),
    "approve-wrong-passphrase": (["approve", "requirements", "--ref", "DEMO-0001"], "wrong"),
    "approve-unknown-ticket": (["approve", "requirements", "--ref", "99"], "me"),
    "request-changes": (["request-changes", "plan", "--ref", "DEMO-0001", "-m", "Split task 1 in two."], "me"),
    "verdict-pass": (["verdict", "pass", "--ref", "DEMO-0002"], "me"),
    "verdict-fail": (["verdict", "fail", "--ref", "DEMO-0002", "-m", "The join drops rows."], "me"),
    "verdict-refused": (["verdict", "pass", "--ref", "DEMO-0001"], "me"),
    "answer": (["answer", "Q1", "--option", "api", "-m", "It is complete.", "--ref", "DEMO-0001"], "me"),
    "close": (["close", "DEMO-0001", "--resolution", "wont_do", "-m", "Not worth it."], "me"),
    "reopen": (["reopen", "DEMO-0002"], "me"),
    "grant": (["grant", "--label", "laptop"], "me"),
    "grant-verbs": (["grant", "--hours", "2", "--scope", "all", "--verbs", "task.done,log"], "me"),
    "grant-refused": (["grant", "--verbs", "approve"], "me"),
    "grant-revoke": (["grant", "revoke", "{grant}", "--reason", "laptop lost"], "me"),
    "member-add": (["member", "add", "--name", "Nina", "--pk", "{pk}", "--cert", "{cert}", "--role", "viewer"], "me"),
    "member-role": (["member", "role", "{quinn}", "maintainer"], "me"),
    "member-remove": (["member", "remove", "{quinn}"], "me"),
    "member-last-owner": (["member", "remove", "{owner}"], "me"),
}


def run(base: Path, name: str, as_json: bool, mp: pytest.MonkeyPatch) -> str:
    argv, who = CASES[name]
    tmp = Path(tempfile.mkdtemp(prefix="orch-golden-human-run-"))
    try:
        ws = Fork(base, tmp / "w")
        owner_ref = (base / "owner.ref").read_text()
        subst = {
            "{pk}": (base / "invitee.pk").read_text(),
            "{cert}": str(ws.tmp / "invitee.cert.json"),
            "{quinn}": (base / "quinn.ref").read_text(),
            "{owner}": owner_ref,
            "{grant}": ws.grant.partition(".")[0],
        }
        argv = [subst.get(a, a) for a in argv]
        if who == "wrong":
            ws.provider.passphrase = "not the passphrase"
        mp.setattr(human, "open_backend", ws.backend)
        mp.setattr(human, "show_secret", lambda text: None)  # the secret goes to a terminal; a golden file is not one
        cli = Cli(ws) if who == "agent" else Cli(ws, grant=False, session=None)
        r = cli(*argv, *(["--json"] if as_json else []))
        if as_json:
            doc = json.loads(r.out)
            return norm(json.dumps(doc, indent=2, ensure_ascii=False)) + f"\n# exit {r.code}\n"
        return norm(f"exit {r.code}\n--- stdout\n{r.out}--- stderr\n{r.err}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_golden(name, as_json, base, request, monkeypatch):
    text = run(base, name, as_json, monkeypatch)
    path = GOLDEN / f"{name}.{'json' if as_json else 'txt'}"
    if request.config.getoption("--update-golden"):
        assert not os.environ.get("CI"), "--update-golden is refused when CI is set"
        path.parent.mkdir(exist_ok=True)
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
            print(f"golden written: {path.name}")
        return
    assert path.exists(), f"missing {path.name}; run pytest tests/ops/test_golden_human.py --update-golden -s"
    assert path.read_text() == text, f"{path.name} differs; run with --update-golden if intended"


def test_no_stale_golden_files(request):
    if request.config.getoption("--update-golden"):
        for p in GOLDEN.glob("*"):
            if p.stem not in CASES:
                p.unlink()
        return
    have = {p.name for p in GOLDEN.glob("*")}
    want = {f"{n}.{e}" for n in CASES for e in ("txt", "json")}
    assert have == want, (sorted(have - want), sorted(want - have))
