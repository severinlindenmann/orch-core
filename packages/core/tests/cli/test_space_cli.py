"""V3 C13: ``orch space``: who may run what, what the owner is shown before ``share`` / ``accept``, files in/out."""

from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

import pytest

from orch.cli import space

SID, WS1, WS2 = "ab" * 16, "11" * 16, "22" * 16


def run(argv, env=None, *, as_json=True):
    out = io.StringIO()
    code = space.main([*argv, *(["--json"] if as_json else [])], env=env or {}, out=out, err=io.StringIO())
    return code, out.getvalue()


@pytest.fixture
def host(monkeypatch):
    log = SimpleNamespace(called=[], reply={}, asked=[], yes=True)
    monkeypatch.setattr(space, "_target", lambda env: object())

    def call(t, op, args, env):
        log.called.append((op, args))
        return log.reply.get(op, {})

    def confirm(prompt, verb="sign"):
        log.asked.append(prompt)
        return log.yes

    monkeypatch.setattr(space, "_call", call)
    monkeypatch.setattr(space, "review_prompt", confirm)
    return log


@pytest.mark.parametrize(
    "argv",
    [["share", SID], ["revoke", SID, WS1], ["agents", SID, "on"],
     ["accept", SID, "--epoch", "2", "--prev-members", WS1, "--members", f"{WS1},{WS2}"]],
)  # fmt: skip
def test_an_agent_cannot_write(host, argv):
    code, out = run(argv, {"ORCH_GRANT": "g"})
    assert code == 3 and json.loads(out)["code"] == "op.not_allowed" and host.called == []


def test_an_agent_may_list_and_get(host, tmp_path):
    host.reply["space.get"] = {"data": "aGk", "author": WS1, "item_id": "x", "name": "n", "size": 2}
    assert run(["list"], {"ORCH_GRANT": "g"})[0] == 0
    assert run(["get", SID, "n", "--out", str(tmp_path / "o")], {"ORCH_SESSION": "s"})[0] == 0
    assert (tmp_path / "o").read_bytes() == b"hi"


def test_get_never_overwrites_and_writes_private(host, tmp_path):
    host.reply["space.get"] = {"data": "aGk", "author": WS1, "item_id": "x", "name": "n", "size": 2}
    out = tmp_path / "o"
    out.write_bytes(b"mine")
    assert run(["get", SID, "n", "--out", str(out)])[0] == 5 and out.read_bytes() == b"mine"
    other = tmp_path / "p"
    run(["get", SID, "n", "--out", str(other)])
    assert other.stat().st_mode & 0o777 == 0o600


def test_put_sends_the_file_and_refuses_a_big_one(host, tmp_path):
    f = tmp_path / "r.txt"
    f.write_bytes(b"hello")
    host.reply["space.put"] = {"item_id": "i" * 32, "seq": 1, "epoch": 1}
    assert run(["put", SID, str(f), "--name", "report"])[0] == 0
    op, args = host.called[-1]
    assert op == "space.put" and args["name"] == "report" and base64.urlsafe_b64decode(args["data"] + "==") == b"hello"
    f.write_bytes(b"x" * 200_000)
    code, out = run(["put", SID, str(f)])
    assert code == 5 and "too large" in json.loads(out)["message"] and len(host.called) == 1


def test_share_shows_the_members_and_sends_exactly_that_list(host):
    host.reply["space.list"] = {"spaces": [{"space_id": SID, "epoch": 3, "members": [WS1, WS2]}]}
    host.reply["space.share"] = {"space_id": SID, "members": [WS1, WS2]}
    assert run(["share", SID])[0] == 0
    prompt = host.asked[-1]
    assert WS1 in prompt and WS2 in prompt and "epoch 3" in prompt
    assert host.called[-1] == ("space.share", {"space_id": SID, "members": [WS1, WS2], "understood": True})
    assert prompt.startswith("The hub and its admins can read what you put here.")  # the warning comes BEFORE the ask
    host.yes = False
    n = len(host.called)
    assert run(["share", SID])[0] == 3 and host.called[n:] == [("space.list", {})]  # not approved: nothing stored


def test_accept_always_asks_and_shows_the_transition(host):
    argv = ["accept", SID, "--epoch", "2", "--prev-members", WS1, "--members", f"{WS1},{WS2}"]
    host.yes = False
    assert run(argv)[0] == 3 and host.called == []
    prompt = host.asked[-1]
    assert SID in prompt and "epoch 2" in prompt and WS1 in prompt and WS2 in prompt
    host.yes = True
    assert run(argv)[0] == 0
    assert host.called[-1] == (
        "space.accept", {"space_id": SID, "epoch": 2, "prev_members": [WS1], "members": [WS1, WS2]}
    )  # fmt: skip


def test_shape_errors(host):
    assert run(["put", SID])[0] == 2 and run(["get", SID, "n"])[0] == 2 and run(["nope"])[0] == 2


def test_get_passes_from(host, tmp_path):
    host.reply["space.get"] = {"data": "aGk", "author": WS1, "item_id": "x", "name": "n", "size": 2}
    assert run(["get", SID, "n", "--out", str(tmp_path / "o"), "--from", WS1])[0] == 0
    assert host.called[-1] == ("space.get", {"space_id": SID, "name": "n", "from": WS1})


def test_share_with_members_still_asks_and_the_list_must_be_the_hubs_current_one(host):
    host.reply["space.list"] = {"spaces": [{"space_id": SID, "epoch": 3, "members": [WS1, WS2]}]}
    host.reply["space.share"] = {"space_id": SID, "members": [WS1, WS2]}
    assert run(["share", SID, "--members", f"{WS1},{WS2}"])[0] == 0
    assert host.asked and WS1 in host.asked[-1]  # asked even with --members
    n, asked = len(host.called), len(host.asked)
    code, out = run(["share", SID, "--members", WS1])  # not what the hub lists now: refused before any prompt
    assert code == 5 and len(host.asked) == asked and [c[0] for c in host.called[n:]] == ["space.list"]
    host.yes = False
    assert run(["share", SID, "--members", f"{WS1},{WS2}"])[0] == 3 and host.called[-1][0] == "space.list"


def test_accept_prompt_shows_the_signed_hashes(host):
    run(["accept", SID, "--epoch", "2", "--prev-members", WS1, "--members", f"{WS1},{WS2}"])
    from orch.hubclient.spaces import members_hash

    prompt = host.asked[-1]
    assert members_hash([WS1])[:16] in prompt and members_hash(sorted([WS1, WS2]))[:16] in prompt
    assert "hub admin" in prompt


def test_an_agent_may_put_restore_and_ask_for_history_the_host_decides(host, tmp_path):
    f = tmp_path / "r.txt"
    f.write_bytes(b"x")
    host.reply["space.put"] = {"item_id": "i" * 32, "seq": 1, "epoch": 1}
    host.reply["space.restore"] = {"item_id": "j" * 32, "name": "n", "restored_from": WS1}
    host.reply["space.history"] = {"versions": []}
    env = {"ORCH_GRANT": "g"}
    for argv in (["put", SID, str(f)], ["restore", SID, WS1], ["history", SID, "n"]):
        assert run(argv, env)[0] == 0
    assert [c[0] for c in host.called] == ["space.put", "space.restore", "space.history"]
    assert host.asked == []  # nobody is asked: the owner's switch is what lets an agent in


def test_agents_switch_sends_on_off(host):
    host.reply["space.agents"] = {"space_id": SID, "agents": True, "warning": "Agents can copy workspace content here"}
    code, out = run(["agents", SID, "on"], as_json=False)
    assert code == 0 and host.called[-1] == ("space.agents", {"space_id": SID, "on": True}) and "Agents can copy" in out
    host.reply["space.agents"] = {"space_id": SID, "agents": False, "warning": "w"}
    assert run(["agents", SID, "off"])[0] == 0 and host.called[-1][1]["on"] is False
    assert run(["agents", SID, "maybe"])[0] == 2


def test_restore_sends_the_item(host):
    host.reply["space.restore"] = {"item_id": "j" * 32, "name": "plan.md", "restored_from": WS1}
    assert run(["restore", SID, WS1])[0] == 0 and host.called[-1] == (
        "space.restore",
        {"space_id": SID, "item_id": WS1},
    )


def test_history_strips_control_characters_from_what_another_workspace_wrote(host):
    evil = "x\x1b[31mred\x07\u202egnp.exe\n\r\x00" + "y" * 300
    host.reply["space.history"] = {
        "versions": [
            {"item_id": "a" * 32, "created_ms": 5, "author": evil, "by": evil, "revoked": False, "size": 3},
            {"item_id": "b" * 32, "created_ms": 4, "author": WS1, "by": "person", "revoked": False, "size": 3,
             "restored_from": "a" * 32},
        ],
        "revoked_unnamed": 0,
    }  # fmt: skip
    for as_json in (False, True):
        code, out = run(["history", SID, "n"], as_json=as_json)
        assert code == 0
        shown = out if not as_json else json.loads(out)["data"]["versions"][0]["author"]
        for bad in ("\x1b", "\x07", "\u202e", "\x00", "\r"):
            assert bad not in shown
        assert len(shown) < 700
    text = run(["history", SID, "n"], as_json=False)[1]
    assert "restored from " + "a" * 32 in text and text.count("\n") <= 4


def test_agents_on_shows_the_warning_and_needs_a_terminal_off_does_not(host, monkeypatch):
    from orch.ops.human import review_prompt as real

    host.reply["space.agents"] = {"space_id": SID, "agents": True, "warning": "w"}
    run(["agents", SID, "on"])
    assert "Agents can copy workspace content here" in host.asked[-1]
    host.yes = False
    n = len(host.called)
    assert run(["agents", SID, "on"])[0] == 3 and len(host.called) == n  # declined: nothing sent
    asked = len(host.asked)
    assert run(["agents", SID, "off"])[0] == 0 and len(host.asked) == asked  # off asks nothing
    n = len(host.called)
    monkeypatch.setattr(space, "review_prompt", real)
    code, out = run(["agents", SID, "on"])
    assert code != 0 and json.loads(out)["code"] == "tty.required"
    assert not [c for c in host.called[n:] if c[0] == "space.agents"]
