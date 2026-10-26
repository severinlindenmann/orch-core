"""L2 security review of the B stream contract (design-b-phone.md, orch-hub#39): adversarial tests.

Passing tests pin properties the review confirmed. ``xfail(strict=True)`` tests state a property the contract or the
hub does not have yet; each names its finding in state/l2-review-p4.md ("B phone design + hub#39") and flips to a pass
(and so fails strict) when it is fixed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from test_relay_client import TOKEN, World

from orch_hub.contract import channelapi as CA
from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract import relayroutes as RR
from orch_hub.contract.canon import b64u, loads_strict
from orch_hub.contract.cases import run
from orch_hub.relay import PushNotifier

VEC = json.loads((Path(__file__).resolve().parents[3] / "contracts" / "hub" / "vectors_hub.json").read_text())
CASES = {c["name"]: c for c in VEC["cases"]}
HUB = "38d8abed52ccfb538da401fd54e64bdc"
USER = "a368bd380588e5dabee62a516031e21f"
FRAG = f"#h1.{HUB}.{USER}.ab12-cd34-ef56"


# --- question: is the dev gate of parse_pair_link complete? ------------------------------------------------------


@pytest.mark.parametrize("authority", ["[::1]:8443", "localhost", "localhost:1", "127.0.0.1", "127.0.0.1:80"])
def test_every_loopback_spelling_is_refused_at_a_real_verifier(authority):
    with pytest.raises(H.Refused) as e:
        CA.parse_pair_link("orch://hub-pair/" + authority + FRAG, dev_mode=False)
    assert e.value.code == "malformed"


@pytest.mark.parametrize("authority", ["127.0.0.2:8443", "0.0.0.0:80", "8.8.8.8", "127.1", "localhost.localdomain"])
def test_other_local_addresses_never_become_plain_http(authority):
    """Not in the loopback list → https (WebPKI then fails for them): no way to plain http outside the dev gate."""
    for dev in (True, False):
        assert CA.parse_pair_link("orch://hub-pair/" + authority + FRAG, dev_mode=dev)["api"].startswith("https://")


@pytest.mark.parametrize("dev", [1, "true", None, False])
def test_dev_gate_wants_the_literal_true(dev):
    with pytest.raises(H.Refused):
        CA.parse_pair_link("orch://hub-pair/localhost:8443" + FRAG, dev_mode=dev)


# --- finding L2B-1 (P1): the card pin does not authenticate the API host -------------------------------------------


def test_card_pin_binds_the_api_origin():
    """L2B-1 (fixed in hub#39): the hub signs its API origin (``hub.api_origin``, a separate object because core,
    publish and the relay check the card's exact field set); a link to a phishing host fails before any password."""
    link = CA.parse_pair_link("orch://hub-pair/hub-login.attacker.example" + FRAG, dev_mode=True)
    assert link["hub_id"] == HUB  # the pin is copied from the real hub's public card: anyone can
    a = CASES["api_origin_valid"]["args"]
    held = H.verify_card(a["card"], now_ms=a["now_ms"], dev_mode=True)
    with pytest.raises(H.Refused) as e:
        CA.verify_api_origin(a["signed"], held, link_api=link["api"], now_ms=a["now_ms"], dev_mode=True)
    assert e.value.code == "other_origin"
    real = CA.parse_pair_link("orch://hub-pair/hub.dev.orch.example" + FRAG, dev_mode=True)
    assert CA.verify_api_origin(a["signed"], held, link_api=real["api"], now_ms=a["now_ms"], dev_mode=True)


# --- finding L2B-4 (P1 for H1): the hub still seals hints to the channel's signing key --------------------------------


@pytest.fixture
def w(tmp_path):
    x = World(tmp_path)
    yield x
    x.env.close()


def test_hub_never_seals_a_hint_to_the_channel_signing_key(w):
    w.client.register()
    u = w.env.user("ann")
    signer, cid = w.phone(u)
    n = PushNotifier(w.client, w.env.svc)
    n.register_target(u, cid, "sandbox", "io.severin.orch", TOKEN)
    n.notify_user(u, "prepare", "cd" * 16)
    for sent in w.relay.sent:  # with no push key registered, nothing may be sent at all
        outer = loads_strict(sent["outer"])
        with pytest.raises(H.Refused):
            RR.open_push(outer["c"], hub_id=outer["hub"], channel_id=cid, msg_id=outer["id"], recipient_priv=signer)
    assert w.relay.sent == []


# --- confirmed: the channel key's typed signer cannot be used beyond its own channel --------------------------------


def _signer():
    s = KS.ChannelSigner(ec.generate_private_key(ec.SECP256R1()))
    return s, s.channel_id


def test_channel_signer_speaks_for_its_own_channel_only():
    s, cid = _signer()
    other = "ee" * 16
    base = {"v": 2, "suite": 2, "hv": 0, "hub_id": HUB, "dev": True}
    with pytest.raises(KS.KeyUseError):
        s.sign_object(base | {"kind": "hub.channel_auth", "user_id": USER, "channel_id": other,
                              "session_ref": b64u(bytes(32)), "ts_ms": 1})  # fmt: skip
    with pytest.raises(KS.KeyUseError):
        s.sign_object(base | {"kind": "hub.channel_push", "channel_id": cid, "push_pub": b64u(s.public),
                              "issued_ms": 1})  # fmt: skip
    with pytest.raises(KS.KeyUseError):
        s.relay_request(HUB, 1, bytes(16), "GET", f"/v2/hub-channels/{HUB}/{other}/mailbox", b"")
    with pytest.raises(KS.KeyUseError):  # the hub's own prefix is never channel-signed
        s.relay_request(HUB, 1, bytes(16), "DELETE", f"/v2/hubs/{HUB}/mailbox/{cid}", b"")
    with pytest.raises(KS.KeyUseError):  # no free-form object kinds (e.g. a custodial or card object)
        s.sign_object(base | {"kind": "hub.card"})


# --- finding L2B-9 (P2, inherited L2P4-12): duplicate channel headers are not refused --------------------------------


def test_duplicate_channel_header_is_malformed():
    a = json.loads(json.dumps(CASES["channel_request_list"]["args"]))
    hdr = a["request"]["headers"]
    k = next(k for k in hdr if k.lower() == "orch-channel-nonce")
    hdr[k.upper()] = "00" * 16  # a second, differently spelt nonce header
    hdr[k] = hdr.pop(k)  # the original spelling last, so the signed one wins the dict
    with pytest.raises(H.Refused) as e:
        run("channel_request", a)
    assert e.value.code == "malformed"
