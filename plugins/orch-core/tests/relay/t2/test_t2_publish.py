"""T2 for R3 (11A.10 "Not yet covered (R3)"): core's publisher against the REAL orch-relay of ``SOURCE``, run at the
internal 4 MiB cap and at the 16 MiB default.

The host side is a real :class:`orch.store.Store` of the vector workspace A (its WSK ``wsk_a``, owner alice, the
owner device ``primary``, the card's own delegation), so the relay checks every byte the way it would for a real host:
the WSK checkpoint signature, the owner-signed genesis pin, the staged pages and manifest, the folds, the
``relay-info`` maximum and, for the restore, the owner policy (certificate, member list with ``operate``, delegation
equal to the card's). Covered: the first link's publication through ``link_publish``; a backlog published by
catch-up in several checkpoints, each set within C; a ticket restore with its abandoned suffix, accepted by the relay,
and ordinary publication after it."""

from __future__ import annotations

import contextlib
import secrets
import shutil

import pytest

from orch import canon, crypto
from orch.crypto.labels import L
from orch.relay import publish
from orch.relay.publish import Publisher
from orch.wsproto.state import StateDir
from tests.store.helpers import Env
from tests.store.test_checkpoints import rollback

from .. import corpus
from ..helpers import OPEN_CONNS, WS, WSK_PRIV, WSK_PUB, Key, directory
from .kit import CERTS, DEVICE, PERSON, PRIMARY, Owner, now_ms, publish_card, session

pytestmark = pytest.mark.relay_t2

CARD_DELEGATION = corpus.S2["card"]["card"]["delegation"]


class VectorOwner:
    """Alice with the vector owner device ``primary`` (its PK-signed certificate from the corpus)."""

    def __init__(self) -> None:
        self.pk, self.dk_sig = corpus.priv("person_alice.sig"), corpus.priv("primary.sig")
        self.pk_pub, self.sig_pub = corpus.pub("person_alice.sig"), corpus.pub("primary.sig")

    def pk_sign(self, payload: bytes) -> bytes:
        return crypto.sign(self.pk, payload)

    def dk_sign(self, payload: bytes) -> bytes:
        return crypto.sign(self.dk_sig, payload)

    @property
    def ref(self) -> str:
        return "p_" + PERSON

    @property
    def device(self) -> str:
        return "d_" + PRIMARY

    def cert(self, **_: object) -> dict:
        return CERTS["primary"]


class VectorEnv(Env):
    """The store helpers over workspace A: the vector WSK and owner, the card's own delegation in the genesis."""

    def __init__(self, tmp) -> None:
        super().__init__(tmp, clock_start=now_ms() // 1000 - 600)  # below the relay's clock, inside its window
        self.owner = VectorOwner()  # type: ignore[assignment]
        self.people = {"owner": self.owner}  # type: ignore[dict-item]
        self.signer = Key(WSK_PRIV)  # type: ignore[assignment]
        self.wsk_pub = WSK_PUB

    def genesis(self) -> dict:
        e = super().genesis()
        e.pop("sig")
        e["delegation"], e["device_cert"] = CARD_DELEGATION, CERTS["primary"]
        e["sig"] = crypto.b64u(self.owner.dk_sign(canon.person_signing_bytes(WS, "workspace", e)))
        return e


class Host:
    """The linked host: the store, the relay session holding workspace A, the genesis pin on the relay, the
    publisher over a fresh relay state directory."""

    def __init__(self, relay, tmp, *, link_cap: int, tickets: int = 2, events: int = 1) -> None:
        self.relay, self.tmp = relay, tmp
        owner = Owner(relay)
        owner.create_account()
        owner.register("phone")
        first = session(relay)
        assert publish_card(first, relay) == 1
        self.env = env = VectorEnv(tmp)
        env.bootstrap()
        self.store = env.store
        self.store.relay_link({"v": 1, "workspace_id": WS}, guard=contextlib.nullcontext)
        self.uids = []
        for t in range(tickets):
            uid = env.new_ticket(title=f"t{t}")
            self.uids.append(uid)
            for i in range(events):
                env.log(uid, f"{t}-{i}")
        self.sess = session(relay)
        self.sess.hold()
        o = {"v": 2, "suite": 2, "kind": "genesis_pin", "workspace_id": WS, "wsk_pub": crypto.b64u(WSK_PUB),
             "genesis": self.store.genesis}  # fmt: skip
        pin = {"o": o, "sig": crypto.b64u(env.owner.dk_sign(L["sig_genesis_pin"] + canon.dumps(o)))}
        directory(self.sess).put_genesis(pin, CERTS["primary"], genesis=self.store.genesis)
        self.state = StateDir(tmp / "relay-state")
        self.alarms: list[tuple[str, str]] = []
        self.link_cap = link_cap
        self.publisher = self.make_publisher()

    def make_publisher(self) -> Publisher:
        def conn():
            from orch.net import http as rhttp

            c = rhttp.RelayConn(self.relay.origin)
            OPEN_CONNS.append(c)
            return c

        return Publisher(
            self.store, job=lambda fn: fn(), session=self.sess, owner_pk_pub=corpus.pub("person_alice.sig"),
            wsk_pub=WSK_PUB, state=self.state, max_publication_bytes=self.link_cap,
            alarm=lambda c, d: self.alarms.append((c, d)), conn=conn,
        )  # fmt: skip

    def relay_holds_current(self) -> bool:
        held = directory(self.sess).get_checkpoint(genesis=self.store.genesis)
        cur = self.store.relay_position(None).current
        return held is not None and cur is not None and held.hash == cur["hash"]

    def close(self) -> None:
        self.publisher.stop()
        self.state.close()
        self.store.close()


@pytest.fixture
def host(relay, tmp_path):
    hosts = []

    def make(**kw):
        h = Host(relay, tmp_path / f"h{len(hosts)}", **kw)
        hosts.append(h)
        return h

    yield make
    for h in hosts:
        h.close()


def test_the_first_link_publishes_through_link_publish_within_relay_info(relay, host):
    h = host(link_cap=16 << 20, tickets=2, events=2)
    h.publisher.link_publish(h.sess, directory(h.sess))
    assert h.publisher.cap() == relay.configured_cap  # relay-info's maximum, read and respected
    assert h.relay_holds_current() and h.store.outbox_entries() == []
    assert not h.alarms
    h.env.log(h.uids[0], "after the link")
    assert h.publisher.step() == ("progress", 0.0) and h.relay_holds_current()


def test_a_backlog_above_c_is_published_by_catch_up_in_several_checkpoints(relay, host):
    h = host(link_cap=2400, tickets=6, events=3)
    h.publisher.link_publish(h.sess, directory(h.sess))
    lines = h.publisher.history.lines()
    assert len(lines) >= 3, lines  # several catch-up checkpoints, each set within C (the relay took every one)
    assert h.relay_holds_current() and not h.alarms
    assert h.publisher.cap() == 2400


def _restore_queued(host, R):
    """Linked and published, the ticket's lost suffix published, the logs rolled back and the owner's restore queued
    (the owner device in the relay's member list with operate, the relay's restore owner policy)."""
    h = host(link_cap=16 << 20, tickets=2, events=1)
    sess = h.sess
    # the owner device is in the relay's member list with operate (the relay's restore owner policy)
    suite = R.SUITES[2]
    wsk = suite.sig_key(bytes.fromhex(corpus.KEYS["wsk_a.sig"]["seed"]))
    wk = secrets.token_bytes(32)
    members = [{"device_id": DEVICE, "person_id": PERSON, "scopes": ["look", "decide"]},
               {"device_id": PRIMARY, "person_id": PERSON, "scopes": ["look", "decide", "operate"]}]  # fmt: skip
    ml = R.make_member_list(suite, wsk, bytes.fromhex(WS), 1, 1, now_ms(), wk, members)
    grants = []
    for d, name in ((DEVICE, "phone"), (PRIMARY, "primary")):
        kx = crypto.unb64u(CERTS[name]["o"]["dk_kx_pub"])
        grants.append(
            R.make_wk_grant(suite, wsk, bytes.fromhex(WS), bytes.fromhex(d), kx, 1, wk, secrets.token_bytes(32))
        )
    sess.call("PUT", f"/v2/workspaces/{WS}/membership", "PUT /v2/workspaces/{ws}/membership",
              body={"member_list": ml, "grants": sorted(grants, key=lambda g: g["o"]["device_id"])},
              holder=True, request_cap=2 << 20)  # fmt: skip
    h.publisher.link_publish(sess, directory(sess))
    uid = h.uids[0]
    h.store.close()
    backup = h.tmp / "backup"
    shutil.copytree(h.env.root, backup)
    h.store = h.env.open()
    for i in range(3):
        h.env.log(uid, f"lost-{i}")
    h.publisher = h.make_publisher()
    h.publisher.link_publish(sess, directory(sess))
    h.store.close()
    rollback(h.env, backup)
    h.store = h.env.open()
    facts = h.store.restore_facts(uid)
    h.store.append(h.env.person_event(h.env.owner, uid, "restore", **facts, reason="rollback"), log=uid)
    entry = h.store.outbox_first()
    assert entry is not None and entry.kind == "restore"
    return h, entry


def test_a_ticket_restore_with_its_abandoned_suffix_is_accepted_by_the_relay(relay, host, R):
    h, entry = _restore_queued(host, R)
    sess, uid = h.sess, h.uids[0]
    h.publisher = h.make_publisher()
    h.publisher.link_publish(sess, directory(sess))
    assert h.relay_holds_current() and not h.alarms, h.alarms
    assert [r["n"] for r in h.publisher.history.restores()] == [entry.n]
    h.env.log(uid, "after the restore")
    h.publisher.link_publish(sess, directory(sess))
    assert h.relay_holds_current()


class Crash(BaseException):
    pass


def test_a_restore_whose_acknowledgement_was_lost_is_finished_not_held(relay, host, R, monkeypatch):
    """Review R1: the relay commits the restore, the host dies before reading the answer; the next start sees H =
    the restore's target and finishes the history and the delete (it never re-sends: the relay would refuse it)."""
    h, entry = _restore_queued(host, R)
    sess = h.sess
    h.publisher = h.make_publisher()
    real_final = h.publisher._final

    def final(pub, source_hash):
        real_final(pub, source_hash)  # the relay commits the restore ...
        raise Crash()  # ... and the host dies before it reads the answer

    monkeypatch.setattr(h.publisher, "_final", final)
    with pytest.raises(Crash):
        h.publisher.publish_first(sess, directory(sess))
    assert directory(sess).get_checkpoint(genesis=h.store.genesis).hash == entry.hash
    h.publisher = h.make_publisher()
    h.publisher.publish_first(sess, directory(sess))
    assert h.store.outbox_entries() == [] and h.relay_holds_current() and not h.alarms, h.alarms
    assert [r["n"] for r in h.publisher.history.restores()] == [entry.n]


def test_the_first_link_renews_the_lease_while_it_publishes(relay, host, monkeypatch):
    """Review R2: publish_first renews the lease on its own connection while it runs (here every 0.5 s through a
    2 s stalled page). The real lease is 40 s, beyond the T2 budget: the outlived-lease case runs in the layer-1
    tests against the simulator's clock."""
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.5)
    h = host(link_cap=16 << 20, tickets=2, events=1)
    renewals = []
    real_renew = h.sess.renew

    def renew(**kw):
        renewals.append(1)
        return real_renew(**kw)

    monkeypatch.setattr(h.sess, "renew", renew)
    real = h.sess.call
    stalled = {"n": 0}

    def call(method, target, route, **kw):
        if "/pages/" in target and not stalled["n"]:
            stalled["n"] = 1
            import time

            time.sleep(2.0)
        return real(method, target, route, **kw)

    monkeypatch.setattr(h.sess, "call", call)
    h.publisher.publish_first(h.sess, directory(h.sess))
    assert len(renewals) >= 2 and h.relay_holds_current() and not h.sess.fenced


def test_publish_module_constants_match_the_relay(relay):
    """The relay's page cap, request cap and line cap as core builds against them."""
    from orch.wsproto import wire

    src = (relay.dir / "src" / "orch_relay" / "checkpoints.py").read_text()
    assert "PAGE_CAP = 4 * 1024 * 1024" in src and wire.PAGE_CAP == 4 * 1024 * 1024
    assert "REQUEST_CAP = CP_CAP + LINE_CAP + 65536" in src and publish.FINAL_CAP == 2 * 1024 * 1024 + 524_288 + 65_536
    assert "LINE_CAP = 524_288" in src and publish.LINE_CAP == 524_288
