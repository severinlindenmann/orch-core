"""Focus phase 2, P2-4: the research routes: host op and cap, closed query, projection to plain fields."""

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
        return self.listed and op == "run.sources" and cap == "focus/1"

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


def url(stub, tail="", run=RUN):
    return f"/api/w/{stub.ws}/runs/{run}/sources{tail}"


def test_the_op_is_ours_under_cap_focus_1():
    assert hostlink.OPS["run.sources"] == 1 and "run.sources" in hostlink.FOCUS_OPS


def test_the_list_is_projected_to_closed_fields(client, token, stub, host):
    host.answer = {
        "secret": "x", "available": True,
        "sources": [
            {"id": "s_aaaaaaaaaa", "kind": "web", "url": "https://a.example/x", "host": "a.example", "title": None,
             "read_at": "2026-10-26T10:00:00Z", "has_extract": True, "evil": 1},
            {"id": "s_bbbbbbbbbb", "kind": "search", "query": "q", "read_at": None,
             "results": [{"title": "T", "url": "https://b.example"}, {"url": 5}, "junk"]},
            {"id": "s_cccccccccc", "kind": "file", "path": "docs/a.md", "read_at": None},
            {"id": "s_dddddddddd", "kind": "other"},
            "junk",
        ],
    }  # fmt: skip
    got = client.get(url(stub), headers=hdr(token)).json()
    assert host.calls == [("run.sources", {"run": RUN})]
    assert "secret" not in got and got["available"] is True
    web, search, file = got["sources"]
    assert web == {"id": "s_aaaaaaaaaa", "kind": "web", "read_at": "2026-10-26T10:00:00Z", "url": "https://a.example/x",
                   "host": "a.example", "title": None, "has_extract": True}  # fmt: skip
    assert search["results"] == [{"title": "T", "url": "https://b.example"}] and file["path"] == "docs/a.md"


def test_one_source_extract_and_the_closed_query(client, token, stub, host):
    host.answer = {"id": "s_aaaaaaaaaa", "url": "https://a.example/x", "extract": "text", "truncated": False, "x": 1}
    got = client.get(url(stub, "/s_aaaaaaaaaa?child=agent-ab1"), headers=hdr(token)).json()
    assert got == {"id": "s_aaaaaaaaaa", "url": "https://a.example/x", "extract": "text", "truncated": False}
    assert host.calls == [("run.sources", {"run": RUN, "source": "s_aaaaaaaaaa", "child": "agent-ab1"})]
    for bad in ("/nope", "/s_aaaaaaaaaa?child=../x", "?x=1"):
        assert client.get(url(stub, bad), headers=hdr(token)).status_code == 400


def test_a_host_without_the_cap_answers_unavailable(client, token, stub, host):
    host.listed = False
    got = client.get(url(stub), headers=hdr(token)).json()
    assert got["available"] is False and got["sources"] == [] and host.calls == []
