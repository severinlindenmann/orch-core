"""R3: the checkpoint publisher (11A.5) against :mod:`tests.relay.pubsim` (the relay's checkpoint routes, its check
order) over loopback, with a real linked store: the exact counter, ordinary publication, catch-up under the cap, the
relay-info maximum, crash injection at every durable boundary, the thread and first-link interfaces."""

from __future__ import annotations

import logging
import random
import threading
import time

import pytest

from orch import canon
from orch.relay import publish
from orch.relay.publish import History, PublishHeld, measure
from orch.store.outbox import Segment
from orch.wsproto import wire
from orch.wsproto.state import StateDir, StateError

from .pubkit import Kit


class Crash(BaseException):
    """A kill injected at a durable boundary (a BaseException: nothing in the publisher may swallow it)."""


@pytest.fixture
def make(tmp_path):
    kits = []

    def build(**kw):
        k = Kit(tmp_path / f"k{len(kits)}", **kw)
        kits.append(k)
        return k

    yield build
    for k in kits:
        k.close()


def calls(k, kind):
    return [d for t, d in k.sim.log if t == kind]


# -- the exact counter (wire) ----------------------------------------------------------------------------------------


def test_the_counter_equals_the_built_bytes_for_random_layouts():
    rng = random.Random(11)
    logs = ["workspace", "01J9ZP0000000000000000000S", "01J9ZP0000000000000000000T"]
    for _ in range(400):
        parts, used = [], set()
        for _ in range(rng.randint(0, 5)):
            kind, lg = rng.choice(["appended", "retained", "abandoned"]), rng.choice(logs)
            if (kind, lg) in used:
                continue
            used.add((kind, lg))
            parts.append((kind, lg, rng.randint(0, 10 ** rng.randint(0, 12)), rng.randint(0, 9)))
        layout = wire.plan_pages(parts)
        digests = [f"sha256:{rng.getrandbits(256):064x}" for _ in layout]
        total = len(wire.build_manifest(layout, digests))
        assert total == wire.manifest_bytes(layout)
        for s in layout:
            heads = [f"sha256:{rng.getrandbits(256):064x}" for _ in range(s.count)]
            body, digest = wire.build_page(s.kind, s.log, s.after_seq, f"sha256:{rng.getrandbits(256):064x}", heads)
            assert len(body) == wire.page_bytes(s.kind, s.log, s.after_seq, s.count)
            assert digest == wire.page_digest(canon.loads_strict(body))
            total += len(body)
        assert total == wire.set_bytes(layout)


def test_pages_split_at_the_page_cap_and_the_manifest_is_in_relay_order():
    layout = wire.plan_pages([("appended", "workspace", 3, wire.PAGE_HEADS + 2),
                              ("appended", "01J9ZP0000000000000000000S", 0, 1),
                              ("abandoned", "workspace", 1, 2)])  # fmt: skip
    assert [(s.i, s.kind, s.log, s.after_seq, s.count) for s in layout] == [
        (0, "abandoned", "workspace", 1, 2),
        (1, "appended", "01J9ZP0000000000000000000S", 0, 1),
        (2, "appended", "workspace", 3, wire.PAGE_HEADS),
        (3, "appended", "workspace", 3 + wire.PAGE_HEADS, 2),
    ]
    full = wire.page_bytes("appended", "workspace", 2**40, wire.PAGE_HEADS)
    assert full <= wire.PAGE_CAP
    with pytest.raises(wire.WireError):
        wire.page_bytes("appended", "workspace", 0, wire.PAGE_HEADS + 1)
    with pytest.raises(wire.WireError):  # the relay refuses a gap between the runs of one (kind, log)
        wire.plan_pages([("appended", "workspace", 0, 2), ("appended", "workspace", 3, 1)])


def test_measure_is_the_counter_over_appended_segments():
    segs = (Segment("workspace", 4, "sha256:" + "1" * 64, ("sha256:" + "2" * 64,) * 3),
            Segment("01J9ZP0000000000000000000S", 0, "sha256:" + "0" * 64, ("sha256:" + "3" * 64,)))  # fmt: skip
    parts = [("appended", "workspace", 4, 3), ("appended", "01J9ZP0000000000000000000S", 0, 1)]
    assert measure(segs) == wire.set_bytes(wire.plan_pages(parts))


# -- ordinary publication --------------------------------------------------------------------------------------------


def test_the_first_publication_reaches_the_newest_checkpoint_and_empties_the_outbox(make):
    k = make(tickets=3, events=2)
    steps = k.drain()
    assert steps[-1] == ("idle", 0.0)
    assert k.sim.held is not None and k.current()["hash"] == canon.checkpoint_hash(k.sim.held["o"])
    assert k.store.outbox_entries() == []
    lines = k.publisher.history.lines()
    assert [(line["n"], line["hash"]) for line in lines] == k.sim.accepted
    assert not k.alarms and not k.diverged


def test_each_append_publishes_only_its_new_heads(make):
    k = make(tickets=2, events=1)
    k.drain()
    before = len(k.sim.log)
    k.env.log(k.uids[0], "next")
    assert k.drain()[-1][0] == "idle"
    new = k.sim.log[before:]
    assert [t for t, _ in new] == ["manifest", "page", "manifest", "final"]  # one page: one ticket head
    assert k.held_n() == k.current()["n"]


def test_nothing_is_sent_when_the_relay_holds_the_newest_checkpoint(make):
    k = make(tickets=1)
    k.drain()
    before = len(k.sim.log)
    assert k.publisher.step() == ("idle", 0.0)
    assert k.sim.log[before:] == []


def test_a_lower_queued_entry_already_covered_gets_only_the_final_put(make, monkeypatch):
    """An entry below H (the relay holds a later checkpoint that contains it): no set, the final PUT alone is answered
    ``ignored``, and only then is the entry deleted."""
    k = make(tickets=1, events=1)
    k.drain()
    k.env.log(k.uids[0], "a")
    k.env.log(k.uids[0], "b")
    entries = k.store.outbox_entries()
    assert len(entries) == 2
    real_first = k.store.outbox_first
    monkeypatch.setattr(k.store, "outbox_first", lambda: entries[-1])  # the newer one goes first, once
    assert k.publisher.step() == ("progress", 0.0)
    monkeypatch.setattr(k.store, "outbox_first", real_first)
    before = list(k.sim.log)
    assert k.publisher.step() == ("progress", 0.0)
    assert [t for t, _ in k.sim.log[len(before) :]] == ["final"]  # ignored: no staging
    assert k.store.outbox_entries() == [] and k.held_n() == entries[-1].n


# -- size: the cap, catch-up, relay-info (P3-D37) ---------------------------------------------------------------------


@pytest.fixture
def sized(monkeypatch):
    """Records the staged bytes of every set the sim accepts."""
    import tests.relay.pubsim as ps

    sizes: list[int] = []
    orig = ps.CheckpointSim._publish

    def spy(self, s):
        before = {k: st["bytes"] for k, st in self.stages.items()}
        r = orig(self, s)
        if r.status == 200 and len(self.accepted) and before:
            gone = [b for key, b in before.items() if key not in self.stages]
            sizes.extend(gone)
        return r

    monkeypatch.setattr(ps.CheckpointSim, "_publish", spy)
    return sizes


def test_a_backlog_above_the_link_cap_is_published_by_catch_up_under_it(make, sized):
    k = make(tickets=6, events=4, link_cap=2000)
    steps = k.drain(limit=200)
    assert steps[-1] == ("idle", 0.0), k.alarms
    assert len(k.sim.accepted) >= 3 and max(sized) <= 2000
    assert k.current()["hash"] == k.sim.accepted[-1][1]
    assert not k.alarms


def test_the_relay_info_maximum_below_link_json_is_never_exceeded(make, sized):
    k = make(tickets=6, events=4, max_pub=2000, link_cap=16 << 20)
    k.drain(limit=200)
    assert k.publisher.cap() == 2000 and max(sized) <= 2000 and len(k.sim.accepted) >= 3
    assert k.current()["hash"] == k.sim.accepted[-1][1]


def test_without_relay_info_the_link_cap_applies_and_the_reader_level_is_zero(make):
    k = make(tickets=2, info=None)
    k.drain()
    assert k.publisher.cap() == k.link_cap and k.publisher.reader_level == 0


def test_a_relay_info_that_is_not_well_formed_is_misbehaving(make):
    k = make(tickets=1, info={"v": 1, "reader": "1"})
    state, wait = k.publisher.step()
    assert state == "wait" and wait >= 1 and k.alarms.codes() == ["relay_misbehaving"]
    assert calls(k, "final") == []


def test_a_set_that_crosses_the_cap_only_through_its_framing_is_split(make, sized):
    """C is the lowest entry's exact set size minus one byte: its heads alone (74 bytes each) fit, its framing does
    not; the exact counter sees it and catch-up splits the backlog."""
    k = make(tickets=2, events=1)
    entry = k.store.outbox_first()
    runs = publish._extension_runs(None, entry.checkpoint)
    exact = wire.set_bytes(publish._layout(runs))
    heads = sum(r.count for r in runs)
    assert heads >= 2 and 74 * heads < exact - 1
    p = k.make_publisher(max_publication_bytes=exact - 1)
    assert k.drain(publisher=p)[-1] == ("idle", 0.0)
    assert len(k.sim.accepted) >= 2 and max(sized) <= exact - 1
    assert k.current()["hash"] == k.sim.accepted[-1][1]


def test_an_event_whose_own_step_is_above_the_cap_is_held_and_nothing_is_sent(make):
    k = make(tickets=1, link_cap=300)
    state, _ = k.publisher.step()
    assert state == "wait" and "relay.too_large" in k.alarms.codes()
    assert calls(k, "page") == [] and calls(k, "final") == []


def test_an_unexpected_too_large_lowers_the_cap_and_replans_never_resends_unchanged(make, sized):
    k = make(tickets=20, events=4, info={"v": 1, "reader": 1, "max_publication_bytes": 16 << 20})
    k.sim.max_pub = 9000  # the relay's real maximum is lower than it announced and than link.json
    k.drain(limit=400)
    assert k.current()["hash"] == k.sim.accepted[-1][1], k.alarms
    assert max(sized) <= 9000
    assert publish.MIN_CAP <= k.publisher.cap() < 16 << 20


def test_too_large_more_than_four_times_an_hour_stops_with_relay_too_large(make):
    k = make(tickets=3, events=2)
    k.sim.max_pub = 50  # nothing ever fits
    for _ in range(40):
        state, _ = k.publisher.step()
        if "relay_too_large" in k.alarms.codes() or "relay.too_large" in k.alarms.codes():
            break
    assert {"relay_too_large", "relay.too_large"} & set(k.alarms.codes())
    assert len(k.publisher._lowerings) <= publish.LOWERINGS_PER_HOUR
    assert k.sim.accepted == []


# -- crash injection at every durable boundary -----------------------------------------------------------------------


def _crash_on(k, monkeypatch, pred):
    """Make the session's next call matching ``pred(method, target)`` raise :class:`Crash` before it is sent."""
    real = k.session.call

    def call(method, target, route, **kw):
        if pred(method, target):
            raise Crash()
        return real(method, target, route, **kw)

    monkeypatch.setattr(k.session, "call", call)
    return real


def _restart(k):
    k.publisher.stop()
    k.publisher = k.make_publisher()
    return k.publisher


def test_kill_between_staging_pages_resumes_with_only_the_missing_pages(make, monkeypatch):
    k = make(tickets=4, events=1)
    seen = {"pages": 0}
    real = k.session.call

    def call(method, target, route, **kw):
        if "/pages/" in target:
            seen["pages"] += 1
            if seen["pages"] == 3:
                raise Crash()
        return real(method, target, route, **kw)

    monkeypatch.setattr(k.session, "call", call)
    with pytest.raises(Crash):
        k.drain()
    assert k.state.read("publish.json") is not None  # the manifest was recorded before the first PUT
    staged_before = len(calls(k, "page"))
    monkeypatch.setattr(k.session, "call", real)
    p = _restart(k)
    k.drain(publisher=p)
    resent = calls(k, "page")[staged_before:]
    first_set = len(set(calls(k, "page")[:staged_before]))
    assert len(resent) >= 1
    # only pages the relay did not hold were sent again for that set
    assert len(set(resent)) == len(resent)
    assert k.store.outbox_entries() == [] and first_set == 2


def test_kill_before_the_final_put_resumes_without_restaging(make, monkeypatch):
    k = make(tickets=3, events=1)
    real = _crash_on(k, monkeypatch, lambda m, t: m == "PUT" and t.endswith("/checkpoint"))
    with pytest.raises(Crash):
        k.drain()
    mark = len(k.sim.log)
    monkeypatch.setattr(k.session, "call", real)
    k.drain(publisher=_restart(k))
    resumed = [t for t, _ in k.sim.log[mark:]]
    assert resumed[:2] == ["manifest", "final"]  # nothing restaged: the identical manifest answered complete
    assert k.store.outbox_entries() == []


def test_kill_after_the_final_put_before_the_history_repeats_nothing_but_the_bookkeeping(make, monkeypatch):
    k = make(tickets=2, events=1)

    def boom(self, **kw):
        raise Crash()

    monkeypatch.setattr(History, "append", boom)
    with pytest.raises(Crash):
        k.drain()
    assert len(k.sim.accepted) == 1
    entry = k.store.outbox_first()
    assert entry is not None and entry.hash == k.sim.accepted[0][1]  # never deleted before the history
    monkeypatch.undo()
    finals = len(calls(k, "final"))
    k.drain(publisher=_restart(k))
    assert [(line["n"], line["hash"]) for line in k.publisher.history.lines()][0] == k.sim.accepted[0]
    assert len(calls(k, "final")) - finals == len(k.sim.accepted) - 1  # the accepted one was not PUT again


def test_kill_after_the_history_before_the_outbox_delete_appends_no_second_line(make, monkeypatch):
    k = make(tickets=2, events=1)
    real_delete = k.store.outbox_delete

    def boom(n, h):
        raise Crash()

    monkeypatch.setattr(k.store, "outbox_delete", boom)
    with pytest.raises(Crash):
        k.drain()
    lines = k.publisher.history.lines()
    assert len(lines) == 1
    monkeypatch.setattr(k.store, "outbox_delete", real_delete)
    k.drain(publisher=_restart(k))
    lines = k.publisher.history.lines()
    assert len(lines) == len({(line["n"], line["hash"]) for line in lines}) == len(k.sim.accepted)


def test_a_torn_journal_is_discarded_and_the_set_restaged_identically(make, monkeypatch):
    k = make(tickets=3, events=1)
    real = _crash_on(k, monkeypatch, lambda m, t: m == "PUT" and t.endswith("/checkpoint"))
    with pytest.raises(Crash):
        k.drain()
    k.state.write("publish-manifest.json", b"{}")  # torn: the digest in publish.json no longer matches
    monkeypatch.setattr(k.session, "call", real)
    k.drain(publisher=_restart(k))
    assert k.store.outbox_entries() == [] and not k.alarms  # identical pages: free no-ops at the relay


# -- the outbox is deleted only after a verified acknowledgement -----------------------------------------------------


def test_an_acknowledgement_for_another_checkpoint_deletes_nothing(make):
    from .mock import json_reply

    k = make(tickets=1)
    k.sim.after = lambda s, r: (json_reply(200, {"result": "accepted", "n": 1, "hash": "sha256:" + "9" * 64})
                                if s.method == "PUT" and s.path.endswith("/checkpoint") else r)  # fmt: skip
    state, _ = k.publisher.step()
    assert state == "wait" and k.alarms.codes() == ["relay_misbehaving"]
    assert len(k.store.outbox_entries()) >= 1 and k.publisher.history.lines() == []


def test_an_acknowledgement_the_relay_does_not_hold_deletes_nothing(make):
    """A relay that answers ``accepted`` but serves the old H: the fresh read refutes it."""
    k = make(tickets=1)
    k.drain()
    k.env.log(k.uids[0], "x")
    old = k.sim.held

    def lie(s, r):
        if s.method == "PUT" and s.path.endswith("/checkpoint") and r.status == 200:
            k.sim.held = old  # it "forgot" right after answering
        return r

    k.sim.after = lie
    state, _ = k.publisher.step()
    assert state == "wait" and "relay_misbehaving" in k.alarms.codes()
    assert len(k.store.outbox_entries()) == 1


def test_a_lost_final_answer_is_retried_with_the_identical_body(make):
    from .mock import Reply

    k = make(tickets=1)
    bodies = []
    hits = {"n": 0}

    def drop(s, r):
        if s.method == "PUT" and s.path.endswith("/checkpoint"):
            bodies.append(s.body)
            hits["n"] += 1
            if hits["n"] == 1:
                return Reply(status=200, body=b"", fault="close")  # the relay committed; the answer is lost
        return r

    k.sim.after = drop
    k.drain()
    assert len(set(bodies[:2])) == 1  # the same bytes (exact-body idempotency): the stored receipt answers
    assert k.store.outbox_entries() == [] and len(k.sim.accepted) == len(k.publisher.history.lines())


# -- the history ----------------------------------------------------------------------------------------------------


def test_the_history_rolls_into_kept_segments(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "HISTORY_SEGMENT", 3)
    st = StateDir(tmp_path / "h")
    h = History(st)
    cp = {"o": {"n": 1}}
    for n in range(1, 9):
        h.append(n=n, hash=f"sha256:{n:064x}", kind="plain", at_ms=n, target=cp)
    h.append(n=8, hash=f"sha256:{8:064x}", kind="plain", at_ms=9, target=cp)  # a repeat after a crash
    assert [line["n"] for line in h.lines()] == list(range(1, 9))
    assert st.read("published-000001.jsonl") is not None and st.read("published-000002.jsonl") is not None
    assert h.last()["n"] == 8 and h.contains(f"sha256:{2:064x}")
    st.write("published.jsonl", b"not json\n")
    with pytest.raises(StateError):
        h.lines()
    st.close()


def test_an_accepted_restore_without_its_record_is_a_state_error(tmp_path):
    st = StateDir(tmp_path / "h")
    st.write("published.jsonl", canon.dumps({"n": 4, "hash": "sha256:" + "4" * 64, "kind": "restore",
                                              "at_ms": 1}) + b"\n")  # fmt: skip
    with pytest.raises(StateError):
        History(st).restores()
    st.close()


# -- the thread, the start fence and the first link ------------------------------------------------------------------


def test_the_thread_waits_for_the_holder_publishes_and_stops(make):
    k = make(tickets=2)
    k.session._gen = None  # noqa: SLF001 - the holder has not taken its generation yet
    p = k.make_publisher()
    p.start()
    time.sleep(0.8)
    assert k.sim.log == []  # the start fence: nothing before this holder's own host/hold
    k.session.hold()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and k.store.outbox_entries():
        time.sleep(0.05)
    assert k.store.outbox_entries() == []
    assert any(t.name == f"orch-relay-pub-{p.id8}" for t in threading.enumerate())
    p.stop()
    assert p.join(5) and p.error is None


def test_a_fence_stops_the_thread_and_tells_r5(make):
    from .sim import _err

    k = make(tickets=1)
    errors = []
    k.sim.fault = lambda s: _err(409, "fenced") if s.method == "PUT" else None
    p = k.make_publisher(on_error=errors.append)
    p.start()
    assert p.join(15)
    assert type(p.error).__name__ == "Fenced" and errors == [p.error]


def test_link_publish_is_the_first_link_hook(make):
    k = make(tickets=2, events=2, link_cap=1200)
    k.publisher.link_publish(k.session, None)
    assert k.store.outbox_entries() == [] and k.current()["hash"] == k.sim.accepted[-1][1]
    with pytest.raises(ValueError):
        k.publisher.link_publish(object(), None)


def test_link_publish_raises_when_held(make):
    k = make(tickets=1, link_cap=300)
    with pytest.raises(PublishHeld) as e:
        k.publisher.link_publish(k.session, None)
    assert e.value.code == "relay.too_large"


def test_no_head_signature_or_body_reaches_the_log(make, caplog):
    caplog.set_level(logging.DEBUG, logger="orch.relay")
    k = make(tickets=2, events=1)
    k.drain()
    text = caplog.text
    for line in k.publisher.history.lines():
        assert line["hash"] not in text
    cp = k.sim.held
    assert cp["sig"] not in text and cp["o"]["workspace_log"]["head"] not in text


def test_an_idle_thread_reads_the_relay_rarely_and_wake_publishes_at_once(make, monkeypatch):
    monkeypatch.setattr(publish, "IDLE_POLL_S", 30.0)
    k = make(tickets=1)
    p = k.make_publisher()
    parked = threading.Event()
    polls = []
    real_idle, real_job = p._idle, p._job  # noqa: SLF001 - observe the idle loop instead of timing it

    def idle():
        parked.set()  # the publisher has finished its last relay-reading step and is in its idle loop
        real_idle()

    def job(fn):
        r = real_job(fn)
        if parked.is_set():
            polls.append(1)  # one local outbox check by the idle loop
        return r

    p._idle, p._job = idle, job  # noqa: SLF001

    def relay_reads():
        return sum(1 for s in k.mock.seen if s.method == "GET" and s.path.endswith("/checkpoint"))

    p.start()
    assert parked.wait(15)
    reads = relay_reads()
    for n in range(1, 4):  # three more idle passes, each woken at once: local checks only, no relay read
        p.wake()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and len(polls) < n:
            time.sleep(0.01)
        assert len(polls) >= n
    assert relay_reads() == reads  # idle: quiet
    k.env.log(k.uids[0], "x")
    p.wake()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and k.store.outbox_entries():
        time.sleep(0.05)
    assert k.store.outbox_entries() == [] and k.held_n() == k.current()["n"]
    p.stop()
    assert p.join(5)


# -- mutation-pass additions -----------------------------------------------------------------------------------------


def test_a_resumed_page_that_no_longer_matches_its_recorded_digest_is_never_sent(make, monkeypatch):
    k = make(tickets=3, events=1)
    real = _crash_on(k, monkeypatch, lambda m, t: "/pages/" in t)
    with pytest.raises(Crash):
        k.drain()
    import json as _json

    raw = k.state.read("publish-manifest.json", limit=8 << 20)
    man = _json.loads(raw)
    for d in man["pages"]:
        d["digest"] = "sha256:" + "e" * 64  # what was recorded is not what the logs build now
    raw = canon.dumps(man)
    head = k.state.read_json("publish.json")
    head["manifest_digest"] = wire.sha256_ref(raw)
    k.state.write("publish-manifest.json", raw, limit=8 << 20)
    k.state.write_json("publish.json", head)
    monkeypatch.setattr(k.session, "call", real)
    k.sim.stages = {}  # the set expired at the relay: the recorded manifest is staged afresh
    pages = len(calls(k, "page"))
    p = _restart(k)
    assert p.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["relay_staging_rebuild"] and len(calls(k, "page")) == pages


def test_an_acknowledgement_with_the_right_n_and_another_hash_deletes_nothing(make):
    from .mock import json_reply

    k = make(tickets=1)

    def other(s, r):
        if s.method == "PUT" and s.path.endswith("/checkpoint") and r.status == 200:
            body = canon.loads_strict(r.body)
            return json_reply(200, {**body, "hash": "sha256:" + "9" * 64})
        return r

    k.sim.after = other
    assert k.publisher.step()[0] == "wait" and k.alarms.codes() == ["relay_misbehaving"]
    assert k.publisher.history.lines() == [] and k.store.outbox_entries()


def test_ignored_for_a_staged_extension_is_a_lie_even_when_the_relay_holds_it(make):
    from .mock import json_reply

    k = make(tickets=1)

    def ignored(s, r):
        if s.method == "PUT" and s.path.endswith("/checkpoint") and r.status == 200:
            return json_reply(200, {**canon.loads_strict(r.body), "result": "ignored"})
        return r

    k.sim.after = ignored
    assert k.publisher.step()[0] == "wait" and k.alarms.codes() == ["relay_misbehaving"]
    assert k.publisher.history.lines() == []


def test_the_cap_is_lowered_at_most_four_times_an_hour(make):
    k = make(tickets=1)
    p = k.publisher
    run = publish._Run("appended", "workspace", {"seq": 0, "acc": publish.ZERO},
                       {"seq": 1, "head": publish.ZERO, "acc": publish.ZERO})  # fmt: skip
    pub = publish._Pub("plain", {}, 1, "sha256:" + "1" * 64, None, runs=[run])
    for i in range(4):  # the literal design number (11A.5), not the constant
        with pytest.raises(publish._Again):
            p._too_large(pub, 10_000_000 >> i)
    with pytest.raises(PublishHeld) as e:
        p._too_large(pub, 100_000)
    assert e.value.code == "relay_too_large"
    k.clock.advance(3601)  # the window rolls
    with pytest.raises(publish._Again):
        p._too_large(pub, 100_000)
    with pytest.raises(PublishHeld) as e:  # never below the floor (review R6): held, not collapsed
        p._too_large(pub, 2 * publish.MIN_CAP - 2)
    assert e.value.code == "relay_too_large"


def test_a_target_lower_than_the_source_in_any_log_is_not_an_extension():
    def cp(ws, t):
        return {"o": {"workspace_log": ws, "tickets": t}}

    e = lambda seq, x: {"seq": seq, "head": f"sha256:{x * 64}", "acc": f"sha256:{x * 64}"}  # noqa: E731
    u = "01J9ZP0000000000000000000S"
    assert publish._extension_runs(cp(e(2, "a"), {u: e(3, "b")}), cp(e(3, "c"), {u: e(2, "d")})) is None
    assert publish._extension_runs(cp(e(2, "a"), {u: e(3, "b")}), cp(e(3, "c"), {})) is None
    assert publish._extension_runs(cp(e(2, "a"), {}), cp(e(2, "f"), {})) is None
    runs = publish._extension_runs(cp(e(2, "a"), {}), cp(e(4, "c"), {u: e(1, "d")}))
    assert [(r.log, r.count) for r in runs] == [("workspace", 2), (u, 1)]


def test_heads_from_the_store_that_do_not_fold_to_the_target_are_never_staged(make, monkeypatch):
    from orch.store.outbox import Segment as Seg

    k = make(tickets=2, events=1)
    real = k.store.relay_segment

    def bent(log, after, upto, **kw):
        s = real(log, after, upto, **kw)
        return Seg(s.log, s.after_seq, "sha256:" + "8" * 64, s.heads)  # a wrong accumulator before the page

    monkeypatch.setattr(k.store, "relay_segment", bent)
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["relay_staging_rebuild"] and calls(k, "page") == [] and calls(k, "manifest") == []


def test_the_publisher_has_r5s_seam_protocol(make):
    """R5's ``orch.relay.worker.Publisher`` (PR #532): start, stop, join(timeout) -> bool, publish_first(session,
    directory). Pinned by shape here so the two PRs plug together without importing R5's module."""
    import inspect

    k = make(tickets=1)
    p = k.publisher
    for name, params in (
        ("start", []),
        ("stop", []),
        ("join", ["timeout"]),
        ("publish_first", ["session", "directory"]),
    ):
        assert list(inspect.signature(getattr(p, name)).parameters) == params, name
    p.publish_first(k.session, None)
    assert k.store.outbox_entries() == [] and p.join(0.1)
