"""#1455: ``orch app`` (the CLI): who may run what, the order of signing and sending, and that nothing is called before
the shape is right. The host half is ``tests/host/test_appops.py``; the host is replaced here."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from orch.cli import app

STAGED = "pa_" + "1" * 32


def run(argv, env, *, as_json=False):
    out, err = io.StringIO(), io.StringIO()
    code = app.main([*argv, *(["--json"] if as_json else [])], env=env, out=out, err=err)
    return code, out.getvalue()


def never(*a, **k):
    raise AssertionError("the host must not be reached")


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(app, "_target", never)
    monkeypatch.setattr(app, "_call", never)
    monkeypatch.setattr(app, "_sign", never)


@pytest.mark.parametrize("argv", [["release", STAGED], ["send", STAGED]])
def test_an_agent_can_neither_release_nor_send(offline, argv):
    code, out = run(argv, {"ORCH_GRANT": "gr_x", "ORCH_SESSION": "s.1"})
    assert code == 3 and out.startswith("error op.not_allowed: a person does this; an agent only prepares")


def test_an_agent_refusal_in_json(offline):
    code, out = run(["release", STAGED], {"ORCH_GRANT": "gr_x"}, as_json=True)
    assert code == 3 and json.loads(out)["code"] == "op.not_allowed"


@pytest.mark.parametrize(
    "argv",
    [[], ["bogus"], ["prepare"], ["prepare", "TST-0001"], ["prepare", "TST-0001", "a.html"],
     ["prepare", "TST-0001", "a.html", "--name", "N", "--kind", "app"], ["release"], ["release", "a", "b"],
     ["send"], ["discard"], ["list", "a", "b"],
     ["prepare", "T", "a", "--name", "N", "--kind", "x", "--access", "public"]],
)  # fmt: skip
def test_a_malformed_command_prints_the_usage_and_touches_nothing(offline, argv):
    code, out = run(argv, {})
    assert code == 2 and "usage: orch app" in out


class Rec:
    def __init__(self):
        self.calls = []

    def target(self, env):
        return SimpleNamespace()

    def call(self, target, op, args, env):
        self.calls.append((op, args))
        if op == "app.send":
            return {"app_id": "a" * 32, "version": 1, "uploaded": True, "live": False, "needs": "the server's approval"}
        if op == "app.prepare":
            return {"staged_id": STAGED, "ticket": "TST-0001", "files": [{}, {}], "access": args["access"]}
        return {}

    def sign(self, target, op, args):
        self.calls.append(("sign:" + op, args))
        return {}


@pytest.fixture
def rec(monkeypatch):
    r = Rec()
    monkeypatch.setattr(app, "_target", r.target)
    monkeypatch.setattr(app, "_call", r.call)
    monkeypatch.setattr(app, "_sign", r.sign)
    monkeypatch.setattr(app, "_tty_ok", lambda: True)
    return r


def test_release_signs_first_then_sends_and_says_it_is_not_live(rec):
    code, out = run(["release", STAGED], {})
    assert code == 0 and [c[0] for c in rec.calls] == ["sign:app.release", "app.send"]
    assert rec.calls[0][1] == {"staged_id": STAGED}
    assert "not live yet" in out


def test_release_without_a_terminal_stops_before_anything_is_signed(rec, monkeypatch):
    monkeypatch.setattr(app, "_tty_ok", lambda: False)
    code, out = run(["release", STAGED], {})
    assert code == 3 and "no terminal" in out and rec.calls == []


def test_a_refused_signature_sends_nothing(rec, monkeypatch):
    def refused(target, op, args):
        raise app.PubError("invalid.input", "not confirmed: nothing was signed", 5)

    monkeypatch.setattr(app, "_sign", refused)
    code, out = run(["release", STAGED], {})
    assert code == 5 and rec.calls == [] and "nothing was signed" in out


def test_an_agent_prepare_passes_the_choices_and_says_nothing_is_live(rec):
    code, out = run(
        ["prepare", "TST-0001", "index.html", "app.js", "--name", "Board", "--kind", "app", "--access", "company",
         "--reach", "https://api.example.com", "--slug", "board"],
        {"ORCH_GRANT": "gr_x", "ORCH_SESSION": "s.1"},
    )  # fmt: skip
    assert code == 0 and rec.calls == [
        ("app.prepare", {"ref": "TST-0001", "artifacts": ["index.html", "app.js"], "name": "Board", "kind": "app",
                         "access": "company", "slug": "board", "reach": ["https://api.example.com"]})
    ]  # fmt: skip
    assert "nothing is live" in out and f"orch app release {STAGED}" in out
