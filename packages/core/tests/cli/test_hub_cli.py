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
HUB = V["ids"]["hub"]
INV = f"{HUB}:inv-1"  # D12c: <hub_id>:<code>


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
    code, out = run(["join", INV, "--hub-card", card_file, "--owner-user-id", USER], {})
    assert code == 0 and "signed" in out and "ok hub joined" in out and "sees only what you share" in out
    ((op, args),) = host.signed
    assert op == "hub.join" and args["mode"] == "selective" and args["hub_card"] == CARD
    assert args["aliases"] == [{"person_id": "4c" * 16, "user_id": USER}] and args["hub_id"] == HUB
    assert host.called == [("hub.connect", {"invite": "inv-1", "hub_card": CARD})]


def test_join_with_a_link_reads_it_on_the_host_then_signs_and_connects(host):
    host.reply["hub.link"] = {"hub_id": HUB, "hub_card": CARD, "code": "inv-1", "endpoint": "10.0.0.5:8105"}
    host.reply["hub.connect"] = {"hub_id": HUB, "status": "bound", "binding_seq": 1}
    link = f"http://h:1/c#h1.{HUB}.inv-1.10.0.0.5:8105"
    code, out = run(["join", "--link", link], {})
    assert code == 0 and "ok hub joined" in out
    ((op, args),) = host.signed
    assert op == "hub.join" and args["hub_card"] == CARD and args["hub_id"] == HUB and args["mode"] == "selective"
    assert host.called == [
        ("hub.link", {"link": link}),
        ("hub.connect", {"invite": "inv-1", "hub_card": CARD, "addr": "10.0.0.5:8105"}),
    ]


def test_a_link_takes_no_invite_or_card(offline, card_file):
    for argv in (["join", "x", "--link", "http://h/c#x"], ["join", "--link", "http://h/c#x", "--hub-card", card_file],
                 ["leave", "--link", "http://h/c#x"]):  # fmt: skip
        code, out = run(argv, {})
        assert code != 0 and "--link belongs to" in out


def test_a_bare_code_needs_hub_id_and_the_hub_only_ever_sees_the_code(host, card_file):
    host.reply["hub.connect"] = {"hub_id": HUB, "status": "bound", "binding_seq": 1}
    code, out = run(["join", "inv-1", "--hub-card", card_file], {})
    assert code == 5 and "names no hub" in out and host.signed == [] and host.called == []
    code, _ = run(["join", "inv-1", "--hub-id", HUB, "--hub-card", card_file], {})
    assert code == 0 and host.signed[0][1]["hub_id"] == HUB and host.called[0][1]["invite"] == "inv-1"


def test_a_malformed_or_conflicting_hub_id_is_refused_before_signing(host, card_file):
    for argv in (
        ["join", "inv-1", "--hub-id", "XYZ", "--hub-card", card_file],
        ["join", INV, "--hub-id", "0" * 32, "--hub-card", card_file],
        ["join", f"{HUB.upper()}:inv-1", "--hub-card", card_file],
    ):
        code, out = run(argv, {})
        assert code == 5 and "error invalid.input" in out
    assert host.signed == [] and host.called == []


def test_join_without_a_terminal_signs_nothing(host, card_file):
    host.tty = False
    code, out = run(["join", "inv-1", "--hub-card", card_file], {})
    assert code == 3 and "no terminal" in out and host.signed == [] and host.called == []


def test_a_hub_that_is_down_after_the_signature_says_how_to_finish(host, card_file):
    host.reply["hub.connect"] = OrchError("hub.unreachable", "down")
    code, out = run(["join", INV, "--hub-card", card_file], {})
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
    code, out = run(["join", INV, "--hub-card", card_file, "--mode", "full"], {})
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
    log = SimpleNamespace(called=[], asked=[], yes=True, answers={})
    preview = {"count": 1, "new": 1, "revoked": 0, "more": 0, "plan": "p" * 43,
               "items": [{"logical": "ticket:t", "kind": "ticket", "line": "ticket K-1: Public note"}]}  # fmt: skip
    reply = {
        "share.preview": preview,
        "share.rule_add": {"note": "changed", "synced": True, "put": 1, "revoke": 0, "kept": 0},
        "share.rule_remove": {"note": "changed", "synced": True, "put": 0, "revoke": 1, "kept": 0},
        "share.revoke": {"note": "withdrawn", "synced": True, "put": 0, "revoke": 1, "kept": 0},
        "share.sync": {"synced": False, "pending": "hub_unreachable"},
        "share.audience": {
            "people": [
                {"user_id": "a" * 32, "name": "Ben"},
                {"user_id": "b" * 32, "name": "Ben"},
                {"user_id": "c" * 32, "name": "Cleo"},
            ],
            "teams": [{"group": "team-b", "members": 2}],
        },
        "share.rules": {
            "rules": [{"kind": "tag", "tag": "x", "count": 1, "describes": "d"}],
            "shared": 1,
            "withdrawn": [],
        },  # fmt: skip
    }
    monkeypatch.setattr(hub, "_target", lambda env: object())
    monkeypatch.setattr(
        hub, "_call", lambda t, op, args, env: (log.called.append((op, args)), log.answers.get(op, reply[op]))[1]
    )

    monkeypatch.setattr(
        __import__("orch.ops.human", fromlist=["review_prompt"]),
        "review_prompt",
        lambda prompt, verb="sign": (log.asked.append(prompt), verb == "continue" and log.yes)[1],  # D4: unsigned
    )
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
    (prompt,) = share_host.asked
    assert "ticket K-1: Public note" in prompt and "cannot be taken back" in prompt
    rule = {"kind": "tag", "tag": "x"}
    assert share_host.called == [
        ("share.preview", {"add": rule}),
        ("share.rule_add", {"rule": rule, "expect": "p" * 43}),
    ]


def test_an_unconfirmed_rule_change_sends_nothing(share_host):
    share_host.yes = False
    code, _ = run(["share", "rule", "remove", "--tag", "x"], {}, as_json=True)
    assert code == 3 and [op for op, _ in share_host.called] == ["share.preview"]


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
    assert code == 0 and "ticket K-1: Public note" in share_host.asked[0]
    assert share_host.called == [("share.preview", {}), ("share.sync", {"expect": "p" * 43})]
    share_host.called.clear()
    share_host.yes = False
    assert run(["share", "sync"], {}, as_json=True)[0] == 3 and [op for op, _ in share_host.called] == ["share.preview"]


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
    assert out.splitlines()[2].startswith(
        "    undo here: open the dashboard (Settings)"
    )  # L2-C8i-6: no JSON pretending to be a command
    code, out = run(["history"], {}, as_json=True)
    assert code == 0 and json.loads(out)["data"] == HIST


# --- R6 (#1594): orch hub network ---------------------------------------------------------------------------------


def test_network_show_set_and_clear(host, monkeypatch):
    host.reply["hub.network"] = {"proxy": None, "user": None, "has_password": False, "broken": False}
    code, out = run(["network"], {})
    assert code == 0 and "directly" in out and host.called == [("hub.network", {})]
    host.reply["hub.network_set"] = {
        "proxy": "https://p.office:3128",
        "user": "a",
        "has_password": True,
        "broken": False,
    }
    monkeypatch.setattr(hub, "_read_password", lambda: "pw")
    code, out = run(["network", "set-proxy", "https://p.office:3128", "--proxy-user", "a"], {})
    assert code == 0 and "pw" not in out and "p.office:3128" in out
    assert host.called[-1] == ("hub.network_set", {"proxy": "https://p.office:3128", "user": "a", "password": "pw"})
    run(["network", "clear-proxy"], {})
    assert host.called[-1] == ("hub.network_set", {"proxy": None})


def test_network_refuses_agents_and_bad_shapes(host):
    code, out = run(["network"], {"ORCH_SESSION": "x"})
    assert code != 0 and host.called == []
    for bad in (["network", "set-proxy"], ["network", "x"], ["network", "--proxy-user", "a"]):
        assert run(bad, {})[0] == 2


def test_share_people_lists_the_hub_people(share_host):
    code, out = run(["share", "people"], {})
    assert code == 0 and "Cleo" in out and "team team-b" in out and share_host.called == [("share.audience", {})]


def test_with_resolves_a_name_or_id_into_the_signed_audience(share_host):
    code, _ = run(["share", "rule", "add", "--ticket", "K-1", "--with", "Cleo", "--team", "team-b"], {}, as_json=True)
    rule = {"kind": "ticket", "ticket": "K-1", "audience": {"users": ["c" * 32], "groups": ["team-b"]}}
    assert code == 0 and ("share.rule_add", {"rule": rule, "expect": "p" * 43}) in share_host.called


def test_an_ambiguous_or_unknown_person_reaches_no_rule_change(share_host):
    for who in ("Ben", "Nobody"):
        code, _ = run(["share", "rule", "add", "--ticket", "K-1", "--with", who], {}, as_json=True)
        assert code == 5
    assert all(op != "share.rule_add" for op, _ in share_host.called)


def test_the_hubs_people_are_cleaned_before_anything_is_printed(share_host):
    share_host.answers["share.audience"] = {
        "people": [{"user_id": "a" * 32, "name": "\x1b[31mBen\u202e\nEvil"}, {"user_id": "zz\x1b[2J", "name": "X"}],
        "teams": [
            {"group": "t\x1b[0m", "members": 7},
            {"group": "ok", "members": True},
            {"group": "fine", "members": 3},
            {"group": "x" * 60, "members": 1},
        ],
    }
    code, out = run(["share", "people"], {})
    assert code == 0 and "\x1b" not in out and "\u202e" not in out and "Evil" in out and "zz" not in out
    assert "team fine (3 people)" in out and "team ok" not in out and "7" not in out and "xxxx" not in out


def test_two_people_with_one_name_are_refused_with_both_ids_listed(share_host):
    code, out = run(["share", "rule", "add", "--ticket", "K-1", "--with", "Ben"], {}, as_json=True)
    assert code == 5 and "a" * 32 in out and "b" * 32 in out and "--with ID" in out
    assert all(op != "share.rule_add" for op, _ in share_host.called)


def test_one_name_shows_the_short_id_in_the_review_and_signs_only_the_id(share_host):
    share_host.answers["share.preview"] = {
        **share_host.answers.get(
            "share.preview", {"count": 1, "new": 1, "revoked": 0, "more": 0, "plan": "p" * 43, "items": []}
        ),
        "review": {"readers": {"people": ["Cleo"], "teams": []}},
    }
    code, _ = run(["share", "rule", "add", "--ticket", "K-1", "--with", "Cleo"], {}, as_json=True)
    prompt = share_host.asked[0]
    assert code == 0 and "You chose: Cleo (..." + "c" * 8 + ")" in prompt
    assert prompt.index("You chose") < prompt.index("Readers (from the hub, not signed): Cleo")
    rule = {"kind": "ticket", "ticket": "K-1", "audience": {"users": ["c" * 32], "groups": []}}
    assert ("share.rule_add", {"rule": rule, "expect": "p" * 43}) in share_host.called


def test_a_team_is_matched_on_its_raw_safe_name_and_an_unsafe_one_is_never_signed(share_host):
    code, _ = run(["share", "rule", "add", "--ticket", "K-1", "--team", "team-b"], {}, as_json=True)
    rule = {"kind": "ticket", "ticket": "K-1", "audience": {"users": [], "groups": ["team-b"]}}
    assert code == 0 and ("share.rule_add", {"rule": rule, "expect": "p" * 43}) in share_host.called
    share_host.answers["share.audience"] = {"people": [], "teams": [{"group": "x" * 60, "members": 1}]}
    code, _ = run(["share", "rule", "add", "--ticket", "K-1", "--team", "x" * 40], {}, as_json=True)
    assert code == 5 and len([1 for op, _ in share_host.called if op == "share.rule_add"]) == 1
