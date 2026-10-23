"""R3 review round 1 (Opus, 15 Oct 2026) regressions: the reviewer's reproductions (R1 to R4, adopted), the lower
findings (R5 to R10), and the tests its surviving mutants asked for (the fresh-read confirmation, the lowering limit,
a restore's too_large, LINE_CAP, publication_complete, the H7 boundary, the answer shapes, R3's own fork hold)."""

from __future__ import annotations

import json
import threading
import time

import pytest

from orch import canon
from orch.relay import publish
from orch.relay.publish import History, PublishHeld
from orch.store.checkpoints import sign_object
from orch.store.errors import StoreError
from orch.wsproto.state import StateDir

from .mock import json_reply
from .pubkit import Kit
from .sim import _err
from .test_publish_restore import _restored


class Crash(BaseException):
    pass


@pytest.fixture
def kit(tmp_path):
    k = Kit(tmp_path, tickets=2, events=1)
    yield k
    k.close()


def _final(s):
    return s.method == "PUT" and s.path.endswith("/checkpoint")


# -- R1: a restore the relay already holds is finished, never held -------------------------------------------------


def test_r1_restore_ack_lost_then_resume(kit, tmp_path):
    k = kit
    _, entry = _restored(k, tmp_path)
    k.sim.after = lambda s, r: json_reply(503, {"error": "overloaded"}) if _final(s) else r
    k.publisher.step()
    assert (entry.n, entry.hash) in k.sim.accepted  # the relay holds the restore target
    k.sim.after = None
    out = k.drain(10)
    assert k.store.outbox_entries() == [], (out, k.alarms.codes())
    assert "restore_source_mismatch" not in k.alarms.codes()
    assert [r["n"] for r in k.publisher.history.restores()] == [entry.n]


def test_r1_restore_crash_after_the_final_put_before_the_history(kit, tmp_path, monkeypatch):
    k = kit
    _, entry = _restored(k, tmp_path)

    def boom(self, **kw):
        raise Crash()

    monkeypatch.setattr(History, "append", boom)
    with pytest.raises(Crash):
        k.drain()
    monkeypatch.undo()
    finals = sum(1 for s in k.mock.seen if _final(s))
    k.publisher = k.make_publisher()
    k.drain(10)
    assert k.store.outbox_entries() == [] and [r["n"] for r in k.publisher.history.restores()] == [entry.n]
    assert sum(1 for s in k.mock.seen if _final(s)) == finals  # nothing re-sent: the relay holds it


def test_r1_restore_crash_between_history_and_delete(kit, tmp_path, monkeypatch):
    k = kit
    _, entry = _restored(k, tmp_path)
    real = k.store.outbox_delete

    def boom(n, h):
        raise Crash()

    monkeypatch.setattr(k.store, "outbox_delete", boom)
    with pytest.raises(Crash):
        k.drain()
    monkeypatch.setattr(k.store, "outbox_delete", real)
    k.publisher = k.make_publisher()
    out = k.drain(10)
    assert k.store.outbox_entries() == [], (out, k.alarms.codes())
    lines = [line for line in k.publisher.history.lines() if line["n"] == entry.n]
    assert len(lines) == 1


def test_r1_a_restore_the_relay_has_moved_past_is_finished(kit, tmp_path, monkeypatch):
    """The relay holds a later checkpoint descending from the restore (its entry's delete was lost and a later entry
    went first): finished from the fresh read, not held."""
    k = kit
    _, entry = _restored(k, tmp_path)
    k.drain()
    k.env.log(k.uids[1], "after")
    k.drain()
    # put the restore entry back, as if its delete had been lost
    k.store._outbox.write(entry)  # noqa: SLF001 - test only: re-create the lost entry
    assert k.store.outbox_first().n == entry.n
    k.drain(10)
    assert k.store.outbox_entries() == [] and "restore_source_mismatch" not in k.alarms.codes()


def test_r1_a_restore_below_h_on_another_branch_still_holds(kit, tmp_path):
    k = kit
    _, entry = _restored(k, tmp_path)
    k.drain()
    o = json.loads(json.dumps(k.sim.held["o"]))
    o["n"] = entry.n + 1
    o["tickets"][k.uids[1]]["head"] = "sha256:" + "5" * 64
    forked = sign_object(k.env.signer.sign, o)
    k.store._outbox.write(entry)  # noqa: SLF001
    k.sim.held = forked
    k.sim.checkpoints[canon.checkpoint_hash(o)] = forked
    k.publisher.classify = lambda H, pos: None  # the planner's own guard, without the classification in front
    state, _ = k.publisher.step()
    assert state == "wait" and k.store.outbox_first().n == entry.n


# -- R2: the first link renews the lease -----------------------------------------------------------------------------


def test_r2_publish_first_renews_between_requests_without_a_thread(kit, monkeypatch):
    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.0)
    p = k.make_publisher(conn=None)

    def age(s):  # every page or manifest PUT takes 25 s of relay time: two of them outlive a 40 s lease
        if s.method == "PUT" and "/staging/" in s.path:
            k.sim.now += 25_000
        return None

    k.sim.fault = age
    p.publish_first(k.session, None)
    assert k.store.outbox_entries() == [] and not k.session.fenced


def test_r2_publish_first_renews_during_a_long_call(kit, monkeypatch):
    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.05)
    holds = []
    real_hold = k.sim._hold

    def hold(s, ws):
        holds.append(k.sim.now)
        return real_hold(s, ws)

    monkeypatch.setattr(k.sim, "_hold", hold)

    real_call = k.session.call
    state = {"done": False}

    def slow(method, target, route, **kw):  # one page PUT spends 50 s of relay time in flight (a slow uplink)
        if "/pages/" in target and not state["done"]:
            state["done"] = True
            for _ in range(5):
                k.sim.advance(10)
                time.sleep(0.2)  # the renewal thread runs meanwhile, on its own connection
        return real_call(method, target, route, **kw)

    monkeypatch.setattr(k.session, "call", slow)
    k.publisher.publish_first(k.session, None)
    assert k.store.outbox_entries() == [] and not k.session.fenced and len(holds) >= 3


def test_r2_without_renewal_the_same_publication_is_fenced(kit, monkeypatch):
    """The control: a running publisher thread (no first-link renewal; the aux thread renews in R5) is fenced."""
    k = kit

    def age(s):
        if s.method == "PUT" and "/staging/" in s.path:
            k.sim.now += 25_000
        return None

    k.sim.fault = age
    from orch.relay.session import Fenced

    with pytest.raises(Fenced):
        for _ in range(10):
            k.publisher.step()


# -- R3: a store fault in the idle check never stops the publisher ---------------------------------------------------


def test_r3_idle_store_busy_and_torn_keep_the_publisher(kit, monkeypatch):
    monkeypatch.setattr(publish, "IDLE_POLL_S", 0.05)
    k = kit
    errors = []
    p = k.make_publisher(on_error=errors.append)
    p.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and k.store.outbox_entries():
        time.sleep(0.05)
    real = k.store.outbox_first
    for code in ("store.busy", "store.torn_write"):

        def fail(code=code):
            raise StoreError(code, "test")

        monkeypatch.setattr(k.store, "outbox_first", fail)
        time.sleep(0.4)
        if code == "store.torn_write":  # under load the idle period may begin later: wait for the alarm itself
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and "relay_outbox_torn" not in k.alarms.codes():
                time.sleep(0.05)
        monkeypatch.setattr(k.store, "outbox_first", real)
        assert p._thread.is_alive() and p.error is None and errors == []  # noqa: SLF001
    assert "relay_outbox_torn" in k.alarms.codes()
    p.stop()
    assert p.join(5)


# -- R4: a stale H read is not a hot loop ----------------------------------------------------------------------------


def test_r4_stale_h_read_backs_off(kit):
    k = kit
    k.drain()
    stale = k.sim.held
    k.env.log(k.uids[0], "x")
    k.drain()
    k.env.log(k.uids[0], "y")
    real_get = k.sim._get

    def get(s):
        held = k.sim.held
        k.sim.held = stale
        try:
            return real_get(s)
        finally:
            k.sim.held = held

    k.sim._get = get
    out = [k.publisher.step() for _ in range(6)]
    finals = sum(1 for s in k.mock.seen if _final(s) and s.body)
    assert any(state == "wait" and wait >= 1 for state, wait in out), out
    assert "relay_misbehaving" in k.alarms.codes() and finals < 20


# -- lower findings --------------------------------------------------------------------------------------------------


def test_r6_too_large_on_a_set_less_final_put_holds_and_keeps_c(kit, monkeypatch):
    k = kit
    k.drain()
    k.env.log(k.uids[0], "a")
    k.env.log(k.uids[0], "b")
    entries = k.store.outbox_entries()
    real_first = k.store.outbox_first
    monkeypatch.setattr(k.store, "outbox_first", lambda: entries[-1])
    k.publisher.step()
    monkeypatch.setattr(k.store, "outbox_first", real_first)
    k.sim.fault = lambda s: _err(400, "too_large") if _final(s) else None
    cap = k.publisher.cap()
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["relay.too_large"] and k.publisher.cap() == cap


def test_r7_a_final_body_above_the_cap_is_held_before_staging(kit, monkeypatch):
    k = kit
    monkeypatch.setattr(publish, "FINAL_CAP", 500)
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["relay.too_large"] and not [s for s in k.mock.seen if "/staging/" in s.path]


def test_r8_a_permanent_refusal_is_held_not_retried_at_once(kit):
    k = kit
    k.sim.fault = lambda s: _err(403, "not_eligible") if _final(s) else None
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["relay_publish_refused"]


def test_r8_a_413_without_a_code_is_too_large(kit):
    from .mock import Reply

    k = kit
    k.sim.fault = lambda s: Reply(status=413, body=b"") if "/pages/" in s.path else None
    k.publisher.step()
    assert k.alarms.codes() == ["relay_too_large"]  # the too_large path (a small set: held at the floor), not a refusal


def test_r9_a_store_chain_refusal_is_not_alarmed_as_a_relay_fork(kit, monkeypatch):
    k = kit

    def no_path(*a, **kw):
        raise StoreError("chain.diverged", "no verified path")

    monkeypatch.setattr(k.store, "relay_segment", no_path)
    k.publisher.step()
    assert k.alarms.codes() == ["relay_store_chain_diverged"] and k.diverged == []


def test_r10_a_crash_during_rollover_does_not_duplicate_the_segment(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "HISTORY_SEGMENT", 3)
    st = StateDir(tmp_path / "h")
    h = History(st)
    cp = {"o": {"n": 1}}
    for n in range(1, 4):
        h.append(n=n, hash=f"sha256:{n:064x}", kind="plain", at_ms=n, target=cp)
    real_write = st.write
    calls = {"n": 0}

    def write(name, data, **kw):
        if name == "published.jsonl" and calls["n"] == 0 and st.read("published-000001.jsonl") is not None:
            calls["n"] += 1
            raise Crash()
        return real_write(name, data, **kw)

    monkeypatch.setattr(st, "write", write)
    with pytest.raises(Crash):
        h.append(n=4, hash=f"sha256:{4:064x}", kind="plain", at_ms=4, target=cp)
    monkeypatch.setattr(st, "write", real_write)
    h2 = History(st)
    h2.append(n=4, hash=f"sha256:{4:064x}", kind="plain", at_ms=4, target=cp)
    assert [line["n"] for line in h2.lines()] == [1, 2, 3, 4]
    assert st.read("published-000002.jsonl") is None
    st.close()


def test_r5_restores_and_segments_are_read_once_per_process(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "HISTORY_SEGMENT", 2)
    st = StateDir(tmp_path / "h")
    h = History(st)
    for n in range(1, 8):
        h.append(n=n, hash=f"sha256:{n:064x}", kind="plain", at_ms=n, target={"o": {"n": n}})
    h.restores()
    reads = []
    real = st.read
    monkeypatch.setattr(st, "read", lambda name, **kw: reads.append(name) or real(name, **kw))
    for _ in range(5):
        h.restores()
        h.last()
    assert not any(name.startswith("published-0") for name in reads)
    st.close()


# -- the reviewer's surviving mutants --------------------------------------------------------------------------------


def _after_final(k, fn):
    def after(s, r):
        if _final(s) and r.status == 200:
            fn()
        return r

    k.sim.after = after


def test_confirm_accepted_needs_the_very_checkpoint_not_a_higher_one(kit):
    k = kit
    k.drain()
    k.env.log(k.uids[0], "x")

    def higher():
        o = json.loads(json.dumps(k.sim.held["o"]))
        o["n"] += 5
        k.sim.held = sign_object(k.env.signer.sign, o)

    _after_final(k, higher)
    assert k.publisher.step()[0] == "wait" and k.alarms.codes() == ["relay_misbehaving"]
    assert len(k.store.outbox_entries()) == 1


def test_confirm_accepted_then_an_empty_relay_deletes_nothing(kit):
    k = kit

    def empty():
        k.sim.held = None

    _after_final(k, empty)
    assert k.publisher.step()[0] == "wait" and k.alarms.codes() == ["relay_misbehaving"]
    assert k.store.outbox_entries() and k.publisher.history.lines() == []


def test_confirm_ignored_then_a_lower_h_deletes_nothing(kit, monkeypatch):
    k = kit
    k.drain()
    first = k.sim.held
    k.env.log(k.uids[0], "a")
    k.env.log(k.uids[0], "b")
    entries = k.store.outbox_entries()
    real_first = k.store.outbox_first
    monkeypatch.setattr(k.store, "outbox_first", lambda: entries[-1])
    k.publisher.step()
    monkeypatch.setattr(k.store, "outbox_first", real_first)

    def lower():
        k.sim.held = first

    _after_final(k, lower)
    before = [e.n for e in k.store.outbox_entries()]
    assert k.publisher.step()[0] == "wait" and k.alarms.codes() == ["relay_misbehaving"]
    assert [e.n for e in k.store.outbox_entries()] == before


def test_a_restore_too_large_at_the_relay_is_held_not_lowered(kit, tmp_path):
    k = kit
    _restored(k, tmp_path)
    k.sim.fault = lambda s: _err(413, "too_large") if "/pages/" in s.path else None
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["relay.too_large"] and k.publisher.cap() == k.link_cap


def test_a_restore_line_above_line_cap_is_held(kit, tmp_path):
    k = kit
    _, entry = _restored(k, tmp_path)
    r = entry.restore
    line = {**r["event"], "pad": "x" * publish.LINE_CAP}
    with pytest.raises(PublishHeld) as e:
        k.publisher._restore_pub(None, entry.checkpoint, line, r["log"], r, [], 1 << 30, entry=entry,
                                 acc_from=r["acc_from"])  # fmt: skip
    assert e.value.code == "relay.too_large"


def test_a_restore_without_complete_evidence_is_held(kit, tmp_path, monkeypatch):
    k = kit
    _, entry = _restored(k, tmp_path)
    from dataclasses import replace

    bad = replace(entry, restore={**entry.restore, "publication_complete": False})
    monkeypatch.setattr(k.store, "outbox_first", lambda: bad)
    before = sum(1 for s in k.mock.seen if "/staging/" in s.path)
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["relay_restore_incomplete"]
    assert sum(1 for s in k.mock.seen if "/staging/" in s.path) == before


def test_h7_holds_one_above_the_numbering(kit):
    k = kit
    k.drain()
    pos = k.store.relay_position(None)
    o = json.loads(json.dumps(k.sim.held["o"]))
    o["n"] = pos.numbering + 1
    k.sim.held = sign_object(k.env.signer.sign, o)
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["relay_host_state_lost"]


@pytest.mark.parametrize(
    "answer",
    [{"complete": False, "missing": [0, 0]}, {"complete": True, "missing": [0]}, {"complete": False, "missing": []}],
)
def test_manifest_answer_shapes(kit, answer):
    """Answered once: a malformed answer is refused at once, before any page is sent on its strength."""
    k = kit
    once = {"n": 0}

    def fault(s):
        if s.path.endswith("/manifest") and not once["n"]:
            once["n"] = 1
            return json_reply(200, answer)
        return None

    k.sim.fault = fault
    assert k.publisher.step()[0] == "wait" and k.alarms.codes() == ["relay_misbehaving"]
    assert not [s for s in k.mock.seen if "/pages/" in s.path]


def test_r4_the_mismatch_count_restarts_after_a_settled_step(kit):
    k = kit
    k.drain()
    hits = {"n": 0}

    def once_each(s):
        if _final(s) and hits["n"] < 1:
            hits["n"] += 1
            return _err(409, "restore_source_mismatch")
        return None

    for text in ("a", "b"):
        hits["n"] = 0
        k.sim.fault = once_each
        k.env.log(k.uids[0], text)
        k.drain(10)
    assert "relay_misbehaving" not in k.alarms.codes() and k.store.outbox_entries() == []


def test_r2_the_renewal_thread_survives_a_transient_failure(kit, monkeypatch):
    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.05)
    monkeypatch.setattr(publish, "RENEW_RETRY_S", 0.02)
    holds = {"n": 0}
    real_hold = k.sim._hold

    def hold(s, ws):
        holds["n"] += 1
        if holds["n"] == 1:
            return _err(503, "overloaded")
        return real_hold(s, ws)

    monkeypatch.setattr(k.sim, "_hold", hold)
    real_call = k.session.call
    state = {"done": False}

    def slow(method, target, route, **kw):
        if "/pages/" in target and not state["done"]:
            state["done"] = True
            for _ in range(5):
                k.sim.advance(10)
                time.sleep(0.2)
        return real_call(method, target, route, **kw)

    monkeypatch.setattr(k.session, "call", slow)
    k.publisher.publish_first(k.session, None)
    assert holds["n"] >= 3 and not k.session.fenced and k.store.outbox_entries() == []


def test_final_answer_field_set(kit):
    k = kit

    def extra(s, r):
        if _final(s) and r.status == 200:
            return json_reply(200, {**canon.loads_strict(r.body), "extra": 1})
        return r

    k.sim.after = extra
    assert k.publisher.step()[0] == "wait" and k.alarms.codes() == ["relay_misbehaving"]
    assert k.publisher.history.lines() == []


def test_r3s_own_classification_holds_a_fork(kit):
    """R3's own hold, without the store's refusal underneath (review R9 mutant 19)."""
    k = kit
    k.drain()
    o = json.loads(json.dumps(k.sim.held["o"]))
    o["tickets"][k.uids[1]]["head"] = "sha256:" + "5" * 64
    k.sim.held = sign_object(k.env.signer.sign, o)
    H = k.publisher._reader().get_checkpoint(genesis=k.store.genesis)
    pos = k.store.relay_position(H.checkpoint)
    with pytest.raises(PublishHeld) as e:
        k.publisher.classify(H, pos)
    assert e.value.code == "chain.diverged" and len(k.diverged) == 1


# -- review round 2 (orch-52/Codex): every deletion goes through a confirmation; the H12d-3 seam --------------------


def test_every_delete_path_goes_through_a_confirmation():
    """``_accepted`` (history, then delete) is reached only from ``_publish`` (after ``_final`` and ``_confirm``) and
    from ``_finish_held`` (its fresh verified read is the confirmation)."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(publish.__file__).read_text())
    callers = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and node.attr == "_accepted" and isinstance(node.ctx, ast.Load):
                    callers.add(fn.name)
    assert callers == {"_publish", "_finish_held"}, callers
    text = Path(publish.__file__).read_text()
    node = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "_publish")
    src = ast.get_source_segment(text, node)
    assert src.index("_final(") < src.index("_confirm(") < src.index("_accepted(")


def _moved_past(k, tmp_path):
    _, entry = _restored(k, tmp_path)
    k.drain()
    k.env.log(k.uids[1], "after")
    k.drain()
    k.store._outbox.write(entry)  # noqa: SLF001 - the entry's delete was lost
    return entry


def test_an_ancestry_claim_without_the_entry_itself_deletes_nothing(kit, tmp_path):
    k = kit
    entry = _moved_past(k, tmp_path)
    k.sim.fault = lambda s: _err(404, "not_found") if s.method == "GET" and s.query.get("hash") == entry.hash else None
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["relay_restore_unconfirmed"] and k.store.outbox_first().n == entry.n


def test_a_gone_entry_is_unconfirmed_not_deleted(kit, tmp_path):
    k = kit
    entry = _moved_past(k, tmp_path)
    k.sim.fault = lambda s: (
        _err(410, "checkpoint_gone") if s.method == "GET" and s.query.get("hash") == entry.hash else None
    )
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.store.outbox_first().n == entry.n


def test_a_relay_that_switches_branch_between_the_reads_deletes_nothing(kit, tmp_path):
    """H equals the restore target at the step's read; the confirmation read serves another branch."""
    k = kit
    _, entry = _restored(k, tmp_path)
    k.sim.after = lambda s, r: json_reply(503, {"error": "overloaded"}) if _final(s) else r
    k.publisher.step()  # the relay holds T; the answer is lost
    k.sim.after = None
    o = json.loads(json.dumps(k.sim.held["o"]))
    o["n"] += 3
    o["tickets"][k.uids[1]]["head"] = "sha256:" + "5" * 64
    forked = sign_object(k.env.signer.sign, o)
    reads = {"n": 0}
    real_get = k.sim._get

    def get(s):
        reads["n"] += 1
        if reads["n"] >= 2 and not s.query.get("hash"):
            k.sim.held = forked
        return real_get(s)

    k.sim._get = get
    k.publisher.step()
    assert k.store.outbox_first().n == entry.n and "restore_source_mismatch" in k.alarms.codes()


def test_a_plain_entry_held_at_the_step_but_gone_at_the_confirmation_is_kept(kit, monkeypatch):
    k = kit

    def boom(self, **kw):
        raise Crash()

    monkeypatch.setattr(History, "append", boom)
    with pytest.raises(Crash):
        k.drain()
    monkeypatch.undo()
    entry = k.store.outbox_first()
    reads = {"n": 0}
    real_get = k.sim._get

    def get(s):
        reads["n"] += 1
        if reads["n"] >= 2:
            k.sim.held = None
        return real_get(s)

    k.sim._get = get
    k.publisher = k.make_publisher()
    assert k.publisher.step()[0] == "wait" and "relay_misbehaving" in k.alarms.codes()
    assert k.store.outbox_first().n == entry.n and k.publisher.history.lines() == []


def test_every_restore_publication_is_built_in_restore_pub(kit, tmp_path, monkeypatch):
    """The H12d-3 hook point (#555: `readers.restore_held(...)`): both the outbox restore and the stored record go
    through `_restore_pub`."""
    k = kit
    _, entry = _restored(k, tmp_path)
    seen = []
    real = publish.Publisher._restore_pub

    def spy(self, H, target, *a, **kw):
        seen.append(target["o"]["n"])
        return real(self, H, target, *a, **kw)

    monkeypatch.setattr(publish.Publisher, "_restore_pub", spy)
    k.drain()
    assert seen and seen[0] == entry.n
    k.sim.roll_back_to(k.sim.accepted[0][1])
    k.sim.restore_ids.clear()
    seen.clear()
    k.publisher = k.make_publisher()
    k.drain()
    assert entry.n in seen  # the stored record's republication


# -- review round 2 delta (Opus): renewal liveness (F1 to F3) and the tests F4 asked for -----------------------------


def _count_holds(k, monkeypatch, answers=()):
    """Count the sim's hold calls; the first answers come from ``answers`` (callables), then the real hold."""
    real = k.sim._hold
    n = {"c": 0}

    def hold(s, ws):
        n["c"] += 1
        if n["c"] <= len(answers):
            return answers[n["c"] - 1]()
        return real(s, ws)

    monkeypatch.setattr(k.sim, "_hold", hold)
    return n


def _slow_first_page(k, monkeypatch, steps=5, each_s=10, sleep=0.2):
    real_call = k.session.call
    st = {"done": False}

    def slow(method, target, route, **kw):
        if "/pages/" in target and not st["done"]:
            st["done"] = True
            for _ in range(steps):
                k.sim.advance(each_s)
                time.sleep(sleep)
        return real_call(method, target, route, **kw)

    monkeypatch.setattr(k.session, "call", slow)


def test_d1_a_transient_sync_renewal_failure_is_retried_not_raised(kit, monkeypatch):
    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.0)
    p = k.make_publisher(conn=None)
    n = _count_holds(k, monkeypatch, [lambda: _err(503, "overloaded")])
    p.publish_first(k.session, None)  # the 503 on the renewal is retried later, never raised
    assert k.store.outbox_entries() == [] and n["c"] >= 1 and not k.session.fenced


def test_d2_a_malformed_renewal_answer_stops_publish_first_at_once(kit, monkeypatch):
    from orch.relay.session import Fenced, RelayMisbehaving

    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.05)
    _count_holds(k, monkeypatch, [lambda: json_reply(200, {"gen": 999, "lease_s": 40, "cursor": 0})])
    _slow_first_page(k, monkeypatch)
    n = {"c": 0}
    real_renew = k.session.renew
    monkeypatch.setattr(k.session, "renew", lambda **kw: n.__setitem__("c", n["c"] + 1) or real_renew(**kw))
    with pytest.raises((RelayMisbehaving, Fenced)):
        k.publisher.publish_first(k.session, None)
    assert n["c"] == 1  # stopped: no renewal after the session error


def test_d2_a_failed_renewal_is_retried_soon(kit, monkeypatch):
    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.3)
    monkeypatch.setattr(publish, "RENEW_RETRY_S", 0.02)
    n = _count_holds(k, monkeypatch, [lambda: _err(503, "overloaded")])
    _slow_first_page(k, monkeypatch, steps=1, each_s=1, sleep=0.5)
    k.publisher.publish_first(k.session, None)
    assert n["c"] >= 2  # the retry came well before the next 0.3 s period would have


def test_d3_a_fenced_renewal_is_noticed_during_a_wait(kit, monkeypatch):
    from orch.relay.session import Fenced

    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.05)
    monkeypatch.setattr(publish, "HELD_RECHECK_S", 3.0)
    k.sim.fault = lambda s: _err(503, "overloaded", retry_after=3) if _final(s) else None
    n = _count_holds(k, monkeypatch, [lambda: _err(409, "fenced")])
    t0 = time.monotonic()
    with pytest.raises(Fenced):
        k.publisher.publish_first(k.session, None)
    assert time.monotonic() - t0 < 2.0 and k.session.fenced and n["c"] == 1  # one fenced renewal, never another


def test_d3_a_permanent_refusal_during_the_first_link_is_held_not_slept(kit):
    k = kit
    k.sim.fault = lambda s: _err(403, "not_eligible") if _final(s) else None
    t0 = time.monotonic()
    with pytest.raises(PublishHeld) as e:
        k.publisher.publish_first(k.session, None)
    assert e.value.code == "relay_publish_refused" and time.monotonic() - t0 < 5


def test_the_renewal_thread_ends_with_publish_first_and_uses_its_own_connection(kit, monkeypatch):
    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.05)
    made = []
    p = k.make_publisher(conn=lambda: made.append(1) or k.conn())
    _slow_first_page(k, monkeypatch, steps=2, each_s=1, sleep=0.2)
    p.publish_first(k.session, None)
    assert not [t for t in threading.enumerate() if t.name.startswith("orch-relay-renew-")]
    assert len(made) == 2  # the publisher's and the renewal thread's
    n = _count_holds(k, monkeypatch)
    k.env.log(k.uids[0], "later")
    p.step()  # after the link no renewal of R3's: R5's aux thread renews
    time.sleep(0.2)
    assert n["c"] == 0


def test_sync_renewal_respects_its_period_and_stops_after_the_link(kit, monkeypatch):
    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 3600.0)
    p = k.make_publisher(conn=None)
    n = _count_holds(k, monkeypatch)
    p.publish_first(k.session, None)
    assert n["c"] == 0  # the period has not passed: no renewal at all
    assert p._sync_renew is False  # noqa: SLF001


def test_sync_waits_are_cut_at_the_renewal_period(kit, monkeypatch):
    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.2)
    p = k.make_publisher(conn=None, mono=time.monotonic)
    once = {"n": 0}

    def fault(s):
        if _final(s) and not once["n"]:
            once["n"] = 1
            return _err(503, "overloaded", retry_after=2)
        return None

    k.sim.fault = fault
    n = _count_holds(k, monkeypatch)
    p.publish_first(k.session, None)
    assert n["c"] >= 4 and k.store.outbox_entries() == []


def test_the_renewal_timing_fits_the_lease():
    from orch.relay.session import AUX_HOLD_DEADLINE_S, LEASE_S

    assert publish.RENEW_EVERY_S == 10.0 and publish.RENEW_RETRY_S == 2.0 and AUX_HOLD_DEADLINE_S == 10.0
    # a renewal that times out, then its retry that times out, still lands inside the lease
    assert publish.RENEW_EVERY_S + AUX_HOLD_DEADLINE_S + publish.RENEW_RETRY_S + AUX_HOLD_DEADLINE_S < LEASE_S


def test_session_renew_is_a_running_renewal_only(kit):
    from orch.relay.session import Fenced, NotHolder

    k = kit
    before = len(k.mock.seen)
    gen = k.session.gen
    k.session._gen = None  # noqa: SLF001
    with pytest.raises(NotHolder):
        k.session.renew()
    k.session._gen = gen  # noqa: SLF001
    assert len(k.mock.seen) == before  # no probe without a generation
    k.sim.holders[k.sim.ws] = (gen + 1, k.sim.now + 40_000)  # another holder took over
    with pytest.raises(Fenced):
        k.session.renew()
    assert k.session.fenced
    sent = len(k.mock.seen)
    with pytest.raises(Fenced):
        k.session.renew()
    assert len(k.mock.seen) == sent  # refused locally once fenced


def test_idle_store_busy_does_not_spin(kit, monkeypatch):
    monkeypatch.setattr(publish, "IDLE_POLL_S", 0.1)
    k = kit
    p = k.make_publisher()
    calls = {"n": 0}

    def busy():
        calls["n"] += 1
        raise StoreError("store.busy", "test")

    monkeypatch.setattr(k.store, "outbox_first", busy)
    t = threading.Thread(target=p._idle, daemon=True)  # noqa: SLF001
    t.start()
    time.sleep(0.55)
    p.stop()
    t.join(2)
    assert 3 <= calls["n"] <= 8


def test_r4_the_second_mismatch_in_a_row_is_misbehaving(kit):
    k = kit
    k.drain()
    k.env.log(k.uids[0], "x")
    k.sim.fault = lambda s: _err(409, "restore_source_mismatch") if _final(s) else None
    assert k.publisher.step() == ("progress", 0.0)
    state, _ = k.publisher.step()
    assert state == "wait" and k.alarms.codes() == ["relay_misbehaving"]


def test_a_plain_pub_without_a_set_is_held_on_too_large(kit):
    k = kit
    pub = publish._Pub("plain", {}, 1, "sha256:" + "1" * 64, None)  # not direct, but no runs: nothing was staged
    with pytest.raises(PublishHeld) as e:
        k.publisher._too_large(pub, 10_000_000)
    assert e.value.code == "relay.too_large" and k.publisher.cap() == k.link_cap


def test_a_413_without_a_code_on_the_final_put_is_too_large(kit):
    from .mock import Reply

    k = kit
    k.sim.fault = lambda s: Reply(status=413, body=b"") if _final(s) else None
    k.publisher.step()
    assert set(k.alarms.codes()) & {"relay_too_large", "relay.too_large"}
    assert "relay_publish_refused" not in k.alarms.codes()


def test_a_conflicting_archived_segment_is_a_state_error(tmp_path, monkeypatch):
    from orch.wsproto.state import StateError

    monkeypatch.setattr(publish, "HISTORY_SEGMENT", 3)
    st = StateDir(tmp_path / "h")
    h = History(st)
    for n in range(1, 4):
        h.append(n=n, hash=f"sha256:{n:064x}", kind="plain", at_ms=n, target={"o": {}})
    h.lines()  # the segment count is probed (none yet)
    st.claim("published-000001.jsonl", b"other lines\n")
    with pytest.raises(StateError):
        h.append(n=4, hash=f"sha256:{4:064x}", kind="plain", at_ms=4, target={"o": {}})
    st.close()


def test_a_finished_restores_record_resends_it_after_a_relay_rollback(kit, tmp_path):
    k = kit
    _, entry = _restored(k, tmp_path)
    k.sim.after = lambda s, r: json_reply(503, {"error": "overloaded"}) if _final(s) else r
    k.publisher.step()  # accepted at the relay; the answer lost
    k.sim.after = None
    k.drain(10)  # finished from the fresh read
    (rec,) = k.publisher.history.restores()
    assert rec["source"] is not None and rec["body"]["restore"]["log"] == k.uids[0] and rec["acc_from"]
    k.sim.roll_back_to(k.sim.accepted[0][1])
    k.sim.restore_ids.clear()
    k.publisher = k.make_publisher()
    k.drain(20)
    assert [h for _, h in k.sim.accepted].count(entry.hash) == 2  # re-sent from the record, accepted again
    assert k.current()["hash"] == canon.checkpoint_hash(k.sim.held["o"])


def test_a_renewal_session_error_is_raised_by_publish_first_itself(kit, monkeypatch):
    """The lease stays valid (the stalled page costs 3 s of relay time): only the handed-over error stops it."""
    from orch.relay.session import RelayMisbehaving

    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.05)
    _count_holds(k, monkeypatch, [lambda: json_reply(200, {"gen": 999, "lease_s": 40, "cursor": 0})])
    _slow_first_page(k, monkeypatch, steps=3, each_s=1, sleep=0.2)
    with pytest.raises(RelayMisbehaving):
        k.publisher.publish_first(k.session, None)


def test_a_handed_over_error_wakes_a_waiting_publish_first(kit, monkeypatch):
    from orch.relay.session import RelayMisbehaving

    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 30.0)
    p = k.make_publisher(conn=None)
    k.sim.fault = lambda s: _err(503, "overloaded", retry_after=4) if _final(s) else None
    out = {}

    def run():
        try:
            p.publish_first(k.session, None)
        except Exception as e:  # noqa: BLE001
            out["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t0 = time.monotonic()
    t.start()
    time.sleep(0.5)
    p._renew_error = RelayMisbehaving("renewal")  # noqa: SLF001 - what the renewal thread hands over
    p._alert.set()  # noqa: SLF001
    t.join(10)
    assert isinstance(out.get("e"), RelayMisbehaving) and time.monotonic() - t0 < 2.5


def test_the_sync_renewal_restarts_its_period_after_a_renewal(kit, monkeypatch):
    k = kit
    p = k.make_publisher(conn=None)
    n = _count_holds(k, monkeypatch)
    p._sync_renew, p._last_renew = True, k.clock.mono()  # noqa: SLF001
    k.clock.advance(publish.RENEW_EVERY_S)
    p._tick()  # noqa: SLF001
    p._tick()  # noqa: SLF001 - the same instant: no second renewal
    assert n["c"] == 1
    k.clock.advance(publish.RENEW_EVERY_S)
    p._tick()  # noqa: SLF001
    assert n["c"] == 2


# -- H12d-3 (#555): readers.restore_held wired at _restore_pub -------------------------------------------------------


def _as_convenience(monkeypatch):
    """Judge every restore line as if it carried ``auth: "convenience"`` (the real line stays valid for the relay):
    the level logic is #555's own ``restore_held``."""
    from orch.ops import readers

    real = readers.restore_held
    monkeypatch.setattr(publish.readers, "restore_held",
                        lambda level, ev: real(level, [{**e, "auth": "convenience"} for e in ev]))  # fmt: skip


def _level_kit(k, level):
    box = {"level": level, "reads": 0}

    def read():
        box["reads"] += 1
        return box["level"]

    k.publisher = k.make_publisher(relay_level=read)
    return box


def test_h12d3_a_convenience_restore_is_held_at_a_low_level_then_released(kit, tmp_path, monkeypatch):
    k = kit
    _, entry = _restored(k, tmp_path)
    _as_convenience(monkeypatch)
    box = _level_kit(k, 0)
    before = sum(1 for s in k.mock.seen if "/staging/" in s.path or _final(s))
    for _ in range(2):
        assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["relay_reader_old"] and k.store.outbox_first().n == entry.n
    assert sum(1 for s in k.mock.seen if "/staging/" in s.path or _final(s)) == before  # nothing sent
    assert k.publisher.cap() == k.link_cap  # never lowered
    box["level"] = 1  # R5's login observer stored the relay's new level
    k.drain(10)
    assert (entry.n, entry.hash) in k.sim.accepted and k.store.outbox_entries() == []
    assert box["reads"] >= 3  # read fresh at every step, never cached


def test_h12d3_none_and_unreadable_levels_hold(kit, tmp_path, monkeypatch):
    k = kit
    _restored(k, tmp_path)
    _as_convenience(monkeypatch)
    for level in (None, "boom"):

        def read(level=level):
            if level == "boom":
                raise OSError("unreadable")
            return level

        p = k.make_publisher(relay_level=read)
        assert p.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.store.outbox_entries()[0].kind == "restore"


def test_h12d3_the_hold_depends_on_the_evidence(kit, tmp_path):
    k = kit
    _, entry = _restored(k, tmp_path)
    _level_kit(k, 0)
    k.drain(10)  # a passphrase restore at level 0: not held
    assert (entry.n, entry.hash) in k.sim.accepted and "relay_reader_old" not in k.alarms.codes()
    r = entry.restore
    p = k.publisher
    with pytest.raises(PublishHeld) as e:  # the same restore with a convenience line: held
        p._restore_pub(None, entry.checkpoint, {**r["event"], "auth": "convenience"}, r["log"], r, [], 1 << 20,
                       entry=entry, acc_from=r["acc_from"])  # fmt: skip
    assert e.value.code == "relay_reader_old"


def test_h12d3_a_restart_keeps_holding_and_wake_rechecks(kit, tmp_path, monkeypatch):
    k = kit
    _, entry = _restored(k, tmp_path)
    _as_convenience(monkeypatch)
    _level_kit(k, 0)
    assert k.publisher.step()[0] == "wait"
    k.publisher.stop()
    box = _level_kit(k, 0)  # a restart: a new publisher on the same state
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S) and k.store.outbox_first().n == entry.n
    p = k.publisher
    p.start()
    time.sleep(0.3)
    box["level"] = 1
    p.wake()  # re-checked at once, not after the hour
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and k.store.outbox_entries():
        time.sleep(0.05)
    assert k.store.outbox_entries() == []
    p.stop()
    assert p.join(5)


def test_h12d3_the_hold_never_blocks_finishing_a_restore_the_relay_holds(kit, tmp_path, monkeypatch):
    """The confirm/delete rule is unaffected: the relay already holds the restore (accepted while the level was 1, the
    answer lost); a later low level does not stop the bookkeeping, and the delete still needs the fresh read."""
    k = kit
    _, entry = _restored(k, tmp_path)
    k.sim.after = lambda s, r: json_reply(503, {"error": "overloaded"}) if _final(s) else r
    k.publisher.step()
    k.sim.after = None
    _as_convenience(monkeypatch)
    _level_kit(k, 0)
    k.drain(10)
    assert k.store.outbox_entries() == [] and [r["n"] for r in k.publisher.history.restores()] == [entry.n]


# -- review delta 2 (Opus): F1 wake cuts only idle and held waits; F4 level default; F5 renewal edges; F6 ------------


def _is_get_h(s):
    return s.method == "GET" and s.path.endswith("/checkpoint") and not s.query.get("hash")


def test_f1_wake_never_cuts_a_retry_after_floor(kit):
    k = kit
    k.sim.fault = lambda s: _err(503, "overloaded", retry_after=60) if _final(s) else None
    p = k.make_publisher(mono=time.monotonic)
    p.start()
    time.sleep(0.5)
    before = sum(1 for s in k.mock.seen if _is_get_h(s))
    for _ in range(5):
        p.wake()  # R5 wakes on every append
        time.sleep(0.2)
    assert sum(1 for s in k.mock.seen if _is_get_h(s)) == before
    p.stop()
    assert p.join(5)


def test_f1_wake_never_retries_a_permanent_refusal(kit):
    k = kit
    k.sim.fault = lambda s: _err(403, "forbidden") if _final(s) else None
    p = k.make_publisher(mono=time.monotonic)
    p.start()
    end = time.monotonic() + 30  # the refused attempt is awaited by its effect, however slow the machine is
    while not any(_final(s) for s in list(k.mock.seen)) and time.monotonic() < end:
        time.sleep(0.01)
    before = sum(1 for s in k.mock.seen if _final(s))
    assert before >= 1
    for _ in range(5):
        p.wake()
        time.sleep(0.05)
    assert sum(1 for s in k.mock.seen if _final(s)) == before
    p.stop()
    assert p.join(5)


def test_f4_without_a_host_level_a_convenience_restore_is_held_whatever_relay_info_says(kit, tmp_path, monkeypatch):
    k = kit
    _, entry = _restored(k, tmp_path)
    _as_convenience(monkeypatch)
    p = k.make_publisher()  # no relay_level=: relay-info says reader 1 (the sim's), which never counts for the hold
    assert p.step() == ("wait", publish.HELD_RECHECK_S)
    assert p.reader_level == 1 and k.alarms.codes() == ["relay_reader_old"]
    assert k.store.outbox_first().n == entry.n


def test_f5_a_redirect_on_the_renewal_is_alarmed_and_stops_the_first_link(kit, monkeypatch):
    from orch.net.http import RedirectRefused

    from .mock import Reply

    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.05)
    _count_holds(k, monkeypatch, [lambda: Reply(status=307, headers={"Location": "https://x"}, body=b"")] * 50)
    _slow_first_page(k, monkeypatch, steps=3, each_s=1, sleep=0.2)
    with pytest.raises(RedirectRefused):
        k.publisher.publish_first(k.session, None)
    assert k.alarms.codes().count("relay_redirect") == 1


def test_f5_an_unexpected_renewal_failure_is_alarmed_and_handed_over(kit, monkeypatch):
    k = kit
    monkeypatch.setattr(publish, "RENEW_EVERY_S", 0.05)

    def bug(**kw):
        raise KeyError("a bug")

    monkeypatch.setattr(k.session, "renew", bug)
    _slow_first_page(k, monkeypatch, steps=3, each_s=1, sleep=0.2)
    with pytest.raises(KeyError):
        k.publisher.publish_first(k.session, None)
    assert "relay_renewal_failed" in k.alarms.codes()


def _plain_held_after_a_lost_history(k, monkeypatch):
    def boom(self, **kw):
        raise Crash()

    monkeypatch.setattr(History, "append", boom)
    with pytest.raises(Crash):
        k.drain()
    monkeypatch.undo()
    return k.store.outbox_first()


def _switch_second_read(k, cp):
    reads = {"n": 0}
    real_get = k.sim._get

    def get(s):
        if not s.query.get("hash"):
            reads["n"] += 1
            if reads["n"] >= 2:
                k.sim.held = cp
        return real_get(s)

    k.sim._get = get


def test_f6_a_lower_checkpoint_with_the_same_logs_does_not_confirm(kit, monkeypatch):
    """The fresh read shows a LOWER n with the very same logs (an ancestor by ancestry, not a descendant): even with
    the entry served by its hash, nothing is deleted (the `H.n > pub.n` order)."""
    k = kit
    entry = _plain_held_after_a_lost_history(k, monkeypatch)
    o = json.loads(json.dumps(entry.checkpoint["o"]))
    o["n"] = entry.n - 1
    lower = sign_object(k.env.signer.sign, o)
    _switch_second_read(k, lower)
    k.publisher = k.make_publisher()
    assert k.publisher.step()[0] == "wait" and "relay_misbehaving" in k.alarms.codes()
    assert k.store.outbox_first().n == entry.n


def test_f6_a_plain_entry_on_another_branch_does_not_confirm_by_hash(kit, monkeypatch):
    k = kit
    entry = _plain_held_after_a_lost_history(k, monkeypatch)
    o = json.loads(json.dumps(entry.checkpoint["o"]))
    o["n"] = entry.n + 4
    o["workspace_log"]["head"] = "sha256:" + "5" * 64  # another branch
    _switch_second_read(k, sign_object(k.env.signer.sign, o))
    k.publisher = k.make_publisher()
    assert k.publisher.step()[0] == "wait" and "relay_misbehaving" in k.alarms.codes()
    assert k.store.outbox_first().n == entry.n  # the relay still serves the entry by hash: not enough


# -- the reviewer's delta-2 mutants (run in r3pub-mut6): the survivors that matter ------------------------------------


def test_sync_tick_raises_a_fence(kit, monkeypatch):
    from orch.relay.session import Fenced

    k = kit
    p = k.make_publisher(conn=None)
    _count_holds(k, monkeypatch, [lambda: _err(409, "fenced")])
    p._sync_renew, p._last_renew = True, k.clock.mono() - publish.RENEW_EVERY_S  # noqa: SLF001
    with pytest.raises(Fenced):
        p._tick()  # noqa: SLF001


def test_first_check_raises_on_a_fenced_session(kit):
    from orch.relay.session import Fenced

    k = kit
    k.session._fenced = True  # noqa: SLF001 - a fence seen by another thread
    with pytest.raises(Fenced):
        k.publisher._first_check()  # noqa: SLF001


def test_sync_tick_retries_soon_after_a_failure(kit, monkeypatch):
    k = kit
    p = k.make_publisher(conn=None)
    n = _count_holds(k, monkeypatch, [lambda: _err(503, "overloaded")])
    p._sync_renew, p._last_renew = True, k.clock.mono() - publish.RENEW_EVERY_S  # noqa: SLF001
    p._tick()  # noqa: SLF001 - fails
    k.clock.advance(publish.RENEW_RETRY_S)
    p._tick()  # noqa: SLF001 - retried after RENEW_RETRY_S, not a full period
    assert n["c"] == 2


def test_a_permanent_quota_during_the_first_link_is_held_not_slept(kit):
    k = kit
    k.sim.fault = lambda s: (json_reply(429, {"error": "quota_exceeded", "quota": "nodes"}, {"Retry-After": "1"})
                             if _final(s) else None)  # fmt: skip
    with pytest.raises(PublishHeld) as e:
        k.publisher.publish_first(k.session, None)
    assert e.value.code == "relay_publish_refused"


def test_a_stale_renewal_error_does_not_outlive_its_first_link(kit):
    from orch.relay.session import RelayMisbehaving

    k = kit
    k.publisher._renew_error = RelayMisbehaving("an earlier link")  # noqa: SLF001
    k.publisher.publish_first(k.session, None)
    assert k.store.outbox_entries() == []


def test_stop_wakes_a_first_link_wait(kit):
    k = kit
    k.sim.fault = lambda s: _err(503, "overloaded", retry_after=30) if _final(s) else None
    p = k.make_publisher(conn=None, mono=time.monotonic)
    out = {}

    def run():
        try:
            p.publish_first(k.session, None)
        except PublishHeld as e:
            out["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.5)
    t0 = time.monotonic()
    p.stop()
    t.join(5)
    assert not t.is_alive() and time.monotonic() - t0 < 2 and out["e"].code == "relay_stopped"


def test_h12d3_a_stored_restores_republication_is_held_too(kit, tmp_path, monkeypatch):
    k = kit
    _, entry = _restored(k, tmp_path)
    k.drain()  # accepted at a good level (passphrase)
    k.sim.roll_back_to(k.sim.accepted[0][1])
    k.sim.restore_ids.clear()
    _as_convenience(monkeypatch)
    _level_kit(k, 0)
    for _ in range(6):
        k.publisher.step()
    assert "relay_reader_old" in k.alarms.codes()
    assert [h for _, h in k.sim.accepted].count(entry.hash) == 1  # the stored restore was not re-sent
