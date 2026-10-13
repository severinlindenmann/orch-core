"""``RelaySigner`` (11A.2): typed, purpose-specific WSK signing; each method validates its context first and builds its
label and bytes itself. A permitted label in the wrong context is refused (11A.10 "signer" adversarial row)."""

from __future__ import annotations

import pytest

from orch import canon, crypto
from orch.crypto.labels import L
from orch.relay import sign, wire

from . import corpus

WS = corpus.IDS["workspace_a"]
ORIGIN = "https://relay.dev.severin.io"
WSK = corpus.priv("wsk_a.sig")
WSK_PUB = corpus.pub("wsk_a.sig")


class Key:
    """A fake :class:`sign.WorkspaceKey` that records every payload it signs."""

    def __init__(self, priv=WSK):
        self.priv, self.signed = priv, []

    @property
    def public_key(self) -> bytes:
        return crypto.public_bytes(self.priv)

    def sign(self, payload: bytes) -> bytes:
        self.signed.append(payload)
        return crypto.sign(self.priv, payload)


class Epochs:
    def __init__(self, held=(1, 2)):
        self.held = set(held)

    def holds(self, epoch: int) -> bool:
        return epoch in self.held


def signer(key=None, *, last_card_seq=0, epochs=None, origin=ORIGIN, ws=WS):
    return sign.RelaySigner(
        key or Key(), sign.LinkContext(workspace_id=ws, origin=origin), epochs=epochs, last_card_seq=last_card_seq
    )


# --- login ----------------------------------------------------------------------------------------------------------


def test_login_signs_auth_bytes_for_the_stored_origin_and_workspace():
    k = Key()
    ch = bytes(range(32))
    sig = signer(k).login(ch)
    msg = wire.auth_bytes(ORIGIN, ch, "workspace", WS)
    assert k.signed == [msg]
    assert crypto.verify(WSK_PUB, sig, msg)


@pytest.mark.parametrize("ch", [bytes(31), bytes(33), "00" * 32, None, bytearray(32)])
def test_login_refuses_a_bad_challenge_without_signing(ch):
    k = Key()
    with pytest.raises(sign.SignerRefused):
        signer(k).login(ch)
    assert k.signed == []


@pytest.mark.parametrize(
    "origin,ws", [("https://relay.example/", WS), ("http://relay.example", WS), (ORIGIN, WS.upper()), (ORIGIN, "zz")]
)
def test_bad_link_context_is_refused_at_construction(origin, ws):
    with pytest.raises(sign.SignerRefused):
        signer(origin=origin, ws=ws)


def test_login_wrong_origin_signature_does_not_verify_for_another_origin():
    ch = bytes(32)
    sig = signer().login(ch)
    other = wire.auth_bytes("https://relay.example", ch, "workspace", WS)
    assert not crypto.verify(WSK_PUB, sig, other)
    as_device = wire.auth_bytes(ORIGIN, ch, "device", WS)
    assert not crypto.verify(WSK_PUB, sig, as_device)


def test_signer_that_returns_garbage_is_refused():
    class Bad(Key):
        def sign(self, payload):
            return b"x" * 63

    with pytest.raises(sign.SignerRefused):
        signer(Bad()).login(bytes(32))


# --- response chunk -------------------------------------------------------------------------------------------------


def _header(**kw):
    base = dict(
        version=2,
        direction=2,
        flags=1,
        suite=2,
        workspace=bytes.fromhex(WS),
        device=bytes.fromhex(corpus.IDS["device_phone"]),
        rid=bytes(range(16)),
        stream=bytes(16),
        seq=0,
        ts_ms=1_790_000_000_000,
        epoch=2,
        salt=bytes(16),
    )
    base.update(kw)
    return wire.Header(**base).pack()


def test_response_chunk_signs_header_and_body():
    k = Key()
    h, body = _header(), bytes(100)
    sig = signer(k, epochs=Epochs()).response_chunk(h, body)
    assert k.signed == [L["sig_bridge"] + h + body]
    assert crypto.verify(WSK_PUB, sig, wire.bridge_signed_bytes(h, body))


@pytest.mark.parametrize(
    "header,body,why",
    [
        (_header(direction=1, flags=0), bytes(100), "a request header (direction 1)"),
        (_header(workspace=bytes.fromhex(corpus.IDS["workspace_b"])), bytes(100), "another workspace"),
        (_header(epoch=3), bytes(100), "an epoch BridgeKeys does not hold"),
        (_header(epoch=0), bytes(100), "epoch 0 (pairing is R6)"),
        (_header(), bytes(wire.MAX_CHUNK - wire.HEADER_LEN - 64 + 1), "over the 256 KiB envelope"),
        (_header(), bytes(15), "shorter than a tag"),
        (b"ORB1" + _header()[4:], bytes(100), "bad magic"),
        (_header()[:-1], bytes(100), "short header"),
    ],
)
def test_response_chunk_refusals(header, body, why):
    k = Key()
    with pytest.raises(sign.SignerRefused):
        signer(k, epochs=Epochs()).response_chunk(header, body)
    assert k.signed == [], why


def test_response_chunk_without_bridge_keys_is_refused():
    with pytest.raises(sign.SignerRefused):
        signer().response_chunk(_header(), bytes(100))


def test_response_chunk_at_the_exact_cap():
    body = bytes(wire.MAX_CHUNK - wire.HEADER_LEN - 64)
    signer(epochs=Epochs()).response_chunk(_header(), body)


# --- card -----------------------------------------------------------------------------------------------------------

CARD = corpus.S2["card"]["card"]


def test_card_signs_the_vector_card_object():
    k = Key()
    o = dict(CARD["o"])
    sig = signer(k).card(o)
    assert k.signed == [bytes.fromhex(corpus.S2["card"]["signed_bytes_wsk"])]
    assert crypto.verify(WSK_PUB, crypto.unb64u(sig, 64), L["sig_card_wsk"] + canon.dumps(o))


@pytest.mark.parametrize(
    "change,why",
    [
        ({"workspace_id": corpus.IDS["workspace_b"]}, "another workspace"),
        ({"wsk_pub": crypto.b64u(corpus.pub("wsk_b.sig"))}, "another WSK"),
        ({"relay_url": "https://relay.example"}, "another relay than the stored origin"),
        ({"card_seq": 0}, "card_seq 0"),
        ({"kind": "member_list"}, "another kind"),
        ({"suite": 1}, "another suite"),
        ({"v": 1}, "another version"),
        ({"extra": 1}, "an extra field"),
        ({"card_seq": True}, "a bool card_seq"),
    ],
)
def test_card_refusals(change, why):
    k = Key()
    o = {**CARD["o"], **change}
    with pytest.raises(sign.SignerRefused):
        signer(k).card(o)
    assert k.signed == [], why


def test_card_missing_field_refused():
    o = dict(CARD["o"])
    del o["ck_commit"]
    with pytest.raises(sign.SignerRefused):
        signer().card(o)


def test_card_seq_must_rise_above_the_last_accepted():
    o = dict(CARD["o"])  # card_seq 1
    with pytest.raises(sign.SignerRefused):
        signer(last_card_seq=1).card(o)
    signer(last_card_seq=0).card(o)


def test_card_seq_never_signs_two_different_cards_at_one_seq():
    s = signer()
    o = dict(CARD["o"])
    s.card(o)
    s.card(dict(o))  # the identical card again (a resend after a lost answer) is fine
    with pytest.raises(sign.SignerRefused):
        s.card({**o, "issued_ms": o["issued_ms"] + 1})
    with pytest.raises(sign.SignerRefused):
        s.card({**o, "card_seq": 0})


def test_no_generic_sign_method():
    s = signer()
    public = {n for n in dir(s) if not n.startswith("_")}
    assert public == {"login", "response_chunk", "card", "public_key", "link"}, public


def test_repr_and_errors_hold_no_secret():
    s = signer()
    with pytest.raises(sign.SignerRefused) as e:
        s.login(b"\x13" * 31)
    assert "13131313" not in str(e.value)
    assert "priv" not in repr(s).lower()
