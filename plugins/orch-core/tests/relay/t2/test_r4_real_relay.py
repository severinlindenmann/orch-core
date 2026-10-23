"""T2 for R4 against the REAL orch-relay of ``SOURCE`` (loopback only): a revocation of a listed device (an
ever-device of workspace A) uploaded by its person is served by ``GET unpublished``; R4's consumer, on core's real
Session, reads it from the start, the item verifies with the store's own verifier (every fact from the signed
objects), and the relay accepts the ack; the notice clears and nothing is read again."""

from __future__ import annotations

import pytest

from orch import crypto
from orch.host import relayr4
from orch.identity import certs
from orch.relay import unpublished as up
from orch.store.store import _verify_forwarded
from orch.wsproto.state import StateDir

from .. import corpus
from .kit import DEVICE, PERSON, Members, Owner, now_ms, publish_card, session

pytestmark = pytest.mark.relay_t2


def test_a_real_unpublished_revocation_is_read_verified_and_acked(relay, R, tmp_path):
    owner = Owner(relay)
    owner.create_account()
    owner.register("phone")
    sess = session(relay)
    assert publish_card(sess, relay) == 1
    sess.hold()
    wk = bytes(range(32))
    Members(R).publish(sess, 1, wk)  # the phone is listed: an ever-device of workspace A
    pk = corpus.priv("person_alice.sig")
    rev = certs.make_revocation(
        corpus.pub("person_alice.sig"), lambda b: crypto.sign(pk, b), device_id_hex=DEVICE, revoked_ms=now_ms(),
        reason="lost",
    )  # fmt: skip
    assert owner.http.ok("POST", "/v2/revocations", {"record": rev}) == {"device_id": DEVICE}
    state = StateDir(tmp_path / "relay")
    held: list[dict] = []
    alarms: list[tuple[str, str]] = []

    def hold(item):
        facts = _verify_forwarded(item)  # the store's verifier: every fact derived from the signed objects
        assert facts is not None
        device, person, reason, *_ = facts
        assert (device, person, reason) == ("d_" + DEVICE, "p_" + PERSON, "lost")
        held.append(item)
        return "held"

    try:
        c = up.UnpublishedConsumer(
            workspace_id=corpus.IDS["workspace_a"], session=sess, state=state, hold=hold, stronger=relayr4.stronger,
            notices=lambda: {"unpublished": None}, alarm=lambda code, d: alarms.append((code, d)), now_ms=now_ms,
        )  # fmt: skip
        c.step()  # a new holder generation: the full read
        assert len(held) == 1 and held[0]["revocation"] == rev
        st = c.load()[0]
        assert st["scan"] is None and st["cursor"] and st["held"][0]["device_id"] == DEVICE
        assert ("unpublished_held",) == alarms[-1][:1]
        c.step()
        assert len(held) == 1  # no notice: nothing read again
        poll = sess.call(
            "GET", f"/v2/workspaces/{corpus.IDS['workspace_a']}/host/poll?after=0&wait=0",
            "GET /v2/workspaces/{ws}/host/poll", holder=True, keys={"items", "notices", "next"},
        )  # fmt: skip
        assert poll["notices"]["unpublished"] is None  # the ack raised the high-water mark past the item
    finally:
        state.close()
