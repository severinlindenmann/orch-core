"""R6a: the first card (11A.15.5, C6; R5's link source) and the RelaySigner methods R6 adds (11A.15.7: member lists
with the H4 epoch floor, WK grants)."""

from __future__ import annotations

import json
import os

import pytest

from orch import canon, crypto
from orch.identity import memberlist, wscard
from orch.identity.epochs import CustodyRefused, default_part
from orch.identity.memberlist import seal_wk, wk_grant_o
from orch.relay.r6.firstcard import GenesisPathMissing, R6LinkSource
from orch.wsproto import card as relay_card
from orch.wsproto import sign
from orch.wsproto.sign import SignerRefused

from . import corpus
from .helpers import OWNER_PK, WS, WSK_PUB, Key, MemLedger
from .r6kit import DELEGATION, DEVICE, Clock, ck_of, expose_rotation, list_for, open_keys, rotate, signer

ORIGIN = "https://relay.example"
PART = default_part("Demo workspace")


@pytest.fixture
def mk(tmp_path):
    m = open_keys(tmp_path)
    yield m
    m.close()


def source(mk, *, blocked=None, sig=None, pin=None):
    return R6LinkSource(
        mk, sig or signer(mk, ORIGIN), delegation=DELEGATION, owner_pk_pub=OWNER_PK, part=PART, now_ms=Clock(),
        blocked=blocked or (lambda: None), genesis_pin=pin,
    )  # fmt: skip


# -- the first card ----------------------------------------------------------------------------------------------


def test_the_first_card_verifies_under_the_owner_key_and_opens_with_ck_1(tmp_path, mk):
    c = source(mk).card()
    vc = wscard.verify_card(c, OWNER_PK)
    assert (vc.workspace_id, vc.card_seq, vc.relay_url, vc.wsk_pub) == (WS, 1, ORIGIN, WSK_PUB)
    o = c["o"]
    assert o["wxk_version"] == 1 and o["card_seq"] == 1
    ck = ck_of(mk, 1)
    assert o["ck_commit"] == relay_card.ck_commit(WS, 1, ck)
    assert relay_card.open_card_part(ck, WS, 1, crypto.unb64u(c["sealed"])) == PART
    wxk = json.loads((tmp_path / "membership" / "keys" / "wxk-1.json").read_bytes())
    assert wxk["pub"] == o["wxk_pub"] and set(wxk) == {"v", "version", "d", "pub", "created_ms"}
    for name in ("keys/wxk-1.json", "keys/ck-1.json", "cards/card-1.json"):
        assert os.stat(tmp_path / "membership" / name).st_mode & 0o777 == 0o600
    assert mk.head()["card"] == {"card_seq": 0, "wxk_version": 1, "ck_seq": 1, "pending_card_seq": 1,
                                 "wxk_retired": []}  # fmt: skip


def test_the_card_is_identical_on_every_call_and_after_a_restart(tmp_path):
    m = open_keys(tmp_path)
    a = canon.dumps(source(m).card())
    assert canon.dumps(source(m).card()) == a
    m.close()
    m = open_keys(tmp_path)
    try:
        assert canon.dumps(source(m).card()) == a
        assert (tmp_path / "membership" / "cards" / "card-1.json").read_bytes() == a
    finally:
        m.close()


def test_card_accepted_moves_the_pending_card(mk):
    source(mk).card()
    with pytest.raises(CustodyRefused):
        mk.card_accepted(2)
    mk.card_accepted(1)
    assert mk.head()["card"]["card_seq"] == 1 and mk.head()["card"]["pending_card_seq"] is None


def test_convenience_blocks_the_card_before_any_key_is_drawn(tmp_path, mk):
    with pytest.raises(CustodyRefused, match="convenience"):
        source(mk, blocked=lambda: "convenience_events_present").card()
    assert os.listdir(tmp_path / "membership" / "keys") == []
    assert os.listdir(tmp_path / "membership" / "cards") == []


def test_genesis_is_refused_until_the_person_path_exists(mk):
    with pytest.raises(GenesisPathMissing):
        source(mk).genesis()
    pin = ({"o": {}}, {"o": {}}, "sha256:" + "0" * 64)
    assert source(mk, pin=lambda: pin).genesis() == pin
    assert source(mk).owner_pk_pub() == OWNER_PK


def test_a_delegation_of_another_workspace_or_wsk_is_refused(mk):
    other = sign.RelaySigner(Key(corpus.priv("person_alice.sig")), sign.LinkContext(WS, ORIGIN), ledger=MemLedger())
    with pytest.raises(CustodyRefused):
        source(mk, sig=other)
    with pytest.raises(CustodyRefused):
        R6LinkSource(mk, signer(mk), delegation=DELEGATION, owner_pk_pub=corpus.pub("wsk_a.sig"), part=PART,
                     now_ms=Clock(), blocked=lambda: None)  # fmt: skip


@pytest.mark.parametrize(
    "point,seq,version",
    [("after_wxk_claim", 1, 2), ("after_ck_claim", 2, 2), ("after_sign", 2, 2), ("after_card_claim", 1, 1),
     ("after_card_head", 1, 1)],
)  # fmt: skip
def test_a_crash_at_every_durable_step_of_the_first_card(tmp_path, point, seq, version):
    """Crash, restart, ask again: a verifying card; an orphan WXK or CK, or a signed but unclaimed card_seq, is never
    reused (the next version or card_seq), and the stored card bytes are resent as they are once claimed."""

    class Crash(Exception):
        pass

    def fault(p):
        if p == point:
            raise Crash(p)

    m = open_keys(tmp_path, fault=fault)
    with pytest.raises(Crash):
        source(m).card()
    m.close()
    m = open_keys(tmp_path)
    try:
        c = source(m).card()
        assert wscard.verify_card(c, OWNER_PK).card_seq == seq and c["o"]["wxk_version"] == version
        assert canon.dumps(source(m).card()) == canon.dumps(c)
        assert m.head()["card"]["pending_card_seq"] == seq
    finally:
        m.close()


def test_the_signer_ledger_refuses_a_rebuilt_card_at_a_signed_card_seq(mk):
    sig = signer(mk, ORIGIN)
    c = source(mk, sig=sig).card()
    o = {**c["o"], "issued_ms": c["o"]["issued_ms"] + 1}
    with pytest.raises(SignerRefused):
        sig.card(o)
    assert sig.card(c["o"]) == c["sig"]  # the recorded signature, never a re-sign


# -- RelaySigner.member_list: the H4 epoch floor and the list ledger --------------------------------------------------


def _list(mk, e, seq, devices=None):
    commit = mk.wk_commit_of(e)
    assert commit is not None
    return list_for(e, seq, commit, devices)


def test_member_list_signature_verifies_and_is_recorded_before_it_leaves(mk):
    sig = signer(mk)
    mk.new_epoch_key(1)
    o = _list(mk, 1, 1)
    s = sig.member_list(o)
    ml = memberlist.check_member_list({"o": o, "sig": s}, WSK_PUB, WS)
    assert ml.epoch == 1 and ml.list_seq == 1
    assert mk.ledger.get("wk_commit", "1")[0] == o["wk_commit"]
    assert mk.ledger.get("member_list", "1")[1] == s
    assert sig.member_list(o) == s  # same list: the recorded signature


def test_two_wk_commit_same_epoch_refused_by_signer_ledger(mk):
    sig = signer(mk)
    mk.new_epoch_key(1)
    sig.member_list(_list(mk, 1, 1))
    other = list_for(1, 2, crypto.b64u(crypto.sha256(b"other")))
    with pytest.raises(SignerRefused, match="another wk_commit"):
        sig.member_list(other)
    assert sig.member_list(_list(mk, 1, 2))  # same epoch, same commit, next list_seq: fine


def test_signer_refuses_list_below_epoch_floor_or_burned(mk):
    sig = signer(mk)
    rotate(mk)
    for e in (2, 3):
        mk.new_epoch_key(e)
    sig.member_list(_list(mk, 3, 2))
    with pytest.raises(SignerRefused, match="H4 floor"):
        sig.member_list(_list(mk, 2, 3))  # below the highest signed epoch
    _, e = expose_rotation(mk)  # epoch 4 exposed...
    mk.abandon()  # ...and burned
    with pytest.raises(SignerRefused, match="burned"):
        sig.member_list(_list(mk, e, 5))


def test_another_list_at_a_signed_list_seq_or_below_the_highest_is_refused(mk):
    sig = signer(mk)
    mk.new_epoch_key(1)
    sig.member_list(_list(mk, 1, 2))
    with pytest.raises(SignerRefused, match="another member list"):
        sig.member_list(_list(mk, 1, 2, devices=["ab" * 16]))
    with pytest.raises(SignerRefused, match="later list_seq"):
        sig.member_list(_list(mk, 1, 1))


@pytest.mark.parametrize(
    "change",
    [
        {"workspace_id": "00" * 16}, {"kind": "card"}, {"epoch": 0}, {"list_seq": 0}, {"wk_commit": "x"},
        {"persons": ["ab" * 16]}, {"members": [{"device_id": DEVICE, "person_id": "ab" * 16, "scopes": ["decide"]}]},
        {"extra": 1},
    ],
)  # fmt: skip
def test_member_list_shape_is_exact(mk, change):
    sig = signer(mk)
    mk.new_epoch_key(1)
    with pytest.raises(SignerRefused):
        sig.member_list({**_list(mk, 1, 1), **change})


def test_member_list_needs_a_ledger_and_the_burned_view(mk):
    mk.new_epoch_key(1)
    o = _list(mk, 1, 1)
    for s in (sign.RelaySigner(Key(), sign.LinkContext(WS, ORIGIN), ledger=mk.ledger),
              sign.RelaySigner(Key(), sign.LinkContext(WS, ORIGIN), burned=mk.is_burned)):  # fmt: skip
        with pytest.raises(SignerRefused):
            s.member_list(o)


def test_the_signer_field_sets_equal_the_builders():
    assert sign.MEMBER_LIST_FIELDS == memberlist.MEMBER_LIST_FIELDS
    assert sign.GRANT_FIELDS == memberlist.GRANT_FIELDS


# -- RelaySigner.wk_grant -----------------------------------------------------------------------------------------


def _grant(e, dev=DEVICE):
    kx = crypto.unb64u(corpus.S2["certs"]["signed"]["phone"]["o"]["dk_kx_pub"])
    return wk_grant_o(WS, dev, e, seal_wk(kx, dev, WS, e, bytes(32)))


def test_wk_grant_signed_only_for_a_device_of_the_list_signed_at_that_seq(mk):
    sig = signer(mk)
    mk.new_epoch_key(1)
    o = _list(mk, 1, 1)
    sig.member_list(o)
    g = _grant(1)
    s = sig.wk_grant(g, signed_list=o)
    memberlist.check_wk_grant({"o": g, "sig": s}, WSK_PUB, WS, DEVICE)
    assert sig.wk_grant(g, signed_list=o) == s
    with pytest.raises(SignerRefused, match="another grant"):
        sig.wk_grant(_grant(1), signed_list=o)  # a fresh seal: other bytes for (device, epoch)
    with pytest.raises(SignerRefused, match="not in the list"):
        sig.wk_grant(_grant(1, "ab" * 16), signed_list=o)
    with pytest.raises(SignerRefused, match="not signed"):
        sig.wk_grant(_grant(1), signed_list={**o, "issued_ms": 1})


def test_wk_grant_needs_a_recorded_wk_commit(mk):
    sig = signer(mk, ledger=MemLedger())
    o = list_for(1, 1, crypto.b64u(bytes(32)))
    with pytest.raises(SignerRefused, match="wk_commit"):
        sig.wk_grant(_grant(1), signed_list=o)


def test_wraps_and_sealed_keys_open_only_for_their_device(mk):
    source(mk).card()
    kx_priv = corpus.priv("phone.kx")
    kx_pub = crypto.public_bytes(kx_priv)
    wrap = mk.card_key_wrap(1, kx_pub, DEVICE)
    assert crypto.open_sealed(kx_priv, bytes.fromhex(DEVICE), "card", bytes.fromhex(WS), 1, wrap) == ck_of(mk, 1)
    with pytest.raises(CustodyRefused):
        mk.card_key_wrap(2, kx_pub, DEVICE)  # no card there
    rotate(mk)
    sealed = mk.sealed_wk(1, kx_pub, DEVICE)
    assert crypto.open_sealed(kx_priv, bytes.fromhex(DEVICE), "wk", bytes.fromhex(WS), 1, sealed) == mk.wk(1)
    _, e = expose_rotation(mk)
    mk.abandon()
    with pytest.raises(CustodyRefused):
        mk.sealed_wk(e, kx_pub, DEVICE)  # nothing new is sealed under a burned epoch
    with pytest.raises(CustodyRefused):
        mk.sealed_wk(99, kx_pub, DEVICE)


def test_a_card_that_does_not_verify_is_never_claimed(tmp_path, mk):
    """The card is verified under the owner key before its file is claimed: a wrong signature never lands."""
    bad = sign.RelaySigner(Key(), sign.LinkContext(WS, ORIGIN), ledger=MemLedger())

    def forged(o):
        return crypto.b64u(crypto.sign(corpus.priv("person_alice.sig"), b"not the card"))

    src = source(mk, sig=bad)
    src._signer = type("S", (), {"card": staticmethod(forged), "public_key": bad.public_key, "link": bad.link})()  # noqa: SLF001
    with pytest.raises(CustodyRefused, match="does not verify"):
        src.card()
    assert os.listdir(tmp_path / "membership" / "cards") == []
    assert mk.head()["card"]["pending_card_seq"] is None
