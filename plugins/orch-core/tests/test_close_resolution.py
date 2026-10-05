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


# -- dashboard ---------------------------------------------------------------------------------------------------------

def test_dashboard_closes_as_superseded_and_shows_it(dash, ws, put):
    new, old = put("open"), put("open")
    page = dash.get(f"/t/{old}").text
    assert f'action="/t/{old}/close"' in page and f'action="/t/{old}/reopen"' not in page
    r = dash.post(f"/t/{old}/close", data={"as": "superseded", "by": "", "message": "replaced"})
    assert "needs the ticket that replaces it" in r.text  # refused, nothing changed
    assert store.load(ws, old)[1].status == "open"
    dash.post(f"/t/{old}/close", data={"as": "superseded", "by": new, "message": "replaced"})
    t = store.load(ws, old)[1]
    assert (t.status, t.meta["resolution"], t.meta["superseded_by"]) == ("done", "superseded", new)
    page = dash.get(f"/t/{old}").text
    assert f"Superseded by {new}" in page and f'href="/t/{new}"' in page and f'action="/t/{old}/reopen"' in page
    assert f"Superseded by {new}" in dash.get("/board?view=list&status=done").text


def test_dashboard_journey_ticks_only_what_happened_for_a_dropped_ticket(dash, ws, put, hops):
    from orch.core.events import read_events
    from orch.dashboard.data import story
    from orch.dashboard.data.cards import ticket_card
    tid = hops.close(put("open"), "not needed", "wont-do").id
    assert "Won&#39;t do" in dash.get(f"/t/{tid}").text
    t = store.load(ws, tid)[1]
    stages = {j["name"]: j["state"] for j in story.journey(t, ticket_card(ws, t), [], read_events(ws, tid), {})}
    assert stages == {"Asked": "done", "Agreed": "todo", "Doing": "todo", "Proven": "todo", "Done": "done"}


def test_dashboard_ignores_a_hidden_replacement_left_from_an_earlier_choice(dash, ws, put):
    other, tid = put("open"), put("open")
    dash.post(f"/t/{tid}/close", data={"as": "wont-do", "by": other, "message": "not needed"})
    t = store.load(ws, tid)[1]
    assert (t.status, t.meta["resolution"]) == ("done", "wont-do") and "superseded_by" not in t.meta


def test_dashboard_offers_no_close_for_an_epic_and_no_done_elsewhere_in_testing(dash, ws, put):
    epic = put("open", type="epic")
    assert f'action="/t/{epic}/close"' not in dash.get(f"/t/{epic}").text
    page = dash.get(f"/t/{put('testing', sections={'Verification': 'ran it'})}").text
    assert 'value="wont-do"' in page and 'value="completed"' not in page and "give the verdict" in page


def test_dashboard_close_dialog_opens_without_js_from_the_menu_link(dash, put):
    tid = put("open")
    page = dash.get(f"/t/{tid}").text
    assert f'href="/t/{tid}?act=close#close-dialog"' in page and 'id="close-dialog" aria-labelledby="close-dialog-title">' in page
    assert 'id="close-dialog" aria-labelledby="close-dialog-title" open>' in dash.get(f"/t/{tid}?act=close").text


def test_new_ticket_puts_the_ask_first_and_shows_the_triage_details(dash):
    html = dash.get("/new").text
    assert html.index('id="ask"') < html.index('id="files"') < html.index('class="new-details"') < html.index('name="type"')
    assert '<details class="new-details"' not in html  # always shown: a human filing a ticket here is triaging it


def test_dashboard_reopen_clears_the_resolution(dash, ws, put, hops):
    tid = hops.close(put("open"), "not needed", "wont-do").id
    dash.post(f"/t/{tid}/reopen", data={"message": "needed after all"})
    t = store.load(ws, tid)[1]
    assert t.status == "backlog" and "resolution" not in t.meta
