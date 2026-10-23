"""R5 on the real host: the ``WorkspaceService`` hook (11A.4), the worker over the R2 loopback seam on a served
workspace (11A.10 layer 1), ``host.status.relay``, the stop marker across restarts, ``relay_fields`` registered once
and ``submit.recover`` never run in ``open``, the reader reservation's lock order, the convenience halt, the stuck
worker, unlink and relink."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import pytest

from orch import crypto
from orch.host import relayhook, relaylink
from orch.host.errors import HostError
from orch.net import http
from orch.relay import worker as relay_worker
from orch.wsproto.state import FileHolderStore, HolderRecord, StateDir
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import lab, served  # noqa: F401 - fixtures
from tests.host.lifecycle_helpers import UID

from .bridgekit import R6, Epochs, Phone, open_chunk
from .helpers import OPEN_CONNS
from .mock import MockRelay
from .test_bridge_loop import BridgeSim, _item

MIB4 = 4 << 20


def _on_worker(served, fn) -> Any:  # noqa: F811
    return served.service.submit(fn).result(60)


def _wait(pred, timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _relay_threads(id8: str) -> list[str]:
    return sorted(t.name for t in threading.enumerate() if t.name.startswith("orch-relay-") and t.name.endswith(id8))


class _NoStep:
    def step(self) -> None:
        return None


def _seams(r6, **kw) -> relayhook.Seams:
    """Every seam set (the gate-open rule): R6's fixture, and stand-ins for R3, R4 and R6a."""
    base = {
        "r6": r6, "publisher": lambda s, w: relay_worker.NoPublisher(), "unpublished": lambda s, w: _NoStep(),
        "link_source": lambda s: relay_worker.NoLinkSource(),
    }  # fmt: skip
    return relayhook.Seams(**{**base, **kw})


def _intent(state, wid) -> None:
    """An unlink intent bound to the current link (its ``linked_at``), as ``relay_unlink`` writes it."""
    from orch.host.custody import read_json

    link = read_json(relaylink.link_path(state, wid), UID)
    relaylink.mark_unlink_intent(state, wid, UID, linked_at=link.get("linked_at") if isinstance(link, dict) else None)


def _reserve(served, origin: str = "https://relay.example") -> dict:  # noqa: F811
    svc = served.service
    return _on_worker(served, lambda: relayhook.reserve_link(svc, origin=origin, max_publication_bytes=MIB4))


class FakeWorker:
    def __init__(self, *, stuck: bool = False) -> None:
        self.stops = 0
        self.stuck = stuck
        self.alarms: list[tuple[str, str]] = []
        self.release = threading.Event()
        self.stop_threads: list[str] = []

    def alarm(self, code: str, detail: str) -> None:
        self.alarms.append((code, detail))

    def stop(self) -> None:
        self.stops += 1
        self.stop_threads.append(threading.current_thread().name)

    def join(self, timeout: float) -> bool:
        if self.stuck:
            return self.release.wait(timeout)
        return True


# -- the hook, dormant ------------------------------------------------------------------------------------------------


def test_the_hook_is_dormant_behind_the_r6_gate_and_status_says_why(served, monkeypatch):  # noqa: F811
    from orch.relay import worker as _w6

    monkeypatch.setattr(_w6, "R6_READY", False)  # the closed gate is what this test is about
    svc = served.service
    assert svc.relay is None and svc.relay_reason == "not_linked"
    c = served.client()
    assert "relay" not in ok(c.req("host.status"))["data"]  # never linked: no relay field
    link = _reserve(served)
    assert link["state"] == "linking" and link["workspace_id"] == served.wid and link["max_publication_bytes"] == MIB4
    assert svc.relay_start() == "none" and svc.relay is None
    assert svc.relay_reason == relay_worker.R6_GATE_REASON
    _on_worker(served, svc._relay_open)  # noqa: SLF001 - the open's hook itself
    assert svc.relay is None and svc.relay_reason == relay_worker.R6_GATE_REASON
    rel = ok(c.req("host.status"))["data"]["relay"]
    assert rel["link"] == "linking" and rel["worker"] == "off" and rel["alive"] is False
    assert rel["gate"] == {"r6_ready": False, "reason": relay_worker.R6_GATE_REASON}
    assert rel["reason"] == relay_worker.R6_GATE_REASON and rel["stopped"] is False
    assert rel["outbox_pending"] == 0 and rel["alarms"] == []
    assert not _relay_threads(served.wid[:8])


def test_start_worker_refuses_the_closed_gate_even_when_called_directly(served, monkeypatch):  # noqa: F811
    from orch.relay import worker as _w6

    monkeypatch.setattr(_w6, "R6_READY", False)  # the closed gate is what this test is about
    _reserve(served)
    with pytest.raises(relay_worker.SeamMissing, match="r6_not_ready"):
        relayhook.start_worker(served.service, object(), relayhook.Seams())
    assert relayhook.running_slots(served.service.state_dir) == set()


def test_with_the_gate_open_a_missing_r6_seam_fails_the_start_and_leaves_nothing_open(served, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    svc = served.service
    _reserve(served)
    svc.relay_starter = relayhook.make_starter(object(), relayhook.Seams())  # (serve installs the real seams, B1)
    assert svc.relay_start() == "none"
    assert svc.relay_reason == "start_failed: SeamMissing"
    StateDir(relayhook.relay_state_path(svc)).close()  # not left locked
    assert relayhook.running_slots(svc.state_dir) == set()


def test_a_start_that_fails_after_opening_the_state_directory_releases_everything(served, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    svc = served.service
    _reserve(served)

    def r6(service):
        raise RuntimeError("R6 load failed")  # after the slot and the StateDir were taken

    svc.relay_starter = relayhook.make_starter(object(), _seams(r6))
    assert svc.relay_start() == "none" and svc.relay_reason == "start_failed: RuntimeError"
    assert relayhook.running_slots(svc.state_dir) == set()  # the slot is released
    StateDir(relayhook.relay_state_path(svc)).close()  # and the directory lock


# -- the hook at open across restarts ---------------------------------------------------------------------------------


@pytest.fixture
def host_lab(tmp_path):
    import errno

    from tests.host.lifecycle_helpers import Lab

    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


def test_the_open_hook_honours_the_stop_marker_across_restarts_registers_once_and_never_recovers(host_lab, monkeypatch):
    import orch.host.relayentry as RE
    import orch.relay.submit as relay_submit

    hl = host_lab
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    starts: list[str] = []
    registered: list[str] = []
    real_register = RE.register

    def make_starter(backend, seams=None):
        def starter(svc):
            starts.append(threading.current_thread().name)
            svc.relay = FakeWorker()

        return starter

    def register(svc):
        registered.append(svc.workspace_id)
        real_register(svc)

    def recover(*a, **k):  # pragma: no cover - must never run in open (J9: the bridge runs it at worker start)
        raise AssertionError("submit.recover in open")

    monkeypatch.setattr(relayhook, "make_starter", make_starter)
    monkeypatch.setattr(RE, "register", register)
    monkeypatch.setattr(relay_submit, "recover", recover)
    with hl.host() as h, h.client() as c:
        wid = ok(hl.register(c))["data"]["workspace_id"]
        svc = h.host.services[wid]
        assert starts == [] and svc.relay_reason == "not_linked" and registered == [wid]
        assert callable(svc.outcomes.relay_fields)
        svc.submit(
            lambda: relayhook.reserve_link(svc, origin="https://relay.example", max_publication_bytes=MIB4)
        ).result(30)
        ok(c.call("host.relay_stop", {"workspace_id": wid}))
    with hl.host() as h, h.client() as c:
        svc = h.host.services[wid]
        assert starts == [] and svc.relay is None and svc.relay_reason == "stopped"  # honoured at open
        assert registered == [wid, wid]  # once per open
        res = ok(c.call("host.relay_start", {"workspace_id": wid}))
        assert res["data"]["worker"] == "started" and len(starts) == 1
    with hl.host() as h:
        svc = h.host.services[wid]
        assert len(starts) == 2 and starts[1].startswith(f"orch-ws-{wid[:8]}")  # at open, on the worker thread
        assert isinstance(svc.relay, FakeWorker)
    assert relaylink.is_stopped(hl.state, wid, UID) is False


# -- the worker over the loopback relay on a served workspace ----------------------------------------------------------


@pytest.fixture
def loopback(served, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    from orch.custody import FileBackend
    from orch.host.dispatch import RegistryBackend

    svc = served.service
    m = MockRelay()
    sim = BridgeSim(m.origin(), owner_pk=b"")
    m.fallback = sim
    wsk_pub = FileBackend(svc.state_dir / "hosts" / served.wid / "keys").public_key("wsk")
    sim.cards[served.wid] = {"o": {"wsk_pub": crypto.b64u(wsk_pub)}}  # what the sim's login checks
    link = _reserve(served, m.origin())
    _on_worker(served, lambda: svc.store.relay_mark_linked(link))
    st = StateDir(relayhook.relay_state_path(svc))  # this directory held gen 1 before (a restart, not a first link)
    hs = FileHolderStore(st)
    hs.save(HolderRecord(True, 1, hs.instance_id(), None))
    st.close()
    sim.holders[served.wid] = (1, sim.now + 3_600_000)
    epochs = Epochs()
    r6 = R6(epochs)
    phone = Phone()
    r6.add(phone)

    def conn(origin, link):
        c = http.RelayConn(origin)
        OPEN_CONNS.append(c)
        return c

    svc.relay_starter = relayhook.make_starter(RegistryBackend(), _seams(lambda s: (epochs, r6)), transport=conn)
    env = {"m": m, "sim": sim, "epochs": epochs, "phone": phone, "wsk_pub": wsk_pub}
    yield env
    svc.relay_stop()
    m.close()


def test_the_worker_serves_through_the_real_relay_entry_and_stops_cleanly(served, loopback):  # noqa: F811
    svc, sim, phone, epochs = served.service, loopback["sim"], loopback["phone"], loopback["epochs"]
    id8 = served.wid[:8]
    assert svc.relay_start() == "started" and svc.relay is not None
    assert relayhook.running_slots(svc.state_dir) == {served.wid}
    assert _wait(lambda: sim.polls >= 1)
    names = _relay_threads(id8)
    for role in ("poll", "resp", "job0", "job1", "aux"):
        assert f"orch-relay-{role}-{id8}" in names
    env = phone.request(epochs.keys, {"op": "whoami", "args": {}}, ws=served.wid)
    _item(sim, 1, env)
    rid = env[40:56].hex()
    assert _wait(lambda: any(k[0] == rid for k in sim.chunks))
    (chunk,) = [v for (r, _), v in sim.chunks.items() if r == rid]
    # the phone is in R6's (fixture) list but not an F1 device: the real host standing, run as a worker job, says so
    assert open_chunk(chunk, epochs.keys, loopback["wsk_pub"])[1] == {"refusal": "not_member"}
    rel = ok(served.client().req("host.status"))["data"]["relay"]
    assert rel["worker"] == "running" and rel["alive"] is True and rel["link"] == "linked" and rel["gen"] == 1
    # a store job in flight when the stop begins finishes before the stop returns; the store is still open for it
    worker = svc.relay
    slow = worker.submit(lambda: (time.sleep(0.3), svc.store is not None)[1])
    assert svc.relay_stop() == "stopped"
    assert slow.result(5) is True
    assert not _relay_threads(id8) and svc.relay is None
    with pytest.raises(relay_worker.WorkerStopped):
        worker.submit(lambda: None)
    assert relaylink.is_stopped(svc.state_dir, served.wid, UID)
    StateDir(relayhook.relay_state_path(svc)).close()  # the worker released its directory lock
    assert relayhook.running_slots(svc.state_dir) == set()
    rel = ok(served.client().req("host.status"))["data"]["relay"]
    assert rel["worker"] == "off" and rel["reason"] == "stopped" and rel["stopped"] is True


def test_a_double_start_keeps_one_worker(served, loopback):  # noqa: F811
    svc = served.service
    assert svc.relay_start() == "started"
    first = svc.relay
    assert svc.relay_start() == "none" and svc.relay is first
    assert relayhook.running_slots(svc.state_dir) == {served.wid}


def test_close_stops_the_worker_before_the_store_closes(served, loopback):  # noqa: F811
    svc = served.service
    assert svc.relay_start() == "started"
    assert _wait(lambda: loopback["sim"].polls >= 1)
    seen: list[bool] = []
    svc.relay.submit(lambda: (time.sleep(0.3), seen.append(svc.store is not None)))
    svc.close()
    assert seen == [True] and svc.store is None
    assert not _relay_threads(served.wid[:8])


def test_a_fenced_worker_is_reported_failed_and_stops_cleanly(served, loopback):  # noqa: F811
    svc, sim = served.service, loopback["sim"]
    assert svc.relay_start() == "started"
    assert _wait(lambda: sim.polls >= 1)
    gen, until = sim.holders[served.wid]
    sim.holders[served.wid] = (gen + 1, until)  # another holder took over
    # "failed" is recorded before the worker thread has finished ending (``alive`` is cleared last): wait for both
    assert _wait(lambda: (r := relayhook.status(svc))["worker"] == "failed" and r["alive"] is False)
    rel = relayhook.status(svc)
    assert rel["reason"] == "Fenced"
    assert svc.relay_start() == "none"  # the failed worker stays in place: stop, then start (operator)
    assert svc.relay_stop() == "stopped" and not _relay_threads(served.wid[:8])


# -- stuck worker, convenience halt -----------------------------------------------------------------------------------


def test_a_stuck_worker_is_bounded_reported_and_blocks_a_new_start(served, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    svc = served.service
    _reserve(served)
    svc.relay_join_seconds = 0.2
    stuck = FakeWorker(stuck=True)
    svc.relay = stuck
    t0 = time.monotonic()
    assert svc.relay_stop() == "stuck"
    assert time.monotonic() - t0 < 5
    rel = relayhook.status(svc)
    assert rel["worker"] == "stuck" and rel["reason"] == "relay_thread_stuck"
    with pytest.raises(HostError) as e:
        svc.relay_start()
    assert e.value.code == "lock.busy"
    stuck.release.set()
    assert svc.relay_stop() == "none"
    assert relayhook.status(svc)["worker"] == "off"


def test_a_foreign_convenience_event_halts_a_running_worker(served, monkeypatch):  # noqa: F811
    svc = served.service
    w = FakeWorker()
    svc.relay = w
    monkeypatch.setattr(relayhook, "convenience_block", lambda s: "convenience_events_present")
    _on_worker(served, lambda: svc._build_journal(audit=True))  # noqa: SLF001 - the foreign-append rebuild
    assert svc.relay is None and svc.relay_reason == "convenience_events_present"
    assert w.stops >= 1 and w.alarms and w.alarms[0][0] == "convenience_events_present"
    # stopped at once on the worker thread itself (no store job is queued from then on), joined on the halt thread
    assert w.stop_threads[0].startswith(f"orch-ws-{served.wid[:8]}")
    assert _wait(lambda: svc.relay_stopping_count() == 0)


def test_the_convenience_check_reads_the_flag_and_history_under_the_store_lock(served):  # noqa: F811
    from orch.host import convenience

    svc = served.service
    assert _on_worker(served, lambda: relayhook.convenience_block(svc)) is None
    convenience.set_enabled(svc.state_dir, served.wid, True, UID)
    assert _on_worker(served, lambda: relayhook.convenience_block(svc)) == "convenience_events_present"


# -- the reader reservation, the cap, unlink --------------------------------------------------------------------------


def test_reserve_link_takes_the_store_lock_first_then_the_host_wide_lock(served, monkeypatch):  # noqa: F811
    """11A.5 "R5 lock order": the store lock, then the guard's host-wide lock; inside the host-wide lock the store
    lock is only re-entered (already held by this thread), never newly taken (two workspaces linking at once would
    deadlock otherwise)."""
    import contextlib

    from orch.host import convenience

    svc = served.service
    store = svc.store
    real_flock, real_locked = convenience._flock, store._locked  # noqa: SLF001
    depth = threading.local()
    order: list[tuple[str, int]] = []

    @contextlib.contextmanager
    def flock(*a, **k):
        order.append(("flock", getattr(depth, "n", 0)))
        depth.flock = True
        try:
            with real_flock(*a, **k):
                yield
        finally:
            depth.flock = False

    @contextlib.contextmanager
    def locked(*a, **k):
        n = getattr(depth, "n", 0)
        if getattr(depth, "flock", False):
            order.append(("store-inside-flock", n))
        depth.n = n + 1
        try:
            with real_locked(*a, **k):
                yield
        finally:
            depth.n = n

    monkeypatch.setattr(convenience, "_flock", flock)
    monkeypatch.setattr(store, "_locked", locked)
    _reserve(served)
    assert order and order[0][0] == "flock" and order[0][1] >= 1  # the store lock is held when the flock is taken
    assert all(n >= 1 for what, n in order if what == "store-inside-flock")  # only re-entered inside it


def test_reserve_link_refuses_without_writing_and_an_existing_link_is_the_stores_refusal(served):  # noqa: F811
    from orch.host import convenience
    from orch.store import StoreError

    svc = served.service
    with pytest.raises(HostError) as e:
        _on_worker(served, lambda: relayhook.reserve_link(svc, origin="ftp://x", max_publication_bytes=MIB4))
    assert e.value.code == "invalid.input"
    convenience.set_enabled(svc.state_dir, served.wid, True, UID)
    with pytest.raises(HostError) as e:
        _reserve(served)
    assert e.value.code == "sign.convenience_readers"
    assert not os.path.lexists(relaylink.link_path(svc.state_dir, served.wid))
    convenience.set_enabled(svc.state_dir, served.wid, False, UID)
    _reserve(served)
    with pytest.raises(StoreError) as e2:
        _reserve(served)
    assert e2.value.code == "relay.link_exists"


def test_the_per_process_worker_slots_cap_at_eight(tmp_path):
    state = tmp_path / "s"
    releases = [relayhook._take_slot(state, f"{i:032x}") for i in range(relaylink.MAX_LINKED)]  # noqa: SLF001
    with pytest.raises(HostError) as e:
        relayhook._take_slot(state, "f" * 32)  # noqa: SLF001
    assert e.value.code == "host.limit"
    with pytest.raises(HostError) as e:
        relayhook._take_slot(state, f"{0:032x}")  # noqa: SLF001
    assert e.value.code == "lock.busy"  # the same workspace twice
    releases[0]()
    releases[0]()  # idempotent
    relayhook._take_slot(state, "f" * 32)()  # noqa: SLF001
    for r in releases:
        r()
    assert relayhook.running_slots(state) == set()


def test_unlink_keeps_the_reservation_and_a_relink_is_allowed(served):  # noqa: F811
    from orch.host.convenience import _relay_paired

    svc = served.service
    link = _reserve(served)
    svc.relay = FakeWorker()
    res = ok(served.control.call("host.relay_unlink", {"workspace_id": served.wid}))
    name = res["data"]["unlinked"]
    assert name == f"unlinked-{link['linked_at']}.json" and svc.relay is None
    d = relaylink.relay_dir(svc.state_dir, served.wid)
    assert (d / name).exists() and not (d / "link.json").exists()
    assert _relay_paired(svc.state_dir, served.wid)  # the reservation is permanent (11A.6)
    assert _on_worker(served, lambda: svc.store.outbox_active) is False
    res = served.control.call("host.relay_unlink", {"workspace_id": served.wid})
    assert res["ok"] is False and res["error"]["code"] == "relay.not_linked"
    _reserve(served)  # relink: allowed, changes nothing for convenience
    ok(served.control.call("host.relay_unlink", {"workspace_id": served.wid}))
    assert len([n for n in os.listdir(d) if n.startswith("unlinked-")]) == 2  # never over an existing one


def test_unlink_is_refused_while_a_stuck_worker_runs(served):  # noqa: F811
    svc = served.service
    _reserve(served)
    svc.relay_join_seconds = 0.1
    stuck = FakeWorker(stuck=True)
    svc.relay = stuck
    with pytest.raises(HostError) as e:
        svc.relay_unlink()
    assert e.value.code == "lock.busy"
    assert relaylink.link_path(svc.state_dir, served.wid).exists()  # nothing renamed
    stuck.release.set()


# -- orch host status / doctor ----------------------------------------------------------------------------------------


def test_orch_host_status_names_the_dormant_relay_worker(served, monkeypatch):  # noqa: F811
    from orch.relay import worker as _w6

    monkeypatch.setattr(_w6, "R6_READY", False)  # the closed gate is what this test is about
    from orch.host import status as host_status

    svc = served.service
    assert host_status._relay(svc.state_dir, served.wid, UID) is None  # noqa: SLF001 - never linked: no relay part
    _reserve(served)
    got = host_status._relay(svc.state_dir, served.wid, UID)  # noqa: SLF001
    assert got.pop("ledger")["held"] == []  # R4's ledger view (11A.9), clean
    assert got == {"linked": True, "blocker": relay_worker.R6_GATE_REASON, "outbox_pending": 0, "held": []}
    relaylink.mark_stopped(svc.state_dir, served.wid, UID)
    assert host_status._relay(svc.state_dir, served.wid, UID)["blocker"] == "stopped"  # noqa: SLF001
    rep = {
        "state_dir": "s", "service": None, "host": {"answers": True, "control": "c"}, "here": None, "problems": [],
        "ok": True,
        "workspaces": [{"prefix": "DEMO", "workspace_id": served.wid, "state": "ok", "socket_answers": True,
                        "person": None, "click_to_sign": False, "root": "/r",
                        "relay": {"linked": True, "blocker": "stopped", "outbox_pending": 2}}],
    }  # fmt: skip
    assert "  relay linked=yes worker=stopped outbox=2" in host_status.render(rep)


def test_the_unlink_job_itself_refuses_while_a_worker_runs(served):  # noqa: F811
    """The race guard: a worker started between ``relay_unlink``'s stop and its job is refused in the job, under
    ``relay_cond`` and the store lock, and nothing is renamed."""
    svc = served.service
    _reserve(served)
    svc.relay = FakeWorker()
    with pytest.raises(HostError) as e:
        _on_worker(served, lambda: relayhook.unlink_job(svc))
    assert e.value.code == "lock.busy"
    assert relaylink.link_path(svc.state_dir, served.wid).exists()
    svc.relay = None


def test_the_open_hook_never_starts_beside_a_stuck_worker(served, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    svc = served.service
    _reserve(served)
    starts: list[int] = []
    svc.relay_starter = lambda s: starts.append(1)
    svc.relay_join_seconds = 0.2
    stuck = FakeWorker(stuck=True)
    with svc.relay_cond:
        svc._relay_stuck = stuck  # noqa: SLF001 - a worker whose threads outlived an earlier stop
    _on_worker(served, svc._relay_open)  # noqa: SLF001
    assert starts == [] and svc.relay_reason == "relay_thread_stuck"
    stuck.release.set()
    with svc.relay_cond:
        svc._relay_stuck = None  # noqa: SLF001


# -- round 2 (Opus review of #532) ------------------------------------------------------------------------------------


def test_a_foreign_convenience_event_between_the_precheck_and_service_relay_is_caught(served, loopback, monkeypatch):  # noqa: F811
    """Review R1, the reviewer's repro adapted: on the ``orch host relay start`` path the start thread's precheck
    passes, then a foreign refresh loads a convenience event on the worker thread while the starter still holds
    ``relay_cond``. The audit waits for the starter, sees the worker (``service.relay`` is set before the start
    thread exists) and halts it: never serving with the event in the history."""
    svc = served.service
    prechecked = threading.Event()
    calls: list[str] = []
    audits: list[Any] = []

    def block(s):
        calls.append(threading.current_thread().name)
        if not prechecked.is_set():
            prechecked.set()
            return None  # clean at the worker's precheck
        return "convenience_events_present"  # the history now holds a foreign convenience event

    monkeypatch.setattr(relayhook, "convenience_block", block)
    real_start = relay_worker.RelayWorker.start

    def start(self):
        real_start(self)
        assert prechecked.wait(10)
        # the foreign-append refresh lands on the worker thread while the starter still holds relay_cond. Give it up
        # to 1 s to run before this start returns (the reviewer's repro waited for it): with the fix it is blocked
        # on relay_cond until the starter is done, so the wait times out and the start goes on
        from concurrent.futures import TimeoutError as FutureTimeout

        audits.append(svc.submit(lambda: svc._build_journal(audit=True)))  # noqa: SLF001
        try:
            audits[0].result(timeout=1.0)
        except FutureTimeout:
            pass

    monkeypatch.setattr(relay_worker.RelayWorker, "start", start)
    assert svc.relay_start() == "started"
    audits[0].result(10)
    assert len(calls) == 2  # the audit ran the check
    assert svc.relay is None and svc.relay_reason == "convenience_events_present"
    assert _wait(lambda: not _relay_threads(served.wid[:8]))
    assert relayhook.status(svc)["worker"] == "off"


def test_the_audit_fails_closed_when_the_check_raises(served, monkeypatch):  # noqa: F811
    svc = served.service
    w = FakeWorker()
    svc.relay = w

    def boom(s):
        raise RuntimeError("cannot read the history")

    monkeypatch.setattr(relayhook, "convenience_block", boom)
    _on_worker(served, lambda: svc._build_journal(audit=True))  # noqa: SLF001
    assert svc.relay is None and svc.relay_reason == "relay_convenience_check_failed" and w.stops >= 1


def test_a_failed_journal_build_still_audits(served, monkeypatch):  # noqa: F811
    from orch.host.journal import Journal

    svc = served.service
    w = FakeWorker()
    svc.relay = w
    monkeypatch.setattr(relayhook, "convenience_block", lambda s: "convenience_events_present")

    def fail(self, store):
        raise RuntimeError("build failed")

    monkeypatch.setattr(Journal, "build", fail)
    _on_worker(served, lambda: svc._build_journal(audit=True))  # noqa: SLF001
    assert svc.relay is None and svc.relay_reason == "convenience_events_present"


def test_the_halts_join_never_runs_on_the_worker_thread(served, monkeypatch):  # noqa: F811
    svc = served.service
    joined: list[str] = []

    class SlowJoin(FakeWorker):
        def join(self, timeout: float) -> bool:
            joined.append(threading.current_thread().name)
            return True

    svc.relay = SlowJoin()
    monkeypatch.setattr(relayhook, "convenience_block", lambda s: "convenience_events_present")
    _on_worker(served, lambda: svc._build_journal(audit=True))  # noqa: SLF001
    assert _wait(lambda: joined)
    assert joined[0].startswith(f"orch-relay-halt-{served.wid[:8]}")


def test_the_convenience_check_holds_the_store_lock(served, monkeypatch):  # noqa: F811
    import contextlib

    from orch.host import convenience

    svc = served.service
    store = svc.store
    real_locked, real_refuse = store._locked, convenience.refuse_reader_op  # noqa: SLF001
    depth = threading.local()
    seen: list[int] = []

    @contextlib.contextmanager
    def locked(*a, **k):
        depth.n = getattr(depth, "n", 0) + 1
        try:
            with real_locked(*a, **k):
                yield
        finally:
            depth.n -= 1

    def refuse(*a, **k):
        seen.append(getattr(depth, "n", 0))
        return real_refuse(*a, **k)

    monkeypatch.setattr(store, "_locked", locked)
    monkeypatch.setattr(convenience, "refuse_reader_op", refuse)
    assert _on_worker(served, lambda: relayhook.convenience_block(svc)) is None
    assert seen and seen[0] >= 1


@pytest.mark.parametrize(
    "link",
    [
        {"v": 1, "workspace_id": "0" * 32, "origin": "https://relay.example", "state": "linked"},
        {"v": 1, "origin": "ftp://relay.example", "state": "linked"},
        {"v": 1, "origin": "https://relay.example/path", "state": "linked"},
        {"v": 1, "origin": "https://relay.example", "state": "linked-ish"},
        ["not", "a", "dict"],
    ],
)
def test_start_refuses_a_link_record_of_another_workspace_or_origin(served, monkeypatch, link):  # noqa: F811
    from orch.host.custody import write_private

    monkeypatch.setattr(relay_worker, "R6_READY", True)
    svc = served.service
    d = relaylink.relay_dir(svc.state_dir, served.wid)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(d / "link.json", link)
    with pytest.raises(HostError) as e:
        relayhook.start_worker(svc, object(), _seams(lambda s: (Epochs(), R6(Epochs()))))
    assert e.value.code == "relay.link_mismatch" and svc.relay is None
    assert relayhook.running_slots(svc.state_dir) == set()
    StateDir(relayhook.relay_state_path(svc)).close()


def test_with_the_gate_open_every_seam_must_be_set_and_conform(served, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    svc = served.service
    _reserve(served)
    fixture = lambda s: (Epochs(), R6(Epochs()))  # noqa: E731
    for missing in ("publisher", "unpublished", "link_source"):
        with pytest.raises(relay_worker.SeamMissing, match=missing):
            relayhook.start_worker(svc, object(), _seams(fixture, **{missing: None}))
    with pytest.raises(relay_worker.SeamMissing, match="publisher"):
        relayhook.start_worker(svc, object(), _seams(fixture, publisher=lambda s, w: object()))
    with pytest.raises(relay_worker.SeamMissing, match="r6"):
        relayhook.start_worker(svc, object(), _seams(lambda s: (object(), object())))
    assert svc.relay is None and relayhook.running_slots(svc.state_dir) == set()
    assert "conn" not in {f.name for f in __import__("dataclasses").fields(relayhook.Seams)}


def test_a_starter_whose_worker_fails_to_start_leaves_no_partial_worker(served, loopback, monkeypatch):  # noqa: F811
    svc = served.service

    def boom(self):
        raise RuntimeError("start failed")

    monkeypatch.setattr(relay_worker.RelayWorker, "start", boom)
    with svc.relay_cond, pytest.raises(RuntimeError):  # start_worker itself clears what it set (not only its caller)
        svc.relay_starter(svc)
    assert svc.relay is None and relayhook.running_slots(svc.state_dir) == set()
    assert svc.relay_start() == "none" and svc.relay is None
    assert svc.relay_reason == "start_failed: RuntimeError"
    assert relayhook.running_slots(svc.state_dir) == set()


def test_the_open_hook_waits_out_a_stop_in_progress(served, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    svc = served.service
    _reserve(served)
    starts: list[int] = []
    svc.relay_starter = lambda s: starts.append(1)
    with svc.relay_cond:
        svc._relay_stopping = 1  # noqa: SLF001
    _on_worker(served, svc._relay_open)  # noqa: SLF001
    assert starts == [] and svc.relay_reason == "stop_in_progress"
    with svc.relay_cond:
        svc._relay_stopping = 0  # noqa: SLF001


def test_unlink_while_closing_or_beside_a_stuck_worker_is_refused(served):  # noqa: F811
    svc = served.service
    _reserve(served)
    svc.relay_join_seconds = 0.1
    stuck = FakeWorker(stuck=True)
    with svc.relay_cond:
        svc._relay_stuck = stuck  # noqa: SLF001
    with pytest.raises(HostError) as e:
        svc.relay_unlink()
    assert e.value.code == "lock.busy" and relaylink.link_path(svc.state_dir, served.wid).exists()
    stuck.release.set()
    with svc.relay_cond:
        svc._relay_stuck = None  # noqa: SLF001
    svc.closing = True
    try:
        with pytest.raises(HostError) as e:
            svc.relay_unlink()
        assert e.value.code == "workspace.unavailable"
    finally:
        svc.closing = False


def test_an_unlink_that_times_out_is_reported_pending_not_internal(served, monkeypatch):  # noqa: F811
    from concurrent.futures import Future

    svc = served.service
    _reserve(served)
    slow: Future = Future()
    monkeypatch.setattr(svc, "submit", lambda fn: slow)  # the rename stays queued behind the worker
    real_result = Future.result
    monkeypatch.setattr(Future, "result", lambda self, timeout=None: real_result(self, timeout=0.05))
    with pytest.raises(HostError) as e:
        svc.relay_unlink()
    assert e.value.code == "lock.busy" and e.value.message.startswith("unlink_pending")
    assert svc.relay_reason == "unlink_pending"
    # D-1: durable while queued: the intent and the marker are on disk, nothing may start, relay start is refused
    assert relaylink.unlink_pending(svc.state_dir, served.wid) and relaylink.is_stopped(svc.state_dir, served.wid, UID)
    assert relaylink.start_blocker(svc.state_dir, served.wid, UID) == "unlink_pending"
    monkeypatch.undo()
    with pytest.raises(HostError) as e:
        svc.relay_start()
    assert e.value.code == "lock.busy"


def test_reserve_link_bounds_max_publication_bytes(served):  # noqa: F811
    svc = served.service
    for bad in (0, (1 << 20) - 1, (1 << 30) + 1, 4.0, True):
        with pytest.raises(HostError) as e:
            _on_worker(served, lambda b=bad: relayhook.reserve_link(svc, origin="https://r.example",
                                                                   max_publication_bytes=b))  # fmt: skip
        assert e.value.code == "invalid.input"


def test_host_status_never_fails_on_a_relay_status_error(served, monkeypatch):  # noqa: F811
    _reserve(served)

    def boom(s):
        raise RuntimeError("status broke")

    monkeypatch.setattr(relayhook, "status", boom)
    data = ok(served.client().req("host.status"))["data"]
    assert data["state"] == "ok" and data["relay"] == {"error": "RuntimeError"}


def test_stored_alarms_are_bounded_and_whitelisted(served):  # noqa: F811
    from orch.host.custody import write_private

    svc = served.service
    _reserve(served)
    rows = [{"code": f"c{i}", "count": 1, "detail": "d", "token": "SECRET"} for i in range(100)]
    write_private(relaylink.relay_dir(svc.state_dir, served.wid) / "alarms.json", {"v": 1, "alarms": rows})
    got = relayhook.status(svc)["alarms"]
    assert len(got) == 64 and all("token" not in r for r in got)


# -- round 3 (Opus delta of #532) -------------------------------------------------------------------------------------


@pytest.mark.parametrize("crash", ["before_rename", "after_rename"])
def test_an_unlink_interrupted_by_a_restart_is_finished_at_open_and_never_serves(host_lab, monkeypatch, crash):
    """D-1: the unlink intent is durable. A restart between the intent and the rename (or between the rename and
    clearing the intent) finishes the unlink at the next open; no worker starts, even with the gate open."""
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    starts: list[str] = []

    def make_starter(backend, seams=None, **kw):
        return lambda svc: starts.append(svc.workspace_id)

    monkeypatch.setattr(relayhook, "make_starter", make_starter)
    hl = host_lab
    with hl.host() as h, h.client() as c:
        wid = ok(hl.register(c))["data"]["workspace_id"]
        svc = h.host.services[wid]
        svc.submit(lambda: relayhook.reserve_link(svc, origin="https://r.example", max_publication_bytes=MIB4)).result(
            30
        )
        _intent(hl.state, wid)  # the unlink began; the host dies before the rename
        if crash == "after_rename":
            link = relaylink.link_path(hl.state, wid)
            os.rename(link, link.parent / "unlinked-1.json")
    with hl.host() as h:
        svc = h.host.services[wid]
        d = relaylink.relay_dir(hl.state, wid)
        assert starts == [] and svc.relay is None and svc.relay_reason == "not_linked"
        assert not (d / "link.json").exists() and list(d.glob("unlinked-*.json"))
        assert not relaylink.unlink_pending(hl.state, wid)


def test_relay_start_is_refused_while_an_unlink_is_pending_and_unlink_sets_the_marker(served, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    svc = served.service
    _reserve(served)
    _intent(svc.state_dir, served.wid)
    with pytest.raises(HostError) as e:
        svc.relay_start()
    assert e.value.code == "lock.busy" and "unlink_pending" in e.value.message
    assert relaylink.start_blocker(svc.state_dir, served.wid, UID) == "unlink_pending"
    relaylink.clear_unlink_intent(svc.state_dir, served.wid, UID)
    svc.relay_reason = None
    assert svc.relay_unlink().startswith("unlinked-")
    assert svc.relay_reason == "not_linked"  # D23: the success updates the reason
    assert relaylink.is_stopped(svc.state_dir, served.wid, UID)  # the marker is set before the stop (D-1)
    assert not relaylink.unlink_pending(svc.state_dir, served.wid)


def test_an_ended_stuck_worker_is_forgotten(served, monkeypatch):  # noqa: F811
    """D21: once a retained stuck worker released everything (``alive()`` false), start and unlink no longer
    answer ``lock.busy``."""
    svc = served.service

    class Ended(FakeWorker):
        def alive(self) -> bool:
            return False

    with svc.relay_cond:
        svc._relay_stuck = Ended()  # noqa: SLF001
    assert svc.relay_start() == "none"  # not lock.busy
    with svc.relay_cond:
        assert svc._relay_stuck is None  # noqa: SLF001


def test_start_worker_refuses_a_truthy_non_true_gate(served, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay_worker, "R6_READY", 1)
    _reserve(served)
    with pytest.raises(relay_worker.SeamMissing, match="r6_not_ready"):
        relayhook.start_worker(served.service, object(), _seams(lambda s: (Epochs(), R6(Epochs()))))


def test_seam_members_must_be_callable_and_the_r6_view_checked():  # D14 to D16
    class BadJoin:
        def start(self): ...
        def stop(self): ...

        join = 1

        def publish_first(self, session, directory): ...

    with pytest.raises(relay_worker.SeamMissing, match="not callable"):
        relayhook._conform(BadJoin(), relay_worker.Publisher, "publisher")  # noqa: SLF001
    with pytest.raises(relay_worker.SeamMissing):
        relayhook._conform(object(), relay_worker.LinkSource, "link_source")  # noqa: SLF001
    with pytest.raises(relay_worker.SeamMissing):
        relayhook._check_r6(Epochs(), object())  # noqa: SLF001 - no member_list
    epochs = Epochs()
    epochs.wk = 1  # type: ignore[assignment]
    with pytest.raises(relay_worker.SeamMissing):
        relayhook._check_r6(epochs, R6(Epochs()))  # noqa: SLF001 - present but not callable


def test_a_non_conforming_link_source_stops_the_first_link_before_anything_is_sent(served, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    from orch.host.dispatch import RegistryBackend

    svc = served.service
    _reserve(served)  # linking: the start thread runs the first link
    sent: list[str] = []

    def transport(origin, link):
        sent.append(origin)
        return http.RelayConn(origin)

    seams = _seams(lambda s: (Epochs(), R6(Epochs())), link_source=lambda s: object())
    svc.relay_starter = relayhook.make_starter(RegistryBackend(), seams, transport=transport)
    assert svc.relay_start() == "started"
    assert _wait(lambda: relayhook.status(svc)["worker"] in ("failed", "stopping"))
    assert relayhook.status(svc)["reason"] == "relay_start_failed: SeamMissing"
    assert svc.relay_stop() == "stopped"


def test_a_seam_factory_reaching_the_audit_on_the_open_path_cannot_self_deadlock(served, monkeypatch):  # noqa: F811
    """Low 3: the starter runs the seam factories under ``relay_cond`` on the worker thread; one that reaches the
    foreign-append audit re-enters the (re-entrant) lock instead of wedging the workspace."""
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    svc = served.service
    _reserve(served)
    reached: list[int] = []

    def r6(service):
        service._build_journal(audit=True)  # noqa: SLF001
        reached.append(1)
        raise relay_worker.SeamMissing("stop here")

    svc.relay_starter = relayhook.make_starter(object(), _seams(r6))
    svc.submit(svc._relay_open).result(10)  # noqa: SLF001 - would hang with a plain Lock
    assert reached == [1] and svc.relay_reason == "start_failed: SeamMissing"


def test_stored_alarms_are_sanitised_and_clipped_on_read_back(served):  # noqa: F811
    from orch.host.custody import write_private

    svc = served.service
    _reserve(served)
    row = {"code": "c\x1b[2J", "count": 1, "detail": "x\n" + "y" * 500}
    write_private(relaylink.relay_dir(svc.state_dir, served.wid) / "alarms.json", {"v": 1, "alarms": [row]})
    (got,) = relayhook.status(svc)["alarms"]
    assert got["code"] == "c?[2J" and got["detail"].startswith("x?") and len(got["detail"]) == 200


# -- round 4 (orch-52's Codex review of ac8eb8a8) ---------------------------------------------------------------------


def _recording_fs(monkeypatch, crash_unlink_once: bool = False) -> list[str]:
    """A recording shim over the calls ``unlink_job`` makes: ``link``, ``fsync`` (a directory) and ``unlink`` of
    ``link.json``, in order; optionally the first unlink of ``link.json`` crashes (a power cut after the archive)."""
    from orch.store import fsio

    events: list[str] = []
    real_link, real_unlink, real_fsync = os.link, os.unlink, fsio.fsync_dir
    crashed: list[int] = []

    def link(src, dst, **kw):
        if str(src).endswith("link.json"):
            events.append("link")
        return real_link(src, dst, **kw)

    def unlink(path, *a, **kw):
        if str(path).endswith("link.json"):
            if crash_unlink_once and not crashed:
                crashed.append(1)
                raise OSError("power cut")
            events.append("unlink")
        return real_unlink(path, *a, **kw)

    def fsync_dir(path):
        events.append("fsync")
        return real_fsync(path)

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(fsio, "fsync_dir", fsync_dir)
    return events


def test_the_archive_is_durable_before_link_json_is_removed(served, monkeypatch):  # noqa: F811
    """P1: link, then a directory fsync, and only then the unlink of link.json (then fsync again)."""
    svc = served.service
    _reserve(served)
    events = _recording_fs(monkeypatch)
    svc.relay_unlink()
    i_link, i_unlink = events.index("link"), events.index("unlink")
    assert "fsync" in events[i_link + 1 : i_unlink] and "fsync" in events[i_unlink + 1 :]


def test_a_crash_between_the_archive_and_the_unlink_recovers_to_one_archive(served, monkeypatch):  # noqa: F811
    svc = served.service
    _reserve(served)
    _recording_fs(monkeypatch, crash_unlink_once=True)
    with pytest.raises(OSError):
        svc.relay_unlink()
    d = relaylink.relay_dir(svc.state_dir, served.wid)
    assert (d / "link.json").exists() and len(list(d.glob("unlinked-*.json"))) == 1  # both, the same file
    assert relaylink.unlink_pending(svc.state_dir, served.wid)
    _on_worker(served, svc._relay_open)  # noqa: SLF001 - the open after a restart finishes it
    assert not (d / "link.json").exists() and len(list(d.glob("unlinked-*.json"))) == 1  # reused, not duplicated
    assert not relaylink.unlink_pending(svc.state_dir, served.wid)


@pytest.mark.parametrize("renamed", [False, True])
def test_a_crash_between_the_intent_and_the_marker_still_leaves_the_worker_stopped(host_lab, monkeypatch, renamed):
    """P2: the intent landed, the marker did not (a crash in between). Recovery persists the marker before it
    completes the unlink, so a later relink never starts without ``orch host relay start``."""
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    starts: list[str] = []
    monkeypatch.setattr(relayhook, "make_starter", lambda backend, seams=None, **kw: lambda s: starts.append(1))
    hl = host_lab
    with hl.host() as h, h.client() as c:
        wid = ok(hl.register(c))["data"]["workspace_id"]
        svc = h.host.services[wid]
        svc.submit(lambda: relayhook.reserve_link(svc, origin="https://r.example", max_publication_bytes=MIB4)).result(
            30
        )
        _intent(hl.state, wid)  # the crash comes before mark_stopped
        if renamed:  # and (both completion branches) after the rename too
            link = relaylink.link_path(hl.state, wid)
            os.rename(link, link.parent / "unlinked-1.json")
        assert not relaylink.is_stopped(hl.state, wid, UID)
    with hl.host() as h:
        svc = h.host.services[wid]
        assert relaylink.is_stopped(hl.state, wid, UID) and not relaylink.unlink_pending(hl.state, wid)
        svc.submit(lambda: relayhook.reserve_link(svc, origin="https://r.example", max_publication_bytes=MIB4)).result(
            30
        )
        # a relink: the open hook does not start it; only relay start (an operator act) may
        svc.submit(svc._relay_open).result(30)  # noqa: SLF001
        assert starts == [] and svc.relay_reason == "stopped"


# -- round 5 (Opus micro-delta of rounds 3+4) -------------------------------------------------------------------------


def test_a_reservation_is_refused_while_an_unlink_is_pending(served):  # noqa: F811
    """L1: a relink waits for the pending unlink (finished at the next open), so a leftover intent can never take the
    new link with it."""
    svc = served.service
    _reserve(served)
    _intent(svc.state_dir, served.wid)
    link = relaylink.link_path(svc.state_dir, served.wid)
    os.rename(link, link.parent / "unlinked-1.json")  # renamed; clearing the intent failed
    with pytest.raises(HostError) as e:
        _reserve(served)
    assert e.value.code == "lock.busy" and "unlink_pending" in e.value.message and not link.exists()


def test_a_stale_intent_for_another_link_is_discarded_with_an_alarm_never_acted_on(served, monkeypatch):  # noqa: F811
    """L1: an intent bound to another ``linked_at`` never renames or stops the current link."""
    from orch.relay import worker as _w6

    monkeypatch.setattr(_w6, "R6_READY", False)  # the closed gate is what this test is about
    from orch.host.custody import read_json

    svc = served.service
    _reserve(served)
    relaylink.mark_unlink_intent(svc.state_dir, served.wid, UID, linked_at=1)  # an old link's intent
    _on_worker(served, svc._relay_open)  # noqa: SLF001
    d = relaylink.relay_dir(svc.state_dir, served.wid)
    assert (d / "link.json").exists() and not list(d.glob("unlinked-*.json"))
    assert not relaylink.unlink_pending(svc.state_dir, served.wid)
    assert not relaylink.is_stopped(svc.state_dir, served.wid, UID)  # not acted on: no marker either
    rows = read_json(d / "alarms.json", UID)["alarms"]
    assert [r["code"] for r in rows] == ["relay_unlink_intent_stale"]
    assert svc.relay_reason == relay_worker.R6_GATE_REASON  # the link goes on as usual (dormant by the gate)


@pytest.mark.parametrize("body", [{"v": 1}, ["x"], {"v": 1, "linked_at": "soon"}])
def test_a_corrupt_intent_fails_closed(served, body):  # noqa: F811
    from orch.host.custody import write_private

    svc = served.service
    _reserve(served)
    write_private(relaylink.unlink_intent_path(svc.state_dir, served.wid), body)
    _on_worker(served, svc._relay_open)  # noqa: SLF001
    assert relaylink.link_path(svc.state_dir, served.wid).exists()  # nothing renamed
    assert relaylink.unlink_pending(svc.state_dir, served.wid) and svc.relay_reason == "unlink_pending"
    assert relaylink.start_blocker(svc.state_dir, served.wid, UID) == "unlink_pending"


def test_unlink_pending_fails_closed_on_a_dangling_symlink_and_an_unreadable_directory(served):  # noqa: F811
    """L5 (E13, E14): ``unlink_pending`` uses ``lstat`` (a dangling symlink is an intent) and counts any doubt."""
    svc = served.service
    _reserve(served)
    intent = relaylink.unlink_intent_path(svc.state_dir, served.wid)
    intent.symlink_to(intent.parent / "nowhere")
    assert relaylink.unlink_pending(svc.state_dir, served.wid) is True
    intent.unlink()
    assert relaylink.unlink_pending(svc.state_dir, served.wid) is False
    d = relaylink.relay_dir(svc.state_dir, served.wid)
    os.chmod(d, 0)
    try:
        assert relaylink.unlink_pending(svc.state_dir, served.wid) is True  # cannot tell: pending
    finally:
        os.chmod(d, 0o700)


def test_the_open_never_falls_through_to_a_start_after_a_failed_unlink(served, monkeypatch):  # noqa: F811
    """L5 (E22): a failed unlink at open returns; it never goes on to start a worker (even if the blocker missed it)."""
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    svc = served.service
    _reserve(served)
    _intent(svc.state_dir, served.wid)
    starts: list[int] = []
    svc.relay_starter = lambda s: starts.append(1)

    def fail(service):
        raise OSError("disk")

    monkeypatch.setattr(relayhook, "unlink_job", fail)
    monkeypatch.setattr(relaylink, "start_blocker", lambda *a: None)
    _on_worker(served, svc._relay_open)  # noqa: SLF001
    assert starts == [] and svc.relay_reason == "unlink_pending"


def test_a_truthy_non_true_gate_keeps_every_host_entry_dormant(served, monkeypatch):  # noqa: F811
    """L2: ``start_blocker`` itself goes through ``r6_ready()``: a gate set to 1 opens nothing."""
    monkeypatch.setattr(relay_worker, "R6_READY", 1)
    svc = served.service
    _reserve(served)
    starts: list[int] = []
    svc.relay_starter = lambda s: starts.append(1)
    assert svc.relay_start() == "none" and svc.relay_reason == relay_worker.R6_GATE_REASON
    _on_worker(served, svc._relay_open)  # noqa: SLF001
    assert starts == [] and svc.relay_reason == relay_worker.R6_GATE_REASON


def test_an_unserved_relay_start_never_clears_the_marker_under_an_intent(served):  # noqa: F811
    """L3: ``host.relay_start`` for a registered workspace that is not served refuses while an unlink is pending."""
    svc = served.service
    _reserve(served)
    _intent(svc.state_dir, served.wid)
    relaylink.mark_stopped(svc.state_dir, served.wid, UID)
    services = served.h.host.services
    held = services.pop(served.wid)
    try:
        res = served.control.call("host.relay_start", {"workspace_id": served.wid})
    finally:
        services[served.wid] = held
    assert res["ok"] is False and res["error"]["code"] == "lock.busy"
    assert relaylink.is_stopped(svc.state_dir, served.wid, UID)


# -- round 6: H12d-3 (#555) wiring ------------------------------------------------------------------------------------


def test_link_step1_reads_relay_info_off_the_worker_and_the_reservation_gets_its_level(served, monkeypatch):  # noqa: F811
    from orch.relay import session as relay_session
    from orch.relay.session import RelayInfo
    from orch.store.readerrecs import read_relay_level

    svc = served.service
    seen: list[str] = []
    levels: list[Any] = []

    def fetch(conn, **kw):
        seen.append(threading.current_thread().name)
        return RelayInfo(1, None)

    real = relaylink.link_reservation

    def reservation(service, **kw):
        levels.append(kw.get("relay_level"))
        return real(service, **kw)

    monkeypatch.setattr(relay_session, "fetch_relay_info", fetch)
    monkeypatch.setattr(relaylink, "link_reservation", reservation)
    rec = relayhook.link_step1(svc, origin="https://r.example", max_publication_bytes=MIB4,
                               transport=lambda o, lk: object())  # fmt: skip
    assert rec["state"] == "linking" and levels == [1]
    assert seen and not seen[0].startswith("orch-ws-")  # the network read is never on the worker thread
    assert read_relay_level(svc.state_dir, served.wid, UID) == 1


def test_link_step1_with_a_level_zero_relay_is_refused_under_convenience(served, monkeypatch):  # noqa: F811
    from orch.host import convenience
    from orch.relay import session as relay_session
    from orch.relay.session import RelayInfo

    svc = served.service
    convenience.set_enabled(svc.state_dir, served.wid, True, UID)
    monkeypatch.setattr(relay_session, "fetch_relay_info", lambda conn, **kw: RelayInfo(0, None))
    with pytest.raises(HostError) as e:
        relayhook.link_step1(svc, origin="https://r.example", max_publication_bytes=MIB4,
                             transport=lambda o, lk: object())  # fmt: skip
    assert e.value.code == "sign.convenience_readers"
    assert not relaylink.link_path(svc.state_dir, served.wid).exists()


def test_the_session_observer_is_bound_to_the_exact_stored_origin(served, loopback, monkeypatch):  # noqa: F811
    """Every login re-reads the reader level; the hook writes only while link.json names the origin exactly as it was
    stored when the worker started (a changed origin, a relink: no write)."""
    from orch.host.custody import read_json, write_private
    from orch.relay import session as relay_session
    from orch.store.readerrecs import read_relay_level

    svc = served.service
    hooks: list[Any] = []
    real = relay_session.Session

    class Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            hooks.append(kw.get("relay_info"))
            super().__init__(*a, **kw)

    monkeypatch.setattr(relay_session, "Session", Spy)
    assert svc.relay_start() == "started"
    assert _wait(lambda: loopback["sim"].polls >= 1)
    (hook,) = hooks
    assert callable(hook)
    hook(1)
    assert read_relay_level(svc.state_dir, served.wid, UID) == 1
    path = relaylink.link_path(svc.state_dir, served.wid)
    link = read_json(path, UID)
    write_private(path, {**link, "origin": link["origin"] + "/"})  # not the stored origin any more
    hook(0)
    assert read_relay_level(svc.state_dir, served.wid, UID) == 1  # nothing written for another origin
    write_private(path, link)
    assert svc.relay_stop() == "stopped"


def test_the_observer_takes_only_the_relay_link_lock_never_the_store_lock_or_relay_cond(served):  # noqa: F811
    """Lock order: relay_cond -> store lock -> relay-link.lock (the reservation's guard); the observer, on a relay
    thread under the session's login lock, takes only relay-link.lock (a leaf), so it never waits for a holder of
    relay_cond or the store lock."""
    from orch.store.readerrecs import read_relay_level

    svc = served.service
    _reserve(served)
    origin = "https://relay.example"
    held, release = threading.Event(), threading.Event()

    def holder():
        with svc.relay_cond, svc.store.locked():
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    done: list[bool] = []

    def observe() -> None:
        done.append(relaylink.observe_relay_level(svc.state_dir, served.wid, UID, 1, lambda c, d: None, origin=origin))

    obs = threading.Thread(target=observe)
    obs.start()
    obs.join(3)
    release.set()
    t.join(5)
    assert done == [False] and read_relay_level(svc.state_dir, served.wid, UID) == 1


def test_a_restart_keeps_the_observed_level(host_lab):
    from orch.store.readerrecs import read_relay_level

    hl = host_lab
    with hl.host() as h, h.client() as c:
        wid = ok(hl.register(c))["data"]["workspace_id"]
        svc = h.host.services[wid]
        svc.submit(lambda: relayhook.reserve_link(svc, origin="https://r.example", max_publication_bytes=MIB4,
                                                  relay_level=1)).result(30)  # fmt: skip
        relaylink.observe_relay_level(hl.state, wid, UID, 2, lambda c, d: None, origin="https://r.example")
        assert read_relay_level(hl.state, wid, UID) == 2
    with hl.host():
        assert read_relay_level(hl.state, wid, UID) == 2
