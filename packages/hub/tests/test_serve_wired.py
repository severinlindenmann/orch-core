"""``orch-hub serve`` with every phase-4 service wired, in process on loopback with a fake relay (orch-hub#30).

One hub: the link server (a fake HOST joins a full-mode workspace, seals a ``wk_seal`` and syncs its log), the web API
(sign-in, ``GET /me/needs-you``, channel registration and revoke) and the relay client (push on a prepare, push-target
delete on a revoke) all run in this process, as ``serve()`` builds them from one ``HubConfig``.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import re
import threading
import time

import pytest
from channels_helpers import PW, new_channel, prove, wall_codes
from cryptography.hazmat.primitives.asymmetric import ec
from custodial_helpers import LOG, WK, core_seal
from publish_helpers import FakeUpstream, _signed, pub_b64
from relay_helpers import FakeRelay
from view_helpers import OWNER, Host

from orch_hub import serve as SV
from orch_hub.access import Access
from orch_hub.channels import Channels
from orch_hub.channels.service import CHANNELS_FILE
from orch_hub.config import HubConfig
from orch_hub.contract import channelapi as CA
from orch_hub.contract import keystore as KS
from orch_hub.contract import relayroutes as RR
from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, loads_strict
from orch_hub.keys import HubKeys
from orch_hub.link import LinkClient

WS = G.WS_A
READY = re.compile(r"orch-hub ready port=(\d+) tls_spki_sha256=([A-Za-z0-9_-]{43}) hub_id=([0-9a-f]{32})\n")
WEB = re.compile(r"orch-hub web port=(\d+)\n")


def now_ms() -> int:
    return int(time.time() * 1000)


class Web:
    """A tiny HTTP client of the hub's web API (Origin and CSRF header as the console sends them)."""

    def __init__(self, port: int) -> None:
        self.port, self.origin = port, f"http://127.0.0.1:{port}"

    def call(self, method: str, path: str, body=None, token: str | None = None):
        h = {"Origin": self.origin, "X-Orch-Csrf": "1", "Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, "/api/v1" + path, body=None if body is None else json.dumps(body), headers=h)
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        finally:
            c.close()


def until(fn, what: str, secs: float = 10.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError(f"timed out: {what}")


@pytest.fixture
def hub(tmp_path):
    yield from _hub(tmp_path, real=False)


@pytest.fixture
def hub_real(tmp_path):
    """The same wired hub as a NON-dev hub (card dev: false), the kind a normal install runs (run-11 B1)."""
    yield from _hub(tmp_path, real=True)


def _hub(tmp_path, real):
    state = tmp_path / "hub"
    keys = HubKeys.init(state, relay_url="http://127.0.0.1:9", real=real)
    # the operator side, before the hub runs: a user with password and TOTP, a group with edit on the workspace, an
    # admin-issued invite that names the user as the workspace owner, a pairing code for the user's phone
    acc = Access(str(state / "access.db"))
    user = keys.issue_uk(None)["o"]["user_id"]
    acc.add_user(user, "ann")
    acc.add_group("team")
    acc.add_member("team", user)
    acc.grant(WS, "team", "edit")
    ch = Channels(keys, acc, state / CHANNELS_FILE, dev_mode=not real)
    ch.set_password(user, PW)
    b32 = ch.new_totp(user)["secret"]
    secret = base64.b32decode(b32 + "=" * (-len(b32) % 8))
    pairing = ch.create_pairing_code(user)["code"]
    ch.close()
    acc.close()

    up = FakeUpstream()
    relay = FakeRelay(now_ms)
    stop, out = threading.Event(), io.StringIO()
    result: list = []
    t = threading.Thread(
        target=lambda: result.append(
            SV.serve(
                state,
                "127.0.0.1:0",
                up.url,
                [f"inv1={user}"],
                web_listen="127.0.0.1:0",
                relay_transport=relay,
                stop=stop,
                out=out,
            )
        ),
        daemon=True,
    )
    t.start()
    until(lambda: WEB.search(out.getvalue()), "ready lines")
    m, w = READY.match(out.getvalue()), WEB.search(out.getvalue())
    assert m and w, out.getvalue()
    keys = HubKeys.open(state)  # the card was re-issued with the TLS pin
    relay("POST", "/v2/hubs", {}, json.dumps(keys.card()).encode(), 5)  # the hub's relay account
    yield {
        "state": state, "keys": keys, "user": user, "secret": secret, "pairing": pairing, "relay": relay,
        "link_port": int(m[1]), "pin": m[2], "web": Web(int(w[1])),
    }  # fmt: skip
    stop.set()
    t.join(15)
    assert not t.is_alive() and result == [0]
    up.close()


def host_link(h) -> LinkClient:
    keys = h["keys"]
    c = LinkClient.open(
        "127.0.0.1", h["link_port"], tls_spki_sha256=h["pin"], hub_id=keys.hub_id, workspace_id=WS,
        wsk=KS.WskSigner(G.K.priv["wsk_a"]), hk_op_pub=keys.op_signer.public, send_wsk_pub=True, timeout=5,
    )  # fmt: skip
    c.mode = "full"
    wsk = G.K.priv["wsk_a"]
    binding = _signed(
        wsk, "sig_hub_binding",
        {
            "v": 2, "suite": 2, "hv": 0, "kind": "hub.binding", "workspace_id": WS, "wsk_pub": pub_b64(wsk),
            "hub_id": keys.hub_id, "hk_root_pub": b64u(keys.hk_root_pub), "mode": "full", "binding_seq": 1,
            "valid_from_ms": now_ms() - 1000, "dev": True,
        },
    )  # fmt: skip
    body = {"invite": "inv1", "card": G.v2_card(), "pk_pub": b64u(G.K.pub["pk_alice"]), "binding": binding}
    assert c.request("join", body) == {}
    assert c.request("wk_seal", {"epoch": 1, "wrap": b64u(core_seal(WK, keys.hub_id, WS, 1, keys.hxk_pub))}) == {}
    return c


def sign_in(h) -> str:
    (code,) = wall_codes(h["secret"])
    s, o = h["web"].call("POST", "/session", {"user_id": h["user"], "password": PW, "totp": code, "client": "app"})
    assert s == 200, o
    return o["token"]


def register_phone(h, token: str):
    """Pair a phone through the pairing session and bind it (protocol §6.4). Returns (signer, channel_id, channel
    session token)."""
    signer = new_channel()
    web = h["web"]
    s, begun = web.call(
        "POST", "/channels/register/begin",
        {"kind": "phone", "key_pub": b64u(signer.public), "pairing_code": h["pairing"]}, token,
    )  # fmt: skip
    assert s == 200, begun
    s, got = web.call(
        "POST", "/channels/register/complete",
        {"challenge_id": begun["challenge_id"], "signed": prove(signer, begun)}, token,
    )  # fmt: skip
    assert s == 200, got
    cid = got["channel_cert"]["o"]["channel_id"]
    auth = signer.sign_object(
        {
            "v": 2, "suite": 2, "kind": "hub.channel_auth", "hv": 0, "hub_id": h["keys"].hub_id, "user_id": h["user"],
            "channel_id": cid, "session_ref": CA.session_ref(token), "ts_ms": now_ms(), "dev": True,
        }
    )  # fmt: skip
    s, bound = web.call("POST", "/session/channel", {"auth": auth}, token)
    assert s == 200, bound
    return signer, cid, bound["token"]


def test_the_wired_hub_end_to_end(hub):
    web, relay = hub["web"], hub["relay"]
    # 1. sign in over the web API; a wrong password is refused
    s, o = web.call("POST", "/session", {"user_id": hub["user"], "password": "x" * 16, "totp": "000000"})
    assert (s, o["error"]) == (401, "bad_credentials")
    pairing = sign_in(hub)
    # the card is public; the pairing session may do nothing but pair and bind
    s, o = web.call("GET", "/hub/card")
    assert s == 200 and o["card"]["o"]["hub_id"] == hub["keys"].hub_id
    assert o["api_origin"]["o"]["origin"] == web.origin  # the loopback address the web API signed from its config
    assert web.call("GET", "/session", None, pairing) == (403, {"error": "forbidden_role"})
    signer, cid, token = register_phone(hub, pairing)
    assert web.call("GET", "/session", None, token)[1]["user_id"] == hub["user"]

    # 2. a full-mode workspace's open question reaches GET /me/needs-you through log_sync
    c = host_link(hub)
    h = Host(ws=WS, wk=WK, key=G.K.priv["wsk_a"])
    h.genesis()
    h.append("workspace", "member.added", roster_v=1)
    h.ticket()
    qhash = h.ask(to=OWNER, text="CSV or API?")
    for log in ("workspace", LOG):
        assert "seq" in c.request("log_sync", h.page(log))
    s, o = web.call("GET", "/me/needs-you", None, token)
    assert s == 200, o
    items = [i for ws in o.get("workspaces", []) for i in ws.get("items", [])] or o.get("items", [])
    assert any(i.get("title") == "CSV or API?" or i.get("hash") == qhash for i in items), o

    # 3. a prepare: the phone registers its APNs target and push key (H1), the hub seals the hint to that key
    priv = ec.generate_private_key(ec.SECP256R1())
    push_obj = signer.sign_object(
        {
            "v": 2, "suite": 2, "kind": "hub.channel_push", "hv": 0, "hub_id": hub["keys"].hub_id, "channel_id": cid,
            "push_pub": b64u(S.public_bytes(priv)), "issued_ms": now_ms(), "dev": True,
        }
    )  # fmt: skip
    s, o = web.call(
        "PUT", f"/channels/{cid}/push",
        {"env": "sandbox", "topic": "io.severin.orch", "token": b64u(b"\x01" * 32), "push_key": push_obj}, token,
    )  # fmt: skip
    assert (s, o) == (200, {"registered": True})
    assert cid in relay.targets
    act = {"type": "ticket.updated", "log": LOG, "set": {"ticket.priority": "high"}}
    s, p = web.call("POST", "/sign/prepare", {"workspace_id": WS, "channel_id": cid, "action": act}, token)
    assert s == 200, p
    until(lambda: relay.sent, "the sealed hint")
    outer = loads_strict(relay.sent[0]["outer"])
    got = RR.open_push(outer["c"], hub_id=outer["hub"], channel_id=cid, msg_id=outer["id"], recipient_priv=priv)
    assert got["type"] == "prepare" and got["ref"] == p["prepared_id"]

    # 4. revoking the channel deletes the push target at the relay (and the held push key)
    assert cid in relay.targets
    s, o = web.call("POST", f"/channels/{cid}/revoke", {}, token)
    assert s == 200, o
    until(lambda: cid not in relay.targets, "push target deleted")
    c.close()


def test_a_non_full_workspace_loses_its_wraps_and_view_at_start(tmp_path):
    """The startup reconcile (D9/D15 reviews): wraps and a synced view of a workspace the registry no longer has as a
    bound full one are retired and dropped, whatever step failed before."""
    from orch_hub.link import Registry
    from orch_hub.link.wkseal import WkSeals
    from orch_hub.reconcile import reconcile_full_only
    from orch_hub.view import build_view_services

    keys = HubKeys.init(tmp_path / "hub")
    seals = WkSeals(str(tmp_path / "wk.db"), hub_id=keys.hub_id, hxk_pub=keys.hxk_pub, exchange=keys.hxk_exchange)
    seals.accept(WS, 1, core_seal(WK, keys.hub_id, WS, 1, keys.hxk_pub))
    reg = Registry()
    (tmp_path / "view").mkdir(mode=0o700)
    vs = build_view_services(tmp_path / "view", wk_seals=seals, registry=reg, access=Access())
    h = Host(ws=WS, wk=WK, key=G.K.priv["wsk_a"])
    h.genesis()
    vs.service.accept(WS, G.K.pub["wsk_a"], h.page("workspace"))
    assert seals.epochs(WS) == [1] and vs.store.workspaces() == [WS]
    reconcile_full_only(reg, seals, vs.store)  # the registry knows no such workspace
    assert seals.epochs(WS) == [] and vs.store.workspaces() == []
    reconcile_full_only(reg, seals, vs.store)  # idempotent
    vs.close()
    seals.close()
    reg.close()


def test_hubconfig_rules():
    card = {"o": {"dev": True, "relay_url": "http://127.0.0.1:9"}}
    cfg = HubConfig.from_card(card, listen="127.0.0.1:0")
    assert cfg.dev is True and cfg.relay_url == "http://127.0.0.1:9"
    with pytest.raises(ValueError):
        HubConfig.from_card({"o": {}}, listen="127.0.0.1:0")  # no dev marker: fail closed
    with pytest.raises(ValueError):
        HubConfig.from_card({"o": {"dev": "yes"}}, listen="127.0.0.1:0")
    with pytest.raises(ValueError):  # behind the edge without its addresses: every request would share one source
        HubConfig(dev=True, listen="127.0.0.1:0", web_listen="127.0.0.1:0", behind_edge=True)
    with pytest.raises(TypeError):  # dev is required
        HubConfig(listen="127.0.0.1:0")  # type: ignore[call-arg]
    with pytest.raises(AttributeError):  # frozen
        cfg.dev = False  # type: ignore[misc]
