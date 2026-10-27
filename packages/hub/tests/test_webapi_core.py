"""V3-D13: session, CSRF, routing, role checks on the hub's own data, error codes."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from webapi_helpers import ORIGIN, WS, WS_OTHER, WS_SEL, Web, shared_item

from orch_hub.view import project as PR
from orch_hub.webapi import ROUTES, WebApi
from orch_hub.webapi.api import _render

HASHES = json.loads((Path(__file__).resolve().parents[3] / "contracts" / "core" / "f1" / "hashes.json").read_text())


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    yield x
    x.close()


def sign_in_body(w, user, **kw):
    return {"user_id": user, "password": "correct horse battery", "totp": w.env.next_code(user), **kw}


def test_browser_sign_in_sets_http_only_strict_cookie_and_no_token_in_body(w):
    s, o, h = w.call("POST", "/session", sign_in_body(w, w.ann))
    assert s == 200 and "token" not in o and o["user_id"] == w.ann
    c = h["Set-Cookie"]
    assert c.startswith("__Host-orch_hub_session=") and "HttpOnly" in c and "SameSite=Strict" in c and "Secure" in c
    token = c.split(";")[0].split("=", 1)[1]
    s, me, _ = w.call("GET", "/session", token=token, cookie=True)
    assert s == 200 and me == {"user_id": w.ann, "name": "ann", "is_admin": False, "needs_sign_in": False}


def test_app_sign_in_returns_token_and_bearer_works_without_origin(w):
    tok = w.login(w.ann)
    s, me, _ = w.call("GET", "/session", token=tok, origin=None)
    assert s == 200 and me["user_id"] == w.ann


def test_bad_credentials_generic_and_rate_limited(w):
    for _ in range(5):
        s, o, _ = w.call(
            "POST", "/session", {"user_id": w.ann, "password": "x" * 12, "totp": "000000", "client": "app"}, origin=None
        )
        assert (s, o) == (401, {"error": "bad_credentials"})
    s, o, _ = w.call("POST", "/session", sign_in_body(w, w.ann, client="app"), origin=None)
    assert (s, o) == (429, {"error": "rate_limited"})


def test_browser_sign_in_without_origin_is_refused_before_credentials_are_checked(w):
    s, o, h = w.call("POST", "/session", sign_in_body(w, w.ann), origin=None)
    assert (s, o) == (403, {"error": "not_allowed"}) and "Set-Cookie" not in h
    assert w.env.db_count("sessions", w.ann) == 0


def test_sign_out_kills_the_session_at_once(w):
    tok = w.login(w.ann)
    s, _, h = w.call("DELETE", "/session", token=tok)
    assert s == 200 and "Max-Age=0" in h["Set-Cookie"]
    assert w.call("GET", "/session", token=tok)[:2] == (401, {"error": "no_session"})


@pytest.mark.parametrize(
    "kw",
    [
        {"csrf": False},  # no non-simple header
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"ctype": "text/plain"},
        {"ctype": "application/x-www-form-urlencoded"},
    ],
)
def test_state_changing_routes_refuse_forged_requests(w, kw):
    tok = w.login(w.admin)
    s, o, _ = w.call("POST", "/admin/groups", {"name": "x"}, token=tok, **kw)
    assert (s, o) == (403, {"error": "not_allowed"})
    assert all(g["name"] != "x" for g in w.env.acc.groups())


def test_cookie_auth_without_origin_is_refused_but_get_is_fine(w):
    tok = w.login(w.admin, "browser")
    s, o, _ = w.call("POST", "/admin/groups", {"name": "x"}, token=tok, cookie=True, origin=None)
    assert (s, o) == (403, {"error": "not_allowed"})
    assert w.call("GET", "/admin/groups", token=tok, cookie=True, origin=None)[0] == 200
    assert w.call("POST", "/admin/groups", {"name": "x"}, token=tok, cookie=True)[0] == 200


def test_host_header_must_match(w):
    s, o, _ = w.call("GET", "/session", host="evil.example")
    assert (s, o) == (403, {"error": "not_allowed"})


def _fill(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: ("a" * 32) if m[1].endswith("id") else "team", path)[len("/api/v1") :]


def test_every_route_checks_the_session_and_admin_routes_the_role(w):
    ann = w.login(w.ann)
    for r in ROUTES:
        if r.auth == "none":
            continue
        assert w.call(r.method, _fill(r.path), {} if r.method != "GET" else None)[:2] == (
            401,
            {"error": "no_session"},
        ), r
        assert w.call(r.method, _fill(r.path), {} if r.method != "GET" else None, token="bogus")[:2] == (
            401,
            {"error": "no_session"},
        ), r
        if r.auth == "admin":
            s, o, _ = w.call(r.method, _fill(r.path), {} if r.method != "GET" else None, token=ann)
            assert (s, o) == (403, {"error": "forbidden_role"}), r


def test_unknown_route_and_method(w):
    assert w.call("GET", "/nope")[:2] == (404, {"error": "unknown_op"})
    assert w.call("PATCH", "/session")[:2] == (404, {"error": "unknown_op"})
    assert w.call("GET", "/admin/users/" + "a" * 32)[:2] == (404, {"error": "unknown_op"})


def test_body_rules(w):
    tok = w.login(w.admin)
    assert w.call("POST", "/admin/groups", b'{"name":"a","name":"b"}', token=tok)[:2] == (400, {"error": "malformed"})
    assert w.call("POST", "/admin/groups", b"[1]", token=tok)[:2] == (400, {"error": "malformed"})
    assert w.call("POST", "/admin/groups", b"{", token=tok)[:2] == (400, {"error": "malformed"})
    assert w.call("POST", "/admin/groups", b"{" + b" " * 70000 + b"}", token=tok)[:2] == (413, {"error": "too_large"})
    assert w.call("POST", "/admin/groups", {"name": 3}, token=tok)[:2] == (400, {"error": "malformed"})


def test_unexpected_failure_is_internal_without_detail(w, monkeypatch):
    tok = w.login(w.ann)
    monkeypatch.setattr(w.env.acc, "users", lambda: (_ for _ in ()).throw(RuntimeError("secret detail /etc/x")))
    s, o, _ = w.call("GET", "/session", token=tok)
    assert (s, o) == (500, {"error": "internal"})


def test_store_error_is_unavailable_not_echoed(w, monkeypatch):
    from orch_hub.access import AccessError

    tok = w.login(w.admin)
    monkeypatch.setattr(
        w.env.acc, "groups", lambda: (_ for _ in ()).throw(AccessError("store_error", "OperationalError"))
    )
    assert w.call("GET", "/admin/groups", token=tok)[:2] == (503, {"error": "unavailable"})


# --- (b) person home -----------------------------------------------------------------------------------------------


def test_my_workspaces_roles_and_selective_only_for_the_owner_alias(w):
    w.reg.add(WS_SEL, "selective", owner=w.ann, name="Acme\u202e")
    w.reg.add(WS_OTHER, "full")  # ann has no grant here
    ann, adm = w.login(w.ann), w.login(w.admin)
    _, o, _ = w.call("GET", "/me/workspaces", token=ann)
    rows = {r["workspace_id"]: r for r in o["workspaces"]}
    assert set(rows) == {WS, WS_SEL}
    assert rows[WS]["role"] == "edit" and rows[WS]["mode"] == "full" and rows[WS]["can_start"] is True
    assert (
        rows[WS_SEL]["role"] == "owner" and rows[WS_SEL]["mode"] == "selective" and rows[WS_SEL]["can_start"] is False
    )
    assert rows[WS_SEL]["name"] == "Acme<U+202E>"  # render_text
    _, o, _ = w.call(
        "GET", "/me/workspaces", token=adm
    )  # hub-admin: admin in every FULL workspace, nothing in selective
    assert {r["workspace_id"] for r in o["workspaces"]} == {WS, WS_OTHER}


def test_needs_you_full_and_selective_escaped_and_scoped(w):
    w.reg.add(WS_SEL, "selective", owner=w.ann)
    w.shares.items[(w.ann, WS_SEL)] = [shared_item("11" * 16, "needs_you", "Pick\u202e one")]
    w.view.rows = [{"item_id": "22" * 16, "kind": "question", "title": "Which\u0085?", "extra": "dropped"}]
    _, o, _ = w.call("GET", "/me/needs-you", token=w.login(w.ann))
    by = {i["mode"]: i for i in o["items"]}
    assert (
        by["selective"]["title"] == "Pick<U+202E> one"
        and by["selective"]["summary_only"]
        and not by["selective"]["signable"]
    )
    assert by["full"]["title"] == "Which<U+0085>?" and by["full"]["signable"] and "extra" not in by["full"]
    # the other user sees neither
    reg_user = w.env.user("bob")
    w.env.acc.add_group("none")
    _, o, _ = w.call("GET", "/me/needs-you", token=w.login(reg_user))
    assert o["items"] == []


def test_needs_you_reports_offline_and_unavailable(w):
    w.reg.add(WS_SEL, "selective", owner=w.ann, status="unbound")
    w.api.d.view = None
    _, o, _ = w.call("GET", "/me/needs-you", token=w.login(w.ann))
    assert o == {"items": [], "offline": [WS_SEL], "unavailable": [WS]}


def test_item_detail_full_selective_and_not_a_member(w):
    w.reg.add(WS_SEL, "selective", owner=w.ann)
    w.shares.items[(w.ann, WS_SEL)] = [shared_item("11" * 16, "question")]
    w.view.detail = {"item_id": "22" * 16, "kind": "question", "title": "T", "content": {"text": "a\u202eb"}}
    ann = w.login(w.ann)
    s, o, _ = w.call("GET", f"/workspaces/{WS}/items/{'22' * 16}", token=ann)
    assert s == 200 and o["content"]["text"] == "a<U+202E>b"
    s, o, _ = w.call("GET", f"/workspaces/{WS_SEL}/items/{'11' * 16}", token=ann)
    assert s == 200 and o["content"]["id"] == "Q1" and o["item_hash"].startswith("sha256:") and o["signable"] is False
    assert w.call("GET", f"/workspaces/{WS_SEL}/items/{'33' * 16}", token=ann)[:2] == (404, {"error": "unknown_item"})
    bob = w.login(w.env.user("bob"))
    assert w.call("GET", f"/workspaces/{WS}/items/{'22' * 16}", token=bob)[:2] == (404, {"error": "unknown_workspace"})
    assert w.call("GET", f"/workspaces/{WS_OTHER}/items/{'22' * 16}", token=bob)[:2] == (
        404,
        {"error": "unknown_workspace"},
    )


def _choice_full():
    v = next(v for v in HASHES["question_hash"] if v["name"] == "choice_full")
    return v, v["hashed"]


def test_verified_question_goes_out_verbatim_and_its_hash_recomputes(w):
    """The route sends a verified question's text/options/spec untouched (the tab in `details` survives) and the hash
    recomputes from the response; escaping through ``_render`` would turn the tab into ``<U+0009>`` and break it."""
    v, h = _choice_full()
    content = {
        "text": h["text"],
        "options": h["options"],
        "spec": h["spec"],
        "blocking": True,
        "ticket_title": "T\u202e",
    }
    assert "\t" in h["spec"]["details"]
    w.view.detail = {"item_id": "22" * 16, "kind": "question", "title": "T", "content": content}
    s, o, _ = w.call("GET", f"/workspaces/{WS}/items/{'22' * 16}", token=w.login(w.ann))
    c = o["content"]
    assert s == 200 and "\t" in c["spec"]["details"] and c["ticket_title"] == "T<U+202E>"
    got = {"text": c["text"], "options": c["options"], **c["spec"]}
    assert PR.question_hash(h["question_id"], h["ticket"], got) == v["hash"]
    assert _render(content)["spec"]["details"] != content["spec"]["details"]  # the old path would have failed the hash


def test_unverifiable_question_with_a_spec_is_escaped_not_verbatim(w):
    _, h = _choice_full()
    content = {"text": "a\u202eb", "options": h["options"], "spec": h["spec"], "unverifiable": True}
    w.view.detail = {"item_id": "22" * 16, "kind": "question", "title": "T", "content": content}
    s, o, _ = w.call("GET", f"/workspaces/{WS}/items/{'22' * 16}", token=w.login(w.ann))
    assert s == 200 and o["content"]["text"] == "a<U+202E>b" and "\t" not in o["content"]["spec"]["details"]


def test_routes_table_is_complete():
    for r in ROUTES:
        assert hasattr(WebApi, "_h_" + r.handler), r
    assert len({(r.method, r.path) for r in ROUTES}) == len(ROUTES)
    assert ORIGIN


# --- selective items carry chain and f1 (orch-hub#57, protocol §8.6 / §10.2b) -------------------------------------------

_H = "sha256:" + "ab" * 32
_CHAIN = {"log": "01" * 13, "seq": 4, "head": _H}


def _f1(gates=("requirements",), questions=()):
    return {
        "roster_v": 2,
        "gates": {g: {"gen": 1, "hash": _H, "policy_hash": _H} for g in gates},
        "questions": {q: {"hash": _H} for q in questions},
    }


def test_selective_items_carry_chain_f1_and_are_signable_for_the_owner_alias(w):
    w.reg.add(WS_SEL, "selective", owner=w.ann)
    one, two, none = "11" * 16, "22" * 16, "33" * 16
    w.shares.items[(w.ann, WS_SEL)] = [
        shared_item(one, "ticket", chain=_CHAIN, f1=_f1()),
        shared_item(two, "ticket", chain=_CHAIN, f1=_f1(("plan",), ("Q1",))),
        shared_item(none, "ticket", chain=_CHAIN, f1=_f1(())),
    ]
    ann = w.login(w.ann)
    _, o, _ = w.call("GET", "/me/needs-you", token=ann)
    by = {i["item_id"]: i for i in o["items"]}
    assert by[one]["signable"] is True and by[one]["chain"] == _CHAIN and by[one]["f1"] == _f1()
    assert by[two]["signable"] is False and by[two]["f1"] == _f1(("plan",), ("Q1",))  # two targets
    assert by[none]["signable"] is False  # empty f1 names nothing
    s, d, _ = w.call("GET", f"/workspaces/{WS_SEL}/items/{one}", token=ann)
    assert s == 200 and d["signable"] is True and d["chain"] == _CHAIN and d["f1"] == _f1() and d["item_hash"]


def test_selective_summary_and_item_without_f1_are_never_signable(w):
    w.reg.add(WS_SEL, "selective", owner=w.ann)
    w.shares.items[(w.ann, WS_SEL)] = [
        shared_item("11" * 16, "needs_you", chain=_CHAIN, f1=_f1()),  # even a summary carrying f1
        shared_item("22" * 16, "ticket", chain=_CHAIN),  # no f1: the hub cannot build on it
        shared_item("33" * 16, "ticket", chain={**_CHAIN, "log": "workspace"}, f1=_f1()),
    ]
    _, o, _ = w.call("GET", "/me/needs-you", token=w.login(w.ann))
    assert [i["signable"] for i in o["items"]] == [False, False, False]
    assert o["items"][1]["f1"] is None


def test_selective_chain_f1_only_for_the_owner_alias(w):
    w.reg.add(WS_SEL, "selective", owner=w.ann)
    w.shares.items[(w.ann, WS_SEL)] = [shared_item("11" * 16, "ticket", chain=_CHAIN, f1=_f1())]
    bob = w.login(w.env.user("bob"))
    _, o, _ = w.call("GET", "/me/needs-you", token=bob)
    assert o["items"] == []
    assert w.call("GET", f"/workspaces/{WS_SEL}/items/{'11' * 16}", token=bob)[:2] == (
        404,
        {"error": "unknown_workspace"},
    )


def test_web_server_accept_queue_holds_a_burst():
    from orch_hub.webapi.server import _BoundedServer

    assert _BoundedServer.request_queue_size >= 128  # the Admin page opens 7 connections at once
