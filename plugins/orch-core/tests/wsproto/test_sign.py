"""``RelaySigner`` (11A.2): typed, purpose-specific WSK signing; each method validates its context first and builds its
label and bytes itself. A permitted label in the wrong context is refused (11A.10 "signer" adversarial row)."""

from __future__ import annotations

import pytest

from orch import canon, crypto
from orch.crypto.labels import L
from orch.wsproto import sign, wire

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


def signer(key=None, *, last_card_seq=0, epochs=None, origin=ORIGIN, ws=WS, ledger=None):
    from .helpers import MemLedger

    return sign.RelaySigner(
        key or Key(),
        sign.LinkContext(workspace_id=ws, origin=origin),
        epochs=epochs,
        last_card_seq=last_card_seq,
        ledger=ledger if ledger is not None else MemLedger(),
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
    first = s.card(o)
    assert s.card(dict(o)) == first  # the identical card again returns the recorded signature, no re-sign
    with pytest.raises(sign.SignerRefused):
        s.card({**o, "issued_ms": o["issued_ms"] + 1})
    with pytest.raises(sign.SignerRefused):
        s.card({**o, "card_seq": 0})


def test_no_generic_sign_method():
    s = signer()
    public = {n for n in dir(s) if not n.startswith("_")}
    want = {"login", "response_chunk", "card", "member_list", "wk_grant", "pair_answer", "cert_request", "push"}
    assert public == want | {"public_key", "link"}, public


def test_repr_and_errors_hold_no_secret():
    s = signer()
    with pytest.raises(sign.SignerRefused) as e:
        s.login(b"\x13" * 31)
    assert "13131313" not in str(e.value)
    assert "priv" not in repr(s).lower()


# --- the durable last-signed ledger (review M2) ----------------------------------------------------------------------


class FailingLedger:
    """Records nothing and fails: the signature must never be returned."""

    def __init__(self, inner, *, write_then_fail=False):
        self.inner, self.write_then_fail = inner, write_then_fail

    def get(self, kind, key):
        return self.inner.get(kind, key)

    def keys(self, kind):
        return self.inner.keys(kind)

    def record(self, kind, key, digest, sig):
        if self.write_then_fail:
            self.inner.record(kind, key, digest, sig)
        raise OSError("disk full")


def test_card_without_a_ledger_is_refused():
    k = Key()
    s = sign.RelaySigner(k, sign.LinkContext(workspace_id=WS, origin=ORIGIN))
    with pytest.raises(sign.SignerRefused):
        s.card(dict(CARD["o"]))
    assert k.signed == []


def test_ledger_failure_means_no_signature_is_returned():
    from .helpers import MemLedger

    with pytest.raises(OSError):
        signer(ledger=FailingLedger(MemLedger())).card(dict(CARD["o"]))


def test_crash_after_the_ledger_write_then_restart():
    """Crash between the durable record and the return: after a restart the same card gets the recorded signature, a
    different card at that card_seq is refused."""
    from .helpers import MemLedger

    mem = MemLedger()
    with pytest.raises(OSError):
        signer(ledger=FailingLedger(mem, write_then_fail=True)).card(dict(CARD["o"]))
    recorded = mem.get("card", "1")
    assert recorded is not None
    k = Key()
    restarted = signer(k, ledger=mem)
    assert restarted.card(dict(CARD["o"])) == recorded[1] and k.signed == []
    with pytest.raises(sign.SignerRefused):
        restarted.card({**CARD["o"], "issued_ms": 5})
    assert k.signed == []


def test_no_card_below_the_highest_signed_seq():
    from .helpers import MemLedger

    mem = MemLedger()
    mem.record("card", "3", "00" * 32, "sig")
    with pytest.raises(sign.SignerRefused):
        signer(ledger=mem).card(dict(CARD["o"]))  # card_seq 1 < 3


def test_a_cached_older_card_is_refused_once_a_later_seq_was_signed():
    """Round 6 (orch-52 #5): the highest-seq rule runs before the cache. The exact card signed earlier at seq 1 is not
    handed out again once seq 2 was signed, even though its signature is in the ledger."""
    from .helpers import MemLedger

    mem = MemLedger()
    k = Key()
    s = signer(k, ledger=mem)
    o1 = dict(CARD["o"])
    s.card(o1)
    s.card({**o1, "card_seq": 2})
    assert mem.get("card", "1") is not None
    k2 = Key()
    with pytest.raises(sign.SignerRefused, match="later card_seq"):
        signer(k2, ledger=mem).card(dict(o1))
    with pytest.raises(sign.SignerRefused, match="later card_seq"):
        s.card(dict(o1))
    assert k2.signed == []
    assert signer(ledger=mem).card({**o1, "card_seq": 2})  # the highest one is still served from the cache


def test_file_ledger_survives_a_restart(tmp_path):
    from orch.wsproto.state import FileSignLedger, StateDir

    st = StateDir(tmp_path / "relay")
    sig = signer(ledger=FileSignLedger(st)).card(dict(CARD["o"]))
    st.close()
    again = FileSignLedger(StateDir(tmp_path / "relay"))
    k = Key()
    assert signer(k, ledger=again).card(dict(CARD["o"])) == sig and k.signed == []
    with pytest.raises(sign.SignerRefused):
        signer(ledger=again).card({**CARD["o"], "issued_ms": 9})


def test_two_signers_on_one_state_dir_never_return_two_cards_at_one_seq(tmp_path):
    """Review D1: concurrent signers sharing a directory; at most one distinct card signature leaves at card_seq 1."""
    import threading

    from orch.wsproto.state import FileSignLedger, StateDir

    st = StateDir(tmp_path / "relay")
    got, errors = [], []
    barrier = threading.Barrier(6)

    def go(i):
        s = signer(ledger=FileSignLedger(st))
        barrier.wait()
        try:
            got.append((i % 2, s.card({**CARD["o"], "issued_ms": 1000 + i % 2})))
        except (sign.SignerRefused, Exception) as e:  # noqa: BLE001 - a refusal is the safe outcome
            errors.append(type(e).__name__)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len({variant for variant, _ in got}) <= 1, got
    st.close()


@pytest.mark.parametrize(
    "bad",
    [
        b'{"card":{"10":null}}',
        b'{"card":{"10":[]}}',
        b'{"card":null}',
        # round 10, R9-1: an entry for another card under a non-canonical key for seq 10
        *(
            b'{"card":{"%s":{"digest":"%s","sig":"s"}}}' % (k.encode(), b"ab" * 32)
            for k in ("010", " 10", "+10", "1_0", "abc", "", "\\uff11\\uff10")
        ),
    ],
)
def test_a_null_ledger_entry_never_lets_a_different_card_be_signed_at_that_seq(tmp_path, bad):
    """Round 9, equivocation: the ledger holds ``null`` (or a malformed entry) at card_seq 10; a request for a card at
    seq 10 is refused (StateError), no signature is made, and the ledger on disk is unchanged."""
    from orch.wsproto.state import FileSignLedger, StateDir, StateError

    with StateDir(tmp_path / "relay") as st:
        st.write(FileSignLedger.FILE, bad)
        k = Key()
        s = signer(k, ledger=FileSignLedger(st))
        for issued in (1, 2):
            with pytest.raises(StateError, match="sign ledger"):
                s.card({**CARD["o"], "card_seq": 10, "issued_ms": issued})
        assert k.signed == []
        assert st.read(FileSignLedger.FILE) == bad
