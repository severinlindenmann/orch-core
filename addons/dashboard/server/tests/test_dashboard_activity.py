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


def test_the_publish_approval_note_is_said_in_words_and_its_cut_text_is_not_shown():
    row = adapt.item(raw("log.added", summary="Publish approval: share files outside t\u2026"), {"p_1": "Ann"})
    assert row["did"] == "approved sharing files outside this computer" and "detail" not in row


def test_sentences_use_names_and_fall_back_to_a_general_one():
    names = {"p_2": "Bo"}
    added = adapt.item(raw("member.added", {"person": "p_2", "role": "maintainer"}, key=None), names)
    assert added["did"] == "added Bo as maintainer" and added["group"] == "workspace"
    future = adapt.item(raw("future.thing", key=None, actor={"kind": "host", "id": None}), names)
    assert future["did"] == "did something"
    agent = {"kind": "agent", "id": "claude-code", "for": "p_2"}
    status = adapt.item(raw("status.changed", {"from": "in_progress", "to": "done"}, actor=agent), names)
    assert status["did"] == "moved the ticket from in progress to done" and status["actor"]["for_name"] == "Bo"


def test_repo_changes_name_the_repo_and_other_setting_changes_stay_general():
    def did(data):
        return adapt.sentence({"type": "settings.changed", "data": data}, lambda p: "x")

    assert did({"keys": ["repos"], "repos": {"web": "set"}}) == "updated the repository “web”"
    assert (
        did({"keys": ["repos"], "repos": {"api": "removed", "web": "set"}})
        == "removed the repository “api”; updated the repository “web”"
    )
    assert did({"keys": ["claim_ttl_min"]}) == "changed the workspace settings"
    pushed = adapt.sentence({"type": "branch.pushed", "data": {"repo_name": "repo"}}, lambda p: "x")
    assert pushed == "saw the ticket's branch in the repository “repo”" and "pushed" not in pushed
    assert did({"keys": ["repos"], "repos": {"a‮b": "set"}}) == "changed the workspace settings"


def hub_row(person=None):
    actor = {"kind": "person", "id": "u_" + "a3" * 16, "via": "c" * 32, "auth": "hub"}
    if person:
        actor["person"] = person
    return {
        "id": "1.e", "at": "2026-10-23T10:00:00Z", "type": "log.added", "log": "t", "uid": "t", "key": "DEMO-0001",
        "title": "A ticket", "seq": 3, "ws_seq": 2, "actor": actor, "summary": "hi", "data": {"text": "hi"},
        "channel_factor": "se-unlocked", "hub_id": "d" * 32,
    }  # fmt: skip


def test_a_hub_signed_row_names_the_member_or_the_hub_user_and_the_way_it_was_signed():
    """Ticket-format 15.4 views: the person (the member it is aliased to, else `Hub user <first 8>`), then `via hub`
    and the factor in words; the raw factor label, the hub id and the channel id never reach the browser."""
    mara = "p_" + "4c" * 16
    named = adapt.item(hub_row(person=mara), {mara: "Mara"})
    assert named["actor"]["name"] == "Mara via hub · phone, unlocked"
    anon = adapt.item(hub_row(), {})
    assert anon["actor"]["name"] == "Hub user a3a3a3a3 via hub · phone, unlocked"
    flat = str(named)
    assert "se-unlocked" not in flat and "d" * 32 not in flat and "c" * 32 not in flat
