"""T2 for R6a against the REAL orch-relay of ``SOURCE`` (loopback only): the first card from R6's key custody is
accepted by the relay and served back verified; a member list and WK grant signed through R6's ``RelaySigner``
methods (the epoch floor and ledgers) with ``WK_e`` from R6's store are accepted, the reference phone opens exactly
that key, and R2's bridge, with R6's store as its ``EpochKeySource`` (and seal lock), answers the phone.

The vector identities (person alice, the phone, workspace A and its WSK) stand in for the owner and the device; the
card's delegation is the vector one (alice for workspace A). Publication planning is R6b's: the list here is built
by hand with the H12c builders."""

from __future__ import annotations

import pytest

from orch import canon, crypto
from orch.identity.epochs import MembershipKeys, R6Gate, default_part
from orch.identity.memberlist import member_list_o, wk_grant_o
from orch.relay.r6.firstcard import R6LinkSource
from orch.wsproto import card as relay_card
from orch.wsproto import sign

from ..helpers import OWNER_PK, WS, Key, directory
from ..r6kit import DELEGATION, KitStore, ck_of, digest
from .kit import CERTS, DEVICE, PERSON, SCOPES, HostSide, Owner, RefDevice, answer_of, now_ms, session

pytestmark = pytest.mark.relay_t2


class _Epochs:
    """The fixture R6 view of :class:`HostSide` reads ``cur``; everything else is R6's real store (its seal lock)."""

    def __init__(self, mk: MembershipKeys) -> None:
        self.mk = mk

    @property
    def cur(self) -> int:
        return self.mk.current_epoch()

    def current_epoch(self) -> int:
        return self.mk.current_epoch()

    def wk(self, e):  # noqa: ANN001, ANN201 - delegation only
        return self.mk.wk(e)

    def refusal_key(self, e):  # noqa: ANN001, ANN201
        return self.mk.refusal_key(e)

    def is_burned(self, e):  # noqa: ANN001, ANN201
        return self.mk.is_burned(e)

    def seal_state(self):  # noqa: ANN201
        return self.mk.seal_state()

    def seal_snapshot(self):  # noqa: ANN201
        return self.mk.seal_snapshot()

    def serialised(self):  # noqa: ANN201
        return self.mk.serialised()


@pytest.fixture
def mk(tmp_path):
    m = MembershipKeys(tmp_path / "membership", WS, now_ms=now_ms)
    store = KitStore()  # no store here: a stand-in for the host's
    R6Gate(m).install(lambda: store)
    yield m
    m.close()


def _signer(mk, relay):
    return sign.RelaySigner(Key(), sign.LinkContext(WS, relay.origin), ledger=mk.ledger, burned=mk.is_burned)


def test_the_first_card_from_r6_custody_is_accepted_and_served_back(relay, mk):
    owner = Owner(relay)
    owner.create_account()
    src = R6LinkSource(mk, _signer(mk, relay), delegation=DELEGATION, owner_pk_pub=OWNER_PK,
                       part=default_part("T2 workspace"), now_ms=now_ms, blocked=lambda: None)  # fmt: skip
    card = src.card()
    sess = session(relay)
    d = directory(sess)
    assert d.put_card(card, first=True) == card["o"]["card_seq"] == 1
    sess.login()
    served = d.get_card()  # verified under the owner's person key, this workspace, this WSK
    assert canon.dumps(served) == canon.dumps(card)
    ck = ck_of(mk, 1)
    assert relay_card.open_card_part(ck, WS, 1, crypto.unb64u(served["sealed"]))["name"] == "T2 workspace"
    mk.card_accepted(1)
    assert canon.dumps(src.card()) == canon.dumps(card)  # the same bytes on a resume


def test_an_r6_signed_list_and_grant_are_accepted_and_r2_serves_under_r6s_key(relay, mk, tmp_path):
    owner = Owner(relay)
    owner.create_account()
    owner.register("phone")
    signer = _signer(mk, relay)
    src = R6LinkSource(mk, signer, delegation=DELEGATION, owner_pk_pub=OWNER_PK, part=default_part("T2"),
                       now_ms=now_ms, blocked=lambda: None)  # fmt: skip
    sess = session(relay)
    assert directory(sess).put_card(src.card(), first=True) == 1
    sess.hold()
    lhw, ehw = mk.high_water()
    e, seq = ehw + 1, lhw + 1
    commit = mk.new_epoch_key(e)
    o = member_list_o(WS, e, seq, now_ms(), commit, [{"device_id": DEVICE, "person_id": PERSON,
                      "scopes": list(SCOPES)}], owner_person_id=PERSON, first=True)  # fmt: skip
    ml = {"o": o, "sig": signer.member_list(o)}
    kx = crypto.unb64u(CERTS["phone"]["o"]["dk_kx_pub"])
    assert mk.wk(e) is None and mk.refusal_key(e) is None  # claimed, not committed: R2 cannot reach it
    g = wk_grant_o(WS, DEVICE, e, mk.sealed_wk(e, kx, DEVICE))  # step 3: sealed inside the store
    grant = {"o": g, "sig": signer.wk_grant(g, signed_list=o)}
    body = canon.dumps(o)
    mk.expose(list_seq=seq, epoch=e, body=body, digest=digest(o), adds=[DEVICE])
    res = sess.call("PUT", f"/v2/workspaces/{WS}/membership", "PUT /v2/workspaces/{ws}/membership",
                    body={"member_list": ml, "grants": [grant]}, holder=True, request_cap=2 << 20)  # fmt: skip
    assert (res["list_seq"], res["epoch"]) == (seq, e), res
    assert mk.commit(list_seq=seq, epoch=e, list_hash=digest(o), list_obj=o, at_ms=now_ms())
    phone = RefDevice(relay, relay_ref(relay))
    phone.login()
    assert phone.fetch_grants() == {e: mk.wk(e)}  # the device holds exactly R6's WK_e
    side = HostSide(relay, tmp_path, sess, epochs=_Epochs(mk))  # type: ignore[arg-type]
    try:
        side.start()
        rid, env = phone.request({"op": "whoami", "args": {}}, epoch=e)
        assert phone.post(rid, env)[0] == 202
        meta, data = answer_of(phone.answer(rid, e)[0])
        assert meta == {"status": "ok"} and data["data"]["principal"] == "r:d_" + DEVICE
        assert side.loop.error is None and not side.alarms
    finally:
        side.close()


def relay_ref(relay):
    from .harness import ref_module

    return ref_module(relay.dir)
