"""#1198 R1-5: the step-up list (`stepup.STEP_UP`) is enforced for every variant, by one helper every body-decides
handler calls first. Invariant: a step-up operation never runs for a non-local session until R1-6."""

# ruff: noqa: F811

import ast
import inspect
import textwrap

import pytest
from fastapi.routing import APIRoute
from remote_kit import HTTPS, app, bearer, passkey_store, remote, ungated  # noqa: F401
from test_dashboard_click import (  # noqa: F401
    HASH,
    PASS_ENV,
    REQ,
    agent_access,
    click_start,
    hub_join,
    rotate_body,
    script,
)

from orch_dashboard import routegate, stepup
from orch_dashboard.stepup import GUARDED_ROUTES, STEP_UP

FEATURES = ["custody", "device_rotate", "runner_switch"]
CHALLENGE_KEY = "A" * 87

#: (STEP_UP entry name, route, body): every variant of every entry, as a browser would send it
VARIANTS = [
    (STEP_UP[0].name, "custody", {"on": True, "passphrase": "correct horse"}),
    (STEP_UP[0].name, "custody", {"on": True, "resume": True}),
    (
        STEP_UP[1].name,
        "custody",
        {"on": False, "request_id": REQ, "view_digest": HASH, "sealed": {"custody.passphrase": PASS_ENV}},
    ),
    (STEP_UP[2].name, "custody/seal", {}),
    (STEP_UP[3].name, "rotate/seal", {}),
    (STEP_UP[4].name, "rotate", rotate_body()),
    (STEP_UP[5].name, "runner/review", {"on": True}),
    (STEP_UP[5].name, "runner/review", {"on": False, "terminals": True}),
    (STEP_UP[6].name, "runner", {"on": True, "view_digest": HASH}),
    (STEP_UP[6].name, "runner", {"on": False, "terminals": True, "view_digest": HASH}),
    (STEP_UP[7].name, "prepare", agent_access()),
    (STEP_UP[8].name, "prepare", hub_join()),
    (STEP_UP[8].name, "prepare", {"action": "hub_leave"}),
    (STEP_UP[9].name, "sign", {"request_id": REQ, "view_digest": HASH, "sealed": {"hub.backup_key": PASS_ENV}}),
    (STEP_UP[9].name, "sign", {"request_id": REQ, "view_digest": HASH, "reveal_key": CHALLENGE_KEY}),
]


def test_every_entry_of_the_list_has_a_variant_below():
    assert {name for name, _, _ in VARIANTS} == {s.name for s in STEP_UP}


@pytest.mark.parametrize(("name", "route", "body"), VARIANTS, ids=[f"{r}-{i}" for i, (_, r, _) in enumerate(VARIANTS)])
def test_each_step_up_variant_is_refused_for_a_remote_session_and_reaches_no_signer(
    ungated, remote, stub, script, name, route, body
):
    """With the route table off, so the handler's own helper is what answers."""
    script.features = FEATURES
    res = ungated.post(f"/api/w/{stub.ws}/click/{route}", json=body, headers=bearer(remote, HTTPS))
    assert res.status_code in (403, 409), (name, res.status_code, res.text)
    error = res.json()["error"]
    assert error["code"] in ("human.click_refused", "custody.terminal_needed"), res.text
    assert "needs a dashboard opened on this computer" in error["message"]
    assert [c for cl in script.clients for c in cl.calls] == [], "the signer must never be called"


def test_a_plain_variant_of_the_same_routes_is_not_refused_by_the_helper(ungated, remote, stub, script):
    script.features = FEATURES
    for route, body in (
        ("runner/review", {"on": False}),
        ("runner", {"on": False, "view_digest": HASH}),
        ("custody", {"on": False}),
    ):
        res = ungated.post(f"/api/w/{stub.ws}/click/{route}", json=body, headers=bearer(remote, HTTPS))
        assert res.status_code == 200, (route, res.text)


def test_the_helper_lets_a_local_session_through(app):
    class Auth:
        def login_kind(self, token):
            return "local"

    class Request:
        class state:
            token = "t"

    stepup.refuse_step_up(Auth(), Request, "custody", type("B", (), {"on": True, "sealed": None})(), "ws")


def first_statement(endpoint):
    tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    body = tree.body[0].body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):  # the docstring
        body = body[1:]
    return body[0]


@pytest.mark.parametrize("route", GUARDED_ROUTES)
def test_every_body_decides_handler_calls_the_helper_first(app, route):
    """Fails when a handler of the list forgets `refuse_step_up(...)` or puts something before it."""
    path = "/api/w/{ws}/click/" + route
    (handler,) = [r for r in app.routes if isinstance(r, APIRoute) and r.path == path and "POST" in r.methods]
    stmt = first_statement(handler.endpoint)
    assert isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call), (route, ast.dump(stmt)[:80])
    call = stmt.value
    assert getattr(call.func, "id", None) == "refuse_step_up", route
    assert isinstance(call.args[2], ast.Constant) and call.args[2].value == route


def test_no_route_adds_a_passkey_or_a_device_yet(app):
    paths = [r.path for r in app.routes if isinstance(r, APIRoute)]
    assert not [p for p in paths if any(w in p for w in ("passkey/add", "passkey/remove", "device/add", "enroll/mint"))]
    assert stepup.NO_ROUTE_YET  # R1-6 must give these a step-up when they get routes


def test_the_body_decides_routes_of_the_table_are_exactly_the_guarded_routes():
    """A new body-decides handler cannot go unguarded: the table marks it, and so must `stepup.GUARDED_ROUTES` (and
    the reverse). Routes that are wholly step-up in the table are guarded too, as a backup."""
    from orch_dashboard.routegate import ALLOWED, TABLE

    prefix = "/api/w/{ws}/click/"
    body_decides = {
        path.removeprefix(prefix)
        for (method, path), (rule, why) in TABLE.items()
        if rule == ALLOWED and why.startswith("body decides") and method == "POST"
    }
    assert body_decides
    whole_route = {
        path.removeprefix(prefix)
        for (_, path), (rule, _) in TABLE.items()
        if rule == routegate.STEP_UP and prefix in path
    }
    guarded = set(GUARDED_ROUTES)
    assert body_decides == guarded - whole_route
    assert whole_route <= guarded
