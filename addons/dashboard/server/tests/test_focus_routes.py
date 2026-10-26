"""Focus phase 2, P2-1: the transcript route: host op and cap, closed query, projection to plain fields."""

import pytest
from conftest import ORIGIN

from orch_dashboard import hostlink

RUN = "r_01J0000000000000000000000A"


def hdr(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "Content-Type": "application/json"}


class Host:
    welcome = {"peer": {"person": "p_me"}}

    def __init__(self):
        self.calls = []
        self.answer = {}
        self.listed = True

    def supports(self, op, cap):
        return self.listed and op == "run.transcript" and cap == "focus/1"

    def call(self, op, args=None, write=False, idem=None, **kw):
        self.calls.append((op, args))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def host(gateway, monkeypatch):
    fake = Host()
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    return fake


def url(stub, run=RUN, query=""):
    return f"/api/w/{stub.ws}/runs/{run}/transcript{query}"


def test_the_op_is_ours_under_cap_focus_1():
    assert "focus/1" in hostlink.CAPS and hostlink.OPS["run.transcript"] == 1


def test_the_route_passes_the_cursor_and_projects_the_items(client, token, stub, host):
    host.answer = {
        "run": RUN,
        "harness": "claude-code",
        "available": True,
        "next": "o:20411",
        "eof": True,
        "skipped": 3,
        "secret_extra": "x",
        "items": [
            {"id": "a", "role": "user", "kind": "text", "text": "hi", "ts": "2026-10-26T10:00:00Z", "junk": 1},
            {
                "id": "b",
                "role": "assistant",
                "kind": "tool",
                "text": "Edit a.py",
                "ts": None,
                "tool": {"name": "Edit", "call": "toolu_1", "x": 1},
                "files": ["a.py", 7],
            },
            {"id": "c", "role": "tool", "kind": "result", "text": "ok", "error": True},
            {"id": "d", "role": "assistant", "kind": "question", "text": "Which?", "options": ["A", 3, "B"]},
            {"id": "e", "role": "robot", "kind": "text", "text": "dropped"},
            "not an item",
        ],
    }
    res = client.get(url(stub, query="?after=o:18234&limit=50"), headers=hdr(token))
    assert res.status_code == 200, res.text
    assert host.calls == [("run.transcript", {"run": RUN, "after": "o:18234", "limit": 50})]
    got = res.json()
    assert got["available"] is True and got["next"] == "o:20411" and got["eof"] is True
    assert "secret_extra" not in res.text and "junk" not in res.text and "skipped" not in got
    assert [i["id"] for i in got["items"]] == ["a", "b", "c", "d"]
    assert got["items"][1]["tool"] == {"name": "Edit", "call": "toolu_1"} and got["items"][1]["files"] == ["a.py"]
    assert got["items"][2]["error"] is True and got["items"][3]["options"] == ["A", "B"]


def test_unavailable_is_data(client, token, stub, host):
    host.answer = {"run": RUN, "harness": "codex", "available": False, "reason": "transcript not found yet",
                   "items": [], "next": None, "eof": True}  # fmt: skip
    got = client.get(url(stub), headers=hdr(token)).json()
    assert got["available"] is False and got["reason"] == "transcript not found yet" and got["items"] == []
    host.listed = False
    got = client.get(url(stub), headers=hdr(token)).json()
    assert got["available"] is False and host.calls == [("run.transcript", {"run": RUN})]


@pytest.mark.parametrize("query", ["?after=7", "?limit=0", "?limit=201", "?child=../x", "?x=1"])
def test_the_query_is_closed(client, token, stub, host, query):
    assert client.get(url(stub, query=query), headers=hdr(token)).status_code == 400
    assert host.calls == []


def test_a_bad_run_id_is_refused(client, token, stub, host):
    assert client.get(url(stub, run="nope"), headers=hdr(token)).status_code == 400
