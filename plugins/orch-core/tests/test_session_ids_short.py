"""A harness session id is stored and shown only in its short form (events.short_session): never in a ticket, an
event or a view in full. Records written before keep matching."""
from orch.core import store
from orch.core.events import Actor, read_events
from orch.core.ops import Ops

FULL = "7f3c9a21-1234-4abc-8def-0123456789ab"


def _agent(sid=FULL):
    return Actor("agent", "claude-code", "cli", sid)


def test_claim_stores_and_emits_only_the_short_form(ws, put):
    a = Ops(ws, _agent())
    tid = put("open")
    t = a.claim(tid)
    path, _ = store.load(ws, t.id)
    text = path.read_text(encoding="utf-8")
    assert FULL not in text and "7f3c9a21" in text
    events = (ws.state_dir / "events.jsonl").read_text(encoding="utf-8")
    assert FULL not in events
    a.release(t.id)
    assert FULL not in (ws.state_dir / "events.jsonl").read_text(encoding="utf-8")
    assert all(FULL not in str(e.data) for e in read_events(ws))


def test_a_claim_written_with_the_full_id_still_belongs_to_its_session(ws, put):
    from orch.core import query
    a = Ops(ws, _agent())
    t = a.claim(put("open"))
    path, tk = store.load(ws, t.id)
    tk.meta["claim"]["session"] = FULL  # what an older orch wrote
    store.save(ws, tk, path)
    assert [e.id for e in query.list_tickets(ws, session=FULL)] == [t.id]
    a.task_add(t.id, ["one"])  # the claim check of task writes
    a.release(t.id)  # the same session: allowed
