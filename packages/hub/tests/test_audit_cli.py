"""The dev CLIs write the audit chain; `inspect` shows it; a workspace can verify it from the files alone."""

from __future__ import annotations

import json
import os
import stat

from test_spaces_cli import ADMIN, state  # noqa: F401

from orch_hub import cli
from orch_hub.contract import audit as AU
from orch_hub.contract import hub as H
from orch_hub.contract import vectorgen as G
from orch_hub.inspect import inspect


def run(st, capsys, group, *argv):
    rc = cli.main([group, "--state", str(st), *argv])
    out = capsys.readouterr()
    assert rc == 0, out.err
    return json.loads(out.out)


def test_group_grant_and_space_commands_are_audited_and_the_chain_verifies(state, capsys):  # noqa: F811
    st, keys = state
    uid = run(st, capsys, "users", "add", "ann")["user_id"]
    run(st, capsys, "groups", "add", "devs")
    run(st, capsys, "groups", "add-member", "devs", uid)
    run(st, capsys, "grants", "set", G.WS_A, "devs", "edit")
    run(st, capsys, "grants", "revoke", G.WS_A, "devs")
    run(st, capsys, "spaces", "create", "--name", "x", "--workspace", G.WS_A, "--as", ADMIN)
    a = inspect(st)["audit"]
    assert [r["record"]["o"]["type"] for r in a["records"]] == ["membership", "grant", "grant", "space"]
    assert a["head"]["seq"] == 4
    held = H.verify_card(keys.card(), now_ms=keys._now(), dev_mode=True, pinned_root=keys.hk_root_pub)
    head = None
    for r in a["records"]:
        o, ops = AU.verify_record_ex(r["record"], held, dev_mode=True, now_ms=keys._now(), event=r["event"])
        head = AU.apply_record(head, r["record"], o, op_seq=ops, current_op_seq=held["op_cert"]["op_seq"])
    assert head["hash"] == a["head"]["hash"]
    assert a["records"][1]["event"]["actor_user_id"] == "0" * 32  # made at the hub's terminal
    assert stat.S_IMODE(os.stat(st / "audit.db").st_mode) == 0o600


def test_a_refused_command_leaves_no_record(state, capsys):  # noqa: F811
    st, _ = state
    run(st, capsys, "groups", "add", "devs")
    rc = cli.main(["grants", "--state", str(st), "revoke", G.WS_A, "devs"])  # not granted
    capsys.readouterr()
    assert rc == 1
    assert inspect(st)["audit"]["records"] == []


def test_groups_remove_and_users_remove_through_the_cli_are_audited(state, capsys):  # noqa: F811
    st, _ = state
    uid = run(st, capsys, "users", "add", "ann")["user_id"]
    run(st, capsys, "groups", "add", "devs")
    run(st, capsys, "groups", "add-member", "devs", uid)
    run(st, capsys, "grants", "set", G.WS_A, "devs", "edit")
    n = len(inspect(st)["audit"]["records"])
    assert cli.main(["groups", "--state", str(st), "remove", "devs"]) == 0
    capsys.readouterr()
    evs = [r["event"] for r in inspect(st)["audit"]["records"][n:]]
    assert sorted((e["workspace_id"] if "role" in e else e["user_id"]) for e in evs) == sorted([G.WS_A, uid])
    run(st, capsys, "groups", "add", "ops")
    run(st, capsys, "groups", "add-member", "ops", uid)
    n = len(inspect(st)["audit"]["records"])
    assert cli.main(["users", "--state", str(st), "remove", uid]) == 0
    capsys.readouterr()
    (e,) = [r["event"] for r in inspect(st)["audit"]["records"][n:]]
    assert (e["group"], e["user_id"], e["added"]) == ("ops", uid, False)
