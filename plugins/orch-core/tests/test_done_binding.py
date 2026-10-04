"""A signed close or done verdict confirms exactly one done: a reopened ticket that is done again needs a fresh one."""
import json

from orch.core import ledger, store
from orch.core.check import run_checks
from orch.core.events import append_event, read_events
from orch.core.schema import _signed
from orch.dashboard.data import story


def _state(ws, tid):
    t = store.load(ws, tid)[1]
    unsigned = any(f.code == "unsigned-decision" and f.ticket == tid for f in run_checks(ws, emit_events=False))
    return story.done_signer(ws, t, read_events(ws, tid)), _signed(ws, t).get("verdict", {}).get("signed"), not unsigned


def _done_without_signature(ws, tid, human):
    """Done again the way an edit of the files would do it: status and a human-looking close event, no ledger entry."""
    path, t = store.load(ws, tid)
    t.meta["status"] = "done"
    store.save(ws, t, path)
    append_event(ws, tid, "ticket.moved", human, {"command": "close", "from": "open", "to": "done"})


def _resign(ws, edit):
    """Rewrite the ledger with `edit` applied to every entry and valid signatures (as an older version wrote it)."""
    key = ledger._key(create=False)
    path = ledger.ledger_path(ws)
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        edit(e)
        e["mac"] = ledger._mac(key, e)
        out.append(json.dumps(e))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def test_reopen_then_done_again_is_not_verified_until_signed_again(ws, hops, human, put):
    tid = put("open")
    hops.close(tid, "first")
    assert _state(ws, tid) == ("closed", True, True)
    hops.reopen(tid, "more to do")
    _done_without_signature(ws, tid, human)
    assert _state(ws, tid) == (None, False, False)
    hops.reopen(tid, "again")
    hops.close(tid, "second")
    assert _state(ws, tid) == ("closed", True, True)
    assert [e["round"] for e in ledger.entries(ws) if e["kind"] == "close"] == [1, 3]  # the unsigned done in between was round 2


def test_verdict_cycle_is_bound_to_its_own_done(ws, hops, human, put):
    tid = put("testing", sections={"Verification": "ok"})
    hops.verdict(tid, "done")
    assert _state(ws, tid) == ("accepted", True, True)
    hops.reopen(tid, "regression")
    path, t = store.load(ws, tid)  # the same verify block comes back by hand: the old entry must not cover it
    t.meta["status"] = "done"
    t.meta.setdefault("gates", {})["verify"] = {"verdict": "done", "at": ledger.entries(ws)[-1]["verify_at"], "via": "tty"}
    store.save(ws, t, path)
    append_event(ws, tid, "verdict.given", human, {"verdict": "done", "from": "testing", "to": "done"})
    assert _state(ws, tid)[0] is None


def test_tampered_round_does_not_verify(ws, hops, put):
    tid = put("open")
    hops.close(tid, "x")
    path = ledger.ledger_path(ws)
    e = json.loads(path.read_text(encoding="utf-8"))
    e["round"] = 5
    path.write_text(json.dumps(e) + "\n", encoding="utf-8")
    assert _state(ws, tid)[0] is None


def test_entry_from_before_rounds_verifies_only_the_first_done_after_it(ws, hops, human, put):
    tid = put("open")
    hops.close(tid, "old")
    _resign(ws, lambda e: e.pop("round", None))
    assert _state(ws, tid) == ("closed", True, True)  # its original done still verifies
    hops.reopen(tid, "again")
    _done_without_signature(ws, tid, human)
    assert _state(ws, tid) == (None, False, False)  # never a later one
