"""Focus phase 2, P2-6: the link and detach routes: host ops under cap focus/1, closed answers."""

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
        return self.listed and op in ("run.link", "run.detach") and cap == "focus/1"

    def call(self, op, args=None, write=False, idem=None, **kw):
        self.calls.append((op, args, write))
        return self.answer


@pytest.fixture
def host(gateway, monkeypatch):
    fake = Host()
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    return fake


def test_the_ops_are_ours_under_cap_focus_1():
    for op in ("run.link", "run.detach"):
        assert hostlink.OPS[op] == 1 and op in hostlink.FOCUS_OPS


def test_link_calls_the_host_and_projects_the_answer(client, token, stub, host):
    host.answer = {"run": {"run": RUN, "ticket": "DEMO-12", "secret": "x"}, "extra": 1}
    res = client.post(f"/api/w/{stub.ws}/runs/{RUN}/link", json={"ticket": "DEMO-12"}, headers=hdr(token))
    assert res.status_code == 200, res.text
    assert res.json() == {"run": RUN, "ticket": "DEMO-12"} and "secret" not in res.text
    assert host.calls == [("run.link", {"run": RUN, "ticket": "DEMO-12"}, False)]


def test_detach_calls_the_host(client, token, stub, host):
    host.answer = {"run": {"run": RUN, "ticket": "DEMO-12", "parent_run": None}}
    res = client.post(f"/api/w/{stub.ws}/runs/{RUN}/detach", json={}, headers=hdr(token))
    assert res.status_code == 200 and res.json() == {"run": RUN, "ticket": "DEMO-12"}
    assert host.calls == [("run.detach", {"run": RUN}, False)]


def test_bad_input_and_a_host_without_the_cap_are_refused(client, token, stub, host):
    base = f"/api/w/{stub.ws}/runs"
    assert client.post(f"{base}/{RUN}/link", json={}, headers=hdr(token)).status_code in (400, 422)
    assert client.post(f"{base}/{RUN}/link", json={"ticket": "../x"}, headers=hdr(token)).status_code in (400, 422)
    assert client.post(f"{base}/{RUN}/link", json={"ticket": "DEMO-1", "x": 1}, headers=hdr(token)).status_code in (
        400,
        422,
    )
    assert client.post(f"{base}/not-a-run/detach", json={}, headers=hdr(token)).status_code in (400, 404, 422)
    host.listed = False
    assert client.post(f"{base}/{RUN}/detach", json={}, headers=hdr(token)).status_code == 404
    assert host.calls == []
