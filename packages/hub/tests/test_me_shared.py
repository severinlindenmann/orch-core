"""P02 (d): GET /me/shared lists the shared TICKETS the CALLER may read, asked with the caller's own identity."""

import pytest
from test_webapi_core import _CHAIN, _f1
from webapi_helpers import WS, WS_SEL, Web, shared_item


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    x.reg.add(WS_SEL, "selective", owner=x.ann)
    x.ben = x.env.user("ben")
    x.shares.items[(x.ben, WS_SEL)] = [shared_item("11" * 16, "ticket", "Invoice export for Ben")]
    x.shares.items[(x.ann, WS_SEL)] = [
        shared_item("11" * 16, "ticket", "Invoice export for Ben"),
        shared_item("22" * 16, "needs_you", "A summary"),  # not a ticket: never listed here
    ]
    x.shares.items[(x.ann, WS_SEL)][0]["item"]["audience"] = {"users": [x.ben], "groups": ["finance"]}
    yield x
    x.close()


def get(w, user, query=""):
    return w.call("GET", "/me/shared" + query, token=w.login(user))


def test_a_granted_person_sees_who_shared_it_and_the_owner_sees_with_whom(w):
    s, o, _ = get(w, w.ben, "?workspace_id=" + WS_SEL)
    assert s == 200 and [i["title"] for i in o["items"]] == ["Invoice export for Ben"]
    assert o["items"][0]["shared_by"] == "ann" and "shared_with" not in o["items"][0]
    assert o["items"][0]["signable"] is False  # H2: a reader never signs
    s, o, _ = get(w, w.ann)
    assert [i["title"] for i in o["items"]] == ["Invoice export for Ben"]  # the needs_you summary is not a ticket
    assert o["items"][0]["workspace_id"] == WS_SEL
    assert o["items"][0]["shared_with"] == ["ben", "Team: finance"] and "shared_by" not in o["items"][0]
    w.shares.items[(w.ann, WS_SEL)][0]["item"].pop("audience")
    assert get(w, w.ann)[1]["items"][0]["shared_with"] == []  # shared with nobody: the page says "Only you"


def test_everyone_else_gets_nothing_not_even_an_error(w):
    carl = w.env.user("carl")
    assert get(w, carl, "?workspace_id=" + WS_SEL)[:2] == (200, {"items": []})
    assert get(w, w.admin)[:2] == (200, {"items": []})  # a hub admin has no part by role
    assert get(w, w.ben, "?workspace_id=" + WS)[:2] == (200, {"items": []})  # a full-mode workspace
    assert get(w, w.ben, "?workspace_id=" + "ee" * 16)[:2] == (200, {"items": []})  # unknown


def test_malformed_workspace_and_no_session(w):
    assert get(w, w.ben, "?workspace_id=zz")[:2] == (400, {"error": "malformed"})
    assert w.call("GET", "/me/shared")[0] == 401


def test_a_reader_never_gets_signable_on_the_single_item_route_either(w):
    ids = "11" * 16
    w.shares.items[(w.ben, WS_SEL)] = [shared_item(ids, "question", chain=_CHAIN, f1=_f1())]
    w.shares.items[(w.ann, WS_SEL)] = [shared_item(ids, "question", chain=_CHAIN, f1=_f1())]
    route = f"/workspaces/{WS_SEL}/items/{ids}"
    assert w.call("GET", route, token=w.login(w.ann))[1]["signable"] is True
    assert w.call("GET", route, token=w.login(w.ben))[1].get("signable", False) is False
