import pytest

from orch.core import store
from orch.core.events import read_events
from orch.core.ledger import entries
from orch.core.protect import protected_changes
from orch.core.query import open_blockers
from orch.errors import HumanOnlyError, NotFoundError, UsageError

from conftest import seen_hash


def test_close_as_wont_do_is_recorded_signed_and_logged(ws, put, hops, aops):
    tid = put("backlog")
    with pytest.raises(HumanOnlyError):
        aops.close(tid, "not worth it", "wont-do")
    t = hops.close(tid, "not worth it", "wont-do")
    assert t.status == "done" and t.meta["resolution"] == "wont-do" and "superseded_by" not in t.meta
    assert "closed as wont-do: not worth it" in t.section("Log")
    assert read_events(ws)[-1].data["resolution"] == "wont-do"
    assert entries(ws)[-1]["kind"] == "close" and entries(ws)[-1]["resolution"] == "wont-do"


def test_superseded_names_its_successor(ws, put, hops):
    old, new = put("open"), put("open")
    with pytest.raises(UsageError):
        hops.close(old, "replaced", "superseded")  # no --by
    with pytest.raises(UsageError):
        hops.close(old, "replaced", "wont-do", new)  # --by only for superseded or duplicate
    with pytest.raises(UsageError):
        hops.close(old, "replaced", "superseded", old)  # not itself
    with pytest.raises(NotFoundError):
        hops.close(old, "replaced", "superseded", "DEMO-9999")
    with pytest.raises(UsageError):
        hops.close(old, "replaced", "finished")
    t = hops.close(old, "replaced", "superseded", new)
    assert (t.meta["resolution"], t.meta["superseded_by"]) == ("superseded", new)
    assert entries(ws)[-1]["superseded_by"] == new
    from orch.dashboard.data.timeline import describe
    assert describe(read_events(ws)[-1]) == f"closed the ticket as superseded by {new} (open → done): replaced"


def test_plain_close_and_verdict_are_completed_and_reopen_clears_it(ws, put, hops):
    tid = hops.close(put("open"), "GH-13 is closed in GitHub").id
    assert store.load(ws, tid)[1].meta["resolution"] == "completed"
    tid = put("testing", sections={"Verification": "ran it"})
    t = hops.verdict(tid, "done", expected_hash=seen_hash(ws, "verdict", tid))
    assert t.meta["resolution"] == "completed"
    old = hops.close(put("open"), "replaced", "duplicate", put("open"))
    t = hops.reopen(old.id, "it was not a duplicate")
    assert "resolution" not in t.meta and "superseded_by" not in t.meta


def test_a_superseded_blocker_hands_its_block_to_the_successor(ws, put, hops):
    new = put("open")
    old = hops.close(put("open"), "replaced", "superseded", new).id
    dropped = hops.close(put("open"), "not needed", "wont-do").id
    waits = store.load(ws, put("open", blocked_by=[old, dropped]))[1]
    assert open_blockers(ws, waits) == [new]
    hops.close(new, "shipped")
    assert open_blockers(ws, waits) == []


def test_resolution_is_protected_from_raw_edits():
    assert protected_changes({"resolution": "completed"}, {"resolution": "wont-do"}) == ["resolution"]
    assert protected_changes({}, {"superseded_by": "DEMO-0001"}) == ["superseded_by"]


def test_ticket_document_carries_the_resolution(ws, put, hops):
    from orch.core.schema import ticket_document, ticket_schema
    import jsonschema
    new = put("open")
    old = hops.close(put("open"), "replaced", "duplicate", new)
    doc = ticket_document(ws, old)
    assert (doc["resolution"], doc["superseded_by"]) == ("duplicate", new)
    jsonschema.validate(doc, ticket_schema())
    assert ticket_document(ws, store.load(ws, new)[1])["resolution"] is None
    legacy = store.load(ws, put("done"))[1]  # closed before resolutions existed
    assert ticket_document(ws, legacy)["resolution"] == "completed"
