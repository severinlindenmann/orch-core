"""Sharing can be taken away from the dashboard with one click: ``share.revoke`` and ``share.rule_remove`` are unsigned
owner actions like ``workspace.cosmetic.set``. The dashboard's workspace link is a column-P peer whose person is the
session's person; the host checks the owner role, refuses agents and other members, rate-limits the two ops and writes
a security-log line naming the person. Nothing else about the share ops changes (the CLI keeps its own confirmation:
tests/cli/test_hub_cli.py)."""

from __future__ import annotations

import pytest

from orch.host import securitylog, shareops
from tests.host import test_share_ops as S
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import err
from tests.host.test_share_ops import agent, labelled, req, rule_op

lab, served, human, selective = S.lab, S.served, S.human, S.selective
dev_workspace, keystore = S.dev_workspace, S.keystore


@pytest.fixture
def log(monkeypatch):
    lines: list[tuple[str, dict]] = []
    real = securitylog.record

    def record(state_dir, host_uid, at, event, **fields):
        lines.append((event, fields))
        return real(state_dir, host_uid, at, event, **fields)

    monkeypatch.setattr(securitylog, "record", record)
    return lines


def test_the_reducing_ops_are_exactly_revoke_and_rule_remove():
    assert shareops.REDUCE_OPS == {"share.revoke", "share.rule_remove"}
    assert shareops.REDUCE_OPS < set(shareops.SHARE_OPS) and not shareops.REDUCE_OPS & shareops.SHARE_READS


def test_the_owner_revokes_and_removes_a_rule_as_their_person_with_a_log_line(served, selective, log):
    _, key = labelled(served, "Public note", "public")
    rule = {"kind": "tag", "tag": "public"}
    ok(rule_op(served, "share.rule_add", rule))
    got = ok(req(served, "share.revoke", {"ref": key}))["data"]
    assert got["revoke"] == 1 and selective.count("share_revoke") == 1
    again = ok(req(served, "share.revoke", {"ref": key}))["data"]  # idempotent: the same answer, nothing new sent
    assert again["revoke"] == 0 and selective.count("share_revoke") == 1
    ok(rule_op(served, "share.rule_remove", rule))
    members = ok(served.client().req("member.list"))["data"]["members"]
    (person,) = [m["person"] for m in members if m["role"] == "owner"]
    reduce_lines = [(e, f) for e, f in log if e in shareops.REDUCE_OPS]
    assert [e for e, _ in reduce_lines] == ["share.revoke", "share.revoke", "share.rule_remove"]
    assert all(f["person"] == person for _, f in reduce_lines)
    add = [f for e, f in log if e == "share.rule_add"]
    assert add and "person" not in add[0]  # nothing else changes: an adding op's line is as it was


def test_ten_reducing_changes_a_minute(served, selective):
    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    for _ in range(shareops.REDUCE_RATE):
        ok(req(served, "share.revoke", {"ref": key}))
    res = req(served, "share.revoke", {"ref": key})
    assert err(res) == "host.busy"
    assert ok(req(served, "share.rules"))["data"]["rules"]  # reads are not limited


def test_an_agent_cannot_take_sharing_away_either(served, selective):
    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    before = list(selective.ops)
    remove = {"rule": {"kind": "tag", "tag": "public"}, "expect": "p" * 43}
    for op, args in (("share.revoke", {"ref": key}), ("share.rule_remove", remove)):
        assert err(agent(served, op, args)) in ("op.not_allowed", "human_only"), op
    assert selective.ops == before


def test_a_member_who_is_not_the_owner_is_refused(tmp_path):
    from tests.host.live_helpers import world

    wd = world(tmp_path, map_to="quinn")  # the socket's person is a plain member
    try:
        for op, args in (("share.revoke", {"ref": "K-1"}),
                         ("share.rule_remove", {"rule": {"kind": "needs_you"}, "expect": "p" * 43})):  # fmt: skip
            assert err(wd.client().req(op, args, idem=True)) == "role.denied", op
    finally:
        wd.close()
        wd.lab.close()
