"""R3 review round 1 (Opus, 15 Oct 2026) regressions: the reviewer's reproductions (R1 to R4, adopted), the lower
findings (R5 to R10), and the tests its surviving mutants asked for (the fresh-read confirmation, the lowering limit,
a restore's too_large, LINE_CAP, publication_complete, the H7 boundary, the answer shapes, R3's own fork hold)."""

from __future__ import annotations

import json
import time

import pytest

from orch import canon
from orch.relay import publish
from orch.relay.publish import History, PublishHeld
from orch.relay.state import StateDir
from orch.store.checkpoints import sign_object
from orch.store.errors import StoreError

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
