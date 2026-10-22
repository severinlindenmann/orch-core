"""H1 (orch-hub, orch-core #861/#864): the hub routes a paired phone needs (protocol.md §6.4): the signed card + API origin,
the pairing -> channel session bind, the session scopes, the push key, cert renewal, user removal."""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest
from channels_helpers import new_channel, prove
from cryptography.hazmat.primitives.asymmetric import ec
from relay_helpers import FakeRelay
from webapi_helpers import ORIGIN, WS, Web

import orch_hub
from orch_hub.channels.pushkeys import PushKeyStore
from orch_hub.contract import channelapi as CA
from orch_hub.contract import hub as H
from orch_hub.contract import suite as S
from orch_hub.contract.canon import b64u, cj
from orch_hub.contract.labels import L
from orch_hub.relay import PushNotifier, RelayClient

TOKEN = bytes(range(40))
DAY = 86_400_000
HOUR = 3_600_000


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    x.api.d.api_origin = ORIGIN
    x.relay = FakeRelay(x.clock)
    client = RelayClient(
        x.env.keys, "http://127.0.0.1:9", transport=x.relay, now_ms=x.clock, sleep=lambda s: None, dev=True
    )
    client.register()
    x.notifier = PushNotifier(client, x.env.svc, PushKeyStore(x.env.svc.db))
    x.api.d.push = x.notifier
    x.env.svc.on_revoke = x.notifier.channel_revoked
    yield x
    x.close()


def push_key() -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    k = ec.generate_private_key(ec.SECP256R1())
    return k, S.public_bytes(k)


def pair(w: Web, user: str, signer=None):
    """Sign in (pairing session), register a phone with an operator code. Returns (signer, channel_id, pairing token)."""
    signer = signer or new_channel()
    code = w.env.svc.create_pairing_code(user)["code"]
    tok = w.login(user, "app")
    s, begun, _ = w.call(
        "POST",
        "/channels/register/begin",
        {"kind": "phone", "key_pub": b64u(signer.public), "pairing_code": code},
        token=tok,
    )
    assert s == 200, begun
    s, got, _ = w.call(
        "POST",
        "/channels/register/complete",
        {"challenge_id": begun["challenge_id"], "signed": prove(signer, begun)},
        token=tok,
    )
    assert s == 200, got
    return signer, got["channel_cert"]["o"]["channel_id"], tok


def auth_obj(w: Web, signer, cid: str, user: str, token: str, **kw):
    o = {
        "v": 2, "suite": S.SUITE_ID, "kind": "hub.channel_auth", "hv": H.HV, "hub_id": w.env.keys.hub_id,
        "user_id": user, "channel_id": cid, "session_ref": CA.session_ref(token), "ts_ms": w.clock(), "dev": True,
    }  # fmt: skip
    return signer.sign_object({**o, **kw})


def bind(w: Web, signer, cid: str, user: str, tok: str, **kw):
    return w.call("POST", "/session/channel", {"auth": auth_obj(w, signer, cid, user, tok, **kw)}, token=tok)


def phone(w: Web, user: str):
    """A fully paired phone with a channel session. Returns (signer, channel_id, channel session token)."""
    signer, cid, tok = pair(w, user)
    s, o, _ = bind(w, signer, cid, user, tok)
    assert s == 200, o
    return signer, cid, o["token"]


def push_obj(w: Web, signer, cid: str, pub: bytes, **kw):
    o = {
        "v": 2, "suite": S.SUITE_ID, "kind": "hub.channel_push", "hv": H.HV, "hub_id": w.env.keys.hub_id,
        "channel_id": cid, "push_pub": b64u(pub), "issued_ms": w.clock(), "dev": True,
    }  # fmt: skip
    return signer.sign_object({**o, **kw})


def put_push(w: Web, cid: str, tok: str, push_key, token: bytes = TOKEN):
    return w.call(
        "PUT", f"/channels/{cid}/push",
        {"env": "sandbox", "topic": "io.severin.orch", "token": b64u(token), "push_key": push_key}, token=tok,
    )  # fmt: skip


# --- GET /hub/card -----------------------------------------------------------------------------------------------------


def test_card_route_gives_the_card_and_an_origin_the_phone_accepts(w):
    s, o, _ = w.call("GET", "/hub/card", origin=None, csrf=False)
    assert s == 200 and o["card"] == w.env.keys.card()
    held = H.verify_card(o["card"], now_ms=w.clock(), dev_mode=True)
    got = CA.verify_api_origin(o["api_origin"], held, link_api=ORIGIN, now_ms=w.clock(), dev_mode=True)
    assert got == {"origin": ORIGIN}
    with pytest.raises(H.Refused) as e:  # a phishing link naming the real hub_id fails on the origin
        CA.verify_api_origin(o["api_origin"], held, link_api="https://evil.example", now_ms=w.clock(), dev_mode=True)
    assert e.value.code == "other_origin"


def test_the_origin_is_the_configured_value_never_the_request(w):
    s, o, _ = w.call("GET", "/hub/card", origin="https://evil.example", csrf=False)
    assert s == 200 and o["api_origin"]["o"]["origin"] == ORIGIN
    s, o, _ = w.call("GET", "/hub/card", origin=None, csrf=False, host="evil.example")
    assert s == 403  # a foreign Host is refused before anything is signed


def test_origin_is_reissued_with_every_new_op_cert(w):
    first = w.call("GET", "/hub/card")[1]
    w.env.keys.rotate_op()
    second = w.call("GET", "/hub/card")[1]
    assert second["api_origin"]["o"]["op_cert_id"] == w.env.keys.op_cert()["o"]["cert_id"]
    assert second["api_origin"]["o"]["op_cert_id"] != first["api_origin"]["o"]["op_cert_id"]
    held = H.verify_card(second["card"], now_ms=w.clock(), dev_mode=True)
    CA.verify_api_origin(second["api_origin"], held, link_api=ORIGIN, now_ms=w.clock(), dev_mode=True)
    with pytest.raises(H.Refused) as e:  # the old object dies with its op cert
        CA.verify_api_origin(first["api_origin"], held, link_api=ORIGIN, now_ms=w.clock(), dev_mode=True)
    assert e.value.code in ("bad_signature", "stale_op_cert")


def test_no_configured_origin_means_unavailable(w):
    w.api.d.api_origin = None
    assert w.call("GET", "/hub/card")[0] == 503


# --- session scopes ------------------------------------------------------------------------------------------------------


def test_a_pairing_session_can_only_pair_bind_and_sign_out(w):
    _, _, tok = pair(w, w.ann)
    for method, path, body in (
        ("GET", "/session", None),
        ("GET", "/me/workspaces", None),
        ("GET", "/me/needs-you", None),
        ("GET", "/me/channels", None),
        ("POST", "/sign/prepare", {"workspace_id": WS, "channel_id": "ab" * 16, "action": {}}),
        ("POST", "/sign/sign", {"prepared_id": "x", "channel_id": "ab" * 16, "channel_proof": "x"}),
        ("GET", "/admin/users", None),
        ("PUT", f"/channels/{'ab' * 16}/push", {"env": "sandbox", "topic": "t", "token": "AA", "push_key": None}),
    ):
        assert w.call(method, path, body, token=tok)[:2] == (403, {"error": "forbidden_role"}), path
    k = new_channel()  # not even a second phone... and never a browser channel
    s, o, _ = w.call("POST", "/channels/register/begin", {"kind": "browser", "key_pub": b64u(k.public)}, token=tok)
    assert (s, o["error"]) == (403, "forbidden_role")
    assert w.call("DELETE", "/session", token=tok)[0] == 200


def test_a_browser_session_cannot_bind_or_register_a_push(w):
    tok = w.login(w.ann)
    assert w.call("POST", "/session/channel", {"auth": {}}, token=tok)[0] == 403
    assert (
        w.call(
            "PUT",
            f"/channels/{'ab' * 16}/push",
            {"env": "sandbox", "topic": "t", "token": "AA", "push_key": None},
            token=tok,
        )[0]
        == 403
    )


def test_bind_makes_a_channel_session_and_consumes_the_pairing_one(w):
    signer, cid, tok = pair(w, w.ann)
    s, o, _ = bind(w, signer, cid, w.ann, tok)
    assert s == 200 and o["channel_id"] == cid and o["user_id"] == w.ann
    assert o["channel_cert"]["o"]["channel_id"] == cid and o["card"] == w.env.keys.card()
    assert o["expires_ms"] <= w.clock() + 12 * HOUR
    assert w.call("GET", "/session", token=tok)[0] == 401  # the pairing session is gone
    assert bind(w, signer, cid, w.ann, tok)[0] == 401
    assert w.call("GET", "/session", token=o["token"])[1]["user_id"] == w.ann


def test_two_racing_binds_never_both_win(w):
    signer, cid, tok = pair(w, w.ann)
    body = {"auth": auth_obj(w, signer, cid, w.ann, tok)}
    got, gate = [], threading.Barrier(6)

    def go():
        gate.wait()
        got.append(w.call("POST", "/session/channel", body, token=tok)[0])

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(got) == [200] + [401] * 5


@pytest.mark.parametrize(
    ("what", "code"),
    [("session_ref", "challenge_invalid"), ("ts_ms", "stale_timestamp"), ("user_id", "other_user")],
)
def test_bind_refusals_leave_the_pairing_session_alive(w, what, code):
    signer, cid, tok = pair(w, w.ann)
    bad = {"session_ref": CA.session_ref("another"), "ts_ms": w.clock() - 10 * 60_000, "user_id": w.admin}[what]
    s, o, _ = bind(w, signer, cid, w.ann, tok, **{what: bad})
    assert (s, o["error"]) == ({"challenge_invalid": 400, "stale_timestamp": 400, "other_user": 403}[code], code)
    assert bind(w, signer, cid, w.ann, tok)[0] == 200


def test_bind_with_another_key_or_users_channel_is_refused(w):
    signer, cid, tok = pair(w, w.ann)
    forged = auth_obj(w, signer, cid, w.ann, tok)
    other = new_channel()  # another key signing the same object, under the right label
    forged["sig"] = b64u(other._sig(L["sig_hub_channel_auth"] + cj(forged["o"])))
    s, o, _ = w.call("POST", "/session/channel", {"auth": forged}, token=tok)
    assert (s, o["error"]) == (403, "bad_signature")
    bsigner, bcid, _ = pair(w, w.admin)
    s, o, _ = bind(w, bsigner, bcid, w.ann, tok)  # the admin's phone key cannot bind ann's session
    assert (s, o["error"]) == (404, "unknown_channel")


def test_a_browser_channel_cannot_bind_a_phone_session(w):
    tok = w.login(w.ann)
    signer, cid = w.register_channel(tok)
    ptok = w.login(w.ann, "app")
    s, o, _ = bind(w, signer, cid, w.ann, ptok)
    assert (s, o["error"]) == (403, "not_allowed")


def test_a_channel_session_is_bound_to_its_one_channel(w):
    _, _, ctok = phone(w, w.ann)
    _, ocid, _ = pair(w, w.ann)
    # no registering, no admin (even for a hub admin), no other channel
    k = new_channel()
    assert w.call(
        "POST",
        "/channels/register/begin",
        {"kind": "phone", "key_pub": b64u(k.public), "pairing_code": "x"},
        token=ctok,
    )[1] == {"error": "forbidden_role"}
    _, _, atok = phone(w, w.admin)
    assert w.call("GET", "/admin/users", token=atok)[1] == {"error": "forbidden_role"}
    act = {"type": "ticket.updated", "log": "L", "set": {}}
    s, o, _ = w.call("POST", "/sign/prepare", {"workspace_id": WS, "channel_id": ocid, "action": act}, token=ctok)
    assert (s, o["error"]) == (403, "other_user")
    assert w.call("POST", f"/channels/{ocid}/revoke", {}, token=ctok)[1] == {"error": "other_user"}
    assert w.call("GET", "/me/channels", token=ctok)[0] == 200


def test_a_channel_session_ends_when_its_channel_is_revoked_or_after_12_h(w):
    _, cid, ctok = phone(w, w.ann)
    assert w.call("GET", "/me/workspaces", token=ctok)[0] == 200
    w.env.svc.revoke(cid)
    assert w.call("GET", "/me/workspaces", token=ctok)[1] == {"error": "channel_revoked"}
    _, _, ctok = phone(w, w.ann)
    w.clock.advance(12 * HOUR + 1)
    assert w.call("GET", "/me/workspaces", token=ctok)[1] == {"error": "no_session"}


def test_the_12h_limit_runs_from_the_password_sign_in_not_the_bind(w):
    signer, cid = None, None
    signer, cid, tok = pair(w, w.ann)
    w.clock.advance(10 * 60_000 - 40_000)  # late in the 15-minute pairing session
    s, o, _ = bind(w, signer, cid, w.ann, tok)
    assert s == 200
    assert o["expires_ms"] <= w.clock() + 12 * HOUR - 8 * 60_000  # about 12 h after the sign-in


def test_cert_renewal_at_bind(w):
    signer, cid, tok = pair(w, w.ann)
    old = w.env.keys.channel_certs(w.ann)[0]["o"]
    w.clock.advance(20 * DAY)  # 11 days of the 31 left
    tok = w.login(w.ann, "app")
    s, o, _ = bind(w, signer, cid, w.ann, tok)
    assert s == 200
    new = o["channel_cert"]["o"]
    assert new["channel_id"] == cid and new["key_pub"] == old["key_pub"] and new["factor"] == old["factor"]
    assert new["expires_ms"] > old["expires_ms"] and new["issued_ms"] == w.clock()
    # a fresh cert is not reissued
    tok = w.login(w.ann, "app")
    assert bind(w, signer, cid, w.ann, tok)[1]["channel_cert"]["o"]["issued_ms"] == new["issued_ms"]


def test_an_expired_cert_cannot_bind_and_is_not_renewed(w):
    signer, cid, tok = pair(w, w.ann)
    w.clock.advance(32 * DAY)
    tok = w.login(w.ann, "app")
    s, o, _ = bind(w, signer, cid, w.ann, tok)
    assert (s, o["error"]) == (403, "cert_expired")


# --- PUT /channels/{id}/push ---------------------------------------------------------------------------------------------


def test_push_registration_restores_the_hint_and_persists_the_key(w):
    signer, cid, ctok = phone(w, w.ann)
    _, pub = push_key()
    s, o, _ = put_push(w, cid, ctok, push_obj(w, signer, cid, pub))
    assert (s, o) == (200, {"registered": True})
    assert w.relay.targets[cid]["token"] == TOKEN
    assert w.notifier.notify_user(w.ann, "prepare", "cd" * 16) == {cid: "sent"}
    assert len(w.relay.sent) == 1
    # persistent: a new store over the same database holds the key and its issued_ms
    again = PushKeyStore(w.env.svc.db)
    assert again[cid] == pub and again.held_issued_ms(cid) == w.clock()


def test_push_key_replay_and_older_are_refused_and_null_keeps_the_key(w):
    signer, cid, ctok = phone(w, w.ann)
    _, pub = push_key()
    obj = push_obj(w, signer, cid, pub)
    assert put_push(w, cid, ctok, obj)[0] == 200
    s, o, _ = put_push(w, cid, ctok, obj)  # the same registration again
    assert (s, o["error"]) == (409, "replay")
    _, pub2 = push_key()
    s, o, _ = put_push(w, cid, ctok, push_obj(w, signer, cid, pub2, issued_ms=w.clock() - 1000))  # older
    assert (s, o["error"]) == (409, "replay")
    assert PushKeyStore(w.env.svc.db)[cid] == pub
    w.clock.advance(1000)  # a new APNs token, no new key
    assert put_push(w, cid, ctok, None, token=TOKEN[::-1])[0] == 200
    assert w.relay.targets[cid]["token"] == TOKEN[::-1] and PushKeyStore(w.env.svc.db)[cid] == pub
    w.clock.advance(1000)  # newer key replaces
    assert put_push(w, cid, ctok, push_obj(w, signer, cid, pub2))[0] == 200
    assert PushKeyStore(w.env.svc.db)[cid] == pub2


def test_null_push_key_without_a_held_key_is_malformed(w):
    _, cid, ctok = phone(w, w.ann)
    s, o, _ = put_push(w, cid, ctok, None)
    assert (s, o["error"]) == (400, "malformed") and cid not in w.relay.targets


def test_push_key_checks(w):
    signer, cid, ctok = phone(w, w.ann)
    _, pub = push_key()
    # signed by another key
    s, o, _ = put_push(w, cid, ctok, {**push_obj(w, signer, cid, pub), "sig": b64u(bytes(64))})
    assert (s, o["error"]) == (403, "bad_signature")
    # the signing key as push key (the signer refuses to make it, so sign by hand under the right label)
    bad = {"v": 2, "suite": S.SUITE_ID, "kind": "hub.channel_push", "hv": H.HV, "hub_id": w.env.keys.hub_id,
           "channel_id": cid, "push_pub": b64u(signer.public), "issued_ms": w.clock(), "dev": True}  # fmt: skip
    msg = L["sig_hub_channel_push"] + cj(bad)
    raw = {"o": bad, "sig": b64u(signer._sig(msg))}
    s, o, _ = put_push(w, cid, ctok, raw)
    assert (s, o["error"]) == (400, "malformed")
    # stale
    s, o, _ = put_push(w, cid, ctok, push_obj(w, signer, cid, pub, issued_ms=w.clock() - 10 * 60_000))
    assert (s, o["error"]) == (400, "stale_timestamp")
    # another channel's id in the path
    s, o, _ = put_push(w, "ab" * 16, ctok, push_obj(w, signer, cid, pub))
    assert (s, o["error"]) == (403, "other_user")
    assert w.relay.targets == {}


def test_revoking_the_channel_drops_the_push_key(w):
    signer, cid, ctok = phone(w, w.ann)
    _, pub = push_key()
    put_push(w, cid, ctok, push_obj(w, signer, cid, pub))
    w.env.svc.revoke(cid)
    assert cid not in PushKeyStore(w.env.svc.db) and cid not in w.relay.targets


# --- user removal ---------------------------------------------------------------------------------------------------------


def test_removing_a_user_revokes_and_purges_every_channel_of_the_user(w):
    s1, c1, t1 = phone(w, w.ann)
    _, c2, _ = pair(w, w.ann)
    _, pub = push_key()
    put_push(w, c1, t1, push_obj(w, s1, c1, pub))
    w.relay.mailbox[(c1, "aa" * 16)] = {"c": "x"}
    atok = w.login(w.admin)
    s, o, _ = w.call("DELETE", f"/admin/users/{w.ann}", token=atok)
    assert s == 200, o
    assert {c1, c2} <= set(w.env.acc.revoked_channels())
    assert c1 not in w.relay.targets and (c1, "aa" * 16) not in w.relay.mailbox
    assert c1 not in PushKeyStore(w.env.svc.db)
    assert w.call("GET", "/session", token=t1)[0] == 401


def test_a_removal_that_is_refused_revokes_nothing(w):
    _, c1, _ = phone(w, w.ann)
    assert w.call("DELETE", f"/admin/users/{w.admin}", token=w.login(w.ann))[0] == 403  # not a hub admin
    assert w.call("DELETE", f"/admin/users/{'ab' * 16}", token=w.login(w.admin))[1] == {"error": "unknown_user"}
    assert c1 not in w.env.acc.revoked_channels()


# --- the mailbox purge (L2B-11) -----------------------------------------------------------------------------------------


def _calls(name: str):
    """Every call of ``.name(...)`` / ``name(...)`` in src/orch_hub: (file, enclosing function)."""
    src, out = Path(orch_hub.__file__).parent, []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for n in ast.walk(fn):
                if isinstance(n, ast.Call):
                    f = n.func
                    if (isinstance(f, ast.Attribute) and f.attr == name) or (isinstance(f, ast.Name) and f.id == name):
                        out.append((path.name, fn.name))
    return out


def test_the_mailbox_purge_is_reachable_only_from_on_revoke_and_restart_pass():
    # the only caller of the relay's DELETE of a whole mailbox
    assert {c for c in _calls("mailbox_delete") if c[0] != "client.py"} == {("notify.py", "channel_revoked")}
    # channel_revoked is called by its async hook (the one wired to Channels.on_revoke) and by flush (the retry of
    # pending_cleanup), and by nothing else
    assert sorted(set(_calls("channel_revoked"))) == [("notify.py", "flush")]
    assert (Path(orch_hub.__file__).parent / "relay" / "notify.py").read_text().count(
        "target=self.channel_revoked,"
    ) == 1
    # flush is called by the notify path (retrying only what channel_revoked/restart_pass queued) and restart_pass
    assert sorted(c for c in set(_calls("flush")) if c[0] == "notify.py") == [
        ("notify.py", "notify_user"),
        ("notify.py", "restart_pass"),
    ]
    # pending_cleanup gets entries only from a failed channel_revoked and from restart_pass's revoked channels
    src = (Path(orch_hub.__file__).parent / "relay" / "notify.py").read_text()
    assert src.count("pending_cleanup.add(") == 1 and src.count("pending_cleanup |=") == 1
    # and the async hook is wired to on_revoke only
    wired = [
        ln.strip()
        for p in Path(orch_hub.__file__).parent.rglob("*.py")
        for ln in p.read_text().splitlines()
        if "channel_revoked_async" in ln and "def " not in ln and not ln.strip().startswith(("#", '"""'))
    ]
    assert wired == [
        "channels.on_revoke = notifier.channel_revoked_async  # D10: a revoked channel's push target and mailbox go"
    ]


def test_a_live_channel_never_has_its_mailbox_purged(w):
    signer, cid, ctok = phone(w, w.ann)
    _, pub = push_key()
    put_push(w, cid, ctok, push_obj(w, signer, cid, pub))
    for _ in range(3):
        w.notifier.notify_user(w.ann, "prepare", "cd" * 16)
    w.notifier.restart_pass()  # nothing revoked: nothing purged
    assert not [x for x in w.relay.log if x[0] == "DELETE"]


# --- the operator's link ---------------------------------------------------------------------------------------------------


def test_pair_code_link_is_the_6_4_link(w, tmp_path):
    from orch_hub.channels.cli import pair_link

    code = w.env.svc.create_pairing_code(w.ann)["code"]
    link = pair_link(w.env.keys.hub_id, w.ann, code, "https://hub.dev.example:8443", False)
    assert link == f"orch://hub-pair/hub.dev.example:8443#h1.{w.env.keys.hub_id}.{w.ann}.{code}"
    got = CA.parse_pair_link(link, dev_mode=False)
    assert got == {"api": "https://hub.dev.example:8443", "hub_id": w.env.keys.hub_id, "user_id": w.ann, "code": code}
    assert pair_link(w.env.keys.hub_id, w.ann, code, "http://127.0.0.1:8443", True).startswith(
        "orch://hub-pair/127.0.0.1:8443#"
    )
    from orch_hub.channels import ChannelError

    for bad, dev in (
        (None, True),  # no origin given: no default
        ("http://hub.example", True),  # plain http on a real host
        ("http://127.0.0.1:8443", False),  # loopback at a real hub
        ("https://Hub.Example", False),  # not lower case
        ("https://hub.example/path", False),
        ("ftp://hub.example", False),
    ):
        with pytest.raises(ChannelError):
            pair_link(w.env.keys.hub_id, w.ann, code, bad, dev)


def test_hubconfig_api_origin_is_one_of_the_allowed_origins():
    from orch_hub.config import HubConfig

    base = {"dev": True, "listen": "127.0.0.1:0", "web_listen": "127.0.0.1:0"}
    one = HubConfig(**base, allowed_origins=frozenset({"https://a.example"}))
    assert one.resolved_api_origin() == "https://a.example"
    two = HubConfig(**base, allowed_origins=frozenset({"https://a.example", "https://b.example"}))
    assert two.resolved_api_origin() is None
    assert (
        HubConfig(**base, allowed_origins=two.allowed_origins, api_origin="https://b.example").resolved_api_origin()
        == "https://b.example"
    )
    with pytest.raises(ValueError):  # a value that is not an allowed origin
        HubConfig(**base, allowed_origins=two.allowed_origins, api_origin="https://c.example")
    with pytest.raises(ValueError):  # behind the edge with several origins and no choice
        HubConfig(**base, allowed_origins=two.allowed_origins, behind_edge=True, trusted_peers=frozenset({"10.0.0.1"}))
