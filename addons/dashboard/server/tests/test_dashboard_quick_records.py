"""M6 uses the existing socket-pair host; no records tool is executed."""

import pytest
from conftest import ORIGIN

from orch_dashboard.errors import HostError


@pytest.fixture
def m6(gateway, stub, monkeypatch):
    link = gateway.enabled(stub.ws)
    link.welcome["caps"] += ["quick/1", "records/1", "run/1"]
    for op in ("quick.list", "records.status", "records.snapshot", "records.note", "records.export_check"):
        link.welcome["ops"][op] = 1
    original = link.call
    script = {"calls": [], "refusal": None, "runner": {"connected": False, "enabled": True, "terminals": False}}
    script["records"] = {
        "configured": True,
        "repo": {"state": "ok"},
        "pending": {"events": 3, "tickets": [{"key": "DEMO-0001", "events": 2}], "more": False, "workspace_events": 1},
        "last": None,
        "auto": {"on": False, "every_min": 5, "push": False},
        "remote": {"url": "https://git.example.test/records", "private": True, "export": "ok"},
        "last_noted": {"by": stub.person, "at": "2026-10-13T10:00:00Z"},
    }

    def call(op, args=None, **kwargs):
        script["calls"].append((op, args))
        if op == "quick.list":
            data = original("ticket.query", {"limit": 1})
            data["rows"][0]["quick"] = {"commits": None, "files": None, "over": None}
            return {**data, "next_page": "opaque.page"}
        if op == "records.status":
            if script["refusal"]:
                raise script["refusal"]
            return script["records"]
        data = original(op, args, **kwargs)
        if op == "whoami":
            data["runner"] = script["runner"]
        return data

    monkeypatch.setattr(link, "call", call)
    return script, link


def headers(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


def test_quick(client, token, stub, m6):
    query = "max_commits=2&max_files=8&status=in-progress&limit=7&page=abc"
    got = client.get(f"/api/w/{stub.ws}/quick?{query}", headers=headers(token))
    assert got.status_code == 200
    body = got.json()
    assert body["available"] is True
    assert body["rows"][0]["quick"] == {"commits": None, "files": None, "over": None}
    assert body["next_page"] == "opaque.page"
    expected = {"max_commits": 2, "max_files": 8, "status": ["in_progress"], "limit": 7, "page": "abc"}
    assert ("quick.list", expected) in m6[0]["calls"]
    assert {"quick", "records"} <= set(client.get("/api/session", headers=headers(token)).json()["features"])


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=201",
        "max_commits=0",
        "max_commits=101",
        "max_files=1001",
        "max_files=0",
        "status=unknown",
        "limit=1.5",
        "limits=foo",
        "page=" + "a" * 513,
        "extra=1",
    ],
)
def test_quick_bounds(client, token, stub, m6, query):
    assert client.get(f"/api/w/{stub.ws}/quick?{query}", headers=headers(token)).status_code == 400
    assert not any(op == "quick.list" for op, _ in m6[0]["calls"])


@pytest.mark.parametrize(
    "cap,op,path", [("quick/1", "quick.list", "quick"), ("records/1", "records.status", "records")]
)
@pytest.mark.parametrize("missing", ["cap", "op"])
def test_missing_cap_or_op(client, token, stub, m6, cap, op, path, missing):
    script, link = m6
    if missing == "cap":
        link.welcome["caps"].remove(cap)
    else:
        del link.welcome["ops"][op]
    assert client.get(f"/api/w/{stub.ws}/{path}", headers=headers(token)).json()["available"] is False
    assert path not in client.get("/api/session", headers=headers(token)).json()["features"]
    assert not any(name == op for name, _ in script["calls"])


def test_records_adapter_and_role_refusal(client, token, stub, m6, monkeypatch):
    script, _ = m6
    script["records"]["remote"]["url"] = "https://git.example.test/\u202espoof"
    script["records"]["repo"]["problem"] = "unsafe\x00text"
    base = f"/api/w/{stub.ws}/records"
    got = client.get(base, headers=headers(token)).json()
    assert got["remote"]["url"] is None and got["remote"]["url_hidden"] is True
    assert got["repo"]["problem"] is None and got["last"] is None
    script["refusal"] = HostError({"code": "role.denied", "message": "owner or maintainer", "retryable": False}, 3)
    monkeypatch.setattr(client.app.state.records_child, "run", lambda *a, **k: pytest.fail("must not spawn"))
    for suffix in ("", "/commit", "/auto-off"):
        if not suffix:
            res = client.get(base, headers=headers(token))
        else:
            res = client.post(
                base + suffix, json={"push": False} if suffix == "/commit" else None, headers=headers(token)
            )
        assert res.status_code == 403 and res.json()["error"]["code"] == "role.denied"


def test_records_actions_security_and_bodies(client, token, stub, m6, monkeypatch, gateway):
    gateway.run = gateway.state / "custom-run"
    calls = []
    monkeypatch.setattr(client.app.state.records_child, "run", lambda *a, **k: calls.append((a, k)) or {"ok": True})
    base = f"/api/w/{stub.ws}/records"
    for path in ("quick", "records"):
        assert client.get(f"/api/w/{stub.ws}/{path}").status_code == 401
    for suffix, body in [("commit", {"push": True}), ("auto-off", None)]:
        path = base + "/" + suffix
        assert client.post(path, json=body, headers={"Origin": ORIGIN}).status_code == 401
        assert client.post(path, json=body, headers={"Authorization": f"Bearer {token}"}).status_code == 403
        evil = {**headers(token), "Origin": "https://evil.example.test"}
        assert client.post(path, json=body, headers=evil).status_code == 403
        evil = {**headers(token), "Host": "evil.example.test"}
        assert client.post(path, json=body, headers=evil).status_code == 421
        assert client.post(path, json=body, headers=headers(token)).status_code == 200
    assert calls == [
        ((stub.ws,), {"push": True, "auto_off": False, "state": gateway.state, "run_dir": gateway.run}),
        ((stub.ws,), {"push": False, "auto_off": True, "state": gateway.state, "run_dir": gateway.run}),
    ]
    for body in ({}, {"push": "true"}, {"push": 1}, {"push": False, "argv": []}, {"push": None}):
        assert client.post(base + "/commit", json=body, headers=headers(token)).status_code == 400
    assert client.post(base + "/auto-off", json={}, headers=headers(token)).status_code == 400
    res = client.post(
        base + "/commit", content=b"x" * 1025, headers={**headers(token), "Content-Type": "application/json"}
    )
    assert res.status_code == 413
    del m6[1].welcome["ops"]["records.snapshot"]
    assert client.post(base + "/commit", json={"push": False}, headers=headers(token)).status_code == 501
    assert len(calls) == 2


@pytest.mark.parametrize("supported", [True, False])
def test_runner_card_and_identity(client, token, stub, m6, supported):
    if not supported:
        m6[1].welcome["caps"].remove("run/1")
    expected = m6[0]["runner"] if supported else None
    assert client.get("/api/workspaces", headers=headers(token)).json()[0]["runner"] == expected
    assert client.get("/api/me", headers=headers(token)).json()[0]["runner"] == expected
    identity = client.get(f"/api/w/{stub.ws}/identity", headers=headers(token)).json()
    assert identity["runner"] == identity["me"]["runner"] == expected


def test_runner_disabled_detail_survives(client, token, stub, m6):
    error = {"code": "runner.disabled", "message": "disabled", "retryable": False, "detail": {"missing": "machine"}}
    m6[0]["refusal"] = HostError(error, 3)
    got = client.get(f"/api/w/{stub.ws}/records", headers=headers(token))
    assert got.status_code == 409 and got.json()["error"]["detail"] == {"missing": "machine"}


@pytest.mark.parametrize("op,cap", [("quick.list", "quick/1"), ("records.status", "records/1")])
def test_hostlink_m6_reads_are_not_writes(op, cap):
    """Exercise HostLink.call itself, not the scripted route layer: no idem/write opts on either read."""
    import os
    import socket
    import threading

    from orch_dashboard.hostlink import HostLink, encode, receive

    a, b = socket.socketpair()
    requests = []
    ws = "a" * 32

    def host():
        with b:
            hello = receive(b)
            assert cap in hello["caps"] and hello["ops"][op] == 1
            b.sendall(
                encode(
                    {
                        "t": "welcome",
                        "proto": 1,
                        "host_uid": os.getuid(),
                        "workspace": {"id": ws},
                        "peer": {"uid": os.getuid(), "class": "person", "person": "p_test"},
                        "caps": [cap],
                        "ops": {op: 1},
                    }
                )
            )
            req = receive(b)
            requests.append(req)
            b.sendall(encode({"t": "res", "id": req["id"], "ok": True, "result": {"data": {"seen": True}}}))

    thread = threading.Thread(target=host)
    thread.start()
    link = HostLink(a, os.getuid(), ws)
    try:
        assert link.call(op, {}) == {"seen": True}
        link.welcome["caps"] = []
        with pytest.raises(HostError, match="op.unsupported"):
            link.call(op, {})
    finally:
        link.close()
        thread.join(3)
    assert len(requests) == 1 and "opts" not in requests[0]
    assert not thread.is_alive()


def test_waiting_status_and_opaque_page(client, token, stub, m6):
    res = client.get(
        f"/api/w/{stub.ws}/quick", params={"status": "waiting", "page": "opaque:token/+="}, headers=headers(token)
    )
    assert res.status_code == 200
    assert ("quick.list", {"status": ["waiting"], "limit": 100, "page": "opaque:token/+="}) in m6[0]["calls"]


def test_current_route(client, token, stub, m6, gateway, monkeypatch):
    calls = []
    monkeypatch.setattr(
        client.app.state.records_child, "run", lambda *a, **kw: calls.append((a, kw)) or {"available": True}
    )
    path = f"/api/w/{stub.ws}/records/current"
    assert client.get(path, headers=headers(token)).status_code == 200
    assert calls == [((stub.ws,), {"status": True, "state": gateway.state, "run_dir": gateway.run})]
    m6[0]["refusal"] = HostError({"code": "role.denied", "message": "denied"}, 4)
    assert client.get(path, headers=headers(token)).status_code == 403
    del m6[1].welcome["ops"]["records.status"]
    assert client.get(path, headers=headers(token)).status_code == 501
    assert len(calls) == 1


def test_empty_labels_reach_set():
    from orch_dashboard.handoff import Details, host_call

    assert host_call(Details(action="details", key="DEMO-0001", labels=[])) == (
        "set",
        {"ref": "DEMO-0001", "pairs": ["labels="]},
    )


def test_c15_records_admin_actions_carry_the_reviewed_address(client, token, stub, m6, monkeypatch, gateway):
    """#1198 C15: init, mark/unmark the remote private and the first push are plain clicks. The address the person saw
    goes to the child as the address that must match; the actions need the ops the host serves."""
    script, link = m6
    link.welcome["ops"]["records.remote_private"] = 1
    calls = []
    monkeypatch.setattr(client.app.state.records_child, "run", lambda *a, **k: calls.append((a, k)) or {"ok": True})
    base = f"/api/w/{stub.ws}/records"
    url = "https://git.example.test/records"
    cases = [
        ("init", {}, "init", None),
        ("remote", {"on": True, "url": url}, "mark", url),
        ("remote", {"on": False}, "unmark", None),
        ("push-initial", {"url": url}, "push-initial", url),
    ]
    for suffix, body, action, expect in cases:
        assert client.post(f"{base}/{suffix}", json=body, headers={"Origin": ORIGIN}).status_code == 401
        assert client.post(f"{base}/{suffix}", json=body, headers=headers(token)).status_code == 200
        (args, kwargs) = calls[-1]
        assert args == (stub.ws,) and kwargs["action"] == action and kwargs["url"] == expect
    # shapes that are not one of the three: refused before anything runs
    n = len(calls)
    bad = [
        ("init", {"x": 1}),
        ("remote", {"on": True}),
        ("remote", {"on": False, "url": url}),
        ("remote", {"on": True, "url": "has space"}),
        ("remote", {"on": True, "url": url, "argv": []}),
        ("push-initial", {}),
        ("push-initial", {"url": "x‮y"}),
        ("push-initial", {"url": url, "remote": "other"}),
    ]
    for suffix, body in bad:
        assert client.post(f"{base}/{suffix}", json=body, headers=headers(token)).status_code in (400, 422), (suffix, body)
    assert len(calls) == n
    # a host without the owner's mark op cannot be asked to mark
    del link.welcome["ops"]["records.remote_private"]
    assert client.post(f"{base}/remote", json={"on": True, "url": url}, headers=headers(token)).status_code == 501
    assert client.post(f"{base}/init", json={}, headers=headers(token)).status_code == 200


def test_c15_records_admin_refusal_from_the_host_stops_before_the_child(client, token, stub, m6, monkeypatch):
    script, link = m6
    link.welcome["ops"]["records.remote_private"] = 1
    script["refusal"] = HostError({"code": "role.denied", "message": "owner or maintainer", "retryable": False}, 3)
    monkeypatch.setattr(client.app.state.records_child, "run", lambda *a, **k: pytest.fail("must not spawn"))
    base = f"/api/w/{stub.ws}/records"
    for suffix, body in [("init", {}), ("remote", {"on": False}), ("push-initial", {"url": "https://a.example/r"})]:
        res = client.post(f"{base}/{suffix}", json=body, headers=headers(token))
        assert res.status_code == 403 and res.json()["error"]["code"] == "role.denied"
