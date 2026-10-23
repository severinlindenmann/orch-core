"""B1 / R6b: the membership publisher (11A.15.4) and the real ``R6View`` over the real key store, signer ledger and
grant store, with a scripted relay in place of the network (the step is driven directly: no thread, no clock)."""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

import pytest

from orch import canon, crypto
from orch.identity import certs, memberlist
from orch.relay.r6.membership import GrantStore, MembershipPublisher, Plan, plan_publication
from orch.relay.r6.view import CommittedView
from orch.relay.retry import RelayRefused
from orch.relay.session import RelayMisbehaving

from . import r6kit
from .bridgekit import T0
from .helpers import OWNER_PK, WS, WSK_PUB, Alarms
from .r6kit import Clock, open_keys, signer


def _person():
    priv = crypto.generate_private_key()
    return priv, crypto.public_bytes(priv)


class Dev:
    """A relay-reachable device of the test person: key pair, certificate and its ``devices/<id>.json`` record."""

    def __init__(
        self, person_priv: Any, scopes: tuple[str, ...] = ("look", "decide"), *, expires_ms: int | None = None
    ):
        pk_pub = crypto.public_bytes(person_priv)
        dk = crypto.generate_private_key()
        self.kx = crypto.generate_private_key()
        self.cert = certs.make_device_cert(
            pk_pub, lambda b: crypto.sign(person_priv, b), dk_sig_pub=crypto.public_bytes(dk),
            dk_kx_pub=crypto.public_bytes(self.kx), label_sealed=bytes(32), created_ms=T0 - 3_600_000,
            expires_ms=expires_ms, scopes_max=list(scopes),
        )  # fmt: skip
        self.id = self.cert["o"]["device_id"]
        self.person = self.cert["o"]["person_id"]
        self.record = {"v": 1, "device_id": self.id, "person_id": self.person, "cert": self.cert,
                       "scopes": list(scopes)}  # fmt: skip


class Events:
    def __init__(self) -> None:
        self.committed: list[tuple[str, dict]] = []
        self.refused: list[tuple[str, str]] = []

    def on_committed(self, offer_id, publication):
        self.committed.append((offer_id, dict(publication)))

    def on_refused(self, offer_id, reason):
        self.refused.append((offer_id, reason))


class FakeSession:
    """The session's ``call`` for the one route the publisher uses; ``script(n, body)`` answers or raises."""

    def __init__(self) -> None:
        self.gen = 1
        self.workspace_id = WS
        self.calls: list[tuple[str, bytes]] = []
        self.held: dict | None = None
        self.script: Any = lambda n, body: {"list_seq": body["member_list"]["o"]["list_seq"],
                                            "epoch": body["member_list"]["o"]["epoch"]}  # fmt: skip

    def call(self, method, path, route, *, raw=None, **kw):
        if method == "GET":  # R1's verified GET members: the relay serves the last list it accepted
            assert path == f"/v2/workspaces/{WS}/members"
            if self.held is None:
                raise RelayRefused(404, "not_found")
            return {"member_list": self.held}
        assert (method, path) == ("PUT", f"/v2/workspaces/{WS}/membership") and kw["holder"] is True
        assert kw["content_type"] == "application/json" and kw["keys"] == {"list_seq", "epoch"}
        self.calls.append((path, raw))
        body = canon.loads_strict(raw)
        out = self.script(len(self.calls), body)
        self.held = body["member_list"]
        return out


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.person_priv, self.pk = _person()
        self.mk = open_keys(tmp, clock=Clock())
        self.signer = signer(self.mk)
        self.grants = GrantStore(tmp / "membership" / "grants")
        self.session = FakeSession()
        self.devs: dict[str, Dev] = {}
        self.ok: set[str] = set()
        self.events = Events()
        self.alarms = Alarms()
        self.errors: list[BaseException] = []
        self.pub = MembershipPublisher(
            self.mk, self.signer, session=self.session, conn=lambda: None, grants=self.grants,
            owner_pk_pub=self.pk, wsk_pub=WSK_PUB, standing=lambda ids: {d for d in ids if d in self.ok},
            device=lambda i: self.devs[i].record if i in self.devs else None,
            device_ids=lambda: sorted(self.devs), alarm=self.alarms, on_error=self.errors.append,
            now_ms=lambda: T0, rng=random.Random(5),
        )  # fmt: skip
        self.pub.bind(self.events)
        self.view = CommittedView(
            self.mk, wsk_pub=WSK_PUB, owner_pk_pub=self.pk,
            device=lambda i: self.devs[i].record if i in self.devs else None,
        )  # fmt: skip

    def add(self, offer: str, scopes: tuple[str, ...] = ("look", "decide")) -> Dev:
        d = Dev(self.person_priv, scopes)
        self.devs[d.id] = d
        self.ok.add(d.id)
        self.pub.post_addition(offer, d.id, d.cert)
        return d

    def close(self) -> None:
        self.grants.close()
        self.mk.close()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    try:
        yield r
    finally:
        r.close()


def _body(rig: Rig, n: int = -1) -> dict:
    return canon.loads_strict(rig.session.calls[n][1])


# -- the plan ---------------------------------------------------------------------------------------------------------


def _head(**kw):
    head = {"committed": None, "exposed": None, "excluded": {}, "holders": {}, "gate": None}
    head.update(kw)
    return head


def _committed(devs, epoch=1):
    return {"epoch": epoch, "list": {"members": [{"device_id": d} for d in devs]}}


def test_the_plan_is_a_rotation_for_the_first_list_a_removal_a_gate_or_a_stale_holder():
    a, b, c = "a" * 32, "b" * 32, "c" * 32
    assert plan_publication(_head(), []) is None
    assert plan_publication(_head(), [a]) == Plan(True, (a,))  # the first list
    h = _head(committed=_committed([a, b]), holders={"1": [a, b]})
    assert plan_publication(h, [a, b]) is None
    assert plan_publication(h, [a, b, c]) == Plan(False, (a, b, c))  # an addition: same epoch
    assert plan_publication(h, [a]) == Plan(True, (a,))  # a removal rotates
    assert plan_publication(h, []) == Plan(True, ())  # the empty rotation
    assert plan_publication({**h, "excluded": {b: {}}}, [a, b]) == Plan(True, (a,))  # excluded is never wanted
    gate = {"up": True, "reasons": [{"device": b, "reason": "f1_standing"}], "since_ms": 0, "target": None}
    assert plan_publication({**h, "gate": gate}, [a, b]) == Plan(True, (a,))  # a gated device is not wanted
    gate = {**gate, "reasons": [{"device": "", "reason": "rotate"}]}
    assert plan_publication({**h, "gate": gate}, [a, b]) == Plan(True, (a, b))  # a gate for all: rotate, keep both
    # the holder invariant: a device that held the epoch's key and is not wanted forces a rotation even if unlisted
    h = _head(committed=_committed([a]), holders={"1": [a, c]})
    assert plan_publication(h, [a]) == Plan(True, (a,))


# -- publication ------------------------------------------------------------------------------------------------------


def test_the_first_list_is_signed_granted_exposed_sent_and_committed_and_the_offer_answered(rig):
    d = rig.add("o1")
    assert rig.view.member_list() is None  # nothing committed: no member
    rig.pub._step()  # noqa: SLF001
    body = _body(rig)
    assert set(body) == {"member_list", "grants"}  # no card accepted yet: no card_key_wraps
    signed = body["member_list"]
    ml = memberlist.check_member_list(signed, WSK_PUB, WS)
    assert (ml.epoch, ml.list_seq, [m.device_id for m in ml.members]) == (1, 1, [d.id])
    assert ml.members[0].scopes == ("look", "decide") and ml.persons == ()
    (grant,) = body["grants"]
    assert memberlist.check_wk_grant(grant, WSK_PUB, WS, d.id).epoch == 1
    head = rig.mk.head()
    assert head["committed"]["list_seq"] == 1 and head["exposed"] is None
    assert head["holders"] == {"1": [d.id]} and rig.mk.current_epoch() == 1
    assert rig.events.committed == [("o1", {"list_seq": 1, "epoch": 1, "adds": [d.id],
                                            "list_hash": head["committed"]["list_hash"]})]  # fmt: skip
    member = rig.view.member_list()
    assert member is not None and member.epoch == 1 and set(member.members) == {d.id}
    assert member.members[d.id].cert == d.cert and member.members[d.id].pk_pub == rig.pk
    assert member.list_hash == head["committed"]["list_hash"]


def test_a_second_device_is_a_same_epoch_addition_that_reuses_the_first_grant_byte_for_byte(rig):
    d1 = rig.add("o1")
    rig.pub._step()  # noqa: SLF001
    first_grant = _body(rig)["grants"][0]
    d2 = rig.add("o2", ("look",))
    rig.pub._step()  # noqa: SLF001
    body = _body(rig)
    ml = memberlist.check_member_list(body["member_list"], WSK_PUB, WS)
    assert (ml.epoch, ml.list_seq) == (1, 2) and [m.device_id for m in ml.members] == sorted([d1.id, d2.id])
    assert {g["o"]["device_id"] for g in body["grants"]} == {d1.id, d2.id}
    assert [g for g in body["grants"] if g["o"]["device_id"] == d1.id] == [first_grant]
    assert rig.mk.head()["holders"]["1"] == sorted([d1.id, d2.id]) and rig.mk.current_epoch() == 1
    assert rig.events.committed[-1][0] == "o2" and rig.events.committed[-1][1]["adds"] == [d2.id]
    assert set(rig.view.member_list().members) == {d1.id, d2.id}


def _accept_card(mk, seq: int = 1) -> None:
    """Stand in for R6a's first card + ``card_accepted`` (a real card needs a real genesis)."""
    ck = crypto.b64u(bytes(range(32)))
    assert mk._keys.claim(  # noqa: SLF001
        f"ck-{seq}.json", canon.dumps({"v": 1, "card_seq": seq, "ck": ck, "created_ms": T0})
    )
    assert mk._cards.claim(f"card-{seq}.json", b"{}")  # noqa: SLF001
    head = {**mk._head, "card": {**mk._head["card"], "card_seq": seq, "ck_seq": seq}}  # noqa: SLF001
    mk._dir.write_json("head.json", head)  # noqa: SLF001
    mk._head = head  # noqa: SLF001


def test_the_card_key_wraps_ride_along_for_devices_new_to_the_list_once_a_card_was_accepted(rig):
    _accept_card(rig.mk)
    d1 = rig.add("o1")
    rig.pub._step()  # noqa: SLF001
    wraps = _body(rig)["card_key_wraps"]
    assert wraps["card_seq"] == 1 and [w["device_id"] for w in wraps["wraps"]] == [d1.id]
    assert len(crypto.unb64u(wraps["wraps"][0]["sealed"])) == memberlist.SEALED_KEY_LEN
    d2 = rig.add("o2")
    rig.pub._step()  # noqa: SLF001
    assert [w["device_id"] for w in _body(rig)["card_key_wraps"]["wraps"]] == [d2.id]  # only the new device


def test_a_lost_response_is_resent_with_the_exact_bytes_and_commits_once(rig):
    rig.add("o1")
    outcomes = [RelayRefused(503, None), RelayMisbehaving("x")]

    def script(n, body):
        if n <= len(outcomes):
            e = outcomes[n - 1]
            raise e
        return {"list_seq": body["member_list"]["o"]["list_seq"], "epoch": body["member_list"]["o"]["epoch"]}

    rig.session.script = script
    rig.pub._step()  # noqa: SLF001 - a 503: nothing committed, the body stays exposed
    assert rig.mk.head()["exposed"] is not None and rig.events.committed == []
    with pytest.raises(RelayMisbehaving):
        rig.pub._step()  # noqa: SLF001 - a malformed answer is transient too (the thread backs off)
    rig.pub._step()  # noqa: SLF001
    assert len({raw for _, raw in rig.session.calls}) == 1 and len(rig.session.calls) == 3
    assert rig.mk.head()["committed"]["list_seq"] == 1 and len(rig.events.committed) == 1


def test_a_content_refusal_burns_excludes_the_added_device_ends_its_offer_and_the_next_plan_rotates(rig):
    d1 = rig.add("o1")
    rig.pub._step()  # noqa: SLF001
    d2 = rig.add("o2")
    rig.session.script = lambda n, body: (_ for _ in ()).throw(RelayRefused(403, "revoked"))
    rig.pub._step()  # noqa: SLF001
    head = rig.mk.head()
    assert head["exposed"] is None and 2 in head["burned"]["list_seqs"]
    assert list(head["excluded"]) == [d2.id] and head["excluded"][d2.id]["reason"] == "revoked"
    assert rig.events.refused == [("o2", "revoked")] and head["h8"]["count"] == 1
    assert head["holders"]["1"] == sorted([d1.id, d2.id])  # H5: it may hold the key whatever the relay said
    n = len(rig.session.calls)
    rig.session.script = lambda n, body: {"list_seq": body["member_list"]["o"]["list_seq"],
                                          "epoch": body["member_list"]["o"]["epoch"]}  # fmt: skip
    rig.pub._step()  # noqa: SLF001
    assert len(rig.session.calls) == n + 1
    ml = memberlist.check_member_list(_body(rig)["member_list"], WSK_PUB, WS)
    assert (ml.epoch, ml.list_seq, [m.device_id for m in ml.members]) == (2, 3, [d1.id])  # a rotation without d2
    assert rig.mk.current_epoch() == 2 and rig.mk.head()["h8"]["count"] == 0


def test_a_device_that_lost_its_standing_is_rotated_out_and_a_rotation_changes_the_epoch_and_the_grant(rig):
    d1 = rig.add("o1")
    d2 = rig.add("o2")
    rig.pub._step()  # noqa: SLF001
    first = {g["o"]["device_id"]: g["o"]["sealed"] for g in _body(rig)["grants"]}
    assert set(first) == {d1.id, d2.id}
    rig.ok.discard(d2.id)  # F1 removed it
    rig.pub._step()  # noqa: SLF001
    body = _body(rig)
    ml = memberlist.check_member_list(body["member_list"], WSK_PUB, WS)
    assert (ml.epoch, [m.device_id for m in ml.members]) == (2, [d1.id])
    assert [g["o"]["device_id"] for g in body["grants"]] == [d1.id] and body["grants"][0]["o"]["epoch"] == 2
    assert body["grants"][0]["o"]["sealed"] != first[d1.id]
    assert set(rig.view.member_list().members) == {d1.id}
    rig.ok.discard(d1.id)
    rig.pub._step()  # noqa: SLF001 - the last device left: the empty rotation evicts it at the relay
    body = _body(rig)
    assert body["member_list"]["o"]["members"] == [] and body["grants"] == []
    assert rig.view.member_list().members == {}


def test_quota_exceeded_is_permanent_no_retry_no_new_pairing(rig):
    rig.add("o1")
    rig.session.script = lambda n, body: (_ for _ in ()).throw(RelayRefused(429, "quota_exceeded", retry_after=1))
    rig.pub._step()  # noqa: SLF001
    assert rig.pub._quota_full is True and "membership_quota" in rig.alarms.codes()  # noqa: SLF001
    assert rig.events.refused == [("o1", "quota_exceeded")] and rig.mk.head()["exposed"] is None


def test_an_exclusion_posted_by_the_pair_thread_is_durable_and_never_published(rig):
    d1 = rig.add("o1")
    rig.pub.post_exclusion("o1", d1.id, "abandoned")
    rig.pub._step()  # noqa: SLF001
    assert d1.id in rig.mk.head()["excluded"] and rig.session.calls == []
    assert rig.events.refused == [] and rig.events.committed == []


def test_an_offer_for_a_device_already_committed_is_answered_at_once_a_repost_after_a_crash(rig):
    d1 = rig.add("o1")
    rig.pub._step()  # noqa: SLF001
    rig.events.committed.clear()
    rig.pub.post_addition("o1", d1.id, d1.cert)  # the pair thread posts again after its crash
    n = len(rig.session.calls)
    rig.pub._step()  # noqa: SLF001
    assert len(rig.session.calls) == n and rig.events.committed[0][1]["adds"] == [d1.id]


def test_a_device_that_is_not_eligible_is_refused_not_published(rig):
    d = rig.add("o1")
    rig.ok.clear()
    rig.pub._step()  # noqa: SLF001
    assert rig.events.refused == [("o1", "device_not_eligible")] and rig.session.calls == []
    del d


def test_ready_needs_a_running_thread_and_a_holder_generation(rig):
    assert rig.pub.ready() is False
    rig.pub.start()
    try:
        import time

        end = time.monotonic() + 5
        while not rig.pub.ready() and time.monotonic() < end:
            time.sleep(0.02)
        assert rig.pub.ready() is True
        rig.session.gen = None
        assert rig.pub.ready() is False
    finally:
        rig.pub.stop()
        assert rig.pub.join(5)
    assert rig.pub.ready() is False


def test_the_thread_publishes_on_a_wake_and_stops_cleanly(rig):
    import time

    rig.pub.start()
    try:
        rig.add("o1")
        end = time.monotonic() + 10
        while not rig.events.committed and time.monotonic() < end:
            time.sleep(0.02)
        assert rig.events.committed and rig.mk.head()["committed"]["list_seq"] == 1
    finally:
        rig.pub.stop()
        assert rig.pub.join(5)


# -- the view, fail closed --------------------------------------------------------------------------------------------


def test_the_view_serves_nothing_for_a_list_that_does_not_verify(rig):
    d = rig.add("o1")
    rig.pub._step()  # noqa: SLF001
    assert rig.view.member_list() is not None
    other = CommittedView(rig.mk, wsk_pub=crypto.public_bytes(crypto.generate_private_key()), owner_pk_pub=rig.pk,
                          device=lambda i: rig.devs[i].record)  # fmt: skip
    assert other.member_list() is None  # not signed by this WSK
    alarms = Alarms()
    nodev = CommittedView(rig.mk, wsk_pub=WSK_PUB, owner_pk_pub=rig.pk, device=lambda i: None, alarm=alarms)
    assert nodev.member_list() is None and alarms.codes() == ["membership_view_refused"]
    stranger = CommittedView(rig.mk, wsk_pub=WSK_PUB, owner_pk_pub=OWNER_PK, device=lambda i: rig.devs[i].record)
    assert stranger.member_list() is None  # another person's entry (and a certificate of another signer)
    wide = dict(rig.devs[d.id].record)
    wide["cert"] = Dev(rig.person_priv, ("look",)).cert  # a certificate of another device: the id does not match
    assert CommittedView(rig.mk, wsk_pub=WSK_PUB, owner_pk_pub=rig.pk, device=lambda i: wide).member_list() is None


def test_the_view_follows_the_committed_epoch_and_drops_a_gated_unsafe_or_missing_state(rig, tmp_path):
    rig.add("o1")
    rig.pub._step()  # noqa: SLF001
    ml = rig.view.member_list()
    assert ml is not None
    assert r6kit.digest  # (the kit's helper, imported for the pinning below)
    # the key store's committed epoch moves under the view: the cached list is never served for another epoch
    rig.mk._ec = 2  # noqa: SLF001 - a test seam: simulate a commit the view has not seen
    assert rig.view.member_list() is None


def test_without_an_attached_worker_the_host_membership_fails_closed(tmp_path):
    from orch.host.relayr6b import HostMembership

    m = HostMembership(object())
    ev = Events()
    m.bind(ev)
    assert m.ready() is False
    m.post_addition("o1", "a" * 32, {})
    m.post_exclusion("o1", "a" * 32, "x")
    assert ev.refused == [("o1", "publisher_unavailable")] and ev.committed == []


def test_a_relay_list_above_what_this_host_allocated_is_h7_host_state_loss(rig):
    rig.add("o1")
    rig.pub._step()  # noqa: SLF001
    owner = crypto.person_id(rig.pk).hex()
    o = memberlist.member_list_o(WS, 40, 50, T0, crypto.b64u(bytes(32)), [], owner_person_id=owner, first=False)
    # a list the WSK signed that this host never exposed (e.g. a restored older state directory)
    rig.session.held = {"o": o, "sig": crypto.b64u(crypto.sign(crypto.generate_private_key(), b"x"))}
    import orch.relay.directory as directory

    real = directory.Directory.get_members
    directory.Directory.get_members = lambda self: rig.session.held  # the verification itself is R1's, tested there
    try:
        rig.pub.reconcile()
    finally:
        directory.Directory.get_members = real
    lhw, ehw = rig.mk.high_water()
    assert lhw >= 50 + 1024 and ehw >= 40 + 1024 and "membership_h7" in rig.alarms.codes()
    head = rig.mk.head()
    assert head["gate"]["reasons"] == [{"device": "", "reason": "h7"}] and head["committed"] is None
    rig.pub._step()  # noqa: SLF001 - and the next list is a rotation above everything, without a card
    ml = memberlist.check_member_list(_body(rig)["member_list"], WSK_PUB, WS)
    assert ml.epoch > ehw and ml.list_seq > lhw


# -- the key store's R6b transitions ----------------------------------------------------------------------------------


def test_expose_records_the_holders_exclude_and_h8_are_durable_and_committed_is_a_lock_free_copy(tmp_path):
    from orch.identity.epochs import CustodyRefused

    mk = open_keys(tmp_path, clock=Clock())
    assert mk.committed_record() is None
    r6kit.rotate(mk, devices=[r6kit.DEVICE])
    assert mk.head()["holders"] == {"1": [r6kit.DEVICE]}
    c = mk.committed_record()
    assert c["epoch"] == 1 and c["list"]["members"][0]["device_id"] == r6kit.DEVICE
    c["epoch"] = 99
    assert mk.committed_record()["epoch"] == 1  # a copy
    other = "e" * 32
    mk.exclude(other, "abandoned")
    mk.exclude(other, "again")  # idempotent: the first reason stays
    mk.set_h8(2, False)
    for bad in (("x", "r"), (other, "")):
        with pytest.raises(CustodyRefused):
            mk.exclude(*bad)
    mk.close()
    again = open_keys(tmp_path, clock=Clock())
    head = again.head()
    assert head["excluded"][other]["reason"] == "abandoned" and head["h8"] == {"count": 2, "alarm": False}
    assert head["holders"] == {"1": [r6kit.DEVICE]}
    again.close()


def test_a_superseded_exposure_keeps_every_holder_of_every_body_it_granted(tmp_path):
    mk = open_keys(tmp_path, clock=Clock())
    r6kit.rotate(mk, devices=[r6kit.DEVICE])
    a, b = "a" * 32, "b" * 32
    lhw, _ = mk.high_water()
    o = r6kit.list_for(1, lhw + 1, mk.wk_commit_of(1), [r6kit.DEVICE, a])
    mk.expose(list_seq=lhw + 1, epoch=1, body=canon.dumps(o), digest=r6kit.digest(o), adds=[r6kit.DEVICE, a])
    lhw, _ = mk.high_water()
    o2 = r6kit.list_for(1, lhw + 1, mk.wk_commit_of(1), [r6kit.DEVICE, b])
    mk.expose(list_seq=lhw + 1, epoch=1, body=canon.dumps(o2), digest=r6kit.digest(o2), adds=[r6kit.DEVICE, b])
    assert mk.head()["holders"]["1"] == sorted([r6kit.DEVICE, a, b])  # H5: a refused or superseded body may be out
    mk.abandon(exclude={b: "revoked"})
    head = mk.head()
    assert head["exposed"] is None and head["excluded"][b]["reason"] == "revoked"
    assert {*head["burned"]["list_seqs"]} == {2, 3}
    mk.close()


# -- fix round (Opus review of B1-H) ---------------------------------------------------------------------


def _ok(n, body):
    return {"list_seq": body["member_list"]["o"]["list_seq"], "epoch": body["member_list"]["o"]["epoch"]}


def test_m1_an_addition_after_an_abandoned_rotation_publishes_at_a_new_epoch_not_below_the_highest_signed(rig):
    d1, d2 = rig.add("o1"), rig.add("o2")
    rig.pub._step()  # noqa: SLF001 - both at epoch 1
    rig.ok.discard(d2.id)
    rig.session.script = lambda n, body: (_ for _ in ()).throw(RelayRefused(503, None))
    rig.pub._step()  # noqa: SLF001 - rotation to epoch 2 exposed, 503
    assert rig.mk.head()["exposed"]["epoch"] == 2
    rig.ok.add(d2.id)
    rig.pub._step()  # noqa: SLF001 - plan None: epoch 2 is abandoned
    assert rig.mk.head()["exposed"] is None and rig.mk.current_epoch() == 1
    d3 = rig.add("o3")
    rig.session.script = _ok
    rig.pub._step()  # noqa: SLF001
    ml = memberlist.check_member_list(_body(rig)["member_list"], WSK_PUB, WS)
    assert ml.epoch > 2 and sorted(m.device_id for m in ml.members) == sorted([d1.id, d2.id, d3.id])
    assert rig.mk.current_epoch() == ml.epoch and rig.events.committed[-1][0] == "o3"


def test_m1_the_plan_rotates_whenever_ehw_is_above_the_committed_epoch():
    head = _head(committed=_committed(["a" * 32]))
    head["committed"]["epoch"] = 1
    assert plan_publication(head, ["a" * 32, "b" * 32], 1) == Plan(False, ("a" * 32, "b" * 32))
    assert plan_publication(head, ["a" * 32, "b" * 32], 2) == Plan(True, ("a" * 32, "b" * 32))
    assert plan_publication(head, ["a" * 32], 2) is None


def test_m2_a_crash_between_signing_a_grant_and_keeping_it_does_not_stall_the_device(rig, monkeypatch):
    rig.add("o1")
    rig.pub._step()  # noqa: SLF001
    d2 = rig.add("o2")
    real = rig.grants.keep
    boom = {"n": 0}

    def crash(epoch, device, signed):
        boom["n"] += 1
        if boom["n"] == 1:
            raise OSError("crash after the signature")
        return real(epoch, device, signed)

    monkeypatch.setattr(rig.grants, "keep", crash)
    with pytest.raises(OSError):
        rig.pub._step()  # noqa: SLF001
    assert rig.grants.get(1, d2.id) is None and rig.grants.get_unsigned(1, d2.id) is not None
    rig.pub._step()  # noqa: SLF001
    assert rig.events.committed[-1][0] == "o2"
    assert {g["o"]["device_id"] for g in _body(rig)["grants"]} == {m for m in rig.mk.head()["holders"]["1"]}


def test_m3_the_card_key_wraps_are_persisted_write_once_per_card_seq_and_device(rig):
    _accept_card(rig.mk)
    d1 = rig.add("o1")
    rig.session.script = lambda n, body: (_ for _ in ()).throw(RelayRefused(503, None))
    rig.pub._step()  # noqa: SLF001
    first = _body(rig)["card_key_wraps"]["wraps"][0]["sealed"]
    assert rig.grants.get_wrap(1, d1.id) == first
    rig.ok.discard(d1.id)
    rig.pub._step()  # noqa: SLF001 - supersedes the exposed body (empty rotation)
    rig.ok.add(d1.id)
    rig.pub._step()  # noqa: SLF001 - abandons it again
    rig.pub._step()  # noqa: SLF001 - a new exposure for d1 re-uses the stored wrap, not a new sealing
    assert _body(rig)["card_key_wraps"]["wraps"][0]["sealed"] == first


@pytest.mark.parametrize("code", ["stale_card", "wrap_conflict"])
def test_m3_a_card_container_refusal_resends_the_same_list_and_grants_without_the_wraps(rig, code):
    _accept_card(rig.mk)
    rig.add("o1")

    def script(n, body):
        if "card_key_wraps" in body:
            raise RelayRefused(409, code)
        return _ok(n, body)

    rig.session.script = script
    rig.pub._step()  # noqa: SLF001 - refused: the next send drops the wraps
    assert rig.events.committed == [] and rig.mk.head()["exposed"] is not None
    rig.pub._step()  # noqa: SLF001
    assert "card_key_wraps" not in _body(rig) and rig.events.committed[0][0] == "o1"
    first, second = (canon.loads_strict(c[1]) for c in rig.session.calls[:2])
    assert first["member_list"] == second["member_list"] and first["grants"] == second["grants"]


def test_l1_a_failing_device_store_is_membership_local_with_a_backoff_not_the_end_of_the_thread(rig):
    rig.add("o1")

    def broken(ids):
        raise RuntimeError("corrupt devices/<id>.json")

    rig.pub._standing = broken  # noqa: SLF001
    rig.pub.start()
    try:
        for _ in range(250):
            if "membership_local" in rig.alarms.codes():
                break
            time.sleep(0.02)
        assert "membership_local" in rig.alarms.codes()
        assert rig.errors == [] and rig.pub._thread.is_alive()  # noqa: SLF001
    finally:
        rig.pub.stop()
        rig.pub.join(2)


@pytest.mark.parametrize(
    ("status", "code", "abandons"),
    [(401, "unauthenticated", False), (401, "session_expired", False), (400, "weird", False),
     (409, "stale_list", True), (401, "bad_signature", True), (403, "cert_expired", True)],
)  # fmt: skip
def test_l3_only_the_listed_content_and_divergence_codes_abandon(rig, status, code, abandons):
    rig.add("o1")
    rig.session.script = lambda n, body: (_ for _ in ()).throw(RelayRefused(status, code))
    rig.pub._step()  # noqa: SLF001
    head = rig.mk.head()
    if abandons:
        assert head["exposed"] is None and head["excluded"] and rig.events.refused
    else:
        assert head["exposed"] is not None and not head["excluded"] and rig.events.refused == []
        assert head["burned"]["list_seqs"] == []
