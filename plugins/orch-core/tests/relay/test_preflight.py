"""Round 6 (orch-52 #1): one local preflight (instance binding, ``pending``, (a), (b), a stored ``gen``) runs before ANY
network call, on every entry point: ``first_link`` (before the session-less ``PUT card``), ``Session.hold``, ``login``,
``challenge``, and every directory call. When it fails, not one request leaves, and the session stays refused."""

from __future__ import annotations

import shutil

import pytest

from orch.relay.link import first_link
from orch.relay.session import Fenced, TakeoverRefused
from orch.wsproto.state import FileHolderStore, HolderRecord, StateDir, StateError

from .helpers import GENESIS, GENESIS_CERT, GENESIS_PIN, OWNER_PK, WS, Alarms, Clock, Holder, directory, make_card
from .helpers import retrier as make_retrier
from .helpers import session as make_session
from .mock import MockRelay
from .sim import SimRelay
from .test_adversarial import Steps

IID = "0" * 32


class CopiedHolder(Holder):
    def binding_problem(self):
        return "this state directory was copied or restored (its instance record names another directory)"


def _no_gen():
    h = Holder()
    h.rec = HolderRecord(True, None, IID, None)
    return h


FAILING = {
    "copied": lambda: CopiedHolder(gen=3),
    "copied_never_held": lambda: CopiedHolder(),
    "pending": lambda: Holder(gen=3, pending=4),
    "not_last_holder": lambda: Holder(gen=3, last_holder=False),
    "other_instance": lambda: Holder(gen=3, instance_id="1" * 32),
    "no_gen": _no_gen,
}


@pytest.fixture
def env(tmp_path):
    m = MockRelay()
    m.store = StateDir(tmp_path / "relay")
    sim = SimRelay(m.origin(), owner_pk=OWNER_PK)
    sim.cards[WS] = make_card(m.origin())
    sim.holders[WS] = (3, sim.now + 40_000)  # the original's lease is live
    m.fallback = sim
    yield m, sim
    assert m.errors == []
    m.store.close()
    m.close()


def _entry_points(m, s, d, clock):
    card = make_card(m.origin())
    return {
        "first_link": lambda: first_link(Steps(card), s, d, make_retrier(clock), store=m.store),
        "hold": s.hold,
        "login": s.login,
        "challenge": s.challenge,
        "put_card_first": lambda: d.put_card(card, first=True),
        "put_card_wg": lambda: d.put_card(card, first=False),
        "get_card": d.get_card,
        "get_members": d.get_members,
        "get_checkpoint": lambda: d.get_checkpoint(genesis=GENESIS),
        "put_genesis": lambda: d.put_genesis(GENESIS_PIN, GENESIS_CERT, genesis=GENESIS),
    }


ENTRIES = [
    "first_link", "hold", "login", "challenge", "put_card_first", "put_card_wg", "get_card", "get_members",
    "get_checkpoint", "put_genesis",
]  # fmt: skip


@pytest.mark.parametrize("why", sorted(FAILING))
@pytest.mark.parametrize("entry", ENTRIES)
def test_a_failed_preflight_sends_nothing_on_any_entry_point(env, entry, why):
    m, sim = env
    clock = Clock(sim)
    alarms = Alarms()
    s = make_session(m.origin(), holder=FAILING[why](), clock=clock, alarms=alarms)
    d = directory(s)
    calls = _entry_points(m, s, d, clock)
    with pytest.raises(TakeoverRefused):
        calls[entry]()
    assert m.seen == [], entry
    assert alarms.codes() == ["relay_takeover_refused"]
    for other in ENTRIES:  # the refusal sticks: every later entry point is refused locally too (Fenced or a subclass)
        with pytest.raises(Fenced):
            calls[other]()
    assert m.seen == [] and len(alarms) == 1


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_passing_preflight_lets_each_entry_point_through(env, entry):
    """The control: the same calls on a directory that passes the preflight do reach the relay."""
    m, sim = env
    clock = Clock(sim)
    s = make_session(m.origin(), holder=Holder(gen=3), clock=clock)
    calls = _entry_points(m, s, directory(s), clock)
    try:
        calls[entry]()
    except Exception:  # noqa: BLE001 - a relay refusal is fine here; only "did it reach the relay" matters
        pass
    assert m.seen != [], entry


def test_a_copied_linking_directory_makes_no_request(env, tmp_path):
    """orch-52's case: a ``linking`` state directory copied with ``cp -r`` (instance record and ``holder.json``
    verbatim) resumes the first link: nothing is sent, the session-less ``PUT card`` included."""
    m, sim = env
    orig = m.store
    h = FileHolderStore(orig)
    h.save(HolderRecord(True, 3, h.instance_id(), None))
    copy_path = tmp_path / "copy"
    shutil.copytree(orig.path, copy_path)
    copy_path.chmod(0o700)
    with StateDir(copy_path) as copy:
        clock = Clock(sim)
        alarms = Alarms()
        s = make_session(m.origin(), holder=FileHolderStore(copy), clock=clock, alarms=alarms)
        with pytest.raises(TakeoverRefused, match="copied or restored"):
            first_link(Steps(make_card(m.origin())), s, directory(s), make_retrier(clock), store=copy)
        assert copy.read("link-card.json") is None  # not even the local card is persisted
    assert m.seen == [] and alarms.codes() == ["relay_takeover_refused"]


def test_first_link_checks_the_link_state_before_the_preflight(env):
    """An unknown ``link.json`` state is refused before anything else, with no alarm and no request."""
    from orch.relay.link import LinkRefused

    m, sim = env
    clock = Clock(sim)
    alarms = Alarms()
    s = make_session(m.origin(), holder=CopiedHolder(gen=3), clock=clock, alarms=alarms)
    steps = Steps(make_card(m.origin()))
    steps.state = "unlinked"
    with pytest.raises(LinkRefused):
        first_link(steps, s, directory(s), make_retrier(clock), store=m.store)
    assert m.seen == [] and alarms == []


def test_operator_takeover_stands_in_for_the_preflight(env):
    """After a refusal only the explicit operator takeover reaches the relay again."""
    m, sim = env
    clock = Clock(sim)
    s = make_session(m.origin(), holder=Holder(gen=3, pending=4), clock=clock)
    with pytest.raises(TakeoverRefused):
        s.login()
    assert m.seen == []
    got = s.operator_takeover()
    assert got.gen == 4 and m.seen != []
    s.login()  # the session works again


def test_hold_at_start_runs_the_preflight_again_after_an_earlier_pass(env):
    """``hold()`` at start never trusts an earlier pass: a ``pending`` record written after the session's first call
    (say by a probe that failed transiently) refuses the next ``hold()`` with no request (11A.5 cost iv)."""
    m, sim = env
    clock = Clock(sim)
    h = Holder(gen=3)
    s = make_session(m.origin(), holder=h, clock=clock)
    s.login()  # the preflight passed on this first call
    n = len(m.seen)
    h.rec = HolderRecord(True, 3, IID, 4)
    with pytest.raises(TakeoverRefused, match="pending"):
        s.hold()
    assert len(m.seen) == n


# -- round 7, R6-1: a refusal sticks for every call kind, even after an earlier pass ------------------------------


def _nothing_leaves(m, calls, s):
    """Every entry point (and logout) is refused locally after a refusal: not one more request."""
    n = len(m.seen)
    for name in ENTRIES:
        with pytest.raises(Fenced):
            calls[name]()
        assert len(m.seen) == n, name
    s.logout()
    assert len(m.seen) == n, "logout"


def test_a_refusal_after_an_earlier_pass_sticks_on_every_entry_point(env):
    """E1: ``login()`` passes the preflight; then a ``pending`` record (a probe that failed transiently) makes
    ``hold()`` refuse. From then on login, challenge, every directory call, the session-less ``PUT card`` and the
    logout send nothing."""
    m, sim = env
    clock = Clock(sim)
    alarms = Alarms()
    h = Holder(gen=3)
    s = make_session(m.origin(), holder=h, clock=clock, alarms=alarms)
    d = directory(s)
    s.login()
    assert s.logged_in and m.seen != []
    h.rec = HolderRecord(True, 3, IID, 4)
    with pytest.raises(TakeoverRefused, match="pending"):
        s.hold()
    h.rec = HolderRecord(True, 3, IID, None)  # even a holder.json that would pass now: the refusal sticks
    _nothing_leaves(m, _entry_points(m, s, d, clock), s)
    assert alarms.codes() == ["relay_takeover_refused"]


def test_a_probe_gen_mismatch_refuses_the_same_session_for_good(env):
    """E2: ``hold()`` is the first call and passes the preflight; the renewal is fenced and the probe is granted gen 6,
    not stored+1 = 4 (another holder, or a restored state). The same session then sends nothing at all."""
    m, sim = env
    sim.holders[WS] = (5, sim.now - 1)  # someone else held 4 and 5 meanwhile; the lease lapsed
    clock = Clock(sim)
    alarms = Alarms()
    s = make_session(m.origin(), holder=Holder(gen=3), clock=clock, alarms=alarms)
    d = directory(s)
    with pytest.raises(TakeoverRefused, match="relay gen 6 != stored"):
        s.hold()
    _nothing_leaves(m, _entry_points(m, s, d, clock), s)
    assert alarms.codes() == ["relay_takeover_refused"]


def test_logout_after_a_refusal_sends_nothing_and_forgets_the_token(env):
    """E6: the logout bypasses ``call()``; after a refusal it sends nothing (the token is still forgotten)."""
    m, sim = env
    clock = Clock(sim)
    h = Holder(gen=3)
    s = make_session(m.origin(), holder=h, clock=clock)
    s.login()
    h.rec = HolderRecord(True, 3, IID, 4)
    with pytest.raises(TakeoverRefused):
        s.hold()
    n = len(m.seen)
    s.logout()
    assert len(m.seen) == n and not s.logged_in


def test_only_the_operator_takeover_clears_a_refusal_after_a_pass(env):
    m, sim = env
    clock = Clock(sim)
    h = Holder(gen=3)
    s = make_session(m.origin(), holder=h, clock=clock)
    s.login()
    h.rec = HolderRecord(True, 3, IID, 4)
    with pytest.raises(TakeoverRefused):
        s.hold()
    with pytest.raises(TakeoverRefused):
        s.preflight()
    assert s.operator_takeover().gen == 4
    n = len(m.seen)
    directory(s).get_card()
    assert len(m.seen) > n


# -- round 7, R6-3: a linked directory without holder.json / instance.json is not a first link ---------------------


def _linked_dir(tmp_path, name, state="linked"):
    """A state directory that held gen 3 and whose ``link.json`` says ``state``; returns its path (closed)."""
    path = tmp_path / name
    with StateDir(path) as st:
        h = FileHolderStore(st)
        h.save(HolderRecord(True, 3, h.instance_id(), None))
        st.write_json("link.json", {"v": 1, "state": state})
    return path


def _restored(tmp_path, missing):
    """``missing``: "holder" deletes holder.json in place (a selective restore); "both" restores a copy without
    holder.json and instance.json (``rsync --exclude``)."""
    import os

    src = _linked_dir(tmp_path, "relay-src")
    if missing == "holder":
        os.unlink(src / "holder.json")
        return src
    dst = tmp_path / "restored"
    shutil.copytree(src, dst)
    dst.chmod(0o700)
    os.unlink(dst / "holder.json")
    os.unlink(dst / "instance.json")
    return dst


@pytest.mark.parametrize("missing", ["holder", "both"])
@pytest.mark.parametrize("entry", ENTRIES)
def test_a_linked_directory_missing_its_holder_files_sends_nothing(env, tmp_path, entry, missing):
    """E3: ``link.json`` says ``linked`` but ``holder.json`` (and ``instance.json``) are gone: the preflight refuses on
    every entry point, names the operator path, and nothing is sent."""
    m, sim = env
    with StateDir(_restored(tmp_path, missing)) as st:
        clock = Clock(sim)
        alarms = Alarms()
        s = make_session(m.origin(), holder=FileHolderStore(st), clock=clock, alarms=alarms)
        d = directory(s)
        calls = _entry_points(m, s, d, clock)
        calls["first_link"] = lambda: first_link(
            Steps(make_card(m.origin())), s, d, make_retrier(clock), store=st
        )  # the link state is the store's too: the first link resumes "linking", so the preflight decides
        with pytest.raises(TakeoverRefused, match="link.json says state 'linked'"):
            calls[entry]()
        assert "orch host relay takeover" in alarms[0][1]
        assert ("instance.json" in alarms[0][1]) == (missing == "both")
        for other in ENTRIES:
            with pytest.raises(Fenced):
                calls[other]()
        assert m.seen == [] and alarms.codes() == ["relay_takeover_refused"]


def test_the_operator_takeover_recovers_a_linked_directory_missing_its_holder_files(env, tmp_path):
    m, sim = env
    with StateDir(_restored(tmp_path, "both")) as st:
        clock = Clock(sim)
        h = FileHolderStore(st)
        s = make_session(m.origin(), holder=h, clock=clock)
        with pytest.raises(TakeoverRefused):
            s.login()
        got = s.operator_takeover()
        assert got.gen == 4 and h.load() == HolderRecord(True, 4, h.instance_id(), None)
        s.login()
    with StateDir(st.path) as again:  # a restart after the recovery passes the preflight
        s2 = make_session(m.origin(), holder=FileHolderStore(again), clock=Clock(sim))
        assert s2.preflight() is not None


@pytest.mark.parametrize("state", ["linking", None])
def test_a_true_first_link_without_holder_files_still_proceeds(env, tmp_path, state):
    """The control: ``linking`` (or no ``link.json`` yet) with neither ``holder.json`` nor ``instance.json`` is a true
    first link: the preflight passes and the request leaves."""
    m, sim = env
    path = tmp_path / "fresh"
    with StateDir(path) as st:
        if state is not None:
            st.write_json("link.json", {"v": 1, "state": state})
        s = make_session(m.origin(), holder=FileHolderStore(st), clock=Clock(sim))
        assert s.preflight() is None
        s.login()
    assert m.seen != []


@pytest.mark.parametrize("state", ["unlinked", 7, "x" * 100])
def test_any_link_state_other_than_linking_counts_as_past_link(env, tmp_path, state):
    m, sim = env
    with StateDir(tmp_path / "odd") as st:
        st.write_json("link.json", {"v": 1, "state": state})
        s = make_session(m.origin(), holder=FileHolderStore(st), clock=Clock(sim))
        with pytest.raises(TakeoverRefused, match="restored or copied linked"):
            s.login()
    assert m.seen == []


@pytest.mark.parametrize("doc", [[], "linked", "linking", 3, [{"state": "linking"}]])
def test_a_link_json_that_is_not_an_object_counts_as_past_link(env, tmp_path, doc):
    """Round 8 (M8): a ``link.json`` that is a list, a string or a number is never ``linking``: refused, none sent."""
    m, sim = env
    with StateDir(tmp_path / "odd") as st:
        st.write_json("link.json", doc)
        s = make_session(m.origin(), holder=FileHolderStore(st), clock=Clock(sim))
        with pytest.raises(TakeoverRefused, match="restored or copied linked"):
            s.login()
    assert m.seen == []


# -- round 8, R7-1: a state file that says JSON null is unreadable, never "absent" --------------------------------


def _null_dir(tmp_path, which):
    path = tmp_path / f"null-{which}"
    with StateDir(path) as st:
        if which == "instance":
            st.write("instance.json", b"null")  # before FileHolderStore claims it
        FileHolderStore(st)
        if which == "holder":
            st.write_json("link.json", {"v": 1, "state": "linking"})
            st.write("holder.json", b"null")
        elif which == "link":
            st.write("link.json", b"null")
    return path


@pytest.mark.parametrize("which", ["holder", "link", "instance"])
@pytest.mark.parametrize("entry", ENTRIES)
def test_a_null_state_file_is_unreadable_not_absent(env, tmp_path, which, entry):
    """``holder.json`` = ``null`` (with ``link.json`` ``linking``), ``link.json`` = ``null`` or ``instance.json`` =
    ``null``: the preflight raises StateError on every entry point, every time, and nothing is sent."""
    m, sim = env
    with StateDir(_null_dir(tmp_path, which)) as st:
        clock = Clock(sim)
        s = make_session(m.origin(), holder=FileHolderStore(st), clock=clock)
        d = directory(s)
        calls = _entry_points(m, s, d, clock)
        with pytest.raises(StateError, match="null"):
            calls[entry]()
        for other in ENTRIES:
            with pytest.raises(StateError, match="null"):
                calls[other]()
    assert m.seen == []


# -- round 8, R7-2: an unreadable state after an earlier pass stops every later call -------------------------------


@pytest.mark.parametrize("bad", [b"null", b"{not json", b"[]"])
def test_an_unreadable_holder_after_an_earlier_pass_stops_every_later_call(env, tmp_path, bad):
    """``login()`` passes; then ``holder.json`` becomes unreadable: ``hold()`` raises StateError, and so does every
    later entry point, with no request. Not sticky: once the file reads again, calls go out."""
    m, sim = env
    with StateDir(_linked_dir(tmp_path, "held")) as st:
        clock = Clock(sim)
        s = make_session(m.origin(), holder=FileHolderStore(st), clock=clock)
        d = directory(s)
        s.login()
        good = st.read("holder.json")
        st.write("holder.json", bad)
        n = len(m.seen)
        with pytest.raises(StateError):
            s.hold()
        calls = _entry_points(m, s, d, clock)
        for name in ENTRIES:
            with pytest.raises(StateError):
                calls[name]()
            assert len(m.seen) == n, name
        st.write("holder.json", good)
        d.get_card()
        assert len(m.seen) > n


@pytest.mark.parametrize("bad", [b"null", b"{not json", b"[]", b'{"instance_id": "' + b"0" * 32 + b'"}'])
def test_f2_an_unreadable_instance_record_after_an_earlier_pass_stops_every_later_call(env, tmp_path, bad):
    """R1 carry-over F2 (R2): ``login()`` passes; then ``instance.json`` becomes unreadable (or loses its directory
    binding): ``hold()`` raises StateError and so does every later entry point, with no request sent."""
    m, sim = env
    with StateDir(_linked_dir(tmp_path, "held-f2")) as st:
        clock = Clock(sim)
        s = make_session(m.origin(), holder=FileHolderStore(st), clock=clock)
        d = directory(s)
        s.login()
        st.write("instance.json", bad)
        n = len(m.seen)
        with pytest.raises(StateError):
            s.hold()
        calls = _entry_points(m, s, d, clock)
        for name in ENTRIES:
            with pytest.raises(StateError):
                calls[name]()
            assert len(m.seen) == n, name


def test_one_session_per_state_directory_a_second_state_dir_is_refused(tmp_path):
    """R1 carry-over: one live instance per state directory (11A.3), so R5 can only ever build one Session over it."""
    with StateDir(tmp_path / "relay-one"):
        with pytest.raises(StateError):
            StateDir(tmp_path / "relay-one")


# -- round 9: preflight is serialised within one Session --------------------------------------------------------------


def test_a_concurrent_passing_preflight_cannot_mask_another_threads_state_error(env, tmp_path):
    """orch-52 F1, forced with events: thread C's first call reads a good ``holder.json`` inside the preflight and
    pauses; ``holder.json`` becomes ``null``; thread A's ``hold()`` runs its preflight. Without the lock A raised
    StateError first and C then set the pass, so a later ``get_card()`` sent a request while the file was still
    ``null``. With it A waits for C, raises StateError after C's pass, and every later call refuses locally."""
    import threading

    m, sim = env
    loaded, go = threading.Event(), threading.Event()

    class Pausing(FileHolderStore):
        def load(self):
            rec = super().load()
            if threading.current_thread().name == "C" and not loaded.is_set():
                loaded.set()
                assert go.wait(10)
            return rec

    with StateDir(_linked_dir(tmp_path, "held")) as st:
        clock = Clock(sim)
        s = make_session(m.origin(), holder=Pausing(st), clock=clock)
        d = directory(s)
        out: dict = {}

        def run(name, fn):
            try:
                fn()
                out[name] = None
            except Exception as e:  # noqa: BLE001 - recorded for the asserts
                out[name] = e

        c = threading.Thread(target=run, args=("C", d.get_card), name="C", daemon=True)
        c.start()
        assert loaded.wait(10)
        st.write("holder.json", b"null")
        a = threading.Thread(target=run, args=("A", s.hold), name="A", daemon=True)
        a.start()
        a.join(0.5)  # with the lock A is blocked here; without it A has already failed
        go.set()
        c.join(10)
        a.join(10)
        assert not c.is_alive() and not a.is_alive()
        assert isinstance(out["A"], StateError), out
        n = len(m.seen)
        for _ in range(2):
            with pytest.raises(StateError, match="null"):
                d.get_card()
        assert len(m.seen) == n


# -- round 10 (R9-3): the alarm is raised after the preflight lock is released; the lock is reentrant ----------------


@pytest.fixture
def hang_guard():
    """Fail (never hang) when the test body blocks for 10 s: a deadlocked lock acquire is interrupted by SIGALRM."""
    import signal

    def boom(*_):
        raise RuntimeError("hang: the preflight lock deadlocked")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(10)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


def test_an_alarm_sink_may_wait_on_another_thread_using_the_same_session(env, hang_guard):
    """The sink hands a call on this Session to another thread and joins it: the preflight lock is no longer held
    while the alarm is raised, so that thread is refused locally at once (nothing sent) instead of blocking."""
    import threading

    m, sim = env
    out: list = []
    s = None

    def sink(code, detail):
        def other():
            try:
                directory(s).get_card()
            except Exception as e:  # noqa: BLE001 - recorded for the asserts
                out.append(e)

        t = threading.Thread(target=other, daemon=True)
        t.start()
        t.join(5)
        out.append(("joined", not t.is_alive(), code))

    s = make_session(m.origin(), holder=FAILING["copied"](), clock=Clock(sim), alarms=sink)
    with pytest.raises(TakeoverRefused):
        s.preflight()
    assert isinstance(out[0], TakeoverRefused) and out[1] == ("joined", True, "relay_takeover_refused"), out
    assert m.seen == []


def test_the_preflight_lock_is_reentrant_for_the_same_thread(env, hang_guard):
    """A same-thread re-entry while the lock is held (a holder store, or a signal handler, calling back into this
    Session) runs instead of deadlocking; an alarm sink re-entering after a refusal is refused locally."""
    m, sim = env
    s = None
    entered: list = []

    class Reentering(Holder):
        def load(self):
            if not entered:
                entered.append(True)
                entered.append(s.preflight())  # re-enters preflight under the lock, same thread
            return super().load()

    s = make_session(m.origin(), holder=Reentering(gen=3), clock=Clock(sim))
    assert s.preflight().gen == 3 and entered[1].gen == 3
    seen: list = []

    def sink(code, detail):
        try:
            r.preflight()
        except TakeoverRefused as e:
            seen.append(e)

    r = make_session(m.origin(), holder=FAILING["pending"](), clock=Clock(sim), alarms=sink)
    with pytest.raises(TakeoverRefused):
        r.preflight()
    assert len(seen) == 1 and m.seen == []
