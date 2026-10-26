"""Worktrees (H11d): the host link's op list, the typed routes and their closed bodies, the projection (host text is
display-only; no signature for create/remove, the idem is the browser's)."""

import pytest
from conftest import ORIGIN

from orch_dashboard import hostlink
from orch_dashboard.worktrees import row

IDEM = "a_01M4Y46B485AXD4CBEJZ15Z1TV"
ROW = {
    "ticket": "DEMO-1",
    "repo": "billing",
    "branch": "feat/x",
    "path": "/w/billing.worktrees/0a1b2c3d-DEMO-1",
    "generation": 3,
    "created_at": "2026-10-18T08:00:00Z",
    "created_by": {"kind": "person", "id": "p_me"},
    "head": "a" * 40,
    "state": "ok",
    "dirty": 2,
    "checked_at": "2026-10-18T08:01:00Z",
}


def hdr(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "Content-Type": "application/json"}


class Host:
    welcome = {"peer": {"person": "p_me"}}

    def __init__(self):
        self.calls = []
        self.answers = {}

    def supports(self, op, cap):
        return op.startswith("worktree.") and cap == "worktree/1"

    def call(self, op, args=None, write=False, idem=None, **kw):
        self.calls.append((op, args, write, idem))
        answer = self.answers[op]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def host(gateway, monkeypatch):
    fake = Host()
    fake.answers["ticket.query"] = {"rows": [{"key": "DEMO-1", "title": "Fix billing"}], "next_page": None}
    monkeypatch.setattr(gateway, "call", lambda ws, op, args=None: fake.call(op, args))
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    return fake


def url(stub, path):
    return f"/api/w/{stub.ws}{path}"


def test_the_worktree_ops_are_ours_under_cap_worktree_1():
    assert "worktree/1" in hostlink.CAPS
    for op in ("worktree.list", "worktree.create", "worktree.remove"):
        assert hostlink.OPS[op] == 1 and op in hostlink.WORKTREE_OPS


def test_list_asks_for_a_check_and_projects_plain_fields(client, token, stub, host):
    host.answers["worktree.list"] = {"worktrees": [ROW]}
    res = client.get(url(stub, "/worktrees"), headers=hdr(token))
    assert res.status_code == 200, res.text
    assert host.calls[0] == ("worktree.list", {"check": True}, False, None)
    got = res.json()
    assert got["available"] is True
    assert got["rows"] == [
        {
            "ticket": "DEMO-1",
            "title": "Fix billing",
            "repo": "billing",
            "branch": "feat/x",
            "path": ROW["path"],
            "state": "ok",
            "dirty": 2,
            "checked": True,
            "created_at": "2026-10-18T08:00:00Z",
            "by": "person",
            "mine": True,
        }
    ]
    assert "head" not in res.text and "generation" not in res.text


def test_row_withholds_hidden_text_and_unknown_values():
    got = row({**ROW, "branch": "feat/‮x", "state": "weird", "dirty": -1, "created_by": {"kind": "robot"}}, "p_me")
    assert got["branch"] is None and got["state"] == "unknown" and got["dirty"] is None
    assert got["by"] is None and got["mine"] is False
    unchecked = row({**ROW, "dirty": None, "checked_at": None}, "p_me")
    assert unchecked["dirty"] is None and unchecked["checked"] is False


def test_create_and_remove_are_writes_with_the_browsers_idem(client, token, stub, host):
    host.answers["worktree.create"] = {"worktree": ROW}
    host.answers["worktree.remove"] = {"removed": True}
    res = client.post(
        url(stub, "/worktrees/create"), json={"ticket": "DEMO-1", "repo": "billing", "idem": IDEM}, headers=hdr(token)
    )
    assert res.status_code == 200, res.text
    assert host.calls[-1] == ("worktree.create", {"ticket": "DEMO-1", "base": "default", "repo": "billing"}, True, IDEM)
    assert res.json()["worktree"]["state"] == "ok"
    res = client.post(url(stub, "/worktrees/remove"), json={"ticket": "DEMO-1", "idem": IDEM}, headers=hdr(token))
    assert res.json() == {"removed": True}
    assert host.calls[-1] == ("worktree.remove", {"ticket": "DEMO-1"}, True, IDEM)


@pytest.mark.parametrize(
    "path,body",
    [
        ("create", {"ticket": "DEMO-1"}),
        ("create", {"ticket": "DEMO-1", "idem": "x"}),
        ("create", {"ticket": "../x", "idem": IDEM}),
        ("create", {"ticket": "DEMO-1", "idem": IDEM, "base": "main"}),
        ("create", {"ticket": "DEMO-1", "idem": IDEM, "path": "/etc"}),
        ("remove", {"ticket": "DEMO-1", "idem": IDEM, "force": True}),
        ("remove", {"ticket": "DEMO-1", "repo": "A b", "idem": IDEM}),
    ],
)
def test_bodies_are_closed_and_force_is_never_offered(client, token, stub, host, path, body):
    assert client.post(url(stub, f"/worktrees/{path}"), json=body, headers=hdr(token)).status_code == 400
    assert host.calls == []
