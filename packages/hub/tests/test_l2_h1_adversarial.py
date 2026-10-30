"""Opus l2-h1 adversarial tests for hub#45 (H1, the phone routes). An xfail was an open finding (fixed) (L2H1-n in
state/l2-review-p4.md "H1 hub#45"); plain tests pin what the review confirmed."""

from __future__ import annotations

import threading

import pytest
from test_phone_routes import DAY, auth_obj, bind, pair, phone, push_key, push_obj, put_push, w  # noqa: F401
from webapi_helpers import WS

from orch_hub.channels.pushkeys import PushKeyStore
from orch_hub.channels.service import ChannelError
from orch_hub.config import HubConfig

# --- scope escapes -------------------------------------------------------------------------------------------------------


def test_a_channel_session_never_reaches_an_admin_route(w):  # noqa: F811
    _, cid, atok = phone(w, w.admin)  # a hub admin's phone
    s, o, _ = w.call("POST", f"/admin/channels/{cid}/revoke", {}, token=atok)
    assert (s, o) == (403, {"error": "forbidden_role"})  # §6.4: no admin routes for a channel session


def test_a_pairing_token_reaches_nothing_beyond_pair_bind_sign_out(w):  # noqa: F811
    _, cid, tok = pair(w, w.ann)
    allowed = {("POST", "/session/channel"), ("DELETE", "/session")}
    from orch_hub.webapi.api import ROUTES

    for rt in ROUTES:
        if rt.auth == "none" or (rt.method, rt.path.removeprefix("/api/v1")) in allowed:
            continue
        if rt.handler in ("reg_begin", "reg_complete"):
            continue
        path = rt.path.removeprefix("/api/v1")
        for k in (
            "channel_id",
            "user_id",
            "workspace_id",
            "item_id",
            "prepared_id",
            "group",
            "change_id",
            "name",
            "space_id",
            "share_id",
            "app_id",
        ):
            path = path.replace("{" + k + "}", cid if k == "channel_id" else "ab" * 16)
        path = path.replace("{invite}", "ab" * 8)  # invite ids are 16 hex
        path = path.replace("{part}", "relay")  # service parts are a closed set
        s, o, _ = w.call(rt.method, path, {} if rt.method != "GET" else None, token=tok)
        assert o == {"error": "forbidden_role"}, (rt.method, rt.path, s, o)


def test_every_channel_request_rechecks_the_access_revocation_list(w):  # noqa: F811
    """A revoke whose access-model write landed but whose channels-row UPDATE did not (``revoke`` writes them in two
    steps) leaves ``channel_state`` == active: the channel session keeps reading views; ``active_channel`` would say no."""
    _, cid, ctok = phone(w, w.ann)
    w.env.acc.revoke_channel(cid)
    assert w.env.svc.active_channel(w.ann, cid) is None
    assert w.call("GET", "/me/workspaces", token=ctok)[1] == {"error": "channel_revoked"}


# --- bind ------------------------------------------------------------------------------------------------------------------


def test_a_channel_auth_replayed_into_a_second_pairing_session_is_refused(w):  # noqa: F811
    signer, cid, tok = pair(w, w.ann)
    o = auth_obj(w, signer, cid, w.ann, tok)
    assert w.call("POST", "/session/channel", {"auth": o}, token=tok)[0] == 200
    tok2 = w.login(w.ann, "app")
    _s, got, _ = w.call("POST", "/session/channel", {"auth": o}, token=tok2)
    assert got["error"] == "challenge_invalid"  # session_ref names the first token
    assert w.call("POST", "/session/channel", {"auth": o}, token=tok)[1] == {"error": "no_session"}  # consumed


def test_many_racing_binds_one_winner(w):  # noqa: F811
    signer, cid, tok = pair(w, w.ann)
    o = auth_obj(w, signer, cid, w.ann, tok)
    out: list[int] = []
    ts: list[threading.Thread] = []
    for _ in range(8):
        ts.append(threading.Thread(target=lambda: out.append(_bind_svc(w, tok, o))))
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out.count(1) == 1


def _bind_svc(w, tok, o) -> int:  # noqa: F811
    try:
        w.env.svc.bind_session(tok, o)
        return 1
    except ChannelError:
        return 0


# --- api_origin -----------------------------------------------------------------------------------------------------------


def test_a_non_dev_hub_refuses_a_plain_http_api_origin():
    base = {"dev": False, "listen": "127.0.0.1:0", "web_listen": "127.0.0.1:0"}
    with pytest.raises(ValueError):
        HubConfig(**base, allowed_origins=frozenset({"http://hub.example"}))


def test_changing_the_api_origin_under_the_same_op_cert_is_refused(w):  # noqa: F811
    a = w.call("GET", "/hub/card")[1]["api_origin"]
    w.api.d.api_origin = "https://other.example"  # a restart with another --api-origin, same state dir
    w.api._origins.clear()
    s, b, _ = w.call("GET", "/hub/card")
    assert s != 200 or b["api_origin"]["o"]["op_cert_id"] != a["o"]["op_cert_id"]


# --- push key ---------------------------------------------------------------------------------------------------------------


def test_push_key_of_another_channel_or_user_is_refused(w):  # noqa: F811
    _s1, c1, t1 = phone(w, w.ann)
    s2, c2, _t2 = phone(w, w.admin)
    _, pub = push_key()
    assert put_push(w, c2, t1, push_obj(w, s2, c2, pub))[1] == {"error": "other_user"}
    assert put_push(w, c1, t1, push_obj(w, s2, c2, pub))[1]["error"] in ("bad_signature", "other_user")


def test_push_key_rollback_by_a_captured_older_object_is_refused(w):  # noqa: F811
    s1, c1, t1 = phone(w, w.ann)
    _, old = push_key()
    _, new = push_key()
    o_old = push_obj(w, s1, c1, old)
    w.clock.advance(1000)
    assert put_push(w, c1, t1, push_obj(w, s1, c1, new))[0] == 200
    assert put_push(w, c1, t1, o_old)[1] == {"error": "replay"}
    assert PushKeyStore(w.env.svc.db)[c1] == new


# --- user removal ---------------------------------------------------------------------------------------------------------


def test_user_removal_that_fails_midway_can_be_completed(w, monkeypatch):  # noqa: F811
    _, c1, _ = phone(w, w.ann)
    _, c2, _ = pair(w, w.ann)
    real, n = w.env.svc.revoke, [0]

    def flaky(cid, **kw):
        n[0] += 1
        if n[0] == 2:
            raise ChannelError("internal", "audit")  # e.g. the chain write of the 2nd revoke fails
        return real(cid, **kw)

    monkeypatch.setattr(w.env.svc, "revoke", flaky)
    with pytest.raises(ChannelError):
        w.env.svc.remove_user(w.ann)
    monkeypatch.setattr(w.env.svc, "revoke", real)
    try:
        w.env.svc.remove_user(w.ann)  # the operator retries
    except ChannelError:
        pass
    assert {c1, c2} <= set(w.env.acc.revoked_channels())


def test_removed_users_channel_session_and_prepared_die(w):  # noqa: F811
    _, cid, ctok = phone(w, w.ann)
    act = {"type": "ticket.updated", "log": "L", "set": {}}
    w.call("POST", "/sign/prepare", {"workspace_id": WS, "channel_id": cid, "action": act}, token=ctok)
    w.env.svc.remove_user(w.ann)
    assert w.call("GET", "/me/workspaces", token=ctok)[0] == 401
    assert w.env.svc.active_channel(w.ann, cid) is None


# --- expiry without renewal -------------------------------------------------------------------------------------------------


def test_cert_expiry_ends_the_channel_session_fail_closed(w):  # noqa: F811
    _, cid, ctok = phone(w, w.ann)
    # the channel session ends at min(login + 12 h, cert end); walk to just before the cert end with fresh binds
    w.clock.advance(31 * DAY - 3_600_000)
    assert w.call("GET", "/me/workspaces", token=ctok)[1] == {"error": "no_session"}  # 12 h long gone
    assert w.env.svc.channel_state(w.ann, cid) == "active"
    w.clock.advance(2 * 3_600_000)
    assert w.env.svc.channel_state(w.ann, cid) == "expired"
    assert w.env.svc.active_channel(w.ann, cid) is None
