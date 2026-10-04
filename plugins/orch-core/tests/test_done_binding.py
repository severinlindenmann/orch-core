"""A signed close or done verdict confirms one done: the ledger alone decides, so the event log, the ticket file and
a reopened ticket's earlier entries cannot make a later done look human-verified."""
import json

from orch.core import ledger, store
from orch.core.check import run_checks
from orch.core.events import append_event, read_events
from orch.core.schema import _signed
from orch.dashboard.data import story


def _state(ws, tid):
    """(done_signer, schema signed.verdict, check reports nothing unsigned)."""
    t = store.load(ws, tid)[1]
    unsigned = any(f.code == "unsigned-decision" and f.ticket == tid for f in run_checks(ws, emit_events=False))
    return story.done_signer(ws, t, read_events(ws, tid)), _signed(ws, t).get("verdict", {}).get("signed"), not unsigned


NOT_VERIFIED = (None, False, False)


def _done_by_hand(ws, tid, human, *, event=True, verify=None):
    """Done again the way an edit of the files would do it: status, optionally a verify block and a human-looking event."""
    path, t = store.load(ws, tid)
    t.meta["status"] = "done"
    if verify:
        t.meta.setdefault("gates", {})["verify"] = verify
    store.save(ws, t, path)
    if event:
        append_event(ws, tid, "ticket.moved", human, {"command": "close", "from": "open", "to": "done"})


def _rewrite(ws, edit, *, drop=None):
    """Rewrite the ledger as an older version would have: `edit` on every entry, valid signatures again, lines for
    which `drop` is true removed, and no head record."""
    key = ledger._key(create=False)
    path = ledger.ledger_path(ws)
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if drop and drop(e):
            continue
        e.pop("n", None)  # an older version did not number its entries
        edit(e)
        e["mac"] = ledger._mac(key, e)
        out.append(json.dumps(e))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    ledger.head_path().unlink(missing_ok=True)  # an older version kept no head record


def _legacy(e):
    e.pop("prev", None)


def test_close_reopen_cycles(ws, hops, human, put):
    tid = put("open")
    hops.close(tid, "first")
    assert _state(ws, tid) == ("closed", True, True)
    hops.reopen(tid, "more to do")
    _done_by_hand(ws, tid, human)
    assert _state(ws, tid) == NOT_VERIFIED
    hops.reopen(tid, "again")
    hops.close(tid, "second")
    assert _state(ws, tid) == ("closed", True, True)


def test_forged_done_without_any_event_is_not_verified(ws, hops, human, put):
    tid = put("open")
    hops.close(tid, "first")
    hops.reopen(tid, "more")
    _done_by_hand(ws, tid, human, event=False)
    assert _state(ws, tid) == NOT_VERIFIED


def test_deleted_or_renumbered_events_do_not_help(ws, hops, human, put):
    tid = put("open")
    hops.close(tid, "first")
    hops.reopen(tid, "more")
    _done_by_hand(ws, tid, human)
    path = ws.events_path if hasattr(ws, "events_path") else None
    from orch.core.events import _path
    p = _path(ws)
    kept = [ln for ln in p.read_text(encoding="utf-8").splitlines() if '"command": "reopen"' not in ln]
    p.write_text("\n".join(kept) + "\n", encoding="utf-8")  # the reopen event is gone: one done event left
    assert _state(ws, tid)[0] is None


def test_back_dated_event_does_not_help(ws, hops, human, put):
    tid = put("open")
    hops.close(tid, "first")
    hops.reopen(tid, "more")
    _done_by_hand(ws, tid, human, event=False)
    from orch.core.events import _path
    with _path(ws).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"seq": 9999, "at": "2000-01-01T00:00:00Z", "ticket": tid, "kind": "ticket.moved",
                            "actor": "human:you", "via": "tty", "data": {"command": "close", "to": "done"}}) + "\n")
    assert _state(ws, tid)[0] is None


def test_verdict_cycle_and_restored_verify_block(ws, hops, human, put):
    tid = put("testing", sections={"Verification": "ok"})
    hops.verdict(tid, "done")
    assert _state(ws, tid) == ("accepted", True, True)
    verify = dict(store.load(ws, tid)[1].meta["gates"]["verify"])
    hops.reopen(tid, "regression")
    _done_by_hand(ws, tid, human, verify=verify)  # the same verify block comes back by hand
    assert _state(ws, tid)[0] is None


def test_a_different_verdict_hash_does_not_verify(ws, hops, human, put):
    tid = put("testing", sections={"Verification": "ok"})
    hops.verdict(tid, "done")
    path, t = store.load(ws, tid)
    t.meta["gates"]["verify"]["hash"] = "sha256:other"
    store.save(ws, t, path)
    assert _state(ws, tid)[0] is None


def test_deleted_ledger_line_breaks_the_chain(ws, hops, put):
    tid = put("open")
    hops.close(tid, "first")
    hops.reopen(tid, "more")
    hops.close(tid, "second")
    assert _state(ws, tid)[0] == "closed"
    _rewrite(ws, lambda e: None, drop=lambda e: e["kind"] == "reopen")
    assert _state(ws, tid) == NOT_VERIFIED


def test_tampered_entry_does_not_verify(ws, hops, put):
    tid = put("open")
    hops.close(tid, "x")
    path = ledger.ledger_path(ws)
    e = json.loads(path.read_text(encoding="utf-8"))
    e["prev"] = "forged"
    path.write_text(json.dumps(e) + "\n", encoding="utf-8")
    assert _state(ws, tid)[0] is None


def test_entry_from_before_the_chain_verifies_only_its_single_done(ws, hops, human, put):
    tid = put("open")
    hops.close(tid, "old")
    _rewrite(ws, _legacy)
    assert _state(ws, tid) == ("closed", True, True)
    hops.reopen(tid, "again")  # a reopen entry now follows it
    _done_by_hand(ws, tid, human)
    assert _state(ws, tid) == NOT_VERIFIED


def test_legacy_entry_with_two_done_events_is_not_verified_and_adoption_fixes_it(ws, hops, human, put):
    tid = put("open")
    hops.close(tid, "old")
    hops.reopen(tid, "again")
    hops.close(tid, "old again")
    _rewrite(ws, _legacy, drop=lambda e: e["kind"] == "reopen")  # as an older version wrote it: no reopen entry
    assert _state(ws, tid) == NOT_VERIFIED
    t = store.load(ws, tid)[1]
    item = next(i for i in ledger.unsigned_items(ws, [t]) if i["kind"] == "close")
    hops.ledger_adopt(item, item["id"])
    assert _state(ws, tid) == ("closed", True, True)


def _pre_chain(ws, tid):
    return [f for f in run_checks(ws, emit_events=False) if f.code == "pre-chain-signature" and f.ticket == tid]


def test_check_flags_a_done_signed_before_the_chain_and_adoption_chains_it(ws, hops, put):
    tid = put("open")
    hops.close(tid, "old")
    assert not _pre_chain(ws, tid)  # a chained entry is not flagged
    _rewrite(ws, _legacy)
    (f,) = _pre_chain(ws, tid)
    assert f.level == "warning" and "before the ledger chain" in f.message and "orch ledger adopt" in f.message
    assert _state(ws, tid) == ("closed", True, True)  # still verified the old way, and no unsigned-decision
    item = next(i for i in ledger.unsigned_items(ws, [store.load(ws, tid)[1]]) if i["kind"] == "close")
    assert "predates the ledger chain" in item["text"]
    hops.ledger_adopt(item, item["id"])
    assert not _pre_chain(ws, tid) and _state(ws, tid) == ("closed", True, True)
    assert "prev" in ledger.status_chain(ws, tid)[-1] and not ledger.unsigned_items(ws, [store.load(ws, tid)[1]])


def test_adopting_a_pre_chain_entry_is_human_only(ws, hops, aops, put):
    import pytest
    from orch.errors import OrchError
    tid = put("open")
    hops.close(tid, "old")
    _rewrite(ws, _legacy)
    item = next(i for i in ledger.unsigned_items(ws, [store.load(ws, tid)[1]]) if i["kind"] == "close")
    with pytest.raises(OrchError):
        aops.ledger_adopt(item, item["id"])
    assert _pre_chain(ws, tid)
