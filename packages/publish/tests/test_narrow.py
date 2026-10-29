"""M4 S2 at orch-publish (#1357, #1453, protocol.md 11.6 and 11.4): the hub's narrowing (``PUT /v2/hub/narrow``) and the
two link ops ``share.extend`` / ``share.password``. One test per Opus condition: 7 (only revoke/stop, full mode, current
WSK, replay, the workspace records it), 8 (C5 exactly), 14 (extend <= 30 days), the carry-forward lines of S2 (``held``
from the card that refuses dev cards, nonces kept >= 240 s, a password is never null)."""

import json
import os

import pytest

from pubsrv import auth, shares
from pubsrv import hub_admin_read as R
from pubsrv import hub_path as H
from pubsrv import published as P
from pubsrv import relay_proto as proto
from tests.hub_helpers import Hub, signed
from tests.test_hub_path_http import bound, conn_of
from tests.test_publish_api_auth import NOW, WS_A, WS_B, Env, err

SHARE = "a" * 32
APP = "c" * 32


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.allow()
    assert e.register().status_code == 200
    e.app.state.settings.dev_mode = True
    return e


def add_share(e, sid=SHARE, ws=WS_A, state="live", exp=NOW + 10**7, kind="secret"):
    e.sql(
        "INSERT INTO shares(id, owner_ws, ticket, kind, token_hash, expires_ms, state, bytes, created_ms, label)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        sid, ws, "TKT", kind, "e" * 64, exp, state, 7, 1, "Q3",
    )  # fmt: skip
    e.sql(
        "INSERT INTO views(view_id, share_id, until_ms) VALUES (?,?,?)", "f" * 32, sid, NOW + 10**6
    )


def add_app(e, aid=APP, ws=WS_A, state="running"):
    e.sql(
        "INSERT INTO apps(id, owner_ws, slug, name, kind, access, state, created_ms, updated_ms)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        aid, ws, "shop-" + aid[:4], "Shop", "app", "public", state, 1, 1,
    )  # fmt: skip
    e.sql(
        "INSERT INTO app_views(view_id, app_id, until_ms) VALUES (?,?,?)",
        "d" * 32,
        aid,
        NOW + 10**6,
    )


def full(e, **kw):
    return bound(e, mode="full", **kw)


def narrow_obj(
    hub, target="share", tid=SHARE, action=None, ws=WS_A, ts=NOW, nonce=None, key=None, **over
):
    o = {
        "v": 2, "suite": 2, "hv": 0, "kind": "hub.release_narrow", "hub_id": hub.id, "workspace_id": ws,
        "target": target, "id": tid, "action": action or ("revoke" if target == "share" else "stop"),
        "ts_ms": ts, "nonce": nonce or proto.b64u(os.urandom(32)),
    }  # fmt: skip
    o.update(over)
    return signed(key or hub.op, H.LABELS["sig_hub_release_narrow"], o)


def put(e, obj):
    return e.c.put("/v2/hub/narrow", content=json.dumps(obj).encode())


def state_of(e, sid=SHARE):
    return e.sql("SELECT state FROM shares WHERE id=?", sid)[0]["state"]


# --- the allowed narrowings ------------------------------------------------------------------------------------


def test_revoke_a_link_ends_it_and_the_workspace_records_it(env):
    hub = full(env)
    add_share(env)
    r = put(env, narrow_obj(hub))
    assert (r.status_code, r.json()) == (
        200,
        {"ok": True, "target": "share", "id": SHARE, "result": "revoked"},
    )
    assert state_of(env) == "revoked"
    assert env.sql("SELECT token_hash FROM shares WHERE id=?", SHARE)[0][0] == "0" * 64
    assert env.sql("SELECT * FROM views") == []  # live view sessions are gone
    rows = env.sql("SELECT target, target_id, workspace_id, hub_id, action FROM hub_narrowings")
    assert [tuple(x) for x in rows] == [("share", SHARE, WS_A, hub.id, "revoke")]
    # cond. 7: the workspace sees the narrowing in its own list
    got = shares.op_list(env.app.state.settings, WS_A)["shares"]
    assert [(s["id"], s["state"], s["narrowed_by"]) for s in got] == [(SHARE, "revoked", "hub")]
    # and the hub's own company list shows it revoked
    c = conn_of(env)
    try:
        assert R.list_published(c, [WS_A], NOW)["rows"][0]["state"] == "revoked"
    finally:
        c.close()


def test_a_second_revoke_changes_nothing_and_records_nothing_more(env):
    hub = full(env)
    add_share(env)
    put(env, narrow_obj(hub))
    r = put(env, narrow_obj(hub))
    assert (r.status_code, r.json()["result"]) == (200, "unchanged")
    assert len(env.sql("SELECT * FROM hub_narrowings")) == 1


def test_stop_an_app_ends_view_sessions_and_is_recorded(env):
    hub = full(env)
    add_app(env)
    r = put(env, narrow_obj(hub, "app", APP))
    assert (r.status_code, r.json()["result"]) == (200, "stopped")
    assert env.sql("SELECT state FROM apps WHERE id=?", APP)[0][0] == "stopped"
    assert env.sql("SELECT * FROM app_views") == []
    assert [tuple(x) for x in env.sql("SELECT target, action FROM hub_narrowings")] == [
        ("app", "stop")
    ]


# --- cond. 7: nothing else, nobody else ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target,action", [("share", "stop"), ("share", "start"), ("share", "extend"), ("share", "remove"),
                      ("app", "revoke"), ("app", "start"), ("app", "extend"), ("app", "remove")]
)  # fmt: skip
def test_only_revoke_a_link_and_stop_an_app(env, target, action):
    hub = full(env)
    add_share(env)
    add_app(env)
    r = put(env, narrow_obj(hub, target, SHARE if target == "share" else APP, action=action))
    assert (r.status_code, err(r)) == (400, "malformed")
    assert state_of(env) == "live"
    assert env.sql("SELECT state FROM apps")[0][0] == "running"
    assert env.sql("SELECT * FROM hub_narrowings") == []


def test_a_selective_workspace_takes_no_hub_narrowing(env):
    hub = bound(env)  # selective
    add_share(env)
    r = put(env, narrow_obj(hub))
    assert (r.status_code, err(r)) == (403, "not_bound")
    assert state_of(env) == "live"


def test_a_binding_under_an_old_wsk_takes_no_narrowing(env):
    hub = full(env)
    add_share(env)
    env.sql(
        "UPDATE workspaces SET wsk_pub=? WHERE workspace_id=?",
        proto.b64u(b"\x04" + b"\x01" * 64),
        WS_A,
    )
    r = put(env, narrow_obj(hub))
    assert (r.status_code, err(r)) == (403, "not_bound")
    assert state_of(env) == "live"


def test_a_target_of_another_workspace_is_refused(env):
    hub = full(env)
    add_share(env, ws=WS_B)
    r = put(env, narrow_obj(hub))  # the hub names WS_A, the link is WS_B's
    assert (r.status_code, err(r)) == (403, "other_workspace")
    assert state_of(env) == "live"
    r = put(env, narrow_obj(hub, tid="9" * 32))
    assert (r.status_code, err(r)) == (404, "not_found")


def test_another_hub_cannot_narrow_this_hubs_workspace(env):
    hub = full(env)
    add_share(env)
    rogue = Hub()
    c = conn_of(env)
    H.accept_hub_card(c, rogue.card, NOW, True)
    c.close()
    r = put(env, narrow_obj(rogue))
    assert (r.status_code, err(r)) == (403, "other_hub")
    assert state_of(env) == "live"
    assert hub.id != rogue.id


def test_a_staged_link_is_not_a_release(env):
    hub = full(env)
    add_share(env, state="staged")
    assert err(put(env, narrow_obj(hub))) == "not_found"


# --- signature before nonce, replay, nonce lifetime, window -----------------------------------------------------


def test_signature_comes_before_the_nonce_is_stored(env):
    hub = full(env)
    add_share(env)
    forged = narrow_obj(hub, key=Hub().op)
    r = put(env, forged)
    assert (r.status_code, err(r)) == (401, "bad_hub_signature")
    assert env.sql("SELECT * FROM nonces") == []  # a forger cannot burn nonces
    assert state_of(env) == "live"


def test_replay_is_refused(env):
    hub = full(env)
    add_share(env)
    obj = narrow_obj(hub)
    assert put(env, obj).status_code == 200
    r = put(env, obj)
    assert (r.status_code, err(r)) == (401, "replay")


def test_nonces_are_kept_at_least_240_seconds(env):
    hub = full(env)
    add_share(env)
    assert auth.NONCE_RETENTION_MS >= 240_000
    assert put(env, narrow_obj(hub)).status_code == 200
    until = env.sql("SELECT until_ms FROM nonces")[0][0]
    assert until - NOW >= 240_000
    # a copy replayed late, while its timestamp is still inside the +-120 s window, is a replay
    obj = narrow_obj(hub, "app", APP, ts=NOW)
    add_app(env)
    assert put(env, obj).status_code == 200
    env.now = NOW + 119_000
    assert err(put(env, obj)) == "replay"


def test_stale_timestamp_is_refused_before_anything_else(env):
    hub = full(env)
    add_share(env)
    r = put(env, narrow_obj(hub, ts=NOW - 121_000))
    assert (r.status_code, err(r)) == (401, "stale_timestamp")
    assert state_of(env) == "live"


def test_unknown_hub_and_malformed(env):
    full(env)
    add_share(env)
    assert err(put(env, narrow_obj(Hub()))) == "unknown_hub"
    assert err(put(env, {"o": {}, "sig": "x"})) == "malformed"
    assert err(put(env, [1])) == "malformed"
    assert env.c.put("/v2/hub/narrow", content=b"{").status_code == 400
    assert env.c.get("/v2/hub/narrow").status_code == 405


def test_extra_members_are_malformed(env):
    hub = full(env)
    add_share(env)
    obj = narrow_obj(hub, extra="x")
    assert err(put(env, obj)) == "malformed"


# --- the held card is the one that refuses dev cards -----------------------------------------------------------


def test_a_dev_hub_card_is_refused_at_a_real_publish(env):
    hub = full(env)  # a dev card, stored while the publish was in dev mode
    add_share(env)
    env.app.state.settings.dev_mode = False  # the same server, now a real one
    r = put(env, narrow_obj(hub))
    assert (r.status_code, err(r)) == (403, "dev_refused")
    assert state_of(env) == "live"
    assert env.sql("SELECT * FROM nonces") == []


# --- share.extend (cond. 14) ------------------------------------------------------------------------------------


def post(e, path, body):
    return e.send("POST", f"/v1/ws/{WS_A}/shares/{SHARE}/{path}", body=json.dumps(body).encode())


@pytest.fixture
def ws_env(tmp_path):
    e = Env(tmp_path)
    e.allow()
    e.admit()
    return e


def test_extend_sets_the_end_days_from_now(ws_env):
    add_share(ws_env, exp=NOW + 86_400_000)
    r = post(ws_env, "extend", {"days": 30})
    assert (r.status_code, r.json()["expires_ms"]) == (200, NOW + 30 * 86_400_000)
    assert ws_env.sql("SELECT expires_ms FROM shares")[0][0] == NOW + 30 * 86_400_000


@pytest.mark.parametrize("body", [{"days": 31}, {"days": 0}, {"days": -1}, {"days": 1.5}, {"days": "3"},
                                  {"days": True}, {}, {"days": 3, "x": 1}, {"days": None}])  # fmt: skip
def test_extend_is_bounded_to_30_days(ws_env, body):
    add_share(ws_env, exp=NOW + 1000)
    r = post(ws_env, "extend", body)
    assert r.status_code == 400
    assert ws_env.sql("SELECT expires_ms FROM shares")[0][0] == NOW + 1000


def test_extend_never_shortens_and_not_a_dead_link(ws_env):
    add_share(ws_env, exp=NOW + 20 * 86_400_000)
    assert err(post(ws_env, "extend", {"days": 3})) == "not_longer"
    ws_env.sql("UPDATE shares SET state='revoked'")
    assert post(ws_env, "extend", {"days": 3}).status_code == 410
    ws_env.sql("UPDATE shares SET state='live', expires_ms=?", NOW - 1)
    assert post(ws_env, "extend", {"days": 3}).status_code == 410


def test_extend_another_workspaces_link_is_not_found(ws_env):
    add_share(ws_env, ws=WS_B)
    assert post(ws_env, "extend", {"days": 3}).status_code == 404


# --- share.password (cond. 8, C5) ------------------------------------------------------------------------------


def record(pw="five plain words here now"):
    return P.make_password_record(pw, os.urandom(16))


def test_a_valid_c5_record_is_stored_and_listed_as_has_password(ws_env):
    add_share(ws_env)
    rec = record()
    r = post(ws_env, "password", {"pw": rec})
    assert (r.status_code, r.json()) == (200, {"id": SHARE, "has_password": True})
    assert json.loads(ws_env.sql("SELECT pw FROM shares")[0][0]) == rec
    assert ws_env.send("GET", f"/v1/ws/{WS_A}/shares").json()["shares"][0]["has_password"] is True
    assert "pw" not in ws_env.send("GET", f"/v1/ws/{WS_A}/shares").json()["shares"][0]


def test_a_password_is_never_null_and_never_a_plain_string(ws_env):
    add_share(ws_env)
    for body in (
        {"pw": None},
        {"pw": "secret"},
        {"pw": ""},
        {},
        {"pw": record(), "x": 1},
        {"password": "x"},
    ):
        assert post(ws_env, "password", body).status_code == 400, body
    assert ws_env.sql("SELECT pw FROM shares")[0][0] is None
    # a link that has a password keeps it when someone sends null
    assert post(ws_env, "password", {"pw": record()}).status_code == 200
    before = ws_env.sql("SELECT pw FROM shares")[0][0]
    assert post(ws_env, "password", {"pw": None}).status_code == 400
    assert ws_env.sql("SELECT pw FROM shares")[0][0] == before


@pytest.mark.parametrize(
    "edit",
    [{"n": 16384}, {"n": 65536}, {"r": 1}, {"p": 2}, {"alg": "argon2"}, {"salt": "AAAA"}, {"dk": "AAAA"},
     {"n": "32768"}, {"extra": 1}],
)  # fmt: skip
def test_only_the_exact_c5_parameters_are_taken(ws_env, edit):
    add_share(ws_env)
    rec = {**record(), **edit}
    assert post(ws_env, "password", {"pw": rec}).status_code == 400
    assert ws_env.sql("SELECT pw FROM shares")[0][0] is None


def test_password_of_a_dead_or_foreign_link(ws_env):
    add_share(ws_env, ws=WS_B)
    assert post(ws_env, "password", {"pw": record()}).status_code == 404


# --- the password gate on opening a link -----------------------------------------------------------------------


def test_the_gate_asks_for_the_password_and_a_wrong_one_is_not_a_view(tmp_path):
    from fastapi.testclient import TestClient

    from pubsrv.serve import create_share_app

    e = Env(tmp_path)
    e.allow()
    e.admit()
    tok = "T" * 43
    import hashlib

    add_share(e, kind="once")
    e.sql("UPDATE shares SET token_hash=?, view_limit=1", hashlib.sha256(tok.encode()).hexdigest())
    rec = P.make_password_record("right words", os.urandom(16))
    e.sql("UPDATE shares SET pw=?", json.dumps(rec))
    web = TestClient(
        create_share_app(e.data, clock=lambda: e.now), base_url="http://x", follow_redirects=False
    )
    assert 'name="p"' in web.get(f"/s/{SHARE}").text
    bad = web.post(f"/s/{SHARE}/open", content=f"t={tok}&p=wrong".encode())
    assert bad.status_code == 401
    missing = web.post(f"/s/{SHARE}/open", content=f"t={tok}".encode())
    assert missing.status_code == 401
    assert (
        e.sql("SELECT views FROM shares")[0][0] == 0
    )  # a wrong password is not a view (the one view is intact)
    ok = web.post(f"/s/{SHARE}/open", content=f"t={tok}&p=right+words".encode())
    assert ok.status_code == 303
    assert e.sql("SELECT views FROM shares")[0][0] == 1


def test_password_tries_are_rate_limited_before_the_kdf(tmp_path, monkeypatch):
    import hashlib

    from fastapi.testclient import TestClient

    from pubsrv.serve import create_share_app

    e = Env(tmp_path)
    e.allow()
    e.admit()
    tok = "T" * 43
    add_share(e)
    e.sql("UPDATE shares SET token_hash=?", hashlib.sha256(tok.encode()).hexdigest())
    e.sql(
        "UPDATE shares SET pw=?", json.dumps(P.make_password_record("right words", os.urandom(16)))
    )
    calls = []
    real = P.password_ok
    monkeypatch.setattr(P, "password_ok", lambda *a: calls.append(1) or real(*a))
    web = TestClient(
        create_share_app(e.data, clock=lambda: e.now), base_url="http://x", follow_redirects=False
    )
    codes = [
        web.post(f"/s/{SHARE}/open", content=f"t={tok}&p=no{i}".encode()).status_code
        for i in range(8)
    ]
    assert codes[:5] == [401] * 5 and codes[5:] == [429] * 3
    assert len(calls) == 5  # the sixth try never reached the KDF
