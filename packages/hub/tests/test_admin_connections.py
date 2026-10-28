"""Connections report the same live, directional decision as the router."""

import pytest
from webapi_helpers import WS, WS_OTHER, WS_SEL, Web

from orch_hub.router import AccessAuthz
from orch_hub.webapi.api import ROUTES, WebApi


@pytest.fixture
def w(tmp_path):
    web = Web(tmp_path)
    web.reg.add(WS_OTHER, "full", owner=web.ann, name="Sender")
    web.reg.add(WS_SEL, "selective", name="Selective")
    web.api.d.authz = AccessAuthz(web.reg, web.env.acc)
    yield web
    web.close()


def read(w):
    status, body, _ = w.call("GET", "/admin/connections", token=w.login(w.admin))
    assert status == 200
    return body


def test_live_owner_alias_edit_grant_is_directional(w):
    assert read(w)["edges"] == [{"from": WS_OTHER, "to": WS}]
    w.env.acc.grant(WS, "team", "read")
    assert read(w)["edges"] == []
    w.env.acc.grant(WS, "team", "edit")
    w.reg.ws[WS_OTHER]["owner"] = None
    assert read(w)["edges"] == []


def test_selective_recipient_and_unbound_workspaces(w):
    w.env.acc.grant(WS_SEL, "team", "edit")
    assert read(w)["edges"] == [{"from": WS_OTHER, "to": WS}]
    w.reg.ws[WS]["status"] = "unbound"
    result = read(w)
    assert result["edges"] == []
    assert {row["workspace_id"] for row in result["workspaces"]} == {WS_OTHER, WS_SEL}
    assert next(row for row in result["workspaces"] if row["workspace_id"] == WS_OTHER) == {
        "workspace_id": WS_OTHER,
        "name": "Sender",
    }  # no presence claim: the map says only who may send to whom


def test_missing_authz_is_unavailable_not_an_empty_map(w):
    w.api.d.authz = None
    status, body, _ = w.call("GET", "/admin/connections", token=w.login(w.admin))
    assert (status, body) == (503, {"error": "unavailable"}) or body == {"error": "unavailable"}


def test_serve_wires_the_real_authz_into_the_web_deps():
    """The fixtures above set ``authz`` by hand: this proves ``serve()`` hands the SAME router authz to ``WebDeps``."""
    import ast
    from pathlib import Path

    import orch_hub.serve as serve_module

    tree = ast.parse(Path(serve_module.__file__).read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "WebDeps"]
    assert calls, "serve() builds WebDeps"
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert isinstance(kw.get("authz"), ast.Name) and kw["authz"].id == "authz"


def test_empty_registry(w):
    w.reg.ws.clear()
    assert read(w) == {"workspaces": [], "edges": []}


def test_non_admin_refused_and_route_registration(w):
    assert w.call("GET", "/admin/connections", token=w.login(w.ann))[:2] == (403, {"error": "forbidden_role"})
    assert w.call("GET", "/admin/connections")[0] == 401
    routes = [r for r in ROUTES if r.path == "/api/v1/admin/connections"]
    assert len(routes) == 1 and routes[0].auth == "admin"
    assert all(hasattr(WebApi, "_h_" + r.handler) for r in ROUTES)
    assert len({(r.method, r.path) for r in ROUTES}) == len(ROUTES)
