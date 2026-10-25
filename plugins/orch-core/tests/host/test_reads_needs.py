"""H6b, ``needs`` (§0.8, §0.8.4): the model's needs with ``who`` limited to persons who can see the ticket, the v1.4
fields (``since``, ``text``, ``status``, ``payload``), the visibility of ``needs {person}``, closed tickets, and the
joint of ``sub_cursor`` with ``sub.open {after}``."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orch import canon
from orch.host import reads_query
from orch.model.needs import Need
from tests.host.dispatch_helpers import err
from tests.host.live_helpers import SESSION, World, jpos_of, ok, principal_of, world
from tests.host.test_dispatch_matrix import as_agent_class
from tests.host.test_reads_query import agent, close_ticket, mk, on_store


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
    s = world(tmp_path, map_to="quinn")
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


_FIXTURE = json.loads((Path(__file__).parent.parent / "vectors" / "f1" / "questions.json").read_text("utf-8"))["forms"]


def needs(w: World, args: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    return ok(w.client().req("needs", args or {}, **kw))["data"]


def model_needs(w: World, uid: str) -> list[Any]:
    return list(w.h.run(w.service.call(lambda: w.service.store.ticket(uid).needs)))


def ask_event(w: World, uid: str, qid_n: str, text: str, to: str = "ticket_owner", blocking: bool = True) -> dict:
    """A ``question.asked`` for an existing question id (a re-ask), appended through the host's store."""
    options = [{"key": "a", "label": "a"}, {"key": "b", "label": "b"}]
    qid = canon.question_id(w.wid, uid, qid_n)
    q = {"id": qid_n, "to": to, "text": text, "blocking": blocking, "kind": "choice", "options": options}
    h = canon.question_hash(qid, uid, q)
    return on_store(w, lambda ws: ws.store.append(
        {"type": "question.asked", "actor": ws.agent, "question": q, "qid": qid, "hash": h}, log=uid
    ).event)  # fmt: skip


def set_policy(w: World, gate: str, approvers: list[str]) -> dict[str, Any]:
    policy = {"approvers": sorted(approvers), "count": 1, "not": [], "applies": "all", "independent": False}
    return w.person_event("policy.changed", gates={gate: policy})


def override(w: World, uid: str, gate: str, approvers: list[str]) -> None:
    policy = {"approvers": sorted(approvers), "count": 1, "not": [], "applies": "all", "independent": False}
    w.person_event("policy.changed", uid, gates={gate: policy})


def promote(w: World, role: str = "maintainer") -> None:
    w.person_event("role.changed", person=w.quinn.ref, role=role)


# --- equals the model ----------------------------------------------------------------------------------------------


def test_needs_equal_the_model_needs_with_who_limited(w):
    uid, key = mk(w, "gates and questions")
    agent(w, "ask", {"ref": key, "text": "which?", "text_question": True, "to": w.lab.owner})
    agent(w, "ask", {"ref": key, "text": "later?", "text_question": True, "non_blocking": True})
    on_store(w, lambda ws: ws.fill(uid))
    items = needs(w)["items"]
    model = model_needs(w, uid)
    assert [(i["kind"], i["ref"], i["detail"]) for i in items] == [(n.kind, n.ref, n.detail) for n in model]
    assert [i["who"] for i in items] == [sorted(n.who) for n in model]  # all visible: nothing to limit
    assert all(i["key"] == key and i["uid"] == uid and i["status"] == "open" for i in items)
    assert [i["kind"] for i in items] == ["question", "question", "approve"]
    assert [i["detail"] for i in items] == ["blocking", "", ""]


def test_a_workspace_with_no_needs_has_no_items(w):
    mk(w, "quiet")
    assert needs(w)["items"] == []


# --- question ------------------------------------------------------------------------------------------------------


def test_a_question_item_has_text_payload_status_and_since(w):
    uid, key = mk(w, "q")
    agent(w, "ask", {"ref": key, "text": "which one?", "option": ["a | a", "b | b"], "rec": "b", "why": "because"})
    asked = [e for e in w.lab.ws.events(key) if e["type"] == "question.asked"][0]
    (item,) = needs(w)["items"]
    assert item == {
        "kind": "question", "key": key, "uid": uid, "ref": "Q1", "detail": "blocking", "who": [w.lab.owner],
        "since": asked["at"], "text": "which one?", "status": "open",
        "payload": {
            "question": "Q1", "blocking": True, "to": "ticket_owner",
            "options": [{"key": "a", "label": "a"}, {"key": "b", "label": "b"}],
            "spec": {"kind": "choice", "recommended": "b", "why": "because"},
        },
    }  # fmt: skip


def test_a_questions_text_is_never_cut_at_its_longest(w):
    """#1123: a question is at most 140 code points (560 bytes), always under the 1 024-byte snip."""
    uid, key = mk(w, "long")
    agent(w, "ask", {"ref": key, "text": "\U0001f600" * 140, "text_question": True})
    (item,) = needs(w)["items"]
    assert item["text"] == "\U0001f600" * 140 and "text_cut" not in item
    agent(w, "ask", {"ref": key, "text": "short", "text_question": True})
    short = needs(w)["items"][1]
    assert short["text"] == "short" and "text_cut" not in short


def test_a_non_blocking_question_has_an_empty_detail_and_no_options_key(w):
    uid, key = mk(w, "nb")
    agent(w, "ask", {"ref": key, "text": "fyi?", "text_question": True, "non_blocking": True})
    (item,) = needs(w)["items"]
    assert item["detail"] == "" and item["payload"] == {"question": "Q1", "blocking": False, "to": "ticket_owner",
                                                        "spec": {"kind": "text"}}  # fmt: skip


def test_since_of_a_question_restarts_only_when_its_text_changes(w):
    uid, key = mk(w, "reask")
    first = ask_event(w, uid, "Q1", "first text")
    assert needs(w)["items"][0]["since"] == first["at"]
    again = ask_event(w, uid, "Q1", "first text")  # a re-ask with the same hash
    assert again["at"] > first["at"]
    (item,) = needs(w)["items"]
    assert item["since"] == first["at"] and item["text"] == "first text"  # does not restart
    changed = ask_event(w, uid, "Q1", "second text")  # a changed hash
    (item,) = needs(w)["items"]
    assert item["since"] == changed["at"] and item["text"] == "second text" and changed["at"] > again["at"]


def test_a_questions_who_is_its_addressees_or_the_owners_and_maintainers(w):
    uid, key = mk(w, "to quinn")
    agent(w, "ask", {"ref": key, "text": "quinn?", "text_question": True, "to": w.quinn.ref})
    agent(
        w, "ask", {"ref": key, "text": "role?", "text_question": True, "to": "reviewers"}
    )  # nobody holds it: owners and maintainers
    items = needs(w, {"person": w.quinn.ref})["items"]
    assert [(i["ref"], i["who"]) for i in items] == [("Q1", [w.quinn.ref])]
    items = needs(w)["items"]  # the owner (the viewer): the fallback only
    assert [(i["ref"], i["who"]) for i in items] == [("Q2", [w.lab.owner])]


def test_who_never_names_a_person_who_cannot_see_the_ticket(wq):
    uid, key = mk(wq, "restricted question")
    agent(wq, "ask", {"ref": key, "text": "owner or quinn?", "text_question": True, "to": wq.lab.owner})
    promote(wq)
    wq.restrict(uid, [wq.quinn.ref])  # the owner (an addressee) cannot see it
    model = model_needs(wq, uid)
    assert model[0].who == (wq.lab.owner,)  # the model names the owner
    (item,) = needs(wq)["items"]  # quinn, now a maintainer: the fallback to owners and maintainers who see it
    assert item["who"] == [wq.quinn.ref] and item["payload"]["to"] == wq.lab.owner  # the stored `to` is not `who`


# --- approve, no_eligible ------------------------------------------------------------------------------------------


def test_an_approve_item_has_payload_and_since_from_the_event_that_made_the_gate_wait(w):
    uid, key = mk(w, "gate")
    filled = on_store(w, lambda ws: ws.fill(uid)).event
    w.write(key, "unrelated later event")
    (item,) = needs(w)["items"]
    assert item == {
        "kind": "approve", "key": key, "uid": uid, "ref": "requirements", "detail": "", "who": [w.lab.owner],
        "since": filled["at"], "status": "open", "payload": {"gate": "requirements", "gen": 1, "count": 1, "have": 0},
    }  # fmt: skip
    assert "text" not in item


def test_approve_since_restarts_when_a_generation_raise_makes_the_gate_wait_again(w):
    uid, key = mk(w, "gate")
    on_store(w, lambda ws: ws.fill(uid))
    on_store(w, lambda ws: ws.approve(uid, "requirements"))
    assert needs(w)["items"] == []  # approved: not waiting any more
    edit = on_store(w, lambda ws: ws.update(uid, {}, {"requirements": "changed requirements"})).event
    (item,) = needs(w)["items"]
    assert (item["kind"], item["ref"]) == ("approve", "requirements")
    assert item["since"] == edit["at"] and item["payload"]["gen"] == 2  # the generation raise


def test_a_gate_nobody_visible_can_decide_is_no_eligible_with_no_visible_person(wq):
    uid, key = mk(wq, "hidden approver")
    on_store(wq, lambda ws: ws.fill(uid))
    promote(wq)
    filled_at = [e for e in wq.lab.ws.events(key) if e["type"] == "ticket.updated"][-1]["at"]
    wq.restrict(uid, [wq.quinn.ref])  # the only approver token (owner) is held by a person who cannot see it
    (item,) = needs(wq)["items"]
    assert [n.kind for n in model_needs(wq, uid)] == ["approve"]  # the model says approve; blocked stays False
    assert item["kind"] == "no_eligible" and item["ref"] == "requirements"
    assert item["payload"] == {"gate": "requirements", "cause": "no_visible_person"}
    assert item["who"] == [wq.quinn.ref]  # the ticket owner cannot see it either: owners and maintainers who can
    assert item["since"] == filled_at and item["status"] == "open" and "text" not in item
    view = wq.h.run(wq.service.call(lambda: wq.service.store.ticket(uid)))
    assert view.gates["requirements"].blocked is False  # GateView.blocked stays F1's token test


def test_a_gate_with_no_approver_token_is_no_eligible_with_no_token(w):
    uid, key = mk(w, "no token")
    on_store(w, lambda ws: ws.fill(uid))
    set_policy(w, "requirements", ["owner", "maintainer"])
    override(w, uid, "requirements", ["maintainer"])
    emptied = set_policy(w, "requirements", ["owner"])  # the intersection is empty now
    (item,) = needs(w)["items"]
    assert item["kind"] == "no_eligible" and item["payload"] == {"gate": "requirements", "cause": "no_token"}
    assert item["who"] == [w.lab.owner]  # the ticket owner (and workspace owners) who can see it
    assert item["since"] == emptied["at"] and item["detail"] == ""
    assert [n.kind for n in model_needs(w, uid)] == ["no_eligible"]
    view = w.h.run(w.service.call(lambda: w.service.store.ticket(uid)))
    assert view.gates["requirements"].blocked is True


def test_no_token_who_is_the_ticket_owner_and_the_owners_and_maintainers_who_see_it(wq):
    uid, key = mk(wq, "no token, quinn maintainer")
    on_store(wq, lambda ws: ws.fill(uid))
    promote(wq)
    set_policy(wq, "requirements", ["owner", "maintainer"])
    override(wq, uid, "requirements", ["maintainer"])
    set_policy(wq, "requirements", ["owner"])
    (item,) = needs(wq)["items"]
    assert item["payload"]["cause"] == "no_token" and item["who"] == sorted([wq.lab.owner, wq.quinn.ref])
    wq.restrict(uid, [wq.quinn.ref])
    (item,) = needs(wq)["items"]
    assert item["who"] == [wq.quinn.ref]  # the owner cannot see the ticket any more


# --- closed tickets (§0.8.4) ---------------------------------------------------------------------------------------


def test_a_closed_ticket_has_no_approve_but_keeps_its_open_question_with_status_closed(w):
    uid, key = mk(w, "closing")
    agent(w, "ask", {"ref": key, "text": "still open?", "text_question": True})
    on_store(w, lambda ws: ws.fill(uid))
    assert [i["kind"] for i in needs(w)["items"]] == ["question", "approve"]
    close_ticket(w, uid)
    items = needs(w)["items"]
    assert [(i["kind"], i["status"]) for i in items] == [("question", "closed")]
    w.person_event("ticket.reopened", uid)
    items = needs(w)["items"]
    assert items[0]["status"] != "closed" and items[0]["kind"] == "question"


# --- person, visibility, columns -----------------------------------------------------------------------------------


def test_an_owner_who_is_not_on_a_restricted_list_gets_none_of_xs_items_on_it(w):
    uid, key = mk(w, "for quinn")
    agent(w, "ask", {"ref": key, "text": "quinn?", "text_question": True, "to": w.quinn.ref})
    other_uid, other = mk(w, "also for quinn")
    agent(w, "ask", {"ref": other, "text": "quinn too?", "text_question": True, "to": w.quinn.ref})
    assert {i["key"] for i in needs(w, {"person": w.quinn.ref})["items"]} == {key, other}
    w.restrict(uid, [w.quinn.ref])  # quinn on the list, the owner (the caller) is not
    assert [i["key"] for i in needs(w, {"person": w.quinn.ref})["items"]] == [other]  # the one the owner may see
    assert key not in json.dumps(needs(w, {"person": w.quinn.ref}))
    w.restrict(other_uid, [w.lab.owner])  # the caller on the list, quinn (X) not: X's items on it are not returned
    assert needs(w, {"person": w.quinn.ref})["items"] == []
    assert key not in json.dumps(needs(w)) and other not in json.dumps(needs(w, {"person": w.quinn.ref}))


def test_a_member_naming_another_person_is_op_not_allowed(wq):
    c = wq.client()
    assert err(c.req("needs", {"person": wq.lab.owner})) == "op.not_allowed"  # a member naming the owner
    assert ok(c.req("needs", {"person": wq.quinn.ref}))  # naming oneself is fine


def test_g_and_n_naming_another_person_are_op_not_allowed_and_a_person_class_owner_may(w):
    assert err(w.client().req("needs", {"person": w.quinn.ref}, grant=w.grant, session=SESSION)) == "op.not_allowed"
    assert ok(w.client().req("needs", {"person": w.quinn.ref}))  # the owner at column P
    as_agent_class(w)
    assert err(w.client().req("needs", {"person": w.quinn.ref})) == "op.not_allowed"  # no-person: workspace items only


def test_needs_of_a_viewer_are_the_viewers_own_and_g_is_the_grants_person(w):
    _, a = mk(w, "owner's")
    _, b = mk(w, "quinn's")
    agent(w, "ask", {"ref": a, "text": "owner?", "text_question": True})
    agent(w, "ask", {"ref": b, "text": "quinn?", "text_question": True, "to": w.quinn.ref})
    assert [i["key"] for i in needs(w)["items"]] == [a]
    assert [i["key"] for i in needs(w, grant=w.grant, session=SESSION)["items"]] == [a]
    assert [i["key"] for i in needs(w, {"person": w.quinn.ref})["items"]] == [b]


def test_no_person_gets_items_on_workspace_tickets_and_none_on_restricted_ones(w):
    uid, key = mk(w, "open question")
    agent(w, "ask", {"ref": key, "text": "anyone?", "text_question": True, "to": w.quinn.ref})
    hidden_uid, hidden = mk(w, "zebracorn")
    agent(w, "ask", {"ref": hidden, "text": "zebracorn?", "text_question": True})
    w.restrict(hidden_uid, [w.lab.owner])
    as_agent_class(w)
    items = needs(w)["items"]  # column N: any `who` on workspace tickets (an ambiguity, documented in the module)
    assert [i["key"] for i in items] == [key] and items[0]["who"] == [w.quinn.ref]
    assert "zebracorn" not in json.dumps(needs(w)) and hidden not in json.dumps(needs(w))


# --- acknowledge and addon -----------------------------------------------------------------------------------------
# An unacknowledged invalid event cannot be produced through the host (the store refuses to append one and a foreign
# one stales the journal), so the two scopes are checked on a model need handed to the item builder, and the
# workspace item through a workspace view that carries one.


def fake_ctx(w: World) -> SimpleNamespace:
    store = w.service.store
    return SimpleNamespace(workspace=store.state.workspace, service=SimpleNamespace(journal=None), store=store)


def test_a_ticket_acknowledge_item_equals_the_model_need(w):
    uid, key = mk(w, "frozen")
    view = w.h.run(w.service.call(lambda: w.service.store.ticket(uid)))
    need = Need("acknowledge", uid, (w.lab.owner,), None, "invalid event in the ticket log")
    item = w.h.run(
        w.service.call(lambda: reads_query.need_item(fake_ctx(w), view, need, since={("acknowledge", None): "x"}))
    )
    assert item == {
        "kind": "acknowledge", "key": key, "uid": uid, "ref": None, "detail": "invalid event in the ticket log",
        "who": [w.lab.owner], "status": "open", "payload": {"scope": "ticket"},
    }  # fmt: skip
    assert "since" not in item and "text" not in item  # absent for acknowledge


def test_a_workspace_acknowledge_item_goes_only_to_a_viewer_in_its_who(w, monkeypatch):
    import dataclasses

    from orch.host.reads import ReadCtx

    real = ReadCtx.workspace.fget
    need = Need("acknowledge", None, (w.lab.owner,), None, "invalid event in the workspace log")
    monkeypatch.setattr(ReadCtx, "workspace", property(lambda self: dataclasses.replace(real(self), needs=(need,))))
    expected = {
        "kind": "acknowledge", "key": None, "uid": None, "ref": None, "detail": "invalid event in the workspace log",
        "who": [w.lab.owner], "payload": {"scope": "workspace"},
    }  # fmt: skip
    assert needs(w)["items"] == [expected]  # the owner is in `who`
    assert "status" not in expected and "since" not in expected
    assert needs(w, grant=w.grant, session=SESSION)["items"] == [expected]  # G: the grant's person is the owner
    assert needs(w, {"person": w.quinn.ref})["items"] == []  # quinn is not in `who`
    as_agent_class(w)
    assert needs(w)["items"] == []  # no-person: no workspace-level item


def test_a_workspace_acknowledge_item_is_not_returned_to_a_member_outside_its_who(wq, monkeypatch):
    import dataclasses

    from orch.host.reads import ReadCtx

    real = ReadCtx.workspace.fget
    need = Need("acknowledge", None, (wq.lab.owner,), None, "invalid event in the workspace log")
    monkeypatch.setattr(ReadCtx, "workspace", property(lambda self: dataclasses.replace(real(self), needs=(need,))))
    assert needs(wq)["items"] == []  # quinn (the caller) is not an owner


def test_an_addon_item_carries_the_rule_text_the_addon_and_the_rule(w):
    """The replayed model produces no ``addon`` need yet (``orch.addons.needs`` is not wired into it), so the
    conversion is checked on a model ``Need`` of that kind."""
    uid, key = mk(w, "addon rule")
    view = w.h.run(w.service.call(lambda: w.service.store.ticket(uid)))
    need = Need("addon", uid, (w.lab.owner, "p_" + "0" * 32), "linter.todo", "fix the lint findings")
    since = {("addon", "linter.todo"): "2026-09-21T14:00:00Z"}
    item = w.h.run(w.service.call(lambda: reads_query.need_item(fake_ctx(w), view, need, since=since)))
    assert item == {
        "kind": "addon", "key": key, "uid": uid, "ref": "linter.todo", "detail": "", "who": [w.lab.owner],
        "since": "2026-09-21T14:00:00Z", "text": "fix the lint findings", "status": "open",
        "payload": {"addon": "linter", "rule": "todo"},
    }  # fmt: skip
    assert [n.kind for n in model_needs(w, uid)] == []


# --- the cursor ----------------------------------------------------------------------------------------------------


def test_the_sub_cursor_of_needs_joined_to_sub_open_loses_and_repeats_nothing(w):
    _, key = mk(w, "live")
    agent(w, "ask", {"ref": key, "text": "q?", "text_question": True})
    c = w.conn()
    data = ok(c.call("needs", {}))["data"]
    cursor = data["sub_cursor"]
    assert cursor == w.snapshot_cursor(principal_of()) and len(data["items"]) == 1
    a = w.write(key, "after 1")
    b = w.write(key, "after 2")
    sub = c.open(["tickets"], after=cursor)
    items, _ = c.items_until(sub["sub"], jpos_of(cursor) + 2)
    assert [(i["type"], i["seq"]) for i in items] == [("log.added", a["seq"]), ("log.added", b["seq"])]


def test_needs_validation(w):
    c = w.client()
    for bad in ({"person": "quinn"}, {"x": 1}, {"person": 5}):
        assert err(c.req("needs", bad)) == "invalid.input", bad


def test_a_rich_question_carries_options_and_spec_verbatim_per_the_frozen_fixture(w):
    """#1123 B3: the needs payload holds the asked ``options`` and ``spec`` as given (every option key, no whitelist);
    a client hashes exactly ``{question_id, ticket, text, options, spec}`` and gets the event's hash."""
    for form in _FIXTURE:
        uid, key = mk(w, "rich")
        q = dict(form["raw_event"]["question"])
        qid = canon.question_id(w.wid, uid, q["id"])
        h = canon.question_hash(qid, uid, q)
        on_store(w, lambda ws, q=q, qid=qid, h=h, uid=uid: ws.store.append(
            {"type": "question.asked", "actor": ws.agent, "question": q, "qid": qid, "hash": h}, log=uid
        ))  # fmt: skip
        (item,) = [i for i in needs(w)["items"] if i["uid"] == uid]
        want = form["hub_needs"]["content"]
        payload = item["payload"]
        assert payload.get("options") == want.get("options") and payload["spec"] == want["spec"]
        assert item["text"] == want["text"]
        again = {**payload["spec"], "id": "Q1", "to": "ticket_owner", "blocking": True, "text": item["text"]}
        if payload.get("options"):
            again["options"] = payload["options"]
        assert canon.question_hash(qid, uid, again) == h
