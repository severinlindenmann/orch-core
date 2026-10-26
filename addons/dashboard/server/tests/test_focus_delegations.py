"""Focus phase 2, P2-2: the delegations route: host op and cap, projection to closed fields."""

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
        return self.listed and op == "delegation.list" and cap == "focus/1"

    def call(self, op, args=None, write=False, idem=None, **kw):
        self.calls.append((op, args))
        return self.answer


@pytest.fixture
def host(gateway, monkeypatch):
    fake = Host()
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    return fake


def url(stub, key="DEMO-12"):
    return f"/api/w/{stub.ws}/tickets/{key}/delegations"


def test_the_op_is_ours_under_cap_focus_1():
    assert hostlink.OPS["delegation.list"] == 1 and "delegation.list" in hostlink.FOCUS_OPS


def test_the_rows_are_projected_to_closed_fields(client, token, stub, host):
    host.answer = {
        "secret": "x",
        "sessions": [
            {
                "run": "r_A",
                "session": "s_1",
                "harness": "claude-code",
                "model": "sonnet",
                "state": "running",
                "policy": {"delegate": False},
                "files": ["a.py", 3],
                "extra": 1,
                "visibility": {"available": True},
                "children": [
                    {
                        "id": "cc:agent-a1",
                        "parent_run": "r_A",
                        "title": "Write tests",
                        "spawned_by": "agent",
                        "harness": "claude-code",
                        "model": "sonnet",
                        "depth": 1,
                        "state": "done",
                        "started_at": "t0",
                        "ended_at": "t1",
                        "run": None,
                        "transcript": {"run": "r_A", "child": "agent-a1", "x": 1},
                        "files": ["t.py"],
                        "junk": 1,
                    },
                    {
                        "id": "run:r_B",
                        "title": "Human",
                        "spawned_by": "bogus",
                        "state": "weird",
                        "transcript": {"run": "r_B"},
                    },
                    {"no_id": True},
                    "not a row",
                ],
            },
            {"run": "r_C", "harness": "codex", "visibility": {"available": False, "reason": "no"}, "children": []},
            {"no_run": 1},
        ],
    }
    res = client.get(url(stub), headers=hdr(token))
    assert res.status_code == 200, res.text
    assert host.calls == [("delegation.list", {"ticket": "DEMO-12"})]
    got = res.json()
    assert "secret" not in res.text and "junk" not in res.text and "extra" not in res.text
    assert got["supported"] is True and [s["run"] for s in got["sessions"]] == ["r_A", "r_C"]
    a = got["sessions"][0]
    assert a["files"] == ["a.py"] and a["visibility"] == {"available": True, "reason": None}
    assert [c["id"] for c in a["children"]] == ["cc:agent-a1", "run:r_B"]
    assert a["children"][0]["transcript"] == {"run": "r_A", "child": "agent-a1"} and a["children"][0]["state"] == "done"
    assert a["children"][1]["spawned_by"] == "agent" and a["children"][1]["state"] == "running"
    assert got["sessions"][1]["visibility"] == {"available": False, "reason": "no"}


def test_a_host_without_the_cap_is_data_not_an_error(client, token, stub, host):
    host.listed = False
    got = client.get(url(stub), headers=hdr(token)).json()
    assert got == {"ticket": "DEMO-12", "sessions": [], "supported": False} and host.calls == []


def test_a_bad_ticket_key_is_refused(client, token, stub, host):
    assert client.get(url(stub, key="../x"), headers=hdr(token)).status_code in (400, 404)
    assert host.calls == []
