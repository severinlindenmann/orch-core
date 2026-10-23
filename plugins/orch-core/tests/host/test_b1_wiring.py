"""B1 host wiring on a real registered workspace: the seam install, the R3 publisher factory, the first card's
``card_accepted``, wake chaining around ``journal.committed``, the membership publisher's attach and its F1 standing
job. (The end-to-end pairing against the real relay is the T2 test.)"""

from __future__ import annotations

import errno
import threading
from types import SimpleNamespace

import pytest

from orch import crypto
from orch.host import relayhook, relaypair, relayr3, relayr6, relayr6b
from orch.host.custody import write_private
from orch.host.relaylink import link_path
from orch.identity import members
from orch.relay import worker as relay_worker
from orch.relay.r6.membership import MembershipPublisher
from orch.relay.session import Fenced
from orch.wsproto.state import StateDir
from tests.host.lifecycle_helpers import Lab
from tests.relay.helpers import Alarms
from tests.relay.test_b1_membership import Dev, _person

ORIGIN = "https://relay.example"
MIB4 = 4 << 20


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover - a sandbox without AF_UNIX bind
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


def _linked(lab, h, c, *, state="linking"):
    res = lab.register(c)
    assert res["ok"], res
    wid = res["result"]["data"]["workspace_id"]
    service = h.host.services[wid]
    link_path(lab.state, wid).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(
        link_path(lab.state, wid),
        {"v": 1, "workspace_id": wid, "origin": ORIGIN, "state": state, "max_publication_bytes": MIB4},
    )
    return wid, service


class FakeWorker:
    """What the seam factories read of a worker: its state directory, session, alarms, jobs, connection factory."""

    def __init__(self, service) -> None:
        self.state = StateDir(relayhook.relay_state_path(service))
        self.session = SimpleNamespace(workspace_id=service.workspace_id, gen=1)
        self.alarm = Alarms()
        self.conn = lambda: None
        self.companions: list = []
        self.stops = 0

    def job(self, fn):
        return fn()

    def stop(self) -> None:
        self.stops += 1

    def close(self) -> None:
        self.state.close()


# -- install_seams ----------------------------------------------------------------------------------------------------


def test_install_seams_sets_all_four_and_the_membership_seam_once_and_keeps_what_a_test_set(monkeypatch):
    monkeypatch.setattr(relayhook, "SEAMS", relayhook.Seams())
    monkeypatch.setattr(relaypair, "MEMBERSHIP", None)
    seams = relayhook.install_seams()
    assert seams.missing() == [] and relayhook.SEAMS is seams
    assert (seams.r6, seams.link_source) == (relayr6.r6, relayr6.link_source)
    assert seams.publisher is relayr3.publisher and callable(seams.unpublished)
    assert relaypair.MEMBERSHIP is relayr6b.membership
    from orch.host import relaypush

    assert seams.push is relaypush.push  # H13a
    assert relayhook.install_seams() == seams  # idempotent
    mine = lambda s, w: None  # noqa: E731
    monkeypatch.setattr(relayhook, "SEAMS", relayhook.Seams(publisher=mine))
    monkeypatch.setattr(relaypair, "MEMBERSHIP", "mine")
    assert relayhook.install_seams().publisher is mine and relaypair.MEMBERSHIP == "mine"


def test_the_r6_gate_is_open_in_the_code_and_a_closed_gate_still_refuses(monkeypatch):
    assert relay_worker.R6_READY is True and relay_worker.r6_ready() is True
    monkeypatch.setattr(relay_worker, "R6_READY", False)
    assert relay_worker.r6_ready() is False


# -- the first card ---------------------------------------------------------------------------------------------------


def test_card_accepted_moves_the_pending_first_card_once(lab):
    with lab.host() as h, h.client() as c:
        _, service = _linked(lab, h, c)
        try:
            src = relayr6.link_source(service)
            src.card()
            mk = relayr6.keys_for(service)
            assert mk.head()["card"]["pending_card_seq"] == 1 and mk.head()["card"]["card_seq"] == 0
            src.card_accepted()
            assert mk.head()["card"]["pending_card_seq"] is None and mk.head()["card"]["card_seq"] == 1
            src.card_accepted()  # a resumed link: nothing pending, nothing to do
            assert mk.head()["card"]["card_seq"] == 1
        finally:
            relayr6.close(service)


def test_the_hooks_link_steps_call_card_accepted_before_the_first_publication(lab):
    with lab.host() as h, h.client() as c:
        _, service = _linked(lab, h, c)
        order: list[str] = []

        class Source:
            def card_accepted(self):
                order.append("accepted")

        class Pub:
            def publish_first(self, session, directory):
                order.append("publish")

        worker = SimpleNamespace(publisher=Pub())
        steps = relayhook._LinkSteps(worker, service, None, Source(), {}, None, None)  # noqa: SLF001
        steps.publish(None, None)
        assert order == ["accepted", "publish"]
        order.clear()
        relayhook._LinkSteps(worker, service, None, object(), {}, None, None).publish(None, None)  # noqa: SLF001
        assert order == ["publish"]  # a source without the hook (a fake) has nothing to move


# -- the R3 publisher factory -----------------------------------------------------------------------------------------


def test_the_publisher_factory_reads_its_inputs_from_the_workspace_and_the_link(lab):
    with lab.host() as h, h.client() as c:
        wid, service = _linked(lab, h, c)
        worker = FakeWorker(service)
        try:
            pub = relayr3.publisher(service, worker)
            owner = members.check_genesis(service.store.genesis_event())["owner_pk_pub"]
            wsk = crypto.unb64u(service.store.genesis_event()["wsk_pub"])
            assert (pub._owner, pub._wsk, pub._link_cap) == (owner, wsk, MIB4)  # noqa: SLF001
            assert pub.id8 == wid[:8] and pub._store is service.store  # noqa: SLF001
            assert pub._job == worker.job and pub._session is worker.session  # noqa: SLF001
            assert pub._relay_level() in (None, 0, 1)  # noqa: SLF001 - the stored level, read fresh
            assert pub._conn_factory is worker.conn and pub._state is worker.state  # noqa: SLF001
            pub._on_error(Fenced("x"))  # noqa: SLF001 - a fence ends the publisher and the worker
            assert worker.stops == 1 and worker.alarm.codes() == ["relay_publisher_stopped"]
            pub._on_diverged(None)  # noqa: SLF001
            assert "chain.diverged" in worker.alarm.codes()
        finally:
            worker.close()


def test_the_publisher_factory_refuses_without_a_connection_a_store_or_a_cap(lab):
    from orch.host.errors import HostError

    with lab.host() as h, h.client() as c:
        _, service = _linked(lab, h, c)
        worker = FakeWorker(service)
        try:
            worker.conn = None
            with pytest.raises(HostError):
                relayr3.publisher(service, worker)
            worker.conn = lambda: None
            worker.state.write_json("link.json", {"v": 1, "origin": ORIGIN, "state": "linking"})
            with pytest.raises(HostError):
                relayr3.publisher(service, worker)  # link.json names no max_publication_bytes
        finally:
            worker.close()


# -- wake chaining ----------------------------------------------------------------------------------------------------


class Wakes:
    def __init__(self) -> None:
        self.n = 0

    def wake(self) -> None:
        self.n += 1


def test_every_commit_wakes_the_publisher_and_only_a_revoking_one_wakes_the_unpublished_consumer(lab):
    with lab.host() as h, h.client() as c:
        _, service = _linked(lab, h, c)
        store = service.store
        assert store.on_commit is not None and store.on_commit != service.journal.committed  # the chained hook
        pub, unp, member = Wakes(), Wakes(), Wakes()
        service.relay = SimpleNamespace(publisher=pub, unpublished=unp, companions=[member])
        try:
            store.on_commit("workspace", {"type": "ticket.created"})
            assert (pub.n, unp.n, member.n) == (1, 0, 1)
            for typ in ("device.revoked", "member.removed", "restore"):
                store.on_commit("workspace", {"type": typ})
            assert (pub.n, unp.n, member.n) == (4, 3, 4)
            service.relay = SimpleNamespace(publisher=relay_worker.NoPublisher(), unpublished=None, companions=[])
            store.on_commit("workspace", {"type": "restore"})  # NoPublisher has no wake, R4 unset: nothing raised
            service.relay = None
            store.on_commit("workspace", {"type": "restore"})  # no worker: nothing to wake
            assert (pub.n, unp.n) == (4, 3)
        finally:
            service.relay = None


def test_a_wake_that_raises_never_fails_the_commit(lab):
    class Boom:
        def wake(self):
            raise RuntimeError("boom")

    with lab.host() as h, h.client() as c:
        _, service = _linked(lab, h, c)
        service.relay = SimpleNamespace(publisher=Boom(), unpublished=Boom(), companions=[Boom()])
        try:
            service.store.on_commit("workspace", {"type": "restore"})
        finally:
            service.relay = None


# -- R6b on the host --------------------------------------------------------------------------------------------------


def test_attach_makes_the_membership_publisher_a_companion_of_the_worker_and_the_view_is_the_real_one(lab):
    with lab.host() as h, h.client() as c:
        wid, service = _linked(lab, h, c)
        worker = FakeWorker(service)
        try:
            epochs, view = relayr6.r6(service)
            assert type(view).__name__ == "CommittedView" and view.member_list() is None
            m = relayr6b.membership(service)
            assert m.ready() is False
            m.attach(worker)
            (inner,) = worker.companions
            assert isinstance(inner, MembershipPublisher) and inner.ws == wid
            assert m.ready() is False  # no thread yet
            assert threading.active_count() >= 1 and not [
                t for t in threading.enumerate() if t.name == f"orch-relay-r6m-{wid[:8]}"
            ]  # attach starts nothing: the worker starts its companions
        finally:
            relayr6b.close(service)
            relayr6.close(service)
            relaypair.close(service)
            worker.close()


class _Store:
    """A store stand-in for the standing job: F1 devices, forwarded evidence, a lock."""

    def __init__(self, devices, evidence=()):
        self.state = SimpleNamespace(workspace=SimpleNamespace(devices=devices))
        self._evidence = list(evidence)

    def locked(self):
        import contextlib

        return contextlib.nullcontext()

    def forwarded_evidence(self):
        return list(self._evidence)


def _view(person_hex, removed=False, revoked=False):
    return SimpleNamespace(person="p_" + person_hex, removed=removed, revoked=revoked)


def test_the_standing_job_keeps_only_devices_with_f1_standing_no_evidence_and_a_valid_certificate():
    priv, pk = _person()
    ok, removed, revoked, evid, unknown = (Dev(priv) for _ in range(5))
    expired = Dev(priv, expires_ms=1_790_000_000_000 - 1000)
    person = ok.person
    store = _Store(
        {
            "d_" + ok.id: _view(person), "d_" + removed.id: _view(person, removed=True),
            "d_" + revoked.id: _view(person, revoked=True), "d_" + evid.id: _view(person),
            "d_" + expired.id: _view(person),
        },
        evidence=[{"device_id": evid.id, "person_id": person, "reason": "lost", "digest": "x"}],
    )  # fmt: skip
    devs = {d.id: d.record for d in (ok, removed, revoked, evid, expired, unknown)}
    service = SimpleNamespace(store=store, clock=lambda: 1_790_000_000.0)
    pairs = SimpleNamespace(device=lambda i: devs.get(i))
    got = relayr6b.standing(service, pairs, pk, sorted(devs))
    assert got == {ok.id}  # unknown has no F1 view, expired fails verify_cert
