"""The Activity page's read: plain-word rows over `activity.list`, closed query, hidden text withheld."""

import pytest
from conftest import ORIGIN

from orch_dashboard.adapters import activity as adapt

ROW_KEYS = {"id", "at", "type", "group", "actor", "did", "ticket", "title", "title_hidden", "detail", "detail_hidden"}


def headers(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


def get(client, token, stub, query=""):
    return client.get(f"/api/w/{stub.ws}/activity{query}", headers=headers(token))


def test_activity_needs_authentication(client, stub):
    assert client.get(f"/api/w/{stub.ws}/activity").status_code == 401


def test_activity_rows_are_plain_and_carry_no_host_internals(client, token, stub, rich):
    res = get(client, token, stub, "?limit=100")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["available"] is True and body["items"]
    for row in body["items"]:
        assert set(row) <= ROW_KEYS
        assert row["did"]
    assert any(r["ticket"] == "DEMO-0001" and r["title"] for r in body["items"])
    assert any(r["actor"]["kind"] == "person" and r["actor"]["name"] for r in body["items"])


def test_activity_filters_reach_the_host_as_fixed_arguments(client, token, stub, rich):
    from orch_dashboard.activity_routes import ActivityQuery, host_args

    assert host_args(ActivityQuery(group="workspace")) == {"limit": 50, "order": "desc", "scope": "workspace"}
    reviews = host_args(ActivityQuery(group="reviews", ticket="DEMO-0001", who="agents"))
    assert reviews["types"] == adapt.GROUP_TYPES["reviews"] and reviews["ticket"] == "DEMO-0001"
    assert host_args(ActivityQuery(who="agents"))["actor_kind"] == ["agent"]
    person = "p_" + "a" * 32
    assert host_args(ActivityQuery(who=person, page="abc"))["person"] == person
    got = get(client, token, stub, "?group=reviews")
    assert got.status_code == 200
    assert all(r["group"] == "reviews" for r in got.json()["items"])


@pytest.mark.parametrize(
    "query",
    ["?group=nope", "?limit=0", "?limit=101", "?who=anyone", "?ticket=bad", "?page=a%20b", "?extra=1", "?types=x"],
)
def test_activity_query_is_closed(client, token, stub, rich, query):
    assert get(client, token, stub, query).status_code == 400


def test_activity_pages_follow_next_page(client, token, stub, rich):
    seen, page = [], None
    for _ in range(40):
        res = get(client, token, stub, "?limit=2" + (f"&page={page}" if page else ""))
        assert res.status_code == 200, res.text
        body = res.json()
        seen += [r["id"] for r in body["items"]]
        page = body["next_page"]
        if not page:
            break
    assert len(seen) > 2 and len(set(seen)) == len(seen)


def raw(kind, data=None, *, key="DEMO-0001", actor=None, **more):
    return {
        "id": "1.e", "key": key, "title": "t", "type": kind, "at": "2026-10-18T09:00:00Z",
        "actor": actor or {"kind": "person", "id": "p_1"}, "summary": "", "data": data or {}, **more,
    }  # fmt: skip


def test_hidden_text_is_withheld_never_repaired():
    row = adapt.item(raw("log.added", title="Fix\u202eit", summary="a\u200bb"), {"p_1": "Ann"})
    assert row["title"] is None and row["title_hidden"] is True
    assert row["detail"] is None and row["detail_hidden"] is True
    assert row["actor"] == {"kind": "person", "id": "p_1", "name": "Ann"} and row["did"] == "wrote a note"


def test_sentences_use_names_and_fall_back_to_a_general_one():
    names = {"p_2": "Bo"}
    added = adapt.item(raw("member.added", {"person": "p_2", "role": "maintainer"}, key=None), names)
    assert added["did"] == "added Bo as maintainer" and added["group"] == "workspace"
    future = adapt.item(raw("future.thing", key=None, actor={"kind": "host", "id": None}), names)
    assert future["did"] == "did something"
    agent = {"kind": "agent", "id": "claude-code", "for": "p_2"}
    status = adapt.item(raw("status.changed", {"from": "in_progress", "to": "done"}, actor=agent), names)
    assert status["did"] == "moved the ticket from in progress to done" and status["actor"]["for_name"] == "Bo"
