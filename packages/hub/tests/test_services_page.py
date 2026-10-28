"""Run-11 #12/#13: the Services read (admin) and the hand-offs read (own workspaces only)."""

from __future__ import annotations

import pytest
from webapi_helpers import WS, WS_OTHER, Web

from orch_hub.router import RouteQueue
from orch_hub.services import Services


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    yield x
    x.close()


def _services(results):
    def probe(url, path, timeout):
        r = results[url]
        if isinstance(r, Exception):
            raise r
        return r

    return Services(relay_url="http://relay.test", publish_url="http://pub.test", now_ms=lambda: 5_000, probe=probe)


def test_services_are_admin_only_and_report_state_version_and_last_answer(w):
    w.api.d.services = _services(
        {"http://relay.test": (200, {"ok": True, "version": "0.1.0"}), "http://pub.test": OSError("down")}
    )
    assert w.call("GET", "/admin/services", token=w.login(w.ann))[:2] == (403, {"error": "forbidden_role"})
    s, o, _ = w.call("GET", "/admin/services", token=w.login(w.admin))
    assert s == 200
    assert o["hub"]["state"] == "up" and o["hub"]["version"]
    assert o["relay"] == {
        "name": "relay",
        "url": "http://relay.test",
        "version": "0.1.0",
        "last_ok_ms": 5000,
        "state": "up",
        "checked_ms": 5000,
    }
    assert o["publish"]["state"] == "down" and o["publish"]["last_ok_ms"] is None
    assert o["workspaces"] == {"count": 1, "online": 1}


def test_publish_is_up_on_any_http_answer_and_reports_no_version(w):
    w.api.d.services = _services({"http://relay.test": (500, None), "http://pub.test": (404, None)})
    o = w.call("GET", "/admin/services", token=w.login(w.admin))[1]
    assert o["publish"]["state"] == "up" and o["publish"]["version"] is None
    assert o["relay"]["state"] == "down"  # the relay must answer 200 on /healthz


def test_publish_reports_its_version_from_healthz(w):
    w.api.d.services = _services(
        {"http://relay.test": (200, {"version": "0.1.0"}), "http://pub.test": (200, {"ok": True, "version": "0.2.0"})}
    )
    o = w.call("GET", "/admin/services", token=w.login(w.admin))[1]
    assert o["publish"]["state"] == "up" and o["publish"]["version"] == "0.2.0"


def test_a_service_that_is_not_set_up_says_so(w):
    w.api.d.services = Services(
        relay_url=None, publish_url=None, probe=lambda *a: (_ for _ in ()).throw(AssertionError("no probe"))
    )
    o = w.call("GET", "/admin/services", token=w.login(w.admin))[1]
    assert o["relay"]["state"] == "not_set_up" and o["publish"]["state"] == "not_set_up"


def test_handoffs_show_only_my_workspaces_by_name(w):
    q = RouteQueue(":memory:")
    q.add(WS, WS_OTHER, "e1" * 8, b"x" * 200, 1000)  # from a workspace I have no part in, to mine: waiting
    q.add(WS_OTHER, WS, "e2" * 8, b"y" * 200, 2000)  # from mine to someone else's
    q.delete(q.add(WS, WS_OTHER, "e3" * 8, b"z" * 200, 3000))  # delivered
    q.add("11" * 16, "22" * 16, "e4" * 8, b"q" * 200, 4000)  # neither end is mine: never listed
    w.api.d.routes = q
    s, o, _ = w.call("GET", "/me/handoffs", token=w.login(w.ann))
    assert s == 200
    rows = o["handoffs"]
    assert [r["sent_ms"] for r in rows] == [3000, 2000, 1000]  # newest first
    assert [r["status"] for r in rows] == ["delivered", "waiting", "waiting"]
    assert rows[1]["from"]["workspace_id"] == WS and rows[1]["to"] == {
        "workspace_id": None,
        "name": None,
        "online": False,
    }
    zed = w.env.user("zed")  # no part in any workspace: sees nothing
    assert w.call("GET", "/me/handoffs", token=w.login(zed))[1]["handoffs"] == []


def test_handoffs_without_a_queue_is_an_empty_list(w):
    assert w.call("GET", "/me/handoffs", token=w.login(w.ann))[1] == {"handoffs": []}


@pytest.mark.parametrize(
    ("behind_edge", "url", "expected"),
    [
        (False, None, {"state": "not_set_up", "url": None}),
        (False, "https://hub.test", {"state": "not_set_up", "url": None}),
        (True, None, {"state": "up", "url": None}),
        (True, "https://hub.test", {"state": "up", "url": "https://hub.test"}),
    ],
)
def test_services_edge_uses_configuration_without_a_probe(w, behind_edge, url, expected):
    def no_probe(*args):
        pytest.fail("Edge must not trigger a network call")

    w.api.d.services = Services(relay_url=None, publish_url=None, behind_edge=behind_edge, edge_url=url, probe=no_probe)
    status, body, _ = w.call("GET", "/admin/services", token=w.login(w.admin))
    assert status == 200
    assert body["edge"] == expected
    assert body["hub"]["state"] == "up"
    assert body["relay"]["state"] == body["publish"]["state"] == "not_set_up"
