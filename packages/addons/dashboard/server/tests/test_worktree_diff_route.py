"""Focus phase 2, P2-5: the live worktree diff route: host op and cap, closed query, projection to closed fields."""

import pytest
from conftest import ORIGIN

from orch_dashboard import hostlink


def hdr(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "Content-Type": "application/json"}


class Host:
    welcome = {"peer": {"person": "p_me"}}

    def __init__(self):
        self.calls = []
        self.answer = {}
        self.listed = True

    def supports(self, op, cap):
        return self.listed and op == "worktree.diff" and cap == "worktree/1"

    def call(self, op, args=None, **kw):
        self.calls.append((op, args))
        return self.answer


@pytest.fixture
def host(gateway, monkeypatch):
    fake = Host()
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    return fake


def url(stub, key="DEMO-1", query=""):
    return f"/api/w/{stub.ws}/tickets/{key}/worktree-diff{query}"


def test_the_op_is_ours_under_the_worktree_cap():
    assert "worktree.diff" in hostlink.OPS and "worktree.diff" in hostlink.WORKTREE_OPS


def test_the_route_projects_to_closed_fields(client, token, stub, host):
    host.answer = {
        "ticket": "DEMO-1", "repo": "proj", "available": True, "path_label": "proj/ab12-DEMO-1", "branch": "feat/x",
        "head": "a" * 40, "checked_at": "2026-10-26T10:00:00Z", "truncated": True, "secret": "x",
        "files": [
            {"path": "a.py", "status": "M", "added": 2, "removed": 1, "binary": False, "junk": 1},
            {"path": "b.bin", "status": "?", "added": -4, "removed": "x", "binary": True},
            {"path": "c", "status": "Z"},
            "nope",
        ],
        "unified": "diff --git a/a.py b/a.py\n",
    }  # fmt: skip
    res = client.get(url(stub, query="?repo=proj"), headers=hdr(token))
    assert res.status_code == 200, res.text
    assert host.calls == [("worktree.diff", {"ticket": "DEMO-1", "repo": "proj"})]
    got = res.json()
    assert "secret" not in res.text and "junk" not in res.text
    assert got["available"] is True and got["truncated"] is True and got["unified"].startswith("diff --git")
    assert [f["path"] for f in got["files"]] == ["a.py", "b.bin"]
    assert got["files"][1]["added"] == 0 and got["files"][1]["removed"] == 0 and got["files"][1]["binary"] is True


def test_unavailable_is_data(client, token, stub, host):
    host.answer = {"available": False, "reason": "no worktree is registered for this ticket"}
    got = client.get(url(stub), headers=hdr(token)).json()
    assert got["available"] is False and got["reason"].startswith("no worktree") and got["files"] == []
    assert host.calls == [("worktree.diff", {"ticket": "DEMO-1"})]
    host.listed = False
    assert client.get(url(stub), headers=hdr(token)).json()["available"] is False
    assert len(host.calls) == 1


@pytest.mark.parametrize("query", ["?repo=../x", "?repo=A", "?x=1"])
def test_the_query_is_closed(client, token, stub, host, query):
    assert client.get(url(stub, query=query), headers=hdr(token)).status_code == 400
    assert host.calls == []
