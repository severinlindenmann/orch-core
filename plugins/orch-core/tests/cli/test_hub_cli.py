"""V3 C2a: ``orch hub``: who may run it, the order of signing and talking to the hub, and what is refused before any
host or key is touched."""

from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace

import pytest

from orch.cli import hub
from orch.ops.errors import OrchError
from tests.hub.vec import V

CARD = V["objects"]["card"]
USER = V["ids"]["user_alice"]


def run(argv, env, *, as_json=False):
    out, err = io.StringIO(), io.StringIO()
    code = hub.main([*argv, *(["--json"] if as_json else [])], env=env, out=out, err=err)
    return code, out.getvalue()


def never(*a, **k):
    raise AssertionError("the host must not be reached")


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(hub, "_target", never)
    monkeypatch.setattr(hub, "_call", never)
    monkeypatch.setattr(hub, "_sign", never)


@pytest.fixture
def card_file(tmp_path):
    p = tmp_path / "hub-card.json"
    p.write_text(json.dumps(CARD))
    return str(p)


@pytest.fixture
def host(monkeypatch):
    """A host that records what the CLI signs and calls."""
    log = SimpleNamespace(signed=[], called=[], reply={}, tty=True)
    person = "p_" + "4c" * 16
    target = SimpleNamespace(entry={"peers": {str(os.getuid()): {"person": person}}})
    monkeypatch.setattr(hub, "_target", lambda env: target)
    monkeypatch.setattr(hub, "_tty_ok", lambda: log.tty)
    monkeypatch.setattr(hub, "_sign", lambda t, op, args: log.signed.append((op, args)))

    def call(t, op, args, env):
        log.called.append((op, args))
        reply = log.reply.get(op, {})
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(hub, "_call", call)
    log.person = person
    return log


@pytest.mark.parametrize("argv", [["join", "inv", "--hub-card", "x"], ["leave"], ["connect", "inv", "--hub-card", "x"],
                                  ["unbind"], ["status"]])  # fmt: skip
@pytest.mark.parametrize(
    "env", [{"ORCH_GRANT": "gr_x"}, {"ORCH_SESSION": "s.1"}, {"ORCH_GRANT": "g", "ORCH_SESSION": "s"}]
)
def test_an_agent_or_a_session_is_refused_before_anything(offline, argv, env):
    code, out = run(argv, env)
    assert code == 3 and out.startswith("error op.not_allowed")


def test_the_refusal_in_json(offline):
    code, out = run(["leave"], {"ORCH_GRANT": "gr_x"}, as_json=True)
    assert code == 3 and json.loads(out)["code"] == "op.not_allowed"


@pytest.mark.parametrize(
    "argv",
    [[], ["bogus"], ["join"], ["join", "inv"], ["join", "a", "b", "--hub-card", "x"], ["leave", "x"], ["status", "x"],
     ["connect"], ["join", "inv", "--hub-card", "x", "--mode", "weird"],
     ["join", "inv", "--hub-card", "x", "--owner-user-id", "NOT-HEX"], ["leave", "--owner-user-id", USER]],
)  # fmt: skip
def test_a_malformed_command_prints_the_usage_and_touches_nothing(offline, argv):
    code, out = run(argv, {})
    assert code in (2, 5) and ("usage: orch hub" in out or "error invalid.input" in out)


def test_full_mode_is_refused_before_any_host_or_key(offline, card_file):
    code, out = run(["join", "inv", "--hub-card", card_file, "--mode", "full"], {})
    assert code == 3 and out.startswith("error hub.full_mode_later") and "C2b" in out


def test_join_signs_then_connects(host, card_file):
    host.reply["hub.connect"] = {"hub_id": V["ids"]["hub"], "status": "bound", "binding_seq": 1}
    code, out = run(["join", "inv-1", "--hub-card", card_file, "--owner-user-id", USER], {})
    assert code == 0 and "signed" in out and "ok hub joined" in out and "sees only what you share" in out
    ((op, args),) = host.signed
    assert op == "hub.join" and args["mode"] == "selective" and args["hub_card"] == CARD
    assert args["aliases"] == [{"person_id": "4c" * 16, "user_id": USER}]
    assert host.called == [("hub.connect", {"invite": "inv-1", "hub_card": CARD})]


def test_join_without_a_terminal_signs_nothing(host, card_file):
    host.tty = False
    code, out = run(["join", "inv-1", "--hub-card", card_file], {})
    assert code == 5 and "no terminal" in out and host.signed == [] and host.called == []


def test_a_hub_that_is_down_after_the_signature_says_how_to_finish(host, card_file):
    host.reply["hub.connect"] = OrchError("hub.unreachable", "down")
    code, out = run(["join", "inv-1", "--hub-card", card_file], {})
    assert code == 9 and "orch hub connect inv-1" in out and "orch hub leave" in out and len(host.signed) == 1


def test_a_bad_card_file_is_refused_before_signing(host, tmp_path):
    for content in (b"not json", b'{"a":1,"a":2}', b"[]", b"x" * 70_000):
        p = tmp_path / "bad.json"
        p.write_bytes(content)
        code, out = run(["join", "inv", "--hub-card", str(p)], {})
        assert code == 5 and "error invalid.input" in out
    code, out = run(["join", "inv", "--hub-card", str(tmp_path / "missing.json")], {})
    assert code == 5 and host.signed == []


def test_leave_signs_then_unbinds_and_works_with_the_hub_down(host):
    host.reply["hub.unbind"] = {"hub_id": V["ids"]["hub"], "unbound": True, "delivered": False}
    code, out = run(["leave"], {})
    assert code == 0 and "recorded here" in out and "orch hub unbind" in out
    assert host.signed == [("hub.leave", {})] and host.called == [("hub.unbind", {})]


def test_leave_when_the_hub_took_it(host):
    host.reply["hub.unbind"] = {"hub_id": V["ids"]["hub"], "unbound": True, "delivered": True}
    code, out = run(["leave"], {})
    assert code == 0 and "the hub and publish are told" in out


def test_status_and_unbind_need_no_signature(host):
    host.reply["hub.status"] = {"joined": True, "hub_id": V["ids"]["hub"], "mode": "selective", "link": "bound",
                                "unbound_pending": False}  # fmt: skip
    code, out = run(["status"], {})
    assert code == 0 and "joined (selective)" in out and host.signed == []
    host.reply["hub.status"] = {"joined": False, "hub_id": None}
    assert "works on its own" in run(["status"], {})[1]
    host.reply["hub.unbind"] = {"hub_id": V["ids"]["hub"], "unbound": True, "delivered": True}
    assert run(["unbind"], {})[0] == 0 and host.signed == []


def test_orch_dispatches_the_hub_verb(monkeypatch):
    from orch.cli.main import main as orch_main

    seen = []
    monkeypatch.setattr(hub, "main", lambda argv, **kw: seen.append(argv) or 0)
    assert orch_main(["hub", "status"], env={}, stdout=io.StringIO(), stderr=io.StringIO()) == 0
    assert seen == [["status"]]
