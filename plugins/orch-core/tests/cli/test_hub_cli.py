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


def test_an_agent_never_joins_in_full_mode(offline, card_file):
    for argv in (["join", "inv", "--hub-card", card_file, "--mode", "full"], ["mode", "full", "--hub-card", card_file]):
        code, out = run(argv, {"ORCH_GRANT": "g"})
        assert code != 0 and out.startswith("error op.not_allowed")


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


def test_join_full_warns_signs_full_and_connects(host, card_file):
    host.reply["hub.connect"] = {"hub_id": V["ids"]["hub"], "status": "bound", "binding_seq": 1, "mode": "full"}
    code, out = run(["join", "inv-1", "--hub-card", card_file, "--mode", "full"], {})
    assert code == 0 and "read everything in this workspace" in out
    assert host.signed[0][0] == "hub.join" and host.signed[0][1]["mode"] == "full"
    assert host.called[0][0] == "hub.connect" and host.called[0][1]["invite"] == "inv-1"


def test_mode_full_signs_hub_mode_and_connects_without_an_invite(host, card_file):
    host.reply["hub.connect"] = {"hub_id": V["ids"]["hub"], "status": "bound", "binding_seq": 2, "mode": "full"}
    code, out = run(["mode", "full", "--hub-card", card_file], {})
    assert code == 0 and "read everything" in out
    assert [o for o, _ in host.signed] == ["hub.mode"] and host.signed[0][1]["mode"] == "full"
    assert host.called[0][0] == "hub.connect" and "invite" not in host.called[0][1]


@pytest.mark.parametrize(
    "argv", [["mode", "selective", "--hub-card", "x"], ["mode", "--hub-card", "x"], ["mode", "full"]]
)
def test_mode_takes_only_full_and_a_card(offline, argv):
    code, out = run(argv, {})
    assert code in (2, 5) and "error invalid.input" in out


def test_status_json_carries_the_pinned_snapshot_and_refresh_asks_the_host_first(host):
    snap = {"version": 3, "roles": {"11" * 16: "admin"}}
    host.reply["hub.status"] = {"joined": True, "hub_id": V["ids"]["hub"], "mode": "selective", "link": "bound",
                                "unbound_pending": False, "snapshot": snap}  # fmt: skip
    code, out = run(["status", "--refresh"], {}, as_json=True)
    assert code == 0 and json.loads(out)["data"]["snapshot"] == snap
    assert [op for op, _ in host.called] == ["hub.refresh", "hub.status"] and host.signed == []
    assert host.called[0][1] == {}
    assert "group snapshot v3 (1 roles)" in run(["status"], {})[1]


def test_refresh_may_carry_a_newer_hub_card(host, card_file):
    host.reply["hub.status"] = {"joined": True, "hub_id": V["ids"]["hub"], "mode": "selective"}
    assert run(["status", "--refresh", "--hub-card", card_file], {})[0] == 0
    assert host.called[0] == ("hub.refresh", {"hub_card": CARD})


@pytest.mark.parametrize("argv", [["status", "--hub-card", "x"], ["leave", "--refresh"], ["unbind", "--refresh"]])
def test_refresh_flags_belong_to_status(host, argv):
    code, out = run(argv, {})
    assert code == 2 and host.called == []


# --- V3 C6: orch hub share ----------------------------------------------------------------------------------------


@pytest.fixture
def share_host(monkeypatch):
    log = SimpleNamespace(called=[], asked=[], yes=True)
    preview = {"count": 1, "new": 1, "revoked": 0, "more": 0, "plan": "p" * 43,
               "items": [{"logical": "ticket:t", "kind": "ticket", "line": "ticket K-1: Public note"}]}  # fmt: skip
    reply = {
        "share.preview": preview,
        "share.rule_add": {"note": "changed", "synced": True, "put": 1, "revoke": 0, "kept": 0},
        "share.rule_remove": {"note": "changed", "synced": True, "put": 0, "revoke": 1, "kept": 0},
        "share.revoke": {"note": "withdrawn", "synced": True, "put": 0, "revoke": 1, "kept": 0},
        "share.sync": {"synced": False, "pending": "hub_unreachable"},
        "share.rules": {
            "rules": [{"kind": "tag", "tag": "x", "count": 1, "describes": "d"}],
            "shared": 1,
            "withdrawn": [],
        },  # fmt: skip
    }
    monkeypatch.setattr(hub, "_target", lambda env: object())
    monkeypatch.setattr(hub, "_call", lambda t, op, args, env: (log.called.append((op, args)), reply[op])[1])
    from orch.cli import publish

    monkeypatch.setattr(publish, "_confirm", lambda prompt, word: (log.asked.append((prompt, word)), log.yes)[1])
    return log


@pytest.mark.parametrize(
    "argv",
    [["share", "rule", "add", "--tag", "x"], ["share", "rule", "list"], ["share", "revoke", "K-1"], ["share", "sync"]],
)
def test_an_agent_cannot_use_share_at_all(share_host, argv):
    code, out = run(argv, {"ORCH_GRANT": "g"}, as_json=True)
    assert code == 3 and json.loads(out)["code"] == "op.not_allowed" and share_host.called == []


def test_rule_add_shows_what_will_be_shared_and_binds_the_plan(share_host):
    code, out = run(["share", "rule", "add", "--tag", "x"], {}, as_json=True)
    assert code == 0
    ((prompt, word),) = share_host.asked
    assert word == "share" and "ticket K-1: Public note" in prompt and "cannot be taken back" in prompt
    rule = {"kind": "tag", "tag": "x"}
    assert share_host.called == [
        ("share.preview", {"add": rule}),
        ("share.rule_add", {"rule": rule, "expect": "p" * 43}),
    ]


def test_an_unconfirmed_rule_change_sends_nothing(share_host):
    share_host.yes = False
    code, _ = run(["share", "rule", "remove", "--tag", "x"], {}, as_json=True)
    assert code == 5 and [op for op, _ in share_host.called] == ["share.preview"]


def test_revoke_says_it_cannot_take_back_what_was_read(share_host):
    code, out = run(["share", "revoke", "K-1"], {})
    assert code == 0 and "cannot be taken back" in out and share_host.called == [("share.revoke", {"ref": "K-1"})]


@pytest.mark.parametrize(
    "argv",
    [["share"], ["share", "rule", "add"], ["share", "rule", "add", "--tag", "a", "--type", "b"],
     ["share", "rule", "list", "--tag", "a"], ["share", "sync", "--questions"], ["share", "revoke"]],
)  # fmt: skip
def test_malformed_share_commands_reach_nothing(share_host, argv):
    assert run(argv, {})[0] == 2 and share_host.called == []


def test_list_and_sync_report_without_a_terminal(share_host):
    code, out = run(["share", "rule", "list"], {})
    assert code == 0 and "1 now" in out
    code, out = run(["share", "sync"], {})
    assert code == 0 and "not updated yet (hub_unreachable)" in out


def test_refused_share_items_show_in_the_normal_output_and_a_stale_one_says_so(share_host, monkeypatch):
    refused = [{"item": "ticket K-1: Public note", "code": "too_large", "stale": True},
               {"item": "ticket K-2: Other", "code": "malformed", "stale": False}]  # fmt: skip
    sync = {"synced": True, "put": 0, "revoke": 0, "kept": 1, "refused": {}, "refused_items": refused}
    rules = {"rules": [], "shared": 1, "withdrawn": [], "refused_items": refused, "new": 0}
    monkeypatch.setattr(hub, "_call", lambda t, op, args, env: sync if op == "share.sync" else rules)
    for argv in (["share", "sync"], ["share", "rule", "list"]):
        code, out = run(argv, {})
        assert code == 0
        assert "not sent (too_large): ticket K-1: Public note\n    the hub has an older version" in out
        assert "not sent (malformed): ticket K-2: Other" in out and out.count("older version") == 1
    code, out = run(["share", "sync"], {}, as_json=True)
    assert json.loads(out)["data"]["refused_items"] == refused


def test_sync_shows_what_is_new_and_binds_the_plan(share_host):
    code, _ = run(["share", "sync"], {}, as_json=True)
    assert code == 0 and "ticket K-1: Public note" in share_host.asked[0][0]
    assert share_host.called == [("share.preview", {}), ("share.sync", {"expect": "p" * 43})]
    share_host.called.clear()
    share_host.yes = False
    assert run(["share", "sync"], {}, as_json=True)[0] == 5 and [op for op, _ in share_host.called] == ["share.preview"]


def test_a_cut_off_list_needs_the_typed_count(share_host, monkeypatch):
    from orch.cli import hub as H

    prev = {"count": 250, "new": 250, "revoked": 0, "more": 150, "plan": "p" * 43, "items": []}
    assert H._confirm_word(prev) == "all 250" and H._confirm_word({**prev, "more": 0}) == "share"  # noqa: SLF001


# --- C8 goldens: keys rotate and hub history (exact output; both are host ops, so no registry golden file) -----------

HIST = {
    "hub_id": V["ids"]["hub"],
    "first": [{"type": "policy.changed", "gates": ["plan"], "at": "2026-10-22T10:00:00Z", "user": "u_1",
               "channel": "c_1", "revert": {"op": "policy.set"}}],
    "stuck": [{"ticket": "ORCH-0001", "gate": "plan", "needed": 2, "can_still_give": 1}],
    "other": [{"type": "gate.approved", "ticket": "ORCH-0001", "at": "2026-10-22T11:00:00Z", "user": "u_1",
               "channel": "c_1", "factor": "password+totp"}],
}  # fmt: skip


def test_keys_rotate_golden(host):
    host.reply["keys.rotate"] = {"epoch_before": 1, "epoch": 2, "rotated": True, "path": "local"}
    code, out = run(["rotate"], {})
    assert code == 0 and host.signed == [] and host.called == [("keys.rotate", {})]
    assert out == "ok new workspace key: epoch 2 (made here; nothing was sent to any hub)\n"
    host.reply["keys.rotate"] = {"epoch_before": 1, "epoch": 1, "rotated": False, "path": "requested"}
    assert run(["rotate"], {})[1] == (
        "ok a new workspace key is requested; it is made with the next membership update (orch hub status)\n"
    )
    code, out = run(["rotate"], {}, as_json=True)
    assert code == 0 and json.loads(out)["data"] == host.reply["keys.rotate"]


@pytest.mark.parametrize("code", ["keys.joined", "keys.no_epoch"])
def test_keys_rotate_refusals_golden(host, code):
    host.reply["keys.rotate"] = OrchError(code, "refused")
    rc, out = run(["rotate"], {}, as_json=True)
    assert rc != 0 and json.loads(out)["code"] == code


def test_hub_history_golden(host):
    host.reply["hub.history"] = HIST
    code, out = run(["history", "--since", "2026-10-20T00:00:00Z"], {})
    assert code == 0 and host.called == [("hub.history", {"since": "2026-10-20T00:00:00Z"})]
    hid = V["ids"]["hub"]
    assert out.splitlines() == [
        f"What hub {hid} did in this workspace:",
        "  [policy.changed] plan at 2026-10-22T10:00:00Z by u_1 via c_1",
        out.splitlines()[2],  # the undo line: its wording is L2-C8i-6 (hardening), not pinned here
        "  stuck: ORCH-0001 plan needs 2, 1 can give it here",
        "  gate.approved ORCH-0001 at 2026-10-22T11:00:00Z by u_1 via c_1 (password+totp)",
        "What the hub has already read stays with it: the new workspace key only protects what comes next.",
    ]
    assert out.splitlines()[2].startswith("    undo here: orch ")
    code, out = run(["history"], {}, as_json=True)
    assert code == 0 and json.loads(out)["data"] == HIST
