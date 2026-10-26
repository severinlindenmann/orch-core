"""H6b, ``ticket.query`` (§0.8, §0.8.4, v1.4 row fields, v1.5 ``Closed.seq``/``event_id``): rows, filters, sorts,
snapshot paging, the F6 rules (a restricted ticket the viewer cannot see contributes nothing), page tokens, and the
joint with ``sub.open {after}``. Each acceptance item of the H6b row in §8 for this op has a test here."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from orch.host import reads_query
from tests.host.dispatch_helpers import err
from tests.host.live_helpers import SESSION, World, jpos_of, ok, principal_of, world
from tests.host.test_dispatch_matrix import as_agent_class

ROW_KEYS = {
    "key", "uid", "title", "type", "status", "priority", "size", "labels", "assignees", "owner", "parent", "epic",
    "claim", "gates", "needs", "updated_at", "head_seq", "restricted", "tasks", "criteria", "blocking_questions",
}  # fmt: skip


@pytest.fixture
def w(tmp_path):
    s = world(tmp_path)
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


@pytest.fixture
def wq(tmp_path):
    """The same workspace, the test UID mapped to the member ``quinn`` (column P)."""
    s = world(tmp_path, map_to="quinn")
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


# --- helpers ------------------------------------------------------------------------------------------------------


def agent(w: World, op: str, args: dict[str, Any], session: str = SESSION) -> dict[str, Any]:
    """An agent op through the host, by the owner's grant (one tick of the test clock first)."""
    w.lab.ws.tick()
    if not hasattr(w, "_agent_conn"):
        w._agent_conn = w.client()
    return ok(w._agent_conn.req(op, args, grant=w.grant, session=session, idem=True))


def mk(w: World, title: str, **args: Any) -> tuple[str, str]:
    res = agent(w, "new", {"title": title, **args})
    return w.lab.ws.uid(res["key"]), res["key"]


def on_store(w: World, fn: Any) -> Any:
    """``fn(ws)`` on the workspace worker with the host's own store (what the test helpers of H6a do)."""
    service, ws = w.service, w.lab.ws
    ws.tick()

    def run() -> Any:
        store = service.store
        old_store, ws.store = ws.store, store
        old_check, store.append_check = store.append_check, None
        try:
            return fn(ws)
        finally:
            store.append_check, ws.store = old_check, old_store
            service.sync()

    return w.h.run(service.call(run))


def query(w: World, args: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    c = kw.pop("client", None) or w.client()
    return ok(c.req("ticket.query", args or {}, **kw))["data"]


def keys(data: dict[str, Any]) -> list[str]:
    return [r["key"] for r in data["rows"]]


def close_ticket(w: World, uid: str, **payload: Any) -> dict[str, Any]:
    return w.person_event("ticket.closed", uid, **{"resolution": "other", **payload})


# --- rows ---------------------------------------------------------------------------------------------------------


def test_a_row_has_exactly_the_contract_keys_and_the_ticket_values(w):
    uid, key = mk(w, "Plain", priority="high", size="m", label=["a", "b"])
    data = query(w)
    assert set(data) == {"rows", "next_page", "total", "sub_cursor", "as_of"}
    assert data["total"] == 1 and data["next_page"] is None
    (row,) = data["rows"]
    assert set(row) == ROW_KEYS  # a plain ticket has no `closed`
    assert (row["key"], row["uid"], row["title"], row["type"], row["status"]) == (key, uid, "Plain", "feature", "open")
    assert (row["priority"], row["size"], row["labels"]) == ("high", "m", ["a", "b"])
    assert row["owner"] == w.lab.owner and row["parent"] is None and row["epic"] is None and row["claim"] is None
    assert row["gates"] == {"requirements": "open", "plan": "open", "verify": "open", "code": "n/a"}
    assert (row["needs"], row["restricted"], row["blocking_questions"]) == ([], False, 0)
    assert row["tasks"] == {"done": 0, "skipped": 0, "total": 0} and row["criteria"] == {"done": 0, "total": 0}
    last = w.lab.ws.events(key)[-1]
    assert (row["updated_at"], row["head_seq"]) == (last["at"], last["seq"])  # the ticket's last event


def test_updated_at_and_head_seq_follow_the_last_event(w):
    _, key = mk(w, "T")
    before = query(w)["rows"][0]
    ev = w.write(key, "later")
    after = query(w)["rows"][0]
    assert after["head_seq"] == ev["seq"] == before["head_seq"] + 1 and after["updated_at"] > before["updated_at"]


def test_assignees_claim_tasks_criteria_and_blocking_questions(w):
    uid, key = mk(w, "Busy")
    agent(w, "claim", {"ref": key})
    agent(w, "ac.add", {"ref": key, "text": "it works"})
    agent(w, "ac.add", {"ref": key, "text": "it is fast"})
    agent(w, "task.add", {"ref": key, "text": "one"})
    agent(w, "task.add", {"ref": key, "text": "two"})
    agent(w, "task.skip", {"task": "T2", "reason": "no"})
    agent(w, "ask", {"ref": key, "text": "which?", "options": ["a", "b"]})
    agent(w, "ask", {"ref": key, "text": "later?", "non_blocking": True})
    (row,) = query(w)["rows"]
    assert row["status"] == "in_progress"
    taken = next(e for e in w.lab.ws.events(key) if e["type"] == "claim.taken")
    assert row["claim"] == {
        "session": SESSION, "agent": taken["actor"]["id"], "for": w.lab.owner, "since": taken["at"], "live": True,
    }  # fmt: skip
    assert row["tasks"] == {"done": 0, "skipped": 1, "total": 2}
    assert row["criteria"] == {"done": 0, "total": 2}
    assert row["blocking_questions"] == 1
    assert row["needs"] == ["question"]


def test_criteria_done_counts_the_models_evidence(w):
    uid, key = mk(w, "Proved")
    agent(w, "claim", {"ref": key})
    agent(w, "ac.add", {"ref": key, "text": "a"})
    agent(w, "ac.add", {"ref": key, "text": "b"})
    view = w.h.run(w.service.call(lambda: w.service.store.ticket(uid)))
    assert sum(bool(a.evidence) for a in view.acceptance) == 0
    assert query(w)["rows"][0]["criteria"] == {"done": 0, "total": 2}


def test_criteria_done_is_the_number_of_criteria_with_model_evidence(w, monkeypatch):
    """Evidence by the F1 §6 rule is the model's ``AcView.evidence``; the row counts exactly the non-empty ones."""
    uid, key = mk(w, "Proved")
    agent(w, "ac.add", {"ref": key, "text": "a"})
    agent(w, "ac.add", {"ref": key, "text": "b"})
    from orch.model.views import AcView

    real = reads_query._row

    def fake_row(ctx, view, *a):
        import dataclasses

        acc = (AcView("AC1", "a", ("art",)), AcView("AC2", "b", ()))
        return real(ctx, dataclasses.replace(view, acceptance=acc), *a)

    monkeypatch.setattr(reads_query, "_row", fake_row)
    assert query(w)["rows"][0]["criteria"] == {"done": 1, "total": 2}


# --- filters ------------------------------------------------------------------------------------------------------


def test_filters_combine_with_and_and_values_inside_one_filter_with_or(w):
    _, a = mk(w, "alpha", type="bug", priority="low", label=["x"])
    _, b = mk(w, "beta", type="chore", priority="high", label=["y"])
    _, c = mk(w, "gamma", type="bug", priority="high", label=["x", "y"])
    assert set(keys(query(w, {"type": ["bug"]}))) == {a, c}
    assert set(keys(query(w, {"type": ["bug", "chore"]}))) == {a, b, c}  # OR inside one filter
    assert set(keys(query(w, {"type": ["bug"], "label": ["y"]}))) == {c}  # AND between filters
    assert set(keys(query(w, {"label": ["x", "y"]}))) == {a, b, c}
    assert keys(query(w, {"status": ["open"], "owner": w.lab.owner, "type": ["chore"]})) == [b]
    assert query(w, {"assignee": w.quinn.ref})["rows"] == []
    assert query(w, {"status": ["done"]})["total"] == 0


def test_q_is_a_nfc_casefold_substring_over_key_title_and_sections(w):
    _, a = mk(w, "Straße unter der Brücke")
    _, b = mk(w, "plain")
    agent(w, "claim", {"ref": b})
    agent(w, "section.set", {"ref": b, "section": "context", "message": "Das Äquivalent liegt im Keller"})
    assert keys(query(w, {"q": "STRASSE"})) == [a]  # casefold: ß == ss
    assert keys(query(w, {"q": "brücke"})) == [a]
    assert keys(query(w, {"q": "brücke"})) == [a]  # NFC: the combining form of ü matches
    assert keys(query(w, {"q": "äquivalent"})) == [b]  # a section text
    assert keys(query(w, {"q": a.lower()})) == [a]  # the key
    assert query(w, {"q": "nothing like this"})["rows"] == []


def test_q_matches_visible_content_only(wq):
    uid, key = mk(wq, "needle in a restricted ticket")
    wq.restrict(uid, [wq.lab.owner])  # quinn (the caller) is not on the list
    assert query(wq, {"q": "needle"})["rows"] == [] and query(wq, {"q": "needle"})["total"] == 0


def test_updated_since_keeps_tickets_updated_at_or_after(w):
    _, a = mk(w, "old")
    w.lab.ws.tick(60)
    _, b = mk(w, "new")
    since = query(w, {"q": "new"})["rows"][0]["updated_at"]
    assert keys(query(w, {"updated_since": since})) == [b]
    assert set(keys(query(w, {"updated_since": "2000-01-01T00:00:00Z"}))) == {a, b}


def test_parent_and_epic_filters_and_the_epic_of_a_row(w):
    _, e = mk(w, "the epic", type="epic")
    _, c = mk(w, "child", parent=e)
    _, g = mk(w, "grandchild", parent=c)
    _, o = mk(w, "orphan")
    rows = {r["key"]: r for r in query(w)["rows"]}
    assert rows[c]["parent"] == e and rows[c]["epic"] == e
    assert rows[g]["parent"] == c and rows[g]["epic"] == e  # the nearest epic ancestor
    assert rows[o]["parent"] is None and rows[o]["epic"] is None and rows[e]["epic"] is None
    assert keys(query(w, {"parent": e})) == [c]
    assert set(keys(query(w, {"epic": e}))) == {c, g}
    assert keys(query(w, {"parent": e.lower()})) == [c]  # any ref form of the parent


def test_parent_or_epic_naming_a_ticket_the_viewer_cannot_see_is_not_found(wq):
    uid, key = mk(wq, "hidden epic", type="epic")
    wq.restrict(uid, [wq.lab.owner])
    c = wq.client()
    for name in ("parent", "epic"):
        assert err(c.req("ticket.query", {name: key})) == "not_found"
        assert err(c.req("ticket.query", {name: "DEMO-9999"})) == "not_found"  # as one that does not exist


def test_needs_me_keeps_rows_with_a_need_of_the_viewer(w):
    _, a = mk(w, "asked")
    _, b = mk(w, "quiet")
    agent(w, "ask", {"ref": a, "text": "owner?", "to": "ticket_owner"})
    agent(w, "ask", {"ref": b, "text": "quinn?", "to": w.quinn.ref})
    assert keys(query(w, {"needs_me": True})) == [a]  # the owner is the viewer; b's question is quinn's
    assert query(w, {"needs_me": True}, grant=w.grant, session=SESSION)["total"] == 1  # G: the grant's person
    as_agent_class(w)
    assert query(w, {"needs_me": True})["rows"] == []  # no-person has no needs


def test_a_row_needs_are_the_distinct_kinds_of_the_needs_op_in_the_models_order(w):
    uid, key = mk(w, "gates and questions")
    agent(w, "ask", {"ref": key, "text": "one?"})
    agent(w, "ask", {"ref": key, "text": "two?"})
    on_store(w, lambda ws: ws.fill(uid))
    (row,) = query(w)["rows"]
    assert row["needs"] == ["question", "approve"]  # distinct, question before approve
    items = ok(w.client().req("needs", {}))["data"]["items"]
    assert [i["kind"] for i in items] == ["question", "question", "approve"]


# --- sorting ------------------------------------------------------------------------------------------------------


@pytest.mark.slow
def test_sorts_orders_and_ties(w):
    _, a = mk(w, "a", priority="low")
    _, b = mk(w, "b", priority="urgent")
    _, c = mk(w, "c")  # the default priority
    _, d = mk(w, "d", priority="urgent")
    assert keys(query(w, {"sort": "key", "order": "asc"})) == [a, b, c, d]
    assert keys(query(w, {"sort": "key"})) == [d, c, b, a]  # order defaults to desc
    assert keys(query(w)) == [d, c, b, a]  # updated desc: the newest first
    assert keys(query(w, {"sort": "updated", "order": "asc"})) == [a, b, c, d]
    prio = {r["key"]: r["priority"] for r in query(w)["rows"]}
    assert prio == {a: "low", b: "urgent", c: "medium", d: "urgent"}
    assert keys(query(w, {"sort": "priority", "order": "asc"})) == [b, d, c, a]  # urgent < ... < low; ties by key asc
    assert keys(query(w, {"sort": "priority", "order": "desc"})) == [a, c, b, d]  # reversed, ties still by key asc


def test_ties_on_updated_at_break_by_key_ascending_for_both_orders(w):
    made = []
    for n in range(3):  # no tick between them: the same `at`
        res = ok(w.client().req("new", {"title": f"t{n}"}, grant=w.grant, session=SESSION, idem=True))
        made.append(res["key"])
    assert len({r["updated_at"] for r in query(w)["rows"]}) == 1
    assert keys(query(w, {"sort": "updated", "order": "asc"})) == made
    assert keys(query(w, {"sort": "updated", "order": "desc"})) == made


# --- paging -------------------------------------------------------------------------------------------------------


@pytest.mark.slow
def test_paging_stays_consistent_under_appends_no_repeat_no_gap(w):
    made = [mk(w, f"t{n}")[1] for n in range(7)]
    args = {"limit": 3, "sort": "key", "order": "asc"}
    first = query(w, args)
    assert keys(first) == made[:3] and first["total"] == 7 and first["next_page"]
    new = mk(w, "appended after the first page")[1]  # a new ticket and events on existing ones, between the pages
    w.write(made[5], "touched")
    w.write(made[0], "touched")
    second = query(w, {**args, "page": first["next_page"]})
    third = query(w, {**args, "page": second["next_page"]})
    got = keys(first) + keys(second) + keys(third)
    assert got == made  # no repeat, no gap, and the ticket created later is not in the snapshot
    assert new not in got and third["next_page"] is None
    for page in (second, third):  # the snapshot's values repeat on every page
        assert (page["sub_cursor"], page["as_of"], page["total"]) == (first["sub_cursor"], first["as_of"], 7)
    rows = {r["key"]: r for r in first["rows"] + second["rows"] + third["rows"]}
    assert rows[made[5]]["head_seq"] == 2  # §0.8: membership and order are the snapshot's, row contents are now
    again = query(w, args)
    assert again["total"] == 8 and again["sub_cursor"] != first["sub_cursor"]  # a new first page is a new snapshot


def test_sorted_paging_with_ties_and_a_default_limit(w):
    made = [mk(w, f"t{n}", priority="high")[1] for n in range(5)]
    seen: list[str] = []
    page = None
    while True:
        args: dict[str, Any] = {"sort": "priority", "order": "asc", "limit": 2}
        if page:
            args["page"] = page
        data = query(w, args)
        seen += keys(data)
        page = data["next_page"]
        if not page:
            break
    assert seen == made  # all tie on priority: key ascending, across pages
    assert len(query(w)["rows"]) == 5  # limit defaults to 50


def test_next_page_is_null_only_when_no_further_visible_row_exists(wq):
    a = mk(wq, "a")
    hidden = mk(wq, "b")
    c = mk(wq, "c")
    args = {"limit": 1, "sort": "key", "order": "asc"}
    first = query(wq, args)
    assert keys(first) == [a[1]] and first["next_page"] and first["total"] == 3
    wq.restrict(hidden[0], [wq.lab.owner])  # becomes invisible to quinn between the pages
    second = query(wq, {**args, "page": first["next_page"]})
    assert keys(second) == [c[1]]  # skipped, not a hole; and the page is full
    assert second["next_page"] is None and second["total"] == 2  # nothing visible after c
    wq.restrict(c[0], [wq.lab.owner])
    third = query(wq, {**args, "page": first["next_page"]})
    assert third["rows"] == [] and third["next_page"] is None and third["total"] == 1


def test_a_ticket_that_becomes_visible_between_pages_adds_nothing(wq):
    a = mk(wq, "a")
    hidden = mk(wq, "b")
    wq.restrict(hidden[0], [wq.lab.owner])
    c = mk(wq, "c")
    args = {"limit": 1, "sort": "key", "order": "asc"}
    first = query(wq, args)
    wq.restrict(hidden[0], [wq.lab.owner, wq.quinn.ref])  # now visible to quinn
    rest = query(wq, {**args, "limit": 5, "page": first["next_page"]})
    assert keys(first) + keys(rest) == [a[1], c[1]] and rest["total"] == 2  # not in the snapshot
    assert query(wq, {"sort": "key", "order": "asc"})["total"] == 3  # a new first page sees it


def test_a_removed_person_on_page_two_is_page_expired(wq):
    """Stricter than "filtered as the new viewer" (§0.8): a change of the viewer's person on the same principal,
    to none included, expires the token (pages.resume)."""
    for n in range(2):
        mk(wq, f"w{n}")
    uid, r = mk(wq, "restricted")
    wq.restrict(uid, [wq.lab.owner, wq.quinn.ref])
    args = {"limit": 1, "sort": "key", "order": "asc"}
    first = query(wq, args)
    assert first["total"] == 3
    wq.person_event("member.removed", person=wq.quinn.ref)  # the UID maps to a removed person: no-person now
    res = wq.client().req("ticket.query", {**args, "limit": 5, "page": first["next_page"]})
    assert err(res) == "page.expired"
    assert r not in json.dumps(res)


def test_a_revoked_grant_on_page_two_changes_the_principal_so_the_token_expires(w):
    for n in range(3):
        mk(w, f"t{n}")
    first = query(w, {"limit": 1}, grant=w.grant, session=SESSION)
    assert first["next_page"]
    w.person_event("grant.revoked", grant=w.lab.ws.grant_id)
    res = w.client().req("ticket.query", {"limit": 1, "page": first["next_page"]}, grant=w.grant, session=SESSION)
    assert err(res) == "page.expired"  # an unverified grant reads as the uid principal: another principal's token


def test_the_first_pages_sub_cursor_joined_to_sub_open_loses_and_repeats_nothing(w):
    _, key = mk(w, "live")
    w.write(key, "before")
    c = w.conn()
    data = ok(c.call("ticket.query", {}))["data"]
    cursor = data["sub_cursor"]
    assert cursor == w.snapshot_cursor(principal_of())
    a = w.write(key, "after 1")
    b = w.write(key, "after 2")
    sub = c.open(["tickets"], after=cursor)
    items, _ = c.items_until(sub["sub"], jpos_of(cursor) + 2)
    assert [(i["type"], i["seq"]) for i in items] == [("log.added", a["seq"]), ("log.added", b["seq"])]
    assert data["rows"][0]["head_seq"] == a["seq"] - 1  # the row is the state at the cursor


# --- page tokens --------------------------------------------------------------------------------------------------


def paged(w: World, **kw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    for n in range(3):
        mk(w, f"t{n}")
    args = {"limit": 1, "status": ["open"]}
    first = query(w, args, **kw)
    assert first["next_page"]
    return args, first


def with_page(args: dict[str, Any], token: str) -> dict[str, Any]:
    return {**args, "page": token}


def test_a_forged_or_altered_page_token_is_page_expired(w):
    args, first = paged(w)
    token = first["next_page"]
    c = w.client()
    head, body, mac = token.split(".")
    pos = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    pos["i"] = 0
    forged_body = base64.urlsafe_b64encode(json.dumps(pos, separators=(",", ":")).encode()).decode().rstrip("=")
    flipped = mac[:-1] + ("A" if mac[-1] != "A" else "B")
    for bad in (
        f"{head}.{forged_body}.{mac}",  # altered body under the old MAC
        f"{head}.{body}.{flipped}",  # altered MAC
        f"{head}.{body}",  # truncated
        "p1.AAAA.BBBB",  # never issued
        "nonsense",
        token[:-2],
    ):
        assert err(c.req("ticket.query", with_page(args, bad))) == "page.expired", bad
    assert ok(c.req("ticket.query", with_page(args, token)))  # the genuine token still works


def test_another_principals_page_token_is_page_expired(w):
    args, first = paged(w)
    res = w.client().req("ticket.query", with_page(args, first["next_page"]), grant=w.grant, session=SESSION)
    assert err(res) == "page.expired"  # issued to uid:<n>, used by g:<grant>@<uid>
    grant_first = query(w, args, grant=w.grant, session=SESSION)
    assert err(w.client().req("ticket.query", with_page(args, grant_first["next_page"]))) == "page.expired"


def test_an_expired_page_token_is_page_expired(w):
    args, first = paged(w)
    w.lab.ws.tick(601)  # more than 10 minutes on the host's clock
    assert err(w.client().req("ticket.query", with_page(args, first["next_page"]))) == "page.expired"


def test_a_token_still_works_just_inside_ten_minutes(w):
    args, first = paged(w)
    w.lab.ws.tick(590)
    assert ok(w.client().req("ticket.query", with_page(args, first["next_page"])))


def test_a_token_with_other_arguments_is_invalid_input(w):
    args, first = paged(w)
    c = w.client()
    for other in ({"limit": 1, "status": ["open", "done"]}, {"limit": 1}, {"limit": 1, "status": ["open"], "q": "t"}):
        assert err(c.req("ticket.query", with_page(other, first["next_page"]))) == "invalid.input", other
    # `limit` and `page` are not part of the digest: a token is valid with another limit
    assert len(query(w, with_page({"limit": 2, "status": ["open"]}, first["next_page"]))["rows"]) == 2


def test_a_token_whose_snapshot_is_gone_is_page_expired(w):
    args, first = paged(w)
    w.service.pages.store.snaps.clear()  # the host-side snapshot is gone (eviction or restart)
    assert err(w.client().req("ticket.query", with_page(args, first["next_page"]))) == "page.expired"


# --- F6: a restricted ticket the viewer cannot see contributes nothing ---------------------------------------------


def hidden_busy_ticket(w: World) -> tuple[str, str]:
    """A restricted ticket (owner only) with a claim, tasks, criteria, a blocking question and an epic's type."""
    uid, key = mk(w, "zebracorn secret epic", type="epic")
    agent(w, "claim", {"ref": key})
    agent(w, "ac.add", {"ref": key, "text": "zebracorn criterion"})
    agent(w, "task.add", {"ref": key, "text": "zebracorn task"})
    agent(w, "ask", {"ref": key, "text": "zebracorn?", "to": w.lab.owner})
    w.restrict(uid, [w.lab.owner])
    return uid, key


def test_restricted_rows_are_hidden_and_total_is_unchanged_by_them(wq):
    mk(wq, "visible one")
    before = query(wq)
    uid, key = hidden_busy_ticket(wq)
    after = query(wq)
    assert after["total"] == before["total"] == 1 and keys(after) == keys(before)
    blob = json.dumps(after)
    assert "zebracorn" not in blob and key not in blob and uid not in blob
    assert query(wq, {"status": ["in_progress", "open"]})["total"] == 1
    assert query(wq, {"needs_me": True})["total"] == 0 and query(wq, {"type": ["epic"]})["total"] == 0


def test_nothing_is_computed_for_a_ticket_the_viewer_cannot_see(wq, monkeypatch):
    mk(wq, "visible one")
    uid, key = hidden_busy_ticket(wq)
    closed_uid, _ = mk(wq, "hidden and closed")
    close_ticket(wq, closed_uid)
    wq.restrict(closed_uid, [wq.lab.owner])
    seen: list[str] = []
    real = {name: getattr(reads_query, name) for name in ("_row", "_claim", "_closed", "ticket_items", "_epic")}

    def spy(name: str, pick: Any):
        def wrapper(*a: Any, **k: Any) -> Any:
            seen.append(pick(*a, **k))
            return real[name](*a, **k)

        return wrapper

    monkeypatch.setattr(reads_query, "_row", spy("_row", lambda ctx, view, *a: view.uid))
    monkeypatch.setattr(reads_query, "ticket_items", spy("ticket_items", lambda ctx, view, **k: view.uid))
    monkeypatch.setattr(reads_query, "_epic", spy("_epic", lambda ctx, view: view.uid))
    monkeypatch.setattr(reads_query, "_claim", spy("_claim", lambda view, events: view.uid))
    monkeypatch.setattr(reads_query, "_closed", spy("_closed", lambda events: "closed"))
    data = query(wq, {"status": ["open", "in_progress", "closed"]})
    assert len(data["rows"]) == 1
    assert seen, "the spies saw the visible ticket"
    assert uid not in seen and closed_uid not in seen and "closed" not in seen  # not even looked at


def test_epic_and_parent_of_a_visible_row_reveal_nothing_about_an_invisible_ticket(wq):
    _, epic = mk(wq, "visible epic", type="epic")
    mid_uid, mid = mk(wq, "the middle", parent=epic)
    _, leaf = mk(wq, "leaf", parent=mid)
    assert {r["key"]: r["epic"] for r in query(wq)["rows"]}[leaf] == epic
    wq.restrict(mid_uid, [wq.lab.owner])  # the middle ticket becomes invisible to quinn
    rows = {r["key"]: r for r in query(wq)["rows"]}
    assert set(rows) == {epic, leaf}
    assert rows[leaf]["parent"] == mid  # the key only
    assert rows[leaf]["epic"] is None  # the walk ends at an invisible parent, even though an epic is above it
    assert "the middle" not in json.dumps(rows)
    hidden_epic_uid, hidden_epic = mk(wq, "hidden epic", type="epic")
    _, kid = mk(wq, "kid of hidden epic", parent=hidden_epic)
    wq.restrict(hidden_epic_uid, [wq.lab.owner])
    kid_row = next(r for r in query(wq)["rows"] if r["key"] == kid)
    assert kid_row["parent"] == hidden_epic and kid_row["epic"] is None


def test_restricted_is_true_exactly_for_non_workspace_rows_the_viewer_may_see(wq):
    _, plain = mk(wq, "plain")
    both_uid, both = mk(wq, "owner and quinn")
    wq.restrict(both_uid, [wq.lab.owner, wq.quinn.ref])
    only_uid, only = mk(wq, "owner only")
    wq.restrict(only_uid, [wq.lab.owner])
    rows = {r["key"]: r for r in query(wq)["rows"]}
    assert set(rows) == {plain, both} and rows[plain]["restricted"] is False and rows[both]["restricted"] is True
    as_agent_class(wq)  # no-person: workspace tickets only, so `restricted` is always false
    n_rows = query(wq)["rows"]
    assert {r["key"]: r["restricted"] for r in n_rows} == {plain: False}
    assert query(wq)["total"] == 1


def test_the_owner_who_is_not_on_a_restricted_list_does_not_see_it_either(w):
    mk(w, "visible")
    uid, key = mk(w, "for quinn only")
    w.restrict(uid, [w.quinn.ref])
    assert key not in json.dumps(query(w)) and query(w)["total"] == 1
    assert query(w, grant=w.grant, session=SESSION)["total"] == 1  # G: the same person


def test_a_no_person_viewer_gets_workspace_rows_only(w):
    _, plain = mk(w, "plain")
    uid, _ = mk(w, "restricted")
    w.restrict(uid, [w.lab.owner])
    as_agent_class(w)
    assert keys(query(w)) == [plain] and query(w)["total"] == 1


# --- closed tickets (§0.8.4, v1.5 Closed.seq/event_id) -------------------------------------------------------------


def test_a_closed_ticket_is_left_out_without_status_and_returned_with_closed(w):
    _, a = mk(w, "stays open")
    uid, b = mk(w, "gets closed")
    ev = close_ticket(w, uid, resolution="wont_do")
    assert keys(query(w)) == [a] and query(w)["total"] == 1  # no status: every status except closed
    assert keys(query(w, {"status": ["open"]})) == [a]
    data = query(w, {"status": ["closed"]})
    (row,) = data["rows"]
    assert row["key"] == b and row["status"] == "closed" and data["total"] == 1
    assert row["closed"] == {
        "resolution": "wont_do", "duplicate_of": None, "at": ev["at"], "by": w.lab.owner, "seq": ev["seq"],
        "event_id": ev["id"],
    }  # fmt: skip
    assert set(keys(query(w, {"status": ["open", "closed"]}))) == {a, b}  # alone or together with others
    plain = next(r for r in query(w, {"status": ["open", "closed"]})["rows"] if r["key"] == a)
    assert "closed" not in plain


def test_a_reopened_ticket_loses_closed_and_is_listed_again(w):
    uid, key = mk(w, "round trip")
    close_ticket(w, uid)
    assert query(w)["total"] == 0 and "closed" in query(w, {"status": ["closed"]})["rows"][0]
    w.person_event("ticket.reopened", uid)
    (row,) = query(w)["rows"]
    assert row["key"] == key and row["status"] != "closed" and "closed" not in row
    assert query(w, {"status": ["closed"]})["rows"] == []
    ev = close_ticket(w, uid, resolution="obsolete")  # closed again: the latest closing since the reopen
    (row,) = query(w, {"status": ["closed"]})["rows"]
    assert row["closed"]["resolution"] == "obsolete" and row["closed"]["event_id"] == ev["id"]


def test_duplicate_of_naming_an_invisible_ticket_is_returned_as_the_key_only(wq):
    uid, key = mk(wq, "the duplicate")
    other_uid, other = mk(wq, "zebracorn original")
    wq.restrict(other_uid, [wq.lab.owner])
    close_ticket(wq, uid, resolution="duplicate", duplicate_of=other)
    (row,) = query(wq, {"status": ["closed"]})["rows"]
    assert row["closed"]["duplicate_of"] == other and row["closed"]["resolution"] == "duplicate"
    assert "zebracorn" not in json.dumps(row)  # the key, never the title or anything resolved
    assert err(wq.client().req("show", {"ref": other})) == "not_found"  # the key resolves to nothing for quinn


def test_gates_of_a_closed_ticket_stay_as_the_model_derives_them(w):
    uid, key = mk(w, "approved then closed")
    on_store(w, lambda ws: ws.fill(uid))
    on_store(w, lambda ws: ws.approve(uid, "requirements"))
    close_ticket(w, uid)
    (row,) = query(w, {"status": ["closed"]})["rows"]
    assert row["gates"]["requirements"] == "approved" and row["needs"] == []  # no approve need for a closed ticket


def test_an_open_question_on_a_closed_ticket_still_counts_for_the_row(w):
    uid, key = mk(w, "closed with a question")
    agent(w, "ask", {"ref": key, "text": "still open?"})
    close_ticket(w, uid)
    (row,) = query(w, {"status": ["closed"]})["rows"]
    assert row["needs"] == ["question"] and row["blocking_questions"] == 1


def test_validation(w):
    c = w.client()
    for bad in ({"limit": 0}, {"limit": 201}, {"status": []}, {"status": ["nope"]}, {"sort": "x"}, {"zzz": 1},
                {"assignee": "quinn"}, {"updated_since": "yesterday"}, {"needs_me": "yes"}):  # fmt: skip
        assert err(c.req("ticket.query", bad)) == "invalid.input", bad


# --- H6b fix round: compact snapshots, rows read now ---------------------------------------------------------------


def snapshots(w: World) -> list[Any]:
    return list(w.service.pages._snaps.values())


@pytest.mark.slow
def test_review_repro_large_rows_still_page_and_the_snapshot_holds_uids_only(w):
    """Review finding 1: 45 tickets with 6 000 labels of 31 characters (about 200 KB a row) used to make the first
    page fail with quota.host (every row was stored). The snapshot is now a uid list."""
    mk(w, "page one")
    labels = [f"l{n:030d}" for n in range(6_000)]
    big = [mk(w, f"big {i}", label=labels)[1] for i in range(12)]  # (the review used 45: same effect, 4x slower)
    args = {"limit": 1, "sort": "key", "order": "asc"}
    c = w.client()
    first = ok(c.req("ticket.query", args))["data"]  # not quota.host
    assert first["total"] == 13 and first["next_page"]
    (snap,) = snapshots(w)
    assert set(snap.data) == {"uids", "sub_cursor", "as_of"} and len(snap.data["uids"]) == 13
    assert snap.size < 3_000  # 13 uids, not 46 rows of 200 KB
    second = ok(c.req("ticket.query", {**args, "page": first["next_page"]}))["data"]
    assert keys(second) == big[:1] and len(second["rows"][0]["labels"]) == 6_000
    assert second["total"] == 13 and second["next_page"]


def test_a_15000_ticket_closed_list_stays_far_under_the_snapshot_cap(w):
    """Review finding 1 (13.6k rows reached the cap): the snapshot of a ``status:["closed"]``-sized list is the uids."""
    from orch.host import pages as pages_mod

    uid, _ = mk(w, "sample")
    uids = [f"{n:0{len(uid)}d}" for n in range(15_000)]  # as long as a real uid
    pages = pages_mod.Pages("ws_big")
    snap = pages.open("ticket.query", "uid:1", "e", None, {"uids": uids, "sub_cursor": "c" * 120, "as_of": "x" * 40})
    assert snap.size < pages_mod.PRINCIPAL_BYTES // 8
    assert snap.size < 15_000 * (len(uid) + 4) + 400


def test_a_later_page_reads_its_rows_from_the_current_state(w):
    made = [mk(w, f"t{n}")[1] for n in range(4)]
    args = {"limit": 2, "sort": "key", "order": "asc"}
    first = query(w, args)
    w.write(made[3], "touched later")
    agent(w, "set", {"ref": made[2], "pairs": ["title=Renamed later"]})
    second = query(w, {**args, "page": first["next_page"]})
    assert keys(second) == made[2:] and second["rows"][0]["title"] == "Renamed later"
    assert second["rows"][1]["head_seq"] == 2
    assert [r["title"] for r in first["rows"]] == ["t0", "t1"]


def test_a_returned_row_is_not_shared_with_a_later_response(w):
    made = [mk(w, f"t{n}", label=["x"])[1] for n in range(3)]
    args = {"limit": 1, "sort": "key", "order": "asc"}
    first = query(w, args)
    token = first["next_page"]
    a = query(w, {**args, "page": token})
    a["rows"][0]["labels"].append("MUTATED")
    a["rows"][0]["title"] = "MUTATED"
    b = query(w, {**args, "page": token})
    assert keys(b) == made[1:2] and b["rows"][0]["labels"] == ["x"] and b["rows"][0]["title"] == "t1"
