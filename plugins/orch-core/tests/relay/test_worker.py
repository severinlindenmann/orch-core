"""R5: :class:`orch.relay.worker.RelayWorker` on its own (fakes for the loop, the session and the service's submit):
the R6 gate, the lifecycle (start, stop, join(timeout) -> bool), the store seam that refuses after stop, start/stop
races, the first-link and convenience steps on the start thread, the R3/R4 seams, the alarms book and fork safety."""

from __future__ import annotations

import ast
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import orch.relay
from orch import fdwait
from orch.relay import worker as W
from orch.wsproto.state import StateDir


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(W, "R6_READY", True)


class FakeLoop:
    def __init__(self, *, stuck: bool = False) -> None:
        self.started = self.stopped = 0
        self.aux_steps: list = []
        self.error = None
        self.release = threading.Event()
        self.stuck = stuck
        self._threads: list[threading.Thread] = []
        self.joins: list[float] = []

    def start(self) -> None:
        self.started += 1
        t = threading.Thread(target=self._run, name="orch-relay-poll-fake", daemon=True)
        self._threads.append(t)
        t.start()

    def _run(self) -> None:
        self.release.wait(30)

    def stop(self) -> None:
        self.stopped += 1
        if not self.stuck:
            self.release.set()

    def join(self, timeout: float) -> bool:
        self.joins.append(timeout)
        for t in self._threads:
            t.join(timeout)
        return not any(t.is_alive() for t in self._threads)

    def threads(self):
        return list(self._threads)


class FakeSession:
    gen, fenced = 3, False

    def __init__(self) -> None:
        self.logouts = 0

    def logout(self, deadline_s: float = 5.0) -> None:
        self.logouts += 1


class FakeRids:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class Pub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def join(self, timeout: float) -> bool:
        self.calls.append("join")
        return True

    def publish_first(self, session, directory) -> None:
        self.calls.append("publish_first")


class Rig:
    def __init__(self, tmp: Path, **kw) -> None:
        self.ex = ThreadPoolExecutor(1, thread_name_prefix="orch-ws-test")
        self.state = StateDir(Path(os.path.realpath(tmp)) / "relay")
        self.session = FakeSession()
        self.loop = FakeLoop(stuck=kw.pop("stuck", False))
        self.rids = FakeRids()
        self.ended = 0
        self.saved: list[dict] = []
        self.alarms = W.Alarms(now_ms=lambda: 7, persist=self.saved.append)
        self.link_state = kw.pop("link_state", "linked")
        self.links = 0
        self.w = W.RelayWorker(
            workspace_id="ab" * 16, state=self.state, session=self.session, submit=self.ex.submit,
            alarms=self.alarms, stop_event=threading.Event(), link_state=lambda: self.link_state,
            on_ended=self._ended, first_link=kw.pop("first_link", self._link), **kw,
        )  # fmt: skip
        self.w.attach(self.loop, self.rids)

    def _ended(self) -> None:
        self.ended += 1

    def _link(self) -> None:
        self.links += 1

    def wait_started(self) -> None:
        assert _wait(lambda: self.loop.started == 1)

    def close(self) -> None:
        self.w.stop()
        self.w.join(5)
        self.ex.shutdown(wait=True)
        self.state.close()


def _wait(pred, timeout=10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def rig(tmp_path, gate):
    r = Rig(tmp_path)
    yield r
    r.close()


# -- the R6 gate ------------------------------------------------------------------------------------------------------


def test_the_r6_gate_is_the_literal_true_and_nothing_in_the_source_assigns_it():
    """11A.1: no poll thread until R6 is merged and integration-tested. The gate is ONE module-level literal; no other
    source line under ``orch/`` assigns or patches it (tests may, through monkeypatch). Flipping it is a reviewed
    change with the 11A.4 "As built in R5" checklist. Honest limit: same-process code can still set the attribute."""
    tree = ast.parse(Path(W.__file__).read_text())
    assigns = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.Assign, ast.AnnAssign))
        and any(isinstance(t, ast.Name) and t.id == "R6_READY" for t in (n.targets if isinstance(n, ast.Assign)
                                                                           else [n.target]))
    ]  # fmt: skip
    assert len(assigns) == 1 and isinstance(assigns[0].value, ast.Constant) and assigns[0].value.value is True
    assert assigns[0] in tree.body  # module level, not inside a function
    src = Path(orch.relay.__file__).resolve().parent.parent
    offenders = []
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Attribute) and t.attr == "R6_READY":
                        offenders.append(str(path))
                    if isinstance(t, ast.Name) and t.id == "R6_READY" and path != Path(W.__file__):
                        offenders.append(str(path))
            if isinstance(node, ast.Constant) and node.value == "R6_READY" and path != Path(W.__file__):
                offenders.append(str(path))  # setattr(worker, "R6_READY", …) and friends
    assert offenders == []


def test_start_refuses_with_the_gate_closed_before_any_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "R6_READY", False)
    r = Rig(tmp_path)
    before = {t.name for t in threading.enumerate()}
    with pytest.raises(W.SeamMissing, match="r6_not_ready"):
        r.w.start()
    assert r.loop.started == 0 and {t.name for t in threading.enumerate()} <= before | {"orch-ws-test_0"}
    r.close()


# -- lifecycle --------------------------------------------------------------------------------------------------------


def test_start_runs_the_precheck_as_a_job_then_the_loop_then_the_publisher(tmp_path, gate):
    pub = Pub()
    seen: list[str] = []
    r = Rig(tmp_path, publisher=pub, precheck=lambda: seen.append(threading.current_thread().name))
    step = lambda: None  # noqa: E731
    r.w.unpublished = type("U", (), {"step": staticmethod(step)})()
    r.w.start()
    r.wait_started()
    assert _wait(lambda: pub.calls == ["start"])  # the publisher right after the loop, in the same locked step
    assert seen == ["orch-ws-test_0"]  # on the workspace's worker thread (the service's submit)
    assert r.links == 0  # linked: no first link
    assert r.loop.aux_steps == [("unpublished", step)]  # R4's seam: one step on the aux thread
    with pytest.raises(RuntimeError, match="twice"):
        r.w.start()
    r.w.stop()
    assert r.w.join(5) is True
    assert pub.calls == ["start", "stop", "join"]
    assert r.session.logouts == 1 and r.rids.closed == 1 and r.ended == 1
    r.w.stop()
    assert r.w.join(5) is True and r.ended == 1 and r.session.logouts == 1  # once
    r.ex.shutdown(wait=True)


def test_a_linking_workspace_runs_the_first_link_before_the_loop(tmp_path, gate):
    order: list[str] = []
    r = Rig(tmp_path, link_state="linking", first_link=lambda: order.append("link"))
    real = r.loop.start
    r.loop.start = lambda: (order.append("loop"), real())  # type: ignore[method-assign]
    r.w.start()
    r.wait_started()
    assert order == ["link", "loop"]
    r.close()


def test_a_failed_first_link_stops_the_worker_with_an_alarm_and_starts_nothing(tmp_path, gate):
    def boom():
        raise W.SeamMissing("R6a")

    r = Rig(tmp_path, link_state="linking", first_link=boom)
    r.w.start()
    assert r.w.join(5) is True
    assert r.loop.started == 0 and r.w.stopping and r.w.error == "relay_start_failed: SeamMissing"
    assert "relay_start_failed" in r.alarms.codes()
    with pytest.raises(W.WorkerStopped):
        r.w.submit(lambda: None)
    r.close()


def test_the_convenience_precheck_keeps_the_worker_from_serving(tmp_path, gate):
    r = Rig(tmp_path, precheck=lambda: "convenience_events_present", link_state="linking")
    r.w.start()
    assert r.w.join(5) is True
    assert r.loop.started == 0 and r.links == 0  # nothing is sent: not even the first link
    assert r.w.error == "convenience_events_present" and r.alarms.codes() == ["convenience_events_present"]
    assert r.saved and r.saved[0]["alarms"][0]["code"] == "convenience_events_present"
    r.close()


def test_stop_during_start_never_starts_the_loop(tmp_path, gate):
    release = threading.Event()
    r = Rig(tmp_path, precheck=lambda: release.wait(10) and None)
    r.w.start()
    time.sleep(0.05)
    r.w.stop()
    release.set()
    assert r.w.join(5) is True
    assert r.loop.started == 0 and r.ended == 1
    r.close()


def test_stop_before_start_refuses_the_start(tmp_path, gate):
    r = Rig(tmp_path)
    r.w.stop()
    with pytest.raises(W.WorkerStopped):
        r.w.start()
    r.close()


def test_submit_refuses_once_stop_began_and_jobs_queued_before_run(tmp_path, gate):
    r = Rig(tmp_path)
    gate_ev = threading.Event()
    first = r.w.submit(lambda: gate_ev.wait(5))
    second = r.w.submit(lambda: "ran")  # queued before stopping: it runs (the worker FIFO)
    r.w.stop()
    with pytest.raises(W.WorkerStopped):
        r.w.submit(lambda: "late")
    with pytest.raises(W.WorkerStopped):
        r.w.job(lambda: "late")
    gate_ev.set()
    assert first.result(5) is True and second.result(5) == "ran"
    r.close()


def test_join_is_bounded_and_false_while_a_thread_lives_then_finishes_once(tmp_path, gate):
    r = Rig(tmp_path, stuck=True)
    r.w.start()
    r.wait_started()
    r.w.stop()
    t0 = time.monotonic()
    assert r.w.join(0.3) is False
    assert time.monotonic() - t0 < 2.0
    assert r.session.logouts == 0 and r.rids.closed == 0 and r.ended == 0  # nothing released while it runs
    assert r.w.alive() is True and r.w.status()["alive"] is True
    r.loop.release.set()
    assert r.w.join(5) is True and r.ended == 1 and r.w.alive() is False
    r.close()


def test_the_loop_is_joined_twice_so_late_spawned_threads_are_caught(tmp_path, gate):
    r = Rig(tmp_path)
    r.w.start()
    r.wait_started()
    r.w.stop()
    assert r.w.join(5) is True
    assert len(r.loop.joins) == 2
    r.close()


def test_status_has_no_secret_and_reports_the_worker(tmp_path, gate):
    pub = Pub()
    r = Rig(tmp_path, publisher=pub)
    r.w.start()
    r.wait_started()
    assert _wait(lambda: r.w.status()["serving"])
    st = r.w.status()
    assert set(st) == {"alive", "stopping", "serving", "error", "gen", "fenced", "alarms"}
    assert st["alive"] is True and st["gen"] == 3
    r.close()


# -- seams ------------------------------------------------------------------------------------------------------------


def test_the_default_seams_refuse_or_do_nothing():
    p = W.NoPublisher()
    p.start()
    p.stop()
    assert p.join(0) is True
    with pytest.raises(W.SeamMissing, match="R3"):
        p.publish_first(None, None)  # type: ignore[arg-type]
    src = W.NoLinkSource()
    for fn in (src.card, src.genesis, src.owner_pk_pub):
        with pytest.raises(W.SeamMissing):
            fn()


def test_alarms_are_bounded_clipped_and_persisted_when_a_code_first_appears():
    saved: list[dict] = []
    a = W.Alarms(now_ms=lambda: 5, persist=saved.append)
    a("fenced", "x" * 1000)
    a("fenced", "again")
    assert len(saved) == 1
    for i in range(W.MAX_ALARM_CODES + 5):
        a(f"c{i}", "d")
    codes = a.codes()
    assert len(codes) == W.MAX_ALARM_CODES + 1 and "relay_alarm_codes_exhausted" in codes
    row = a.snapshot()["alarms"][0]
    assert row["count"] == 2 and row["detail"] == "again" and len(saved) == W.MAX_ALARM_CODES + 1


def test_alarms_survive_a_failing_write():
    def fail(snap):
        raise OSError("disk full")

    a = W.Alarms(now_ms=lambda: 5, persist=fail)
    a("relay_quota", "route")
    assert a.codes() == ["relay_quota"]


# -- fork safety ------------------------------------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork")
@pytest.mark.parametrize("held", [False, True])
def test_a_forked_child_cannot_use_or_release_the_parents_worker(tmp_path, gate, held):
    """In the child: no store job, no start, no stop effect, no logout, no close; it never takes the worker's lock (a
    parent thread may hold it at the fork: ``held`` forks while another thread holds it). Bounded: the parent waits
    at most 10 s for the child's answer and kills it otherwise."""
    import signal

    r = Rig(tmp_path)
    r.w.start()
    r.wait_started()
    assert _wait(lambda: not any(t.name.startswith("orch-relay-start-") for t in threading.enumerate()))
    holding, release = threading.Event(), threading.Event()
    if held:

        def hold():
            with r.w._lock:  # noqa: SLF001 - the parent thread that holds the worker's lock at the fork
                holding.set()
                release.wait(10)

        threading.Thread(target=hold, daemon=True).start()
        assert holding.wait(5)
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child
        signal.alarm(8)
        code = 0
        try:
            try:
                r.w.submit(lambda: None)
                code |= 1
            except W.WorkerStopped:
                pass
            try:
                r.w.start()
                code |= 2
            except (W.WorkerStopped, RuntimeError):
                pass
            r.w.stop()
            assert r.w.join(0.5) is True
            if r.session.logouts or r.rids.closed:
                code |= 4
        except BaseException:  # noqa: BLE001
            code |= 8
        os.write(wfd, bytes([code]))
        os._exit(0)
    os.close(wfd)
    try:
        ready = fdwait.readable(rfd, 10)
        got = os.read(rfd, 1) if ready else b"hang"
    finally:
        os.close(rfd)
        if not ready:
            os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        release.set()
    assert got == b"\x00"
    # the parent's worker and its directory lock are intact
    assert r.w.submit(lambda: 41 + 1).result(5) == 42
    with pytest.raises(Exception, match="in use"):
        StateDir(r.state.path)
    r.close()


def test_the_worker_closes_its_state_directory_so_a_next_worker_can_open_it(tmp_path, gate):
    r = Rig(tmp_path)
    path = r.state.path
    r.w.start()
    r.wait_started()
    r.w.stop()
    assert r.w.join(5)
    again = StateDir(path)  # the lock was released by join's finish
    again.close()
    r.ex.shutdown(wait=True)


def test_a_loop_that_stopped_itself_is_reported_failed_not_serving(tmp_path, gate):
    r = Rig(tmp_path)
    r.w.start()
    r.wait_started()
    assert _wait(lambda: r.w.status()["serving"])
    r.loop.error = RuntimeError("fenced")  # R2's guard records the error and stops its own threads
    r.loop.release.set()
    assert _wait(lambda: not r.w.alive())
    st = r.w.status()
    assert st["serving"] is False and st["error"] == "RuntimeError" and st["alive"] is False
    r.close()


# -- round 2 (Opus review of #532) ------------------------------------------------------------------------------------


def test_a_workerstop_on_the_start_thread_stops_the_worker_and_is_re_raised(tmp_path, gate):
    """N4-1 (head commit d06e95a4): the start thread never swallows R2's WorkerStop; it stops the worker first."""
    from orch.relay.bridge import WorkerStop
    from orch.relay.session import Fenced

    seen: list[type] = []
    old = threading.excepthook
    threading.excepthook = lambda args: seen.append(args.exc_type)
    try:

        def fenced():
            raise WorkerStop(Fenced("fenced"))

        r = Rig(tmp_path, link_state="linking", first_link=fenced)
        r.w.start()
        assert r.w.join(5) is True
    finally:
        threading.excepthook = old
    assert seen == [WorkerStop] and r.w.stopping and r.loop.started == 0
    assert r.w.error == "relay_start_failed: WorkerStop"
    r.close()


def test_join_waits_for_the_start_thread_before_releasing_anything(tmp_path, gate):
    release = threading.Event()
    r = Rig(tmp_path, link_state="linking", first_link=lambda: release.wait(10) and None)
    r.w.start()
    time.sleep(0.05)
    r.w.stop()
    assert r.w.join(0.2) is False
    assert r.rids.closed == 0 and r.ended == 0 and r.session.logouts == 0  # the start thread still runs
    release.set()
    assert _wait(lambda: r.ended == 1)  # the reaper releases once it ended: no second join needed
    assert r.rids.closed == 1 and r.session.logouts == 1 and r.w.alive() is False
    r.close()


def test_join_waits_for_the_publisher_before_releasing_anything(tmp_path, gate):
    done = threading.Event()

    class SlowPub(Pub):
        def join(self, timeout: float) -> bool:
            return done.wait(timeout)

    r = Rig(tmp_path, publisher=SlowPub())
    r.w.start()
    r.wait_started()
    r.w.stop()
    assert r.w.join(0.2) is False and r.ended == 0 and r.rids.closed == 0
    assert r.w.alive() is True  # still holding its directory and slot
    done.set()
    assert _wait(lambda: r.ended == 1) and r.w.alive() is False
    r.close()


def test_a_companion_starts_after_the_publisher_stops_with_the_worker_and_is_joined_before_the_release(tmp_path, gate):
    """B1: R6b's membership thread is a companion: started after the loop and the publisher, stopped by ``stop()`` and
    joined (bounded) before anything is released."""
    done = threading.Event()
    log: list[str] = []

    class Companion:
        def start(self) -> None:
            log.append("start")

        def stop(self) -> None:
            log.append("stop")

        def join(self, timeout: float) -> bool:
            return done.wait(timeout)

    r = Rig(tmp_path)
    r.w.companions.append(Companion())
    r.w.start()
    r.wait_started()
    assert _wait(lambda: log == ["start"])
    r.w.stop()
    assert "stop" in log
    assert r.w.join(0.2) is False and r.ended == 0 and r.w.alive() is True
    done.set()
    assert _wait(lambda: r.ended == 1)
    r.close()


def test_a_publisher_whose_start_submits_a_job_does_not_deadlock_the_start(tmp_path, gate):
    got: list[int] = []
    box: dict = {}

    class JobPub(Pub):
        def start(self) -> None:
            got.append(box["w"].job(lambda: 7))

    r = Rig(tmp_path, publisher=JobPub())
    box["w"] = r.w
    r.w.start()
    assert _wait(lambda: got == [7], timeout=5)
    r.close()


def test_the_gate_helper_opens_only_for_true(monkeypatch):
    for value, opened in ((False, False), (1, False), ("yes", False), (True, True)):
        monkeypatch.setattr(W, "R6_READY", value)
        assert W.r6_ready() is opened


def test_alarm_text_has_no_control_characters():
    a = W.Alarms(now_ms=lambda: 1)
    a("code\x1b[2J", "detail\nwith\x07bell")
    row = a.snapshot()["alarms"][0]
    assert row["code"] == "code?[2J" and row["detail"] == "detail?with?bell"


def test_check_seam_refuses_an_object_without_the_protocol_methods():
    assert isinstance(W.check_seam(W.NoPublisher(), W.Publisher, "publisher"), W.NoPublisher)
    with pytest.raises(W.SeamMissing, match="publisher"):
        W.check_seam(object(), W.Publisher, "publisher")


def test_the_history_is_checked_again_after_a_first_link_before_anything_is_served(tmp_path, gate):
    answers = iter([None, "convenience_events_present"])
    r = Rig(tmp_path, link_state="linking", precheck=lambda: next(answers))
    r.w.start()
    assert r.w.join(5) is True
    assert r.links == 1 and r.loop.started == 0 and r.w.error == "convenience_events_present"
    r.close()


# -- round 3 (Opus delta of #532) -------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(5))
def test_join_never_reports_ended_before_the_release_and_releases_once(tmp_path, gate, seed):
    """D4/D5: the reaper and several later joins race; every ``join`` that answers true does so only after the logout,
    the rid store and ``on_ended`` ran, exactly once, and ``alive()`` stays true while the release runs."""
    import random

    rnd = random.Random(seed)
    r = Rig(tmp_path, stuck=True)
    during: list[bool] = []

    def slow_logout(deadline_s: float = 5.0) -> None:
        during.append(r.w.alive())
        time.sleep(0.1)
        r.session.logouts += 1

    r.session.logout = slow_logout  # type: ignore[method-assign]
    r.w.start()
    r.wait_started()
    r.w.stop()
    assert r.w.join(0.05) is False  # the reaper starts
    r.w.REAP_PAUSE_S = 0.01
    seen: list[tuple[bool, int, int, int]] = []

    def joiner() -> None:
        time.sleep(rnd.random() * 0.05)
        ok = r.w.join(5)
        seen.append((ok, r.session.logouts, r.rids.closed, r.ended))

    threads = [threading.Thread(target=joiner) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(rnd.random() * 0.05)
    r.loop.release.set()
    for t in threads:
        t.join(10)
    assert seen and all(s == (True, 1, 1, 1) for s in seen)
    assert during == [True] and r.ended == 1 and r.w.alive() is False
    r.close()


def test_a_stop_during_publisher_start_leaves_the_publisher_stopped(tmp_path, gate):
    """D6: a stop that lands after the loop started but before the publisher's start returned must still end with
    the publisher stopped (the re-stop after ``publisher.start``)."""
    box: dict = {}

    class StopMidStart(Pub):
        def start(self) -> None:
            t = threading.Thread(target=box["w"].stop)  # the stop runs while start has not yet started anything
            t.start()
            t.join(5)
            self.calls.append("start")

    pub = StopMidStart()
    r = Rig(tmp_path, publisher=pub)
    box["w"] = r.w
    r.w.start()
    assert r.w.join(5) is True
    after_start = pub.calls[pub.calls.index("start") + 1 :]
    assert "stop" in after_start  # stopped again after its start returned
    r.close()


def test_relay_worker_start_refuses_a_truthy_non_true_gate(tmp_path, monkeypatch):
    """D19: ``RelayWorker.start`` opens only for the literal True (through ``r6_ready``)."""
    monkeypatch.setattr(W, "R6_READY", 1)
    r = Rig(tmp_path)
    with pytest.raises(W.SeamMissing, match="r6_not_ready"):
        r.w.start()
    assert r.loop.started == 0
    r.close()


def test_the_reaper_waits_between_rounds_and_releases_only_once_everything_ended(tmp_path, gate):
    """Low 1: a publisher ``join`` that answers false at once does not make the reaper spin; D2: the reaper releases
    only when every thread ended, never after one bounded round."""
    calls: list[float] = []
    done = threading.Event()

    class NoWaitPub(Pub):
        def join(self, timeout: float) -> bool:
            calls.append(timeout)
            return done.is_set()

    r = Rig(tmp_path, publisher=NoWaitPub())
    r.w.REAP_JOIN_S, r.w.REAP_PAUSE_S = 0.05, 0.05
    r.w.start()
    r.wait_started()
    r.w.stop()
    assert r.w.join(0.05) is False
    time.sleep(0.5)
    assert r.ended == 0 and r.rids.closed == 0  # many bounded rounds, still not released
    assert len(calls) < 40  # paced, not ~500k per second
    done.set()
    assert _wait(lambda: r.ended == 1)
    r.close()


def test_a_release_that_raises_never_wedges_the_slot(tmp_path, gate):
    r = Rig(tmp_path)
    r.w.start()
    r.wait_started()

    def boom() -> None:
        raise OSError("disk gone")

    r.rids.close = boom  # type: ignore[method-assign]
    r.w.stop()
    assert r.w.join(5) is True
    assert r.ended == 1 and r.w.alive() is False and "relay_release_failed" in r.alarms.codes()
    r.close()


def test_every_release_step_runs_even_when_one_raises(tmp_path, gate):
    """L4: a rid-store close that raises never keeps the state directory locked: each step on its own, alarmed."""
    r = Rig(tmp_path)
    path = r.state.path
    r.w.start()
    r.wait_started()

    def boom() -> None:
        raise OSError("disk gone")

    r.rids.close = boom  # type: ignore[method-assign]
    r.w.stop()
    assert r.w.join(5) is True
    StateDir(path).close()  # released despite the failed rid-store close
    row = next(a for a in r.alarms.snapshot()["alarms"] if a["code"] == "relay_release_failed")
    assert row["detail"] == "rids: OSError" and r.ended == 1
    r.ex.shutdown(wait=True)
